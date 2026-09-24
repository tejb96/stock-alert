"""Tests for state.py — run from repo root: python -m pytest scripts"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from factories import make_trend
from market import STAGE_EARLY, STAGE_LATE
from state import (
    COOLING_MULTIPLIER,
    AlertRecord,
    Trajectory,
    apply_history,
    empty_digest_state,
    iso,
    load_json,
    momentum_multiplier,
    record_run,
    save_json,
    score_alerts,
    scorecard_alerts,
    summarize_by_stage,
    trajectory_for,
)

NOW = datetime(2026, 9, 23, 12, 37, tzinfo=UTC)


def snapshot(hours_ago: float, rows: dict[str, int], shown: list[str]) -> dict:
    return {
        "ts": iso(NOW - timedelta(hours=hours_ago)),
        "rows": {t: [m, 10] for t, m in rows.items()},
        "shown": shown,
    }


def test_trajectory_streak_and_mentions():
    snapshots = [
        snapshot(30, {"ACME": 5}, []),
        snapshot(20, {"ACME": 12}, ["ACME"]),
        snapshot(10, {"ACME": 41}, ["ACME"]),
    ]
    trajectory = trajectory_for("ACME", 96, snapshots)
    assert trajectory.streak == 2
    assert trajectory.mentions == [5, 12, 41, 96]
    assert trajectory.rising_steps == 3
    assert not trajectory.cooling


def test_trajectory_stops_at_gap():
    snapshots = [
        snapshot(30, {"ACME": 50}, ["ACME"]),
        snapshot(20, {}, []),
        snapshot(10, {"ACME": 8}, []),
    ]
    trajectory = trajectory_for("ACME", 20, snapshots)
    assert trajectory.streak == 0
    assert trajectory.mentions == [8, 20]


def test_trajectory_new_ticker():
    trajectory = trajectory_for("NEW", 9, [snapshot(10, {"OTHER": 5}, ["OTHER"])])
    assert trajectory == Trajectory(streak=0, mentions=[9])
    assert momentum_multiplier(trajectory) == 1.0


def test_trajectory_caps_points():
    snapshots = [snapshot(h, {"ACME": 100 - h}, []) for h in (60, 50, 40, 30, 20, 10)]
    assert len(trajectory_for("ACME", 100, snapshots).mentions) == 4


def test_momentum_multiplier():
    assert momentum_multiplier(Trajectory(2, [10, 20, 40])) == pytest.approx(1.30)
    assert momentum_multiplier(Trajectory(5, [1, 2, 3, 4, 5, 6])) == pytest.approx(1.45)
    assert momentum_multiplier(Trajectory(1, [40, 30])) == COOLING_MULTIPLIER
    assert momentum_multiplier(Trajectory(1, [10, 50, 50])) == 1.0


def test_apply_history_boosts_sustained_buzz():
    rows = [make_trend("STEADY", mentions=40, mentions_24h_ago=10), make_trend("ONEOFF", mentions=40, mentions_24h_ago=10)]
    snapshots = [snapshot(20, {"STEADY": 10}, []), snapshot(10, {"STEADY": 25, "ONEOFF": 60}, [])]
    adjusted, trajectories = apply_history(rows, snapshots)
    by_ticker = {r.ticker: r for r in adjusted}
    assert by_ticker["STEADY"].trend_score == pytest.approx(rows[0].trend_score * 1.30)
    assert by_ticker["ONEOFF"].trend_score == pytest.approx(rows[1].trend_score * COOLING_MULTIPLIER)
    assert trajectories["STEADY"].mentions == [10, 25, 40]


def test_record_run_marks_new_streaks_and_prunes():
    old = {"ts": iso(NOW - timedelta(days=40)), "ticker": "OLD", "price": 1.0, "new": True}
    state = {
        "snapshots": [snapshot(24 * 20, {"X": 5}, []), snapshot(10, {"ACME": 20}, ["ACME"])],
        "alerts": [old],
    }
    rows = [make_trend("ACME", mentions=40), make_trend("NEXT", mentions=30), make_trend("TINY", mentions=2)]
    alerts = [AlertRecord("ACME", 4.12, STAGE_EARLY, 12.3), AlertRecord("NEXT", 8.0, STAGE_LATE, 9.0)]
    result = record_run(state, now=NOW, rows=rows, alerts=alerts, spy_price=500.0, min_mentions=5)

    assert len(result["snapshots"]) == 2
    latest = result["snapshots"][-1]
    assert latest["ts"] == "2026-09-23T12:37:00Z"
    assert set(latest["rows"]) == {"ACME", "NEXT"}
    assert latest["shown"] == ["ACME", "NEXT"]

    assert [a["ticker"] for a in result["alerts"]] == ["ACME", "NEXT"]
    assert [a["new"] for a in result["alerts"]] == [False, True]
    assert result["alerts"][0]["spy"] == 500.0


def test_state_round_trip(tmp_path):
    path = tmp_path / "nested" / "digest.json"
    assert load_json(path, empty_digest_state()) == {"snapshots": [], "alerts": []}
    save_json(path, {"alerts": [1]})
    assert load_json(path, {}) == {"alerts": [1]}
    path.write_text("{corrupt")
    assert load_json(path, {"fallback": True}) == {"fallback": True}


def alert(ticker: str, days_ago: float, price: float, stage: str | None, *, new: bool = True, spy: float = 100.0):
    return {"ts": iso(NOW - timedelta(days=days_ago)), "ticker": ticker, "price": price, "stage": stage, "new": new, "spy": spy}


def test_scorecard_alerts_window():
    alerts = [
        alert("FRESH", 0.2, 10, STAGE_EARLY),
        alert("OK", 3, 10, STAGE_EARLY),
        alert("REPEAT", 3, 10, STAGE_EARLY, new=False),
        alert("STALE", 20, 10, STAGE_EARLY),
    ]
    assert [a["ticker"] for a in scorecard_alerts(alerts, now=NOW)] == ["OK"]


def test_score_and_summarize():
    alerts = [
        alert("WIN", 3, 10.0, STAGE_EARLY),
        alert("LOSS", 3, 10.0, STAGE_EARLY),
        alert("CHASE", 3, 10.0, STAGE_LATE),
        alert("GONE", 3, 10.0, STAGE_LATE),
    ]
    scored = score_alerts(alerts, {"WIN": 13.0, "LOSS": 9.0, "CHASE": 10.5}, spy_now=102.0)
    assert [s.ticker for s in scored] == ["WIN", "LOSS", "CHASE"]
    assert scored[0].ret == pytest.approx(30.0)
    assert scored[0].excess == pytest.approx(28.0)

    stats = summarize_by_stage(scored)
    assert stats[STAGE_EARLY].count == 2
    assert stats[STAGE_EARLY].avg_return == pytest.approx(10.0)
    assert stats[STAGE_EARLY].win_rate == 50.0
    # +5% but SPY +2% → still beat the market.
    assert stats[STAGE_LATE].avg_excess == pytest.approx(3.0)
    assert stats[STAGE_LATE].win_rate == 100.0
