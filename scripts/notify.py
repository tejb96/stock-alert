#!/usr/bin/env python3
"""Stateless cron: accelerating ApeWisdom tickers + Yahoo price/volume context → Discord embeds."""

from __future__ import annotations

import asyncio
import math
import statistics
import os
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx

YAHOO_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
YAHOO_QUOTE_PAGE_URL = "https://finance.yahoo.com/quote/{symbol}"
APEWISDOM_PAGE_URL = "https://apewisdom.io/api/v1.0/filter/all-stocks/page/{page}"
GITHUB_MODELS_URL_DEFAULT = "https://models.github.ai/inference/chat/completions"

# Discord embed limits: https://discord.com/developers/docs/resources/message#embed-object-embed-limits
EMBED_TITLE_LIMIT = 256
EMBED_DESCRIPTION_LIMIT = 4096
EMBED_FIELD_VALUE_LIMIT = 1024
EMBED_TOTAL_LIMIT = 6000
EMBEDS_PER_MESSAGE = 10

# How many ranked candidates to price-check per final slot; junk (ETFs, pennies,
# non-tickers Yahoo doesn't know) gets dropped, so we over-fetch.
CANDIDATE_MULTIPLIER = 3
RELVOL_LOOKBACK_DAYS = 20
# Floor for the intraday volume projection so an early-session run isn't wildly extrapolated.
MIN_SESSION_FRACTION = 0.15

STAGE_EARLY = "early"
STAGE_MOVING = "moving"
STAGE_LATE = "late"

# Moves are judged against each stock's own typical daily swing (stdev of daily % returns),
# so a 4% day counts as big for MCD but noise for a small cap.
MIN_VOLATILITY_SAMPLES = 10
MOVING_SIGMA = 1.5
LATE_SIGMA = 3.0
# Price already up this much → the move has likely happened (chasing), whatever the volatility.
LATE_CHANGE_1D = 15.0
LATE_CHANGE_5D = 30.0
# Fixed fallback when there isn't enough history to measure volatility.
MOVING_CHANGE_1D = 5.0
MOVING_CHANGE_5D = 10.0

STAGE_STYLE = {
    STAGE_EARLY: ("🟢", "Early", 0x2ECC71),
    STAGE_MOVING: ("🟡", "Moving", 0xF1C40F),
    STAGE_LATE: ("🔴", "Late / chasing", 0xE74C3C),
    None: ("⚪", "No price data", 0x95A5A6),
}


class NotifyError(Exception):
    pass


@dataclass(frozen=True)
class TrendRow:
    ticker: str
    name: str
    rank: int
    rank_24h_ago: int | None
    mentions: int
    mentions_24h_ago: int | None
    upvotes: int
    change_24h: float | None
    trend_score: float


@dataclass(frozen=True)
class StockQuote:
    ticker: str
    price: float
    change_1d: float | None
    change_5d: float | None
    rel_volume: float | None
    fifty_two_week_low: float | None
    fifty_two_week_high: float | None
    instrument_type: str | None = None
    daily_volatility: float | None = None

    def sigma_1d(self) -> float | None:
        if self.change_1d is None or not self.daily_volatility:
            return None
        return self.change_1d / self.daily_volatility

    def sigma_5d(self) -> float | None:
        if self.change_5d is None or not self.daily_volatility:
            return None
        return self.change_5d / (self.daily_volatility * math.sqrt(5))


@dataclass(frozen=True)
class Signal:
    trend: TrendRow
    quote: StockQuote
    stage: str | None


@dataclass(frozen=True)
class NewsHeadline:
    title: str
    url: str
    source: str
    body: str | None = None


@dataclass(frozen=True)
class TickerResearch:
    ticker: str
    summary: str | None
    headlines: list[NewsHeadline]


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return int(raw)


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return float(raw)


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def compute_change_24h(mentions: int, mentions_24h_ago: int | None) -> float | None:
    if mentions_24h_ago is None or mentions_24h_ago <= 0:
        return None
    return ((mentions - mentions_24h_ago) / mentions_24h_ago) * 100


