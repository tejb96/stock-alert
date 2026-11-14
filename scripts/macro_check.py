#!/usr/bin/env python3
"""Market alarms: Discord alert when a crash signal or a bottom signal switches on.

Silent unless something changes. Signals are judged on completed daily bars only, so every alert
is one the backtest would also have sent: a run during the session posts nothing new, and the
first run after the close posts for that day. A signal alerts when it switches on, not again
while it stays on or within COOLDOWN_DAYS, unless a crash alarm reads WORSE_STEP times worse than
at its last alert. When the last crash alarm switches off and no bottom signal has fired, one
line says so. Signals in UNPOSTED are computed and scored but never posted.

Backtest: python scripts/macro_check.py --backtest
scores every alert the rules would have sent, as far back as each series goes, against SPY.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from common import (
    EMBED_DESCRIPTION_LIMIT,
    EMBED_TITLE_LIMIT,
    MESSAGE_CONTENT_LIMIT,
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
    SIDES,
    SIGNALS,
    WATCH,
    YAHOO_SERIES,
    Intraday,
    IntradayBar,
    Market,
    Series,
    Signal,
    as_of,
    evaluate,
    fetch_intraday,
    fetch_market,
    sessions_on,
    signed,
    unreadable,
)
from state import MACRO_STATE_FILE, load_json, save_json

COOLDOWN_DAYS = 10
FIRED_RETENTION_DAYS = 90
# A crash alarm re-alerts inside its cooldown, or while it stays on, once it reads this many times
# worse than at its last alert. Chosen on data through 2019: see the README.
WORSE_STEP = 1.25
CLEARED_MIN_SESSIONS = 2
# Computed, kept in the state and scored by the backtest, but never posted: neither beat a typical
# day through 2019 or over the whole history. They don't count as a bottom signal for the cleared
# note either, since nobody heard from them.
UNPOSTED = frozenset({"credit_healing", "carry_stabilized"})
# Closes missed by more than this many sessions (the job was off) update the state without posting.
CATCH_UP_SESSIONS = 3
NEW_YORK = ZoneInfo("America/New_York")
SESSION_SETTLED = time(16, 20)  # New York; the VIX prints until 16:15

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
CHECK_HOURS = range(10, 16)
SESSION_FIRST_HOUR = 9
INTRADAY_BAR = timedelta(hours=1)

NEW = "new"
WORSE = "worse"
CLEARED = "cleared"

# Backtest summary rows beyond the signals themselves.
ALL_LABEL = {BEAR: "all crash alarms", BULL: "all bottom signals"}
WORSE_LABEL = "worse than last alert"
CLEARED_LABEL = "cleared, no bottom signal"

# What the messages say about themselves. Every number below is printed by `--backtest` (its
# "Records for the messages" block), run on closing prices from 1993 to Nov 2026 with each signal
# counted from where its data starts. Rerun it and paste to refresh them.
SIDE_STYLE = {
    BEAR: (
        "🔴",
        0xE74C3C,
        "Crash alarm",
        "Volatility is here, not a sell signal: these fire late, most of the fall usually done, and SPY has on average risen over the next 20 days, in bigger swings both ways.",
    ),
    WATCH: (
        "🟠",
        0xE67E22,
        "Early warning",
        "Fires near the highs, before any crash alarm. A reason to check hedges and position size, not a crash call.",
    ),
    BULL: (
        "🟢",
        0x2ECC71,
        "Bottom signal",
        "Much of the rebound is usually gone, and the edge over a typical 20 days shows only since 2020. Scale in, don't go all in.",
    ),
}
RECORD = {
    "carry_unwind": "Since 1996 (59 alerts): 9% below the 3-month high, 58% of the fall done; next 20 days +1.8% (typical +0.7%).",
    "vol_shock": "Since 1993 (95 alerts): 8% below the 3-month high, 68% of the fall done; next 20 days +1.7% (typical +0.8%).",
    "credit_stress": "Since 2007 (57 alerts): 11% below the 3-month high, 67% of the fall done; next 20 days +1.5% (typical +0.8%).",
    "funding_stress": "Since 2021 (4 alerts): 1% below the 3-month high, 20% of the fall done; next 20 days +0.6% (typical +1.0%).",
    "bank_stress": "Since 2006 (13 alerts): 14% below the 3-month high, 71% of the fall done; next 20 days -0.6% (typical +0.8%).",
    "rates_shock": "Since 1993 (30 alerts): 8% below the 3-month high, 66% of the fall done; next 20 days +0.8% (typical +0.8%).",
    "stress_building": "Since 2011 (39 alerts): 3% below the 3-month high; 51% saw a further 3% fall within 20 days (typical 29%).",
    "vol_capitulation": "Since 1993 (35 alerts): next 20 days +1.9% (typical +0.8%); 42% of the rebound already gone; 43% saw a lower low.",
    "vol_normalizing": "Since 2011 (45 alerts): next 20 days +1.7% (typical +1.0%); 66% of the rebound already gone; 11% saw a lower low.",
    WORSE_LABEL: "Since 1993 (65 re-alerts): worst close of the next 20 days -6.8% on average (typical -2.7%).",
}
# Measured while credit healing and yen stabilizing could still hold this note back.
CLEARED_RECORD = "Since 1993 (61 clearings): 32% saw a lower low within 20 days (typical 19%)."


@dataclass(frozen=True)
class Alert:
    signal: Signal
    kind: str  # NEW or WORSE
    previous: dict[str, Any] | None = None  # this signal's last alert, while it is still remembered


@dataclass(frozen=True)
class Cleared:
    """A crash alarm that alerted during a stress episode which has now ended with no bottom signal on."""

    key: str
    title: str
    since: date
    sessions: int


def transitions(
    signals: list[Signal],
    state: dict[str, Any],
    *,
    today: date,
    spy: float | None = None,
    unknown: Iterable[str] = (),
) -> tuple[list[Alert], list[Cleared], dict[str, Any]]:
    """What one completed session changes: alerts to post, alarms that cleared, the state to store.

    `unknown` names signals whose inputs could not be read today. A missing feed is not an alarm
    switching off, so the stress episode is not called over while one of its alarms is unknown.
    """
    on = {s.key for s in signals}
    previously_active = set(state.get("active") or [])
    cutoff = (today - timedelta(days=FIRED_RETENTION_DAYS)).isoformat()
    fired = {k: v for k, v in (state.get("fired") or {}).items() if isinstance(v, str) and v >= cutoff}
    last = {k: v for k, v in (state.get("last") or {}).items() if k in fired}
    opened = {k: dict(v) for k, v in (state.get("open") or {}).items()}

    alerts = []
    for signal in signals:
        previous = last.get(signal.key)
        cooling = signal.key in fired and (today - date.fromisoformat(fired[signal.key])).days < COOLDOWN_DAYS
        if signal.key not in previously_active and not cooling:
            kind = NEW
        elif signal.side == BEAR and previous and signal.severity >= previous["severity"] * WORSE_STEP:
            kind = WORSE
        else:
            kind = None
        if kind:
            alerts.append(Alert(signal, kind, previous))
            fired[signal.key] = today.isoformat()
            last[signal.key] = {
                "day": today.isoformat(),
                "severity": round(signal.severity, 4),
                "spy": round(spy, 2) if spy else None,
                "readings": {r.label: round(r.value, 4) for r in signal.readings},
            }
            if signal.side == BEAR:
                opened.setdefault(signal.key, {"since": today.isoformat(), "sessions": 0, "title": signal.title})
        if signal.key in opened:
            opened[signal.key]["sessions"] += 1

    # The stress episode ends at the first close with no crash alarm on. It gets one note, unless a
    # posted bottom signal came on at any point in it and has already said so, or no alarm lasted
    # beyond a single close (what follows those is no different from any other day).
    bottomed = bool(opened) and (bool(state.get("bottomed")) or any(s.side == BULL and s.key not in UNPOSTED for s in signals))
    cleared = []
    unread = (set(unknown) & set(opened)) - on
    if opened and not unread and not any(s.side == BEAR for s in signals):
        if not bottomed and max(e["sessions"] for e in opened.values()) >= CLEARED_MIN_SESSIONS:
            cleared = [Cleared(key, e["title"], date.fromisoformat(e["since"]), e["sessions"]) for key, e in opened.items()]
        opened, bottomed = {}, False

    new_state = {"asof": today.isoformat(), "active": sorted(on), "fired": fired, "last": last, "open": opened, "bottomed": bottomed}
    return alerts, cleared, new_state


def closed_sessions(market: Market, now: datetime) -> list[date]:
    """Trading days whose daily bars are complete as of `now`."""
    local = now.astimezone(NEW_YORK)
    last = local.date() if local.time() >= SESSION_SETTLED else local.date() - timedelta(days=1)
    vix_days = set(market["VIX"].dates)
    return [d for d in market["SPY"].dates if d <= last and d in vix_days]


@dataclass(frozen=True)
class Step:
    """One completed session, processed: what to post for it (if anything) and the state after it."""

    day: date
    payload: dict[str, Any] | None
    state: dict[str, Any]
    posted: list[str]


def session_steps(market: Market, state: dict[str, Any], now: datetime) -> Iterator[Step]:
    """Every completed session the state hasn't seen yet, oldest first.

    Normally that is one session on the first run after the close and none during the day. More
    than one means runs were missed. A state file from before sessions were tracked starts from
    the latest.
    """
    sessions = closed_sessions(market, now)
    asof = state.get("asof")
    due = [d for d in sessions if d > date.fromisoformat(asof)] if asof else sessions[-1:]
    for day in due:
        view = as_of(market, day)
        active = evaluate(view)
        alerts, cleared, state = transitions(active, state, today=day, spy=view["SPY"].last, unknown=unreadable(view, day))
        alerts = [a for a in alerts if a.signal.key not in UNPOSTED]
        payload, posted = None, []
        if (alerts or cleared) and day in sessions[-CATCH_UP_SESSIONS:]:
            payload = build_payload(alerts, cleared, active, view, day=day, late=day != sessions[-1])
            posted = [f"{day} {a.kind} {a.signal.key}" for a in alerts] + [f"{day} {CLEARED} {c.key}" for c in cleared]
        yield Step(day, payload, state, posted)


# --- messages ----------------------------------------------------------------------------------


def short_date(day: date) -> str:
    return f"{day:%b} {day.day}"


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


def comparison_line(alert: Alert, market: Market) -> str:
    """How the readings have moved since this signal's last alert."""
    previous = alert.previous
    if not previous:
        return ""
    then = previous.get("readings") or {}
    moved = [f"{r.label} {r.text(then[r.label])} → {r.text()}" for r in alert.signal.readings if r.label in then]
    spy = market.get("SPY")
    if spy is not None and len(spy) and previous.get("spy"):
        moved.append(f"SPY {format_pct((spy.last / previous['spy'] - 1) * 100)}")
    if not moved:
        return ""
    lead = "Worse than" if alert.kind == WORSE else "Since"
    return f"↕️ {lead} the {short_date(date.fromisoformat(previous['day']))} alert: " + " · ".join(moved)


