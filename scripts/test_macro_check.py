"""Tests for macro_check.py — run from repo root: python -m pytest scripts"""

from __future__ import annotations

from datetime import UTC, date, datetime

from macro import BEAR, BULL, Signal
from macro_check import backtest, build_payload, transitions
from test_macro import series

TODAY = date(2026, 9, 24)
SHOCK = Signal("vol_shock", BEAR, "Volatility shock", ("VIX **32.0**",), "context")
WASHOUT = Signal("vol_capitulation", BULL, "Volatility capitulation", ("VIX spiked",), "context")


def test_transitions_alert_only_when_switching_on():
    new, state = transitions([SHOCK], {}, today=TODAY)
    assert new == [SHOCK]
    assert state == {"active": ["vol_shock"], "fired": {"vol_shock": "2026-09-24"}}
    again, _ = transitions([SHOCK], state, today=date(2026, 9, 25))
    assert again == []


def test_transitions_cooldown_suppresses_flicker():
    state = {"active": [], "fired": {"vol_shock": "2026-09-20"}}
    assert transitions([SHOCK], state, today=TODAY)[0] == []
    assert transitions([SHOCK], state, today=date(2026, 10, 1))[0] == [SHOCK]


def test_transitions_prunes_old_fired_dates():
    _, state = transitions([], {"active": ["vol_shock"], "fired": {"vol_shock": "2025-01-01"}}, today=TODAY)
    assert state == {"active": [], "fired": {}}


def test_build_payload_lists_active_and_guidance():
    market = {"SPY": series(700, 690, 650), "VIX": series(15, 25, 32)}
    payload = build_payload([SHOCK], [SHOCK, WASHOUT], market, when=datetime(2026, 9, 24, 15, 25, tzinfo=UTC))
    content = payload["content"]
    assert "🔴 Active: Volatility shock" in content
    assert "🟢 Active: Volatility capitulation" in content
    assert "cut leverage" in content
    assert "take profit" not in content  # guidance only for sides that just alerted
    assert payload["embeds"][0]["title"] == "🔴 Crash alarm — Volatility shock"
    assert "📜 context" in payload["embeds"][0]["description"]


def test_backtest_replays_daily_with_cooldown():
    market = {"SPY": series(*[700] * 6), "VIX": series(15, 32, 15, 33, 15, 15)}
    alerts = backtest(market, date(2026, 1, 1))
    assert [(d.day, s.key) for d, s in alerts] == [(2, "vol_shock")]  # the day-4 repeat is inside the cooldown
