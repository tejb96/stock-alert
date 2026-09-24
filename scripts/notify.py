#!/usr/bin/env python3
"""Digest cron: accelerating Reddit tickers + price, insider and catalyst context → Discord embeds."""

from __future__ import annotations

import asyncio
import os
import sys
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import httpx

from common import (
    EMBED_DESCRIPTION_LIMIT,
    EMBED_FIELD_VALUE_LIMIT,
    EMBED_TITLE_LIMIT,
    NotifyError,
    clip,
    env,
    env_bool,
    env_float,
    env_int,
    fit_embeds,
    format_pct,
    send_discord,
)
from market import (
    STAGE_EARLY,
    STAGE_LATE,
    STAGE_MOVING,
    YAHOO_QUOTE_PAGE_URL,
    EarningsEvent,
    StockQuote,
    classify_stage,
    fetch_earnings_calendar,
    fetch_stock_quotes,
    format_price_line,
    is_tradeable,
)
from reddit import TrendRow, enrich_and_rank, fetch_apewisdom
from research import Research, research_ticker
from sec import SecActivity, SecClient, format_activity_line, ticker_activity
from state import (
    DIGEST_STATE_FILE,
    AlertRecord,
    ScoredAlert,
    StageStats,
    Trajectory,
    apply_history,
    empty_digest_state,
    load_json,
    record_run,
    save_json,
    score_alerts,
    scorecard_alerts,
    summarize_by_stage,
)

# How many ranked candidates to price-check per final slot; junk (ETFs, pennies,
# non-tickers Yahoo doesn't know) gets dropped, so we over-fetch.
CANDIDATE_MULTIPLIER = 3
BENCHMARK = "SPY"
SCORECARD_WEEKDAY = 6  # Sunday

STAGE_STYLE = {
    STAGE_EARLY: ("🟢", "Early", 0x2ECC71),
    STAGE_MOVING: ("🟡", "Moving", 0xF1C40F),
    STAGE_LATE: ("🔴", "Late / chasing", 0xE74C3C),
    None: ("⚪", "No price data", 0x95A5A6),
}
SCORECARD_COLOR = 0x3498DB


@dataclass(frozen=True)
class Signal:
    trend: TrendRow
    quote: StockQuote
    stage: str | None
    trajectory: Trajectory | None = None
    sec: SecActivity | None = None
    earnings: EarningsEvent | None = None
    research: Research | None = None


def build_signals(
    candidates: list[TrendRow],
    quotes: dict[str, StockQuote],
    *,
    min_price: float,
    top_n: int,
    trajectories: dict[str, Trajectory] | None = None,
) -> list[Signal]:
    signals: list[Signal] = []
    for trend in candidates:
        quote = quotes.get(trend.ticker)
        # No Yahoo quote usually means ApeWisdom matched a word, not a real ticker.
        if quote is None or not is_tradeable(quote, min_price=min_price):
            continue
        trajectory = (trajectories or {}).get(trend.ticker)
        signals.append(Signal(trend, quote, classify_stage(quote), trajectory=trajectory))
        if len(signals) >= top_n:
            break
    return signals


def _ordinal(n: int) -> str:
    suffix = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def format_mentions_line(trend: TrendRow) -> str:
    if trend.mentions_24h_ago:
        mentions = f"{trend.mentions_24h_ago} → **{trend.mentions}** mentions ({format_pct(trend.change_24h)})"
    else:
        mentions = f"**{trend.mentions}** mentions (new in 24h)"
    rank = f"rank #{trend.rank}"
    if trend.rank_24h_ago is not None:
        rank = f"rank #{trend.rank_24h_ago} → **#{trend.rank}**"
    return f"📈 Reddit: {mentions} · {rank}"


