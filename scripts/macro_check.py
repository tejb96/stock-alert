#!/usr/bin/env python3
"""Market alarms: Discord alert when a crash signal or a bottom signal switches on.

Silent unless something changes. A signal alerts when it turns on, and not again while it stays on
or within COOLDOWN_DAYS of its last alert, so a signal flickering around its threshold intraday
doesn't spam.

Backtest: python scripts/macro_check.py --backtest 2020-01-01
prints every alert the rules would have sent since then, with SPY's return afterwards.
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import UTC, date, datetime, timedelta
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
    fit_embeds,
    format_pct,
    send_discord,
)
from macro import BEAR, BULL, Market, Signal, as_of, evaluate, fetch_market
from state import MACRO_STATE_FILE, load_json, save_json

COOLDOWN_DAYS = 10
FIRED_RETENTION_DAYS = 90
# History needed before the first backtest day so every signal has its lookback.
BACKTEST_WARMUP_DAYS = 150
FORWARD_DAYS = (5, 20)

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


def forward_return(market: Market, day: date, days: int) -> float | None:
    spy = market["SPY"]
    idx = spy.dates.index(day)
    if idx + days >= len(spy):
        return None
    return (spy.values[idx + days] / spy.values[idx] - 1) * 100


def print_backtest(market: Market, alerts: list[tuple[date, Signal]]) -> None:
    header = f"{'date':<11} {'side':<5} {'signal':<34} " + " ".join(f"SPY+{d}d".rjust(8) for d in FORWARD_DAYS)
    print(header)
    for day, signal in alerts:
        fwd = " ".join(format_pct(forward_return(market, day, d)).rjust(8) for d in FORWARD_DAYS)
        print(f"{day.isoformat():<11} {signal.side:<5} {signal.title:<34} {fwd}")
        for line in signal.lines:
            print(f"{'':<18}{line.replace('**', '')}")
    print(f"\n{len(alerts)} alerts ({sum(s.side == BEAR for _, s in alerts)} bear, {sum(s.side == BULL for _, s in alerts)} bull)")


async def run(backtest_start: date | None) -> None:
    user_agent = env("YAHOO_USER_AGENT", "stock_alert-cron/1.0")
    async with httpx.AsyncClient(timeout=20.0, headers={"User-Agent": user_agent}) as client:
        fetch_start = backtest_start - timedelta(days=BACKTEST_WARMUP_DAYS) if backtest_start else None
        market = await fetch_market(client, start=fetch_start)
        if "VIX" not in market or "SPY" not in market:
            raise NotifyError("core series (VIX, SPY) could not be fetched")

        if backtest_start:
            print_backtest(market, backtest(market, backtest_start))
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


def main(argv: list[str]) -> int:
    backtest_start = None
    if len(argv) >= 2 and argv[0] == "--backtest":
        backtest_start = date.fromisoformat(argv[1])
    try:
        asyncio.run(run(backtest_start))
    except NotifyError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except httpx.HTTPError as exc:
        print(f"http error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
