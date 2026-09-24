"""Tests for market.py — run from repo root: python -m pytest scripts"""

from __future__ import annotations

from datetime import date

import pytest

from common import NotifyError
from factories import make_quote, yahoo_payload
from market import (
    STAGE_EARLY,
    STAGE_LATE,
    STAGE_MOVING,
    classify_stage,
    extract_stock_quote,
    format_price_line,
    is_tradeable,
    parse_nasdaq_earnings,
)


def test_extract_stock_quote_changes_and_relvol_outside_session():
    quote = extract_stock_quote("ACME", yahoo_payload(), now=5000)
    assert quote.price == 11.0
    assert quote.change_1d == pytest.approx(10.0)
    assert quote.change_5d == pytest.approx(10.0)
    assert quote.rel_volume == pytest.approx(3.0)
    assert quote.instrument_type == "EQUITY"


def test_extract_stock_quote_projects_intraday_volume():
    # Halfway through the session with 1000 shares traded → pace of 2000 vs 1000 avg.
    quote = extract_stock_quote("ACME", yahoo_payload(volume=1000), now=1500)
    assert quote.rel_volume == pytest.approx(2.0)


def test_extract_stock_quote_skips_missing_bars():
    payload = yahoo_payload(closes=[None, 10.0, 11.0], volumes=[None, 0, 500])
    quote = extract_stock_quote("ACME", payload, now=5000)
    assert quote.change_1d == pytest.approx(10.0)
    assert quote.change_5d is None
    assert quote.rel_volume is None


def test_extract_stock_quote_volatility_excludes_current_session():
    closes = [100.0, 101.0] * 8 + [130.0]
    quote = extract_stock_quote("ACME", yahoo_payload(price=130.0, closes=closes), now=5000)
    assert quote.daily_volatility == pytest.approx(1.0, abs=0.05)
    assert quote.sigma_1d() == pytest.approx(quote.change_1d / quote.daily_volatility)


def test_extract_stock_quote_needs_history_for_volatility():
    quote = extract_stock_quote("ACME", yahoo_payload(), now=5000)
    assert quote.daily_volatility is None


def test_extract_stock_quote_requires_price():
    with pytest.raises(NotifyError):
        extract_stock_quote("ACME", {"chart": {"result": [{"meta": {}}]}})


@pytest.mark.parametrize(
    ("change_1d", "change_5d", "expected"),
    [
        (1.0, 3.0, STAGE_EARLY),
        (6.0, 4.0, STAGE_MOVING),
        (-7.0, -12.0, STAGE_MOVING),
        (2.0, 35.0, STAGE_LATE),
        (18.0, None, STAGE_LATE),
        (None, None, None),
    ],
)
def test_classify_stage_fixed_thresholds(change_1d, change_5d, expected):
    assert classify_stage(make_quote(change_1d=change_1d, change_5d=change_5d)) == expected


@pytest.mark.parametrize(
    ("change_1d", "change_5d", "volatility", "expected"),
    [
        (-4.8, -4.1, 1.2, STAGE_MOVING),  # 4% is a big day for a mega-cap
        (4.0, 3.0, 8.0, STAGE_EARLY),  # same move is noise for a volatile small cap
        (1.0, 20.0, 2.0, STAGE_LATE),  # 5d rally far outside normal range
        (-12.0, 2.0, 3.0, STAGE_MOVING),  # sharp drop is in play, not "late"
        (40.0, None, 30.0, STAGE_LATE),  # absolute cap applies regardless of volatility
    ],
)
def test_classify_stage_relative_to_volatility(change_1d, change_5d, volatility, expected):
    quote = make_quote(change_1d=change_1d, change_5d=change_5d, daily_volatility=volatility)
    assert classify_stage(quote) == expected


def test_is_tradeable():
    assert is_tradeable(make_quote(), min_price=1.0)
    assert not is_tradeable(make_quote(price=0.5), min_price=1.0)
    assert not is_tradeable(make_quote(instrument_type="ETF"), min_price=1.0)
    assert is_tradeable(make_quote(instrument_type=None), min_price=1.0)


def test_format_price_line_shows_move_vs_usual():
    line = format_price_line(make_quote(change_1d=-4.8, daily_volatility=1.2))
    assert "1d -4.8% (4.0× usual)" in line
    assert "RelVol **4.8×**" in line
    assert "52w $2.00 – $9.50" in line


def test_format_price_line_hides_ordinary_move_ratio():
    assert "usual" not in format_price_line(make_quote(change_1d=1.0, daily_volatility=3.0))


def test_parse_nasdaq_earnings():
    payload = {
        "data": {
            "rows": [
                {"symbol": "cost", "time": "time-after-hours"},
                {"symbol": "DRI", "time": "time-pre-market"},
                {"symbol": "XYZ", "time": "time-not-supplied"},
                {"name": "no symbol"},
            ]
        }
    }
    events = parse_nasdaq_earnings(payload, date(2026, 9, 24))
    assert [(e.ticker, e.timing) for e in events] == [
        ("COST", "after close"),
        ("DRI", "before open"),
        ("XYZ", None),
    ]


def test_parse_nasdaq_earnings_empty_day():
    assert parse_nasdaq_earnings({"data": {"rows": None}}, date(2026, 9, 26)) == []
    assert parse_nasdaq_earnings({"data": None}, date(2026, 9, 26)) == []
