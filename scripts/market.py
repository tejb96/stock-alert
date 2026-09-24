"""Yahoo Finance price/volume context and the Nasdaq earnings calendar."""

from __future__ import annotations

import asyncio
import math
import statistics
import sys
import time
from dataclasses import dataclass
from datetime import date, timedelta
from itertools import pairwise
from typing import Any

import httpx

from common import NotifyError, format_pct

YAHOO_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
YAHOO_QUOTE_PAGE_URL = "https://finance.yahoo.com/quote/{symbol}"
NASDAQ_EARNINGS_URL = "https://api.nasdaq.com/api/calendar/earnings"
# Nasdaq's API rejects non-browser clients.
NASDAQ_HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36",
    "Accept": "application/json",
}

RELVOL_LOOKBACK_DAYS = 20
# Floor for the intraday volume projection so an early-session run isn't wildly extrapolated.
MIN_SESSION_FRACTION = 0.15

STAGE_EARLY = "early"
STAGE_MOVING = "moving"
STAGE_LATE = "late"

TREND_UP = "up"
TREND_DOWN = "down"
TREND_SIDEWAYS = "sideways"
TRENDING_DAYS_1M = 21
TRENDING_DAYS_3M = 63

# Moves are judged against each stock's own typical daily swing (stdev of daily % returns),
# so a 4% day counts as big for MCD but noise for a small cap.
MIN_VOLATILITY_SAMPLES = 10
# ~2 months of daily returns: recent enough to reflect the stock's current temperament.
VOLATILITY_LOOKBACK_DAYS = 42
MOVING_SIGMA = 1.5
LATE_SIGMA = 3.0
# Price already up this much → the move has likely happened (chasing), whatever the volatility.
LATE_CHANGE_1D = 15.0
LATE_CHANGE_5D = 30.0
# Fixed fallback when there isn't enough history to measure volatility.
MOVING_CHANGE_1D = 5.0
MOVING_CHANGE_5D = 10.0

EARNINGS_TIMING = {
    "time-pre-market": "before open",
    "time-after-hours": "after close",
}


@dataclass(frozen=True)
class StockQuote:
    ticker: str
    price: float
    change_1d: float | None
    change_5d: float | None
    rel_volume: float | None
    fifty_two_week_low: float | None
    fifty_two_week_high: float | None
    instrument_type: str | None = None
    daily_volatility: float | None = None
    change_1m: float | None = None
    change_3m: float | None = None
    sma50: float | None = None
    sma200: float | None = None

    @property
    def from_high_pct(self) -> float | None:
        if not self.fifty_two_week_high:
            return None
        return (self.price / self.fifty_two_week_high - 1) * 100

    def sigma_1d(self) -> float | None:
        if self.change_1d is None or not self.daily_volatility:
            return None
        return self.change_1d / self.daily_volatility

    def sigma_5d(self) -> float | None:
        if self.change_5d is None or not self.daily_volatility:
            return None
        return self.change_5d / (self.daily_volatility * math.sqrt(5))


@dataclass(frozen=True)
class EarningsEvent:
    ticker: str
    day: date
    timing: str | None


def _pct_change(new: float, old: float | None) -> float | None:
    if old is None or old <= 0:
        return None
    return (new / old - 1) * 100


def _session_fraction(meta: dict[str, Any], now: float) -> float | None:
    """Fraction of today's regular session elapsed, or None when outside it."""
    regular = meta.get("currentTradingPeriod", {}).get("regular", {})
    start = regular.get("start")
    end = regular.get("end")
    if not isinstance(start, (int, float)) or not isinstance(end, (int, float)) or end <= start:
        return None
    if not start <= now < end:
        return None
    return max((now - start) / (end - start), MIN_SESSION_FRACTION)


