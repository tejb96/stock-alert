#!/usr/bin/env python3
"""Market-hours SEC watcher: posts only when insiders buy, or a watched ticker files a notable 8-K.

Runs every ~20 minutes. Each run scans EDGAR's latest-filings feed back to the previous run,
so nothing is missed between runs and nothing is posted twice.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
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
    env_float,
    env_int,
    fit_embeds,
    format_pct,
    format_usd,
    send_discord,
)
from market import (
    MARKET_TZ,
    YAHOO_QUOTE_PAGE_URL,
    StockQuote,
    fetch_stock_quotes,
    format_price_line,
    is_tradeable,
)
from sec import (
    ISSUER_BDC,
    ISSUER_FUND,
    FeedEntry,
    Form4,
    SecActivity,
    SecClient,
    describe_items,
    filing_index_url,
    is_notable_8k,
    ticker_activity,
)
from state import (
    DIGEST_STATE_FILE,
    SEC_STATE_FILE,
    iso,
    load_json,
    parse_iso,
    save_json,
)

FEED_PAGE_SIZE = 100
# Each Form 4 is listed once for the issuer and once per reporting owner, so a busy day fills
# ~2,000 feed entries. GitHub often skips scheduled runs for hours, so page back far enough to reach
# the previous run; EDGAR serves the feed ~5,000 entries (2–3 trading days) deep.
MAX_FEED_PAGES = 50
FIRST_RUN_LOOKBACK = timedelta(hours=1)
# Covers a weekend plus a skipped Monday run.
MAX_LOOKBACK = timedelta(days=4)
# Re-scan a little before the last check in case EDGAR published entries slightly out of order.
LOOKBACK_OVERLAP = timedelta(minutes=10)
# Only needs to outlive MAX_LOOKBACK; older accessions can never re-enter the scan window.
SEEN_RETENTION = timedelta(days=1)
# A trade reported more than this long after it happened is flagged as stale.
LATE_FILING_DAYS = 5
# Trades older than this aren't news any more, however late they were reported.
DEFAULT_MAX_TRADE_AGE_DAYS = 30
# An insider price this far outside the stock's 52-week range is in another currency or share class
# (e.g. Bradesco directors buying São Paulo shares in reais), so neither the value nor the
# comparison with the US quote holds.
PRICE_RANGE_SLACK = (0.8, 1.25)
# Non-traded funds report placeholder symbols.
TICKER_RE = re.compile(r"^[A-Z]{1,5}([.-][A-Z]{1,2})?$")
PLACEHOLDER_TICKERS = frozenset({"NONE", "NA", "N/A", "NULL"})
WATCH_DIGEST_DAYS = 7
WATCH_INSIDER_DAYS = 30

INSIDER_COLOR = 0x9B59B6
EIGHT_K_COLOR = 0x7F8C8D


@dataclass(frozen=True)
class InsiderAlert:
    ticker: str
    issuer_name: str
    filings: list[Form4]
    activity: SecActivity | None
    quote: StockQuote | None
    reddit: tuple[int, int] | None

    @property
    def new_value(self) -> float:
        return sum(f.buy_value for f in self.filings)

    @property
    def insider_price(self) -> float | None:
        """Share-weighted average price of the new purchases."""
        shares = sum(f.buy_shares for f in self.filings)
        return self.new_value / shares if shares else None

    @property
    def latest_trade(self) -> str | None:
        return max((f.last_buy_date for f in self.filings if f.last_buy_date), default=None)


def feed_window_start(last_check: str | None, now: datetime) -> datetime:
    if not last_check:
        return now - FIRST_RUN_LOOKBACK
    try:
        start = parse_iso(last_check) - LOOKBACK_OVERLAP
    except ValueError:
        return now - FIRST_RUN_LOOKBACK
    return max(start, now - MAX_LOOKBACK)


def new_entries(
    entries: list[FeedEntry],
    *,
    since: datetime,
    seen: set[str],
    role: str,
    form: str,
) -> list[FeedEntry]:
    """One entry per unseen accession within the window, for the given filer role.

    Amendments (4/A, 8-K/A) are skipped so a corrected filing doesn't re-announce the original.
    """
    picked: dict[str, FeedEntry] = {}
    for entry in entries:
        if entry.form != form or entry.role != role or entry.updated < since or entry.accession in seen:
            continue
        picked.setdefault(entry.accession, entry)
    return list(picked.values())


def is_listed_ticker(ticker: str) -> bool:
    return ticker not in PLACEHOLDER_TICKERS and bool(TICKER_RE.match(ticker))


def qualifying_buys(form4s: list[Form4], *, min_value: float) -> list[Form4]:
    return [f for f in form4s if f.buy_value >= min_value and f.is_insider and is_listed_ticker(f.ticker)]


def price_mismatch(insider_price: float | None, quote: StockQuote | None) -> bool:
    """The insider's price can't be a price of this listing: another currency or share class."""
    if insider_price is None or quote is None:
        return False
    low, high = quote.fifty_two_week_low, quote.fifty_two_week_high
    if not low or not high:
        return False
    return not PRICE_RANGE_SLACK[0] * low <= insider_price <= PRICE_RANGE_SLACK[1] * high


def trade_age_days(alert: InsiderAlert, today: date) -> int | None:
    try:
        return (today - date.fromisoformat(alert.latest_trade)).days if alert.latest_trade else None
    except ValueError:
        return None


def skip_reason(
    alert: InsiderAlert,
    *,
    today: date,
    alert_value: float,
    min_price: float,
    max_trade_age_days: int,
) -> str | None:
    """Why this purchase isn't worth an alert, or None to post it.

    Posts buys big enough on their own or part of a multi-insider cluster, and never IPO
    allocations, pennies, funds, stale trades, or prices that don't belong to this listing."""
    quote, activity = alert.quote, alert.activity
    if quote is not None:
        if not is_tradeable(quote, min_price=min_price):
            return "penny stock or fund"
        # Under a week of trading history: insiders "buying" at the offering price, not in the market.
        if quote.change_5d is None:
            return "IPO allocation"
    if activity is not None and activity.kind == ISSUER_FUND:
        return "closed-end fund"
    if price_mismatch(alert.insider_price, quote):
        return "price in another currency or share class"
    age = trade_age_days(alert, today)
    if age is not None and age > max_trade_age_days:
        return f"trade {age}d old"
    is_cluster = activity is not None and activity.buyer_count >= 2
    if alert.new_value < alert_value and not is_cluster:
        return "too small"
    return None


