"""Market-wide crash alarms and bottom signals from free Yahoo and FRED daily data.

Bear signals flag the stresses behind past crashes: yen carry unwinds, volatility shocks, credit,
funding and bank stress, rate shocks. They fire late, so one watch signal flags two of those
stresses starting together while stocks are still near their highs. Bull signals flag the washouts
and recoveries that marked bottoms: volatility capitulation, fear normalizing, credit healing, the
yen stabilizing.

Every signal is a pure function of the history up to a given day, so the same code runs live and
in the backtest. Both judge completed daily bars: as_of() is the view at a day's close.
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
from datetime import UTC, date, datetime, timedelta, timezone, tzinfo
from itertools import pairwise
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

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
# FRED publishes each observation the next business day, so at a day's close the newest print a
# live run can have seen is the previous day's. as_of() holds FRED series back to match.
FRED_LAG_DAYS = 1
FRED_STALE_DAYS = 7  # a FRED series this far behind is treated as missing
# A live run fetches a year of daily bars; as_of() gives every replayed day the same view.
LIVE_BARS = 252
INTRADAY_INTERVAL = "60m"
INTRADAY_RANGE = "730d"  # the most history Yahoo serves at this interval

BEAR = "bear"
WATCH = "watch"
BULL = "bull"
SIDES = (BEAR, WATCH, BULL)

# Early warning: two stresses at once while stocks are still near their highs. Chosen on data
# through 2019 (see the README); it fires about 3% below the high where the crash alarms average 9%.
EARLY_TERM = 0.95  # VIX9D / VIX3M out of its normal shape (TERM_NORMAL), well short of inverted
EARLY_CREDIT_10D = -1.5  # % — HYG / IEF
EARLY_NEAR_HIGH = 5.0  # % — SPY within this of its high over EARLY_HIGH_DAYS
EARLY_HIGH_DAYS = 60

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

    def upto(self, day: date, *, keep: int | None = None) -> Series:
        """Bars dated on or before `day`; only the last `keep` of them when given."""
        end = bisect.bisect_right(self.dates, day)
        start = max(0, end - keep) if keep else 0
        return Series(self.dates[start:end], self.values[start:end], self.highs[start:end])


Market = dict[str, Series]

# One intraday bar: when it started (UTC), its last price and its high.
IntradayBar = tuple[datetime, float, float]
Intraday = dict[str, tuple[IntradayBar, ...]]


def signed(value: float, spec: str) -> str:
    """format(), without printing a value that rounds to zero as -0.0."""
    shown = format(value, spec)
    return shown.replace("-", "+" if "+" in spec else "") if float(shown) == 0 else shown


@dataclass(frozen=True)
class Reading:
    """One number behind a signal, kept so a later alert can say how it has moved."""

    label: str
    value: float
    spec: str = ".2f"
    unit: str = ""

    def text(self, value: float | None = None) -> str:
        return f"{signed(self.value if value is None else value, self.spec)}{self.unit}"


@dataclass(frozen=True)
class Signal:
    key: str
    side: str
    title: str
    lines: tuple[str, ...]
    context: str
    # How far past its trigger: 1.0 is exactly on the threshold. Compared between alerts of the
    # same signal to tell when one has got materially worse.
    severity: float = 1.0
    readings: tuple[Reading, ...] = ()


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
    returns = [math.log(b / a) for a, b in pairwise(window) if a > 0 and b > 0]
    if len(returns) < 2:
        return None
    return statistics.stdev(returns) * math.sqrt(periods_per_year) * 100


def beyond(value: float | None, threshold: float) -> float:
    """A reading as a multiple of its threshold; 0 when there is no reading."""
    return value / threshold if value is not None else 0.0


def readings(*items: tuple[str, float | None, str, str]) -> tuple[Reading, ...]:
    return tuple(Reading(label, value, spec, unit) for label, value, spec, unit in items if value is not None)


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
    legs = sorted((beyond(usd5, CARRY_USDJPY_5D), beyond(aud5, CARRY_AUDJPY_5D), beyond(vol, CARRY_FX_VOL)), reverse=True)
    return Signal(
        "carry_unwind",
        BEAR,
        "Yen carry trade unwinding",
        tuple(hits),
        "Aug 2024: USD/JPY 161→142 in 4 weeks, Nikkei −12% in a day, VIX 65 intraday.",
        severity=legs[CARRY_MIN_HITS - 1],  # the weakest leg that still counts
        readings=readings(("USD/JPY 5d", usd5, "+.1f", "%"), ("AUD/JPY 5d", aud5, "+.1f", "%"), ("USD/JPY vol", vol, ".0f", "%")),
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
    inversion = min(beyond(term.last, TERM_INVERTED), beyond(vix.last, VIX_INVERSION_FLOOR)) if term is not None else 0.0
    return Signal(
        "vol_shock",
        BEAR,
        "Volatility shock",
        tuple(hits),
        "Leveraged vol sellers and risk-parity funds de-risk mechanically when this hits (Feb 2018, Mar 2020, Aug 2024).",
        severity=max(beyond(vix.last, VIX_PANIC), inversion),
        readings=readings(("VIX", vix.last, ".1f", ""), ("9-day/3-month", term.last if term is not None else None, ".2f", "")),
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
        "Lenders are pricing in defaults. Stocks have usually fallen by the time this fires: it confirms stress, it doesn't lead it.",
        severity=max(beyond(hyg_ief, CREDIT_HYG_IEF_10D), beyond(widening, CREDIT_OAS_20D)),
        readings=readings(("HYG/IEF 10d", hyg_ief, "+.1f", "%"), ("High-yield spread", oas.last if oas is not None and len(oas) else None, ".2f", "%")),
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
        severity=beyond(avg, FUNDING_SOFR_IORB),
        readings=readings(("SOFR over IORB", avg * 100, "+.0f", "bp")),
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
        severity=beyond(rel, BANK_KRE_SPY_5D),
        readings=readings(("KRE vs SPY 5d", rel, "+.1f", "%")),
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
    has_move = move is not None and len(move)
    return Signal(
        "rates_shock",
        BEAR,
        "Rates shock",
        tuple(hits),
        "Fast yield spikes reprice every asset at once (Oct 2023, UK gilts Sep 2022, Apr 2025).",
        severity=max(beyond(jump, RATES_10Y_5D), beyond(move.last if has_move else None, MOVE_PANIC)),
        readings=readings(("10y yield", tenyear.last if jump is not None else None, ".2f", "%"), ("MOVE", move.last if has_move else None, ".0f", "")),
    )


# --- early warning -----------------------------------------------------------------------------


def stress_building(m: Market) -> Signal | None:
    term = ratio(m.get("VIX9D"), m.get("VIX3M"))
    credit = change_pct(ratio(m.get("HYG"), m.get("IEF")), 10)
    spy = m.get("SPY")
    if term is None or credit is None or spy is None or len(spy) < EARLY_HIGH_DAYS:
        return None
    off_high = (spy.last / max(spy.values[-EARLY_HIGH_DAYS:]) - 1) * 100
    if term.last < EARLY_TERM or credit > EARLY_CREDIT_10D or off_high < -EARLY_NEAR_HIGH:
        return None
    return Signal(
        "stress_building",
        WATCH,
        "Stress building near the highs",
        (
            f"VIX curve flattening: 9-day / 3-month **{term.last:.2f}** (normal is below {EARLY_TERM})",
            f"Junk bonds vs Treasuries (HYG/IEF) **{format_pct(credit)}** in 10d",
            f"SPY still only {format_pct(off_high)} from its 3-month high",
        ),
        "Options and credit are both flinching while stocks have barely moved. Earlier than the crash alarms, and wrong about half the time.",
        severity=min(beyond(term.last, EARLY_TERM), beyond(credit, EARLY_CREDIT_10D)),
        readings=readings(("9-day/3-month", term.last, ".2f", ""), ("HYG/IEF 10d", credit, "+.1f", "%")),
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
        readings=readings(("VIX", vix.last, ".1f", "")),
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
        "The 'panic is over' tell. It confirms a low more than it catches one: most of the first bounce is usually gone.",
        readings=readings(("VIX", vix.last, ".1f", ""), ("9-day/3-month", term.last, ".2f", "")),
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
        "Lenders are coming back. On its own this has not marked durable lows — see its record.",
        readings=readings(("HYG/IEF off its low", bounce, "+.1f", "%")),
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
        readings=readings(("USD/JPY", usdjpy.last, ".2f", "")),
    )


CHECKS: tuple[Callable[[Market], Signal | None], ...] = (
    carry_unwind,
    vol_shock,
    credit_stress,
    funding_stress,
    bank_stress,
    rates_shock,
    stress_building,
    vol_capitulation,
    vol_normalizing,
    credit_healing,
    carry_stabilized,
)


@dataclass(frozen=True)
class Spec:
    side: str
    needs: tuple[str, ...]  # series it cannot fire without; the backtest counts its quiet years from where these begin
    also: tuple[str, ...] = ()  # series behind its other legs (shorter history: the 9-day VIX, the FRED spread)


SIGNALS = {
    "carry_unwind": Spec(BEAR, ("USDJPY",), ("AUDJPY",)),
    "vol_shock": Spec(BEAR, ("VIX",), ("VIX9D", "VIX3M")),
    "credit_stress": Spec(BEAR, ("HYG", "IEF"), ("HYOAS",)),
    "funding_stress": Spec(BEAR, ("SOFR", "IORB")),
    "bank_stress": Spec(BEAR, ("KRE", "SPY")),
    "rates_shock": Spec(BEAR, ("DGS10",), ("MOVE",)),
    "stress_building": Spec(WATCH, ("VIX9D", "VIX3M", "HYG", "IEF", "SPY")),
    "vol_capitulation": Spec(BULL, ("VIX",)),
    "vol_normalizing": Spec(BULL, ("VIX", "VIX9D", "VIX3M")),
    "credit_healing": Spec(BULL, ("HYG", "IEF"), ("HYOAS",)),
    "carry_stabilized": Spec(BULL, ("USDJPY",), ("AUDJPY",)),
}
CHECK_BY_KEY = {check.__name__: check for check in CHECKS}


def unreadable(market: Market, day: date) -> set[str]:
    """Signals with an input that failed to load or has no bar for `day`: their state is unknown, not off."""

    def gone(name: str) -> bool:
        s = market.get(name)
        if s is None:
            return True
        if not len(s):  # before the series began: that leg simply doesn't exist yet
            return False
        return s.dates[-1] < day - timedelta(days=FRED_STALE_DAYS if name in FRED_SERIES else 0)

    return {key for key, spec in SIGNALS.items() if any(gone(name) for name in (*spec.needs, *spec.also))}


def sessions_on(key: str, market: Market, *, window: int = 20) -> tuple[int, int]:
    """(closes in a row, closes out of the last `window`) that a signal has been on, ending with the latest."""
    calendar = market.get("SPY") or market.get("VIX")
    if calendar is None or key not in CHECK_BY_KEY:
        return 0, 0
    on = [CHECK_BY_KEY[key](as_of(market, day)) is not None for day in reversed(calendar.dates[-window:])]
    return next((i for i, flag in enumerate(on) if not flag), len(on)), sum(on)


def as_of(market: Market, day: date) -> Market:
    """What a run just after `day`'s close sees: that day's final bars, FRED a day behind."""
    published = day - timedelta(days=FRED_LAG_DAYS)
    return {key: s.upto(published if key in FRED_SERIES else day, keep=LIVE_BARS) for key, s in market.items()}


def evaluate(market: Market) -> list[Signal]:
    return [signal for check in CHECKS if (signal := check(market)) is not None]


# --- fetching ----------------------------------------------------------------------------------


def bar_zone(meta: dict) -> tzinfo:
    """The exchange's timezone, so each bar is dated by the clock in force that day, not today's."""
    try:
        return ZoneInfo(meta["exchangeTimezoneName"])
    except (KeyError, ValueError, ZoneInfoNotFoundError):
        return timezone(timedelta(seconds=meta.get("gmtoffset") or 0))


def parse_yahoo_series(payload: dict) -> Series:
    result = payload["chart"]["result"][0]
    meta = result.get("meta", {})
    zone = bar_zone(meta)
    quote = result["indicators"]["quote"][0]
    closes = quote.get("close") or []
    opens = quote.get("open") or [None] * len(closes)
    highs = quote.get("high") or [None] * len(closes)
    # FX feeds repeat the current day's bar with the live price: a day's first bar holds its open,
    # its last bar the latest price.
    bars: dict[date, list[float]] = {}
    for ts, open_, close, high in zip(result.get("timestamp") or [], opens, closes, highs):
        if close is None:
            continue
        day = datetime.fromtimestamp(ts, zone).date()
        bar = bars.setdefault(day, [float(open_ if open_ is not None else close), float(close), float(close)])
        bar[1] = float(close)
        bar[2] = max(bar[2], float(close), float(high) if high is not None else float(close))
    days = sorted(bars)
    values = [bars[d][1] for d in days]
    if meta.get("instrumentType") == "CURRENCY" and days:
        # Yahoo's daily FX bars have no closing price ("close" repeats the open), so a day's close
        # is the next bar's open and the newest day's is the live price.
        values = [bars[d][0] for d in days[1:]] + [float(meta.get("regularMarketPrice") or values[-1])]
    return Series(tuple(days), tuple(values), tuple(bars[d][2] for d in days))


def parse_yahoo_intraday(payload: dict) -> tuple[IntradayBar, ...]:
    result = payload["chart"]["result"][0]
    quote = result["indicators"]["quote"][0]
    closes = quote.get("close") or []
    highs = quote.get("high") or [None] * len(closes)
    return tuple(
        (datetime.fromtimestamp(ts, UTC), float(close), float(high) if high is not None else float(close))
        for ts, close, high in zip(result.get("timestamp") or [], closes, highs)
        if close is not None
    )


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


async def fetch_yahoo_intraday(client: httpx.AsyncClient, symbol: str) -> tuple[IntradayBar, ...]:
    """Hourly bars for as far back as Yahoo serves them (about two years)."""
    try:
        response = await client.get(YAHOO_CHART_URL.format(symbol=symbol), params={"interval": INTRADAY_INTERVAL, "range": INTRADAY_RANGE})
        response.raise_for_status()
        return parse_yahoo_intraday(response.json())
    except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError) as exc:
        print(f"macro: skipping hourly {symbol}: {exc}", file=sys.stderr)
        return ()


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


async def fetch_intraday(client: httpx.AsyncClient) -> Intraday:
    """Hourly bars for every Yahoo series, for replaying what a mid-session run would have seen."""
    gate = asyncio.Semaphore(FETCH_CONCURRENCY)

    async def one(key: str, symbol: str) -> tuple[str, tuple[IntradayBar, ...]]:
        async with gate:
            return key, await fetch_yahoo_intraday(client, symbol)

    return {key: bars for key, bars in await asyncio.gather(*(one(k, sym) for k, sym in YAHOO_SERIES.items())) if bars}