def persistence_line(signal: Signal, market: Market) -> str:
    """How long a crash alarm has been on."""
    if signal.side != BEAR:
        return ""
    streak, recent = sessions_on(signal.key, market)
    if not streak:
        return ""
    return f"⏱ Day {streak} of this alarm · on at {recent} of the last 20 closes"


def build_embed(alert: Alert, market: Market) -> dict[str, Any]:
    signal = alert.signal
    emoji, color, label, _ = SIDE_STYLE[signal.side]
    record = RECORD.get(WORSE_LABEL if alert.kind == WORSE else signal.key)
    lines = [*signal.lines, comparison_line(alert, market), persistence_line(signal, market), f"📜 {signal.context}", f"🧮 {record}" if record else ""]
    return {
        "title": clip(f"{emoji} {label}{', worse' if alert.kind == WORSE else ''} — {signal.title}", EMBED_TITLE_LIMIT),
        "color": color,
        "description": clip("\n".join(line for line in lines if line), EMBED_DESCRIPTION_LIMIT),
    }


def cleared_line(cleared: list[Cleared]) -> str:
    """The one-line note for a stress episode that ended without a bottom signal."""
    if not cleared:
        return ""
    alarms = ", ".join(f"**{c.title}** (on at {c.sessions} close{'' if c.sessions == 1 else 's'} from {short_date(c.since)})" for c in cleared)
    return f"⚪ No crash alarm is on any more: {alarms}. Going quiet without a bottom signal is not an all-clear. {CLEARED_RECORD}".rstrip()


