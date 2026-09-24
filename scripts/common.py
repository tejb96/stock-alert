"""Shared helpers: env parsing, formatting, Discord delivery."""

from __future__ import annotations

import os
from typing import Any

import httpx

# Discord embed limits: https://discord.com/developers/docs/resources/message#embed-object-embed-limits
EMBED_TITLE_LIMIT = 256
EMBED_DESCRIPTION_LIMIT = 4096
EMBED_FIELD_VALUE_LIMIT = 1024
EMBED_TOTAL_LIMIT = 6000
EMBEDS_PER_MESSAGE = 10


class NotifyError(Exception):
    pass


def env(name: str, default: str) -> str:
    return os.environ.get(name, default)


def env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return int(raw)


def env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return float(raw)


def env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def format_pct(value: float | None) -> str:
    if value is None:
        return "n/a"
    sign = "+" if value >= 0 else ""
    return f"{sign}{value:.1f}%"


def format_usd(value: float) -> str:
    """Compact dollars: $950, $410k, $1.2M, $3.4B."""
    for threshold, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "k")):
        # Promote values that would round up to "1000k" into the next unit.
        if abs(value) >= threshold * 0.9995:
            scaled = value / threshold
            return f"${scaled:.1f}{suffix}" if abs(scaled) < 10 else f"${scaled:.0f}{suffix}"
    return f"${value:,.0f}"


def clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def embed_size(embed: dict[str, Any]) -> int:
    size = len(embed.get("title", "")) + len(embed.get("description", ""))
    size += len(embed.get("footer", {}).get("text", ""))
    for field in embed.get("fields", []):
        size += len(field["name"]) + len(field["value"])
    return size


def fit_embeds(embeds: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep embeds in order within Discord's count and combined-size limits.

    Every card's core (title, description) is placed first; fields (research, sources) are then
    added in order while they fit, so heavy research on early cards never crowds out later cards.
    """
    bare = [{k: v for k, v in e.items() if k != "fields"} for e in embeds[:EMBEDS_PER_MESSAGE]]
    kept: list[dict[str, Any]] = []
    total = 0
    for embed in bare:
        size = embed_size(embed)
        if total + size > EMBED_TOTAL_LIMIT:
            break
        kept.append(embed)
        total += size

    for embed, original in zip(kept, embeds):
        fields = original.get("fields")
        if not fields:
            continue
        extra = sum(len(f["name"]) + len(f["value"]) for f in fields)
        if total + extra <= EMBED_TOTAL_LIMIT:
            embed["fields"] = fields
            total += extra
    return kept


async def send_discord(client: httpx.AsyncClient, payload: dict[str, Any]) -> None:
    webhook_url = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    if not webhook_url:
        raise NotifyError("DISCORD_WEBHOOK_URL is not configured")

    response = await client.post(webhook_url, json=payload)
    if response.status_code >= 400:
        raise NotifyError(f"Discord webhook failed: {response.status_code} {response.text}")
