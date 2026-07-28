from __future__ import annotations

import sys
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.core.market_clock import (
    NY,
    MarketClock,
    MarketState,
    easter,
    is_half_day,
    is_trading_day,
    last_weekday,
    next_trading_day,
    nth_weekday,
    nyse_half_days,
    nyse_holidays,
)
from src.sentinel.gate import GateDecision, decide

CLOCK = MarketClock()


def ny(year, month, day, hour, minute=0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=NY)


def utc(year, month, day, hour, minute=0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=timezone.utc)


def test_easter_dates():
    assert easter(2026) == date(2026, 4, 5)
    assert easter(2027) == date(2027, 3, 28)
    assert easter(2024) == date(2024, 3, 31)


def test_nth_weekday_finds_third_monday():
    assert nth_weekday(2026, 1, 0, 3) == date(2026, 1, 19)


def test_last_weekday_finds_last_monday_of_may():
    assert last_weekday(2026, 5, 0) == date(2026, 5, 25)


def test_last_weekday_handles_december():
    assert last_weekday(2026, 12, 4) == date(2026, 12, 25)


def test_2026_holiday_set_is_complete():
    holidays = nyse_holidays(2026)
    assert len(holidays) == 10
    for expected in (
        date(2026, 1, 1), date(2026, 1, 19), date(2026, 2, 16), date(2026, 4, 3),
        date(2026, 5, 25), date(2026, 6, 19), date(2026, 7, 3), date(2026, 9, 7),
        date(2026, 11, 26), date(2026, 12, 25),
    ):
        assert expected in holidays


def test_independence_day_moves_back_when_it_lands_on_saturday():
    assert date(2026, 7, 4).weekday() == 5
    assert date(2026, 7, 3) in nyse_holidays(2026)
    assert date(2026, 7, 4) not in nyse_holidays(2026)


def test_independence_day_moves_forward_when_it_lands_on_sunday():
    assert date(2027, 7, 4).weekday() == 6
    assert date(2027, 7, 5) in nyse_holidays(2027)


def test_new_year_on_saturday_is_not_observed():
    assert date(2022, 1, 1).weekday() == 5
    assert date(2021, 12, 31) not in nyse_holidays(2022)
    assert date(2022, 1, 1) not in nyse_holidays(2022)


def test_good_friday_is_a_holiday():
    assert date(2026, 4, 3) in nyse_holidays(2026)
    assert date(2027, 3, 26) in nyse_holidays(2027)


def test_half_days_exclude_dates_that_are_already_holidays():
    half = nyse_half_days(2026)
    assert date(2026, 11, 27) in half
    assert date(2026, 12, 24) in half
    assert date(2026, 7, 3) not in half


def test_trading_day_checks():
    assert is_trading_day(date(2026, 7, 28))
    assert not is_trading_day(date(2026, 7, 25))
    assert not is_trading_day(date(2026, 12, 25))


def test_next_trading_day_skips_weekend_and_holiday():
    assert next_trading_day(date(2026, 7, 24)) == date(2026, 7, 27)
    assert next_trading_day(date(2026, 12, 24)) == date(2026, 12, 28)


@pytest.mark.parametrize(
    "moment,expected",
    [
        (ny(2026, 7, 28, 3, 30), MarketState.CLOSED),
        (ny(2026, 7, 28, 4, 0), MarketState.PRE_MARKET),
        (ny(2026, 7, 28, 9, 29), MarketState.PRE_MARKET),
        (ny(2026, 7, 28, 9, 30), MarketState.REGULAR),
        (ny(2026, 7, 28, 15, 59), MarketState.REGULAR),
        (ny(2026, 7, 28, 16, 0), MarketState.AFTER_HOURS),
        (ny(2026, 7, 28, 19, 59), MarketState.AFTER_HOURS),
        (ny(2026, 7, 28, 20, 0), MarketState.CLOSED),
    ],
)
def test_state_transitions_across_a_regular_day(moment, expected):
    assert CLOCK.state(moment) is expected


def test_weekend_is_closed_all_day():
    assert CLOCK.state(ny(2026, 7, 25, 12, 0)) is MarketState.CLOSED


