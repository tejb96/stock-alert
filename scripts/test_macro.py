"""Tests for macro.py — run from repo root: python -m pytest scripts"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from macro import (
    BEAR,
    BULL,
    CHECKS,
    FRED_SERIES,
    LIVE_BARS,
    SIGNALS,
    WATCH,
    Reading,
    Series,
    as_of,
    bank_stress,
    carry_stabilized,
    carry_unwind,
    change,
    change_pct,
    credit_healing,
    credit_stress,
    evaluate,
    funding_stress,
    parse_fred_csv,
    parse_yahoo_intraday,
    parse_yahoo_series,
    rates_shock,
    ratio,
    realized_vol,
    sessions_on,
    spread,
    stress_building,
    unreadable,
    vol_capitulation,
    vol_normalizing,
    vol_shock,
)

START = date(2026, 1, 1)


def series(*values: float, highs=None, start: date = START) -> Series:
    days = tuple(start + timedelta(days=i) for i in range(len(values)))
    return Series(days, tuple(float(v) for v in values), tuple(highs) if highs else tuple(float(v) for v in values))


def flat(value: float, n: int = 30) -> list[float]:
    return [value] * n


def test_upto_truncates_by_date():
    s = series(1, 2, 3, 4)
    assert s.upto(START + timedelta(days=1)).values == (1.0, 2.0)
    assert len(s.upto(START - timedelta(days=1))) == 0


def test_upto_keeps_only_the_latest_bars():
    s = series(1, 2, 3, 4, highs=[1, 2, 3, 5])
    kept = s.upto(START + timedelta(days=3), keep=2)
    assert (kept.values, kept.highs, kept.dates[0]) == ((3.0, 4.0), (3, 5), START + timedelta(days=2))
    assert s.upto(START + timedelta(days=1), keep=5).values == (1.0, 2.0)


def test_changes_and_short_history():
    s = series(100, 101, 102, 110)
    assert change_pct(s, 3) == pytest.approx(10.0)
    assert change(s, 1) == pytest.approx(8.0)
    assert change_pct(s, 4) is None
    assert change_pct(None, 1) is None


def test_ratio_and_spread_align_on_shared_dates():
    a = series(10, 20, 30)
    b = Series((START + timedelta(days=1), START + timedelta(days=2), START + timedelta(days=9)), (5.0, 10.0, 1.0))
    assert ratio(a, b).values == (4.0, 3.0)
    assert spread(a, b).values == (15.0, 20.0)
    assert ratio(a, None) is None


def test_realized_vol_flat_is_zero():
    assert realized_vol(series(*flat(100, 12)), 10) == 0
    assert realized_vol(series(1, 2), 10) is None


def test_carry_unwind_needs_two_conditions():
    calm_usd = series(*flat(160, 25))
    falling_usd = series(*flat(160, 20), 158, 156, 154, 153, 152)
    falling_aud = series(*flat(110, 20), 108, 106, 105, 104, 103)
    assert carry_unwind({"USDJPY": falling_usd}) is None  # yen move alone, vol still low
    signal = carry_unwind({"USDJPY": falling_usd, "AUDJPY": falling_aud})
    assert signal.side == BEAR
    assert len(signal.lines) >= 2
    assert carry_unwind({"USDJPY": calm_usd, "AUDJPY": series(*flat(110, 25))}) is None


def test_vol_shock_on_panic_level_or_inversion():
    assert vol_shock({"VIX": series(15, 32)}).key == "vol_shock"
    inverted = {"VIX": series(24), "VIX9D": series(28), "VIX3M": series(25)}
    assert "inverted" in vol_shock(inverted).lines[0]
    assert vol_shock({"VIX": series(18), "VIX9D": series(21), "VIX3M": series(19)}) is None  # inverted but calm


def test_credit_stress_from_etfs_or_spread():
    hyg = series(*flat(80, 10), 76)
    ief = series(*flat(95, 11))
    assert credit_stress({"HYG": hyg, "IEF": ief}) is not None
    oas = series(*flat(3.0, 20), 4.0)
    assert "+100bp" in credit_stress({"HYOAS": oas}).lines[0]
    assert credit_stress({"HYG": series(*flat(80, 11)), "IEF": ief}) is None


def test_funding_stress_averages_three_prints():
    iorb = series(*flat(3.90, 3))
    assert funding_stress({"SOFR": series(3.88, 3.90, 4.15), "IORB": iorb}) is None  # one-day quarter-end spike
    assert funding_stress({"SOFR": series(4.02, 4.00, 4.05), "IORB": iorb}) is not None


def test_bank_stress_is_relative_to_spy():
    spy = series(*flat(700, 6))
    assert bank_stress({"KRE": series(70, 70, 70, 70, 70, 62), "SPY": spy}) is not None
    assert bank_stress({"KRE": series(*flat(70, 6)), "SPY": spy}) is None


def test_rates_shock():
    assert rates_shock({"DGS10": series(4.2, 4.2, 4.3, 4.4, 4.5, 4.65)}) is not None
    assert rates_shock({"MOVE": series(155)}) is not None
    assert rates_shock({"DGS10": series(*flat(4.2, 6)), "MOVE": series(100)}) is None


def test_stress_building_needs_both_stresses_near_the_high():
    n = 70
    spy = series(*flat(700, n - 1), 686)  # 2% below the high
    hyg = series(*flat(80, n - 10), *[80 - 0.16 * i for i in range(1, 11)])  # -2% in 10d
    ief = series(*flat(95, n))
    curve = {"VIX9D": series(*flat(19.6, n)), "VIX3M": series(*flat(20, n))}  # 0.98
    m = {"SPY": spy, "HYG": hyg, "IEF": ief, **curve}
    signal = stress_building(m)
    assert (signal.key, signal.side) == ("stress_building", WATCH)
    assert "0.98" in signal.lines[0] and "-2.0%" in signal.lines[1] and "-2.0%" in signal.lines[2]
    assert stress_building({**m, "VIX9D": series(*flat(18, n))}) is None  # curve still in its normal shape
    assert stress_building({**m, "HYG": series(*flat(80, n))}) is None  # credit holding up
    assert stress_building({**m, "SPY": series(*flat(700, n - 1), 660)}) is None  # stocks have already fallen
    assert stress_building({**m, "SPY": series(*flat(700, 30))}) is None  # not enough history for the high


def test_vol_capitulation_needs_spike_and_reversal():
    assert vol_capitulation({"VIX": series(38, highs=[65])}).side == BULL
    assert vol_capitulation({"VIX": series(60, highs=[65])}) is None
    assert vol_capitulation({"VIX": series(20, highs=[25])}) is None


def test_vol_normalizing_only_after_a_shock():
    vix = [18] * 10 + [35, 40, 30] + [20] * 7
    nine = [16] * 10 + [45, 48, 35] + [17] * 7
    three = [20] * 20
    m = {"VIX": series(*vix), "VIX9D": series(*nine), "VIX3M": series(*three)}
    assert vol_normalizing(m).key == "vol_normalizing"
    calm = {"VIX": series(*flat(15, 20)), "VIX9D": series(*flat(14, 20)), "VIX3M": series(*flat(17, 20))}
    assert vol_normalizing(calm) is None


def test_credit_healing_only_after_stress():
    hyg = [80] * 20 + [76, 75, 76, 77.5]
    m = {"HYG": series(*hyg), "IEF": series(*flat(95, len(hyg)))}
    assert credit_healing(m).key == "credit_healing"
    bounce_only = [80] * 20 + [79, 78.6, 79.2, 80.5]
    assert credit_healing({"HYG": series(*bounce_only), "IEF": series(*flat(95, len(bounce_only)))}) is None


def test_carry_stabilized_only_after_unwind():
    usd = [160] * 20 + [158, 156, 154, 153, 152, 152, 153, 154, 155, 156]
    aud = [110] * 20 + [108, 106, 105, 104, 103, 103, 104, 104, 105, 106]
    assert carry_stabilized({"USDJPY": series(*usd), "AUDJPY": series(*aud)}).key == "carry_stabilized"
    rising = [150 + i * 0.3 for i in range(30)]
    assert carry_stabilized({"USDJPY": series(*rising), "AUDJPY": series(*flat(110))}) is None


def test_evaluate_and_as_of():
    m = {"VIX": series(15, 32, highs=[16, 33])}
    assert [s.key for s in evaluate(m)] == ["vol_shock"]
    assert evaluate(as_of(m, START)) == []


def test_as_of_holds_fred_back_a_day():
    """FRED publishes a day late: at Wednesday's close the newest print is Tuesday's."""
    wednesday = START + timedelta(days=2)
    m = {"VIX": series(15, 16, 17, 18), "DGS10": series(4.0, 4.1, 4.2, 4.3)}
    view = as_of(m, wednesday)
    assert view["VIX"].values == (15.0, 16.0, 17.0)
    assert view["DGS10"].values == (4.0, 4.1)
    assert "DGS10" in FRED_SERIES


def test_as_of_gives_the_year_of_bars_a_live_run_fetches():
    long = series(*range(1, 401))
    assert len(as_of({"VIX": long}, long.dates[-1])["VIX"]) == LIVE_BARS


def test_severity_measures_how_far_past_the_trigger():
    assert vol_shock({"VIX": series(15, 45)}).severity == pytest.approx(1.5)  # VIX 45 against 30
    inverted = vol_shock({"VIX": series(24.2), "VIX9D": series(28.875), "VIX3M": series(25)})
    assert inverted.severity == pytest.approx(1.1)  # curve 1.155 against 1.05; the VIX floor is cleared by the same margin
    barely = vol_shock({"VIX": series(22.0), "VIX9D": series(35), "VIX3M": series(25)})
    assert barely.severity == pytest.approx(1.0)  # held back by the VIX floor, however inverted the curve
    assert [(r.label, r.text()) for r in inverted.readings] == [("VIX", "24.2"), ("9-day/3-month", "1.16")]


def test_carry_severity_is_its_weakest_counted_leg():
    falling_usd = series(*flat(160, 20), 158, 156, 154, 153, 152)  # -5% in 5d
    falling_aud = series(*flat(110, 20), 108, 106, 105, 104, 105.6)  # -4% in 5d
    signal = carry_unwind({"USDJPY": falling_usd, "AUDJPY": falling_aud})
    assert signal.severity == pytest.approx(1.0, abs=0.01)  # two legs needed, and AUD/JPY is only just there
    assert [r.label for r in signal.readings] == ["USD/JPY 5d", "AUD/JPY 5d", "USD/JPY vol"]


def test_reading_formats_old_and_new_values_alike():
    reading = Reading("HYG/IEF 10d", -3.84, "+.1f", "%")
    assert (reading.text(), reading.text(-2.0), reading.text(-0.04)) == ("-3.8%", "-2.0%", "+0.0%")


def test_every_check_is_registered():
    assert [check.__name__ for check in CHECKS] == list(SIGNALS)
    hot = {"VIX": series(15, 45)}
    assert vol_shock(hot).side == SIGNALS["vol_shock"].side == BEAR
    assert SIGNALS["vol_normalizing"].side == BULL


def test_unreadable_tells_a_missing_feed_from_a_quiet_signal():
    today = START + timedelta(days=2)
    full = {name: series(1, 2, 3) for spec in SIGNALS.values() for name in (*spec.needs, *spec.also)}
    assert unreadable(full, today) == set()
    assert unreadable({k: v for k, v in full.items() if k != "VIX9D"}, today) == {"vol_shock", "stress_building", "vol_normalizing"}  # fetch failed
    assert unreadable({**full, "MOVE": series(1, 2)}, today) == {"rates_shock"}  # no bar for today
    assert unreadable({**full, "VIX9D": Series((), ())}, today) == set()  # before the series began
    assert unreadable({**full, "SOFR": series(1, 2)}, today) == set()  # FRED runs behind by design
    assert unreadable({**full, "SOFR": series(1, 2)}, today + timedelta(days=10)) >= {"funding_stress"}


def test_sessions_on_counts_the_streak_and_the_recent_closes():
    m = {"SPY": series(*flat(700, 8)), "VIX": series(15, 31, 15, 15, 32, 33, 34, 35)}
    assert sessions_on("vol_shock", m) == (4, 5)
    assert sessions_on("vol_shock", m, window=3) == (3, 3)
    assert sessions_on("vol_shock", as_of(m, START + timedelta(days=3))) == (0, 1)
    assert sessions_on("not_a_signal", m) == (0, 0)


def test_parse_yahoo_series_dedupes_days_and_skips_gaps():
    payload = {
        "chart": {
            "result": [
                {
                    "meta": {"gmtoffset": 3600},
                    "timestamp": [1790204400, 1790290800, 1790377200, 1790402400],
                    "indicators": {"quote": [{"close": [157.0, None, 158.0, 158.5], "high": [157.5, None, 158.2, None]}]},
                }
            ]
        }
    }
    s = parse_yahoo_series(payload)
    assert s.values == (157.0, 158.5)
    assert s.highs == (157.5, 158.5)
    assert s.dates[1] - s.dates[0] == timedelta(days=2)


def test_parse_yahoo_series_dates_each_bar_by_that_days_clock():
    """A London-midnight bar from July is stamped 23:00 UTC the evening before; today's offset must not date it."""
    payload = {
        "chart": {
            "result": [
                {
                    "meta": {"gmtoffset": 0, "exchangeTimezoneName": "Europe/London"},
                    "timestamp": [1784674800, 1794528000],  # 2026-07-21 23:00 UTC, 2026-11-13 00:00 UTC
                    "indicators": {"quote": [{"close": [20.0, 21.0], "high": [20.5, 21.5]}]},
                }
            ]
        }
    }
    assert parse_yahoo_series(payload).dates == (date(2026, 7, 22), date(2026, 11, 13))


