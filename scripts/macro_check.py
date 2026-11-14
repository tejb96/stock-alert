#!/usr/bin/env python3
"""Market alarms: Discord alert when a crash signal or a bottom signal switches on.

Silent unless something changes. A signal alerts when it turns on, and not again while it stays on
or within COOLDOWN_DAYS of its last alert, so a signal flickering around its threshold intraday
doesn't spam.

Backtest: python scripts/macro_check.py --backtest 2020-01-01
prints every alert the rules would have sent since then, with SPY's return afterwards.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from common import (
    EMBED_DESCRIPTION_LIMIT,
    EMBED_TITLE_LIMIT,
    NotifyError,
    clip,
    env,
    env_bool,
    fit_embeds,
    format_pct,
    send_discord,
)
from macro import (
    BEAR,
    BULL,
    FRED_LAG_DAYS,
    FRED_SERIES,
    LIVE_BARS,
    SIGNAL_SERIES,
    SIGNAL_SIDES,
    Intraday,
    IntradayBar,
    Market,
    Series,
    Signal,
    as_of,
    evaluate,
    fetch_intraday,
    fetch_market,
)
from state import MACRO_STATE_FILE, load_json, save_json

COOLDOWN_DAYS = 10
FIRED_RETENTION_DAYS = 90
# History needed before the first backtest day so every signal has its lookback.
BACKTEST_WARMUP_DAYS = 150
# Backtest scoring, in trading days.
FORWARD_DAYS = (5, 20, 60)
DRAWDOWN_DAYS = (20, 60)
EPISODE_DAYS = 60  # how far either side of an alert to look for SPY's high and its trough
REBOUND_DAYS = 20
FALSE_ALARM_DAYS = 20
FALSE_ALARM_DRAWDOWN = -3.0  # % — a crash alarm that SPY never closed this far below was a false alarm
SIGNAL_WARMUP_DAYS = 45  # calendar days of a series before a signal built on it can fire
BACKTEST_SPLIT = date(2020, 1, 1)  # thresholds are tuned on data before this and tested on data after
# Intraday replay: a run after each hourly bar of the US session, as the cron is scheduled.
NEW_YORK = ZoneInfo("America/New_York")
CHECK_HOURS = range(10, 16)
SESSION_FIRST_HOUR = 9
INTRADAY_BAR = timedelta(hours=1)

SIDE_STYLE = {
    # Backtested 2018–2026: by the time these fire most of the drop has usually happened, so they
    # mean "size down, don't chase", not "open a short".
    BEAR: ("🔴", 0xE74C3C, "Crash alarm", "Stress is here — cut leverage and hedge rather than chase shorts now; the bottom signals are what to wait for."),
    # Backtested 2018–2026: SPY averaged +3–6% in the next 20 days vs ~+1% on a typical day.
    BULL: ("🟢", 0x2ECC71, "Bottom signal", "Washout or recovery sign — historically a time to take profit on shorts and start scaling in. Two or more together is stronger."),
}


def transitions(signals: list[Signal], state: dict[str, Any], *, today: date) -> tuple[list[Signal], dict[str, Any]]:
    """Signals that just switched on (outside their cooldown), and the state to store."""
    previously_active = set(state.get("active") or [])
    cutoff = (today - timedelta(days=FIRED_RETENTION_DAYS)).isoformat()
    fired = {k: v for k, v in (state.get("fired") or {}).items() if isinstance(v, str) and v >= cutoff}
    new = []
    for signal in signals:
        if signal.key in previously_active:
            continue
        last = fired.get(signal.key)
        if last and (today - date.fromisoformat(last)).days < COOLDOWN_DAYS:
            continue
        new.append(signal)
        fired[signal.key] = today.isoformat()
    return new, {"active": sorted(s.key for s in signals), "fired": fired}


def snapshot_line(market: Market) -> str:
    parts = []
    spy = market.get("SPY")
    if spy is not None and len(spy):
        high = max(spy.values[-21:])
        parts.append(f"SPY ${spy.last:,.2f} ({format_pct((spy.last / high - 1) * 100)} vs 1-month high)")
    vix = market.get("VIX")
    if vix is not None and len(vix):
        parts.append(f"VIX {vix.last:.1f}")
    usdjpy = market.get("USDJPY")
    if usdjpy is not None and len(usdjpy):
        parts.append(f"USD/JPY {usdjpy.last:.2f}")
    oas = market.get("HYOAS")
    if oas is not None and len(oas):
        parts.append(f"HY spread {oas.last:.2f}%")
    return "📊 " + " · ".join(parts) if parts else ""


def build_embed(signal: Signal) -> dict[str, Any]:
    emoji, color, label, _ = SIDE_STYLE[signal.side]
    return {
        "title": clip(f"{emoji} {label} — {signal.title}", EMBED_TITLE_LIMIT),
        "color": color,
        "description": clip("\n".join([*signal.lines, f"📜 {signal.context}"]), EMBED_DESCRIPTION_LIMIT),
    }


def build_payload(new: list[Signal], active: list[Signal], market: Market, *, when: datetime) -> dict[str, Any]:
    lines = [f"**Market alarms** · {when:%Y-%m-%d %H:%M} UTC", snapshot_line(market)]
    for side in (BEAR, BULL):
        on = [s.title for s in active if s.side == side]
        if on:
            lines.append(f"{SIDE_STYLE[side][0]} Active: {', '.join(on)}")
    for side in (BEAR, BULL):
        if any(s.side == side for s in new):
            lines.append(f"_{SIDE_STYLE[side][3]}_")
    content = "\n".join(line for line in lines if line)
    return {"content": content, "embeds": fit_embeds([build_embed(s) for s in new])}


def backtest(market: Market, start: date) -> list[tuple[date, Signal]]:
    """Replay the live rules over each trading day from `start`, alerting as the cron would."""
    spy = market.get("SPY")
    if spy is None:
        raise NotifyError("backtest needs SPY for the trading calendar")
    state: dict[str, Any] = {}
    alerts = []
    for day in (d for d in spy.dates if d >= start):
        new, state = transitions(evaluate(as_of(market, day)), state, today=day)
        alerts += [(day, s) for s in new]
    return alerts


# --- backtest scoring --------------------------------------------------------------------------


@dataclass(frozen=True)
class Outcome:
    """What SPY did around one alert. Percentages; None where the data ends before the horizon."""

    day: date
    signal: Signal
    forward: tuple[float | None, ...]  # return over each of FORWARD_DAYS
    drawdown: tuple[float | None, ...]  # worst close over each of DRAWDOWN_DAYS, vs the alert's close
    off_high: float  # vs the highest close of the EPISODE_DAYS before
    to_trough: float | None  # further fall to the lowest close of the EPISODE_DAYS after
    days_to_trough: int | None
    off_low: float  # vs the lowest close of the EPISODE_DAYS before
    days_from_low: int
    upside: float | None  # rise to the highest close of the REBOUND_DAYS after
    new_low: bool | None  # closed below that earlier low within REBOUND_DAYS

    @property
    def drop_done(self) -> float | None:
        """Share of the high-to-trough fall that was already behind SPY at the alert."""
        if self.to_trough is None:
            return None
        total = (1 + self.off_high / 100) * (1 + self.to_trough / 100) - 1
        return self.off_high / 100 / total * 100 if total < 0 else None

    @property
    def rebound_missed(self) -> float | None:
        """Share of the low-to-next-high rise that was already gone at the alert."""
        if self.upside is None:
            return None
        total = (1 + self.off_low / 100) * (1 + self.upside / 100) - 1
        return self.off_low / 100 / total * 100 if total > 0 else None


def score(closes: tuple[float, ...], idx: int, day: date, signal: Signal) -> Outcome:
    here = closes[idx]

    def ahead(days: int) -> tuple[float, ...] | None:
        return closes[idx + 1 : idx + 1 + days] if idx + days < len(closes) else None

    def pct(value: float, base: float) -> float:
        return (value / base - 1) * 100

    before = closes[max(0, idx - EPISODE_DAYS) : idx + 1]
    low = min(before)
    after, rebound = ahead(EPISODE_DAYS), ahead(REBOUND_DAYS)
    trough = min(here, *after) if after else None
    return Outcome(
        day=day,
        signal=signal,
        forward=tuple(pct(w[-1], here) if (w := ahead(d)) else None for d in FORWARD_DAYS),
        drawdown=tuple(min(0.0, pct(min(w), here)) if (w := ahead(d)) else None for d in DRAWDOWN_DAYS),
        off_high=pct(here, max(before)),
        to_trough=pct(trough, here) if trough else None,
        days_to_trough=(here, *after).index(trough) if after else None,
        off_low=pct(here, low),
        days_from_low=len(before) - 1 - max(i for i, c in enumerate(before) if c == low),
        upside=pct(max(here, *rebound), here) if rebound else None,
        new_low=min(rebound) < low if rebound else None,
    )


def mean(values: Iterable[float | None]) -> float | None:
    known = [v for v in values if v is not None]
    return sum(known) / len(known) if known else None


def median(values: Iterable[float | None]) -> float | None:
    known = [v for v in values if v is not None]
    return statistics.median(known) if known else None


def share(flags: Iterable[bool | None]) -> float | None:
    known = [f for f in flags if f is not None]
    return sum(known) / len(known) * 100 if known else None


def is_false_alarm(outcome: Outcome) -> bool | None:
    worst = outcome.drawdown[DRAWDOWN_DAYS.index(FALSE_ALARM_DAYS)]
    return None if worst is None else worst > FALSE_ALARM_DRAWDOWN


def first_possible_day(market: Market, key: str) -> date | None:
    """First day a signal has the series it needs, so years it could not fire don't count as quiet."""
    needed = [market.get(name) for name in SIGNAL_SERIES[key]]
    if any(s is None for s in needed):
        return None
    return max(s.dates[0] for s in needed) + timedelta(days=SIGNAL_WARMUP_DAYS)


