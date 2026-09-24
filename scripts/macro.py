"""Market-wide crash alarms and bottom signals from free Yahoo and FRED daily data.

Bear signals flag the stresses behind past crashes: yen carry unwinds, volatility shocks, credit,
funding and bank stress, rate shocks. Bull signals flag the washouts and recoveries that marked
bottoms: volatility capitulation, fear normalizing, credit healing, the yen stabilizing.

Every signal is a pure function of the history up to a given day, so the same code runs live
(where the last bar is the session in progress) and in the backtest.
"""

from __future__ import annotations

import asyncio
import bisect
import csv
import io
import math
import statistics
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

import httpx

from common import format_pct
from market import YAHOO_CHART_URL

FRED_CSV_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv"

YAHOO_SERIES = {
    "USDJPY": "USDJPY=X",
    "AUDJPY": "AUDJPY=X",
    "VIX": "^VIX",
    "VIX9D": "^VIX9D",
    "VIX3M": "^VIX3M",
    "MOVE": "^MOVE",
    "HYG": "HYG",
    "IEF": "IEF",
    "KRE": "KRE",
    "SPY": "SPY",
}
FRED_SERIES = {"HYOAS": "BAMLH0A0HYM2", "SOFR": "SOFR", "IORB": "IORB", "DGS10": "DGS10"}
FETCH_CONCURRENCY = 6

BEAR = "bear"
BULL = "bull"

# Bear thresholds.
CARRY_USDJPY_5D = -3.0  # % — yen strengthening this fast forces carry positions shut
CARRY_AUDJPY_5D = -4.0
CARRY_FX_VOL = 14.0  # annualized % over 10 days; USD/JPY normally runs 7–10%
CARRY_MIN_HITS = 2
VIX_PANIC = 30.0
VIX_INVERSION_FLOOR = 22.0
TERM_INVERTED = 1.05  # VIX9D / VIX3M: near-term fear priced clearly above 3-month fear
CREDIT_HYG_IEF_10D = -3.5  # % — junk bonds lagging Treasuries
CREDIT_OAS_20D = 0.75  # percentage points of high-yield spread widening
FUNDING_SOFR_IORB = 0.10  # pp, averaged over 3 prints so quarter-end spikes don't count
BANK_KRE_SPY_5D = -10.0
RATES_10Y_5D = 0.40  # pp
MOVE_PANIC = 150.0

# Bull thresholds.
CAPITULATION_VIX_HIGH = 30.0
CAPITULATION_REVERSAL = 0.85  # VIX closing ≥15% below its intraday high: panic sold into
# Recovery signals only count after their matching alarm was on within this many trading days.
RECOVERY_LOOKBACK = 15
TERM_NORMAL = 0.95
VIX_OFF_PEAK = 0.70
CREDIT_BOUNCE = 2.0  # HYG/IEF % off its 20-day low
CARRY_REBOUND_5D = 1.0


@dataclass(frozen=True)
class Series:
    dates: tuple[date, ...]
    values: tuple[float, ...]
    highs: tuple[float, ...] = ()

    def __len__(self) -> int:
        return len(self.values)

    @property
    def last(self) -> float:
        return self.values[-1]

    def upto(self, day: date) -> Series:
        end = bisect.bisect_right(self.dates, day)
        return Series(self.dates[:end], self.values[:end], self.highs[:end])


Market = dict[str, Series]


@dataclass(frozen=True)
class Signal:
    key: str
    side: str
    title: str
    lines: tuple[str, ...]
    context: str


# --- series math -------------------------------------------------------------------------------


def change_pct(s: Series | None, n: int) -> float | None:
    if s is None or len(s) <= n or s.values[-1 - n] <= 0:
        return None
    return (s.values[-1] / s.values[-1 - n] - 1) * 100


def change(s: Series | None, n: int) -> float | None:
    if s is None or len(s) <= n:
        return None
    return s.values[-1] - s.values[-1 - n]


def combine(a: Series | None, b: Series | None, op: Callable[[float, float], float]) -> Series | None:
    """Pointwise op over the dates both series share."""
    if a is None or b is None:
        return None
    other = dict(zip(b.dates, b.values))
    days = [d for d in a.dates if d in other]
    if not days:
        return None
    mine = dict(zip(a.dates, a.values))
    return Series(tuple(days), tuple(op(mine[d], other[d]) for d in days))


