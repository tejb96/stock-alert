"""News headlines (DuckDuckGo) and structured catalyst summaries (Claude API)."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from dataclasses import dataclass, field
from typing import Any

import anthropic

from common import env

DEFAULT_RESEARCH_MODEL = "claude-opus-5-5"

CATALYST_TYPES = (
    "earnings",
    "guidance",
    "fda/clinical",
    "contract/partnership",
    "m&a",
    "insider/institutional",
    "analyst",
    "product",
    "legal/regulatory",
    "macro/sector",
    "meme/social",
    "none found",
)
PRICED_IN_VALUES = ("no", "partly", "yes", "unclear")
SENTIMENT_VALUES = ("bullish", "bearish", "mixed", "unclear")


@dataclass(frozen=True)
class NewsHeadline:
    title: str
    url: str
    source: str
    body: str | None = None


@dataclass(frozen=True)
class Research:
    ticker: str
    headlines: list[NewsHeadline]
    catalyst: str | None = None
    catalyst_type: str | None = None
    fresh: bool | None = None
    priced_in: str | None = None
    risks: list[str] = field(default_factory=list)
    sentiment: str | None = None
    sentiment_reason: str | None = None


def news_query(ticker: str, name: str = "") -> str:
    """Short tickers are ambiguous ("MSS" is also a racing series), so anchor on the company name."""
    return f'"{name}" {ticker} stock' if name else f"{ticker} stock"


def _fetch_news_sync(query: str, max_results: int) -> list[NewsHeadline]:
    from ddgs import DDGS

    rows = DDGS().news(
        query=query,
        region="us-en",
        timelimit="w",
        max_results=max_results,
    )
    headlines: list[NewsHeadline] = []
    for row in rows:
        title = row.get("title")
        url = row.get("url")
        if not title or not url:
            continue
        source = str(row.get("source") or "unknown")
        body_raw = row.get("body")
        headlines.append(
            NewsHeadline(
                title=str(title).strip(),
                url=str(url).strip(),
                source=source.strip(),
                body=str(body_raw) if body_raw else None,
            )
        )
    return headlines


async def fetch_news_headlines(ticker: str, *, max_results: int, name: str = "") -> list[NewsHeadline]:
    try:
        headlines = await asyncio.to_thread(_fetch_news_sync, news_query(ticker, name), max_results)
        if not headlines and name:
            headlines = await asyncio.to_thread(_fetch_news_sync, news_query(ticker), max_results)
        return headlines
    # ddgs is a scraper whose failure modes change between releases; a missing headline must never
    # sink the whole run, so anything it raises just means "no news".
    except Exception as exc:  # noqa: BLE001
        print(f"research: news fetch failed for {ticker}: {exc}", file=sys.stderr)
        return []


def build_research_prompt(ticker: str, context: str, headlines: list[NewsHeadline]) -> list[dict[str, str]]:
    headline_lines = [
        f"{i}. {h.title} ({h.source})" + (f" — {h.body}" if h.body else "") for i, h in enumerate(headlines, start=1)
    ]
    user_content = (
        f"Ticker: {ticker}\n{context}\n\n"
        f"Recent headlines (past week):\n{chr(10).join(headline_lines) or '(none found)'}\n\n"
        "Return JSON with exactly these keys:\n"
        '  "catalyst": one sentence (max 200 chars) on the most likely reason for the Reddit buzz, '
        "or say the headlines don't explain it\n"
        f'  "catalyst_type": one of {list(CATALYST_TYPES)}\n'
        '  "fresh": true if the catalyst news is from the last ~48 hours, false if older\n'
        f'  "priced_in": one of {list(PRICED_IN_VALUES)} — has the price move already reflected the catalyst?\n'
        '  "risks": up to 3 short strings (e.g. dilution, upcoming earnings, low float, pump-and-dump pattern)\n'
        f'  "sentiment": one of {list(SENTIMENT_VALUES)} — is the news flow driving this buzz good or bad for the '
        "stock? A crash, lawsuit, downgrade or selloff is bearish even when people are buying the dip\n"
        '  "sentiment_reason": one short sentence (max 140 chars) on why, e.g. "Down 30% in 3 months on AI '
        'capex fears; buzz is mostly bagholders and dip-buyers"\n'
        "Use only the data above. Do not invent facts."
    )
    return [
        {
            "role": "system",
            "content": (
                "You are a factual market analyst writing terse context for a trader's alert feed. "
                "No price targets or buy/sell advice. Output only a JSON object."
            ),
        },
        {"role": "user", "content": user_content},
    ]


def parse_research_json(ticker: str, content: str, headlines: list[NewsHeadline]) -> Research:
    """Parse the model's JSON, tolerating code fences and bad values. Falls back to plain text."""
    text = content.strip()
    if text.startswith("```"):
        text = text.strip("`").removeprefix("json").strip()
    try:
        data: Any = json.loads(text)
    except json.JSONDecodeError:
        return Research(ticker, headlines, catalyst=content.strip() or None)
    if not isinstance(data, dict):
        return Research(ticker, headlines, catalyst=content.strip() or None)

    catalyst = data.get("catalyst")
    catalyst_type = str(data.get("catalyst_type") or "").strip().lower()
    priced_in = str(data.get("priced_in") or "").strip().lower()
    fresh = data.get("fresh")
    risks_raw = data.get("risks")
    risks = [str(r).strip() for r in risks_raw if str(r).strip()][:3] if isinstance(risks_raw, list) else []
    sentiment = str(data.get("sentiment") or "").strip().lower()
    sentiment_reason = data.get("sentiment_reason")

    return Research(
        ticker=ticker,
        headlines=headlines,
        catalyst=str(catalyst).strip() if catalyst else None,
        catalyst_type=catalyst_type if catalyst_type in CATALYST_TYPES else None,
        fresh=fresh if isinstance(fresh, bool) else None,
        priced_in=priced_in if priced_in in PRICED_IN_VALUES else None,
        risks=risks,
        sentiment=sentiment if sentiment in SENTIMENT_VALUES else None,
        sentiment_reason=str(sentiment_reason).strip() if sentiment_reason else None,
    )