def format_trajectory_line(trajectory: Trajectory) -> str:
    path = " → ".join(str(m) for m in trajectory.mentions)
    if trajectory.streak:
        appearance = f"{_ordinal(trajectory.streak + 1)} digest in a row"
        if trajectory.cooling:
            return f"🧊 {appearance}, cooling off · mentions by run {path}"
        return f"🔥 {appearance} · mentions by run {path}"
    if len(trajectory.mentions) >= 2:
        return f"👀 Building across runs · mentions {path}"
    return "🆕 First time on the radar"


def format_earnings_line(event: EarningsEvent, *, today: date) -> str:
    days = (event.day - today).days
    when = "today" if days <= 0 else "tomorrow" if days == 1 else f"in {days}d"
    timing = f", {event.timing}" if event.timing else ""
    return f"📅 Earnings {when} ({event.day.strftime('%a %b %-d')}{timing})"


def _research_fields(research: Research) -> list[dict[str, Any]]:
    fields: list[dict[str, Any]] = []
    if research.catalyst:
        tags = [t for t in (research.catalyst_type,) if t]
        if research.fresh is not None:
            tags.append("fresh news" if research.fresh else "older news")
        if research.priced_in:
            tags.append(f"priced in: {research.priced_in}")
        value = research.catalyst + (f"\n*{' · '.join(tags)}*" if tags else "")
        fields.append({"name": "📰 Catalyst", "value": clip(value, EMBED_FIELD_VALUE_LIMIT)})
    if research.risks:
        value = "\n".join(f"• {r}" for r in research.risks)
        fields.append({"name": "⚠️ Risks", "value": clip(value, EMBED_FIELD_VALUE_LIMIT)})
    if research.headlines:
        value = ""
        for h in research.headlines[:2]:
            line = f"• [{clip(h.title, 90)}]({h.url}) — {h.source}"
            candidate = f"{value}\n{line}" if value else line
            if len(candidate) > EMBED_FIELD_VALUE_LIMIT:
                break
            value = candidate
        if value:
            fields.append({"name": "Sources", "value": value})
    return fields


def build_signal_embed(index: int, signal: Signal, *, today: date) -> dict[str, Any]:
    emoji, label, color = STAGE_STYLE[signal.stage]
    trend = signal.trend
    title = f"{emoji} {index}. {trend.ticker}"
    if trend.name:
        title += f" — {trend.name}"
    title += f" · {label}"

    lines = [format_price_line(signal.quote), format_mentions_line(trend)]
    if signal.trajectory is not None:
        lines.append(format_trajectory_line(signal.trajectory))
    if signal.sec is not None:
        sec_line = format_activity_line(signal.sec, today=today)
        if sec_line:
            lines.append(sec_line)
    if signal.earnings is not None:
        lines.append(format_earnings_line(signal.earnings, today=today))

    embed: dict[str, Any] = {
        "title": clip(title, EMBED_TITLE_LIMIT),
        "url": YAHOO_QUOTE_PAGE_URL.format(symbol=trend.ticker),
        "color": color,
        "description": clip("\n".join(lines), EMBED_DESCRIPTION_LIMIT),
        "footer": {"text": f"Trend score {trend.trend_score:.1f} · {trend.upvotes} upvotes"},
    }
    if signal.research is not None:
        fields = _research_fields(signal.research)
        if fields:
            embed["fields"] = fields
    return embed


def _utc(when: datetime | None) -> datetime:
    ts = when or datetime.now(UTC)
    return ts.replace(tzinfo=UTC) if ts.tzinfo is None else ts.astimezone(UTC)


def build_payload(signals: list[Signal], *, when: datetime | None = None) -> dict[str, Any]:
    ts = _utc(when)
    header = f"**Stock alert** · {ts.strftime('%Y-%m-%d %H:%M')} UTC"
    if not signals:
        return {"content": f"{header}\nNo accelerating tickers passed the filters this run."}

    counts = {stage: sum(1 for s in signals if s.stage == stage) for stage in (STAGE_EARLY, STAGE_MOVING, STAGE_LATE)}
    parts = [f"{STAGE_STYLE[stage][0]} {count} {STAGE_STYLE[stage][1].lower()}" for stage, count in counts.items() if count]
    insider_count = sum(1 for s in signals if s.sec is not None and s.sec.buys)
    if insider_count:
        parts.append(f"🏛 {insider_count} with insider buying")
    content = f"{header}\nReddit acceleration picks — {' · '.join(parts)}" if parts else header

    embeds = [build_signal_embed(i, s, today=ts.date()) for i, s in enumerate(signals, start=1)]
    return {"content": content, "embeds": fit_embeds(embeds)}