def ratio(a: Series | None, b: Series | None) -> Series | None:
    return combine(a, b, lambda x, y: x / y if y else math.nan)


def spread(a: Series | None, b: Series | None) -> Series | None:
    return combine(a, b, lambda x, y: x - y)


def realized_vol(s: Series | None, n: int, *, periods_per_year: int = 252) -> float | None:
    """Annualized % stdev of the last n daily log returns."""
    if s is None or len(s) <= n:
        return None
    window = s.values[-1 - n :]
    returns = [math.log(b / a) for a, b in zip(window, window[1:]) if a > 0 and b > 0]
    if len(returns) < 2:
        return None
    return statistics.stdev(returns) * math.sqrt(periods_per_year) * 100


# --- bear signals ------------------------------------------------------------------------------


def carry_unwind(m: Market) -> Signal | None:
    usdjpy = m.get("USDJPY")
    hits = []
    usd5 = change_pct(usdjpy, 5)
    if usd5 is not None and usd5 <= CARRY_USDJPY_5D:
        hits.append(f"USD/JPY **{format_pct(usd5)}** in 5d (now {usdjpy.last:.2f}) — yen surging")
    aud5 = change_pct(m.get("AUDJPY"), 5)
    if aud5 is not None and aud5 <= CARRY_AUDJPY_5D:
        hits.append(f"AUD/JPY **{format_pct(aud5)}** in 5d — carry pairs being dumped")
    vol = realized_vol(usdjpy, 10)
    if vol is not None and vol >= CARRY_FX_VOL:
        hits.append(f"USD/JPY 10d realized vol **{vol:.0f}%** (normal 7–10%)")
    if len(hits) < CARRY_MIN_HITS:
        return None
    return Signal(
        "carry_unwind",
        BEAR,
        "Yen carry trade unwinding",
        tuple(hits),
        "Aug 2024: USD/JPY 161→142 in 4 weeks, Nikkei −12% in a day, VIX 65 intraday.",
    )


def vol_shock(m: Market) -> Signal | None:
    vix = m.get("VIX")
    if vix is None or not len(vix):
        return None
    term = ratio(m.get("VIX9D"), m.get("VIX3M"))
    hits = []
    if vix.last >= VIX_PANIC:
        hits.append(f"VIX **{vix.last:.1f}** ({format_pct(change_pct(vix, 5))} in 5d)")
    if term is not None and term.last >= TERM_INVERTED and vix.last >= VIX_INVERSION_FLOOR:
        hits.append(f"VIX curve inverted: 9-day / 3-month **{term.last:.2f}** — near-term panic priced above later")
    if not hits:
        return None
    return Signal(
        "vol_shock",
        BEAR,
        "Volatility shock",
        tuple(hits),
        "Leveraged vol sellers and risk-parity funds de-risk mechanically when this hits (Feb 2018, Mar 2020, Aug 2024).",
    )


def credit_stress(m: Market) -> Signal | None:
    hits = []
    hyg_ief = change_pct(ratio(m.get("HYG"), m.get("IEF")), 10)
    if hyg_ief is not None and hyg_ief <= CREDIT_HYG_IEF_10D:
        hits.append(f"Junk bonds vs Treasuries (HYG/IEF) **{format_pct(hyg_ief)}** in 10d")
    oas = m.get("HYOAS")
    widening = change(oas, 20)
    if widening is not None and widening >= CREDIT_OAS_20D:
        hits.append(f"High-yield spread **{oas.last:.2f}%**, +{widening * 100:.0f}bp in 20 days")
    if not hits:
        return None
    return Signal(
        "credit_stress",
        BEAR,
        "Credit stress",
        tuple(hits),
        "Credit usually cracks before stocks do — lenders see defaults coming first.",
    )


def funding_stress(m: Market) -> Signal | None:
    gap = spread(m.get("SOFR"), m.get("IORB"))
    if gap is None or len(gap) < 3:
        return None
    avg = sum(gap.values[-3:]) / 3
    if avg < FUNDING_SOFR_IORB:
        return None
    return Signal(
        "funding_stress",
        BEAR,
        "Repo funding stress",
        (f"SOFR **{avg * 100:+.0f}bp** above interest on reserves (3-day avg) — cash is scarce overnight",),
        "Sep 2019: repo rates spiked to 10% and the Fed had to inject liquidity within days.",
    )