def extract_stock_quote(ticker: str, payload: dict[str, Any], *, now: float | None = None) -> StockQuote:
    result = payload.get("chart", {}).get("result")
    if not result:
        raise NotifyError(f"Yahoo chart response missing result for {ticker}")

    meta = result[0].get("meta", {})
    price = meta.get("regularMarketPrice")
    if price is None:
        price = meta.get("previousClose")
    if price is None:
        raise NotifyError(f"Yahoo chart response missing price for {ticker}")
    price = float(price)

    quote_series = (result[0].get("indicators", {}).get("quote") or [{}])[0]
    closes = [float(c) for c in quote_series.get("close") or [] if c is not None]
    # The last daily bar is the current (or most recent) session, which regularMarketVolume covers.
    prior_volumes = [float(v) for v in (quote_series.get("volume") or [])[:-1] if v]
    prior_volumes = prior_volumes[-RELVOL_LOOKBACK_DAYS:]

    change_1d = meta.get("regularMarketChangePercent")
    if change_1d is not None:
        change_1d = float(change_1d)
    elif len(closes) >= 2:
        change_1d = _pct_change(price, closes[-2])

    change_5d = _pct_change(price, closes[-6]) if len(closes) >= 6 else None

    # Exclude the current session so today's move doesn't inflate its own yardstick.
    history = closes[:-1][-VOLATILITY_LOOKBACK_DAYS:]
    returns = [(b / a - 1) * 100 for a, b in pairwise(history) if a > 0]
    daily_volatility = statistics.stdev(returns) if len(returns) >= MIN_VOLATILITY_SAMPLES else None

    rel_volume: float | None = None
    volume = meta.get("regularMarketVolume")
    if volume and prior_volumes:
        fraction = _session_fraction(meta, time.time() if now is None else now)
        projected = float(volume) / fraction if fraction else float(volume)
        rel_volume = projected / (sum(prior_volumes) / len(prior_volumes))

    low = meta.get("fiftyTwoWeekLow")
    high = meta.get("fiftyTwoWeekHigh")
    instrument_type = meta.get("instrumentType")

    return StockQuote(
        ticker=ticker,
        price=price,
        change_1d=change_1d,
        change_5d=change_5d,
        rel_volume=rel_volume,
        fifty_two_week_low=float(low) if low is not None else None,
        fifty_two_week_high=float(high) if high is not None else None,
        instrument_type=str(instrument_type) if instrument_type else None,
        daily_volatility=daily_volatility,
        change_1m=_pct_change(price, closes[-TRENDING_DAYS_1M - 1]) if len(closes) > TRENDING_DAYS_1M else None,
        change_3m=_pct_change(price, closes[-TRENDING_DAYS_3M - 1]) if len(closes) > TRENDING_DAYS_3M else None,
        sma50=_sma(closes, 50),
        sma200=_sma(closes, 200),
    )


def _sma(closes: list[float], days: int) -> float | None:
    return sum(closes[-days:]) / days if len(closes) >= days else None


def classify_stage(quote: StockQuote) -> str | None:
    """Has the price already reacted to the buzz?"""
    c1, c5 = quote.change_1d, quote.change_5d
    if c1 is None and c5 is None:
        return None
    if (c1 is not None and c1 >= LATE_CHANGE_1D) or (c5 is not None and c5 >= LATE_CHANGE_5D):
        return STAGE_LATE

    if quote.daily_volatility:
        z1, z5 = quote.sigma_1d(), quote.sigma_5d()
        if (z1 is not None and z1 >= LATE_SIGMA) or (z5 is not None and z5 >= LATE_SIGMA):
            return STAGE_LATE
        if (z1 is not None and abs(z1) >= MOVING_SIGMA) or (z5 is not None and abs(z5) >= MOVING_SIGMA):
            return STAGE_MOVING
        return STAGE_EARLY

    if (c1 is not None and abs(c1) >= MOVING_CHANGE_1D) or (c5 is not None and abs(c5) >= MOVING_CHANGE_5D):
        return STAGE_MOVING
    return STAGE_EARLY


def classify_trend(quote: StockQuote) -> str | None:
    """Where the stock sits vs its 50- and 200-day averages: the months-long picture the 5-day stage misses."""
    if quote.sma50 is None:
        return None
    above = [quote.price > quote.sma50]
    if quote.sma200 is not None:
        above.append(quote.price > quote.sma200)
    elif quote.change_3m is not None:
        above.append(quote.change_3m > 0)
    if all(above):
        return TREND_UP
    if not any(above):
        return TREND_DOWN
    return TREND_SIDEWAYS