def num(value: float | None, spec: str = "+.1f") -> str:
    return "n/a" if value is None else format(value + 0.0, spec)


def summary_rows(market: Market, outcomes: list[Outcome], side: str, first: date, last: date) -> list[list[str]]:
    """One row per signal for alerts in [first, last], each followed by the same numbers for any day."""
    spy = market["SPY"]
    rows = []
    for key in (k for k, s in SIGNAL_SIDES.items() if s == side):
        possible = first_possible_day(market, key)
        if possible is None or possible > last:
            continue
        since = max(first, possible)
        hits = [o for o in outcomes if o.signal.key == key and since <= o.day <= last]
        every = [score(spy.values, i, d, hits[0].signal if hits else None) for i, d in enumerate(spy.dates) if since <= d <= last]
        years = max((last - since).days / 365.25, 1e-9)
        for label, group in ((key, hits), (f"  any day since {since:%Y-%m}", every)):
            row = [label, str(len(group)), f"{len(group) / years:.1f}"] if group is hits else [label, "", ""]
            row += [num(mean(o.forward[i] for o in group)) for i in range(len(FORWARD_DAYS))]
            if side == BEAR:
                row += [num(mean(o.drawdown[i] for o in group)) for i in range(len(DRAWDOWN_DAYS))]
                row += [
                    num(share(is_false_alarm(o) for o in group), ".0f"),
                    num(mean(o.off_high for o in group)),
                    num(mean(o.drop_done for o in group), ".0f"),
                    num(median(o.days_to_trough for o in group), ".0f"),
                ]
            else:
                row += [
                    num(median(o.days_from_low for o in group), ".0f"),
                    num(mean(o.off_low for o in group)),
                    num(mean(o.upside for o in group)),
                    num(mean(o.rebound_missed for o in group), ".0f"),
                    num(share(o.new_low for o in group), ".0f"),
                ]
            rows.append(row)
    return rows


