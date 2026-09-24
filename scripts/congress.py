"""House members' stock trades from the Clerk's official Periodic Transaction Reports (PTRs).

The STOCK Act makes members report trades within 45 days. The Clerk publishes a yearly index of
every disclosure (a zip with one XML file) and each e-filed PTR as a text PDF, which we parse.
Paper filings are scans with no text layer, so they're skipped.

The Senate's eFD site blocks automated clients, and executive-branch (White House) trade reports
aren't published as a machine-readable feed, so this covers the House only.
"""

from __future__ import annotations

import asyncio
import io
import re
import sys
import zipfile
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from typing import Any
from xml.etree import ElementTree as ET

import httpx
from pypdf.errors import PyPdfError

from market import YAHOO_CHART_URL

HOUSE_INDEX_URL = "https://disclosures-clerk.house.gov/public_disc/financial-pdfs/{year}FD.zip"
HOUSE_PTR_URL = "https://disclosures-clerk.house.gov/public_disc/ptr-pdfs/{year}/{doc_id}.pdf"

KIND_BUY = "buy"
KIND_SELL = "sell"
KIND_EXCHANGE = "exchange"
TRANSACTION_KINDS = {"P": KIND_BUY, "S": KIND_SELL, "S (partial)": KIND_SELL, "E": KIND_EXCHANGE}
OWNERS = {"SP": "spouse", "JT": "joint", "DC": "child"}
# Stocks and options on them; bonds, funds and private holdings say little about a company.
TRADED_ASSET_TYPES = frozenset({"ST", "OP"})

# "... Common Stock (BE) [ST]\nP 07/24/2026 07/24/2026 $1,000,001 -\n$5,000,000"
TRADE_RE = re.compile(
    r"\((?P<ticker>[A-Z][A-Z0-9.]{0,6})\)\s*\[(?P<asset_type>[A-Z]{2})\]\s*"
    r"(?P<type>S \(partial\)|P|S|E)\s+"
    r"(?P<traded>\d{2}/\d{2}/\d{4})\s+(?P<notified>\d{2}/\d{2}/\d{4})\s+"
    r"(?P<amount>Over \$[\d,]+|\$[\d,]+(?:\s*-\s*\$[\d,]+)?)"
)
# Lines that end one transaction's details or belong to the table header, never to an asset name.
FIELD_PREFIXES = ("F S:", "S O:", "D:", "L:", "C:", "$200?", "Gains >", "Filing ID #")


@dataclass(frozen=True)
class PtrFiling:
    doc_id: str
    member: str
    district: str
    filed: date

    @property
    def year(self) -> int:
        return self.filed.year

    @property
    def url(self) -> str:
        return HOUSE_PTR_URL.format(year=self.year, doc_id=self.doc_id)

    @property
    def electronic(self) -> bool:
        """E-filed PTRs have 8-digit IDs starting with 2; paper scans start with 8 and have no text."""
        return self.doc_id.startswith("2")


@dataclass(frozen=True)
class CongressTrade:
    ticker: str
    asset_type: str
    kind: str
    partial: bool
    traded: date
    notified: date
    amount_low: int
    amount_high: int | None
    owner: str
    description: str | None = None
    amended: bool = False
    """A correction to an older filing: not new information."""
    fills: int = 1
    """Separate transactions merged into this one (same stock, side and owner in one filing)."""

    @property
    def is_option(self) -> bool:
        return self.asset_type == "OP"

    @property
    def option_side(self) -> str:
        text = (self.description or "").lower()
        if "call" in text:
            return "calls"
        if "put" in text:
            return "puts"
        return "options"

    @property
    def yahoo_symbol(self) -> str:
        return self.ticker.replace(".", "-")


def _us_date(raw: str) -> date:
    return datetime.strptime(raw.strip(), "%m/%d/%Y").replace(tzinfo=UTC).date()


def parse_house_index(xml_bytes: bytes) -> list[PtrFiling]:
    """Periodic Transaction Reports (FilingType P) from the Clerk's yearly disclosure index."""
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as exc:
        print(f"congress: unreadable House index: {exc}", file=sys.stderr)
        return []
    filings: list[PtrFiling] = []
    for member in root.iter("Member"):
        if (member.findtext("FilingType") or "").strip() != "P":
            continue
        doc_id = (member.findtext("DocID") or "").strip()
        try:
            filed = _us_date(member.findtext("FilingDate") or "")
        except ValueError:
            continue
        name = " ".join(p for p in ((member.findtext("First") or "").strip(), (member.findtext("Last") or "").strip()) if p)
        if doc_id and name:
            filings.append(PtrFiling(doc_id, name, (member.findtext("StateDst") or "").strip(), filed))
    return filings


def parse_amount(raw: str) -> tuple[int, int | None]:
    """"$15,001 - $50,000" → (15001, 50000); "Over $50,000,000" → (50000001, None)."""
    numbers = [int(n.replace(",", "")) for n in re.findall(r"\$([\d,]+)", raw)]
    if raw.startswith("Over"):
        return numbers[0] + 1, None
    return numbers[0], numbers[1] if len(numbers) > 1 else None