TREND_STYLE = {
    TREND_UP: ("📈", "Uptrend"),
    TREND_DOWN: ("📉", "Downtrend"),
    TREND_SIDEWAYS: ("↔️", "Sideways"),
}


def format_trend_line(quote: StockQuote) -> str | None:
    trend = classify_trend(quote)
    parts = [f"{TREND_STYLE[trend][0]} **{TREND_STYLE[trend][1]}**"] if trend else []
    if quote.change_1m is not None:
        parts.append(f"1m {format_pct(quote.change_1m)}")
    if quote.change_3m is not None:
        parts.append(f"3m {format_pct(quote.change_3m)}")
    from_high = quote.from_high_pct
    if from_high is not None:
        parts.append("at 52w high" if from_high > -1 else f"{abs(from_high):.0f}% below 52w high")
    return " · ".join(parts) if parts else None


def format_price_line(quote: StockQuote, *, show_range: bool = True) -> str:
    day = f"1d {format_pct(quote.change_1d)}"
    z1 = quote.sigma_1d()
    if z1 is not None and abs(z1) >= 1:
        day += f" ({abs(z1):.1f}× usual)"
    parts = [f"**${quote.price:,.2f}**", day, f"5d {format_pct(quote.change_5d)}"]
    if quote.rel_volume is not None:
        parts.append(f"RelVol **{quote.rel_volume:.1f}×**")
    line = "💵 " + " · ".join(parts)
    if show_range and quote.fifty_two_week_low is not None and quote.fifty_two_week_high is not None:
        line += f"\n📊 52w ${quote.fifty_two_week_low:,.2f} – ${quote.fifty_two_week_high:,.2f}"
    return line


def is_tradeable(quote: StockQuote, *, min_price: float) -> bool:
    if quote.price < min_price:
        return False
    return quote.instrument_type in (None, "EQUITY")


async def fetch_stock_quote(client: httpx.AsyncClient, ticker: str) -> StockQuote | None:
    params = {"interval": "1d", "range": "1y"}
    try:
        response = await client.get(YAHOO_CHART_URL.format(symbol=ticker), params=params)
        response.raise_for_status()
        return extract_stock_quote(ticker, response.json())
    except (NotifyError, httpx.HTTPError, ValueError):
        return None


async def fetch_stock_quotes(client: httpx.AsyncClient, tickers: list[str]) -> dict[str, StockQuote]:
    fetched = await asyncio.gather(*[fetch_stock_quote(client, t) for t in dict.fromkeys(tickers)])
    return {q.ticker: q for q in fetched if q is not None}


def parse_nasdaq_earnings(payload: dict[str, Any], day: date) -> list[EarningsEvent]:
    rows = (payload.get("data") or {}).get("rows") or []
    events: list[EarningsEvent] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        symbol = row.get("symbol")
        if not symbol or not isinstance(symbol, str):
            continue
        events.append(EarningsEvent(symbol.strip().upper(), day, EARNINGS_TIMING.get(str(row.get("time")))))
    return events


async def fetch_earnings_calendar(
    client: httpx.AsyncClient,
    *,
    start: date,
    business_days: int = 10,
) -> dict[str, EarningsEvent]:
    """Next earnings date per ticker over the coming business days. Empty on any failure."""
    days: list[date] = []
    cursor = start
    while len(days) < business_days:
        if cursor.weekday() < 5:
            days.append(cursor)
        cursor += timedelta(days=1)

    async def fetch_day(day: date) -> list[EarningsEvent]:
        response = await client.get(
            NASDAQ_EARNINGS_URL,
            params={"date": day.isoformat()},
            headers=NASDAQ_HEADERS,
            timeout=15.0,
        )
        response.raise_for_status()
        return parse_nasdaq_earnings(response.json(), day)

    try:
        results = await asyncio.gather(*[fetch_day(day) for day in days])
    except (httpx.HTTPError, ValueError) as exc:
        print(f"earnings: calendar fetch failed: {exc}", file=sys.stderr)
        return {}

    calendar: dict[str, EarningsEvent] = {}
    for events in results:
        for event in events:
            calendar.setdefault(event.ticker, event)
    return calendar