def test_holiday_is_closed_during_normal_hours():
    assert CLOCK.state(ny(2026, 12, 25, 11, 0)) is MarketState.CLOSED


def test_half_day_closes_at_one_pm():
    assert CLOCK.state(ny(2026, 11, 27, 12, 59)) is MarketState.REGULAR
    assert CLOCK.state(ny(2026, 11, 27, 13, 1)) is MarketState.AFTER_HOURS
    assert CLOCK.state(ny(2026, 11, 27, 17, 1)) is MarketState.CLOSED


def test_daylight_saving_shifts_the_utc_window():
    assert CLOCK.state(utc(2026, 7, 15, 14, 0)) is MarketState.REGULAR
    assert CLOCK.state(utc(2026, 1, 15, 14, 0)) is MarketState.PRE_MARKET
    assert CLOCK.state(utc(2026, 1, 15, 15, 0)) is MarketState.REGULAR


def test_naive_datetime_is_treated_as_utc():
    naive = datetime(2026, 7, 15, 14, 0)
    assert CLOCK.state(naive) is MarketState.REGULAR


def test_can_trade_only_during_regular_session():
    assert CLOCK.can_trade(ny(2026, 7, 28, 11, 0))
    assert not CLOCK.can_trade(ny(2026, 7, 28, 8, 0))
    assert not CLOCK.can_trade(ny(2026, 7, 28, 17, 0))


def test_can_analyze_outside_regular_but_not_when_closed():
    assert CLOCK.can_analyze(ny(2026, 7, 28, 8, 0))
    assert CLOCK.can_analyze(ny(2026, 7, 28, 17, 0))
    assert not CLOCK.can_analyze(ny(2026, 7, 25, 12, 0))


def test_next_open_same_day_before_the_bell():
    assert CLOCK.next_open(ny(2026, 7, 28, 6, 0)) == ny(2026, 7, 28, 9, 30)


def test_next_open_rolls_to_monday_after_friday_close():
    assert CLOCK.next_open(ny(2026, 7, 24, 21, 0)) == ny(2026, 7, 27, 9, 30)


def test_next_open_skips_christmas():
    assert CLOCK.next_open(ny(2026, 12, 24, 18, 0)) == ny(2026, 12, 28, 9, 30)


def test_seconds_until_open_is_zero_during_session():
    assert CLOCK.seconds_until_open(ny(2026, 7, 28, 11, 0)) == 0.0


def test_seconds_until_open_counts_the_gap():
    assert CLOCK.seconds_until_open(ny(2026, 7, 28, 8, 30)) == pytest.approx(3600.0)


def test_session_window_reports_half_day():
    window = CLOCK.session_window(ny(2026, 11, 27, 10, 0))
    assert window.half_day
    assert window.regular_close == ny(2026, 11, 27, 13, 0)


def test_gate_analyses_immediately_during_session():
    verdict = decide(MarketState.REGULAR, age_hours=0.5)
    assert verdict.decision is GateDecision.ANALYZE_NOW


def test_gate_queues_when_market_is_closed():
    verdict = decide(MarketState.CLOSED, age_hours=1.0)
    assert verdict.decision is GateDecision.QUEUE_FOR_OPEN


def test_gate_analyses_after_hours_so_the_decision_is_ready():
    verdict = decide(MarketState.AFTER_HOURS, age_hours=0.2)
    assert verdict.decision is GateDecision.ANALYZE_NOW


def test_gate_holds_an_already_analysed_event_until_the_open():
    verdict = decide(MarketState.PRE_MARKET, age_hours=8.0, already_queued=True)
    assert verdict.decision is GateDecision.QUEUE_FOR_OPEN


def test_gate_expires_events_older_than_the_queue_window():
    verdict = decide(MarketState.REGULAR, age_hours=19.0)
    assert verdict.decision is GateDecision.EXPIRE
    assert "priced in" in verdict.reason


def test_gate_expiry_wins_over_every_market_state():
    for state in MarketState:
        assert decide(state, age_hours=24.0).decision is GateDecision.EXPIRE