def compute_trend_score(
    mentions: int,
    mentions_24h_ago: int | None,
    rank: int,
    rank_24h_ago: int | None,
) -> float:
    """Reward acceleration, not size.

    acceleration = doublings in mentions over 24h (a ticker with no prior mentions counts from 0)
    climb        = doublings in rank position over 24h (rank 200 → 25 is 3 doublings)
    score        = log2(1 + mentions) × (acceleration + 0.5 × climb)

    log2(mentions) keeps a 3 → 40 spike ahead of a 900 → 950 mega-cap while still
    preferring 40 → 400 over 3 → 30. Declines contribute nothing rather than going negative.
    """
    acceleration = max(math.log2((mentions + 1) / ((mentions_24h_ago or 0) + 1)), 0.0)
    climb = 0.0
    if rank_24h_ago is not None and rank > 0 and rank_24h_ago > 0:
        climb = max(math.log2(rank_24h_ago / rank), 0.0)
    return math.log2(1 + mentions) * (acceleration + 0.5 * climb)


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

    mentions_24h_ago = _optional_int(raw.get("mentions_24h_ago"))
    rank_24h_ago = _optional_int(raw.get("rank_24h_ago"))
    return TrendRow(
        ticker=ticker_raw.strip().upper(),
        name=str(raw.get("name") or "").strip(),
        rank=rank,
        rank_24h_ago=rank_24h_ago,
        mentions=mentions,
        mentions_24h_ago=mentions_24h_ago,
        upvotes=upvotes,
        change_24h=compute_change_24h(mentions, mentions_24h_ago),
        trend_score=compute_trend_score(mentions, mentions_24h_ago, rank, rank_24h_ago),
    )


def enrich_and_rank(rows: list[TrendRow], *, min_mentions: int, top_n: int) -> list[TrendRow]:
    eligible = [row for row in rows if row.mentions >= min_mentions and row.trend_score > 0]
    ranked = sorted(eligible, key=lambda r: r.trend_score, reverse=True)
    return ranked[:top_n]


def _pct_change(new: float, old: float | None) -> float | None:
    if old is None or old <= 0:
        return None
    return (new / old - 1) * 100


def _session_fraction(meta: dict[str, Any], now: float) -> float | None:
    """Fraction of today's regular session elapsed, or None when outside it."""
    regular = meta.get("currentTradingPeriod", {}).get("regular", {})
    start = regular.get("start")
    end = regular.get("end")
    if not isinstance(start, (int, float)) or not isinstance(end, (int, float)) or end <= start:
        return None
    if not start <= now < end:
        return None
    return max((now - start) / (end - start), MIN_SESSION_FRACTION)


def _extract_stock_quote(ticker: str, payload: dict[str, Any], *, now: float | None = None) -> StockQuote:
    result = payload.get("chart", {}).get("result")
    if not result:
        raise NotifyError(f"Yahoo chart response missing result for {ticker}")

    meta = result[0].get("meta", {})
    price = meta.get("regularMarketPrice")
    if price is None:
        price = meta.get("previousClose")
    if price is None:
        raise NotifyError(f"Yahoo chart response missing price for {ticker}")
    price = float(price)

    quote_series = (result[0].get("indicators", {}).get("quote") or [{}])[0]
    closes = [float(c) for c in quote_series.get("close") or [] if c is not None]
    # The last daily bar is the current (or most recent) session, which regularMarketVolume covers.
    prior_volumes = [float(v) for v in (quote_series.get("volume") or [])[:-1] if v]
    prior_volumes = prior_volumes[-RELVOL_LOOKBACK_DAYS:]

    change_1d = meta.get("regularMarketChangePercent")
    if change_1d is not None:
        change_1d = float(change_1d)
    elif len(closes) >= 2:
        change_1d = _pct_change(price, closes[-2])

    change_5d = _pct_change(price, closes[-6]) if len(closes) >= 6 else None

    # Exclude the current session so today's move doesn't inflate its own yardstick.
    history = closes[:-1]
    returns = [(b / a - 1) * 100 for a, b in zip(history, history[1:]) if a > 0]
    daily_volatility = statistics.stdev(returns) if len(returns) >= MIN_VOLATILITY_SAMPLES else None

    rel_volume: float | None = None
    volume = meta.get("regularMarketVolume")
    if volume and prior_volumes:
        fraction = _session_fraction(meta, time.time() if now is None else now)
        projected = float(volume) / fraction if fraction else float(volume)
        rel_volume = projected / (sum(prior_volumes) / len(prior_volumes))

    low = meta.get("fiftyTwoWeekLow")
    high = meta.get("fiftyTwoWeekHigh")
    instrument_type = meta.get("instrumentType")

    return StockQuote(
        ticker=ticker,
        price=price,
        change_1d=change_1d,
        change_5d=change_5d,
        rel_volume=rel_volume,
        fifty_two_week_low=float(low) if low is not None else None,
        fifty_two_week_high=float(high) if high is not None else None,
        instrument_type=str(instrument_type) if instrument_type else None,
        daily_volatility=daily_volatility,
    )