def volume_line(spy: Series, posted: list[date], first: date, last: date) -> str:
    """How often something posts: the bar is that most days nothing does."""
    sessions = sum(first <= d <= last for d in spy.dates)
    mine = [d for d in posted if first <= d <= last]
    days = len(set(mine))
    years = max((last - first).days / 365.25, 1e-9)
    return f"Posts: {len(mine)} alerts on {days} of {sessions} sessions ({days / max(sessions, 1) * 100:.1f}%), {len(mine) / years:.1f} alerts and {days / years:.1f} posting days a year."


SUMMARY_COLUMNS = {
    BEAR: ("signal", "n", "/yr", *(f"+{d}d" for d in FORWARD_DAYS), *(f"dd{d}" for d in DRAWDOWN_DAYS), "false%", "offhigh", "done%", "d>trough"),
    BULL: ("signal", "n", "/yr", *(f"+{d}d" for d in FORWARD_DAYS), "d<low", "offlow", "upside", "missed%", "newlow%"),
}
SUMMARY_LEGEND = {
    BEAR: (
        f"+Nd: mean SPY return N trading days after the alert's close. ddN: mean worst close within N days. "
        f"false%: alerts whose worst close in {FALSE_ALARM_DAYS} days stayed within {-FALSE_ALARM_DRAWDOWN:.0f}%. "
        f"offhigh: SPY vs its {EPISODE_DAYS}-day high at the alert. done%: share of the fall from that high to the lowest close of the next "
        f"{EPISODE_DAYS} days that was already over at the alert. d>trough: median trading days from the alert to that trough."
    ),
    BULL: (
        f"d<low: median trading days since SPY's lowest close of the prior {EPISODE_DAYS} days. offlow: SPY above that low at the alert. "
        f"upside: rise to the highest close of the next {REBOUND_DAYS} days. missed%: share of the low-to-that-high rise already gone. "
        f"newlow%: alerts followed by a close below the earlier low within {REBOUND_DAYS} days."
    ),
}


