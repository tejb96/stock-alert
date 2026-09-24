"""Run history kept between cron runs: mention trajectories, streaks, and alert outcomes.

State lives as JSON files in STATE_DIR, which CI checks out from the `state` branch and
pushes back after each run. Missing or corrupt files just mean starting fresh.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from reddit import TrendRow

DIGEST_STATE_FILE = "digest.json"
SEC_STATE_FILE = "sec.json"

SNAPSHOT_RETENTION_DAYS = 14
ALERT_RETENTION_DAYS = 30
TRAJECTORY_POINTS = 4

# Score multiplier per consecutive run of rising mentions (capped), and for cooling off.
MOMENTUM_STEP_BONUS = 0.15
MOMENTUM_MAX_STEPS = 3
COOLING_MULTIPLIER = 0.8


def iso(ts: datetime) -> str:
    return ts.isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_iso(raw: str) -> datetime:
    return datetime.fromisoformat(raw.replace("Z", "+00:00"))


def load_json(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        return default
    except (OSError, ValueError) as exc:
        print(f"state: ignoring unreadable {path}: {exc}", file=sys.stderr)
        return default
    return data if isinstance(data, dict) else default


def save_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=1, sort_keys=True) + "\n")


def empty_digest_state() -> dict[str, Any]:
    return {"snapshots": [], "alerts": []}


@dataclass(frozen=True)
class Trajectory:
    streak: int
    """Consecutive previous digests this ticker was shown in."""
    mentions: list[int]
    """Mentions in consecutive recent runs, oldest → newest, ending with this run."""

    @property
    def rising_steps(self) -> int:
        steps = 0
        for older, newer in zip(reversed(self.mentions[:-1]), reversed(self.mentions[1:])):
            if newer <= older:
                break
            steps += 1
        return steps

    @property
    def cooling(self) -> bool:
        return len(self.mentions) >= 2 and self.mentions[-1] < self.mentions[-2]


def trajectory_for(ticker: str, mentions_now: int, snapshots: list[dict[str, Any]]) -> Trajectory:
    streak = 0
    for snapshot in reversed(snapshots):
        if ticker not in snapshot.get("shown", []):
            break
        streak += 1

    history: list[int] = []
    for snapshot in reversed(snapshots):
        row = snapshot.get("rows", {}).get(ticker)
        if row is None or len(history) >= TRAJECTORY_POINTS - 1:
            break
        history.append(int(row[0]))
    return Trajectory(streak=streak, mentions=[*reversed(history), mentions_now])


def momentum_multiplier(trajectory: Trajectory) -> float:
    if trajectory.cooling:
        return COOLING_MULTIPLIER
    return 1 + MOMENTUM_STEP_BONUS * min(trajectory.rising_steps, MOMENTUM_MAX_STEPS)


def apply_history(
    rows: list[TrendRow],
    snapshots: list[dict[str, Any]],
) -> tuple[list[TrendRow], dict[str, Trajectory]]:
    """Scale each row's score by its multi-run momentum; return rows and their trajectories."""
    adjusted: list[TrendRow] = []
    trajectories: dict[str, Trajectory] = {}
    for row in rows:
        trajectory = trajectory_for(row.ticker, row.mentions, snapshots)
        trajectories[row.ticker] = trajectory
        adjusted.append(replace(row, trend_score=row.trend_score * momentum_multiplier(trajectory)))
    return adjusted, trajectories


@dataclass(frozen=True)
class AlertRecord:
    ticker: str
    price: float
    stage: str | None
    score: float


def record_run(
    state: dict[str, Any],
    *,
    now: datetime,
    rows: list[TrendRow],
    alerts: list[AlertRecord],
    spy_price: float | None,
    min_mentions: int,
) -> dict[str, Any]:
    snapshots = list(state.get("snapshots", []))
    previously_shown = set(snapshots[-1].get("shown", [])) if snapshots else set()
    snapshots.append(
        {
            "ts": iso(now),
            "rows": {r.ticker: [r.mentions, r.rank] for r in rows if r.mentions >= min_mentions},
            "shown": [a.ticker for a in alerts],
        }
    )

    alert_log = list(state.get("alerts", []))
    for alert in alerts:
        alert_log.append(
            {
                "ts": iso(now),
                "ticker": alert.ticker,
                "price": round(alert.price, 4),
                "stage": alert.stage,
                "score": round(alert.score, 2),
                "spy": round(spy_price, 4) if spy_price else None,
                # First appearance of a streak; the scorecard judges each streak once, from its start.
                "new": alert.ticker not in previously_shown,
            }
        )

    snapshot_cutoff = now - timedelta(days=SNAPSHOT_RETENTION_DAYS)
    alert_cutoff = now - timedelta(days=ALERT_RETENTION_DAYS)
    return {
        **state,
        "snapshots": [s for s in snapshots if parse_iso(s["ts"]) >= snapshot_cutoff],
        "alerts": [a for a in alert_log if parse_iso(a["ts"]) >= alert_cutoff],
    }


@dataclass(frozen=True)
class ScoredAlert:
    ticker: str
    stage: str | None
    ts: datetime
    ret: float
    excess: float | None


@dataclass(frozen=True)
class StageStats:
    count: int
    avg_return: float
    avg_excess: float | None
    win_rate: float


def scorecard_alerts(
    alerts: list[dict[str, Any]],
    *,
    now: datetime,
    window_days: int = 14,
    min_age_hours: int = 20,
) -> list[dict[str, Any]]:
    """Streak-starting alerts old enough to judge."""
    earliest = now - timedelta(days=window_days)
    latest = now - timedelta(hours=min_age_hours)
    return [a for a in alerts if a.get("new") and a.get("price") and earliest <= parse_iso(a["ts"]) <= latest]


def score_alerts(
    alerts: list[dict[str, Any]],
    prices: dict[str, float],
    spy_now: float | None,
) -> list[ScoredAlert]:
    scored: list[ScoredAlert] = []
    for alert in alerts:
        price_now = prices.get(alert["ticker"])
        if price_now is None:
            continue
        ret = (price_now / alert["price"] - 1) * 100
        excess = None
        if spy_now and alert.get("spy"):
            excess = ret - (spy_now / alert["spy"] - 1) * 100
        scored.append(ScoredAlert(alert["ticker"], alert.get("stage"), parse_iso(alert["ts"]), ret, excess))
    return scored


def summarize_by_stage(scored: list[ScoredAlert]) -> dict[str | None, StageStats]:
    by_stage: dict[str | None, list[ScoredAlert]] = {}
    for item in scored:
        by_stage.setdefault(item.stage, []).append(item)

    summary: dict[str | None, StageStats] = {}
    for stage, items in by_stage.items():
        excesses = [i.excess for i in items if i.excess is not None]
        # A "win" beats the market when we know SPY's move, otherwise just goes up.
        wins = sum(1 for i in items if (i.excess if i.excess is not None else i.ret) > 0)
        summary[stage] = StageStats(
            count=len(items),
            avg_return=sum(i.ret for i in items) / len(items),
            avg_excess=sum(excesses) / len(excesses) if excesses else None,
            win_rate=wins / len(items) * 100,
        )
    return summary
