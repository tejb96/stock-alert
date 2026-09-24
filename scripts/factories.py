"""Test data builders shared across test modules."""

from __future__ import annotations

from typing import Any

from market import StockQuote
from reddit import TrendRow, compute_change_24h, compute_trend_score


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
) -> dict[str, Any]:
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


FORM4_XML = """<?xml version="1.0"?>
<ownershipDocument>
    <documentType>4</documentType>
    <issuer>
        <issuerCik>0000012345</issuerCik>
        <issuerName>ACME CORP</issuerName>
        <issuerTradingSymbol>acme</issuerTradingSymbol>
    </issuer>
    <reportingOwner>
        <reportingOwnerId>
            <rptOwnerCik>0000099999</rptOwnerCik>
            <rptOwnerName>DOE JANE</rptOwnerName>
        </reportingOwnerId>
        <reportingOwnerRelationship>
            <isDirector>0</isDirector>
            <isOfficer>1</isOfficer>
            <officerTitle>CFO</officerTitle>
        </reportingOwnerRelationship>
    </reportingOwner>
    <aff10b5One>0</aff10b5One>
    <nonDerivativeTable>
        {transactions}
    </nonDerivativeTable>
</ownershipDocument>"""


def form4_transaction(code: str, shares: float, price: float | None, day: str = "2026-09-21") -> str:
    price_xml = f"<value>{price}</value>" if price is not None else '<footnoteId id="F1"/>'
    return f"""<nonDerivativeTransaction>
            <transactionDate><value>{day}</value></transactionDate>
            <transactionCoding><transactionCode>{code}</transactionCode></transactionCoding>
            <transactionAmounts>
                <transactionShares><value>{shares}</value></transactionShares>
                <transactionPricePerShare>{price_xml}</transactionPricePerShare>
            </transactionAmounts>
        </nonDerivativeTransaction>"""


def form4_xml(*transactions: str) -> str:
    return FORM4_XML.replace("{transactions}", "\n".join(transactions))