def format_table(header: tuple[str, ...], rows: list[list[str]]) -> str:
    widths = [max(len(header[i]), *(len(r[i]) for r in rows)) for i in range(len(header))]
    lines = [[cell.ljust(w) if i == 0 else cell.rjust(w) for i, (cell, w) in enumerate(zip(line, widths))] for line in (list(header), *rows)]
    return "\n".join("  ".join(line).rstrip() for line in lines)


def print_summary(market: Market, alerts: list[tuple[date, Signal]], start: date, split: date | None) -> None:
    spy = market["SPY"]
    index = {d: i for i, d in enumerate(spy.dates)}
    outcomes = [score(spy.values, index[day], day, signal) for day, signal in alerts]
    end = spy.dates[-1]
    periods = [(f"{start} to {end}", start, end)]
    if split and start < split <= end:
        periods += [(f"{start} to {split - timedelta(days=1)}", start, split - timedelta(days=1)), (f"{split} to {end}", split, end)]
    print("Series history:  " + "  ".join(f"{k} {s.dates[0]:%Y-%m}" for k, s in sorted(market.items(), key=lambda kv: kv[1].dates[0])))
    for label, first, last in periods:
        for side in (BEAR, BULL):
            rows = summary_rows(market, outcomes, side, first, last)
            if rows:
                print(f"\n{SIDE_STYLE[side][2]}s, {label}\n{format_table(SUMMARY_COLUMNS[side], rows)}")
        print(volume_line(spy, [day for day, _ in alerts], first, last))
    for side in (BEAR, BULL):
        print(f"\n{SIDE_STYLE[side][2]} columns. {SUMMARY_LEGEND[side]}")
    print("Horizons that run past the end of the data are left out of the averages.")