def bank_stress(m: Market) -> Signal | None:
    rel = change_pct(ratio(m.get("KRE"), m.get("SPY")), 5)
    if rel is None or rel > BANK_KRE_SPY_5D:
        return None
    return Signal(
        "bank_stress",
        BEAR,
        "Regional bank stress",
        (f"Regional banks (KRE) **{format_pct(rel)}** vs S&P 500 in 5d",),
        "Mar 2023: SVB and Signature failed within a week of KRE breaking down.",
    )


def rates_shock(m: Market) -> Signal | None:
    hits = []
    tenyear = m.get("DGS10")
    jump = change(tenyear, 5)
    if jump is not None and jump >= RATES_10Y_5D:
        hits.append(f"10y yield **{tenyear.last:.2f}%**, +{jump * 100:.0f}bp in 5 days")
    move = m.get("MOVE")
    if move is not None and len(move) and move.last >= MOVE_PANIC:
        hits.append(f"Bond volatility (MOVE) **{move.last:.0f}**")
    if not hits:
        return None
    return Signal(
        "rates_shock",
        BEAR,
        "Rates shock",
        tuple(hits),
        "Fast yield spikes reprice every asset at once (Oct 2023, UK gilts Sep 2022, Apr 2025).",
    )


# --- bull signals ------------------------------------------------------------------------------


def recently(check: Callable[[Market], Signal | None], m: Market, keys: tuple[str, ...]) -> bool:
    """Was `check` on at any of the previous RECOVERY_LOOKBACK bars of keys[0]?"""
    anchor = m.get(keys[0])
    if anchor is None:
        return False
    subset = {k: m[k] for k in keys if k in m}
    return any(check(as_of(subset, day)) is not None for day in anchor.dates[-1 - RECOVERY_LOOKBACK : -1])


def vol_capitulation(m: Market) -> Signal | None:
    vix = m.get("VIX")
    if vix is None or not len(vix) or not vix.highs:
        return None
    high = vix.highs[-1]
    if high < CAPITULATION_VIX_HIGH or vix.last > high * CAPITULATION_REVERSAL:
        return None
    return Signal(
        "vol_capitulation",
        BULL,
        "Volatility capitulation",
        (f"VIX spiked to **{high:.1f}** then fell back to {vix.last:.1f} ({(vix.last / high - 1) * 100:.0f}% off the high)",),
        "Panic got sold into — Aug 5 2024 (65→38) and Mar 2020 lows. Often the washout day, not yet the all-clear.",
    )


def vol_normalizing(m: Market) -> Signal | None:
    vix = m.get("VIX")
    term = ratio(m.get("VIX9D"), m.get("VIX3M"))
    if vix is None or term is None or len(vix) < 20 or term.last >= TERM_NORMAL:
        return None
    peak = max(vix.values[-20:])
    if vix.last > peak * VIX_OFF_PEAK or not recently(vol_shock, m, ("VIX", "VIX9D", "VIX3M")):
        return None
    return Signal(
        "vol_normalizing",
        BULL,
        "Fear normalizing",
        (
            f"VIX curve back to normal: 9-day / 3-month **{term.last:.2f}** after the volatility shock alarm",
            f"VIX {vix.last:.1f}, {(vix.last / peak - 1) * 100:.0f}% off its 20-day peak of {peak:.1f}",
        ),
        "The classic 'panic is over' tell — shorts opened into the spike have usually made their money by now.",
    )


def credit_healing(m: Market) -> Signal | None:
    rel = ratio(m.get("HYG"), m.get("IEF"))
    if rel is None or len(rel) < 20:
        return None
    low = min(rel.values[-20:])
    bounce = (rel.last / low - 1) * 100
    if bounce < CREDIT_BOUNCE or not recently(credit_stress, m, ("HYG", "IEF", "HYOAS")):
        return None
    return Signal(
        "credit_healing",
        BULL,
        "Credit healing",
        (f"Junk bonds vs Treasuries (HYG/IEF) **{format_pct(bounce)}** off their 20-day low after the credit stress alarm",),
        "Credit leads stocks on the way down and on the way up — lenders returning is a durable bottom sign.",
    )


