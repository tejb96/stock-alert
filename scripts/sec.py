"""SEC EDGAR: insider trades (Form 4) and material events (8-K).

EDGAR requires a declared User-Agent with a contact email and allows at most 10 requests/second:
https://www.sec.gov/os/accessing-edgar-data
"""

from __future__ import annotations

import asyncio
import re
import sys
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

import httpx

from common import format_usd

SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SEC_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
SEC_SUBMISSION_TXT_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{folder}/{accession}.txt"
SEC_FILING_INDEX_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{folder}/{accession}-index.htm"
SEC_CURRENT_FEED_URL = "https://www.sec.gov/cgi-bin/browse-edgar"

SEC_MIN_INTERVAL_SECONDS = 0.12
ATOM_NS = {"atom": "http://www.w3.org/2005/Atom"}
FEED_TITLE_RE = re.compile(r"^(?P<form>\S+) - (?P<name>.*) \((?P<cik>\d{10})\) \((?P<role>[^)]+)\)$")
FEED_ITEM_RE = re.compile(r"Item (\d+\.\d+)")

EIGHT_K_ITEMS = {
    "1.01": "Material agreement",
    "1.02": "Agreement terminated",
    "1.03": "Bankruptcy",
    "1.05": "Cybersecurity incident",
    "2.01": "Acquisition / disposal",
    "2.02": "Earnings results",
    "2.03": "New debt",
    "2.04": "Debt acceleration",
    "2.05": "Restructuring / layoffs",
    "2.06": "Impairment",
    "3.01": "Delisting notice",
    "3.02": "Unregistered share sale",
    "3.03": "Shareholder rights change",
    "4.01": "Auditor change",
    "4.02": "Restatement",
    "5.01": "Change in control",
    "5.02": "Officer / director change",
    "5.03": "Bylaws change",
    "5.07": "Shareholder vote",
    "7.01": "Reg FD disclosure",
    "8.01": "Other material event",
    "9.01": "Exhibits",
}
# Routine items that don't move stocks on their own.
LOW_SIGNAL_ITEMS = frozenset({"9.01", "5.07", "5.03"})


def user_agent(contact_email: str) -> str:
    return f"stock-alert {contact_email}"


def accession_folder(accession: str) -> str:
    return accession.replace("-", "")


def filing_index_url(cik: int, accession: str) -> str:
    return SEC_FILING_INDEX_URL.format(cik=cik, folder=accession_folder(accession), accession=accession)


def describe_items(items: list[str]) -> str:
    notable = [i for i in items if i not in LOW_SIGNAL_ITEMS] or items
    return ", ".join(f"{EIGHT_K_ITEMS.get(i, 'Item ' + i)} ({i})" for i in notable)


def is_notable_8k(items: list[str]) -> bool:
    return any(item not in LOW_SIGNAL_ITEMS for item in items)


@dataclass(frozen=True)
class Form4:
    accession: str
    issuer_cik: int
    ticker: str
    issuer_name: str
    owner: str
    role: str
    is_officer: bool
    is_director: bool
    is_ten_percent_owner: bool
    planned: bool
    filed: str
    buy_shares: float = 0.0
    buy_value: float = 0.0
    sell_value: float = 0.0
    last_buy_date: str | None = None

    @property
    def buy_avg_price(self) -> float | None:
        return self.buy_value / self.buy_shares if self.buy_shares else None

    @property
    def is_insider(self) -> bool:
        """Officer or director: people with an operating view of the business, not just a fund."""
        return self.is_officer or self.is_director


@dataclass(frozen=True)
class FilingRef:
    form: str
    accession: str
    filed: str
    items: list[str] = field(default_factory=list)
    primary_document: str = ""


@dataclass(frozen=True)
class FeedEntry:
    form: str
    cik: int
    name: str
    role: str
    accession: str
    updated: datetime
    items: list[str]


@dataclass(frozen=True)
class SecActivity:
    ticker: str
    cik: int
    buys: list[Form4]
    sell_value: float
    eight_ks: list[FilingRef]

    @property
    def buy_value(self) -> float:
        return sum(f.buy_value for f in self.buys)

    @property
    def buyer_count(self) -> int:
        return len({f.owner for f in self.buys})


def _text(node: ET.Element | None, path: str) -> str | None:
    if node is None:
        return None
    found = node.find(path)
    if found is None or found.text is None:
        return None
    text = found.text.strip()
    return text or None


def _flag(node: ET.Element | None, path: str) -> bool:
    return (_text(node, path) or "").lower() in ("1", "true")