def test_parse_yahoo_series_takes_fx_closes_from_the_next_open():
    """Yahoo's daily FX bars repeat the open as the close; the real close is where the next bar opens."""
    payload = {
        "chart": {
            "result": [
                {
                    "meta": {"instrumentType": "CURRENCY", "exchangeTimezoneName": "Europe/London", "regularMarketPrice": 159.69},
                    "timestamp": [1794268800, 1794355200, 1794441600],  # 2026-11-10/11/12 00:00 UTC
                    "indicators": {
                        "quote": [
                            {"open": [157.99, 158.73, 159.74], "close": [157.98, 158.71, 159.73], "high": [158.84, 159.89, 160.04]},
                        ]
                    },
                }
            ]
        }
    }
    s = parse_yahoo_series(payload)
    assert s.dates == (date(2026, 11, 10), date(2026, 11, 11), date(2026, 11, 12))
    assert s.values == (158.73, 159.74, 159.69)  # each day ends where the next begins; the newest at the live price
    assert s.highs == (158.84, 159.89, 160.04)


def test_parse_yahoo_intraday_keeps_bar_times_and_skips_gaps():
    payload = {"chart": {"result": [{"timestamp": [1794580200, 1794583800, 1794587400], "indicators": {"quote": [{"close": [20.0, None, 21.0], "high": [20.4, None, None]}]}}]}}
    bars = parse_yahoo_intraday(payload)
    assert [(b[0].isoformat(), b[1], b[2]) for b in bars] == [("2026-11-13T14:30:00+00:00", 20.0, 20.4), ("2026-11-13T16:30:00+00:00", 21.0, 21.0)]


def test_parse_fred_csv_skips_missing():
    text = "observation_date,SOFR\n2026-09-18,3.90\n2026-09-19,.\n2026-09-22,3.87\n"
    s = parse_fred_csv(text)
    assert s.values == (3.90, 3.87)
    assert s.dates == (date(2026, 9, 18), date(2026, 9, 22))
