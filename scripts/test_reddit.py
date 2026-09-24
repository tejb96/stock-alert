"""Tests for reddit.py — run from repo root: python -m pytest scripts"""

from __future__ import annotations

import pytest

from factories import make_trend
from reddit import compute_change_24h, compute_trend_score, enrich_and_rank, parse_apewisdom_row


def test_compute_change_24h():
    assert compute_change_24h(150, 100) == 50.0
    assert compute_change_24h(100, 0) is None
    assert compute_change_24h(100, None) is None


def test_trend_score_prefers_acceleration_over_size():
    small_spike = compute_trend_score(40, 3, 30, 200)
    mega_cap = compute_trend_score(950, 900, 1, 1)
    assert small_spike > mega_cap


def test_trend_score_prefers_bigger_spike_at_same_growth():
    assert compute_trend_score(400, 40, 10, 10) > compute_trend_score(30, 3, 10, 10)


def test_trend_score_declines_score_zero():
    assert compute_trend_score(50, 100, 40, 20) == 0.0


def test_trend_score_new_ticker_counts_from_zero():
    assert compute_trend_score(10, None, 50, None) == pytest.approx(3.459 * 3.459, rel=1e-3)


def test_trend_score_rank_climb_adds():
    base = compute_trend_score(20, 10, 50, None)
    assert compute_trend_score(20, 10, 50, 200) > base


def test_parse_apewisdom_row():
    row = parse_apewisdom_row(
        {
            "ticker": "gme",
            "name": "GameStop",
            "rank": 2,
            "rank_24h_ago": "8",
            "mentions": 120,
            "upvotes": 500,
            "mentions_24h_ago": 65,
        }
    )
    assert row is not None
    assert row.ticker == "GME"
    assert row.name == "GameStop"
    assert row.rank_24h_ago == 8
    assert row.change_24h == pytest.approx(84.615, rel=1e-3)
    assert row.trend_score == compute_trend_score(120, 65, 2, 8)


def test_parse_apewisdom_row_rejects_missing_fields():
    assert parse_apewisdom_row({"ticker": "X", "rank": 1}) is None
    assert parse_apewisdom_row({"rank": 1, "mentions": 5, "upvotes": 1}) is None


def test_enrich_and_rank_filters_min_mentions_and_flat():
    rows = [
        make_trend("SPIKE", mentions=40, mentions_24h_ago=3),
        make_trend("TINY", mentions=3, mentions_24h_ago=None),
        make_trend("FLAT", mentions=500, mentions_24h_ago=600, rank=1, rank_24h_ago=1),
        make_trend("MID", mentions=30, mentions_24h_ago=15, rank=40, rank_24h_ago=40),
    ]
    top = enrich_and_rank(rows, min_mentions=5, top_n=5)
    assert [r.ticker for r in top] == ["SPIKE", "MID"]