RESEARCH_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "catalyst": {"type": "string"},
        "catalyst_type": {"type": "string", "enum": list(CATALYST_TYPES)},
        "fresh": {"type": "boolean"},
        "priced_in": {"type": "string", "enum": list(PRICED_IN_VALUES)},
        "risks": {"type": "array", "items": {"type": "string"}},
        "sentiment": {"type": "string", "enum": list(SENTIMENT_VALUES)},
        "sentiment_reason": {"type": "string"},
    },
    "required": ["catalyst", "catalyst_type", "fresh", "priced_in", "risks", "sentiment", "sentiment_reason"],
    "additionalProperties": False,
}
# Re-runs a request the model's safety classifiers decline on Anthropic's recommended fallback model.
FALLBACK_BETA = "server-side-fallback-2026-07-01"

_anthropic_client: anthropic.AsyncAnthropic | None = None


def _client() -> anthropic.AsyncAnthropic:
    global _anthropic_client
    if _anthropic_client is None:
        _anthropic_client = anthropic.AsyncAnthropic(timeout=90.0, max_retries=3)
    return _anthropic_client


def research_available() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY", "").strip())


async def summarize_with_claude(ticker: str, context: str, headlines: list[NewsHeadline]) -> Research | None:
    """Catalyst, sentiment and risks as schema-checked JSON. None when unconfigured or the call fails."""
    if not research_available():
        return None

    messages = build_research_prompt(ticker, context, headlines)
    try:
        response = await _client().beta.messages.create(
            model=env("RESEARCH_MODEL", DEFAULT_RESEARCH_MODEL),
            max_tokens=4000,
            system=messages[0]["content"],
            messages=[{"role": "user", "content": messages[1]["content"]}],
            output_config={"effort": "low", "format": {"type": "json_schema", "schema": RESEARCH_SCHEMA}},
            betas=[FALLBACK_BETA],
            fallbacks="default",
        )
    except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as exc:
        print(f"research: Claude API rejected the key for {ticker}: {exc.message}", file=sys.stderr)
        return None
    except anthropic.APIStatusError as exc:
        print(f"research: Claude API error for {ticker}: {exc.status_code} {exc.message}", file=sys.stderr)
        return None
    except anthropic.APIConnectionError as exc:
        print(f"research: Claude API unreachable for {ticker}: {exc!r}", file=sys.stderr)
        return None

    if response.stop_reason == "refusal":
        print(f"research: Claude declined to research {ticker}", file=sys.stderr)
        return None
    text = next((block.text for block in response.content if block.type == "text"), None)
    if not text:
        print(f"research: Claude returned no text for {ticker} (stop: {response.stop_reason})", file=sys.stderr)
        return None
    return parse_research_json(ticker, text, headlines)


async def research_ticker(
    ticker: str,
    context: str,
    *,
    max_news: int,
    name: str = "",
) -> Research | None:
    headlines = await fetch_news_headlines(ticker, max_results=max_news, name=name)
    summary = await summarize_with_claude(ticker, context, headlines)
    if summary is not None:
        return summary
    return Research(ticker, headlines) if headlines else None
