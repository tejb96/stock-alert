"""Which congressional trades are worth a look: who traded, what, how big, and how fresh.

Signals, each worth points (see score_trade):
- Leadership roles and committee chairs, where studies find the outperformance concentrates
- Committee overlap: buying into a sector the member's committee oversees
- Purchases only (sales happen for taxes, houses, rebalancing), bigger = more conviction
- Call options, especially long-dated ones: leveraged and expiring, so a deliberate bet
- Cluster buys: several different members buying the same stock within a month
- Fast filers: a trade disclosed within days is still actionable; one filed on day 44 mostly isn't

Member roles come from the maintained unitedstates/congress-legislators dataset; a company's
sector comes from its SEC SIC code.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

import httpx

from congress import KIND_BUY, CongressTrade, PtrFiling

LEGISLATORS_URL = "https://unitedstates.github.io/congress-legislators/legislators-current.json"
COMMITTEES_URL = "https://unitedstates.github.io/congress-legislators/committees-current.json"
MEMBERSHIP_URL = "https://unitedstates.github.io/congress-legislators/committee-membership-current.json"

DEFENSE = "defense"
FINANCE = "finance"
ENERGY = "energy"
HEALTH = "health"
TECH = "tech"
TRANSPORT = "transport"
AGRICULTURE = "agriculture"
CONSTRUCTION = "construction"
MINING = "mining"

# SIC code ranges (inclusive) → sector.
SIC_SECTORS: tuple[tuple[int, int, str], ...] = (
    (100, 999, AGRICULTURE),
    (1000, 1099, MINING),
    (1200, 1399, ENERGY),
    (1400, 1499, MINING),
    (1500, 1799, CONSTRUCTION),
    (2000, 2099, AGRICULTURE),
    (2830, 2836, HEALTH),
    (2870, 2879, AGRICULTURE),
    (2910, 2911, ENERGY),
    (3240, 3299, CONSTRUCTION),
    (3480, 3489, DEFENSE),
    (3570, 3579, TECH),
    (3660, 3679, TECH),
    (3710, 3716, TRANSPORT),
    (3720, 3729, DEFENSE),
    (3730, 3732, DEFENSE),
    (3740, 3743, TRANSPORT),
    (3760, 3769, DEFENSE),
    (3812, 3812, DEFENSE),
    (3840, 3851, HEALTH),
    (4000, 4799, TRANSPORT),
    (4800, 4899, TECH),
    (4900, 4991, ENERGY),
    (5122, 5122, HEALTH),
    (6000, 6411, FINANCE),
    (6700, 6799, FINANCE),
    (7370, 7379, TECH),
    (8000, 8099, HEALTH),
)

# House committee → sectors it writes the rules (or budgets) for. Appropriations touches
# everything, so it's left out rather than matching every trade.
COMMITTEE_SECTORS: dict[str, frozenset[str]] = {
    "HSAS": frozenset({DEFENSE}),  # Armed Services
    "HSBA": frozenset({FINANCE}),  # Financial Services
    "HSIF": frozenset({ENERGY, HEALTH, TECH}),  # Energy and Commerce
    "HSAG": frozenset({AGRICULTURE}),
    "HSII": frozenset({ENERGY, MINING}),  # Natural Resources
    "HSPW": frozenset({TRANSPORT, CONSTRUCTION}),  # Transportation and Infrastructure
    "HSSY": frozenset({TECH, DEFENSE}),  # Science, Space, and Technology
    "HSHM": frozenset({DEFENSE, TECH}),  # Homeland Security
    "HLIG": frozenset({DEFENSE, TECH}),  # Intelligence
    "HSZS": frozenset({TECH, DEFENSE}),  # China select committee: chips, export controls
    "HSWM": frozenset({HEALTH, FINANCE}),  # Ways and Means: Medicare, tax
    "HSVR": frozenset({HEALTH}),  # Veterans' Affairs
    "HSFA": frozenset({DEFENSE}),  # Foreign Affairs: arms sales, sanctions
    "HSJU": frozenset({TECH}),  # Judiciary: antitrust
}
LEADER_TITLES = frozenset({"Chair", "Chairman", "Chairwoman", "Ranking Member"})

POINTS_BUY = 1
POINTS_SIZE = ((1_000_001, 3), (250_001, 2), (50_001, 1))
# Only the smallest $1k–$15k range: typically an advisor-run account, not a personal bet.
SMALL_TRADE_BELOW = 15_001
POINTS_SMALL = -2
POINTS_CALLS = 2
POINTS_LONG_DATED = 1
POINTS_PUTS = 1
POINTS_LEADERSHIP = 2
POINTS_CHAIR = 1
POINTS_OVERLAP = 3
POINTS_OVERLAP_CHAIR = 1
POINTS_PER_CLUSTER_MEMBER = 2
MAX_CLUSTER_POINTS = 4
POINTS_FILED_WITHIN_7D = 2
POINTS_FILED_WITHIN_14D = 1
POINTS_FILED_LATE = -1
LATE_FILING_DAYS = 30
LONG_DATED_DAYS = 180
CLUSTER_WINDOW_DAYS = 30

EXPIRY_RE = re.compile(r"(\d{1,2})/(\d{1,2})/(\d{4}|\d{2})\b")


@dataclass(frozen=True)
class CommitteeSeat:
    committee_id: str
    name: str
    title: str | None = None

    @property
    def leads(self) -> bool:
        return self.title in LEADER_TITLES


@dataclass(frozen=True)
class MemberProfile:
    name: str
    party: str | None = None
    leadership: str | None = None
    committees: list[CommitteeSeat] = field(default_factory=list)

    @property
    def party_letter(self) -> str:
        return (self.party or "?")[0]

    @property
    def top_role(self) -> str | None:
        """The one-line role shown on a card: leadership first, then committee chairs."""
        if self.leadership:
            return self.leadership
        chairs = [f"{s.title}, {s.name}" for s in self.committees if s.leads]
        return chairs[0] if chairs else None


@dataclass(frozen=True)
class Signal:
    points: int
    label: str


@dataclass(frozen=True)
class ScoredTrade:
    trade: CongressTrade
    score: int
    signals: list[Signal]


def sector_for_sic(sic: int | None) -> str | None:
    if sic is None:
        return None
    for low, high, sector in SIC_SECTORS:
        if low <= sic <= high:
            return sector
    return None


def _short_committee_name(name: str) -> str:
    for prefix in ("House Permanent Select Committee on ", "House Select Committee on ", "House Committee on "):
        if name.startswith(prefix):
            name = name.removeprefix(prefix)
            break
    return name.removeprefix("the ")


def district_key(state: str, district: int | str) -> str:
    return f"{state}{int(district):02d}"


def build_profiles(
    legislators: list[dict[str, Any]],
    committees: list[dict[str, Any]],
    membership: dict[str, list[dict[str, Any]]],
    *,
    today: date,
) -> dict[str, MemberProfile]:
    """House member profiles keyed by state + district ("CA11"), which is how PTRs identify them."""
    names = {c["thomas_id"]: _short_committee_name(c["name"]) for c in committees if c.get("type") == "house"}
    seats: dict[str, list[CommitteeSeat]] = {}
    for committee_id, members in membership.items():
        if committee_id not in names:
            continue  # Senate/joint committees and subcommittees (e.g. HSAS25)
        for member in members:
            seat = CommitteeSeat(committee_id, names[committee_id], member.get("title"))
            seats.setdefault(member.get("bioguide", ""), []).append(seat)

    profiles: dict[str, MemberProfile] = {}
    for person in legislators:
        term = (person.get("terms") or [{}])[-1]
        if term.get("type") != "rep" or "district" not in term:
            continue
        leadership = None
        for role in person.get("leadership_roles") or []:
            if role.get("chamber") == "house" and (not role.get("end") or role["end"] >= today.isoformat()):
                leadership = role.get("title")
        bioguide = person.get("id", {}).get("bioguide", "")
        name = person.get("name", {})
        profiles[district_key(term["state"], term["district"])] = MemberProfile(
            name=name.get("official_full") or f"{name.get('first', '')} {name.get('last', '')}".strip(),
            party=term.get("party"),
            leadership=leadership,
            committees=seats.get(bioguide, []),
        )
    return profiles


async def fetch_profiles(client: httpx.AsyncClient, *, today: date) -> dict[str, MemberProfile]:
    """Empty on failure: trades are still scored on size, options, clusters and freshness."""
    try:
        responses = [await client.get(url, timeout=30.0) for url in (LEGISLATORS_URL, COMMITTEES_URL, MEMBERSHIP_URL)]
        for response in responses:
            response.raise_for_status()
        legislators, committees, membership = (r.json() for r in responses)
        return build_profiles(legislators, committees, membership, today=today)
    except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
        print(f"congress: member roles unavailable: {exc}", file=sys.stderr)
        return {}


def option_expiry(description: str | None) -> date | None:
    """Expiry from free-text like "expiration date of 6/17/27" or "Expires 10/16/2026"."""
    if not description:
        return None
    dates: list[date] = []
    for month, day, year in EXPIRY_RE.findall(description):
        full_year = int(year) + 2000 if len(year) == 2 else int(year)
        try:
            dates.append(date(full_year, int(month), int(day)))
        except ValueError:
            continue
    return max(dates) if dates else None


def cluster_members(
    ticker: str,
    traded: date,
    recent_buys: list[dict[str, Any]],
    *,
    window_days: int = CLUSTER_WINDOW_DAYS,
) -> set[str]:
    """Distinct members who bought this ticker within the window around this trade."""
    window = timedelta(days=window_days)
    members: set[str] = set()
    for buy in recent_buys:
        if buy.get("ticker") != ticker:
            continue
        try:
            bought = date.fromisoformat(buy["traded"])
        except (KeyError, ValueError):
            continue
        if abs(bought - traded) <= window:
            members.add(buy.get("member", ""))
    return members


def score_trade(
    trade: CongressTrade,
    filing: PtrFiling,
    profile: MemberProfile | None,
    sector: str | None,
    cluster: set[str],
) -> ScoredTrade:
    signals: list[Signal] = []
    if trade.kind != KIND_BUY:
        return ScoredTrade(trade, 0, signals)
    signals.append(Signal(POINTS_BUY, "purchase"))

    for threshold, points in POINTS_SIZE:
        if trade.amount_low >= threshold:
            signals.append(Signal(points, "large position"))
            break
    else:
        if trade.amount_low < SMALL_TRADE_BELOW:
            signals.append(Signal(POINTS_SMALL, "small (under $15k)"))

    if trade.is_option:
        side = trade.option_side
        if side == "calls":
            signals.append(Signal(POINTS_CALLS, "call options"))
            expiry = option_expiry(trade.description)
            if expiry is not None and (expiry - trade.traded).days >= LONG_DATED_DAYS:
                months = round((expiry - trade.traded).days / 30.4)
                signals.append(Signal(POINTS_LONG_DATED, f"long-dated ({months} months)"))
        elif side == "puts":
            signals.append(Signal(POINTS_PUTS, "put options (bearish bet)"))

    if profile is not None:
        if profile.leadership:
            signals.append(Signal(POINTS_LEADERSHIP, profile.leadership))
        overlapping = [s for s in profile.committees if sector and sector in COMMITTEE_SECTORS.get(s.committee_id, ())]
        if overlapping:
            seat = next((s for s in overlapping if s.leads), overlapping[0])
            label = f"{seat.title + ' of ' if seat.leads else 'sits on '}{seat.name}, which oversees {sector}"
            signals.append(Signal(POINTS_OVERLAP + (POINTS_OVERLAP_CHAIR if seat.leads else 0), label))
        elif any(s.leads for s in profile.committees):
            seat = next(s for s in profile.committees if s.leads)
            signals.append(Signal(POINTS_CHAIR, f"{seat.title}, {seat.name}"))

    others = len(cluster - {filing.member})
    if others:
        points = min(others * POINTS_PER_CLUSTER_MEMBER, MAX_CLUSTER_POINTS)
        signals.append(Signal(points, f"cluster: {others + 1} members bought within {CLUSTER_WINDOW_DAYS}d"))

    lag = (filing.filed - trade.traded).days
    if lag <= 7:
        signals.append(Signal(POINTS_FILED_WITHIN_7D, f"fresh: filed {max(lag, 0)}d after"))
    elif lag <= 14:
        signals.append(Signal(POINTS_FILED_WITHIN_14D, f"filed {lag}d after"))
    elif lag > LATE_FILING_DAYS:
        signals.append(Signal(POINTS_FILED_LATE, f"stale: filed {lag}d after"))

    return ScoredTrade(trade, sum(s.points for s in signals), signals)


def record_buys(
    recent_buys: list[dict[str, Any]],
    filing: PtrFiling,
    trades: list[CongressTrade],
    *,
    today: date,
    retention_days: int = 90,
) -> list[dict[str, Any]]:
    """Remember purchases so a later filing can be recognised as part of a cluster."""
    added = [
        {"ticker": t.ticker, "member": filing.member, "traded": t.traded.isoformat()}
        for t in trades
        if t.kind == KIND_BUY and not t.amended
    ]
    cutoff = (today - timedelta(days=retention_days)).isoformat()
    kept = [b for b in [*recent_buys, *added] if b.get("traded", "") >= cutoff]
    unique = {(b["ticker"], b["member"], b["traded"]): b for b in kept}
    return list(unique.values())
