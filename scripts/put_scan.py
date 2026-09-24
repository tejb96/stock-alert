#!/usr/bin/env python3
"""Daily cash-secured put scan: the richest-priced, safest-looking puts on a watchlist → Discord.

Premium sellers earn the gap between implied volatility (what the put is priced for) and
realized volatility (what the stock actually does). Picks are ranked by annualized yield
weighted by that gap, after filtering for liquidity, distance to breakeven and earnings.
Anchor tickers (stocks you'd be happy to own, e.g. GME) get a strike ladder.

Alert-only: posts only when selling puts is unusually well paid — an anchor's implied volatility
is high against its own past year (IV rank) and a ladder strike pays a good annualized yield, or
a watchlist put passes every filter. Quiet days post nothing but still record IV history.
"""

from __future__ import annotations

import asyncio
import json
import sys
from dataclasses import dataclass, replace
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
    env_float,
    env_int,
    fit_embeds,
    format_usd,
    send_discord,
)
from market import YAHOO_QUOTE_PAGE_URL
from options import (
    Chain,
    History,
    PutCandidate,
    PutFilter,
    fetch_chain,
    fetch_earnings_date,
    fetch_history,
    iv_rank,
    put_candidates,
)
from state import OPTIONS_STATE_FILE, load_json, save_json

DEFAULT_ANCHORS = "GME"
# Liquid options, mostly established businesses or broad ETFs, strikes cheap enough to secure with cash.
# Only keep names you would genuinely hold if assigned.
DEFAULT_WATCHLIST = "KO,PFE,T,VZ,BAC,KMI,MO,PYPL,UBER,F,CMCSA,KHC,USB,NKE,SOFI,XLF,XLE,EEM,KRE,SLV"
FETCH_CONCURRENCY = 6
IV_HISTORY_DAYS = 365

# Strike ladder for anchor tickers: (label, emoji, min |delta|, max |delta|).
LADDER_BANDS = (
    ("Conservative", "🟢", 0.10, 0.18),
    ("Balanced", "🟡", 0.18, 0.25),
    ("Aggressive", "🟠", 0.25, 0.35),
)
LADDER_MAX_SPREAD_PCT = 50.0
PICK_COLOR = 0x2ECC71
ANCHOR_COLOR = 0x3498DB
MANAGEMENT_NOTE = "Close at ~50% profit · decide by 21 DTE · only sell puts on stocks you'd hold at breakeven"
# Until a year of IV history builds up there's no IV rank; fall back to implied vs realized volatility.
DEFAULT_ALERT_IV_RANK = 50.0
DEFAULT_ALERT_IV_EDGE = 1.3
DEFAULT_ALERT_MIN_ANNUALIZED = 30.0


@dataclass(frozen=True)
class TickerScan:
    chain: Chain
    history: History
    earnings: date | None
    iv_rank: float | None = None

    @property
    def ticker(self) -> str:
        return self.chain.ticker

    @property
    def below_trend(self) -> bool:
        return self.history.sma50 is not None and self.chain.price < self.history.sma50


def csv_tickers(raw: str) -> list[str]:
    return [t.strip().upper() for t in raw.split(",") if t.strip()]


def load_rules() -> PutFilter:
    return PutFilter(
        min_dte=env_int("CSP_MIN_DTE", 28),
        max_dte=env_int("CSP_MAX_DTE", 60),
        min_delta=env_float("CSP_MIN_DELTA", 0.15),
        max_delta=env_float("CSP_MAX_DELTA", 0.25),
        min_open_interest=env_int("CSP_MIN_OI", 50),
        min_bid=env_float("CSP_MIN_BID", 0.10),
        max_spread_pct=env_float("CSP_MAX_SPREAD_PCT", 25.0),
        max_capital=env_float("CSP_MAX_CAPITAL", 10_000),
        min_cushion_sigmas=env_float("CSP_MIN_CUSHION_SIGMAS", 0.8),
    )


