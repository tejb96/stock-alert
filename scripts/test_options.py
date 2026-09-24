"""Tests for options.py — run from repo root: python -m pytest scripts"""

from __future__ import annotations

import math
from datetime import date

import pytest

from common import NotifyError
from options import (
    Chain,
    History,
    OptionQuote,
    PutCandidate,
    PutFilter,
    compute_history,
    iv_rank,
    parse_cboe_chain,
    parse_earnings_date,
    parse_osi,
    put_candidates,
)

TODAY = date(2026, 9, 24)
EXPIRY = date(2026, 10, 30)  # 36 DTE


def make_put(strike: float, *, bid: float, ask: float, delta: float, iv: float = 0.5, oi: int = 500, expiry: date = EXPIRY) -> OptionQuote:
    return OptionQuote(f"GME{expiry:%y%m%d}P{int(strike * 1000):08d}", expiry, "P", strike, bid, ask, iv, delta, oi, 10)


def make_chain(*options: OptionQuote, price: float = 24.0) -> Chain:
    return Chain("GME", price, 0.55, list(options))


HISTORY = History(realized_vol=0.40, sma50=20.0)


def test_parse_osi():
    assert parse_osi("GME261016P00022500") == (date(2026, 10, 16), "P", 22.5)
    assert parse_osi("SPXW261016C05800000") == (date(2026, 10, 16), "C", 5800.0)
    assert parse_osi("garbage") is None
    assert parse_osi("GME261399P00022500") is None


def test_parse_cboe_chain():
    payload = {
        "timestamp": "2026-09-23 03:58:53",
        "data": {
            "current_price": 24.07,
            "iv30": 54.5,
            "options": [
                {"option": "GME261016P00022000", "bid": 0.31, "ask": 0.45, "iv": 0.48, "delta": -0.21, "open_interest": 3112.0, "volume": 347.0},
                {"option": "GME261016C00026000", "bid": 0.5, "ask": 0.6, "iv": 0.5, "delta": 0.3, "open_interest": None, "volume": 0},
                {"option": "not-an-option"},
            ],
        },
    }
    chain = parse_cboe_chain("GME", payload)
    assert chain.price == 24.07
    assert chain.iv30 == pytest.approx(0.545)
    assert len(chain.options) == 2
    put = chain.puts()[0]
    assert (put.strike, put.open_interest, put.mid) == (22.0, 3112, pytest.approx(0.38))
    assert chain.options[1].open_interest == 0


def test_parse_cboe_chain_requires_price():
    with pytest.raises(NotifyError):
        parse_cboe_chain("GME", {"data": {"options": []}})
    with pytest.raises(NotifyError):
        parse_cboe_chain("GME", {})


def test_compute_history():
    # Alternating ±1% moves → ~1% daily stdev → ~16% annualized.
    closes = [100.0]
    for i in range(70):
        closes.append(closes[-1] * (1.01 if i % 2 else 0.99))
    history = compute_history(closes)
    assert history.realized_vol == pytest.approx(0.01 * math.sqrt(252), rel=0.05)
    assert history.sma50 == pytest.approx(sum(closes[-50:]) / 50)
    assert compute_history([100.0, 101.0]) == History(None, None)


def test_candidate_metrics():
    put = make_put(22.0, bid=0.45, ask=0.55, delta=-0.2, iv=0.5)
    c = PutCandidate("GME", 24.0, put, 36, 0.40)
    assert c.premium == pytest.approx(0.50)
    assert c.capital == 2200
    assert c.breakeven == pytest.approx(21.5)
    assert c.yield_pct == pytest.approx(0.5 / 22 * 100)
    assert c.annualized_pct == pytest.approx(0.5 / 22 * 100 * 365 / 36)
    assert c.cushion_pct == pytest.approx((24 - 21.5) / 24 * 100)
    assert c.edge == pytest.approx(1.25)
    assert c.cushion_sigmas == pytest.approx(math.log(24 / 21.5) / (0.40 * math.sqrt(36 / 365)))
    assert 0.5 < c.prob_profit < 1
    assert c.score == pytest.approx(c.annualized_pct * 1.25)


