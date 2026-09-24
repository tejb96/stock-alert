"""Tests for notify.py — run from repo root: python -m pytest scripts"""

from __future__ import annotations

from datetime import UTC, date, datetime

from common import EMBED_FIELD_VALUE_LIMIT, EMBED_TOTAL_LIMIT, embed_size
from factories import make_quote, make_trend
from market import STAGE_EARLY, STAGE_LATE, STAGE_MOVING, EarningsEvent
from notify import (
    Signal,
    build_payload,
    build_research_context,
    build_scorecard_embed,
    build_signal_embed,
    build_signals,
    format_earnings_line,
    format_trajectory_line,
)
from research import NewsHeadline, Research
from sec import SecActivity
from state import ScoredAlert, StageStats, Trajectory
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
    assert embed["title"] == "🟢 1. ACME — Acme Corp · Early"
    assert embed["url"] == "https://finance.yahoo.com/quote/ACME"
    assert embed["color"] == 0x2ECC71
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
    research = Research(
        "ACME",
        [NewsHeadline("Acme wins contract", "https://news.example/a", "Reuters")] * 3,
        catalyst="DoD contract award.",
        catalyst_type="contract/partnership",
        fresh=True,
        priced_in="no",
        risks=["small cap", "earnings soon"],
    )
    sec = SecActivity("ACME", 12345, [make_form4(value=410_000)], 0, [])
    s = signal(
        trajectory=Trajectory(2, [12, 41, 58]),
        sec=sec,
        earnings=EarningsEvent("ACME", date(2026, 9, 25), "after close"),
        research=research,
    )
    embed = build_signal_embed(1, s, today=TODAY)
    description = embed["description"]
    assert "🔥 3rd digest in a row · mentions by run 12 → 41 → 58" in description
    assert "🏛 **CFO Jane Doe bought $410k**" in description
    assert "📅 Earnings in 2d (Fri Sep 25, after close)" in description

    fields = {f["name"]: f["value"] for f in embed["fields"]}
    assert fields["📰 Catalyst"] == "DoD contract award.\n*contract/partnership · fresh news · priced in: no*"
    assert fields["⚠️ Risks"] == "• small cap\n• earnings soon"
    assert fields["Sources"].count("\n") == 1  # two headlines max


def test_research_without_ai_shows_only_sources():
    research = Research("ACME", [NewsHeadline("Headline", "https://x.example", "CNBC")])
    embed = build_signal_embed(1, signal(research=research), today=TODAY)
    assert [f["name"] for f in embed["fields"]] == ["Sources"]


def test_research_field_values_respect_limit():
    research = Research(
        "ACME",
        [NewsHeadline("h" * 200, "https://news.example/" + "p" * 900, "Src")] * 2,
        catalyst="x" * 3000,
        risks=["r" * 600, "r" * 600],
    )
    embed = build_signal_embed(1, signal(research=research), today=TODAY)
    assert all(len(f["value"]) <= EMBED_FIELD_VALUE_LIMIT for f in embed["fields"])


def test_format_trajectory_line_variants():
    assert format_trajectory_line(Trajectory(0, [9])) == "🆕 First time on the radar"
    assert format_trajectory_line(Trajectory(0, [5, 9])) == "👀 Building across runs · mentions 5 → 9"
    assert format_trajectory_line(Trajectory(1, [40, 30])) == "🧊 2nd digest in a row, cooling off · mentions by run 40 → 30"
    assert format_trajectory_line(Trajectory(10, [1, 2])).startswith("🔥 11th digest")


def test_format_earnings_line():
    assert format_earnings_line(EarningsEvent("A", TODAY, "before open"), today=TODAY) == (
        "📅 Earnings today (Wed Sep 23, before open)"
    )
    assert format_earnings_line(EarningsEvent("A", date(2026, 9, 24), None), today=TODAY) == (
        "📅 Earnings tomorrow (Thu Sep 24)"
    )


def test_build_payload_header_and_counts():
    sec = SecActivity("A", 1, [make_form4()], 0, [])
    signals = [signal("A", sec=sec), signal("B"), signal("C", STAGE_LATE)]
    payload = build_payload(signals, when=datetime(2026, 9, 23, 12, 37, tzinfo=UTC))
    assert payload["content"].startswith("**Stock alert** · 2026-09-23 12:37 UTC")
    assert "🟢 2 early · 🔴 1 late / chasing · 🏛 1 with insider buying" in payload["content"]
    assert len(payload["embeds"]) == 3


def test_build_payload_no_signals():
    payload = build_payload([], when=datetime(2026, 9, 23, 12, 37, tzinfo=UTC))
    assert "No accelerating tickers" in payload["content"]
    assert "embeds" not in payload


def test_build_payload_stays_under_discord_total_limit():
    research = Research(
        "X",
        [NewsHeadline("h" * 100, "https://x.example", "S")] * 2,
        catalyst="s" * 1000,
        risks=["r" * 300] * 3,
    )
    signals = [
        Signal(make_trend(f"T{i}"), make_quote(f"T{i}"), STAGE_MOVING, research=research)
        for i in range(10)
    ]
    payload = build_payload(signals)
    assert sum(embed_size(e) for e in payload["embeds"]) <= EMBED_TOTAL_LIMIT
    # All cards survive; research is kept on the earliest cards that still fit.
    assert len(payload["embeds"]) == 10
    with_fields = ["fields" in e for e in payload["embeds"]]
    assert with_fields[0]
    assert not with_fields[-1]
    assert with_fields == sorted(with_fields, reverse=True)


def test_build_research_context():
    s = signal(
        trajectory=Trajectory(1, [20, 58]),
        sec=SecActivity("ACME", 1, [make_form4(value=410_000)], 0, []),
        earnings=EarningsEvent("ACME", date(2026, 9, 25), None),
    )
    context = build_research_context(s, today=TODAY)
    assert "6 → 58 mentions in 24h, rank #31 (was #212)" in context
    assert "relative volume 4.8x, stage Early" in context
    assert "Mentions across recent runs: 20 → 58" in context
    assert "SEC filings: CFO Jane Doe bought $410k" in context
    assert "Earnings in 2d (Fri Sep 25)" in context


def test_build_scorecard_embed():
    stats = {
        STAGE_EARLY: StageStats(count=4, avg_return=6.2, avg_excess=4.1, win_rate=75.0),
        STAGE_LATE: StageStats(count=2, avg_return=-8.0, avg_excess=None, win_rate=0.0),
    }
    ts = datetime(2026, 9, 14, tzinfo=UTC)
    scored = [ScoredAlert("WIN", STAGE_EARLY, ts, 31.0, 29.0), ScoredAlert("LOSS", STAGE_LATE, ts, -12.0, -14.0)]
    embed = build_scorecard_embed(stats, scored, window_days=14)
    description = embed["description"]
    assert embed["title"] == "📊 Scorecard — alerts from the last 14 days"
    assert "🟢 **Early**: 4 picks · avg +6.2% · vs SPY +4.1% · 75% winners" in description
    assert "🔴 **Late / chasing**: 2 picks · avg -8.0% · 0% winners" in description
    assert description.index("Early") < description.index("Late")
    assert "Best: **WIN** +31.0% (Sep 14)" in description
    assert "Worst: **LOSS** -12.0% (Sep 14)" in description