def record_iv(state: dict[str, Any], scans: list[TickerScan], *, today: date) -> dict[str, Any]:
    """Store today's IV30 per ticker (one sample per day) and drop samples past the retention window."""
    cutoff = (today - timedelta(days=IV_HISTORY_DAYS)).isoformat()
    history: dict[str, dict[str, float]] = {
        ticker: {day: iv for day, iv in samples.items() if day >= cutoff}
        for ticker, samples in (state.get("iv30") or {}).items()
        if isinstance(samples, dict)
    }
    for scan in scans:
        if scan.chain.iv30:
            history.setdefault(scan.ticker, {})[today.isoformat()] = round(scan.chain.iv30, 4)
    return {"iv30": {t: s for t, s in history.items() if s}}


def with_iv_rank(scan: TickerScan, state: dict[str, Any]) -> TickerScan:
    if not scan.chain.iv30:
        return scan
    samples = list(((state.get("iv30") or {}).get(scan.ticker) or {}).values())
    return replace(scan, iv_rank=iv_rank(scan.chain.iv30, samples))


def best_picks(scans: list[TickerScan], rules: PutFilter, *, today: date, min_edge: float, top_n: int) -> list[PutCandidate]:
    """Best put per ticker, keeping only tickers whose puts are priced above their realized moves."""
    picks: list[PutCandidate] = []
    for scan in scans:
        candidates = put_candidates(scan.chain, scan.history, today=today, earnings=scan.earnings, rules=rules)
        if candidates and candidates[0].edge is not None and candidates[0].edge >= min_edge:
            picks.append(candidates[0])
    return sorted(picks, key=lambda c: c.score, reverse=True)[:top_n]


def ladder(scan: TickerScan, rules: PutFilter, *, today: date) -> list[tuple[str, str, float, float, PutCandidate | None]]:
    # You've chosen to own this stock, so show aggressive strikes too, whatever their cushion,
    # and tolerate wider quotes (flagged) rather than show nothing.
    relaxed = replace(rules, min_cushion_sigmas=0.0, max_spread_pct=max(rules.max_spread_pct, LADDER_MAX_SPREAD_PCT))
    rows = []
    for label, emoji, low, high in LADDER_BANDS:
        found = put_candidates(
            scan.chain, scan.history, today=today, earnings=scan.earnings, rules=relaxed, delta_range=(low, high)
        )
        rows.append((label, emoji, low, high, found[0] if found else None))
    return rows


def _day(value: date) -> str:
    return f"{value:%b} {value.day}"


def _strike(value: float) -> str:
    return f"${value:,.2f}".replace(".00", "")


def format_vol_line(scan: TickerScan, iv: float | None = None) -> str:
    iv = iv if iv is not None else scan.chain.iv30
    parts = [f"**${scan.chain.price:,.2f}**"]
    if iv:
        vol = f"IV {iv:.0%}"
        rv = scan.history.realized_vol
        if rv:
            vol += f" vs realized {rv:.0%} ({iv / rv:.1f}×)"
        parts.append(vol)
    if scan.iv_rank is not None:
        parts.append(f"IV rank {scan.iv_rank:.0f}")
    return "💵 " + " · ".join(parts)


def format_earnings_line(scan: TickerScan, *, today: date) -> str | None:
    if scan.history.is_fund:
        return None
    if scan.earnings is None or scan.earnings < today:
        return "📅 Next earnings date unknown — check before selling"
    return f"📅 Earnings {_day(scan.earnings)} ({(scan.earnings - today).days}d) — expiries after it are skipped"


def warning_lines(scan: TickerScan, pick: PutCandidate | None = None) -> list[str]:
    warnings = []
    if scan.below_trend:
        warnings.append("⚠️ Below its 50-day average — puts get assigned in downtrends")
    edge = pick.edge if pick else None
    if edge is not None and edge < 1:
        warnings.append("⚠️ Puts priced below the stock's real moves — thin edge, consider waiting")
    return warnings


def format_pick_lines(pick: PutCandidate) -> list[str]:
    o = pick.option
    lines = [
        (
            f"🎯 **${pick.premium:.2f}** mid (bid {o.bid:.2f} / ask {o.ask:.2f}) · Δ {abs(o.delta):.2f} · "
            f"{pick.dte} DTE · OI {o.open_interest:,}"
        ),
        f"💰 {pick.yield_pct:.1f}% on {format_usd(pick.capital)} cash · **{pick.annualized_pct:.0f}%/yr**",
    ]
    safety = f"🛡 Breakeven ${pick.breakeven:,.2f} ({pick.cushion_pct:.1f}% below)"
    if pick.cushion_sigmas is not None:
        safety += f" · {pick.cushion_sigmas:.1f} typical moves away"
    if pick.prob_profit is not None:
        safety += f" · ~{pick.prob_profit:.0%} chance of profit"
    lines.append(safety)
    return lines


