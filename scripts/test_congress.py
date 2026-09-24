"""Tests for congress.py and congress_check.py — run from repo root: python -m pytest scripts"""

from __future__ import annotations

from datetime import UTC, date, datetime

from congress import (
    KIND_BUY,
    KIND_EXCHANGE,
    KIND_SELL,
    CongressTrade,
    PtrFiling,
    close_on,
    merge_fills,
    parse_amount,
    parse_house_index,
    parse_ptr_text,
)
from congress_check import (
    FilingAlert,
    build_filing_embed,
    build_payload,
    format_trade_line,
    is_watched,
    pending_filings,
    pick_trades,
    prune_seen,
)
from congress_signals import MemberProfile, ScoredTrade, Signal

# Text layers of real e-filed PTRs (pypdf output, trimmed). Labels come out with NULs in them.
PELOSI_PTR = (
    "Name: Hon. Nancy Pelosi\nStatus: Member\nState/District: CA11\n"
    "ID Owner Asset Transaction\nType\nDate Notification\nDate\nAmount Cap.\nGains >\n$200?\n"
    "SP Bloom Energy Corporation Class A\nCommon Stock (BE) [ST]\n"
    "P 07/24/2026 07/24/2026 $1,000,001 -\n$5,000,000\n"
    "F\x00\x00 S\x00\x00: New\nD\x00\x00: Purchased 10,000 shares.\n"
    "SP Bloom Energy Corporation Class A\nCommon Stock (BE) [OP]\n"
    "P 07/24/2026 07/24/2026 $1,000,001 -\n$5,000,000\n"
    "F S: New\nD: Purchased 100 call options with a strike price of $100 and an expiration date of 6/17/27.\n"
    "Filing ID #20035143"
)
MIXED_PTR = (
    "Gains >\n$200?\n"
    "Howmet Aerospace Inc. Common\nStock (HWM) [ST]\n"
    "S (partial) 08/11/2026 09/01/2026 $1,001 - $15,000\nF S: New\nS O: Moran Wealth IRA\n"
    "JT Microsoft Corporation - Common\nStock (MSFT) [OP]\n"
    "S 08/14/2026 09/14/2026 $250,001 -\n$500,000\nF S: New\nS O: Morgan Stanley\n"
    "D: Call options; Strike price $340; Expires 12/18/2026\n"
    "DC Charter Communications, Inc. - Class\nA Common Stock (CHTR) [ST]\n"
    "E 08/20/2026 09/11/2026 $1,001 - $15,000\nF S: New\nD: Shares received thru merger\n"
    "SP Energy Northwest WA PWR UTIL\n[GS]\nS 08/13/2026 08/31/2026 $500,001 -\n$1,000,000\nF S: New\n"
    "SP Irvine Ranch CA WRSR UTIL [GS] P 08/20/2026 08/31/2026 $100,001 -\n$250,000\nF S: New\n"
    "2000140445 Alphabet Inc. - Class A Common\nStock (GOOGL) [ST]\n"
    "S 06/03/2025 06/04/2025 $250,001 -\n$500,000\nF S: Amended\n"
    "Moog Inc. Class A (MOG.A) [ST]\nP 08/07/2026 09/10/2026 Over $50,000,000\nF S: New\n"
)

INDEX_XML = b"""<?xml version="1.0" encoding="utf-8"?>
<FinancialDisclosure>
  <Member><Prefix>Hon.</Prefix><Last>Pelosi</Last><First>Nancy</First><FilingType>P</FilingType>
    <StateDst>CA11</StateDst><Year>2026</Year><FilingDate>8/21/2026</FilingDate><DocID>20035143</DocID></Member>
  <Member><Last>Aaron</Last><First>Richard</First><FilingType>W</FilingType>
    <StateDst>MI04</StateDst><FilingDate>4/15/2026</FilingDate><DocID>8068</DocID></Member>
  <Member><Last>Paper</Last><First>Pat</First><FilingType>P</FilingType>
    <StateDst>TX01</StateDst><FilingDate>9/1/2026</FilingDate><DocID>8221322</DocID></Member>
  <Member><Last>Broken</Last><First>Bo</First><FilingType>P</FilingType><FilingDate>soon</FilingDate><DocID>1</DocID></Member>
</FinancialDisclosure>"""


