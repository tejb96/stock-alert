"""Option chains (CBOE delayed quotes), realized volatility, and cash-secured put metrics."""

from __future__ import annotations

import math
import re
import statistics
import sys
from dataclasses import dataclass, replace
from datetime import date, datetime
from typing import Any

import httpx

from common import NotifyError
from market import NASDAQ_HEADERS, YAHOO_CHART_URL

# Free, keyless, ~15 min delayed during the session; includes IV and greeks per contract.
CBOE_OPTIONS_URL = "https://cdn-api.cboe.com/api/global/delayed_quotes/options/{symbol}.json"
NASDAQ_EARNINGS_DATE_URL = "https://api.nasdaq.com/api/analyst/{symbol}/earnings-date"

# OSI symbol tail: YYMMDD, C/P, strike × 1000 in 8 digits (the root is the ticker).
OSI_RE = re.compile(r"(\d{6})([CP])(\d{8})$")
EARNINGS_DATE_RE = re.compile(r"([A-Z][a-z]{2} \d{1,2}, \d{4})")

TRADING_DAYS = 252
RV_SHORT_DAYS = 20
RV_LONG_DAYS = 60
TREND_SMA_DAYS = 50
# Premium this far above realized volatility earns no extra score; beyond it, high IV usually means news.
EDGE_CAP = 2.0
MIN_IV_HISTORY = 20


@dataclass(frozen=True)
class OptionQuote:
    symbol: str
    expiry: date
    kind: str  # "C" or "P"
    strike: float
    bid: float
    ask: float
    iv: float  # annualized, decimal
    delta: float
    open_interest: int
    volume: int

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2

    @property
    def spread_pct(self) -> float:
        return (self.ask - self.bid) / self.mid * 100 if self.mid > 0 else math.inf


@dataclass(frozen=True)
class Chain:
    ticker: str
    price: float
    iv30: float | None  # decimal
    options: list[OptionQuote]

    def puts(self) -> list[OptionQuote]:
        return [o for o in self.options if o.kind == "P"]


@dataclass(frozen=True)
class History:
    """Volatility and trend from daily closes."""

    realized_vol: float | None  # annualized decimal, the larger of the 20d and 60d measures
    sma50: float | None
    instrument_type: str | None = None

    @property
    def is_fund(self) -> bool:
        return self.instrument_type in ("ETF", "MUTUALFUND")


@dataclass(frozen=True)
class PutCandidate:
    ticker: str
    price: float
    option: OptionQuote
    dte: int
    realized_vol: float | None

    @property
    def premium(self) -> float:
        return self.option.mid

    @property
    def capital(self) -> float:
        return self.option.strike * 100

    @property
    def breakeven(self) -> float:
        return self.option.strike - self.premium

    @property
    def yield_pct(self) -> float:
        """Premium as a share of the cash set aside."""
        return self.premium / self.option.strike * 100

    @property
    def annualized_pct(self) -> float:
        return self.yield_pct * 365 / self.dte

    @property
    def cushion_pct(self) -> float:
        return (self.price - self.breakeven) / self.price * 100

    @property
    def years(self) -> float:
        return self.dte / 365

    @property
    def edge(self) -> float | None:
        """Implied vs realized volatility: > 1 means the put is priced for bigger moves than the stock makes."""
        if not self.realized_vol or self.option.iv <= 0:
            return None
        return self.option.iv / self.realized_vol

    @property
    def cushion_sigmas(self) -> float | None:
        """Distance to breakeven in typical (realized) moves over the option's life."""
        vol = self.realized_vol or self.option.iv
        if vol <= 0 or self.breakeven <= 0:
            return None
        return math.log(self.price / self.breakeven) / (vol * math.sqrt(self.years))

    @property
    def prob_profit(self) -> float | None:
        """Chance of finishing above breakeven, lognormal at the option's own IV (zero drift)."""
        sigma = self.option.iv
        if sigma <= 0 or self.breakeven <= 0:
            return None
        spread = sigma * math.sqrt(self.years)
        d2 = (math.log(self.price / self.breakeven) - 0.5 * spread**2) / spread
        return normal_cdf(d2)

    @property
    def score(self) -> float:
        """Annualized yield weighted by how richly the put is priced relative to the stock's real moves."""
        edge = self.edge if self.edge is not None else 1.0
        return self.annualized_pct * min(edge, EDGE_CAP)


@dataclass(frozen=True)
class PutFilter:
    min_dte: int = 28
    max_dte: int = 60
    min_delta: float = 0.15
    max_delta: float = 0.25
    min_open_interest: int = 50
    min_bid: float = 0.10
    max_spread_pct: float = 25.0
    max_capital: float = 10_000
    min_cushion_sigmas: float = 0.8