def print_alerts(market: Market, alerts: list[tuple[date, Signal]]) -> None:
    spy = market["SPY"]
    index = {d: i for i, d in enumerate(spy.dates)}
    print(f"\n{'date':<11} {'side':<5} {'signal':<34} " + " ".join(f"SPY+{d}d".rjust(8) for d in FORWARD_DAYS))
    for day, signal in alerts:
        fwd = " ".join(format_pct(f).rjust(8) for f in score(spy.values, index[day], day, signal).forward)
        print(f"{day.isoformat():<11} {signal.side:<5} {signal.title:<34} {fwd}")
        for line in signal.lines:
            print(f"{'':<18}{line.replace('**', '')}")
    print(f"\n{len(alerts)} alerts ({sum(s.side == BEAR for _, s in alerts)} bear, {sum(s.side == BULL for _, s in alerts)} bull)")


# --- intraday replay ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Trigger:
    """A signal that was off at the previous close and came on at a mid-session check."""

    day: date
    signal: Signal
    at: datetime
    held: bool  # still on at that day's close
    spy_to_close: float | None  # % SPY moved between the check and the close


def intraday_view(market: Market, bars: dict[str, dict[date, list[IntradayBar]]], day: date, at: datetime) -> Market:
    """What a live run at `at` on trading day `day` would have fetched: `day`'s bar still forming."""
    view = {}
    for key, daily in market.items():
        if key in FRED_SERIES:
            view[key] = daily.upto(day - timedelta(days=FRED_LAG_DAYS), keep=LIVE_BARS)
            continue
        past = daily.upto(day - timedelta(days=1), keep=LIVE_BARS - 1)
        done = [b for b in bars.get(key, {}).get(day, []) if b[0] + INTRADAY_BAR <= at]
        if done:
            past = Series((*past.dates, day), (*past.values, done[-1][1]), (*past.highs, max(b[2] for b in done)))
        view[key] = past
    return view


def replay_intraday(market: Market, intraday: Intraday) -> tuple[list[Trigger], list[date]]:
    """Mid-session triggers at each hourly check, over the days hourly bars cover."""
    bars: dict[str, dict[date, list[IntradayBar]]] = {}
    for key, series in intraday.items():
        for bar in series:
            local = bar[0].astimezone(NEW_YORK)
            if local.hour >= SESSION_FIRST_HOUR:  # leave out the VIX's overnight session
                bars.setdefault(key, {}).setdefault(local.date(), []).append(bar)
    spy = market["SPY"]
    covered = set(bars.get("VIX", {})) & set(bars.get("SPY", {}))
    days = [d for d in spy.dates if d in covered]
    closes = dict(zip(spy.dates, spy.values))
    triggers = []
    on_before = {s.key for s in evaluate(as_of(market, spy.dates[spy.dates.index(days[0]) - 1]))} if days else set()
    for day in days:
        seen: set[str] = set()
        for hour in CHECK_HOURS:
            at = datetime(day.year, day.month, day.day, hour, 30, tzinfo=NEW_YORK).astimezone(UTC)
            view = intraday_view(market, bars, day, at)
            fresh = [s for s in evaluate(view) if s.key not in on_before and s.key not in seen]
            seen.update(s.key for s in fresh)
            triggers += [(s, at, view["SPY"].last) for s in fresh]
        on_close = {s.key for s in evaluate(as_of(market, day))}
        triggers = [
            Trigger(day, t[0], t[1], t[0].key in on_close, (closes[day] / t[2] - 1) * 100) if isinstance(t, tuple) else t for t in triggers
        ]
        on_before = on_close
    return triggers, days