def build_research_context(signal: Signal, *, today: date) -> str:
    trend, quote = signal.trend, signal.quote
    prior = trend.mentions_24h_ago if trend.mentions_24h_ago is not None else 0
    rel_volume = f"{quote.rel_volume:.1f}x" if quote.rel_volume is not None else "n/a"
    lines = [
        f"Reddit (ApeWisdom): {prior} → {trend.mentions} mentions in 24h, "
        f"rank #{trend.rank} (was #{trend.rank_24h_ago or 'n/a'})",
        f"Price: ${quote.price:.2f}, 1d {format_pct(quote.change_1d)}, 5d {format_pct(quote.change_5d)}, "
        f"relative volume {rel_volume}, stage {STAGE_STYLE[signal.stage][1]}",
    ]
    if quote.daily_volatility:
        lines.append(f"Typical daily move: ±{quote.daily_volatility:.1f}%")
    if signal.trajectory is not None and len(signal.trajectory.mentions) > 1:
        lines.append(f"Mentions across recent runs: {' → '.join(map(str, signal.trajectory.mentions))}")
    if signal.sec is not None:
        sec_line = format_activity_line(signal.sec, today=today)
        if sec_line:
            lines.append("SEC filings: " + sec_line.removeprefix("🏛 ").replace("**", ""))
    if signal.earnings is not None:
        lines.append(format_earnings_line(signal.earnings, today=today).removeprefix("📅 "))
    return "\n".join(lines)


def build_scorecard_embed(
    stats: dict[str | None, StageStats],
    scored: list[ScoredAlert],
    *,
    window_days: int,
) -> dict[str, Any]:
    lines: list[str] = []
    for stage in (STAGE_EARLY, STAGE_MOVING, STAGE_LATE, None):
        if stage not in stats:
            continue
        s = stats[stage]
        emoji, label, _ = STAGE_STYLE[stage]
        vs_spy = f" · vs {BENCHMARK} {format_pct(s.avg_excess)}" if s.avg_excess is not None else ""
        lines.append(
            f"{emoji} **{label}**: {s.count} picks · avg {format_pct(s.avg_return)}{vs_spy} · "
            f"{s.win_rate:.0f}% winners"
        )
    ranked = sorted(scored, key=lambda a: a.ret, reverse=True)
    if ranked:
        best, worst = ranked[0], ranked[-1]
        lines.append("")
        lines.append(f"Best: **{best.ticker}** {format_pct(best.ret)} ({best.ts.strftime('%b %-d')})")
        if worst is not best:
            lines.append(f"Worst: **{worst.ticker}** {format_pct(worst.ret)} ({worst.ts.strftime('%b %-d')})")
    return {
        "title": f"📊 Scorecard — alerts from the last {window_days} days",
        "color": SCORECARD_COLOR,
        "description": clip("\n".join(lines), EMBED_DESCRIPTION_LIMIT),
        "footer": {
            "text": f"Return from alert price to now. Each streak counted once, from its first alert. "
            f"Winners beat {BENCHMARK} over the same period."
        },
    }


async def build_scorecard(
    client: httpx.AsyncClient,
    alerts: list[dict[str, Any]],
    *,
    now: datetime,
    window_days: int,
    spy_now: float | None,
) -> dict[str, Any] | None:
    pending = scorecard_alerts(alerts, now=now, window_days=window_days)
    if not pending:
        return None
    quotes = await fetch_stock_quotes(client, [a["ticker"] for a in pending])
    scored = score_alerts(pending, {t: q.price for t, q in quotes.items()}, spy_now)
    if not scored:
        return None
    return build_scorecard_embed(summarize_by_stage(scored), scored, window_days=window_days)