def build_payload(
    alerts: list[Alert],
    cleared: list[Cleared],
    active: list[Signal],
    market: Market,
    *,
    day: date,
    late: bool = False,
) -> dict[str, Any]:
    """One message for one completed session. `market` is that session's view (as_of)."""
    header = f"**Market alarms** · close of {day:%a} {short_date(day)}" + (" (sent late: an earlier run was missed)" if late else "")
    lines = [header, snapshot_line(market)]
    for side in SIDES:
        on = [s.title for s in active if s.side == side and s.key not in UNPOSTED]
        if on:
            lines.append(f"{SIDE_STYLE[side][0]} Active: {', '.join(on)}")
    for side in SIDES:
        if any(a.signal.side == side for a in alerts):
            lines.append(f"_{SIDE_STYLE[side][3]}_")
    lines.append(cleared_line(cleared))
    content = clip("\n".join(line for line in lines if line), MESSAGE_CONTENT_LIMIT)
    return {"content": content, "embeds": fit_embeds([build_embed(a, market) for a in alerts])}


# --- backtest ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class Post:
    """Something the rules would have posted after one day's close."""

    day: date
    kind: str  # NEW, WORSE or CLEARED
    key: str
    side: str
    title: str
    lines: tuple[str, ...] = ()

    @property
    def posted(self) -> bool:
        """False for a signal that is scored but never sent (UNPOSTED)."""
        return self.key not in UNPOSTED


