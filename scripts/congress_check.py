#!/usr/bin/env python3
"""Daily House trade watcher: posts only purchases that score as worth a look.

Each purchase in a newly filed Periodic Transaction Report is scored (congress_signals.py):
leadership or committee chair, committee overlap with the company's sector, size, call options
and how long-dated they are, other members buying the same stock, and how quickly it was filed.
Trades scoring CONGRESS_MIN_SCORE or more are posted. Members in CONGRESS_WATCH have every
stock/option trade posted regardless.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

from common import (
    EMBED_DESCRIPTION_LIMIT,
    EMBED_TITLE_LIMIT,
    EMBEDS_PER_MESSAGE,
    NotifyError,
    clip,
    env,
    env_bool,
    env_int,
    fit_embeds,
    format_pct,
    format_usd,
    send_discord,
)
from congress import (
    KIND_BUY,
    KIND_EXCHANGE,
    KIND_SELL,
    CongressTrade,
    PtrFiling,
    close_on,
    fetch_close_history,
    fetch_house_index,
    fetch_ptr_trades,
    merge_fills,
)
from congress_signals import (
    MemberProfile,
    ScoredTrade,
    cluster_members,
    fetch_profiles,
    record_buys,
    score_trade,
    sector_for_sic,
)
from sec import SecClient
from state import CONGRESS_STATE_FILE, load_json, save_json

# Filings older than this are never posted, so a first run doesn't flood the channel.
LOOKBACK_DAYS = 10
# With no purchase history yet, read this far back so clusters can be spotted from day one.
SEED_DAYS = 45
# Seen IDs only need to outlive the seed window.
SEEN_RETENTION_DAYS = 60
FETCH_CONCURRENCY = 4
TRADES_PER_CARD = 6
REPORTING_DEADLINE_DAYS = 45

BUY_COLOR = 0x2ECC71
SELL_COLOR = 0xE74C3C
MIXED_COLOR = 0xF1C40F


@dataclass(frozen=True)
class FilingAlert:
    filing: PtrFiling
    trades: list[ScoredTrade]
    profile: MemberProfile | None
    watched: bool

    @property
    def score(self) -> int:
        return max((t.score for t in self.trades), default=0)


def csv_names(raw: str) -> list[str]:
    return [n.strip().lower() for n in raw.split(",") if n.strip()]


def is_watched(member: str, watch: list[str]) -> bool:
    lowered = member.lower()
    return any(name in lowered for name in watch)


def pick_trades(scored: list[ScoredTrade], *, watched: bool, min_score: int) -> list[ScoredTrade]:
    picked: list[ScoredTrade] = []
    for item in scored:
        trade = item.trade
        if trade.kind == KIND_EXCHANGE or trade.amended:
            continue  # Merger share swaps and corrections of old filings aren't new decisions.
        if watched or item.score >= min_score:
            picked.append(item)
    return sorted(picked, key=lambda t: (t.score, t.trade.amount_low), reverse=True)


def pending_filings(filings: list[PtrFiling], seen: dict[str, str], *, today: date, days: int) -> list[PtrFiling]:
    cutoff = today - timedelta(days=days)
    return [f for f in filings if f.doc_id not in seen and f.filed >= cutoff and f.electronic]


def prune_seen(seen: dict[str, str], *, today: date) -> dict[str, str]:
    cutoff = (today - timedelta(days=SEEN_RETENTION_DAYS)).isoformat()
    return {doc_id: filed for doc_id, filed in seen.items() if filed >= cutoff}


def format_amount(trade: CongressTrade) -> str:
    if trade.amount_high is None:
        return f"over {format_usd(trade.amount_low - 1)}"
    return f"{format_usd(trade.amount_low)}–{format_usd(trade.amount_high)}"


def format_trade_line(item: ScoredTrade, *, since_trade: float | None) -> str:
    trade = item.trade
    verb = {KIND_BUY: "🟢 BUY", KIND_SELL: "🔴 SELL"}[trade.kind]
    if trade.partial:
        verb += " (part)"
    what = f"{trade.ticker} {trade.option_side}" if trade.is_option else trade.ticker
    when = f"{trade.traded:%b} {trade.traded.day}"
    if trade.fills > 1:
        when = f"{trade.fills} trades from {when}"
    parts = [f"{verb} **{what}** {format_amount(trade)}", when]
    if trade.owner != "self":
        parts.append(trade.owner)
    if since_trade is not None:
        parts.append(f"stock {format_pct(since_trade)} since")
    line = " · ".join(parts)
    if item.score:
        line = f"`{item.score:>2}` {line}"
    reasons = [s.label for s in item.signals if s.points > 0 and s.label != "purchase"]
    reasons += [s.label for s in item.signals if s.points < 0]
    if reasons:
        line += f"\n  ↳ {' · '.join(reasons)}"
    if trade.is_option and trade.description:
        line += f"\n  ↳ {clip(trade.description, 140)}"
    return line


def filing_lag_days(alert: FilingAlert) -> int:
    return max((alert.filing.filed - min(t.trade.traded for t in alert.trades)).days, 0)


def build_filing_embed(alert: FilingAlert, returns: dict[tuple[str, date], float]) -> dict[str, Any]:
    items = alert.trades
    lines = [format_trade_line(t, since_trade=returns.get((t.trade.ticker, t.trade.traded))) for t in items[:TRADES_PER_CARD]]
    if len(items) > TRADES_PER_CARD:
        lines.append(f"…and {len(items) - TRADES_PER_CARD} more in the filing")

    kinds = {t.trade.kind for t in items}
    color = BUY_COLOR if kinds == {KIND_BUY} else SELL_COLOR if kinds == {KIND_SELL} else MIXED_COLOR
    lag = filing_lag_days(alert)
    late = f" ⚠️ past the {REPORTING_DEADLINE_DAYS}-day deadline" if lag > REPORTING_DEADLINE_DAYS else ""
    district = alert.filing.district
    if alert.profile is not None:
        district = f"{alert.profile.party_letter}-{district}"
    title = f"{'⭐ ' if alert.watched else ''}🏛 Rep. {alert.filing.member} ({district})"
    role = alert.profile.top_role if alert.profile is not None else None
    if role:
        title += f" · {role}"
    return {
        "title": clip(title, EMBED_TITLE_LIMIT),
        "url": alert.filing.url,
        "color": color,
        "description": clip("\n".join(lines), EMBED_DESCRIPTION_LIMIT),
        "footer": {"text": f"Filed {alert.filing.filed:%b %-d} · {lag} days after the earliest trade{late}"},
    }


def build_payload(alerts: list[FilingAlert], returns: dict[tuple[str, date], float], *, when: datetime) -> dict[str, Any]:
    ranked = sorted(alerts, key=lambda a: (a.score, a.watched), reverse=True)
    shown = ranked[:EMBEDS_PER_MESSAGE]
    trade_count = sum(len(a.trades) for a in alerts)
    header = (
        f"**Congress trades** · {when:%a %b %-d} — {trade_count} trade{'s' if trade_count != 1 else ''} "
        f"worth a look in {len(alerts)} new House filing{'s' if len(alerts) != 1 else ''}"
    )
    if len(ranked) > len(shown):
        header += f" (top {len(shown)} shown)"
    header += (
        "\nScore = leadership/chair, committee overlap, size, call options, cluster buying, filing speed. "
        "Amounts are reporting ranges; \"since\" is the move you missed while it went unreported."
    )
    return {"content": header, "embeds": fit_embeds([build_filing_embed(a, returns) for a in shown])}


async def returns_since_trade(client: httpx.AsyncClient, trades: list[CongressTrade]) -> dict[tuple[str, date], float]:
    symbols = sorted({t.yahoo_symbol for t in trades})
    histories = dict(zip(symbols, await asyncio.gather(*[fetch_close_history(client, s) for s in symbols])))
    returns: dict[tuple[str, date], float] = {}
    for trade in trades:
        history = histories.get(trade.yahoo_symbol) or []
        then = close_on(history, trade.traded)
        if then and history:
            returns[(trade.ticker, trade.traded)] = (history[-1][1] / then - 1) * 100
    return returns


async def sectors_for(client: httpx.AsyncClient, tickers: set[str]) -> dict[str, str | None]:
    contact_email = os.environ.get("SEC_CONTACT_EMAIL", "").strip()
    if not contact_email:
        print("congress: SEC_CONTACT_EMAIL not set; skipping committee-overlap scoring", file=sys.stderr)
        return {}
    sec = SecClient(client, contact_email)
    ordered = sorted(tickers)
    sics = await asyncio.gather(*[sec.sic_for(t) for t in ordered])
    return {t: sector_for_sic(sic) for t, sic in zip(ordered, sics)}


async def run() -> None:
    watch = csv_names(env("CONGRESS_WATCH", ""))
    min_score = env_int("CONGRESS_MIN_SCORE", 6)
    lookback_days = env_int("CONGRESS_LOOKBACK_DAYS", LOOKBACK_DAYS)
    state_path = Path(env("STATE_DIR", "state")) / CONGRESS_STATE_FILE
    user_agent = env("YAHOO_USER_AGENT", "stock_alert-cron/1.0")

    now = datetime.now(UTC)
    today = now.date()
    state = load_json(state_path, {})
    seen: dict[str, str] = dict(state.get("seen") or {})
    recent_buys: list[dict[str, Any]] = list(state.get("buys") or [])
    read_days = lookback_days if "buys" in state else max(SEED_DAYS, lookback_days)

    async with httpx.AsyncClient(timeout=20.0, headers={"User-Agent": user_agent}) as client:
        years = [today.year - 1, today.year] if today.month == 1 else [today.year]
        filings = [f for year in years for f in await fetch_house_index(client, year)]
        if not filings:
            raise NotifyError("House disclosure index had no PTR filings")
        pending = pending_filings(filings, seen, today=today, days=read_days)

        gate = asyncio.Semaphore(FETCH_CONCURRENCY)

        async def read(filing: PtrFiling) -> list[CongressTrade] | None:
            async with gate:
                return await fetch_ptr_trades(client, filing)

        parsed: list[tuple[PtrFiling, list[CongressTrade]]] = []
        for filing, trades in zip(pending, await asyncio.gather(*[read(f) for f in pending])):
            if trades is None:
                continue  # Retry next run.
            seen[filing.doc_id] = filing.filed.isoformat()
            recent_buys = record_buys(recent_buys, filing, trades, today=today)
            parsed.append((filing, merge_fills(trades)))

        post_cutoff = today - timedelta(days=lookback_days)
        postable = [(f, t) for f, t in parsed if f.filed >= post_cutoff]
        buy_tickers = {t.ticker for _, trades in postable for t in trades if t.kind == KIND_BUY}
        profiles, sectors = await asyncio.gather(fetch_profiles(client, today=today), sectors_for(client, buy_tickers))

        alerts: list[FilingAlert] = []
        for filing, trades in postable:
            profile = profiles.get(filing.district)
            scored = [
                score_trade(t, filing, profile, sectors.get(t.ticker), cluster_members(t.ticker, t.traded, recent_buys))
                for t in trades
            ]
            watched = is_watched(filing.member, watch)
            picked = pick_trades(scored, watched=watched, min_score=min_score)
            if picked:
                alerts.append(FilingAlert(filing, picked, profile, watched))

        if alerts:
            returns = await returns_since_trade(client, [t.trade for a in alerts for t in a.trades])
            payload = build_payload(alerts, returns, when=now)
            if env_bool("DRY_RUN", False):
                print(json.dumps(payload, indent=1, ensure_ascii=False))
            else:
                await send_discord(client, payload)

    save_json(state_path, {"seen": prune_seen(seen, today=today), "buys": recent_buys})
    print(f"congress_check: read {len(parsed)} filings, {len(postable)} new, {len(alerts)} worth posting.")


def main() -> int:
    try:
        asyncio.run(run())
    except NotifyError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except httpx.HTTPError as exc:
        print(f"http error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
