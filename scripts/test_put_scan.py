"""Tests for put_scan.py — run from repo root: python -m pytest scripts"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime

from options import Chain, History, PutFilter
from put_scan import (
    TickerScan,
    best_picks,
    build_anchor_embed,
    build_payload,
    csv_tickers,
    ladder,
    format_earnings_line,
    record_iv,
    rich_reason,
    scan_due,
    should_repost,
    with_iv_rank,
)
from test_options import TODAY, make_put

NOW = datetime(2026, 9, 24, 14, 47, tzinfo=UTC)


def make_scan(*options, ticker: str = "GME", price: float = 24.0, realized_vol: float = 0.40, earnings=None, sma50=20.0):
    return TickerScan(Chain(ticker, price, 0.55, list(options)), History(realized_vol, sma50), earnings)


def test_csv_tickers():
    assert csv_tickers(" gme, ko ,,SPY") == ["GME", "KO", "SPY"]


def test_record_iv_keeps_one_sample_per_day_and_prunes():
    state = {"iv30": {"GME": {"2025-01-01": 0.9, "2026-09-23": 0.5}, "OLD": {"2024-01-01": 0.3}}}
    scan = make_scan(make_put(21, bid=0.3, ask=0.34, delta=-0.18))
    updated = record_iv(state, [scan], today=TODAY)
    assert updated == {"iv30": {"GME": {"2026-09-23": 0.5, "2026-09-24": 0.55}}}


def test_with_iv_rank_needs_history():
    scan = make_scan()
    assert with_iv_rank(scan, {}).iv_rank is None
    samples = {f"2026-08-{d:02d}": 0.40 + 0.005 * d for d in range(1, 22)}
    ranked = with_iv_rank(scan, {"iv30": {"GME": samples}})
    assert ranked.iv_rank == 100  # 0.55 is above every stored sample


def test_best_picks_requires_edge_and_ranks():
    put = make_put(21.5, bid=0.40, ask=0.44, delta=-0.22, iv=0.5)
    rich = make_scan(put, ticker="AAA", realized_vol=0.30)
    fair = make_scan(put, ticker="BBB", realized_vol=0.42)
    cheap = make_scan(put, ticker="CCC", realized_vol=0.60)
    picks = best_picks([fair, cheap, rich], PutFilter(), today=TODAY, min_edge=1.1, top_n=5)
    assert [p.ticker for p in picks] == ["AAA", "BBB"]
    assert len(best_picks([fair, rich], PutFilter(), today=TODAY, min_edge=1.1, top_n=1)) == 1


def test_ladder_relaxes_cushion_and_spread_for_anchors():
    scan = make_scan(
        make_put(21.0, bid=0.22, ask=0.32, delta=-0.15),  # wide quote, still shown
        make_put(22.0, bid=0.45, ask=0.50, delta=-0.22),
    )
    rows = ladder(scan, PutFilter(), today=TODAY)
    assert [row[0] for row in rows] == ["Conservative", "Balanced", "Aggressive"]
    assert rows[0][4].option.strike == 21.0
    assert rows[1][4].option.strike == 22.0
    assert rows[2][4] is None


def test_anchor_embed_flags_wide_quotes_and_gaps():
    scan = make_scan(make_put(21.0, bid=0.22, ask=0.32, delta=-0.15))
    embed = build_anchor_embed(scan, PutFilter(), today=TODAY)
    assert embed["title"].startswith("⚓ GME")
    values = [f["value"] for f in embed["fields"]]
    assert "Wide quote" in values[0]
    assert values[2] == "Nothing liquid in this range right now"
    assert "earnings date unknown" in embed["description"]


def test_build_payload_with_and_without_picks():
    put = make_put(21.5, bid=0.40, ask=0.44, delta=-0.22)
    scan = make_scan(put, ticker="AAA", sma50=30.0, earnings=date(2026, 11, 3))
    picks = best_picks([scan], PutFilter(), today=TODAY, min_edge=1.1, top_n=5)
    payload = build_payload(picks, [scan], {"AAA": scan}, PutFilter(), when=NOW)
    assert "Put scan" in payload["content"]
    pick = payload["embeds"][0]
    assert pick["title"] == "1. AAA — sell Oct 30 $21.50 put"
    assert "Below its 50-day average" in pick["description"]
    assert "Earnings Nov 3 (40d)" in pick["description"]
    assert payload["embeds"][1]["title"].startswith("⚓ AAA")

    anchors_only = build_payload([], [scan], {"AAA": scan}, PutFilter(), when=NOW, reasons=["AAA: IV rank 80"])
    assert "💰 Premiums are rich — AAA: IV rank 80" in anchors_only["content"]
    assert anchors_only["embeds"][0]["title"].startswith("⚓ AAA")


def test_rich_reason_gates_on_iv_and_yield():
    scan = make_scan(make_put(22.0, bid=0.45, ask=0.50, delta=-0.22))  # IV30 55% vs realized 40%
    gate = {"today": TODAY, "min_iv_rank": 50, "min_iv_edge": 1.3, "min_annualized": 10}
    reason = rich_reason(scan, PutFilter(), **gate)
    assert reason is not None and reason.startswith("GME: IV 55% vs realized 40% (1.4×), up to ")
    assert rich_reason(scan, PutFilter(), **{**gate, "min_iv_edge": 1.5}) is None
    assert rich_reason(scan, PutFilter(), **{**gate, "min_annualized": 500}) is None
    # Once IV rank exists it decides, whatever implied vs realized says.
    assert rich_reason(replace(scan, iv_rank=72.0), PutFilter(), **gate).startswith("GME: IV rank 72, up to ")
    assert rich_reason(replace(scan, iv_rank=30.0), PutFilter(), **gate) is None


def test_fund_skips_earnings_line():
    put = make_put(21.5, bid=0.40, ask=0.44, delta=-0.22)
    scan = make_scan(put, ticker="XLF")
    scan = TickerScan(scan.chain, History(0.3, 20.0, "ETF"), None)
    picks = best_picks([scan], PutFilter(), today=TODAY, min_edge=1.1, top_n=5)
    payload = build_payload(picks, [], {"XLF": scan}, PutFilter(), when=NOW)
    assert "earnings" not in payload["embeds"][0]["description"].lower()


def test_record_iv_keeps_other_state():
    state = {"iv30": {}, "posted": {"GME": {"date": "2026-09-20", "iv30": 0.6}}, "last_scan": "2026-09-23"}
    updated = record_iv(state, [make_scan()], today=TODAY)
    assert updated["posted"] == state["posted"]
    assert updated["last_scan"] == "2026-09-23"


def test_should_repost_waits_out_cooldown_unless_iv_jumps():
    posted = {"GME": {"date": "2026-09-21", "iv30": 0.61}}
    assert should_repost("KO", 0.30, posted, today=TODAY)  # never posted
    assert not should_repost("GME", 0.62, posted, today=TODAY)  # 3 days later, same IV
    assert should_repost("GME", 0.67, posted, today=TODAY)  # IV up 6 vol points
    assert should_repost("GME", 0.62, posted, today=date(2026, 9, 28))  # cooldown over
    assert should_repost("GME", 0.62, {"GME": {"date": "garbage"}}, today=TODAY)


def test_scan_due_once_per_market_day():
    assert scan_due({}, NOW) is None  # 10:47 ET
    assert scan_due({"last_scan": "2026-09-24"}, NOW) == "already scanned today"
    assert scan_due({"last_scan": "2026-09-23"}, NOW) is None
    assert scan_due({}, datetime(2026, 9, 24, 13, 40, tzinfo=UTC)).startswith("market not open yet")  # 9:40 ET
    assert scan_due({}, datetime(2026, 9, 26, 15, 0, tzinfo=UTC)).startswith("market not open yet")  # Saturday


def test_estimated_earnings_skips_a_margin_before_it():
    scan = replace(make_scan(), earnings=date(2026, 12, 8), earnings_estimated=True)
    assert scan.earnings_cutoff == date(2026, 12, 1)
    line = format_earnings_line(scan, today=TODAY)
    assert line.startswith("📅 Earnings expected ~Dec 8 (75d, estimated from past reports)")
    assert make_scan(earnings=date(2026, 12, 8)).earnings_cutoff == date(2026, 12, 8)


def test_build_payload_anchor_only_header_skips_pick_rules():
    scan = make_scan(make_put(21.5, bid=0.40, ask=0.44, delta=-0.22))
    payload = build_payload([], [scan], {"GME": scan}, PutFilter(), when=NOW, reasons=["GME: IV rank 80"])
    assert payload["content"].startswith("**Put scan** · Thu Sep 24 10:47 ET\n💰 Premiums are rich")
    assert "Δ 0.15" not in payload["content"]
