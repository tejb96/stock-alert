#!/usr/bin/env python3
"""Stateless cron: gold/silver ratio, top 3 ApeWisdom trends, Yahoo quotes → Discord."""

from __future__ import annotations

import asyncio
import os
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx

YAHOO_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
GOLD_SYMBOL = "GC=F"
SILVER_SYMBOL = "SI=F"
APEWISDOM_STOCKS_URL = "https://apewisdom.io/api/v1.0/filter/all-stocks"

TOP_TRENDS = 3


class NotifyError(Exception):
    pass


@dataclass(frozen=True)
class RatioQuote:
    gold_price: float
    silver_price: float
    ratio: float
    market_state: str


@dataclass(frozen=True)
class TrendRow:
    ticker: str
    rank: int
    mentions: int
    upvotes: int
    change_24h: float | None
    trend_score: float


@dataclass(frozen=True)
class StockQuote:
    ticker: str
    price: float
    change_pct: float | None
    fifty_two_week_low: float | None
    fifty_two_week_high: float | None


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return int(raw)


def compute_change_24h(mentions: int, mentions_24h_ago: int | None) -> float | None:
    if mentions_24h_ago is None or mentions_24h_ago <= 0:
        return None
    return ((mentions - mentions_24h_ago) / mentions_24h_ago) * 100


def compute_trend_score(mentions: int, change_24h: float | None) -> float:
    growth = max(change_24h or 0.0, 0.0)
    return mentions * (1 + growth / 100)


def _parse_apewisdom_row(raw: dict[str, Any]) -> TrendRow | None:
    ticker_raw = raw.get("ticker")
    if not ticker_raw or not isinstance(ticker_raw, str):
        return None

    try:
        rank = int(raw["rank"])
        mentions = int(raw["mentions"])
        upvotes = int(raw["upvotes"])
    except (KeyError, TypeError, ValueError):
        return None

    mentions_24h_raw = raw.get("mentions_24h_ago")
    mentions_24h_ago: int | None
    if mentions_24h_raw is None:
        mentions_24h_ago = None
    else:
        try:
            mentions_24h_ago = int(mentions_24h_raw)
        except (TypeError, ValueError):
            mentions_24h_ago = None

    ticker = ticker_raw.strip().upper()
    change_24h = compute_change_24h(mentions, mentions_24h_ago)
    return TrendRow(
        ticker=ticker,
        rank=rank,
        mentions=mentions,
        upvotes=upvotes,
        change_24h=change_24h,
        trend_score=compute_trend_score(mentions, change_24h),
    )


def enrich_and_rank(rows: list[TrendRow], *, min_mentions: int, top_n: int = TOP_TRENDS) -> list[TrendRow]:
    eligible = [row for row in rows if row.mentions >= min_mentions]
    ranked = sorted(eligible, key=lambda r: r.trend_score, reverse=True)
    return ranked[:top_n]


def _extract_yahoo_price(payload: dict[str, Any]) -> tuple[float, str]:
    result = payload.get("chart", {}).get("result")
    if not result:
        raise NotifyError("Yahoo chart response missing result")

    meta = result[0].get("meta", {})
    price = meta.get("regularMarketPrice")
    if price is None:
        price = meta.get("previousClose")
    if price is None:
        raise NotifyError("Yahoo chart response missing price")

    market_state = str(meta.get("marketState", "unknown"))
    return float(price), market_state


def _extract_stock_quote(ticker: str, payload: dict[str, Any]) -> StockQuote:
    result = payload.get("chart", {}).get("result")
    if not result:
        raise NotifyError(f"Yahoo chart response missing result for {ticker}")

    meta = result[0].get("meta", {})
    price = meta.get("regularMarketPrice")
    if price is None:
        price = meta.get("previousClose")
    if price is None:
        raise NotifyError(f"Yahoo chart response missing price for {ticker}")

    change_pct = meta.get("regularMarketChangePercent")
    if change_pct is not None:
        change_pct = float(change_pct)

    low = meta.get("fiftyTwoWeekLow")
    high = meta.get("fiftyTwoWeekHigh")

    return StockQuote(
        ticker=ticker,
        price=float(price),
        change_pct=change_pct,
        fifty_two_week_low=float(low) if low is not None else None,
        fifty_two_week_high=float(high) if high is not None else None,
    )


def _format_change(change_24h: float | None) -> str:
    if change_24h is None:
        return "n/a"
    sign = "+" if change_24h >= 0 else ""
    return f"{sign}{change_24h:.1f}%"


def _format_trend_block(index: int, row: TrendRow) -> str:
    return (
        f"**{index}. {row.ticker}** · Rank #{row.rank} · "
        f"Mentions **{row.mentions}** · 24h **{_format_change(row.change_24h)}** · "
        f"Score **{row.trend_score:.1f}**"
    )


