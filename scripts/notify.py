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
GITHUB_MODELS_URL_DEFAULT = "https://models.github.ai/inference/chat/completions"
RESEARCH_MARKER = "**Research —"
DISCORD_CONTENT_LIMIT = 2000

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


@dataclass(frozen=True)
class NewsHeadline:
    title: str
    url: str
    source: str
    body: str | None = None


@dataclass(frozen=True)
class TickerResearch:
    ticker: str
    summary: str
    headlines: list[NewsHeadline]


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return int(raw)


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


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


def _fetch_news_sync(ticker: str, max_results: int) -> list[NewsHeadline]:
    from ddgs import DDGS

    rows = DDGS().news(
        query=f"{ticker} stock",
        region="us-en",
        timelimit="w",
        max_results=max_results,
    )
    headlines: list[NewsHeadline] = []
    for row in rows:
        title = row.get("title")
        url = row.get("url")
        if not title or not url:
            continue
        source = str(row.get("source") or "unknown")
        body_raw = row.get("body")
        body = str(body_raw) if body_raw else None
        headlines.append(
            NewsHeadline(
                title=str(title).strip(),
                url=str(url).strip(),
                source=source.strip(),
                body=body,
            )
        )
    return headlines


async def fetch_news_headlines(ticker: str, *, max_results: int) -> list[NewsHeadline]:
    try:
        return await asyncio.to_thread(_fetch_news_sync, ticker, max_results)
    except Exception as exc:
        print(f"research: news fetch failed for {ticker}: {exc}", file=sys.stderr)
        return []


def _build_research_prompt(
    ticker: str,
    trend: TrendRow,
    headlines: list[NewsHeadline],
) -> list[dict[str, str]]:
    change_str = _format_change(trend.change_24h)
    headline_lines: list[str] = []
    for index, headline in enumerate(headlines, start=1):
        snippet = f" — {headline.body}" if headline.body else ""
        headline_lines.append(f"{index}. {headline.title} ({headline.source}){snippet}")

    user_content = (
        f"Ticker: {ticker}\n"
        f"Reddit (ApeWisdom): rank #{trend.rank}, {trend.mentions} mentions, "
        f"{change_str} mentions vs 24h ago, trend score {trend.trend_score:.1f}\n\n"
        f"Recent headlines:\n"
        f"{chr(10).join(headline_lines)}\n\n"
        "In 2-3 sentences, explain why this ticker is likely trending on Reddit right now.\n"
        "Use only the headlines and stats above. No investment advice."
    )

    return [
        {
            "role": "system",
            "content": (
                "You write brief market context blurbs for a Discord digest. "
                "Be factual. No price targets or buy/sell advice. "
                "2-3 sentences max (~400 chars)."
            ),
        },
        {"role": "user", "content": user_content},
    ]


def _headline_fallback_summary(headlines: list[NewsHeadline]) -> str:
    bullets = [f"• {h.title} ({h.source})" for h in headlines[:3]]
    return "Recent headlines:\n" + "\n".join(bullets)


async def summarize_with_github_models(
    client: httpx.AsyncClient,
    ticker: str,
    trend: TrendRow,
    headlines: list[NewsHeadline],
) -> str | None:
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if not token:
        return None

    model = _env("RESEARCH_MODEL", "openai/gpt-4o-mini")
    url = _env("GITHUB_MODELS_URL", GITHUB_MODELS_URL_DEFAULT)
    messages = _build_research_prompt(ticker, trend, headlines)

    try:
        response = await client.post(
            url,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            json={
                "model": model,
                "messages": messages,
                "max_tokens": 200,
                "temperature": 0.3,
            },
            timeout=30.0,
        )
        if response.status_code >= 400:
            print(
                f"research: GitHub Models failed for {ticker}: "
                f"{response.status_code} {response.text}",
                file=sys.stderr,
            )
            return None

        payload = response.json()
        choices = payload.get("choices")
        if not choices:
            print(f"research: GitHub Models empty choices for {ticker}", file=sys.stderr)
            return None

        message = choices[0].get("message", {})
        content = message.get("content")
        if not content or not isinstance(content, str):
            print(f"research: GitHub Models missing content for {ticker}", file=sys.stderr)
            return None

        return content.strip()
    except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
        print(f"research: GitHub Models error for {ticker}: {exc}", file=sys.stderr)
        return None


async def summarize_research(
    client: httpx.AsyncClient,
    ticker: str,
    trend: TrendRow,
    headlines: list[NewsHeadline],
) -> str:
    summary = await summarize_with_github_models(client, ticker, trend, headlines)
    if summary:
        return summary
    return _headline_fallback_summary(headlines)


def _format_research_block(research: TickerResearch) -> str:
    lines = [
        f"**Research — {research.ticker}** (top trend)",
        research.summary,
    ]
    if research.headlines:
        lines.append("")
        lines.append("**Sources**")
        for headline in research.headlines:
            lines.append(f"• [{headline.title}]({headline.url}) — {headline.source}")
    return "\n".join(lines)


def truncate_discord_content(content: str, *, limit: int = DISCORD_CONTENT_LIMIT) -> str:
    if len(content) <= limit:
        return content

    marker_idx = content.find(RESEARCH_MARKER)
    if marker_idx < 0:
        return content[: limit - 1] + "…"

    prefix = content[:marker_idx].rstrip()
    if len(prefix) >= limit:
        return prefix[: limit - 1] + "…"

    research = content[marker_idx:]
    joiner = "\n\n" if prefix else ""
    base_len = len(prefix) + len(joiner)

    def fits(part: str) -> bool:
        return base_len + len(part) <= limit

    if fits(research):
        return prefix + joiner + research

    sources_marker = "\n\n**Sources**"
    sources_idx = research.find(sources_marker)
    if sources_idx >= 0:
        without_sources = research[:sources_idx]
        if fits(without_sources):
            return prefix + joiner + without_sources

        header_end = research.find("\n")
        if header_end >= 0:
            header = research[: header_end + 1]
            summary = research[header_end + 1 : sources_idx].strip()
            available = limit - base_len - len(header) - 1
            if available > 0:
                if len(summary) > available:
                    summary = summary[: available - 1] + "…"
                trimmed = header + summary
                if fits(trimmed):
                    return prefix + joiner + trimmed

    truncated = research[: limit - base_len - 1] + "…"
    return prefix + joiner + truncated


def build_message(
    quote: RatioQuote,
    trends: list[TrendRow],
    stock_quotes: list[StockQuote],
    *,
    research: TickerResearch | None = None,
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

    if research is not None:
        lines.extend(["", _format_research_block(research)])

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

        research: TickerResearch | None = None
        if top_trends and _env_bool("ENABLE_RESEARCH", True):
            top = top_trends[0]
            max_news = _env_int("RESEARCH_NEWS_COUNT", 5)
            headlines = await fetch_news_headlines(top.ticker, max_results=max_news)
            if headlines:
                summary = await summarize_research(client, top.ticker, top, headlines)
                research = TickerResearch(top.ticker, summary, headlines[:3])

        content = build_message(ratio, top_trends, stock_quotes, research=research)
        content = truncate_discord_content(content)
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