def carry_stabilized(m: Market) -> Signal | None:
    usdjpy = m.get("USDJPY")
    now = change_pct(usdjpy, 5)
    if now is None or now < CARRY_REBOUND_5D or not recently(carry_unwind, m, ("USDJPY", "AUDJPY")):
        return None
    return Signal(
        "carry_stabilized",
        BULL,
        "Yen carry stabilizing",
        (f"USD/JPY **{format_pct(now)}** in 5d ({usdjpy.last:.2f}) after the carry unwind alarm",),
        "Forced yen buying is exhausted — in Aug 2024 stocks bottomed as USD/JPY stopped falling.",
    )


CHECKS: tuple[Callable[[Market], Signal | None], ...] = (
    carry_unwind,
    vol_shock,
    credit_stress,
    funding_stress,
    bank_stress,
    rates_shock,
    vol_capitulation,
    vol_normalizing,
    credit_healing,
    carry_stabilized,
)


def as_of(market: Market, day: date) -> Market:
    return {key: s.upto(day) for key, s in market.items()}


def evaluate(market: Market) -> list[Signal]:
    return [signal for check in CHECKS if (signal := check(market)) is not None]


# --- fetching ----------------------------------------------------------------------------------


def parse_yahoo_series(payload: dict) -> Series:
    result = payload["chart"]["result"][0]
    offset = result.get("meta", {}).get("gmtoffset") or 0
    quote = result["indicators"]["quote"][0]
    closes = quote.get("close") or []
    highs = quote.get("high") or [None] * len(closes)
    # FX feeds sometimes repeat the current day's bar; keep the latest per date.
    bars: dict[date, tuple[float, float]] = {}
    for ts, close, high in zip(result.get("timestamp") or [], closes, highs):
        if close is None:
            continue
        day = datetime.fromtimestamp(ts + offset, UTC).date()
        bars[day] = (float(close), float(high) if high is not None else float(close))
    days = sorted(bars)
    return Series(tuple(days), tuple(bars[d][0] for d in days), tuple(max(bars[d]) for d in days))


def parse_fred_csv(text: str) -> Series:
    days, values = [], []
    rows = csv.reader(io.StringIO(text))
    next(rows, None)
    for row in rows:
        if len(row) < 2:
            continue
        try:
            day, value = date.fromisoformat(row[0]), float(row[1])
        except ValueError:  # FRED marks holidays with "."
            continue
        days.append(day)
        values.append(value)
    return Series(tuple(days), tuple(values))


async def fetch_yahoo(client: httpx.AsyncClient, symbol: str, *, start: date | None) -> Series | None:
    params: dict[str, str | int] = {"interval": "1d"}
    if start is None:
        params["range"] = "1y"
    else:
        params["period1"] = int(datetime(start.year, start.month, start.day, tzinfo=UTC).timestamp())
        params["period2"] = int(datetime.now(UTC).timestamp())
    try:
        response = await client.get(YAHOO_CHART_URL.format(symbol=symbol), params=params)
        response.raise_for_status()
        return parse_yahoo_series(response.json())
    except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError) as exc:
        print(f"macro: skipping {symbol}: {exc}", file=sys.stderr)
        return None


async def fetch_fred(client: httpx.AsyncClient, series_id: str, *, start: date | None) -> Series | None:
    since = start or datetime.now(UTC).date() - timedelta(days=365)
    try:
        # FRED's firewall silently drops custom and browser user agents but lets library defaults through.
        response = await client.get(
            FRED_CSV_URL,
            params={"id": series_id, "cosd": since.isoformat()},
            headers={"User-Agent": f"python-httpx/{httpx.__version__}"},
        )
        response.raise_for_status()
        return parse_fred_csv(response.text)
    except (httpx.HTTPError, ValueError) as exc:
        print(f"macro: skipping FRED {series_id}: {exc}", file=sys.stderr)
        return None


async def fetch_market(client: httpx.AsyncClient, *, start: date | None = None) -> Market:
    """All series, from `start` (backtest) or the last year (live). Failed sources are left out."""
    gate = asyncio.Semaphore(FETCH_CONCURRENCY)

    async def one(key: str, fetch) -> tuple[str, Series | None]:
        async with gate:
            return key, await fetch

    jobs = [one(k, fetch_yahoo(client, sym, start=start)) for k, sym in YAHOO_SERIES.items()]
    jobs += [one(k, fetch_fred(client, sid, start=start)) for k, sid in FRED_SERIES.items()]
    return {key: s for key, s in await asyncio.gather(*jobs) if s is not None and len(s)}