def classify_stage(quote: StockQuote) -> str | None:
    """Has the price already reacted to the buzz?"""
    c1, c5 = quote.change_1d, quote.change_5d
    if c1 is None and c5 is None:
        return None
    if (c1 is not None and c1 >= LATE_CHANGE_1D) or (c5 is not None and c5 >= LATE_CHANGE_5D):
        return STAGE_LATE

    if quote.daily_volatility:
        z1, z5 = quote.sigma_1d(), quote.sigma_5d()
        if (z1 is not None and z1 >= LATE_SIGMA) or (z5 is not None and z5 >= LATE_SIGMA):
            return STAGE_LATE
        if (z1 is not None and abs(z1) >= MOVING_SIGMA) or (z5 is not None and abs(z5) >= MOVING_SIGMA):
            return STAGE_MOVING
        return STAGE_EARLY

    if (c1 is not None and abs(c1) >= MOVING_CHANGE_1D) or (c5 is not None and abs(c5) >= MOVING_CHANGE_5D):
        return STAGE_MOVING
    return STAGE_EARLY


def is_tradeable(quote: StockQuote, *, min_price: float) -> bool:
    if quote.price < min_price:
        return False
    return quote.instrument_type in (None, "EQUITY")


def build_signals(
    candidates: list[TrendRow],
    quotes: dict[str, StockQuote],
    *,
    min_price: float,
    top_n: int,
) -> list[Signal]:
    signals: list[Signal] = []
    for trend in candidates:
        quote = quotes.get(trend.ticker)
        # No Yahoo quote usually means ApeWisdom matched a word, not a real ticker.
        if quote is None or not is_tradeable(quote, min_price=min_price):
            continue
        signals.append(Signal(trend, quote, classify_stage(quote)))
        if len(signals) >= top_n:
            break
    return signals


def _format_pct(value: float | None) -> str:
    if value is None:
        return "n/a"
    sign = "+" if value >= 0 else ""
    return f"{sign}{value:.1f}%"


def _format_mentions_line(trend: TrendRow) -> str:
    if trend.mentions_24h_ago:
        mentions = (
            f"{trend.mentions_24h_ago} → **{trend.mentions}** mentions "
            f"({_format_pct(trend.change_24h)})"
        )
    else:
        mentions = f"**{trend.mentions}** mentions (new in 24h)"
    rank = f"rank #{trend.rank}"
    if trend.rank_24h_ago is not None:
        rank = f"rank #{trend.rank_24h_ago} → **#{trend.rank}**"
    return f"📈 Reddit: {mentions} · {rank}"


def _format_price_line(quote: StockQuote) -> str:
    day = f"1d {_format_pct(quote.change_1d)}"
    z1 = quote.sigma_1d()
    if z1 is not None and abs(z1) >= 1:
        day += f" ({abs(z1):.1f}× usual)"
    parts = [
        f"**${quote.price:,.2f}**",
        day,
        f"5d {_format_pct(quote.change_5d)}",
    ]
    if quote.rel_volume is not None:
        parts.append(f"RelVol **{quote.rel_volume:.1f}×**")
    line = "💵 " + " · ".join(parts)
    if quote.fifty_two_week_low is not None and quote.fifty_two_week_high is not None:
        line += f"\n📊 52w ${quote.fifty_two_week_low:,.2f} – ${quote.fifty_two_week_high:,.2f}"
    return line


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _embed_size(embed: dict[str, Any]) -> int:
    size = len(embed.get("title", "")) + len(embed.get("description", ""))
    size += len(embed.get("footer", {}).get("text", ""))
    for field in embed.get("fields", []):
        size += len(field["name"]) + len(field["value"])
    return size