def test_candidate_edge_capped_and_missing():
    put = make_put(22.0, bid=0.45, ask=0.55, delta=-0.2, iv=2.0)
    assert PutCandidate("GME", 24.0, put, 36, 0.40).score == pytest.approx(
        PutCandidate("GME", 24.0, put, 36, 0.40).annualized_pct * 2.0
    )
    no_rv = PutCandidate("GME", 24.0, put, 36, None)
    assert no_rv.edge is None
    assert no_rv.score == pytest.approx(no_rv.annualized_pct)


def test_put_candidates_filters():
    good = make_put(21.0, bid=0.30, ask=0.34, delta=-0.18)
    chain = make_chain(
        good,
        make_put(20.0, bid=0.20, ask=0.24, delta=-0.10),  # delta too small
        make_put(23.0, bid=0.90, ask=1.00, delta=-0.35),  # delta too big
        make_put(21.5, bid=0.30, ask=0.60, delta=-0.20),  # spread too wide
        make_put(21.5, bid=0.40, ask=0.44, delta=-0.21, oi=10),  # illiquid
        make_put(21.5, bid=0.05, ask=0.06, delta=-0.21),  # bid too small
        make_put(21.0, bid=0.10, ask=0.12, delta=-0.18, expiry=date(2026, 10, 9)),  # too soon
        make_put(21.0, bid=0.90, ask=1.00, delta=-0.18, expiry=date(2027, 1, 15)),  # too far
    )
    picks = put_candidates(chain, HISTORY, today=TODAY, earnings=None, rules=PutFilter())
    assert [p.option for p in picks] == [good]


def test_put_candidates_skips_expiries_spanning_earnings():
    early = make_put(21.0, bid=0.30, ask=0.34, delta=-0.18, expiry=date(2026, 10, 23))
    late = make_put(21.0, bid=0.40, ask=0.44, delta=-0.18, expiry=date(2026, 11, 20))
    chain = make_chain(early, late)
    picks = put_candidates(chain, HISTORY, today=TODAY, earnings=date(2026, 11, 3), rules=PutFilter())
    assert [p.option for p in picks] == [early]
    # An earnings date already past doesn't block anything.
    picks = put_candidates(chain, HISTORY, today=TODAY, earnings=date(2026, 9, 1), rules=PutFilter())
    assert len(picks) == 2


def test_put_candidates_capital_cushion_and_delta_override():
    put = make_put(22.0, bid=0.60, ask=0.66, delta=-0.24)
    chain = make_chain(put)
    assert put_candidates(chain, HISTORY, today=TODAY, earnings=None, rules=PutFilter(max_capital=2000)) == []
    assert put_candidates(chain, HISTORY, today=TODAY, earnings=None, rules=PutFilter(min_cushion_sigmas=5)) == []
    assert put_candidates(chain, HISTORY, today=TODAY, earnings=None, rules=PutFilter(), delta_range=(0.3, 0.4)) == []


def test_put_candidates_ranked_by_score():
    low = make_put(20.5, bid=0.20, ask=0.22, delta=-0.15)
    high = make_put(21.5, bid=0.40, ask=0.44, delta=-0.22)
    picks = put_candidates(make_chain(low, high), HISTORY, today=TODAY, earnings=None, rules=PutFilter())
    assert [p.option for p in picks] == [high, low]


def test_iv_rank():
    history = [0.4 + 0.01 * i for i in range(21)]  # 0.40 – 0.60
    assert iv_rank(0.50, history) == pytest.approx(50)
    assert iv_rank(0.70, history) == 100
    assert iv_rank(0.50, history[:5]) is None
    assert iv_rank(0.50, [0.5] * 30) is None


def test_parse_earnings_date():
    assert parse_earnings_date({"data": {"announcement": "Earnings announcement* for KO: Oct 20, 2026"}}) == date(2026, 10, 20)
    assert parse_earnings_date({"data": {"announcement": "Earnings announcement* for GME: "}}) is None
    assert parse_earnings_date({}) is None
