"""Tests for sec.py — run from repo root: python -m pytest scripts"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from common import format_usd
from factories import form4_transaction, form4_xml
from sec import (
    FilingRef,
    Form4,
    SecActivity,
    describe_items,
    estimate_next_earnings,
    extract_ownership_xml,
    format_activity_line,
    is_common_equity,
    is_notable_8k,
    issuer_kind,
    parse_current_feed,
    parse_form4,
    parse_recent_filings,
)

TODAY = date(2026, 9, 23)


def make_form4(owner: str = "Jane Doe", value: float = 100_000, filed: str = "2026-09-21", **kwargs) -> Form4:
    defaults = {
        "accession": "0000000000-26-000001",
        "issuer_cik": 12345,
        "ticker": "ACME",
        "issuer_name": "ACME CORP",
        "owner": owner,
        "role": "CFO",
        "is_officer": True,
        "is_director": False,
        "is_ten_percent_owner": False,
        "planned": False,
        "filed": filed,
        "buy_shares": value / 4,
        "buy_value": value,
    }
    defaults.update(kwargs)
    return Form4(**defaults)


def test_parse_form4_open_market_buy():
    xml = form4_xml(
        form4_transaction("P", 10_000, 4.00, "2026-09-19"),
        form4_transaction("P", 5_000, 4.30, "2026-09-21"),
    )
    filing = parse_form4(xml, accession="acc-1", filed="2026-09-22")
    assert filing is not None
    assert filing.ticker == "ACME"
    assert filing.issuer_cik == 12345
    assert filing.owner == "Doe Jane"
    assert filing.role == "CFO"
    assert filing.is_insider
    assert filing.buy_shares == 15_000
    assert filing.buy_value == pytest.approx(61_500)
    assert filing.buy_avg_price == pytest.approx(4.10)
    assert filing.last_buy_date == "2026-09-21"
    assert filing.sell_value == 0


def test_parse_form4_ignores_grants_exercises_and_unpriced():
    xml = form4_xml(
        form4_transaction("A", 50_000, 0),  # grant at $0
        form4_transaction("M", 30_000, None),  # option exercise, price in footnote
        form4_transaction("S", 1_000, 330.19),
    )
    filing = parse_form4(xml, accession="acc-2", filed="2026-09-22")
    assert filing is not None
    assert filing.buy_value == 0
    assert filing.buy_avg_price is None
    assert filing.sell_value == pytest.approx(330_190)


def test_parse_form4_counts_only_common_stock():
    xml = form4_xml(
        form4_transaction("P", 40_000, 25.00, title="9.25% Series B Cumulative Redeemable Preferred Stock"),
        form4_transaction("P", 1_000, 17.37, title="BRADESCO PN EJ N1 (Preference shares - BBDC4)"),
        form4_transaction("P", 500, 10.00, title="Warrants to purchase Common Stock"),
        form4_transaction("P", 2_000, 4.00, title="Class A Common Stock"),
    )
    filing = parse_form4(xml, accession="acc-1", filed="2026-09-22")
    assert filing is not None
    assert filing.buy_shares == 2_000
    assert filing.buy_value == 8_000


def test_is_common_equity():
    assert is_common_equity("Common Stock")
    assert is_common_equity("American Depositary Shares")
    assert is_common_equity("Common Units representing limited partner interests")
    assert is_common_equity(None)
    assert not is_common_equity("Series A Preferred Stock")
    assert not is_common_equity("6.5% Senior Notes due 2030")


def test_issuer_kind():
    def refs(*forms):
        return [FilingRef(form=f, accession=f"a{i}", filed="2026-09-01") for i, f in enumerate(forms)]

    assert issuer_kind(refs("4", "NPORT-P", "N-CSR")) == "fund"
    assert issuer_kind(refs("10-Q", "N-54A", "4")) == "bdc"
    assert issuer_kind(refs("10-K", "8-K")) is None


def test_estimate_next_earnings():
    def report(day, items=("2.02", "9.01")):
        return FilingRef(form="8-K", accession=day, filed=day, items=list(items))

    filings = [
        report("2026-09-08"),
        report("2026-08-31", ("1.01", "2.02")),  # pre-announcement alongside a deal
        report("2026-06-02"),
        report("2026-03-24"),
        report("2025-12-09"),
        report("2025-09-09"),
        report("2025-11-01", ("8.01",)),
    ]
    # Same quarter a year earlier, 52 weeks on (same weekday).
    assert estimate_next_earnings(filings, today=date(2026, 10, 13)) == date(2026, 12, 8)
    # Only one report on file: about a quarter after it.
    assert estimate_next_earnings([report("2026-09-08")], today=date(2026, 10, 13)) == date(2026, 12, 8)
    assert estimate_next_earnings([report("2025-01-08")], today=date(2026, 10, 13)) is None
    assert estimate_next_earnings([], today=date(2026, 10, 13)) is None


def test_parse_form4_rejects_bad_xml_and_missing_issuer():
    assert parse_form4("<not xml", accession="a", filed="2026-09-22") is None
    assert parse_form4("<ownershipDocument/>", accession="a", filed="2026-09-22") is None


def test_extract_ownership_xml_from_full_submission():
    submission = f"<SEC-DOCUMENT>\n<TEXT>\n<XML>\n{form4_xml()}\n</XML>\n</TEXT>"
    extracted = extract_ownership_xml(submission)
    assert extracted.startswith("<ownershipDocument>")
    assert extracted.endswith("</ownershipDocument>")
    assert extract_ownership_xml("<SEC-DOCUMENT>nothing</SEC-DOCUMENT>") is None


FEED_XML = """<?xml version="1.0" encoding="ISO-8859-1" ?>
<feed xmlns="http://www.w3.org/2005/Atom">
<entry>
<title>8-K - ACME CORP (0000012345) (Filer)</title>
<summary type="html"> &lt;b&gt;Filed:&lt;/b&gt; 2026-09-23 &lt;br&gt;Item 1.01: Entry into a Material Definitive Agreement
&lt;br&gt;Item 9.01: Financial Statements and Exhibits</summary>
<updated>2026-09-23T17:30:45-04:00</updated>
<id>urn:tag:sec.gov,2008:accession-number=0000012345-26-000044</id>
</entry>
<entry>
<title>4 - DOE JANE (0000099999) (Reporting)</title>
<summary type="html"> &lt;b&gt;Filed:&lt;/b&gt; 2026-09-23</summary>
<updated>2026-09-23T21:48:00-04:00</updated>
<id>urn:tag:sec.gov,2008:accession-number=0001437749-26-031099</id>
</entry>
<entry>
<title>garbage</title>
<updated>2026-09-23T21:48:00-04:00</updated>
<id>urn:tag:sec.gov,2008:accession-number=1</id>
</entry>
</feed>"""


def test_parse_current_feed():
    entries = parse_current_feed(FEED_XML)
    assert len(entries) == 2
    eight_k, form4 = entries
    assert eight_k.form == "8-K"
    assert eight_k.cik == 12345
    assert eight_k.name == "ACME CORP"
    assert eight_k.role == "Filer"
    assert eight_k.accession == "0000012345-26-000044"
    assert eight_k.items == ["1.01", "9.01"]
    assert eight_k.updated == datetime(2026, 9, 23, 21, 30, 45, tzinfo=UTC)
    assert form4.role == "Reporting"
    assert form4.items == []


def test_parse_current_feed_bad_xml():
    assert parse_current_feed("<html>blocked</html") == []


def test_parse_recent_filings():
    payload = {
        "filings": {
            "recent": {
                "form": ["4", "8-K"],
                "accessionNumber": ["a-1", "a-2"],
                "filingDate": ["2026-09-17", "2026-09-10"],
                "items": ["", "2.02,9.01"],
                "primaryDocument": ["xslF345X06/form4.xml", "acme-8k.htm"],
            }
        }
    }
    refs = parse_recent_filings(payload)
    assert refs[0] == FilingRef("4", "a-1", "2026-09-17", [], "xslF345X06/form4.xml")
    assert refs[1].items == ["2.02", "9.01"]


def test_eight_k_item_helpers():
    assert is_notable_8k(["1.01", "9.01"])
    assert not is_notable_8k(["9.01"])
    assert not is_notable_8k(["5.07", "9.01"])
    assert describe_items(["1.01", "9.01"]) == "Material agreement (1.01)"
    assert describe_items(["9.99"]) == "Item 9.99 (9.99)"


def test_activity_line_single_buyer():
    activity = SecActivity("ACME", 12345, [make_form4(value=410_000)], sell_value=0, eight_ks=[])
    line = format_activity_line(activity, today=TODAY)
    assert line == "🏛 **CFO Jane Doe bought $410k** (30d, latest 2d ago)"


def test_activity_line_cluster_and_8k():
    buys = [make_form4("A", 200_000, "2026-09-20"), make_form4("B", 1_000_000, "2026-09-23")]
    eight_k = FilingRef("8-K", "a-9", "2026-09-22", ["1.01", "9.01"])
    activity = SecActivity("ACME", 12345, buys, sell_value=50_000, eight_ks=[eight_k])
    line = format_activity_line(activity, today=TODAY)
    assert "**2 insiders bought $1.2M** (30d, latest today)" in line
    assert "8-K 1d ago: Material agreement (1.01)" in line


def test_activity_line_only_sells_or_nothing():
    sells = SecActivity("ACME", 1, [], sell_value=3_200_000, eight_ks=[])
    assert format_activity_line(sells, today=TODAY) == "🏛 Insiders: no buys, sold $3.2M (30d, not pre-scheduled)"
    planned = SecActivity("ACME", 1, [], sell_value=5_300_000, eight_ks=[], planned_sell_value=5_300_000)
    assert format_activity_line(planned, today=TODAY).endswith("sold $5.3M (30d, all pre-scheduled 10b5-1 — routine)")
    partly = SecActivity("ACME", 1, [], sell_value=5_300_000, eight_ks=[], planned_sell_value=4_000_000)
    assert format_activity_line(partly, today=TODAY).endswith("sold $5.3M (30d, $1.3M of it not pre-scheduled)")
    assert format_activity_line(SecActivity("ACME", 1, [], 0, []), today=TODAY) is None


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (950, "$950"),
        (41_000, "$41k"),
        (410_000, "$410k"),
        (999_999, "$1.0M"),
        (1_234_567, "$1.2M"),
        (3.4e9, "$3.4B"),
    ],
)
def test_format_usd(value, expected):
    assert format_usd(value) == expected