def _format_research_fields(research: TickerResearch) -> list[dict[str, Any]]:
    fields: list[dict[str, Any]] = []
    if research.summary:
        fields.append({"name": "📰 Why it's trending", "value": _clip(research.summary, EMBED_FIELD_VALUE_LIMIT)})
    if research.headlines:
        lines = [f"• [{_clip(h.title, 120)}]({h.url}) — {h.source}" for h in research.headlines]
        value = ""
        for line in lines:
            candidate = f"{value}\n{line}" if value else line
            if len(candidate) > EMBED_FIELD_VALUE_LIMIT:
                break
            value = candidate
        if value:
            fields.append({"name": "Sources", "value": value})
    return fields


def build_signal_embed(index: int, signal: Signal, research: TickerResearch | None = None) -> dict[str, Any]:
    emoji, label, color = STAGE_STYLE[signal.stage]
    trend = signal.trend
    title = f"{emoji} {index}. {trend.ticker}"
    if trend.name:
        title += f" — {trend.name}"
    title += f" · {label}"

    embed: dict[str, Any] = {
        "title": _clip(title, EMBED_TITLE_LIMIT),
        "url": YAHOO_QUOTE_PAGE_URL.format(symbol=trend.ticker),
        "color": color,
        "description": _clip(
            f"{_format_price_line(signal.quote)}\n{_format_mentions_line(trend)}",
            EMBED_DESCRIPTION_LIMIT,
        ),
        "footer": {"text": f"Trend score {trend.trend_score:.1f} · {trend.upvotes} upvotes"},
    }
    if research is not None and research.ticker == trend.ticker:
        fields = _format_research_fields(research)
        if fields:
            embed["fields"] = fields
    return embed


def build_payload(
    signals: list[Signal],
    *,
    research: TickerResearch | None = None,
    when: datetime | None = None,
) -> dict[str, Any]:
    ts = when or datetime.now(UTC)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    else:
        ts = ts.astimezone(UTC)

    header = f"**Stock alert** · {ts.strftime('%Y-%m-%d %H:%M')} UTC"
    if not signals:
        return {"content": f"{header}\nNo accelerating tickers passed the filters this run."}

    counts = {stage: sum(1 for s in signals if s.stage == stage) for stage in (STAGE_EARLY, STAGE_MOVING, STAGE_LATE)}
    summary = " · ".join(
        f"{STAGE_STYLE[stage][0]} {count} {STAGE_STYLE[stage][1].lower()}" for stage, count in counts.items() if count
    )
    content = f"{header}\nReddit acceleration picks — {summary}" if summary else header

    embeds: list[dict[str, Any]] = []
    total = 0
    for index, signal in enumerate(signals[:EMBEDS_PER_MESSAGE], start=1):
        embed = build_signal_embed(index, signal, research)
        size = _embed_size(embed)
        if total + size > EMBED_TOTAL_LIMIT and "fields" in embed:
            del embed["fields"]
            size = _embed_size(embed)
        if total + size > EMBED_TOTAL_LIMIT:
            break
        embeds.append(embed)
        total += size

    return {"content": content, "embeds": embeds}


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


