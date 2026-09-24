"""Tests for macro.py — run from repo root: python -m pytest scripts"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from macro import (
    BEAR,
    BULL,
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
    parse_yahoo_series,
    rates_shock,
    ratio,
    realized_vol,
    spread,
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


def test_parse_fred_csv_skips_missing():
    text = "observation_date,SOFR\n2026-09-18,3.90\n2026-09-19,.\n2026-09-22,3.87\n"
    s = parse_fred_csv(text)
    assert s.values == (3.90, 3.87)
    assert s.dates == (date(2026, 9, 18), date(2026, 9, 22))