def trade(ticker="ACME", kind=KIND_BUY, low=15_001, high=50_000, **kwargs) -> CongressTrade:
    defaults = {
        "asset_type": "ST", "partial": False, "traded": date(2026, 8, 3), "notified": date(2026, 8, 3), "owner": "self"
    }
    return CongressTrade(ticker=ticker, kind=kind, amount_low=low, amount_high=high, **{**defaults, **kwargs})


def filing(member="Nancy Pelosi", doc_id="20035143", filed=date(2026, 8, 21)) -> PtrFiling:
    return PtrFiling(doc_id, member, "CA11", filed)


def test_parse_house_index_keeps_ptrs_only():
    filings = parse_house_index(INDEX_XML)
    assert filings == [
        PtrFiling("20035143", "Nancy Pelosi", "CA11", date(2026, 8, 21)),
        PtrFiling("8221322", "Pat Paper", "TX01", date(2026, 9, 1)),
    ]
    assert filings[0].url == "https://disclosures-clerk.house.gov/public_disc/ptr-pdfs/2026/20035143.pdf"
    assert filings[0].electronic and not filings[1].electronic
    assert parse_house_index(b"<not xml") == []


def test_parse_ptr_text_pelosi():
    trades = parse_ptr_text(PELOSI_PTR)
    assert [(t.ticker, t.asset_type, t.kind, t.owner, t.amount_low, t.amount_high) for t in trades] == [
        ("BE", "ST", KIND_BUY, "spouse", 1_000_001, 5_000_000),
        ("BE", "OP", KIND_BUY, "spouse", 1_000_001, 5_000_000),
    ]
    assert trades[0].traded == date(2026, 7, 24)
    assert trades[0].description == "Purchased 10,000 shares."
    assert trades[1].option_side == "calls"


def test_parse_ptr_text_variants():
    trades = parse_ptr_text(MIXED_PTR)
    by_ticker = {t.ticker: t for t in trades}
    # Bonds ([GS]) have no ticker and are skipped.
    assert list(by_ticker) == ["HWM", "MSFT", "CHTR", "GOOGL", "MOG.A"]
    assert by_ticker["HWM"].kind == KIND_SELL and by_ticker["HWM"].partial and by_ticker["HWM"].owner == "self"
    assert by_ticker["MSFT"].owner == "joint" and by_ticker["MSFT"].description.startswith("Call options")
    assert by_ticker["CHTR"].kind == KIND_EXCHANGE and by_ticker["CHTR"].owner == "child"
    assert by_ticker["GOOGL"].amended and by_ticker["GOOGL"].owner == "self"
    assert (by_ticker["MOG.A"].amount_low, by_ticker["MOG.A"].amount_high) == (50_000_001, None)
    assert by_ticker["MOG.A"].yahoo_symbol == "MOG-A"


def test_parse_amount():
    assert parse_amount("$15,001 - $50,000") == (15_001, 50_000)
    assert parse_amount("$1,000,001 -\n$5,000,000") == (1_000_001, 5_000_000)
    assert parse_amount("Over $50,000,000") == (50_000_001, None)


def scored(t: CongressTrade, score: int, *labels: str) -> ScoredTrade:
    return ScoredTrade(t, score, [Signal(1, "purchase"), *(Signal(2, label) for label in labels)])


def test_pick_trades_threshold_and_watchlist():
    items = [
        scored(trade("HIGH"), 7),
        scored(trade("LOW"), 3),
        scored(trade("SELL", kind=KIND_SELL, low=250_001, high=500_000), 0),
        scored(trade("SWAP", kind=KIND_EXCHANGE), 0),
        scored(trade("FIX", amended=True), 9),
    ]
    assert [t.trade.ticker for t in pick_trades(items, watched=False, min_score=6)] == ["HIGH"]
    watched = pick_trades(items, watched=True, min_score=6)
    assert [t.trade.ticker for t in watched] == ["HIGH", "LOW", "SELL"]