def _float(node: ET.Element | None, path: str) -> float | None:
    raw = _text(node, path)
    if raw is None:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def extract_ownership_xml(submission_text: str) -> str | None:
    start = submission_text.find("<ownershipDocument")
    end = submission_text.find("</ownershipDocument>")
    if start < 0 or end < 0:
        return None
    return submission_text[start : end + len("</ownershipDocument>")]


def _role(relationship: ET.Element | None) -> str:
    title = _text(relationship, "officerTitle")
    if _flag(relationship, "isOfficer") and title:
        return title
    if _flag(relationship, "isOfficer"):
        return "Officer"
    if _flag(relationship, "isDirector"):
        return "Director"
    if _flag(relationship, "isTenPercentOwner"):
        return "10% owner"
    return _text(relationship, "otherText") or "Insider"


def parse_form4(xml_text: str, *, accession: str, filed: str) -> Form4 | None:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return None

    issuer = root.find("issuer")
    ticker = (_text(issuer, "issuerTradingSymbol") or "").upper()
    cik_raw = _text(issuer, "issuerCik")
    if not ticker or not cik_raw:
        return None

    owner_node = root.find("reportingOwner")
    relationship = owner_node.find("reportingOwnerRelationship") if owner_node is not None else None
    owner = _text(owner_node, "reportingOwnerId/rptOwnerName") or "Unknown"

    buy_shares = buy_value = sell_value = 0.0
    last_buy_date: str | None = None
    for txn in root.findall("nonDerivativeTable/nonDerivativeTransaction"):
        code = _text(txn, "transactionCoding/transactionCode")
        shares = _float(txn, "transactionAmounts/transactionShares/value")
        price = _float(txn, "transactionAmounts/transactionPricePerShare/value")
        if shares is None or price is None or price <= 0:
            continue
        # P = open-market purchase, S = open-market sale. Grants, exercises and gifts are ignored.
        if code == "P":
            buy_shares += shares
            buy_value += shares * price
            txn_date = _text(txn, "transactionDate/value")
            if txn_date and (last_buy_date is None or txn_date > last_buy_date):
                last_buy_date = txn_date
        elif code == "S":
            sell_value += shares * price

    return Form4(
        accession=accession,
        issuer_cik=int(cik_raw),
        ticker=ticker,
        issuer_name=_text(issuer, "issuerName") or ticker,
        owner=owner.title() if owner.isupper() else owner,
        role=_role(relationship),
        is_officer=_flag(relationship, "isOfficer"),
        is_director=_flag(relationship, "isDirector"),
        is_ten_percent_owner=_flag(relationship, "isTenPercentOwner"),
        planned=_flag(root, "aff10b5One"),
        filed=filed,
        buy_shares=buy_shares,
        buy_value=buy_value,
        sell_value=sell_value,
        last_buy_date=last_buy_date,
    )


def parse_recent_filings(payload: dict[str, Any]) -> list[FilingRef]:
    recent = payload.get("filings", {}).get("recent", {})
    forms = recent.get("form") or []
    refs: list[FilingRef] = []
    for i, form in enumerate(forms):
        try:
            accession = recent["accessionNumber"][i]
            filed = recent["filingDate"][i]
        except (KeyError, IndexError):
            continue
        items_raw = (recent.get("items") or [""] * len(forms))[i] or ""
        primary = (recent.get("primaryDocument") or [""] * len(forms))[i] or ""
        items = [item.strip() for item in items_raw.split(",") if item.strip()]
        refs.append(FilingRef(form=form, accession=accession, filed=filed, items=items, primary_document=primary))
    return refs


def parse_current_feed(xml_text: str) -> list[FeedEntry]:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []

    entries: list[FeedEntry] = []
    for entry in root.findall("atom:entry", ATOM_NS):
        title = entry.findtext("atom:title", default="", namespaces=ATOM_NS).strip()
        match = FEED_TITLE_RE.match(title)
        entry_id = entry.findtext("atom:id", default="", namespaces=ATOM_NS)
        updated_raw = entry.findtext("atom:updated", default="", namespaces=ATOM_NS)
        if not match or "accession-number=" not in entry_id:
            continue
        try:
            updated = datetime.fromisoformat(updated_raw).astimezone(UTC)
        except ValueError:
            continue
        summary = entry.findtext("atom:summary", default="", namespaces=ATOM_NS)
        entries.append(
            FeedEntry(
                form=match["form"],
                cik=int(match["cik"]),
                name=match["name"],
                role=match["role"],
                accession=entry_id.split("accession-number=", 1)[1].strip(),
                updated=updated,
                items=FEED_ITEM_RE.findall(summary),
            )
        )
    return entries