def shown(label: str) -> str:
    """A row label in backtest output, marked when that signal is scored but never posted."""
    return f"{label} (not posted)" if label in UNPOSTED else label


def backtest(market: Market, start: date) -> list[Post]:
    """Replay the live rules over each trading day from `start`, posting as the cron would."""
    spy = market.get("SPY")
    if spy is None:
        raise NotifyError("backtest needs SPY for the trading calendar")
    state: dict[str, Any] = {}
    posts = []
    for day in (d for d in spy.dates if d >= start):
        view = as_of(market, day)
        alerts, cleared, state = transitions(evaluate(view), state, today=day, spy=view["SPY"].last, unknown=unreadable(view, day))
        posts += [Post(day, a.kind, a.signal.key, a.signal.side, a.signal.title, a.signal.lines) for a in alerts]
        if cleared:
            posts.append(Post(day, CLEARED, CLEARED, BEAR, "Crash alarms cleared: " + ", ".join(c.title for c in cleared)))
    return posts


@dataclass(frozen=True)
class Outcome:
    """What SPY did around one day. Percentages; None where the data ends before the horizon."""

    day: date
    forward: tuple[float | None, ...]  # return over each of FORWARD_DAYS
    drawdown: tuple[float | None, ...]  # worst close over each of DRAWDOWN_DAYS, vs that day's close
    off_high: float  # vs the highest close of the EPISODE_DAYS before
    to_trough: float | None  # further fall to the lowest close of the EPISODE_DAYS after
    days_to_trough: int | None
    off_low: float  # vs the lowest close of the EPISODE_DAYS before
    days_from_low: int
    upside: float | None  # rise to the highest close of the REBOUND_DAYS after
    new_low: bool | None  # closed below that earlier low within REBOUND_DAYS

    @property
    def drop_done(self) -> float | None:
        """Share of the high-to-trough fall that was already behind SPY."""
        if self.to_trough is None:
            return None
        total = (1 + self.off_high / 100) * (1 + self.to_trough / 100) - 1
        return self.off_high / 100 / total * 100 if total < 0 else None

    @property
    def rebound_missed(self) -> float | None:
        """Share of the low-to-next-high rise that was already gone."""
        if self.upside is None:
            return None
        total = (1 + self.off_low / 100) * (1 + self.upside / 100) - 1
        return self.off_low / 100 / total * 100 if total > 0 else None

    @property
    def false_alarm(self) -> bool | None:
        worst = self.drawdown[DRAWDOWN_DAYS.index(FALSE_ALARM_DAYS)]
        return None if worst is None else worst > FALSE_ALARM_DRAWDOWN