def test_merge_fills_combines_repeat_stock_trades():
    trades = [
        trade("ESE", low=1_001, high=15_000, traded=date(2026, 8, 24)),
        trade("ESE", low=1_001, high=15_000, traded=date(2026, 8, 19)),
        trade("ESE", kind=KIND_SELL, low=15_001),
        trade("MSFT", asset_type="OP", description="Call options; Strike price $330"),
        trade("MSFT", asset_type="OP", description="Call options; Strike price $340"),
    ]
    merged = merge_fills(trades)
    assert [(t.ticker, t.kind, t.fills) for t in merged] == [
        ("ESE", KIND_BUY, 2), ("ESE", KIND_SELL, 1), ("MSFT", KIND_BUY, 1), ("MSFT", KIND_BUY, 1)
    ]
    assert (merged[0].amount_low, merged[0].amount_high, merged[0].traded) == (2_002, 30_000, date(2026, 8, 19))


def test_is_watched():
    assert is_watched("Nancy Pelosi", ["pelosi"])
    assert not is_watched("Josh Gottheimer", ["pelosi"])
    assert not is_watched("Nancy Pelosi", [])


def test_pending_filings_and_prune():
    filings = [
        filing(doc_id="21", filed=date(2026, 9, 20)),
        filing(doc_id="22", filed=date(2026, 9, 1)),
        filing(doc_id="23"),
        filing(doc_id="8221322", filed=date(2026, 9, 20)),  # paper scan
    ]
    fresh = pending_filings(filings, {"23": "2026-08-21"}, today=date(2026, 9, 24), days=10)
    assert [f.doc_id for f in fresh] == ["21"]
    assert prune_seen({"old": "2026-07-01", "new": "2026-09-20"}, today=date(2026, 9, 24)) == {"new": "2026-09-20"}


def test_format_trade_line():
    option = trade("BE", asset_type="OP", owner="spouse", low=1_000_001, high=5_000_000,
                   description="Purchased 100 call options", traded=date(2026, 7, 24))
    assert format_trade_line(scored(option, 9, "call options"), since_trade=44.2) == (
        "` 9` 🟢 BUY **BE calls** $1.0M–$5.0M · Jul 24 · spouse · stock +44.2% since\n"
        "  ↳ call options\n  ↳ Purchased 100 call options"
    )
    sale = trade("HWM", kind=KIND_SELL, partial=True, low=50_000_001, high=None)
    assert format_trade_line(ScoredTrade(sale, 0, []), since_trade=None) == "🔴 SELL (part) **HWM** over $50M · Aug 3"
    fills = trade("ESE", fills=3, traded=date(2026, 8, 19))
    stale = ScoredTrade(fills, 0, [Signal(1, "purchase"), Signal(-1, "stale: filed 40d after")])
    assert format_trade_line(stale, since_trade=None) == (
        "🟢 BUY **ESE** $15k–$50k · 3 trades from Aug 19\n  ↳ stale: filed 40d after"
    )


def test_close_on_uses_next_session():
    history = [(date(2026, 7, 24), 100.0), (date(2026, 7, 27), 110.0)]
    assert close_on(history, date(2026, 7, 25)) == 110.0
    assert close_on(history, date(2026, 8, 1)) is None


def test_build_filing_embed_and_payload():
    profile = MemberProfile("Hakeem Jeffries", "Democrat", "House Minority Leader")
    items = [scored(trade("INTC", low=500_001, high=1_000_000), 8), scored(trade("BE", traded=date(2026, 7, 24)), 6)]
    alert = FilingAlert(filing("Hakeem Jeffries"), items, profile, False)
    embed = build_filing_embed(alert, {("BE", date(2026, 7, 24)): 44.2})
    assert embed["title"] == "🏛 Rep. Hakeem Jeffries (D-CA11) · House Minority Leader"
    assert embed["color"] == 0x2ECC71
    assert embed["description"].startswith("` 8` 🟢 BUY **INTC** $500k–$1.0M")
    assert "stock +44.2% since" in embed["description"]
    assert embed["footer"]["text"] == "Filed Aug 21 · 28 days after the earliest trade"

    other = FilingAlert(filing("Josh Gottheimer", "2"), [scored(trade("MSFT"), 9)], None, False)
    payload = build_payload([alert, other], {}, when=datetime(2026, 9, 24, 22, 41, tzinfo=UTC))
    assert payload["content"].startswith("**Congress trades** · Thu Sep 24 — 3 trades worth a look in 2 new House filings")
    assert payload["embeds"][0]["title"] == "🏛 Rep. Josh Gottheimer (CA11)"  # highest score first