def filing_lag_days(filing: Form4) -> int | None:
    if not filing.last_buy_date:
        return None
    try:
        return (date.fromisoformat(filing.filed) - date.fromisoformat(filing.last_buy_date)).days
    except ValueError:
        return None


def watched_tickers(digest_state: dict[str, Any], sec_state: dict[str, Any], *, now: datetime) -> dict[str, str]:
    """Ticker → why we're watching it."""
    watched: dict[str, str] = {}
    digest_cutoff = now - timedelta(days=WATCH_DIGEST_DAYS)
    for alert in digest_state.get("alerts", []):
        # Only picks actually posted: tracked-but-silent picks (mostly mega-caps) aren't on your radar.
        if not alert.get("alerted"):
            continue
        ts = parse_iso(alert["ts"])
        if ts >= digest_cutoff:
            watched[alert["ticker"]] = f"Reddit alert {ts.strftime('%b %-d')}"
    insider_cutoff = now - timedelta(days=WATCH_INSIDER_DAYS)
    for ticker, raw in sec_state.get("insider_tickers", {}).items():
        ts = parse_iso(raw)
        if ts >= insider_cutoff:
            watched.setdefault(ticker, f"insider buying alert {ts.strftime('%b %-d')}")
    return watched


def latest_reddit(digest_state: dict[str, Any]) -> dict[str, tuple[int, int]]:
    snapshots = digest_state.get("snapshots", [])
    if not snapshots:
        return {}
    return {t: (int(v[0]), int(v[1])) for t, v in snapshots[-1].get("rows", {}).items()}


def build_insider_embed(alert: InsiderAlert, *, today: date) -> dict[str, Any]:
    lines: list[str] = []
    for filing in sorted(alert.filings, key=lambda f: f.buy_value, reverse=True)[:5]:
        avg = f" @ ${filing.buy_avg_price:,.2f}" if filing.buy_avg_price else ""
        plan = " · 10b5-1 plan" if filing.planned else ""
        lag = filing_lag_days(filing)
        late = f" · ⚠️ filed {lag}d after trade" if lag is not None and lag > LATE_FILING_DAYS else ""
        lines.append(
            f"**{filing.owner}** ({filing.role}) bought **{format_usd(filing.buy_value)}** · "
            f"{filing.buy_shares:,.0f} sh{avg} · {filing.last_buy_date or filing.filed}{plan}{late}"
        )

    if alert.activity is not None and alert.activity.buyer_count > 1:
        lines.append(
            f"👥 **Cluster:** {alert.activity.buyer_count} insiders bought "
            f"{format_usd(alert.activity.buy_value)} in the last 30 days"
        )

    if alert.quote is not None:
        lines.append(format_price_line(alert.quote))
        if alert.insider_price:
            lines.append(f"↔️ Now {format_pct((alert.quote.price / alert.insider_price - 1) * 100)} vs insider price")

    if alert.reddit is not None:
        mentions, rank = alert.reddit
        lines.append(f"📈 Also on Reddit: {mentions} mentions (rank #{rank})")

    latest = max(alert.filings, key=lambda f: f.filed)
    kind = " · BDC" if alert.activity is not None and alert.activity.kind == ISSUER_BDC else ""
    return {
        "title": clip(f"🏛 Insider buying — {alert.ticker} ({alert.issuer_name}){kind}", EMBED_TITLE_LIMIT),
        "url": filing_index_url(latest.issuer_cik, latest.accession),
        "color": INSIDER_COLOR,
        "description": clip("\n".join(lines), EMBED_DESCRIPTION_LIMIT),
        "footer": {"text": f"Form 4 · open-market purchase · filed {_filed_when(latest.filed, today)}"},
    }