class SecClient:
    """Rate-limited EDGAR access with the declared User-Agent SEC requires."""

    def __init__(self, client: httpx.AsyncClient, contact_email: str) -> None:
        self._client = client
        self._headers = {"User-Agent": user_agent(contact_email), "Accept-Encoding": "gzip, deflate"}
        self._lock = asyncio.Lock()
        self._last_request = 0.0
        self._tickers: dict[str, tuple[int, str]] | None = None
        self._tickers_lock = asyncio.Lock()

    async def get(self, url: str, **params: Any) -> httpx.Response:
        async with self._lock:
            wait = SEC_MIN_INTERVAL_SECONDS - (time.monotonic() - self._last_request)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_request = time.monotonic()
        response = await self._client.get(url, headers=self._headers, params=params or None, timeout=20.0)
        response.raise_for_status()
        return response

    async def ticker_map(self) -> dict[str, tuple[int, str]]:
        """Ticker → (CIK, company title)."""
        async with self._tickers_lock:
            if self._tickers is None:
                payload = (await self.get(SEC_TICKERS_URL)).json()
                self._tickers = {
                    str(row["ticker"]).upper(): (int(row["cik_str"]), str(row["title"]))
                    for row in payload.values()
                    if isinstance(row, dict) and row.get("ticker") and row.get("cik_str")
                }
        return self._tickers

    async def cik_for(self, ticker: str) -> int | None:
        entry = (await self.ticker_map()).get(ticker.upper())
        return entry[0] if entry else None

    async def recent_filings(self, cik: int) -> list[FilingRef]:
        return parse_recent_filings((await self.get(SEC_SUBMISSIONS_URL.format(cik=cik))).json())

    async def fetch_form4(self, cik: int, accession: str, filed: str) -> Form4 | None:
        url = SEC_SUBMISSION_TXT_URL.format(cik=cik, folder=accession_folder(accession), accession=accession)
        xml_text = extract_ownership_xml((await self.get(url)).text)
        if xml_text is None:
            return None
        return parse_form4(xml_text, accession=accession, filed=filed)

    async def current_feed(self, form: str, *, start: int = 0, count: int = 100) -> list[FeedEntry]:
        response = await self.get(
            SEC_CURRENT_FEED_URL,
            action="getcurrent",
            type=form,
            owner="include",
            start=str(start),
            count=str(count),
            output="atom",
        )
        return parse_current_feed(response.text)


async def ticker_activity(
    sec: SecClient,
    ticker: str,
    *,
    today: date,
    form4_days: int = 30,
    eight_k_days: int = 7,
    max_form4: int = 10,
) -> SecActivity | None:
    """Recent insider buys/sells and notable 8-Ks for one ticker. None if SEC doesn't know it."""
    try:
        cik = await sec.cik_for(ticker)
        if cik is None:
            return None
        filings = await sec.recent_filings(cik)

        form4_cutoff = (today - timedelta(days=form4_days)).isoformat()
        eight_k_cutoff = (today - timedelta(days=eight_k_days)).isoformat()
        form4_refs = [f for f in filings if f.form == "4" and f.filed >= form4_cutoff][:max_form4]
        eight_ks = [f for f in filings if f.form == "8-K" and f.filed >= eight_k_cutoff and is_notable_8k(f.items)]

        parsed = await asyncio.gather(*[sec.fetch_form4(cik, f.accession, f.filed) for f in form4_refs])
    except (httpx.HTTPError, ValueError, KeyError) as exc:
        print(f"sec: activity lookup failed for {ticker}: {exc}", file=sys.stderr)
        return None

    form4s = [f for f in parsed if f is not None]
    return SecActivity(
        ticker=ticker,
        cik=cik,
        buys=[f for f in form4s if f.buy_value > 0],
        sell_value=sum(f.sell_value for f in form4s),
        eight_ks=eight_ks,
    )


def _days_ago(filed: str, today: date) -> str:
    try:
        delta = (today - date.fromisoformat(filed)).days
    except ValueError:
        return filed
    if delta <= 0:
        return "today"
    if delta == 1:
        return "1d ago"
    return f"{delta}d ago"


def format_activity_line(activity: SecActivity, *, today: date) -> str | None:
    parts: list[str] = []
    if activity.buys:
        latest = max(activity.buys, key=lambda f: f.filed)
        who = f"{latest.role} {latest.owner}" if activity.buyer_count == 1 else f"{activity.buyer_count} insiders"
        parts.append(f"**{who} bought {format_usd(activity.buy_value)}** (30d, latest {_days_ago(latest.filed, today)})")
    elif activity.sell_value > 0:
        parts.append(f"Insiders: no buys, sold {format_usd(activity.sell_value)} (30d)")
    for filing in activity.eight_ks[:2]:
        parts.append(f"8-K {_days_ago(filing.filed, today)}: {describe_items(filing.items)}")
    if not parts:
        return None
    return "🏛 " + " · ".join(parts)