def _build_research_prompt(signal: Signal, headlines: list[NewsHeadline]) -> list[dict[str, str]]:
    trend, quote = signal.trend, signal.quote
    headline_lines: list[str] = []
    for index, headline in enumerate(headlines, start=1):
        snippet = f" — {headline.body}" if headline.body else ""
        headline_lines.append(f"{index}. {headline.title} ({headline.source}){snippet}")

    prior = trend.mentions_24h_ago if trend.mentions_24h_ago is not None else 0
    rel_volume = f"{quote.rel_volume:.1f}x" if quote.rel_volume is not None else "n/a"
    user_content = (
        f"Ticker: {trend.ticker}\n"
        f"Reddit (ApeWisdom): {prior} → {trend.mentions} mentions in 24h, "
        f"rank #{trend.rank} (was #{trend.rank_24h_ago or 'n/a'}), trend score {trend.trend_score:.1f}\n"
        f"Price: ${quote.price:.2f}, 1d {_format_pct(quote.change_1d)}, "
        f"5d {_format_pct(quote.change_5d)}, relative volume {rel_volume}\n\n"
        f"Recent headlines:\n"
        f"{chr(10).join(headline_lines)}\n\n"
        "In 2-3 sentences: what is the likely catalyst for the Reddit buzz, is it fresh "
        "(last day or two) or old news, and has the price already reacted?\n"
        "Use only the headlines and stats above. If the headlines don't explain it, say so."
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


async def summarize_with_github_models(
    client: httpx.AsyncClient,
    signal: Signal,
    headlines: list[NewsHeadline],
) -> str | None:
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if not token:
        return None

    ticker = signal.trend.ticker
    model = _env("RESEARCH_MODEL", "openai/gpt-4o-mini")
    url = _env("GITHUB_MODELS_URL", GITHUB_MODELS_URL_DEFAULT)
    messages = _build_research_prompt(signal, headlines)

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


async def fetch_apewisdom(client: httpx.AsyncClient, *, pages: int) -> list[TrendRow]:
    responses = await asyncio.gather(
        *[client.get(APEWISDOM_PAGE_URL.format(page=page)) for page in range(1, pages + 1)]
    )

    rows: list[TrendRow] = []
    for response in responses:
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise NotifyError("ApeWisdom response is not a JSON object")

        results = payload.get("results")
        if not isinstance(results, list):
            raise NotifyError("ApeWisdom response missing results list")

        for raw in results:
            if not isinstance(raw, dict):
                continue
            row = _parse_apewisdom_row(raw)
            if row is not None:
                rows.append(row)

    if not rows:
        raise NotifyError("No valid ticker rows in ApeWisdom response")

    return rows


async def fetch_stock_quote(client: httpx.AsyncClient, ticker: str) -> StockQuote | None:
    params = {"interval": "1d", "range": "2mo"}
    try:
        response = await client.get(YAHOO_CHART_URL.format(symbol=ticker), params=params)
        response.raise_for_status()
        return _extract_stock_quote(ticker, response.json())
    except (NotifyError, httpx.HTTPError, ValueError):
        return None


async def send_discord(client: httpx.AsyncClient, payload: dict[str, Any]) -> None:
    webhook_url = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    if not webhook_url:
        raise NotifyError("DISCORD_WEBHOOK_URL is not configured")

    response = await client.post(webhook_url, json=payload)
    if response.status_code >= 400:
        raise NotifyError(f"Discord webhook failed: {response.status_code} {response.text}")


async def run() -> None:
    min_mentions = _env_int("MIN_MENTIONS", 5)
    pages = _env_int("APEWISDOM_PAGES", 3)
    top_n = _env_int("TOP_N", 5)
    min_price = _env_float("MIN_PRICE", 1.0)
    user_agent = _env("YAHOO_USER_AGENT", "stock_alert-cron/1.0")

    headers = {"User-Agent": user_agent}
    async with httpx.AsyncClient(timeout=20.0, headers=headers) as client:
        raw_trends = await fetch_apewisdom(client, pages=pages)
        candidates = enrich_and_rank(raw_trends, min_mentions=min_mentions, top_n=top_n * CANDIDATE_MULTIPLIER)

        fetched = await asyncio.gather(*[fetch_stock_quote(client, t.ticker) for t in candidates])
        quotes = {q.ticker: q for q in fetched if q is not None}
        signals = build_signals(candidates, quotes, min_price=min_price, top_n=top_n)

        research: TickerResearch | None = None
        if signals and _env_bool("ENABLE_RESEARCH", True):
            top = signals[0]
            max_news = _env_int("RESEARCH_NEWS_COUNT", 5)
            headlines = await fetch_news_headlines(top.trend.ticker, max_results=max_news)
            if headlines:
                summary = await summarize_with_github_models(client, top, headlines)
                research = TickerResearch(top.trend.ticker, summary, headlines[:3])

        await send_discord(client, build_payload(signals, research=research))


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