def _filed_when(filed: str, today: date) -> str:
    try:
        days = (today - date.fromisoformat(filed)).days
    except ValueError:
        return filed
    return "today" if days <= 0 else "yesterday" if days == 1 else f"{filed} ({days}d ago)"


def build_8k_embed(ticker: str, entries: list[FeedEntry], reason: str, quote: StockQuote | None) -> dict[str, Any]:
    """One card per ticker, however many 8-Ks it filed since the last run (newest first)."""
    entries = sorted(entries, key=lambda e: e.updated, reverse=True)
    latest = entries[0]
    lines = []
    for entry in entries:
        filed = f"{entry.updated:%b %-d %H:%M} UTC"
        if len(entries) == 1:
            lines.append(f"**{describe_items(entry.items)}**")
        else:
            lines.append(f"• [{describe_items(entry.items)}]({filing_index_url(entry.cik, entry.accession)}) · {filed}")
    if quote is not None:
        lines.append(format_price_line(quote, show_range=False))
    lines.append(f"Watching because: {reason} · [Yahoo]({YAHOO_QUOTE_PAGE_URL.format(symbol=ticker)})")
    plural = f"{len(entries)} 8-Ks" if len(entries) > 1 else "8-K"
    return {
        "title": clip(f"📄 {plural} — {ticker} ({latest.name})", EMBED_TITLE_LIMIT),
        "url": filing_index_url(latest.cik, latest.accession),
        "color": EIGHT_K_COLOR,
        "description": clip("\n".join(lines), EMBED_DESCRIPTION_LIMIT),
        "footer": {"text": f"Filed {latest.updated:%Y-%m-%d %H:%M} UTC"},
    }


async def scan_feed(sec: SecClient, form: str, *, since: datetime) -> list[FeedEntry]:
    entries: list[FeedEntry] = []
    for page in range(MAX_FEED_PAGES):
        try:
            batch = await sec.current_feed(form, start=page * FEED_PAGE_SIZE, count=FEED_PAGE_SIZE)
        except httpx.HTTPStatusError:
            # EDGAR stops serving the feed ~5,000 entries deep (503); keep what was read.
            if page == 0:
                raise
            break
        entries.extend(batch)
        if len(batch) < FEED_PAGE_SIZE or min(e.updated for e in batch) < since:
            return entries
    oldest = min((e.updated for e in entries), default=None)
    print(f"::warning::sec_check: Form {form} feed ends at {oldest}, before {iso(since)}; older filings were skipped.")
    return entries


async def find_insider_alerts(
    client: httpx.AsyncClient,
    sec: SecClient,
    entries: list[FeedEntry],
    *,
    today: date,
    min_value: float,
    alert_value: float,
    min_price: float,
    max_trade_age_days: int,
    reddit: dict[str, tuple[int, int]],
) -> list[InsiderAlert]:
    parsed = await asyncio.gather(
        *[sec.fetch_form4(e.cik, e.accession, e.updated.astimezone(MARKET_TZ).date().isoformat()) for e in entries],
        return_exceptions=True,
    )
    form4s = [p for p in parsed if isinstance(p, Form4)]
    failures = [p for p in parsed if isinstance(p, BaseException)]
    if failures:
        print(f"sec_check: {len(failures)} Form 4 fetches failed, first: {failures[0]}", file=sys.stderr)

    by_ticker: dict[str, list[Form4]] = {}
    for filing in qualifying_buys(form4s, min_value=min_value):
        by_ticker.setdefault(filing.ticker, []).append(filing)
    if not by_ticker:
        return []

    quotes = await fetch_stock_quotes(client, list(by_ticker))
    activities = await asyncio.gather(*[ticker_activity(sec, t, today=today) for t in by_ticker])

    alerts: list[InsiderAlert] = []
    for (ticker, filings), activity in zip(by_ticker.items(), activities):
        alert = InsiderAlert(ticker, filings[0].issuer_name, filings, activity, quotes.get(ticker), reddit.get(ticker))
        # Unknown to Yahoo is allowed (thin OTC names still matter); known pennies and funds are skipped.
        reason = skip_reason(
            alert, today=today, alert_value=alert_value, min_price=min_price, max_trade_age_days=max_trade_age_days
        )
        if reason is None:
            alerts.append(alert)
        elif reason != "too small":
            print(f"sec_check: skipped {ticker} ({format_usd(alert.new_value)}): {reason}")
    return sorted(alerts, key=lambda a: a.new_value, reverse=True)