def score(closes: tuple[float, ...], idx: int, day: date) -> Outcome:
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


def num(value: float | None, spec: str = "+.1f") -> str:
    return "n/a" if value is None else signed(value, spec)


def first_possible_day(market: Market, key: str) -> date | None:
    """First day a signal has the series it needs, so years it could not fire don't count as quiet."""
    needed = [market.get(name) for name in SIGNALS[key].needs]
    if any(s is None for s in needed):
        return None
    return max(s.dates[0] for s in needed) + timedelta(days=SIGNAL_WARMUP_DAYS)


@dataclass(frozen=True)
class Group:
    """One row of the summary: a set of posts and the days they could have happened on."""

    label: str
    side: str
    hits: list[Outcome]
    every: list[Outcome]
    since: date
    last: date

    @property
    def per_year(self) -> float:
        return len(self.hits) / max((self.last - self.since).days / 365.25, 1e-9)


def groups(market: Market, posts: list[Post], first: date, last: date) -> list[Group]:
    """Posts in [first, last] by signal (first alerts), then every escalation, then every clearing."""
    spy = market["SPY"]
    everyday = {d: score(spy.values, i, d) for i, d in enumerate(spy.dates) if first <= d <= last}

    def group(label: str, side: str, since: date, wanted: Iterable[Post]) -> Group:
        hits = [everyday[p.day] for p in wanted if since <= p.day <= last]
        return Group(label, side, hits, [o for d, o in everyday.items() if d >= since], since, last)

    out = []
    for key, spec in SIGNALS.items():
        possible = first_possible_day(market, key)
        if possible is not None and possible <= last:
            out.append(group(key, spec.side, max(first, possible), (p for p in posts if p.key == key and p.kind == NEW)))
    for side, label in ALL_LABEL.items():
        out.append(group(label, side, first, (p for p in posts if p.side == side and p.kind == NEW)))
    out.append(group(WORSE_LABEL, BEAR, first, (p for p in posts if p.kind == WORSE)))
    out.append(group(CLEARED_LABEL, BEAR, first, (p for p in posts if p.kind == CLEARED)))
    return out