def build_pick_embed(index: int, pick: PutCandidate, scan: TickerScan, *, today: date) -> dict[str, Any]:
    title = f"{index}. {pick.ticker} — sell {_day(pick.option.expiry)} {_strike(pick.option.strike)} put"
    lines = [
        format_vol_line(scan, pick.option.iv),
        *format_pick_lines(pick),
        format_earnings_line(scan, today=today),
        *warning_lines(scan, pick),
    ]
    lines = [line for line in lines if line]
    return {
        "title": clip(title, EMBED_TITLE_LIMIT),
        "url": YAHOO_QUOTE_PAGE_URL.format(symbol=pick.ticker) + "/options",
        "color": PICK_COLOR,
        "description": clip("\n".join(lines), EMBED_DESCRIPTION_LIMIT),
        "footer": {"text": f"Score {pick.score:.0f} · {pick.option.symbol}"},
    }


def format_ladder_row(pick: PutCandidate, *, max_spread_pct: float) -> str:
    text = (
        f"Sell **{_day(pick.option.expiry)} {_strike(pick.option.strike)}** put @ ${pick.premium:.2f} "
        f"(Δ {abs(pick.option.delta):.2f}, {pick.dte}d)\n"
        f"{pick.yield_pct:.1f}% · {pick.annualized_pct:.0f}%/yr on {format_usd(pick.capital)} · "
        f"BE ${pick.breakeven:,.2f} (−{pick.cushion_pct:.1f}%)"
    )
    if pick.prob_profit is not None:
        text += f" · ~{pick.prob_profit:.0%} profit"
    if pick.option.spread_pct > max_spread_pct:
        text += f"\n⚠️ Wide quote (bid {pick.option.bid:.2f} / ask {pick.option.ask:.2f}) — limit order near mid"
    return text


def build_anchor_embed(scan: TickerScan, rules: PutFilter, *, today: date) -> dict[str, Any]:
    rows = ladder(scan, rules, today=today)
    picks = [row[4] for row in rows if row[4] is not None]
    lines = [format_vol_line(scan), format_earnings_line(scan, today=today), *warning_lines(scan, picks[0] if picks else None)]
    lines = [line for line in lines if line]
    fields = [
        {
            "name": f"{emoji} {label} (Δ {low:.2f}–{high:.2f})",
            "value": format_ladder_row(pick, max_spread_pct=rules.max_spread_pct) if pick else "Nothing liquid in this range right now",
            "inline": False,
        }
        for label, emoji, low, high, pick in rows
    ]
    return {
        "title": clip(f"⚓ {scan.ticker} — strike ladder ({rules.min_dte}–{rules.max_dte} DTE)", EMBED_TITLE_LIMIT),
        "url": YAHOO_QUOTE_PAGE_URL.format(symbol=scan.ticker) + "/options",
        "color": ANCHOR_COLOR,
        "description": clip("\n".join(lines), EMBED_DESCRIPTION_LIMIT),
        "fields": fields,
        "footer": {"text": "A stock you're happy to own: if assigned, sell covered calls above your breakeven (the wheel)"},
    }


def rich_reason(
    scan: TickerScan,
    rules: PutFilter,
    *,
    today: date,
    min_iv_rank: float,
    min_iv_edge: float,
    min_annualized: float,
) -> str | None:
    """Why this anchor's puts are worth selling today, or None if premiums are ordinary."""
    iv, rv = scan.chain.iv30, scan.history.realized_vol
    if scan.iv_rank is not None:
        if scan.iv_rank < min_iv_rank:
            return None
        why = f"IV rank {scan.iv_rank:.0f}"
    elif iv and rv and iv / rv >= min_iv_edge:
        why = f"IV {iv:.0%} vs realized {rv:.0%} ({iv / rv:.1f}×)"
    else:
        return None
    best = max(
        (row[4] for row in ladder(scan, rules, today=today) if row[4] is not None),
        key=lambda p: p.annualized_pct,
        default=None,
    )
    if best is None or best.annualized_pct < min_annualized:
        return None
    return f"{scan.ticker}: {why}, up to {best.annualized_pct:.0f}%/yr"


