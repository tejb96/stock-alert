"""Tests for research.py — run from repo root: python -m pytest scripts"""

from __future__ import annotations

import json

from research import (
    NewsHeadline,
    build_research_prompt,
    news_query,
    parse_research_json,
)

HEADLINES = [NewsHeadline("Acme wins DoD contract", "https://news.example/a", "Reuters", "snippet")]


def test_parse_research_json_full():
    content = json.dumps(
        {
            "catalyst": "DoD contract award announced Tuesday.",
            "catalyst_type": "Contract/Partnership",
            "fresh": True,
            "priced_in": "partly",
            "risks": ["small cap", "earnings in 9 days", "dilution history", "extra"],
            "sentiment": "Bearish",
            "sentiment_reason": "Selloff on guidance cut",
        }
    )
    research = parse_research_json("ACME", content, HEADLINES)
    assert research.catalyst == "DoD contract award announced Tuesday."
    assert research.catalyst_type == "contract/partnership"
    assert research.fresh is True
    assert research.priced_in == "partly"
    assert research.risks == ["small cap", "earnings in 9 days", "dilution history"]
    assert research.headlines == HEADLINES
    assert research.sentiment == "bearish"
    assert research.sentiment_reason == "Selloff on guidance cut"


def test_parse_research_json_drops_invalid_enum_values():
    content = json.dumps({"catalyst": "x", "catalyst_type": "vibes", "fresh": "yes", "priced_in": "maybe", "risks": "none"})
    research = parse_research_json("ACME", content, [])
    assert research.catalyst_type is None
    assert research.fresh is None
    assert research.priced_in is None
    assert research.risks == []


def test_parse_research_json_code_fence():
    content = '```json\n{"catalyst": "Earnings beat", "fresh": false}\n```'
    research = parse_research_json("ACME", content, [])
    assert research.catalyst == "Earnings beat"
    assert research.fresh is False


def test_parse_research_json_plain_text_fallback():
    research = parse_research_json("ACME", "Buzz is about a contract win.", HEADLINES)
    assert research.catalyst == "Buzz is about a contract win."
    assert research.catalyst_type is None


def test_build_research_prompt_includes_context_and_headlines():
    messages = build_research_prompt("ACME", "Reddit: 6 → 58 mentions\nPrice: $4.12", HEADLINES)
    user = messages[1]["content"]
    assert "Ticker: ACME" in user
    assert "Reddit: 6 → 58 mentions" in user
    assert "1. Acme wins DoD contract (Reuters) — snippet" in user
    assert '"priced_in"' in user
    assert "JSON" in messages[0]["content"]


def test_build_research_prompt_without_headlines():
    assert "(none found)" in build_research_prompt("ACME", "ctx", [])[1]["content"]


def test_news_query_anchors_on_company_name():
    assert news_query("MSS", "Maison Solutions") == '"Maison Solutions" MSS stock'
    assert news_query("MSS") == "MSS stock"
