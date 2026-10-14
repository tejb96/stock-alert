"""Tests for sec_check.py — run from repo root: python -m pytest scripts"""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime, timedelta

import httpx

from factories import make_quote
from sec import ISSUER_BDC, ISSUER_FUND, FeedEntry, SecActivity
from sec_check import (
    FIRST_RUN_LOOKBACK,
    MAX_LOOKBACK,
    InsiderAlert,
    build_8k_embed,
    build_insider_embed,
    feed_window_start,
    filing_lag_days,
    is_listed_ticker,
    latest_reddit,
    new_entries,
    price_mismatch,
    qualifying_buys,
    scan_feed,
    skip_reason,
    watched_tickers,
)
from state import iso
from test_sec import make_form4

NOW = datetime(2026, 9, 23, 18, 13, tzinfo=UTC)


def entry(accession: str, *, minutes_ago: int = 5, role: str = "Issuer", form: str = "4", items=()) -> FeedEntry:
    return FeedEntry(form, 12345, "ACME CORP", role, accession, NOW - timedelta(minutes=minutes_ago), list(items))


def test_feed_window_start():
    assert feed_window_start(None, NOW) == NOW - FIRST_RUN_LOOKBACK
    assert feed_window_start("garbage", NOW) == NOW - FIRST_RUN_LOOKBACK
    assert feed_window_start(iso(NOW - timedelta(minutes=20)), NOW) == NOW - timedelta(minutes=30)
    # Backfill across a weekend and skipped runs, but not without limit.
    assert feed_window_start(iso(NOW - timedelta(days=3)), NOW) == NOW - timedelta(days=3, minutes=10)
    assert feed_window_start(iso(NOW - timedelta(days=9)), NOW) == NOW - MAX_LOOKBACK


def test_new_entries_filters_role_window_seen_and_dupes():
    entries = [
        entry("a"),
        entry("a", role="Reporting"),
        entry("a"),
        entry("b", role="Reporting"),
        entry("c", minutes_ago=90),
        entry("d"),
        entry("e", form="4/A"),
    ]
    picked = new_entries(entries, since=NOW - timedelta(minutes=30), seen={"d"}, role="Issuer", form="4")
    assert [e.accession for e in picked] == ["a"]


def test_qualifying_buys():
    filings = [
        make_form4("Big Officer", 250_000),
        make_form4("Small Officer", 10_000),
        make_form4("Fund LP", 5_000_000, is_officer=False, is_director=False, is_ten_percent_owner=True),
        make_form4("Director", 60_000, is_officer=False, is_director=True),
    ]
    assert [f.owner for f in qualifying_buys(filings, min_value=50_000)] == ["Big Officer", "Director"]


def test_qualifying_buys_skips_placeholder_tickers():
    assert qualifying_buys([make_form4(ticker="NONE")], min_value=0) == []
    assert is_listed_ticker("BRK.B")
    assert not is_listed_ticker("N/A")
    assert not is_listed_ticker("TOOLONG")


def check(value: float, activity=None, quote=None, *, buy_price: float = 4.0, last_buy_date: str | None = None):
    filing = make_form4(value=value, buy_shares=value / buy_price, last_buy_date=last_buy_date)
    alert = InsiderAlert("ACME", "ACME CORP", [filing], activity, quote, None)
    return skip_reason(alert, today=date(2026, 9, 23), alert_value=100_000, min_price=1.0, max_trade_age_days=30)


def test_skip_reason():
    cluster = SecActivity("ACME", 1, [make_form4("A"), make_form4("B")], 0, [])
    solo = SecActivity("ACME", 1, [make_form4("A")], 0, [])
    assert check(150_000, solo, make_quote()) is None
    assert check(40_000, solo, make_quote()) == "too small"
    assert check(40_000, cluster, make_quote()) is None
    assert check(150_000) is None  # unknown to Yahoo is allowed
    assert check(5e6, cluster, make_quote(change_5d=None)) == "IPO allocation"
    assert check(5e6, cluster, make_quote(price=0.5)) == "penny stock or fund"
    assert check(5e6, cluster, make_quote(instrument_type="MUTUALFUND")) == "penny stock or fund"


def test_skip_reason_funds_bdcs_and_stale_trades():
    fund = SecActivity("PDI", 1, [make_form4("A"), make_form4("B")], 0, [], kind=ISSUER_FUND)
    bdc = SecActivity("SAR", 1, [make_form4("A")], 0, [], kind=ISSUER_BDC)
    assert check(250_000, fund, make_quote()) == "closed-end fund"
    assert check(250_000, bdc, make_quote()) is None
    assert check(250_000, None, make_quote(), last_buy_date="2026-08-11") == "trade 43d old"
    assert check(250_000, None, make_quote(), last_buy_date="2026-09-01") is None


def test_skip_reason_price_from_another_listing():
    # Bradesco directors buy São Paulo shares at R$17 while the US ADR trades near $4 (52w $2–$9.50).
    assert check(32e6, None, make_quote(price=4.32), buy_price=17.37) == "price in another currency or share class"
    # Preferred-style $25 par purchase on a stock trading far above it.
    assert price_mismatch(25.0, make_quote(price=4.12)) is True
    assert price_mismatch(4.0, make_quote()) is False
    assert price_mismatch(1.7, make_quote()) is False  # 15% under the 52w low: a real dip buy
    assert price_mismatch(4.0, None) is False


def test_filing_lag_days():
    assert filing_lag_days(make_form4(filed="2026-09-23", last_buy_date="2026-08-31")) == 23
    assert filing_lag_days(make_form4(last_buy_date=None)) is None