def summary_rows(found: list[Group], side: str) -> list[list[str]]:
    """Each group's numbers, followed by the same numbers for any day it could have fired on."""
    rows = []
    for g in (g for g in found if g.side == side):
        for label, outcomes in ((shown(g.label), g.hits), (f"  any day since {g.since:%Y-%m}", g.every)):
            row = [label, str(len(outcomes)), f"{g.per_year:.1f}"] if outcomes is g.hits else [label, "", ""]
            row += [num(mean(o.forward[i] for o in outcomes)) for i in range(len(FORWARD_DAYS))]
            if side != BULL:
                row += [num(mean(o.drawdown[i] for o in outcomes)) for i in range(len(DRAWDOWN_DAYS))]
                row += [
                    num(share(o.false_alarm for o in outcomes), ".0f"),
                    num(mean(o.off_high for o in outcomes)),
                    num(mean(o.drop_done for o in outcomes), ".0f"),
                    num(median(o.days_to_trough for o in outcomes), ".0f"),
                ]
            else:
                row += [
                    num(median(o.days_from_low for o in outcomes), ".0f"),
                    num(mean(o.off_low for o in outcomes)),
                    num(mean(o.upside for o in outcomes)),
                    num(mean(o.rebound_missed for o in outcomes), ".0f"),
                    num(share(o.new_low for o in outcomes), ".0f"),
                ]
            rows.append(row)
    return rows


def volume_line(spy: Series, posts: list[Post], first: date, last: date) -> str:
    """How often something posts: the bar is that most days nothing does. UNPOSTED signals don't count."""
    sessions = sum(first <= d <= last for d in spy.dates)
    mine = [p for p in posts if first <= p.day <= last and p.posted]
    days = len({p.day for p in mine})
    years = max((last - first).days / 365.25, 1e-9)
    kinds = ", ".join(f"{sum(p.kind == kind for p in mine) / years:.1f} {kind}" for kind in (NEW, WORSE, CLEARED))
    return f"Posts: {len(mine)} on {days} of {sessions} sessions ({days / max(sessions, 1) * 100:.1f}%). Per year: {kinds}; {days / years:.1f} posting days."