def normal_cdf(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def _float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def parse_osi(symbol: str) -> tuple[date, str, float] | None:
    match = OSI_RE.search(symbol)
    if not match:
        return None
    raw_date, kind, raw_strike = match.groups()
    try:
        expiry = datetime.strptime(raw_date, "%y%m%d").date()
    except ValueError:
        return None
    return expiry, kind, int(raw_strike) / 1000


def parse_cboe_chain(ticker: str, payload: dict[str, Any]) -> Chain:
    data = payload.get("data")
    if not isinstance(data, dict):
        raise NotifyError(f"CBOE response missing data for {ticker}")
    price = _float(data.get("current_price")) or _float(data.get("close"))
    if price <= 0:
        raise NotifyError(f"CBOE response missing price for {ticker}")

    options: list[OptionQuote] = []
    for row in data.get("options") or []:
        if not isinstance(row, dict):
            continue
        parsed = parse_osi(str(row.get("option", "")))
        if not parsed:
            continue
        expiry, kind, strike = parsed
        options.append(
            OptionQuote(
                symbol=str(row["option"]),
                expiry=expiry,
                kind=kind,
                strike=strike,
                bid=_float(row.get("bid")),
                ask=_float(row.get("ask")),
                iv=_float(row.get("iv")),
                delta=_float(row.get("delta")),
                open_interest=int(_float(row.get("open_interest"))),
                volume=int(_float(row.get("volume"))),
            )
        )

    return Chain(ticker, price, _float(data.get("iv30")) / 100 or None, options)


def compute_history(closes: list[float]) -> History:
    returns = [math.log(b / a) for a, b in zip(closes, closes[1:]) if a > 0 and b > 0]
    vols = [
        statistics.stdev(returns[-days:]) * math.sqrt(TRADING_DAYS)
        for days in (RV_SHORT_DAYS, RV_LONG_DAYS)
        if len(returns) >= days
    ]
    sma = statistics.fmean(closes[-TREND_SMA_DAYS:]) if len(closes) >= TREND_SMA_DAYS else None
    # The larger measure, so a recent calm stretch doesn't make every put look rich.
    return History(max(vols) if vols else None, sma)


def iv_rank(current: float, history: list[float]) -> float | None:
    """Where today's IV30 sits in its stored range, 0–100. None until there's enough history."""
    if len(history) < MIN_IV_HISTORY:
        return None
    low, high = min(history), max(history)
    if high <= low:
        return None
    return max(0.0, min(100.0, (current - low) / (high - low) * 100))


def parse_earnings_date(payload: dict[str, Any]) -> date | None:
    announcement = str((payload.get("data") or {}).get("announcement") or "")
    match = EARNINGS_DATE_RE.search(announcement)
    if not match:
        return None
    try:
        return datetime.strptime(match.group(1), "%b %d, %Y").date()
    except ValueError:
        return None


def put_candidates(
    chain: Chain,
    history: History,
    *,
    today: date,
    earnings: date | None,
    rules: PutFilter,
    delta_range: tuple[float, float] | None = None,
) -> list[PutCandidate]:
    """Puts that pass the liquidity, timing and risk rules, best score first.

    Expirations on or after the next earnings date are skipped: the premium looks rich only
    because the market expects a gap, which is exactly the risk a put seller wants to avoid.
    """
    low, high = delta_range or (rules.min_delta, rules.max_delta)
    picks: list[PutCandidate] = []
    for option in chain.puts():
        dte = (option.expiry - today).days
        if not rules.min_dte <= dte <= rules.max_dte:
            continue
        if earnings is not None and today <= earnings <= option.expiry:
            continue
        if option.strike >= chain.price or not low <= abs(option.delta) <= high:
            continue
        if option.bid < rules.min_bid or option.open_interest < rules.min_open_interest:
            continue
        if option.spread_pct > rules.max_spread_pct or option.strike * 100 > rules.max_capital:
            continue
        candidate = PutCandidate(chain.ticker, chain.price, option, dte, history.realized_vol)
        sigmas = candidate.cushion_sigmas
        if sigmas is None or sigmas < rules.min_cushion_sigmas:
            continue
        picks.append(candidate)
    return sorted(picks, key=lambda c: c.score, reverse=True)


async def fetch_chain(client: httpx.AsyncClient, ticker: str) -> Chain | None:
    try:
        response = await client.get(CBOE_OPTIONS_URL.format(symbol=ticker), timeout=30.0, follow_redirects=True)
        response.raise_for_status()
        return parse_cboe_chain(ticker, response.json())
    except (NotifyError, httpx.HTTPError, ValueError) as exc:
        print(f"options: CBOE chain for {ticker} failed: {exc}", file=sys.stderr)
        return None


async def fetch_history(client: httpx.AsyncClient, ticker: str) -> History:
    try:
        response = await client.get(YAHOO_CHART_URL.format(symbol=ticker), params={"interval": "1d", "range": "6mo"})
        response.raise_for_status()
        result = response.json()["chart"]["result"][0]
        closes = [float(c) for c in result["indicators"]["quote"][0]["close"] if c is not None]
        instrument_type = result["meta"].get("instrumentType")
    except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError):
        return History(None, None)
    return replace(compute_history(closes), instrument_type=instrument_type)


async def fetch_earnings_date(client: httpx.AsyncClient, ticker: str) -> date | None:
    try:
        response = await client.get(NASDAQ_EARNINGS_DATE_URL.format(symbol=ticker), headers=NASDAQ_HEADERS, timeout=15.0)
        response.raise_for_status()
        return parse_earnings_date(response.json())
    except (httpx.HTTPError, ValueError):
        return None