async def run() -> None:
    contact_email = os.environ.get("SEC_CONTACT_EMAIL", "").strip()
    if not contact_email:
        print("sec_check: SEC_CONTACT_EMAIL not set, skipping.")
        return

    state_dir = Path(env("STATE_DIR", "state"))
    sec_path = state_dir / SEC_STATE_FILE
    sec_state = load_json(sec_path, {"seen": {}, "insider_tickers": {}})
    digest_state = load_json(state_dir / DIGEST_STATE_FILE, {})
    min_value = env_float("SEC_MIN_BUY_USD", 25_000)
    alert_value = env_float("SEC_ALERT_USD", 100_000)
    min_price = env_float("MIN_PRICE", 1.0)
    max_trade_age_days = env_int("SEC_MAX_TRADE_AGE_DAYS", DEFAULT_MAX_TRADE_AGE_DAYS)

    now = datetime.now(UTC)
    since = feed_window_start(sec_state.get("last_check"), now)
    seen: dict[str, str] = dict(sec_state.get("seen", {}))
    insider_tickers: dict[str, str] = dict(sec_state.get("insider_tickers", {}))
    watched = watched_tickers(digest_state, sec_state, now=now)

    user_agent = env("YAHOO_USER_AGENT", "stock_alert-cron/1.0")
    async with httpx.AsyncClient(timeout=20.0, headers={"User-Agent": user_agent}) as client:
        sec = SecClient(client, contact_email)

        form4_entries = new_entries(await scan_feed(sec, "4", since=since), since=since, seen=set(seen), role="Issuer", form="4")
        insider_alerts = await find_insider_alerts(
            client,
            sec,
            form4_entries,
            today=now.astimezone(MARKET_TZ).date(),
            min_value=min_value,
            alert_value=alert_value,
            min_price=min_price,
            max_trade_age_days=max_trade_age_days,
            reddit=latest_reddit(digest_state),
        )

        eight_k_embeds: list[dict[str, Any]] = []
        eight_k_entries: list[FeedEntry] = []
        if watched:
            ticker_map = await sec.ticker_map()
            cik_to_ticker = {cik: t for t, (cik, _) in ticker_map.items() if t in watched}
            eight_k_entries = new_entries(
                await scan_feed(sec, "8-K", since=since), since=since, seen=set(seen), role="Filer", form="8-K"
            )
            by_ticker: dict[str, list[FeedEntry]] = {}
            for entry in eight_k_entries:
                ticker = cik_to_ticker.get(entry.cik)
                if ticker and is_notable_8k(entry.items):
                    by_ticker.setdefault(ticker, []).append(entry)
            quotes = await fetch_stock_quotes(client, list(by_ticker)) if by_ticker else {}
            eight_k_embeds = [
                build_8k_embed(ticker, entries, watched[ticker], quotes.get(ticker))
                for ticker, entries in by_ticker.items()
            ]

        embeds = [build_insider_embed(a, today=now.astimezone(MARKET_TZ).date()) for a in insider_alerts] + eight_k_embeds
        for start in range(0, len(embeds), EMBEDS_PER_MESSAGE):
            payload = {"embeds": fit_embeds(embeds[start : start + EMBEDS_PER_MESSAGE])}
            if env_bool("DRY_RUN", False):
                print(json.dumps(payload, indent=1, ensure_ascii=False))
            else:
                await send_discord(client, payload)

    if env_bool("DRY_RUN", False):
        print(f"sec_check: dry run, {len(form4_entries)} Form 4s and {len(eight_k_entries)} 8-Ks since {iso(since)}.")
        return

    stamp = iso(now)
    for entry in [*form4_entries, *eight_k_entries]:
        seen[entry.accession] = stamp
    for alert in insider_alerts:
        insider_tickers[alert.ticker] = stamp
    seen_cutoff = now - SEEN_RETENTION
    save_json(
        sec_path,
        {
            "last_check": stamp,
            "seen": {acc: ts for acc, ts in seen.items() if parse_iso(ts) >= seen_cutoff},
            "insider_tickers": {
                t: ts for t, ts in insider_tickers.items() if parse_iso(ts) >= now - timedelta(days=WATCH_INSIDER_DAYS)
            },
        },
    )
    print(
        f"sec_check: {len(form4_entries)} Form 4s, {len(eight_k_entries)} 8-Ks since {iso(since)}; "
        f"posted {len(insider_alerts)} insider and {len(eight_k_embeds)} 8-K alerts."
    )


def main() -> int:
    try:
        asyncio.run(run())
    except NotifyError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except httpx.HTTPError as exc:
        print(f"http error: {exc!r}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
