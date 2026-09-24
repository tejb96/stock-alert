"""Tests for congress_signals.py — run from repo root: python -m pytest scripts"""

from __future__ import annotations

from datetime import date

import pytest

from congress import KIND_BUY, KIND_SELL, CongressTrade, PtrFiling
from congress_signals import (
    DEFENSE,
    FINANCE,
    HEALTH,
    TECH,
    CommitteeSeat,
    MemberProfile,
    build_profiles,
    cluster_members,
    option_expiry,
    record_buys,
    score_trade,
    sector_for_sic,
)

TODAY = date(2026, 9, 24)
FILING = PtrFiling("20035555", "Pat Member", "TX11", date(2026, 8, 7))


def trade(**kwargs) -> CongressTrade:
    defaults = {
        "ticker": "LMT", "asset_type": "ST", "kind": KIND_BUY, "partial": False, "traded": date(2026, 8, 3),
        "notified": date(2026, 8, 3), "amount_low": 15_001, "amount_high": 50_000, "owner": "self",
    }
    return CongressTrade(**{**defaults, **kwargs})


def labels(scored) -> list[str]:
    return [s.label for s in scored.signals]


@pytest.mark.parametrize(
    ("sic", "sector"),
    [(3812, DEFENSE), (3674, TECH), (7372, TECH), (6022, FINANCE), (2834, HEALTH), (5812, None), (None, None)],
)
def test_sector_for_sic(sic, sector):
    assert sector_for_sic(sic) == sector


def test_build_profiles_leadership_committees_and_districts():
    legislators = [
        {
            "id": {"bioguide": "A1"},
            "name": {"official_full": "Steve Scalise"},
            "terms": [{"type": "rep", "state": "LA", "district": 1, "party": "Republican"}],
            "leadership_roles": [
                {"title": "House Majority Whip", "chamber": "house", "start": "2019-01-03", "end": "2023-01-03"},
                {"title": "House Majority Leader", "chamber": "house", "start": "2025-01-03"},
            ],
        },
        {
            "id": {"bioguide": "B2"},
            "name": {"first": "Al", "last": "Large"},
            "terms": [{"type": "rep", "state": "WY", "district": 0, "party": "Republican"}],
        },
        {"id": {"bioguide": "S3"}, "name": {"last": "Senator"}, "terms": [{"type": "sen", "state": "WY"}]},
    ]
    committees = [
        {"type": "house", "thomas_id": "HSAS", "name": "House Committee on Armed Services"},
        {"type": "house", "thomas_id": "HLIG", "name": "House Permanent Select Committee on Intelligence"},
        {"type": "senate", "thomas_id": "SSAS", "name": "Senate Committee on Armed Services"},
    ]
    membership = {
        "HSAS": [{"bioguide": "B2", "title": "Chairman"}],
        "HSAS25": [{"bioguide": "B2", "title": "Chairman"}],  # subcommittee: ignored
        "HLIG": [{"bioguide": "A1"}],
        "SSAS": [{"bioguide": "S3"}],
    }
    profiles = build_profiles(legislators, committees, membership, today=TODAY)
    assert set(profiles) == {"LA01", "WY00"}
    assert profiles["LA01"].leadership == "House Majority Leader"
    assert profiles["LA01"].committees == [CommitteeSeat("HLIG", "Intelligence")]
    assert profiles["WY00"] == MemberProfile("Al Large", "Republican", None, [CommitteeSeat("HSAS", "Armed Services", "Chairman")])
    assert profiles["WY00"].top_role == "Chairman, Armed Services"
    assert profiles["WY00"].party_letter == "R"


def test_option_expiry():
    assert option_expiry("Purchased 100 call options with a strike price of $100 and an expiration date of 6/17/27.") == (
        date(2027, 6, 17)
    )
    assert option_expiry("Call options; Strike price $340; Expires 12/18/2026") == date(2026, 12, 18)
    assert option_expiry("Purchased 10,000 shares.") is None
    assert option_expiry(None) is None


def test_score_committee_chair_buying_their_sector_filed_fast():
    chair = MemberProfile("Pat Member", "Republican", None, [CommitteeSeat("HSAS", "Armed Services", "Chairman")])
    scored = score_trade(trade(amount_low=50_001, amount_high=100_000), FILING, chair, DEFENSE, set())
    assert labels(scored) == [
        "purchase",
        "large position",
        "Chairman of Armed Services, which oversees defense",
        "fresh: filed 4d after",
    ]
    assert scored.score == 1 + 1 + 4 + 2


def test_score_long_dated_calls_cluster_and_leadership():
    leader = MemberProfile("Pat Member", "Democrat", "House Minority Whip", [CommitteeSeat("HSBA", "Financial Services")])
    calls = trade(
        ticker="NVDA", asset_type="OP", amount_low=250_001, amount_high=500_000, traded=date(2026, 7, 1),
        description="Purchased 50 call options with a strike price of $150 and an expiration date of 1/15/27.",
    )
    scored = score_trade(calls, FILING, leader, TECH, {"Pat Member", "Other One", "Other Two"})
    assert labels(scored) == [
        "purchase",
        "large position",
        "call options",
        "long-dated (7 months)",
        "House Minority Whip",
        "cluster: 3 members bought within 30d",
        "stale: filed 37d after",
    ]
    assert scored.score == 1 + 2 + 2 + 1 + 2 + 4 - 1


def test_score_small_unrelated_buy_and_sales():
    member = MemberProfile("Pat Member", "Democrat", None, [CommitteeSeat("HSAG", "Agriculture", "Ranking Member")])
    small = score_trade(trade(amount_low=1_001, amount_high=15_000, traded=date(2026, 7, 20)), FILING, member, TECH, set())
    assert labels(small) == ["purchase", "small (under $15k)", "Ranking Member, Agriculture"]  # filed 18d: neutral
    assert small.score == 1 - 2 + 1
    assert score_trade(trade(kind=KIND_SELL), FILING, member, TECH, set()).score == 0


def test_puts_count_as_a_smaller_bearish_bet():
    puts = trade(asset_type="OP", description="Purchased 10 put options, strike $90, expires 12/18/2026")
    assert "put options (bearish bet)" in labels(score_trade(puts, FILING, None, None, set()))


def test_cluster_members_and_record_buys():
    buys = record_buys([], FILING, [trade(), trade(ticker="XOM", kind=KIND_SELL)], today=TODAY)
    assert buys == [{"ticker": "LMT", "member": "Pat Member", "traded": "2026-08-03"}]
    other = PtrFiling("2", "Other One", "CA01", date(2026, 8, 20))
    buys = record_buys(buys, other, [trade(traded=date(2026, 8, 25)), trade(ticker="RTX")], today=TODAY)
    buys = record_buys(buys, other, [trade(traded=date(2026, 8, 25))], today=TODAY)  # re-read: no duplicate
    assert len(buys) == 3
    assert cluster_members("LMT", date(2026, 8, 10), buys) == {"Pat Member", "Other One"}
    assert cluster_members("LMT", date(2026, 10, 10), buys) == set()
    assert record_buys(buys, other, [], today=date(2027, 1, 1)) == []
