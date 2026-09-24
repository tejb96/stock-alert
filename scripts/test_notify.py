"""Tests for scripts/notify.py — run from repo root: python -m pytest scripts/test_notify.py"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from notify import (  # noqa: E402
    EMBED_FIELD_VALUE_LIMIT,
    EMBED_TOTAL_LIMIT,
    STAGE_EARLY,
    STAGE_LATE,
    STAGE_MOVING,
    NewsHeadline,
    Signal,
    StockQuote,
    TickerResearch,
    TrendRow,
    build_payload,
    build_signal_embed,
    build_signals,
    classify_stage,
    compute_change_24h,
    compute_trend_score,
    enrich_and_rank,
    _build_research_prompt,
    _embed_size,
    _extract_stock_quote,
    _parse_apewisdom_row,
)


def make_trend(
    ticker: str = "ACME",
    *,
    mentions: int = 58,
    mentions_24h_ago: int | None = 6,
    rank: int = 31,
    rank_24h_ago: int | None = 212,
    name: str = "Acme Corp",
) -> TrendRow:
    return TrendRow(
        ticker=ticker,
        name=name,
        rank=rank,
        rank_24h_ago=rank_24h_ago,
        mentions=mentions,
        mentions_24h_ago=mentions_24h_ago,
        upvotes=120,
        change_24h=compute_change_24h(mentions, mentions_24h_ago),
        trend_score=compute_trend_score(mentions, mentions_24h_ago, rank, rank_24h_ago),
    )


def make_quote(
    ticker: str = "ACME",
    *,
    price: float = 4.12,
    change_1d: float | None = 2.1,
    change_5d: float | None = 3.0,
    rel_volume: float | None = 4.8,
    instrument_type: str | None = "EQUITY",
    daily_volatility: float | None = None,
) -> StockQuote:
    return StockQuote(ticker, price, change_1d, change_5d, rel_volume, 2.0, 9.5, instrument_type, daily_volatility)


def yahoo_payload(
    *,
    price: float = 11.0,
    closes: list[float | None] | None = None,
    volumes: list[int | None] | None = None,
    volume: int = 3000,
    regular: tuple[int, int] = (1000, 2000),
    instrument_type: str = "EQUITY",
) -> dict:
    return {
        "chart": {
            "result": [
                {
                    "meta": {
                        "regularMarketPrice": price,
                        "regularMarketVolume": volume,
                        "fiftyTwoWeekLow": 5.0,
                        "fiftyTwoWeekHigh": 20.0,
                        "instrumentType": instrument_type,
                        "currentTradingPeriod": {"regular": {"start": regular[0], "end": regular[1]}},
                    },
                    "indicators": {
                        "quote": [
                            {
                                "close": closes if closes is not None else [10.0] * 7 + [11.0],
                                "volume": volumes if volumes is not None else [1000] * 7 + [3000],
                            }
                        ]
                    },
                }
            ]
        }
    }


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
    row = _parse_apewisdom_row(
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
    assert _parse_apewisdom_row({"ticker": "X", "rank": 1}) is None
    assert _parse_apewisdom_row({"rank": 1, "mentions": 5, "upvotes": 1}) is None


def test_enrich_and_rank_filters_min_mentions_and_flat():
    rows = [
        make_trend("SPIKE", mentions=40, mentions_24h_ago=3),
        make_trend("TINY", mentions=3, mentions_24h_ago=None),
        make_trend("FLAT", mentions=500, mentions_24h_ago=600, rank=1, rank_24h_ago=1),
        make_trend("MID", mentions=30, mentions_24h_ago=15, rank=40, rank_24h_ago=40),
    ]
    top = enrich_and_rank(rows, min_mentions=5, top_n=5)
    assert [r.ticker for r in top] == ["SPIKE", "MID"]


def test_extract_stock_quote_changes_and_relvol_outside_session():
    quote = _extract_stock_quote("ACME", yahoo_payload(), now=5000)
    assert quote.price == 11.0
    assert quote.change_1d == pytest.approx(10.0)
    assert quote.change_5d == pytest.approx(10.0)
    assert quote.rel_volume == pytest.approx(3.0)
    assert quote.instrument_type == "EQUITY"


def test_extract_stock_quote_projects_intraday_volume():
    # Halfway through the session with 1000 shares traded → pace of 2000 vs 1000 avg.
    quote = _extract_stock_quote("ACME", yahoo_payload(volume=1000), now=1500)
    assert quote.rel_volume == pytest.approx(2.0)


def test_extract_stock_quote_skips_missing_bars():
    payload = yahoo_payload(closes=[None, 10.0, 11.0], volumes=[None, 0, 500])
    quote = _extract_stock_quote("ACME", payload, now=5000)
    assert quote.change_1d == pytest.approx(10.0)
    assert quote.change_5d is None
    assert quote.rel_volume is None


def test_extract_stock_quote_volatility_excludes_current_session():
    closes = [100.0, 101.0] * 8 + [130.0]
    quote = _extract_stock_quote("ACME", yahoo_payload(price=130.0, closes=closes), now=5000)
    assert quote.daily_volatility == pytest.approx(1.0, abs=0.05)
    assert quote.sigma_1d() == pytest.approx(quote.change_1d / quote.daily_volatility)


def test_extract_stock_quote_needs_history_for_volatility():
    quote = _extract_stock_quote("ACME", yahoo_payload(), now=5000)
    assert quote.daily_volatility is None


def test_extract_stock_quote_requires_price():
    with pytest.raises(Exception):
        _extract_stock_quote("ACME", {"chart": {"result": [{"meta": {}}]}})


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
def test_classify_stage(change_1d, change_5d, expected):
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


def test_price_line_shows_move_vs_usual():
    quote = make_quote(change_1d=-4.8, daily_volatility=1.2)
    embed = build_signal_embed(1, Signal(make_trend(), quote, STAGE_MOVING))
    assert "1d -4.8% (4.0× usual)" in embed["description"]


def test_build_signals_drops_junk_and_caps():
    candidates = [make_trend(t) for t in ("ETF", "PENNY", "WORD", "GOOD", "ALSO", "EXTRA")]
    quotes = {
        "ETF": make_quote("ETF", instrument_type="ETF"),
        "PENNY": make_quote("PENNY", price=0.4),
        "GOOD": make_quote("GOOD"),
        "ALSO": make_quote("ALSO", change_5d=40.0),
        "EXTRA": make_quote("EXTRA"),
    }
    signals = build_signals(candidates, quotes, min_price=1.0, top_n=2)
    assert [s.trend.ticker for s in signals] == ["GOOD", "ALSO"]
    assert [s.stage for s in signals] == [STAGE_EARLY, STAGE_LATE]


def test_build_signal_embed_content():
    embed = build_signal_embed(1, Signal(make_trend(), make_quote(), STAGE_EARLY))
    assert embed["title"] == "🟢 1. ACME — Acme Corp · Early"
    assert embed["url"] == "https://finance.yahoo.com/quote/ACME"
    assert embed["color"] == 0x2ECC71
    assert "$4.12" in embed["description"]
    assert "RelVol **4.8×**" in embed["description"]
    assert "6 → **58** mentions (+866.7%)" in embed["description"]
    assert "rank #212 → **#31**" in embed["description"]
    assert "fields" not in embed


def test_build_signal_embed_new_ticker_wording():
    trend = make_trend(mentions=9, mentions_24h_ago=None, rank_24h_ago=None)
    embed = build_signal_embed(1, Signal(trend, make_quote(), STAGE_EARLY))
    assert "**9** mentions (new in 24h)" in embed["description"]
    assert "rank #31" in embed["description"]


def test_build_signal_embed_attaches_research_to_matching_ticker():
    research = TickerResearch(
        "ACME",
        "Contract award announced today.",
        [NewsHeadline("Acme wins contract", "https://news.example/a", "Reuters")],
    )
    embed = build_signal_embed(1, Signal(make_trend(), make_quote(), STAGE_EARLY), research)
    names = [f["name"] for f in embed["fields"]]
    assert names == ["📰 Why it's trending", "Sources"]
    assert "[Acme wins contract](https://news.example/a) — Reuters" in embed["fields"][1]["value"]

    other = build_signal_embed(2, Signal(make_trend("OTHER"), make_quote("OTHER"), STAGE_EARLY), research)
    assert "fields" not in other


def test_research_without_ai_summary_shows_only_sources():
    research = TickerResearch("ACME", None, [NewsHeadline("Headline", "https://x.example", "CNBC")])
    embed = build_signal_embed(1, Signal(make_trend(), make_quote(), STAGE_EARLY), research)
    assert [f["name"] for f in embed["fields"]] == ["Sources"]


def test_research_field_values_respect_limit():
    research = TickerResearch(
        "ACME",
        "x" * 3000,
        [NewsHeadline("h" * 200, "https://news.example/" + "p" * 300, "Src") for _ in range(10)],
    )
    embed = build_signal_embed(1, Signal(make_trend(), make_quote(), STAGE_EARLY), research)
    assert all(len(f["value"]) <= EMBED_FIELD_VALUE_LIMIT for f in embed["fields"])


def test_build_payload_header_and_stage_counts():
    signals = [
        Signal(make_trend("A"), make_quote("A"), STAGE_EARLY),
        Signal(make_trend("B"), make_quote("B"), STAGE_EARLY),
        Signal(make_trend("C"), make_quote("C"), STAGE_LATE),
    ]
    payload = build_payload(signals, when=datetime(2026, 9, 23, 12, 37, tzinfo=UTC))
    assert payload["content"].startswith("**Stock alert** · 2026-09-23 12:37 UTC")
    assert "🟢 2 early · 🔴 1 late / chasing" in payload["content"]
    assert len(payload["embeds"]) == 3


def test_build_payload_no_signals():
    payload = build_payload([], when=datetime(2026, 9, 23, 12, 37, tzinfo=UTC))
    assert "No accelerating tickers" in payload["content"]
    assert "embeds" not in payload


def test_build_payload_stays_under_discord_total_limit():
    long_name = "N" * 400
    signals = [
        Signal(make_trend(f"T{i}", name=long_name), make_quote(f"T{i}"), STAGE_MOVING) for i in range(10)
    ]
    research = TickerResearch("T0", "s" * 1024, [NewsHeadline("h" * 100, "https://x.example", "S")] * 3)
    payload = build_payload(signals, research=research)
    assert sum(_embed_size(e) for e in payload["embeds"]) <= EMBED_TOTAL_LIMIT


def test_build_research_prompt_includes_stats():
    signal = Signal(make_trend(), make_quote(), STAGE_EARLY)
    headlines = [NewsHeadline("Retail traders pile in", "https://example.com", "CNBC", "snippet")]
    user_content = _build_research_prompt(signal, headlines)[1]["content"]

    assert "ACME" in user_content
    assert "6 → 58 mentions" in user_content
    assert "was #212" in user_content
    assert "$4.12" in user_content
    assert "relative volume 4.8x" in user_content
    assert "Retail traders pile in (CNBC) — snippet" in user_content