def test_watched_tickers_merges_posted_reddit_alerts_and_insider_history():
    digest_state = {
        "alerts": [
            {"ts": iso(NOW - timedelta(days=2)), "ticker": "ACME", "alerted": True},
            {"ts": iso(NOW - timedelta(days=2)), "ticker": "JPM", "alerted": False},  # tracked, never posted
            {"ts": iso(NOW - timedelta(days=10)), "ticker": "OLD", "alerted": True},
        ]
    }
    sec_state = {"insider_tickers": {"BUYR": iso(NOW - timedelta(days=20)), "ACME": iso(NOW - timedelta(days=1))}}
    watched = watched_tickers(digest_state, sec_state, now=NOW)
    assert watched == {"ACME": "Reddit alert Sep 21", "BUYR": "insider buying alert Sep 3"}


def test_latest_reddit():
    assert latest_reddit({}) == {}
    state = {"snapshots": [{"rows": {"A": [1, 99]}}, {"rows": {"ACME": [58, 31]}}]}
    assert latest_reddit(state) == {"ACME": (58, 31)}


def test_build_insider_embed_cluster_quote_and_reddit():
    filings = [
        make_form4("Jane Doe", 400_000, buy_shares=100_000, last_buy_date="2026-09-21"),
        make_form4("John Roe", 100_000, role="Director", buy_shares=25_000, planned=True),
    ]
    activity = SecActivity("ACME", 12345, filings + [make_form4("Third", 700_000)], 0, [])
    alert = InsiderAlert("ACME", "ACME CORP", filings, activity, make_quote(price=4.40), (58, 31))
    embed = build_insider_embed(alert, today=date(2026, 9, 23))

    description = embed["description"]
    assert embed["title"] == "🏛 Insider buying — ACME (ACME CORP)"
    assert embed["url"].endswith("/12345/000000000026000001/0000000000-26-000001-index.htm")
    lines = description.split("\n")
    assert lines[0] == "**Jane Doe** (CFO) bought **$400k** · 100,000 sh @ $4.00 · 2026-09-21"
    assert lines[1].endswith("· 10b5-1 plan")
    assert "filed" not in lines[0]
    assert "👥 **Cluster:** 3 insiders bought $1.2M in the last 30 days" in description
    assert "↔️ Now +10.0% vs insider price" in description
    assert "📈 Also on Reddit: 58 mentions (rank #31)" in description
    assert embed["footer"]["text"] == "Form 4 · open-market purchase · filed 2026-09-21 (2d ago)"


def test_build_insider_embed_flags_late_filing():
    late = make_form4(filed="2026-09-23", last_buy_date="2026-08-31")
    alert = InsiderAlert("ACME", "ACME CORP", [late], None, None, None)
    assert "⚠️ filed 23d after trade" in build_insider_embed(alert, today=date(2026, 9, 23))["description"]


def test_build_insider_embed_minimal():
    alert = InsiderAlert("ACME", "ACME CORP", [make_form4()], None, None, None)
    description = build_insider_embed(alert, today=date(2026, 9, 23))["description"]
    assert "Cluster" not in description
    assert "Reddit" not in description
    assert "💵" not in description


def test_build_8k_embed():
    e = entry("0000012345-26-000044", form="8-K", role="Filer", items=["1.01", "9.01"])
    embed = build_8k_embed("ACME", [e], "Reddit alert Sep 21", make_quote())
    assert embed["title"] == "📄 8-K — ACME (ACME CORP)"
    assert "**Material agreement (1.01)**" in embed["description"]
    assert "💵 **$4.12**" in embed["description"]
    assert "Watching because: Reddit alert Sep 21" in embed["description"]


def test_build_8k_embed_groups_a_tickers_filings():
    earnings = entry("0000012345-26-000045", form="8-K", role="Filer", items=["2.02", "9.01"], minutes_ago=60)
    deal = entry("0000012345-26-000046", form="8-K", role="Filer", items=["1.01"], minutes_ago=5)
    embed = build_8k_embed("ACME", [earnings, deal], "Reddit alert Sep 21", None)
    assert embed["title"] == "📄 2 8-Ks — ACME (ACME CORP)"
    lines = embed["description"].split("\n")
    assert lines[0].startswith("• [Material agreement (1.01)]")
    assert lines[1].startswith("• [Earnings results (2.02)]")
    assert embed["url"].endswith("0000012345-26-000046-index.htm")


class StubFeed:
    """current_feed() pages of 100 entries, a minute apart, until EDGAR's depth limit (a 503)."""

    def __init__(self, pages: int):
        self.pages = pages
        self.calls = 0

    async def current_feed(self, form: str, *, start: int, count: int):
        self.calls += 1
        if start // count >= self.pages:
            request = httpx.Request("GET", "https://www.sec.gov/cgi-bin/browse-edgar")
            raise httpx.HTTPStatusError("503", request=request, response=httpx.Response(503, request=request))
        return [entry(f"a{start + i}", minutes_ago=start + i) for i in range(count)]


def test_scan_feed_stops_at_window_or_feed_depth(capsys):
    stub = StubFeed(pages=3)
    entries = asyncio.run(scan_feed(stub, "4", since=NOW - timedelta(minutes=150)))
    assert len(entries) == 200 and stub.calls == 2  # second page reaches the window start
    assert "::warning::" not in capsys.readouterr().out

    entries = asyncio.run(scan_feed(StubFeed(pages=3), "4", since=NOW - timedelta(days=1)))
    assert len(entries) == 300  # EDGAR's 503 past its depth ends the scan with what was read
    assert "::warning::sec_check: Form 4 feed ends at" in capsys.readouterr().out
