"""Tests for sec_check.py — run from repo root: python -m pytest scripts"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from factories import make_quote
from sec import FeedEntry, SecActivity
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
    qualifying_buys,
    should_alert,
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
    # After a weekend, cap the backfill instead of scanning days of filings.
    assert feed_window_start(iso(NOW - timedelta(days=3)), NOW) == NOW - MAX_LOOKBACK


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


def test_should_alert():
    cluster = SecActivity("ACME", 1, [make_form4("A"), make_form4("B")], 0, [])
    solo = SecActivity("ACME", 1, [make_form4("A")], 0, [])
    kw = {"alert_value": 100_000, "min_price": 1.0}
    assert should_alert(150_000, solo, make_quote(), **kw)
    assert not should_alert(40_000, solo, make_quote(), **kw)
    assert should_alert(40_000, cluster, make_quote(), **kw)
    assert should_alert(150_000, None, None, **kw)  # unknown to Yahoo is allowed
    assert not should_alert(5e6, cluster, make_quote(change_5d=None), **kw)  # IPO allocation
    assert not should_alert(5e6, cluster, make_quote(price=0.5), **kw)
    assert not should_alert(5e6, cluster, make_quote(instrument_type="MUTUALFUND"), **kw)


def test_filing_lag_days():
    assert filing_lag_days(make_form4(filed="2026-09-23", last_buy_date="2026-08-31")) == 23
    assert filing_lag_days(make_form4(last_buy_date=None)) is None


def test_watched_tickers_merges_digest_and_insider_history():
    digest_state = {
        "alerts": [
            {"ts": iso(NOW - timedelta(days=2)), "ticker": "ACME"},
            {"ts": iso(NOW - timedelta(days=10)), "ticker": "OLD"},
        ]
    }
    sec_state = {"insider_tickers": {"BUYR": iso(NOW - timedelta(days=20)), "ACME": iso(NOW - timedelta(days=1))}}
    watched = watched_tickers(digest_state, sec_state, now=NOW)
    assert watched == {"ACME": "in the Reddit digest Sep 21", "BUYR": "insider buying alert Sep 3"}


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
    embed = build_8k_embed("ACME", e, "in the Reddit digest Sep 21")
    assert embed["title"] == "📄 8-K — ACME (ACME CORP)"
    assert "**Material agreement (1.01)**" in embed["description"]
    assert "Watching because: in the Reddit digest Sep 21" in embed["description"]