def build_payload(
    picks: list[PutCandidate],
    anchors: list[TickerScan],
    scans: dict[str, TickerScan],
    rules: PutFilter,
    *,
    when: datetime,
    reasons: list[str] | None = None,
) -> dict[str, Any]:
    today = when.date()
    header = f"**Put scan** · {when:%Y-%m-%d %H:%M} UTC\n"
    if reasons:
        header += f"💰 Premiums are rich — {'; '.join(reasons)}\n"
    header += (
        f"Cash-secured puts, {rules.min_dte}–{rules.max_dte} DTE, Δ {rules.min_delta:.2f}–{rules.max_delta:.2f}, "
        f"no earnings before expiry. {MANAGEMENT_NOTE}."
    )
    embeds = [build_pick_embed(i, p, scans[p.ticker], today=today) for i, p in enumerate(picks, start=1)]
    embeds += [build_anchor_embed(a, rules, today=today) for a in anchors]
    return {"content": header, "embeds": fit_embeds(embeds[:EMBEDS_PER_MESSAGE])}


async def scan_ticker(client: httpx.AsyncClient, ticker: str, gate: asyncio.Semaphore) -> TickerScan | None:
    async with gate:
        chain = await fetch_chain(client, ticker)
        if chain is None:
            print(f"put_scan: no option chain for {ticker}", file=sys.stderr)
            return None
        history, earnings = await asyncio.gather(fetch_history(client, ticker), fetch_earnings_date(client, ticker))
    return TickerScan(chain, history, earnings)


async def run() -> None:
    anchors = csv_tickers(env("CSP_ANCHORS", DEFAULT_ANCHORS))
    watchlist = csv_tickers(env("CSP_WATCHLIST", DEFAULT_WATCHLIST))
    top_n = env_int("CSP_TOP_N", 5)
    min_edge = env_float("CSP_MIN_EDGE", 1.1)
    rules = load_rules()
    state_path = Path(env("STATE_DIR", "state")) / OPTIONS_STATE_FILE
    user_agent = env("YAHOO_USER_AGENT", "stock_alert-cron/1.0")

    now = datetime.now(UTC)
    tickers = list(dict.fromkeys([*anchors, *watchlist]))
    gate = asyncio.Semaphore(FETCH_CONCURRENCY)
    async with httpx.AsyncClient(timeout=20.0, headers={"User-Agent": user_agent}) as client:
        fetched = [s for s in await asyncio.gather(*[scan_ticker(client, t, gate) for t in tickers]) if s]
        if not fetched:
            raise NotifyError("no option chains could be fetched")

        state = record_iv(load_json(state_path, {}), fetched, today=now.date())
        scans = {s.ticker: with_iv_rank(s, state) for s in fetched}
        # Anchors get their own ladder, so they're not repeated as picks.
        watch_scans = [s for t, s in scans.items() if t not in anchors]
        picks = best_picks(watch_scans, rules, today=now.date(), min_edge=min_edge, top_n=top_n)
        today = now.date()
        reasons = {
            s.ticker: reason
            for s in (scans[t] for t in anchors if t in scans)
            if (
                reason := rich_reason(
                    s,
                    rules,
                    today=today,
                    min_iv_rank=env_float("CSP_ALERT_IV_RANK", DEFAULT_ALERT_IV_RANK),
                    min_iv_edge=env_float("CSP_ALERT_IV_EDGE", DEFAULT_ALERT_IV_EDGE),
                    min_annualized=env_float("CSP_ALERT_MIN_ANNUALIZED", DEFAULT_ALERT_MIN_ANNUALIZED),
                )
            )
        }
        anchor_scans = [scans[t] for t in anchors if t in reasons]
        if picks or anchor_scans:
            payload = build_payload(
                picks, anchor_scans, scans, rules, when=now, reasons=list(reasons.values())
            )
            if env_bool("DRY_RUN", False):
                print(json.dumps(payload, indent=1))
            else:
                await send_discord(client, payload)
        else:
            print("put_scan: premiums ordinary today; nothing posted.")

    save_json(state_path, state)
    print(f"put_scan: {len(fetched)}/{len(tickers)} chains, {len(picks)} picks, {len(anchor_scans)} anchors.")


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