async def enrich_signals(
    client: httpx.AsyncClient,
    signals: list[Signal],
    *,
    today: date,
    research_top_n: int,
    max_news: int,
) -> list[Signal]:
    contact_email = os.environ.get("SEC_CONTACT_EMAIL", "").strip()
    sec = SecClient(client, contact_email) if contact_email else None

    async def sec_lookup(signal: Signal) -> SecActivity | None:
        if sec is None:
            return None
        return await ticker_activity(sec, signal.trend.ticker, today=today)

    earnings, *activities = await asyncio.gather(
        fetch_earnings_calendar(client, start=today),
        *[sec_lookup(s) for s in signals],
    )
    signals = [
        replace(s, sec=activity, earnings=earnings.get(s.trend.ticker))
        for s, activity in zip(signals, activities)
    ]

    # Sequential: DuckDuckGo rate-limits bursts, and GitHub Models' free tier is per-minute.
    enriched: list[Signal] = []
    for index, signal in enumerate(signals):
        if index < research_top_n:
            context = build_research_context(signal, today=today)
            research = await research_ticker(
                client, signal.trend.ticker, context, max_news=max_news, name=signal.trend.name
            )
            signal = replace(signal, research=research)
        enriched.append(signal)
    return enriched


async def run() -> None:
    min_mentions = env_int("MIN_MENTIONS", 5)
    pages = env_int("APEWISDOM_PAGES", 3)
    top_n = env_int("TOP_N", 5)
    min_price = env_float("MIN_PRICE", 1.0)
    research_top_n = env_int("RESEARCH_TOP_N", 3) if env_bool("ENABLE_RESEARCH", True) else 0
    max_news = env_int("RESEARCH_NEWS_COUNT", 5)
    window_days = env_int("SCORECARD_DAYS", 14)
    state_path = Path(env("STATE_DIR", "state")) / DIGEST_STATE_FILE
    user_agent = env("YAHOO_USER_AGENT", "stock_alert-cron/1.0")

    now = datetime.now(UTC)
    state = load_json(state_path, empty_digest_state())

    async with httpx.AsyncClient(timeout=20.0, headers={"User-Agent": user_agent}) as client:
        rows = await fetch_apewisdom(client, pages=pages)
        adjusted, trajectories = apply_history(rows, state.get("snapshots", []))
        candidates = enrich_and_rank(adjusted, min_mentions=min_mentions, top_n=top_n * CANDIDATE_MULTIPLIER)

        quotes = await fetch_stock_quotes(client, [*(c.ticker for c in candidates), BENCHMARK])
        spy = quotes.get(BENCHMARK)
        signals = build_signals(candidates, quotes, min_price=min_price, top_n=top_n, trajectories=trajectories)
        signals = await enrich_signals(
            client, signals, today=now.date(), research_top_n=research_top_n, max_news=max_news
        )

        await send_discord(client, build_payload(signals, when=now))

        scorecard_mode = env("SCORECARD", "auto").strip().lower()
        if scorecard_mode == "always" or (scorecard_mode == "auto" and now.weekday() == SCORECARD_WEEKDAY):
            embed = await build_scorecard(
                client, state.get("alerts", []), now=now, window_days=window_days, spy_now=spy.price if spy else None
            )
            if embed is not None:
                await send_discord(client, {"embeds": [embed]})

    alerts = [AlertRecord(s.trend.ticker, s.quote.price, s.stage, s.trend.trend_score) for s in signals]
    state = record_run(
        state, now=now, rows=rows, alerts=alerts, spy_price=spy.price if spy else None, min_mentions=min_mentions
    )
    save_json(state_path, state)


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