def _owner(before: str) -> str:
    """Owner code opens the asset's first line, after the previous trade's detail lines."""
    lines = [line.strip() for line in before.splitlines()]
    asset_lines: list[str] = []
    for line in reversed(lines):
        if not line or line.startswith(FIELD_PREFIXES):
            break
        asset_lines.append(line)
    if not asset_lines:
        return "self"
    tokens = asset_lines[-1].split()
    # Amended filings put a numeric transaction ID before the owner code.
    if tokens and tokens[0].isdigit():
        tokens = tokens[1:]
    return OWNERS.get(tokens[0], "self") if tokens else "self"


def _field(after: str, prefix: str) -> str | None:
    for line in after.splitlines():
        line = line.strip()
        if line.startswith(prefix):
            return line.removeprefix(prefix).strip() or None
    return None


def parse_ptr_text(text: str) -> list[CongressTrade]:
    text = text.replace("\x00", "")
    matches = list(TRADE_RE.finditer(text))
    trades: list[CongressTrade] = []
    for i, match in enumerate(matches):
        if match["asset_type"] not in TRADED_ASSET_TYPES:
            continue
        previous_end = matches[i - 1].end() if i else 0
        next_start = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        try:
            traded, notified = _us_date(match["traded"]), _us_date(match["notified"])
        except ValueError:
            continue
        low, high = parse_amount(match["amount"])
        details = text[match.end():next_start]
        trades.append(
            CongressTrade(
                ticker=match["ticker"],
                asset_type=match["asset_type"],
                kind=TRANSACTION_KINDS[match["type"]],
                partial=match["type"] == "S (partial)",
                traded=traded,
                notified=notified,
                amount_low=low,
                amount_high=high,
                owner=_owner(text[previous_end:match.start()]),
                description=_field(details, "D:"),
                amended=(_field(details, "F S:") or "").lower() == "amended",
            )
        )
    return trades


def extract_pdf_text(pdf_bytes: bytes) -> str:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(pdf_bytes))
    return "\n".join(page.extract_text() or "" for page in reader.pages).replace("\x00", "")


async def fetch_house_index(client: httpx.AsyncClient, year: int) -> list[PtrFiling]:
    response = await client.get(HOUSE_INDEX_URL.format(year=year), timeout=60.0)
    response.raise_for_status()
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        name = next((n for n in archive.namelist() if n.lower().endswith(".xml")), None)
        if name is None:
            return []
        return parse_house_index(archive.read(name))


async def fetch_ptr_trades(client: httpx.AsyncClient, filing: PtrFiling) -> list[CongressTrade] | None:
    """Trades in one PTR, or None when the PDF can't be fetched or read."""
    try:
        response = await client.get(filing.url, timeout=30.0)
        response.raise_for_status()
        text = await asyncio.to_thread(extract_pdf_text, response.content)
    # Malformed PDFs can also surface as plain lookup/value errors from inside pypdf.
    except (httpx.HTTPError, PyPdfError, ValueError, KeyError, IndexError, TypeError) as exc:
        print(f"congress: could not read PTR {filing.doc_id} ({filing.member}): {exc}", file=sys.stderr)
        return None
    return parse_ptr_text(text)


def merge_fills(trades: list[CongressTrade]) -> list[CongressTrade]:
    """Combine repeated stock trades (same ticker, side, owner) into one, summing the ranges.

    Options stay separate: different strikes and expiries are different bets."""
    merged: dict[tuple[str, str, str], CongressTrade] = {}
    result: list[CongressTrade] = []
    for trade in trades:
        if trade.is_option or trade.amended:
            result.append(trade)
            continue
        key = (trade.ticker, trade.kind, trade.owner)
        existing = merged.get(key)
        if existing is None:
            merged[key] = trade
            result.append(trade)
            continue
        high = None if existing.amount_high is None or trade.amount_high is None else existing.amount_high + trade.amount_high
        combined = replace(
            existing,
            traded=min(existing.traded, trade.traded),
            amount_low=existing.amount_low + trade.amount_low,
            amount_high=high,
            partial=existing.partial and trade.partial,
            fills=existing.fills + trade.fills,
        )
        merged[key] = combined
        result[result.index(existing)] = combined
    return result


def close_on(history: list[tuple[date, float]], day: date) -> float | None:
    """Close on the trade date, or the next session's if it was a weekend or holiday."""
    for bar_day, close in history:
        if bar_day >= day:
            return close
    return None


async def fetch_close_history(client: httpx.AsyncClient, ticker: str) -> list[tuple[date, float]]:
    try:
        response = await client.get(YAHOO_CHART_URL.format(symbol=ticker), params={"interval": "1d", "range": "1y"})
        response.raise_for_status()
        result: dict[str, Any] = response.json()["chart"]["result"][0]
        stamps = result["timestamp"]
        closes = result["indicators"]["quote"][0]["close"]
    except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError):
        return []
    return [
        (datetime.fromtimestamp(ts, UTC).date(), float(close))
        for ts, close in zip(stamps, closes)
        if close is not None
    ]