def _format_stock_quote_line(quote: StockQuote) -> str:
    change = "n/a"
    if quote.change_pct is not None:
        sign = "+" if quote.change_pct >= 0 else ""
        change = f"{sign}{quote.change_pct:.1f}%"

    range_part = ""
    if quote.fifty_two_week_low is not None and quote.fifty_two_week_high is not None:
        range_part = f" · 52w ${quote.fifty_two_week_low:.2f}–${quote.fifty_two_week_high:.2f}"

    return f"**{quote.ticker}** · ${quote.price:.2f} ({change}){range_part}"


def build_message(
    quote: RatioQuote,
    trends: list[TrendRow],
    stock_quotes: list[StockQuote],
    *,
    when: datetime | None = None,
) -> str:
    ts = when or datetime.now(UTC)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    else:
        ts = ts.astimezone(UTC)

    lines = [
        f"**Stock alert digest** · {ts.strftime('%Y-%m-%d %H:%M')} UTC",
        "",
        "**Gold/Silver ratio**",
        (
            f"Ratio **{quote.ratio:.2f}** · "
            f"Gold ${quote.gold_price:,.2f}/oz · Silver ${quote.silver_price:,.2f}/oz · "
            f"Market: {quote.market_state}"
        ),
    ]

    if trends:
        lines.extend(
            [
                "",
                "**Top trends** (apewisdom · by trend score)",
                *[_format_trend_block(i, row) for i, row in enumerate(trends, start=1)],
            ]
        )

    if stock_quotes:
        lines.extend(
            [
                "",
                "**Yahoo quotes**",
                *[_format_stock_quote_line(q) for q in stock_quotes],
            ]
        )

    return "\n".join(lines)


async def fetch_ratio(client: httpx.AsyncClient) -> RatioQuote:
    params = {"interval": "1m", "range": "1d"}
    gold_resp, silver_resp = await asyncio.gather(
        client.get(YAHOO_CHART_URL.format(symbol=GOLD_SYMBOL), params=params),
        client.get(YAHOO_CHART_URL.format(symbol=SILVER_SYMBOL), params=params),
    )
    gold_resp.raise_for_status()
    silver_resp.raise_for_status()

    gold_price, gold_market = _extract_yahoo_price(gold_resp.json())
    silver_price, silver_market = _extract_yahoo_price(silver_resp.json())

    if silver_price <= 0:
        raise NotifyError("Invalid silver price")

    market_state = gold_market if gold_market == silver_market else f"{gold_market}/{silver_market}"
    return RatioQuote(
        gold_price=gold_price,
        silver_price=silver_price,
        ratio=gold_price / silver_price,
        market_state=market_state,
    )


async def fetch_apewisdom(client: httpx.AsyncClient, *, top_n: int) -> list[TrendRow]:
    response = await client.get(APEWISDOM_STOCKS_URL)
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise NotifyError("ApeWisdom response is not a JSON object")

    results = payload.get("results")
    if not isinstance(results, list):
        raise NotifyError("ApeWisdom response missing results list")

    rows: list[TrendRow] = []
    for raw in results[:top_n]:
        if not isinstance(raw, dict):
            continue
        row = _parse_apewisdom_row(raw)
        if row is not None:
            rows.append(row)

    if not rows:
        raise NotifyError("No valid ticker rows in ApeWisdom response")

    return rows


async def fetch_stock_quote(client: httpx.AsyncClient, ticker: str) -> StockQuote | None:
    params = {"interval": "1d", "range": "1d"}
    try:
        response = await client.get(YAHOO_CHART_URL.format(symbol=ticker), params=params)
        response.raise_for_status()
        return _extract_stock_quote(ticker, response.json())
    except (NotifyError, httpx.HTTPError):
        return None


async def send_discord(client: httpx.AsyncClient, content: str) -> None:
    webhook_url = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    if not webhook_url:
        raise NotifyError("DISCORD_WEBHOOK_URL is not configured")

    response = await client.post(webhook_url, json={"content": content})
    if response.status_code >= 400:
        raise NotifyError(f"Discord webhook failed: {response.status_code} {response.text}")


async def run() -> None:
    min_mentions = _env_int("MIN_MENTIONS", 20)
    apewisdom_top_n = _env_int("APEWISDOM_TOP_N", 50)
    user_agent = _env("YAHOO_USER_AGENT", "stock_alert-cron/1.0")

    headers = {"User-Agent": user_agent}
    async with httpx.AsyncClient(timeout=20.0, headers=headers) as client:
        ratio = await fetch_ratio(client)
        raw_trends = await fetch_apewisdom(client, top_n=apewisdom_top_n)
        top_trends = enrich_and_rank(raw_trends, min_mentions=min_mentions, top_n=TOP_TRENDS)

        stock_quotes: list[StockQuote] = []
        for trend in top_trends:
            quote = await fetch_stock_quote(client, trend.ticker)
            if quote is not None:
                stock_quotes.append(quote)

        content = build_message(ratio, top_trends, stock_quotes)
        await send_discord(client, content)


def main() -> int:
    try:
        asyncio.run(run())
    except NotifyError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except httpx.HTTPError as exc:
        print(f"http error: {exc}", file=sys.stderr)
        return 1

    print("Discord notification sent.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
