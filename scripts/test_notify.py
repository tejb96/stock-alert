"""Tests for scripts/notify.py — run from repo root: python -m pytest scripts/test_notify.py"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from notify import (  # noqa: E402
    RatioQuote,
    StockQuote,
    TrendRow,
    build_message,
    compute_change_24h,
    compute_trend_score,
    enrich_and_rank,
    _extract_stock_quote,
    _parse_apewisdom_row,
)


def test_compute_change_24h_positive():
    assert compute_change_24h(150, 100) == 50.0


def test_compute_change_24h_zero_baseline():
    assert compute_change_24h(100, 0) is None
    assert compute_change_24h(100, None) is None


def test_compute_trend_score_examples():
    assert compute_trend_score(100, 50.0) == 150.0
    assert compute_trend_score(50, 200.0) == 150.0
    assert compute_trend_score(100, None) == 100.0
    assert compute_trend_score(100, -10.0) == 100.0


def test_parse_apewisdom_row():
    row = _parse_apewisdom_row(
        {
            "ticker": "gme",
            "rank": 2,
            "mentions": 120,
            "upvotes": 500,
            "mentions_24h_ago": 65,
        }
    )
    assert row is not None
    assert row.ticker == "GME"
    assert row.rank == 2
    assert row.mentions == 120
    assert row.change_24h == pytest.approx(84.615, rel=1e-3)
    assert row.trend_score == pytest.approx(221.538, rel=1e-3)


def test_enrich_and_rank_top_three():
    rows = [
        TrendRow("LOW", 10, 50, 10, 50.0, 75.0),
        TrendRow("HIGH", 1, 200, 100, 100.0, 400.0),
        TrendRow("MID", 5, 100, 50, 80.0, 180.0),
        TrendRow("SKIP", 20, 5, 1, None, 5.0),
    ]
    top = enrich_and_rank(rows, min_mentions=20, top_n=3)
    assert [r.ticker for r in top] == ["HIGH", "MID", "LOW"]


def test_build_message_includes_all_sections():
    ratio = RatioQuote(
        gold_price=2350.0,
        silver_price=28.5,
        ratio=82.456,
        market_state="REGULAR",
    )
    trends = [
        TrendRow("GME", 2, 120, 500, 85.0, 222.0),
        TrendRow("AMC", 5, 80, 200, 40.0, 112.0),
    ]
    quotes = [
        StockQuote("GME", 25.40, 3.2, 18.0, 30.10),
        StockQuote("AMC", 4.50, -1.5, 3.0, 12.0),
    ]
    when = datetime(2026, 5, 30, 8, 0, tzinfo=UTC)
    message = build_message(ratio, trends, quotes, when=when)

    assert "**Stock alert digest** · 2026-05-30 08:00 UTC" in message
    assert "Ratio **82.46**" in message
    assert "Gold $2,350.00/oz" in message
    assert "**1. GME** · Rank #2" in message
    assert "**Yahoo quotes**" in message
    assert "**GME** · $25.40 (+3.2%)" in message
    assert "52w $18.00–$30.10" in message


def test_build_message_ratio_only():
    ratio = RatioQuote(2000.0, 25.0, 80.0, "CLOSED")
    message = build_message(ratio, [], [], when=datetime(2026, 1, 1, 20, 0, tzinfo=UTC))
    assert "**Gold/Silver ratio**" in message
    assert "**Top trends**" not in message
    assert "**Yahoo quotes**" not in message


def test_extract_stock_quote_from_yahoo_payload():
    payload = {
        "chart": {
            "result": [
                {
                    "meta": {
                        "regularMarketPrice": 25.4,
                        "regularMarketChangePercent": 3.2,
                        "fiftyTwoWeekLow": 18.0,
                        "fiftyTwoWeekHigh": 30.1,
                    }
                }
            ]
        }
    }
    quote = _extract_stock_quote("GME", payload)
    assert quote.ticker == "GME"
    assert quote.price == 25.4
    assert quote.change_pct == 3.2
    assert quote.fifty_two_week_low == 18.0
    assert quote.fifty_two_week_high == 30.1
