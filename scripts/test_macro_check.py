"""Tests for macro_check.py — run from repo root: python -m pytest scripts"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

import pytest

from common import EMBED_TOTAL_LIMIT, MESSAGE_CONTENT_LIMIT, embed_size
from macro import BEAR, BULL, WATCH, Reading, Series, Signal
from macro_check import (
    CLEARED,
    CLEARED_LABEL,
    CLEARED_RECORD,
    NEW,
    NEW_YORK,
    RECORD,
    SIDE_STYLE,
    UNPOSTED,
    WORSE,
    WORSE_LABEL,
    Alert,
    Cleared,
    Group,
    Post,
    backtest,
    build_payload,
    closed_sessions,
    groups,
    parse_args,
    record_line,
    replay_intraday,
    score,
    session_steps,
    summary_rows,
    transitions,
    volume_line,
)
from test_macro import series

TODAY = date(2026, 9, 24)
SHOCK = Signal(
    "vol_shock", BEAR, "Volatility shock", ("VIX **32.0**",), "context", severity=1.07, readings=(Reading("VIX", 32.0, ".1f"), Reading("9-day/3-month", 1.1))
)
CREDIT = Signal("credit_stress", BEAR, "Credit stress", ("HYG/IEF **-4.0%**",), "context", severity=1.1)
WASHOUT = Signal("vol_capitulation", BULL, "Volatility capitulation", ("VIX spiked",), "context")
EARLY = Signal("stress_building", WATCH, "Stress building near the highs", ("VIX curve flattening",), "context", severity=1.02)
HEALING = Signal("credit_healing", BULL, "Credit healing", ("HYG/IEF **+3.0%** off its low",), "context")


def weekdays(start: date, n: int) -> tuple[date, ...]:
    days, day = [], start
    while len(days) < n:
        if day.weekday() < 5:
            days.append(day)
        day += timedelta(days=1)
    return tuple(days)


def daily(days: tuple[date, ...], values: list[float]) -> Series:
    return Series(days, tuple(float(v) for v in values), tuple(float(v) for v in values))


def run_days(sequence: list[list[Signal]], start: date = TODAY) -> list[tuple[list[Alert], list[Cleared]]]:
    """Feed one list of active signals per consecutive day through transitions."""
    state: dict = {}
    out = []
    for offset, signals in enumerate(sequence):
        alerts, cleared, state = transitions(signals, state, today=start + timedelta(days=offset), spy=700.0)
        out.append((alerts, cleared))
    return out


def test_transitions_alert_only_when_switching_on():
    alerts, cleared, state = transitions([SHOCK], {}, today=TODAY, spy=700.0)
    assert [(a.signal, a.kind, a.previous) for a in alerts] == [(SHOCK, NEW, None)]
    assert cleared == []
    assert state["asof"] == "2026-09-24"
    assert state["active"] == ["vol_shock"]
    assert state["fired"] == {"vol_shock": "2026-09-24"}
    assert state["last"]["vol_shock"] == {"day": "2026-09-24", "severity": 1.07, "spy": 700.0, "readings": {"VIX": 32.0, "9-day/3-month": 1.1}}
    again, _, _ = transitions([SHOCK], state, today=date(2026, 9, 25))
    assert again == []


def test_transitions_cooldown_suppresses_flicker():
    state = {"active": [], "fired": {"vol_shock": "2026-09-20"}}
    assert transitions([SHOCK], state, today=TODAY)[0] == []
    assert [a.kind for a in transitions([SHOCK], state, today=date(2026, 10, 1))[0]] == [NEW]


def test_transitions_reads_state_saved_before_sessions_were_tracked():
    old = {"active": ["vol_normalizing"], "fired": {"vol_normalizing": "2026-09-20", "vol_shock": "2026-09-15"}}
    alerts, cleared, state = transitions([SHOCK], old, today=TODAY)
    assert (alerts, cleared) == ([], [])  # still cooling, and nothing to compare a worse reading against
    assert state["active"] == ["vol_shock"]


def test_transitions_realerts_when_materially_worse():
    worse = replace(SHOCK, severity=1.07 * 1.25)
    slightly = replace(SHOCK, severity=1.07 * 1.2)
    first, second, third, fourth = run_days([[SHOCK], [slightly], [worse], [worse]])
    assert [a.kind for a in first[0]] == [NEW]
    assert second[0] == []
    assert [(a.kind, a.previous["severity"], a.previous["day"]) for a in third[0]] == [(WORSE, 1.07, "2026-09-24")]
    assert fourth[0] == []  # the bar moves up to the reading it last alerted at


def test_transitions_worse_reading_breaks_through_the_cooldown():
    worse = replace(SHOCK, severity=2.0)
    days = run_days([[SHOCK], [], [], [worse]])
    assert [a.kind for a in days[3][0]] == [WORSE]


def test_transitions_bottom_signals_never_escalate():
    stronger = replace(WASHOUT, severity=5.0)
    days = run_days([[WASHOUT], [stronger]])
    assert days[1][0] == []


def test_early_warning_alerts_once_and_is_not_a_crash_alarm():
    days = run_days([[EARLY], [replace(EARLY, severity=3.0)], [EARLY], []])
    assert [a.kind for a in days[0][0]] == [NEW]
    assert days[1][0] == []  # only crash alarms re-alert when they get worse
    assert [c for _, c in days] == [[], [], [], []]  # and only they open an episode to clear


def test_early_warning_does_not_hold_a_cleared_note_back():
    days = run_days([[SHOCK], [SHOCK, EARLY], [EARLY]])
    assert [c.key for c in days[2][1]] == ["vol_shock"]


def test_transitions_prunes_old_fired_dates():
    stale = {"active": ["vol_shock"], "fired": {"vol_shock": "2025-01-01"}, "last": {"vol_shock": {"day": "2025-01-01", "severity": 1.0}}}
    _, _, state = transitions([], stale, today=TODAY)
    assert (state["active"], state["fired"], state["last"]) == ([], {}, {})


def test_cleared_note_once_the_last_alarm_goes_quiet():
    days = run_days([[SHOCK, CREDIT], [SHOCK, CREDIT], [CREDIT], []])
    assert [c for _, c in days[:3]] == [[], [], []]  # credit stress is still on
    assert days[3][1] == [Cleared("vol_shock", "Volatility shock", TODAY, 2), Cleared("credit_stress", "Credit stress", TODAY, 3)]


def test_no_cleared_note_for_a_single_close():
    assert run_days([[SHOCK], []])[1][1] == []


def test_no_cleared_note_when_a_bottom_signal_spoke_for_the_episode():
    assert run_days([[SHOCK], [SHOCK, WASHOUT], [SHOCK], []])[3][1] == []
    assert run_days([[SHOCK], [SHOCK], [WASHOUT]])[2][1] == []


def test_an_unposted_bottom_signal_does_not_hold_the_cleared_note_back():
    assert "credit_healing" in UNPOSTED
    days = run_days([[CREDIT], [CREDIT], [HEALING]])
    assert [c.key for c in days[2][1]] == ["credit_stress"]  # nobody heard from credit healing, so the episode still gets its note


def test_cleared_note_is_not_repeated_by_flicker():
    days = run_days([[SHOCK], [SHOCK], [], [SHOCK], []])
    assert len(days[2][1]) == 1
    assert (days[3][0], days[4][1]) == ([], [])  # back on inside the cooldown: no alert, so no second note


def test_missing_feed_is_not_an_alarm_clearing():
    state: dict = {}
    for offset in range(2):
        _, _, state = transitions([SHOCK], state, today=TODAY + timedelta(days=offset))
    _, cleared, state = transitions([], state, today=TODAY + timedelta(days=2), unknown={"vol_shock"})
    assert cleared == []
    assert state["active"] == []  # not on, so a real switch-on later can still alert
    assert list(state["open"]) == ["vol_shock"]
    _, cleared, state = transitions([], state, today=TODAY + timedelta(days=3), unknown={"credit_stress"})
    assert [c.key for c in cleared] == ["vol_shock"]
    assert state["open"] == {}


def test_closed_sessions_wait_for_the_close():
    days = weekdays(date(2026, 11, 2), 5)  # Mon Nov 2 – Fri Nov 6
    market = {"SPY": daily(days, [700] * 5), "VIX": daily(days, [15] * 5)}
    thursday = days[3]

    def at(hour: int, minute: int, day: date = thursday) -> datetime:
        return datetime(day.year, day.month, day.day, hour, minute, tzinfo=NEW_YORK)

    assert closed_sessions(market, at(15, 46))[-1] == days[2]  # 14 minutes before the close
    assert closed_sessions(market, at(16, 10))[-1] == days[2]  # the VIX is still printing
    assert closed_sessions(market, at(16, 27))[-1] == thursday
    assert closed_sessions(market, at(20, 42).astimezone(UTC))[-1] == thursday  # 01:42 UTC the next day
    assert closed_sessions(market, at(18, 0, date(2026, 11, 8)))[-1] == days[4]  # Sunday evening
    assert closed_sessions({**market, "VIX": daily(days[:3], [15] * 3)}, at(17, 0))[-1] == days[2]  # VIX bar not in yet


def shock_market(n: int = 30, spike_days: int = 1) -> dict[str, Series]:
    days = weekdays(date(2026, 9, 28), n)
    vix = [15.0] * (n - spike_days) + [32.0] * spike_days
    return {"SPY": daily(days, [700.0] * n), "VIX": daily(days, vix)}


def test_session_steps_post_nothing_until_the_close():
    market = shock_market()
    last = market["SPY"].dates[-1]
    state = {"asof": market["SPY"].dates[-2].isoformat(), "active": [], "fired": {}}
    during = datetime(last.year, last.month, last.day, 15, 46, tzinfo=NEW_YORK)
    assert list(session_steps(market, state, during)) == []
    after = datetime(last.year, last.month, last.day, 16, 27, tzinfo=NEW_YORK)
    (step,) = session_steps(market, state, after)
    assert step.day == last
    assert step.posted == [f"{last} new vol_shock"]
    assert step.state["asof"] == last.isoformat()
    assert f"close of {last:%a %b} {last.day}" in step.payload["content"]
    assert "sent late" not in step.payload["content"]
    assert list(session_steps(market, step.state, after + timedelta(hours=3))) == []  # already processed


def test_session_steps_catch_up_on_missed_closes():
    market = shock_market(spike_days=2)
    days = market["SPY"].dates
    state = {"asof": days[-3].isoformat(), "active": [], "fired": {}}
    now = datetime(days[-1].year, days[-1].month, days[-1].day, 17, 0, tzinfo=NEW_YORK)
    first, second = session_steps(market, state, now)
    assert (first.day, second.day) == (days[-2], days[-1])
    assert "sent late" in first.payload["content"]
    assert second.payload is None  # still on, already alerted


def test_session_steps_start_from_the_latest_close_without_history():
    market = shock_market(spike_days=3)
    days = market["SPY"].dates
    now = datetime(days[-1].year, days[-1].month, days[-1].day, 17, 0, tzinfo=NEW_YORK)
    (step,) = session_steps(market, {"active": ["vol_shock"], "fired": {"vol_shock": days[-3].isoformat()}}, now)
    assert (step.day, step.payload) == (days[-1], None)


def test_session_steps_skip_posting_closes_missed_long_ago():
    market = shock_market(spike_days=6)
    days = market["SPY"].dates
    state = {"asof": days[-7].isoformat(), "active": [], "fired": {}}
    now = datetime(days[-1].year, days[-1].month, days[-1].day, 17, 0, tzinfo=NEW_YORK)
    steps = list(session_steps(market, state, now))
    assert len(steps) == 6
    assert all(step.payload is None for step in steps)  # the switch-on was six closes ago
    assert steps[-1].state["active"] == ["vol_shock"]


def test_unposted_signal_is_tracked_but_never_sent():
    n = 24
    days = weekdays(date(2026, 9, 28), n)
    hyg = [80.0] * 20 + [76.0, 75.0, 76.0, 77.5]  # credit stress at three closes, then credit healing
    market = {
        "SPY": daily(days, [700.0] * n),
        "VIX": daily(days, [15.0] * n),
        "HYG": daily(days, hyg),
        "IEF": daily(days, [95.0] * n),
        "HYOAS": daily(days, [3.0] * n),
    }
    state: dict = {"asof": days[-5].isoformat(), "active": [], "fired": {}}
    steps = []
    for day in days[-4:]:
        (step,) = session_steps(market, state, datetime(day.year, day.month, day.day, 17, 0, tzinfo=NEW_YORK))
        steps.append(step)
        state = step.state
    assert [s.posted for s in steps] == [[f"{days[-4]} new credit_stress"], [], [], [f"{days[-1]} cleared credit_stress"]]
    assert state["active"] == ["credit_healing"]
    assert "credit_healing" in state["fired"]  # its cooldown still runs, as the backtest scores it
    last = steps[-1].payload
    assert last["embeds"] == []
    assert "Credit healing" not in last["content"]
    assert "⚪ No crash alarm is on any more: **Credit stress** (on at 3 closes" in last["content"]


def test_build_payload_lists_active_and_guidance():
    market = {"SPY": series(700, 690, 650), "VIX": series(15, 25, 32)}
    payload = build_payload([Alert(SHOCK, NEW)], [], [SHOCK, WASHOUT], market, day=date(2026, 9, 24))
    content = payload["content"]
    assert content.startswith("**Market alarms** · close of Thu Sep 24")
    assert "🔴 Active: Volatility shock" in content
    assert "🟢 Active: Volatility capitulation" in content
    assert SIDE_STYLE[BEAR][3] in content
    assert SIDE_STYLE[BULL][3] not in content  # guidance only for sides that just alerted
    embed = payload["embeds"][0]
    assert embed["title"] == "🔴 Crash alarm — Volatility shock"
    assert "📜 context" in embed["description"]
    assert f"🧮 {RECORD['vol_shock']}" in embed["description"]
    assert "⏱ Day 1 of this alarm · on at 1 of the last 20 closes" in embed["description"]
    assert "↕️" not in embed["description"]  # nothing to compare a first alert with


def test_build_payload_marks_an_early_warning_as_its_own_kind():
    market = {"SPY": series(700, 690, 686), "VIX": series(15, 17, 19)}
    payload = build_payload([Alert(EARLY, NEW)], [], [EARLY], market, day=date(2026, 9, 24))
    assert payload["embeds"][0]["title"] == "🟠 Early warning — Stress building near the highs"
    assert "🟠 Active: Stress building near the highs" in payload["content"]
    assert SIDE_STYLE[WATCH][3] in payload["content"]
    assert SIDE_STYLE[BEAR][3] not in payload["content"]
    assert "⏱" not in payload["embeds"][0]["description"]  # day counts are for crash alarms
    assert f"🧮 {RECORD['stress_building']}" in payload["embeds"][0]["description"]


def test_build_payload_compares_with_the_last_alert():
    market = {"SPY": series(700, 690, 665), "VIX": series(15, 25, 32)}
    previous = {"day": "2026-09-10", "severity": 0.8, "spy": 700.0, "readings": {"VIX": 22.2, "9-day/3-month": 1.05}}
    worse = build_payload([Alert(SHOCK, WORSE, previous)], [], [SHOCK], market, day=date(2026, 9, 24))["embeds"][0]
    assert worse["title"] == "🔴 Crash alarm, worse — Volatility shock"
    assert "↕️ Worse than the Sep 10 alert: VIX 22.2 → 32.0 · 9-day/3-month 1.05 → 1.10 · SPY -5.0%" in worse["description"]
    assert f"🧮 {RECORD[WORSE_LABEL]}" in worse["description"]
    refire = build_payload([Alert(SHOCK, NEW, previous)], [], [SHOCK], market, day=date(2026, 9, 24))["embeds"][0]
    assert "↕️ Since the Sep 10 alert: VIX 22.2 → 32.0" in refire["description"]
    assert refire["title"] == "🔴 Crash alarm — Volatility shock"


def test_build_payload_cleared_note_needs_no_embed():
    market = {"SPY": series(700, 690, 650), "VIX": series(15, 25, 18)}
    cleared = [Cleared("vol_shock", "Volatility shock", date(2026, 9, 21), 2), Cleared("credit_stress", "Credit stress", date(2026, 9, 18), 1)]
    payload = build_payload([], cleared, [], market, day=date(2026, 9, 24))
    assert payload["embeds"] == []
    assert "⚪ No crash alarm is on any more: **Volatility shock** (on at 2 closes from Sep 21), **Credit stress** (on at 1 close from Sep 18)" in payload["content"]
    assert "not an all-clear" in payload["content"]
    assert "Active:" not in payload["content"]


def test_build_payload_stays_within_discord_limits():
    market = {"SPY": series(700, 690, 650), "VIX": series(15, 25, 32)}
    wordy = [Alert(replace(SHOCK, key=f"k{i}", lines=("x" * 3000,)), NEW) for i in range(12)]
    cleared = [Cleared(f"k{i}", "A long alarm title " * 5, date(2026, 9, 21), 3) for i in range(30)]
    payload = build_payload(wordy, cleared, [a.signal for a in wordy], market, day=date(2026, 9, 24))
    assert len(payload["content"]) <= MESSAGE_CONTENT_LIMIT
    assert 0 < len(payload["embeds"]) <= 10
    assert sum(embed_size(e) for e in payload["embeds"]) <= EMBED_TOTAL_LIMIT


def test_every_posted_signal_states_its_record():
    from macro import SIGNALS

    assert UNPOSTED < set(SIGNALS)
    assert set(RECORD) == (set(SIGNALS) - UNPOSTED) | {WORSE_LABEL}
    assert all(text.startswith("Since ") for text in (*RECORD.values(), CLEARED_RECORD))


def test_side_and_record_are_one_short_line_each():
    """Read on a phone: the guidance and the record are a line apiece, not paragraphs."""
    assert all("\n" not in text and len(text) <= 120 for text in (*RECORD.values(), CLEARED_RECORD))
    assert all("\n" not in style[3] and len(style[3]) <= 170 for style in SIDE_STYLE.values())


def test_backtest_replays_daily_with_cooldown():
    market = {"SPY": series(*[700] * 6), "VIX": series(15, 32, 15, 33, 15, 15)}
    posts = backtest(market, date(2026, 1, 1))
    assert [(p.day.day, p.key, p.kind) for p in posts] == [(2, "vol_shock", NEW)]  # the day-4 repeat is inside the cooldown


def test_backtest_posts_escalations_and_clearings():
    calm = series(*[15] * 7)
    market = {"SPY": series(*[700] * 7), "VIX": series(15, 31, 32, 40, 41, 15, 15), "VIX9D": calm, "VIX3M": calm}
    posts = backtest(market, date(2026, 1, 1))
    assert [(p.day.day, p.kind) for p in posts] == [(2, NEW), (4, WORSE), (6, CLEARED)]
    assert posts[-1].title == "Crash alarms cleared: Volatility shock"


def test_score_measures_the_fall_around_an_alert():
    closes = (100.0, 90.0, 85.0, 80.0, 88.0) + (90.0,) * 60
    outcome = score(closes, 1, date(2026, 1, 2))
    assert outcome.off_high == pytest.approx(-10.0)
    assert outcome.to_trough == pytest.approx((80 / 90 - 1) * 100)
    assert outcome.days_to_trough == 2
    assert outcome.drop_done == pytest.approx(50.0)  # 10 of the 20 points from 100 to 80
    assert outcome.forward[0] == pytest.approx(0.0)  # five days on: back at 90
    assert outcome.drawdown[0] == pytest.approx((80 / 90 - 1) * 100)
    assert outcome.false_alarm is False


def test_score_measures_the_rebound_already_gone():
    closes = (100.0, 80.0, 90.0) + (100.0,) * 60
    outcome = score(closes, 2, date(2026, 1, 3))
    assert (outcome.off_low, outcome.days_from_low) == (pytest.approx(12.5), 1)
    assert outcome.upside == pytest.approx((100 / 90 - 1) * 100)
    assert outcome.rebound_missed == pytest.approx(50.0)  # 10 of the 20 points from 80 to 100
    assert outcome.new_low is False
    assert outcome.false_alarm is True


def test_score_leaves_out_horizons_past_the_data():
    outcome = score((100.0, 99.0, 98.0), 1, date(2026, 1, 2))
    assert outcome.forward == (None, None, None)
    assert (outcome.to_trough, outcome.upside, outcome.new_low, outcome.false_alarm, outcome.drop_done) == (None, None, None, None, None)


def test_summary_compares_each_signal_with_any_day():
    n = 200
    days = tuple(date(2020, 1, 1) + timedelta(days=i) for i in range(n))
    spy = Series(days, tuple(100.0 + i for i in range(n)))
    market = {"SPY": spy, "VIX": Series(days, (15.0,) * n), "HYG": Series(days, (80.0,) * n), "IEF": Series(days, (95.0,) * n)}
    posts = [
        Post(days[100], NEW, "vol_shock", BEAR, "Volatility shock"),
        Post(days[110], WORSE, "vol_shock", BEAR, "Volatility shock"),
        Post(days[120], NEW, "credit_healing", BULL, "Credit healing"),
    ]
    found = groups(market, posts, days[60], days[-1])
    by_label = {g.label: g for g in found}
    assert len(by_label["vol_shock"].hits) == 1
    assert len(by_label[WORSE_LABEL].hits) == 1
    assert by_label[CLEARED_LABEL].hits == []
    assert "funding_stress" not in by_label  # no SOFR in this market
    rows = summary_rows(found, BEAR)
    assert rows[0][:2] == ["vol_shock", "1"]
    assert rows[1][0].startswith("  any day since 2020-0")
    assert rows[0][3] == "+2.5"  # 5 points on 200 five days later
    assert record_line(by_label["vol_shock"]).startswith("Since 2020 (1 alerts): 0% below the 3-month high, ")
    assert record_line(by_label[CLEARED_LABEL]) is None
    assert [r[:2] for r in summary_rows(found, BULL) if "credit_healing" in r[0]] == [["credit_healing (not posted)", "1"]]
    assert "Posts: 2 on 2 of 140 sessions (1.4%)" in volume_line(spy, posts, days[60], days[-1])  # credit healing is not posted


def test_record_line_for_an_early_warning_counts_the_falls_that_followed():
    fell = score((100.0,) * 60 + (95.0,) * 70, 59, date(2020, 3, 2))  # a 5% fall within 20 days
    held = score(tuple(100.0 + i for i in range(130)), 60, date(2021, 3, 2))
    group = Group("stress_building", WATCH, [fell, held], [held], date(2011, 2, 1), date(2026, 11, 13))
    assert record_line(group) == "Since 2011 (2 alerts): 0% below the 3-month high; 50% saw a further 3% fall within 20 days (typical 0%)."


def test_replay_intraday_flags_triggers_gone_by_the_close():
    days = weekdays(date(2026, 10, 5), 10)
    market = {"SPY": daily(days, [700.0] * 10), "VIX": daily(days, [15.0] * 10)}
    day = days[-1]

    def bar(key_hour: int, minute: int, close: float) -> tuple[datetime, float, float]:
        return (datetime(day.year, day.month, day.day, key_hour, minute, tzinfo=NEW_YORK).astimezone(UTC), close, close)

    intraday = {
        "VIX": (bar(9, 0, 16.0), bar(12, 0, 31.0), bar(13, 0, 17.0)),  # over 30 only for the bar ending 13:00
        "SPY": (bar(9, 30, 699.0), bar(12, 30, 690.0), bar(13, 30, 698.0)),
    }
    triggers, covered = replay_intraday(market, intraday)
    assert covered == [day]
    by_key = {t.signal.key: t for t in triggers}
    assert set(by_key) == {"vol_shock", "vol_capitulation"}  # the spike, then its reversal an hour later
    shock = by_key["vol_shock"]
    assert shock.held is False
    assert shock.at.astimezone(NEW_YORK).hour == 13
    assert shock.spy_to_close == pytest.approx((700 / 690 - 1) * 100)  # from SPY's bar that ended at 13:30
    assert by_key["vol_capitulation"].at.astimezone(NEW_YORK).hour == 14


def test_parse_args():
    assert parse_args([]).backtest is None
    assert parse_args(["--backtest"]).backtest == date(1990, 1, 1)
    args = parse_args(["--backtest", "2018-01-01", "--split", "2022-01-01", "--alerts"])
    assert (args.backtest, args.split, args.alerts) == (date(2018, 1, 1), date(2022, 1, 1), True)