def print_intraday(market: Market, intraday: Intraday, alerts: list[tuple[date, Signal]]) -> None:
    triggers, days = replay_intraday(market, intraday)
    if not days:
        print("\nIntraday: no hourly bars were available, so mid-session triggers could not be checked against the close.")
        return
    rows = []
    for key, side in SIGNAL_SIDES.items():
        mine = [t for t in triggers if t.signal.key == key]
        held = [t for t in mine if t.held]
        at_close = sum(1 for day, s in alerts if s.key == key and days[0] <= day <= days[-1])
        rows.append(
            [
                key,
                side,
                str(len(mine)),
                str(len(held)),
                num(share(not t.held for t in mine), ".0f"),
                num(mean(t.spy_to_close for t in held)),
                num(mean(t.spy_to_close for t in mine if not t.held)),
                str(at_close),
            ]
        )
    total = [t for t in triggers]
    rows.append(["all", "", str(len(total)), str(sum(t.held for t in total)), num(share(not t.held for t in total), ".0f"), "", "", ""])
    header = ("signal", "side", "midday", "held", "lost%", "spy>close held", "spy>close lost", "close alerts")
    print(f"\nMid-session triggers vs the close, {days[0]} to {days[-1]} ({len(days)} sessions, checks at :30 past {CHECK_HOURS[0]}-{CHECK_HOURS[-1]}h New York)")
    print(format_table(header, rows))
    print(
        "midday: days a signal that was off at the previous close came on at an hourly check. held: still on at that day's close. "
        "lost%: share that were off again by the close, which a close-only backtest never sees. spy>close: mean SPY move from the "
        "check to the close. close alerts: alerts the daily replay sent over the same days. Yahoo serves hourly bars for about two "
        "years, so earlier history cannot be checked this way; the VIX bars end on the hour and the others on the half hour, so "
        "each check mixes prices up to 30 minutes apart."
    )


async def run(args: argparse.Namespace) -> None:
    user_agent = env("YAHOO_USER_AGENT", "stock_alert-cron/1.0")
    async with httpx.AsyncClient(timeout=20.0, headers={"User-Agent": user_agent}) as client:
        fetch_start = args.backtest - timedelta(days=BACKTEST_WARMUP_DAYS) if args.backtest else None
        market = await fetch_market(client, start=fetch_start)
        if "VIX" not in market or "SPY" not in market:
            raise NotifyError("core series (VIX, SPY) could not be fetched")

        if args.backtest:
            start = max(args.backtest, market["SPY"].dates[0])
            alerts = backtest(market, start)
            print_summary(market, alerts, start, args.split)
            print_intraday(market, await fetch_intraday(client), alerts)
            if args.alerts:
                print_alerts(market, alerts)
            return

        now = datetime.now(UTC)
        state_path = Path(env("STATE_DIR", "state")) / MACRO_STATE_FILE
        active = evaluate(market)
        new, state = transitions(active, load_json(state_path, {}), today=now.date())
        if new:
            payload = build_payload(new, active, market, when=now)
            if env_bool("DRY_RUN", False):
                print(json.dumps(payload, indent=1, ensure_ascii=False))
            else:
                await send_discord(client, payload)

    save_json(state_path, state)
    print(f"macro_check: {len(market)} series, active: {[s.key for s in active] or 'none'}, alerted: {[s.key for s in new] or 'none'}.")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Market alarms: post what switched on, or backtest the rules.")
    parser.add_argument(
        "--backtest",
        nargs="?",
        type=date.fromisoformat,
        const=date(1990, 1, 1),
        metavar="START",
        help="score every alert the rules would have sent since START (default: as far back as the data goes)",
    )
    parser.add_argument("--split", type=date.fromisoformat, default=BACKTEST_SPLIT, metavar="DATE", help="also score the periods before and from DATE separately")
    parser.add_argument("--alerts", action="store_true", help="with --backtest, also list every alert")
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    try:
        asyncio.run(run(parse_args(argv)))
    except NotifyError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except httpx.HTTPError as exc:
        print(f"http error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
