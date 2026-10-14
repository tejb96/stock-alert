"""Tests for notify.py — run from repo root: python -m pytest scripts"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime

from common import EMBED_TOTAL_LIMIT, embed_size
from factories import make_quote, make_trend
from market import STAGE_EARLY, STAGE_LATE, STAGE_MOVING, EarningsEvent
from notify import (
    Signal,
    alert_reasons,
    already_ran_today,
    build_alert_payload,
    build_recap_payload,
    build_scorecard_embed,
    build_signal_embed,
    build_signals,
    format_earnings_line,
    format_trajectory_line,
)
from sec import SecActivity
from state import ScoredAlert, StageStats, Trajectory, WeekSummary
from test_sec import make_form4

TODAY = date(2026, 9, 23)


def signal(ticker: str = "ACME", stage: str | None = STAGE_EARLY, **kwargs) -> Signal:
    return Signal(make_trend(ticker), make_quote(ticker), stage, **kwargs)


def test_build_signals_drops_junk_and_caps():
    candidates = [make_trend(t) for t in ("ETF", "PENNY", "WORD", "GOOD", "ALSO", "EXTRA")]
    quotes = {
        "ETF": make_quote("ETF", instrument_type="ETF"),
        "PENNY": make_quote("PENNY", price=0.4),
        "GOOD": make_quote("GOOD"),
        "ALSO": make_quote("ALSO", change_5d=40.0),
        "EXTRA": make_quote("EXTRA"),
    }
    trajectories = {"GOOD": Trajectory(1, [10, 58])}
    signals = build_signals(candidates, quotes, min_price=1.0, top_n=2, trajectories=trajectories)
    assert [s.trend.ticker for s in signals] == ["GOOD", "ALSO"]
    assert [s.stage for s in signals] == [STAGE_EARLY, STAGE_LATE]
    assert signals[0].trajectory == Trajectory(1, [10, 58])
    assert signals[1].trajectory is None


def test_build_signal_embed_content():
    embed = build_signal_embed(1, signal(), today=TODAY)
    assert embed["title"] == "1. ACME — Acme Corp"
    assert embed["url"] == "https://finance.yahoo.com/quote/ACME"
    assert embed["color"] == 0x2ECC71
    assert embed["description"].startswith("🟢 **Early** (price quiet over the last 5d)")
    assert "$4.12" in embed["description"]
    assert "6 → **58** mentions (+866.7%)" in embed["description"]
    assert "rank #212 → **#31**" in embed["description"]
    assert "fields" not in embed


def test_build_signal_embed_new_ticker_wording():
    trend = make_trend(mentions=9, mentions_24h_ago=None, rank_24h_ago=None)
    embed = build_signal_embed(1, Signal(trend, make_quote(), STAGE_EARLY), today=TODAY)
    assert "**9** mentions (new in 24h)" in embed["description"]
    assert "rank #31" in embed["description"]


def test_build_signal_embed_with_all_context():
    sec = SecActivity("ACME", 12345, [make_form4(value=410_000)], 0, [])
    s = signal(
        trajectory=Trajectory(2, [12, 41, 58]),
        sec=sec,
        earnings=EarningsEvent("ACME", date(2026, 9, 25), "after close"),
    )
    embed = build_signal_embed(1, s, today=TODAY)
    description = embed["description"]
    assert description.startswith("🟢 **Early** (price quiet over the last 5d)")
    assert "🔥 3rd day on the list, still heating up · mentions by day 12 → 41 → 58" in description
    assert "🏛 **CFO Jane Doe bought $410k**" in description
    assert "📅 Earnings in 2d (Fri Sep 25, after close)" in description
    assert "fields" not in embed


def test_format_trajectory_line_variants():
    assert format_trajectory_line(Trajectory(0, [9])) == "🆕 First time on the radar"
    assert format_trajectory_line(Trajectory(0, [5, 9])) == "👀 New to the list, building for days · mentions 5 → 9"
    assert format_trajectory_line(Trajectory(1, [40, 30])) == "🧊 2nd day on the list, cooling off · mentions by day 40 → 30"
    assert format_trajectory_line(Trajectory(10, [1, 2])).startswith("🔥 11th day")


def test_format_earnings_line():
    assert format_earnings_line(EarningsEvent("A", TODAY, "before open"), today=TODAY) == (
        "📅 Earnings today (Wed Sep 23, before open)"
    )
    assert format_earnings_line(EarningsEvent("A", date(2026, 9, 24), None), today=TODAY) == (
        "📅 Earnings tomorrow (Thu Sep 24)"
    )


UPTREND = replace(make_quote(price=5.0), sma50=4.5, sma200=4.0)
INSIDERS = SecActivity("ACME", 1, [make_form4()], 0, [])


def confirmed(ticker: str = "ACME", **kwargs) -> Signal:
    return replace(signal(ticker, sec=INSIDERS, **kwargs), quote=replace(UPTREND, ticker=ticker))


def test_alert_reasons_needs_all_four():
    assert alert_reasons(confirmed()) == ["volume 4.8× normal", "insiders buying", "uptrend"]
    # Each gate on its own blocks the alert.
    assert alert_reasons(confirmed(stage=STAGE_MOVING)) == []
    assert alert_reasons(replace(confirmed(), quote=replace(UPTREND, rel_volume=1.1))) == []
    assert alert_reasons(replace(confirmed(), sec=None)) == []
    assert alert_reasons(replace(confirmed(), sec=SecActivity("ACME", 1, [], 0, []))) == []
    sideways = replace(UPTREND, sma200=6.0)  # above the 50-day, below the 200-day
    assert alert_reasons(replace(confirmed(), quote=sideways)) == []
    assert alert_reasons(signal(sec=INSIDERS)) == []  # no 50-day average known


def test_build_alert_payload():
    payload = build_alert_payload([confirmed("A"), confirmed("B")], when=datetime(2026, 9, 23, 12, 37, tzinfo=UTC))
    assert payload["content"].startswith("🔔 **Reddit alert** · Wed Sep 23 — 2 picks worth a look")
    assert [e["title"] for e in payload["embeds"]] == ["1. A — Acme Corp", "2. B — Acme Corp"]
    assert payload["embeds"][0]["description"].startswith(
        "✅ **Confirmed by:** volume 4.8× normal, insiders buying, uptrend\n🟢 **Early**"
    )


def test_build_alert_payload_stays_under_discord_total_limit():
    signals = [confirmed(f"T{i}") for i in range(12)]
    payload = build_alert_payload(signals)
    assert sum(embed_size(e) for e in payload["embeds"]) <= EMBED_TOTAL_LIMIT
    assert len(payload["embeds"]) == 10


def test_build_recap_payload():
    first_seen = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)
    weeks = [
        WeekSummary("ORCL", 4, first_seen, 140.0, True, 30, 40),
        WeekSummary("BB", 1, first_seen, 4.0, False, 53, 48),
    ]
    quotes = {"ORCL": make_quote("ORCL", price=147.0)}
    signals = [signal("META", stage=STAGE_MOVING)]
    scorecard = {"title": "📊 Scorecard"}
    payload = build_recap_payload(
        weeks, signals, quotes, when=datetime(2026, 9, 27, 21, 13, tzinfo=UTC), scorecard=scorecard
    )
    assert payload["content"] == "🗓 **Reddit weekly recap** · week ending Sun Sep 27 — 2 tickers tracked, 1 alert sent"
    week, outlook, card = payload["embeds"]
    assert week["description"].split("\n") == [
        "🔔 **ORCL** 4 days · mentions 30 → 40 ↗️ · price +5.0% since Mon",
        "• **BB** 1 day · mentions 53 → 48 ➡️",
    ]
    assert outlook["description"] == "1. **META** · 🟡 Moving"
    assert card == scorecard


def test_build_recap_payload_quiet_week():
    payload = build_recap_payload([], [], {}, when=datetime(2026, 9, 27, tzinfo=UTC), scorecard=None)
    assert payload["embeds"] == []


def test_build_recap_payload_hides_unused_legend():
    weeks = [WeekSummary("BB", 1, datetime(2026, 9, 21, tzinfo=UTC), 4.0, False, 53, 48)]
    payload = build_recap_payload(weeks, [signal()], {}, when=datetime(2026, 9, 27, tzinfo=UTC), scorecard=None)
    assert payload["embeds"][0]["footer"]["text"] == "days = days in the daily top picks"


def test_build_scorecard_embed():
    stats = {
        STAGE_EARLY: StageStats(count=4, avg_return=6.2, avg_excess=4.1, win_rate=75.0),
        STAGE_LATE: StageStats(count=2, avg_return=-8.0, avg_excess=None, win_rate=0.0),
    }
    ts = datetime(2026, 9, 14, tzinfo=UTC)
    scored = [ScoredAlert("WIN", STAGE_EARLY, ts, 31.0, 29.0), ScoredAlert("LOSS", STAGE_LATE, ts, -12.0, -14.0)]
    posted = StageStats(count=1, avg_return=12.0, avg_excess=10.0, win_rate=100.0)
    embed = build_scorecard_embed(stats, scored, window_days=14, posted=posted)
    description = embed["description"]
    assert embed["title"] == "📊 Scorecard — picks from the last 14 days"
    assert description.startswith("🔔 **Alerts sent**: 1 picks · avg +12.0% · +10.0% vs SPY · 100% winners")
    assert "🟢 **Early**: 4 picks · avg +6.2% · +4.1% vs SPY · 75% winners" in description
    assert "🔴 **Late / chasing**: 2 picks · avg -8.0% · 0% winners" in description
    assert description.index("Early") < description.index("Late")
    assert "Best: **WIN** +31.0% (Sep 14)" in description
    assert "Worst: **LOSS** -12.0% (Sep 14)" in description


def test_already_ran_today_uses_new_york_day():
    now = datetime(2026, 9, 23, 15, 0, tzinfo=UTC)  # 11:00 ET
    assert already_ran_today(datetime(2026, 9, 23, 12, 30, tzinfo=UTC), now)
    # 00:30 UTC on the 23rd is still the evening of the 22nd in New York.
    assert not already_ran_today(datetime(2026, 9, 23, 0, 30, tzinfo=UTC), now)
    assert not already_ran_today(None, now)