FALL_COLUMNS = ("signal", "n", "/yr", *(f"+{d}d" for d in FORWARD_DAYS), *(f"dd{d}" for d in DRAWDOWN_DAYS), "false%", "offhigh", "done%", "d>trough")
SUMMARY_COLUMNS = {
    BEAR: FALL_COLUMNS,
    WATCH: FALL_COLUMNS,
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


def record_line(g: Group) -> str | None:
    """A group's record as one short line for the Discord message ("typical" is any day it could have fired on)."""
    if not g.hits:
        return None
    twenty, worst = FORWARD_DAYS.index(20), DRAWDOWN_DAYS.index(20)
    noun = {CLEARED_LABEL: "clearings", WORSE_LABEL: "re-alerts"}.get(g.label, "alerts")
    lead = f"Since {g.since.year} ({len(g.hits)} {noun})"

    def fell(outcomes: list[Outcome]) -> str:
        return num(share(None if o.false_alarm is None else not o.false_alarm for o in outcomes), ".0f")

    if g.label == CLEARED_LABEL:
        return f"{lead}: {num(share(o.new_low for o in g.hits), '.0f')}% saw a lower low within 20 days (typical {num(share(o.new_low for o in g.every), '.0f')}%)."
    if g.label == WORSE_LABEL:
        return (
            f"{lead}: worst close of the next 20 days {num(mean(o.drawdown[worst] for o in g.hits))}% on average "
            f"(typical {num(mean(o.drawdown[worst] for o in g.every))}%)."
        )
    later = f"next 20 days {num(mean(o.forward[twenty] for o in g.hits))}% (typical {num(mean(o.forward[twenty] for o in g.every))}%)"
    below = f"{num(-mean(o.off_high for o in g.hits), '.0f')}% below the 3-month high"
    if g.side == BEAR:
        return f"{lead}: {below}, {num(mean(o.drop_done for o in g.hits), '.0f')}% of the fall done; {later}."
    if g.side == WATCH:
        return f"{lead}: {below}; {fell(g.hits)}% saw a further 3% fall within 20 days (typical {fell(g.every)}%)."
    return (
        f"{lead}: {later}; {num(mean(o.rebound_missed for o in g.hits), '.0f')}% of the rebound already gone; "
        f"{num(share(o.new_low for o in g.hits), '.0f')}% saw a lower low."
    )


def print_summary(market: Market, posts: list[Post], start: date, split: date | None) -> None:
    spy = market["SPY"]
    end = spy.dates[-1]
    periods = [(f"{start} to {end}", start, end)]
    if split and start < split <= end:
        periods += [(f"{start} to {split - timedelta(days=1)}", start, split - timedelta(days=1)), (f"{split} to {end}", split, end)]
    print("Series history:  " + "  ".join(f"{k} {s.dates[0]:%Y-%m}" for k, s in sorted(market.items(), key=lambda kv: kv[1].dates[0])))
    for label, first, last in periods:
        found = groups(market, posts, first, last)
        for side in SIDES:
            if rows := summary_rows(found, side):
                print(f"\n{SIDE_STYLE[side][2]}s, {label}\n{format_table(SUMMARY_COLUMNS[side], rows)}")
        print(volume_line(spy, posts, first, last))
    for side in (BEAR, BULL):
        print(f"\n{SIDE_STYLE[side][2]}{' and early warning' if side == BEAR else ''} columns. {SUMMARY_LEGEND[side]}")
    print("Horizons that run past the end of the data are left out of the averages.")
    print(f"\nRecords for the messages (RECORD and CLEARED_RECORD), {start} to {end}:")
    for g in groups(market, posts, start, end):
        if g.label not in UNPOSTED and (line := record_line(g)):
            print(f'    "{g.label}": "{line}",')


def print_posts(market: Market, posts: list[Post]) -> None:
    spy = market["SPY"]
    index = {d: i for i, d in enumerate(spy.dates)}
    print(f"\n{'date':<11} {'kind':<8} {'signal':<34} " + " ".join(f"SPY+{d}d".rjust(8) for d in FORWARD_DAYS))
    for post in posts:
        fwd = " ".join(format_pct(f).rjust(8) for f in score(spy.values, index[post.day], post.day).forward)
        title = post.title if post.posted else f"{post.title} (not posted)"
        print(f"{post.day.isoformat():<11} {post.kind:<8} {title:<34} {fwd}")
        for line in post.lines:
            print(f"{'':<21}{line.replace('**', '')}")


# --- intraday replay ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Trigger:
    """A signal that was off at the previous close and came on at a mid-session check."""

    day: date
    signal: Signal
    at: datetime
    held: bool  # still on at that day's close
    spy_to_close: float  # % SPY moved between the check and the close


def intraday_view(market: Market, bars: dict[str, dict[date, list[IntradayBar]]], day: date, at: datetime) -> Market:
    """What a run at `at` on trading day `day` would have fetched: `day`'s bar still forming."""
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
        fresh: dict[str, tuple[Signal, datetime, float]] = {}
        for hour in CHECK_HOURS:
            at = datetime(day.year, day.month, day.day, hour, 30, tzinfo=NEW_YORK).astimezone(UTC)
            view = intraday_view(market, bars, day, at)
            for signal in evaluate(view):
                if signal.key not in on_before:
                    fresh.setdefault(signal.key, (signal, at, view["SPY"].last))
        on_close = {s.key for s in evaluate(as_of(market, day))}
        triggers += [Trigger(day, s, at, s.key in on_close, (closes[day] / price - 1) * 100) for s, at, price in fresh.values()]
        on_before = on_close
    return triggers, days


def print_intraday(market: Market, intraday: Intraday, posts: list[Post]) -> None:
    triggers, days = replay_intraday(market, intraday)
    if not days:
        print("\nIntraday: no hourly bars were available, so mid-session triggers could not be checked against the close.")
        return
    rows = []
    for key, spec in SIGNALS.items():
        mine = [t for t in triggers if t.signal.key == key]
        rows.append(
            [
                shown(key),
                spec.side,
                str(len(mine)),
                str(sum(t.held for t in mine)),
                num(share(not t.held for t in mine), ".0f"),
                num(mean(t.spy_to_close for t in mine if t.held)),
                num(mean(t.spy_to_close for t in mine if not t.held)),
                str(sum(p.key == key and p.kind == NEW and days[0] <= p.day <= days[-1] for p in posts)),
            ]
        )
    rows.append(["all", "", str(len(triggers)), str(sum(t.held for t in triggers)), num(share(not t.held for t in triggers), ".0f"), "", "", ""])
    header = ("signal", "side", "midday", "held", "lost%", "spy>close held", "spy>close lost", "close alerts")
    print(f"\nMid-session triggers vs the close, {days[0]} to {days[-1]} ({len(days)} sessions, checks at :30 past {CHECK_HOURS[0]}-{CHECK_HOURS[-1]}h New York)")
    print(format_table(header, rows))
    print(
        "midday: days a signal that was off at the previous close came on at an hourly check. held: still on at that day's close. "
        "lost%: share that were off again by the close, which a close-only replay never sees. spy>close: mean SPY move from the "
        "check to the close. close alerts: first alerts the daily replay sent over the same days. Yahoo serves hourly bars for "
        "about two years, so earlier history cannot be checked this way. Hourly closes miss spikes inside the hour, and the VIX "
        "bars end on the hour while the others end on the half hour, so lost% is a floor."
    )


# --- entry point -------------------------------------------------------------------------------


async def run(args: argparse.Namespace) -> None:
    user_agent = env("YAHOO_USER_AGENT", "stock_alert-cron/1.0")
    async with httpx.AsyncClient(timeout=20.0, headers={"User-Agent": user_agent}) as client:
        fetch_start = args.backtest - timedelta(days=BACKTEST_WARMUP_DAYS) if args.backtest else None
        market = await fetch_market(client, start=fetch_start)
        if "VIX" not in market or "SPY" not in market:
            raise NotifyError("core series (VIX, SPY) could not be fetched")

        if args.backtest:
            start = max(args.backtest, market["SPY"].dates[0])
            posts = backtest(market, start)
            print_summary(market, posts, start, args.split)
            print_intraday(market, await fetch_intraday(client), posts)
            if args.alerts:
                print_posts(market, posts)
            return

        state_path = Path(env("STATE_DIR", "state")) / MACRO_STATE_FILE
        state = load_json(state_path, {})
        processed, posted = [], []
        for step in session_steps(market, state, datetime.now(UTC)):
            if step.payload and env_bool("DRY_RUN", False):
                print(json.dumps(step.payload, indent=1, ensure_ascii=False))
            elif step.payload:
                await send_discord(client, step.payload)
            state = step.state
            save_json(state_path, state)
            processed.append(step.day.isoformat())
            posted += step.posted

    missing = sorted((set(YAHOO_SERIES) | set(FRED_SERIES)) - set(market))
    print(
        f"macro_check: {len(market)} series (missing: {missing or 'none'}), closes processed: {processed or 'none'}, "
        f"on at last close: {state.get('active') or 'none'}, posted: {posted or 'none'}, "
        f"on with the latest bars: {[s.key for s in evaluate(market)] or 'none'}."
    )


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Market alarms: post what changed at the last close, or backtest the rules.")
    parser.add_argument(
        "--backtest",
        nargs="?",
        type=date.fromisoformat,
        const=date(1990, 1, 1),
        metavar="START",
        help="score every alert the rules would have sent since START (default: as far back as the data goes)",
    )
    parser.add_argument("--split", type=date.fromisoformat, default=BACKTEST_SPLIT, metavar="DATE", help="also score the periods before and from DATE separately")
    parser.add_argument("--alerts", action="store_true", help="with --backtest, also list every post")
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
