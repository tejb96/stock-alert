#!/usr/bin/env python3
"""Reddit buzz, alert-only: posts only when a pick clears a deliberately strict bar.

Every weekday run ranks accelerating Reddit tickers, checks the top ones against price, trend and
SEC filings, and records them as tracked picks. A pick is posted only when the buzz is early (price
quiet), the stock is in an uptrend, volume is unusual and insiders have been buying — all four.
Most days post nothing. The Sunday recap and scorecard run only when asked for (DIGEST_MODE=recap).
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import httpx

from common import (
    EMBED_DESCRIPTION_LIMIT,
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
    MARKET_TZ,
    STAGE_EARLY,
    STAGE_LATE,
    STAGE_MOVING,
    TREND_UP,
    YAHOO_QUOTE_PAGE_URL,
    EarningsEvent,
    StockQuote,
    classify_stage,
    classify_trend,
    fetch_earnings_calendar,
    fetch_stock_quotes,
    format_price_line,
    format_trend_line,
    is_tradeable,
)
from reddit import TrendRow, enrich_and_rank, fetch_apewisdom
from sec import SecActivity, SecClient, format_activity_line, ticker_activity
from state import (
    DIGEST_STATE_FILE,
    AlertRecord,
    ScoredAlert,
    StageStats,
    Trajectory,
    WeekSummary,
    apply_history,
    empty_digest_state,
    last_run_at,
    load_json,
    posted_alerts,
    record_run,
    save_json,
    score_alerts,
    scorecard_alerts,
    summarize,
    summarize_by_stage,
    week_summaries,
)

# How many ranked candidates to price-check per final slot; junk (ETFs, pennies,
# non-tickers Yahoo doesn't know) gets dropped, so we over-fetch.
CANDIDATE_MULTIPLIER = 3
BENCHMARK = "SPY"
# Mention moves smaller than this read as "steady".
STEADY_MENTIONS_PCT = 10.0
# Trading volume at least this multiple of normal confirms the buzz is moving real money.
CONFIRM_REL_VOLUME = 1.5
RECAP_MAX_TICKERS = 12

STAGE_STYLE = {
    STAGE_EARLY: ("🟢", "Early", 0x2ECC71),
    STAGE_MOVING: ("🟡", "Moving", 0xF1C40F),
    STAGE_LATE: ("🔴", "Late / chasing", 0xE74C3C),
    None: ("⚪", "No price data", 0x95A5A6),
}
# What "Early" etc. mean, spelled out on each card so a quiet price isn't mistaken for a buy signal.
STAGE_HINT = {
    STAGE_EARLY: "price quiet over the last 5d",
    STAGE_MOVING: "price already moving",
    STAGE_LATE: "big move already happened",
    None: "",
}
SCORECARD_COLOR = 0x3498DB
RECAP_COLOR = 0x5865F2


@dataclass(frozen=True)
class Signal:
    trend: TrendRow
    quote: StockQuote
    stage: str | None
    trajectory: Trajectory | None = None
    sec: SecActivity | None = None
    earnings: EarningsEvent | None = None


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
        appearance = f"{_ordinal(trajectory.streak + 1)} day on the list"
        if trajectory.cooling:
            return f"🧊 {appearance}, cooling off · mentions by day {path}"
        return f"🔥 {appearance}, still heating up · mentions by day {path}"
    if len(trajectory.mentions) >= 2:
        return f"👀 New to the list, building for days · mentions {path}"
    return "🆕 First time on the radar"


def mentions_arrow(change: float | None) -> str:
    if change is None:
        return ""
    if change >= STEADY_MENTIONS_PCT:
        return "↗️"
    if change <= -STEADY_MENTIONS_PCT:
        return "↘️"
    return "➡️"


def format_earnings_line(event: EarningsEvent, *, today: date) -> str:
    days = (event.day - today).days
    when = "today" if days <= 0 else "tomorrow" if days == 1 else f"in {days}d"
    timing = f", {event.timing}" if event.timing else ""
    return f"📅 Earnings {when} ({event.day.strftime('%a %b %-d')}{timing})"


def build_signal_embed(index: int, signal: Signal, *, today: date) -> dict[str, Any]:
    emoji, label, color = STAGE_STYLE[signal.stage]
    trend = signal.trend
    title = f"{index}. {trend.ticker}"
    if trend.name:
        title += f" — {trend.name}"

    stage = f"{emoji} **{label}**" + (f" ({STAGE_HINT[signal.stage]})" if STAGE_HINT[signal.stage] else "")
    lines = [stage, format_price_line(signal.quote, show_range=False)]
    trend_line = format_trend_line(signal.quote)
    if trend_line:
        lines.append(trend_line)
    lines.append(format_mentions_line(trend))
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
    return embed


def alert_reasons(signal: Signal) -> list[str]:
    """What confirms a pick is worth acting on today; empty when it doesn't clear the bar.

    Reddit chatter alone has no edge (the scorecard showed early picks trailing SPY), so the bar
    asks for all of it at once: the price hasn't reacted yet, the stock is in an uptrend, real money
    is trading it, and the people who run the company have been buying."""
    if signal.stage != STAGE_EARLY or classify_trend(signal.quote) != TREND_UP:
        return []
    rel_volume = signal.quote.rel_volume
    if rel_volume is None or rel_volume < CONFIRM_REL_VOLUME:
        return []
    if signal.sec is None or not signal.sec.buys:
        return []
    return [f"volume {rel_volume:.1f}× normal", "insiders buying", "uptrend"]


def _utc(when: datetime | None) -> datetime:
    ts = when or datetime.now(UTC)
    return ts.replace(tzinfo=UTC) if ts.tzinfo is None else ts.astimezone(UTC)


def build_alert_payload(signals: list[Signal], *, when: datetime | None = None) -> dict[str, Any]:
    """Only the picks that cleared the alert bar, each with what confirmed it."""
    ts = _utc(when)
    content = (
        f"🔔 **Reddit alert** · {ts.astimezone(MARKET_TZ):%a %b %-d} — "
        f"{len(signals)} pick{'s' if len(signals) != 1 else ''} worth a look: buzz building, price quiet, "
        "uptrend, heavy volume and insider buying"
    )
    embeds = []
    for index, signal in enumerate(signals, start=1):
        embed = build_signal_embed(index, signal, today=ts.date())
        header = f"✅ **Confirmed by:** {', '.join(alert_reasons(signal))}"
        embed["description"] = clip(f"{header}\n{embed['description']}", EMBED_DESCRIPTION_LIMIT)
        embeds.append(embed)
    return {"content": content, "embeds": fit_embeds(embeds)}


def format_week_line(week: WeekSummary, quote: StockQuote | None) -> str:
    parts = [f"{'🔔' if week.alerted else '•'} **{week.ticker}** {week.days} day{'s' if week.days != 1 else ''}"]
    if week.mentions_first is not None and week.mentions_last is not None:
        change = week.mentions_change
        arrow = f" {mentions_arrow(change)}" if change is not None else ""
        parts.append(f"mentions {week.mentions_first} → {week.mentions_last}{arrow}")
    if quote is not None and week.first_price:
        parts.append(
            f"price {format_pct((quote.price / week.first_price - 1) * 100)} since "
            f"{week.first_seen.astimezone(MARKET_TZ):%a}"
        )
    return " · ".join(parts)


def format_outlook_line(index: int, signal: Signal) -> str:
    parts = []
    stage_emoji, stage_label, _ = STAGE_STYLE[signal.stage]
    parts.append(f"{stage_emoji} {stage_label}")
    trend = classify_trend(signal.quote)
    if trend is not None:
        parts.append({"up": "📈 Uptrend", "down": "📉 Downtrend", "sideways": "↔️ Sideways"}[trend])
    return f"{index}. **{signal.trend.ticker}** · {' · '.join(parts)}"


def build_recap_payload(
    weeks: list[WeekSummary],
    signals: list[Signal],
    quotes: dict[str, StockQuote],
    *,
    when: datetime,
    scorecard: dict[str, Any] | None,
) -> dict[str, Any]:
    ts = _utc(when)
    alerted = sum(1 for w in weeks if w.alerted)
    content = (
        f"🗓 **Reddit weekly recap** · week ending {ts.astimezone(MARKET_TZ):%a %b %-d} — "
        f"{len(weeks)} ticker{'s' if len(weeks) != 1 else ''} tracked, {alerted} alert{'s' if alerted != 1 else ''} sent"
    )
    embeds: list[dict[str, Any]] = []
    if weeks:
        lines = [format_week_line(w, quotes.get(w.ticker)) for w in weeks[:RECAP_MAX_TICKERS]]
        if len(weeks) > RECAP_MAX_TICKERS:
            lines.append(f"…and {len(weeks) - RECAP_MAX_TICKERS} more")
        embeds.append(
            {
                "title": "📋 This week's tracked picks",
                "color": RECAP_COLOR,
                "description": clip("\n".join(lines), EMBED_DESCRIPTION_LIMIT),
                "footer": {
                    "text": ("🔔 = cleared the alert bar and was posted · " if alerted else "")
                    + "days = days in the daily top picks"
                },
            }
        )
    if signals:
        embeds.append(
            {
                "title": "👀 Heading into the week",
                "color": RECAP_COLOR,
                "description": clip(
                    "\n".join(format_outlook_line(i, s) for i, s in enumerate(signals, start=1)), EMBED_DESCRIPTION_LIMIT
                ),
            }
        )
    if scorecard is not None:
        embeds.append(scorecard)
    return {"content": content, "embeds": fit_embeds(embeds)}


def _stats_line(label: str, s: StageStats) -> str:
    vs_spy = f" · {format_pct(s.avg_excess)} vs {BENCHMARK}" if s.avg_excess is not None else ""
    return f"{label}: {s.count} picks · avg {format_pct(s.avg_return)}{vs_spy} · {s.win_rate:.0f}% winners"


def build_scorecard_embed(
    stats: dict[str | None, StageStats],
    scored: list[ScoredAlert],
    *,
    window_days: int,
    posted: StageStats | None = None,
) -> dict[str, Any]:
    lines: list[str] = []
    if posted is not None:
        lines.append(_stats_line("🔔 **Alerts sent**", posted))
    for stage in (STAGE_EARLY, STAGE_MOVING, STAGE_LATE, None):
        if stage not in stats:
            continue
        emoji, label, _ = STAGE_STYLE[stage]
        lines.append(_stats_line(f"{emoji} **{label}**", stats[stage]))
    ranked = sorted(scored, key=lambda a: a.ret, reverse=True)
    if ranked:
        best, worst = ranked[0], ranked[-1]
        lines.append("")
        lines.append(f"Best: **{best.ticker}** {format_pct(best.ret)} ({best.ts.strftime('%b %-d')})")
        if worst is not best:
            lines.append(f"Worst: **{worst.ticker}** {format_pct(worst.ret)} ({worst.ts.strftime('%b %-d')})")
    return {
        "title": f"📊 Scorecard — picks from the last {window_days} days",
        "color": SCORECARD_COLOR,
        "description": clip("\n".join(lines), EMBED_DESCRIPTION_LIMIT),
        "footer": {
            "text": f"Return from pick price to now. Tracked picks by stage, each streak counted once from its start; "
            f"alerts from when they were posted. Winners beat {BENCHMARK} over the same period."
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
    posted = posted_alerts(alerts, now=now, window_days=window_days)
    if not pending:
        return None
    quotes = await fetch_stock_quotes(client, [a["ticker"] for a in [*pending, *posted]])
    prices = {t: q.price for t, q in quotes.items()}
    scored = score_alerts(pending, prices, spy_now)
    if not scored:
        return None
    scored_posted = score_alerts(posted, prices, spy_now)
    return build_scorecard_embed(
        summarize_by_stage(scored),
        scored,
        window_days=window_days,
        posted=summarize(scored_posted) if scored_posted else None,
    )


async def enrich_signals(
    client: httpx.AsyncClient,
    signals: list[Signal],
    *,
    today: date,
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
    return [
        replace(s, sec=activity, earnings=earnings.get(s.trend.ticker))
        for s, activity in zip(signals, activities)
    ]


async def run() -> None:
    min_mentions = env_int("MIN_MENTIONS", 5)
    pages = env_int("APEWISDOM_PAGES", 3)
    top_n = env_int("TOP_N", 5)
    min_price = env_float("MIN_PRICE", 1.0)
    window_days = env_int("SCORECARD_DAYS", 14)
    state_path = Path(env("STATE_DIR", "state")) / DIGEST_STATE_FILE
    user_agent = env("YAHOO_USER_AGENT", "stock_alert-cron/1.0")

    now = datetime.now(UTC)
    state = load_json(state_path, empty_digest_state())
    previous_run = last_run_at(state)
    if already_ran_today(previous_run, now) and not env_bool("FORCE", False):
        print(f"Already ran today ({previous_run:%H:%M} UTC); set FORCE=1 to run again.")
        return
    # The weekly recap is opt-in: it's a digest to read, not something to act on.
    recap = env("DIGEST_MODE", "alert").strip().lower() == "recap"

    async with httpx.AsyncClient(timeout=20.0, headers={"User-Agent": user_agent}) as client:
        rows = await fetch_apewisdom(client, pages=pages)
        adjusted, trajectories = apply_history(rows, state.get("snapshots", []))
        candidates = enrich_and_rank(adjusted, min_mentions=min_mentions, top_n=top_n * CANDIDATE_MULTIPLIER)

        weeks = week_summaries(state, now=now) if recap else []
        quotes = await fetch_stock_quotes(client, [*(c.ticker for c in candidates), *(w.ticker for w in weeks), BENCHMARK])
        spy = quotes.get(BENCHMARK)
        signals = build_signals(candidates, quotes, min_price=min_price, top_n=top_n, trajectories=trajectories)
        signals = await enrich_signals(client, signals, today=now.date())
        alerts = [s for s in signals if alert_reasons(s)]

        payload: dict[str, Any] | None = None
        if recap:
            scorecard = await build_scorecard(
                client, state.get("alerts", []), now=now, window_days=window_days, spy_now=spy.price if spy else None
            )
            payload = build_recap_payload(weeks, signals, quotes, when=now, scorecard=scorecard)
        elif alerts:
            payload = build_alert_payload(alerts, when=now)

        if payload is None:
            print(f"notify: {len(signals)} picks tracked, none cleared the alert bar; nothing posted.")
        elif env_bool("DRY_RUN", False):
            print(json.dumps(payload, indent=1, ensure_ascii=False))
        else:
            await send_discord(client, payload)
        if env_bool("DRY_RUN", False):
            return

    records = [
        AlertRecord(
            s.trend.ticker,
            s.quote.price,
            s.stage,
            s.trend.trend_score,
            alerted=not recap and s in alerts,
        )
        for s in signals
    ]
    state = record_run(
        state, now=now, rows=rows, alerts=records, spy_price=spy.price if spy else None, min_mentions=min_mentions
    )
    save_json(state_path, state)


def already_ran_today(previous_run: datetime | None, now: datetime) -> bool:
    """GitHub's cron can start runs hours late or bunch them up; never alert twice in one US day."""
    if previous_run is None:
        return False
    return previous_run.astimezone(MARKET_TZ).date() == now.astimezone(MARKET_TZ).date()


def main() -> int:
    try:
        asyncio.run(run())
    except NotifyError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except httpx.HTTPError as exc:
        print(f"http error: {exc}", file=sys.stderr)
        return 1

    print("Digest run finished.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
