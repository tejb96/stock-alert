"""ApeWisdom Reddit mention data and the acceleration score."""

from __future__ import annotations

import asyncio
import math
from dataclasses import dataclass
from typing import Any

import httpx

from common import NotifyError

APEWISDOM_PAGE_URL = "https://apewisdom.io/api/v1.0/filter/all-stocks/page/{page}"


@dataclass(frozen=True)
class TrendRow:
    ticker: str
    name: str
    rank: int
    rank_24h_ago: int | None
    mentions: int
    mentions_24h_ago: int | None
    upvotes: int
    change_24h: float | None
    trend_score: float


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def compute_change_24h(mentions: int, mentions_24h_ago: int | None) -> float | None:
    if mentions_24h_ago is None or mentions_24h_ago <= 0:
        return None
    return ((mentions - mentions_24h_ago) / mentions_24h_ago) * 100


def compute_trend_score(
    mentions: int,
    mentions_24h_ago: int | None,
    rank: int,
    rank_24h_ago: int | None,
) -> float:
    """Reward acceleration, not size.

    acceleration = doublings in mentions over 24h (a ticker with no prior mentions counts from 0)
    climb        = doublings in rank position over 24h (rank 200 → 25 is 3 doublings)
    score        = log2(1 + mentions) × (acceleration + 0.5 × climb)

    log2(mentions) keeps a 3 → 40 spike ahead of a 900 → 950 mega-cap while still
    preferring 40 → 400 over 3 → 30. Declines contribute nothing rather than going negative.
    """
    acceleration = max(math.log2((mentions + 1) / ((mentions_24h_ago or 0) + 1)), 0.0)
    climb = 0.0
    if rank_24h_ago is not None and rank > 0 and rank_24h_ago > 0:
        climb = max(math.log2(rank_24h_ago / rank), 0.0)
    return math.log2(1 + mentions) * (acceleration + 0.5 * climb)


def parse_apewisdom_row(raw: dict[str, Any]) -> TrendRow | None:
    ticker_raw = raw.get("ticker")
    if not ticker_raw or not isinstance(ticker_raw, str):
        return None

    try:
        rank = int(raw["rank"])
        mentions = int(raw["mentions"])
        upvotes = int(raw["upvotes"])
    except (KeyError, TypeError, ValueError):
        return None

    mentions_24h_ago = _optional_int(raw.get("mentions_24h_ago"))
    rank_24h_ago = _optional_int(raw.get("rank_24h_ago"))
    return TrendRow(
        ticker=ticker_raw.strip().upper(),
        name=str(raw.get("name") or "").strip(),
        rank=rank,
        rank_24h_ago=rank_24h_ago,
        mentions=mentions,
        mentions_24h_ago=mentions_24h_ago,
        upvotes=upvotes,
        change_24h=compute_change_24h(mentions, mentions_24h_ago),
        trend_score=compute_trend_score(mentions, mentions_24h_ago, rank, rank_24h_ago),
    )


def enrich_and_rank(rows: list[TrendRow], *, min_mentions: int, top_n: int) -> list[TrendRow]:
    eligible = [row for row in rows if row.mentions >= min_mentions and row.trend_score > 0]
    ranked = sorted(eligible, key=lambda r: r.trend_score, reverse=True)
    return ranked[:top_n]


async def fetch_apewisdom(client: httpx.AsyncClient, *, pages: int) -> list[TrendRow]:
    responses = await asyncio.gather(
        *[client.get(APEWISDOM_PAGE_URL.format(page=page)) for page in range(1, pages + 1)]
    )

    rows: list[TrendRow] = []
    for response in responses:
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise NotifyError("ApeWisdom response is not a JSON object")

        results = payload.get("results")
        if not isinstance(results, list):
            raise NotifyError("ApeWisdom response missing results list")

        for raw in results:
            if not isinstance(raw, dict):
                continue
            row = parse_apewisdom_row(raw)
            if row is not None:
                rows.append(row)

    if not rows:
        raise NotifyError("No valid ticker rows in ApeWisdom response")

    return rows
