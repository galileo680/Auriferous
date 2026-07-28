from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from enum import Enum
from functools import lru_cache
from zoneinfo import ZoneInfo

import structlog

NY = ZoneInfo("America/New_York")

PRE_MARKET_OPEN = time(4, 0)
REGULAR_OPEN = time(9, 30)
REGULAR_CLOSE = time(16, 0)
AFTER_HOURS_CLOSE = time(20, 0)

HALF_DAY_CLOSE = time(13, 0)
HALF_DAY_AFTER_HOURS_CLOSE = time(17, 0)


class MarketState(str, Enum):
    CLOSED = "CLOSED"
    PRE_MARKET = "PRE_MARKET"
    REGULAR = "REGULAR"
    AFTER_HOURS = "AFTER_HOURS"

    @property
    def can_trade(self) -> bool:
        return self is MarketState.REGULAR

    @property
    def can_analyze(self) -> bool:
        return self is not MarketState.CLOSED


def easter(year: int) -> date:
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month, day = divmod(h + l - 7 * m + 114, 31)
    return date(year, month, day + 1)


def nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    first = date(year, month, 1)
    offset = (weekday - first.weekday()) % 7
    return first + timedelta(days=offset + 7 * (n - 1))


def last_weekday(year: int, month: int, weekday: int) -> date:
    if month == 12:
        last = date(year, 12, 31)
    else:
        last = date(year, month + 1, 1) - timedelta(days=1)
    offset = (last.weekday() - weekday) % 7
    return last - timedelta(days=offset)


def observed(day: date) -> date | None:
    if day.weekday() == 5:
        return day - timedelta(days=1)
    if day.weekday() == 6:
        return day + timedelta(days=1)
    return day


@lru_cache(maxsize=16)
def nyse_holidays(year: int) -> frozenset[date]:
    days: set[date] = set()

    new_year = date(year, 1, 1)
    if new_year.weekday() != 5:
        days.add(observed(new_year))

    days.add(nth_weekday(year, 1, 0, 3))
    days.add(nth_weekday(year, 2, 0, 3))
    days.add(easter(year) - timedelta(days=2))
    days.add(last_weekday(year, 5, 0))

    for holiday in (date(year, 6, 19), date(year, 7, 4), date(year, 12, 25)):
        moved = observed(holiday)
        if moved is not None:
            days.add(moved)

    days.add(nth_weekday(year, 9, 0, 1))
    days.add(nth_weekday(year, 11, 3, 4))

    return frozenset(days)


@lru_cache(maxsize=16)
def nyse_half_days(year: int) -> frozenset[date]:
    holidays = nyse_holidays(year)
    days: set[date] = set()

    independence_eve = date(year, 7, 3)
    if independence_eve.weekday() < 5 and independence_eve not in holidays:
        days.add(independence_eve)

    days.add(nth_weekday(year, 11, 3, 4) + timedelta(days=1))

    christmas_eve = date(year, 12, 24)
    if christmas_eve.weekday() < 5 and christmas_eve not in holidays:
        days.add(christmas_eve)

    return frozenset(days)


def is_trading_day(day: date) -> bool:
    if day.weekday() >= 5:
        return False
    return day not in nyse_holidays(day.year)


def is_half_day(day: date) -> bool:
    return day in nyse_half_days(day.year)


def next_trading_day(day: date) -> date:
    candidate = day + timedelta(days=1)
    while not is_trading_day(candidate):
        candidate += timedelta(days=1)
    return candidate


@dataclass(frozen=True)
class SessionWindow:
    state: MarketState
    day: date
    regular_open: datetime
    regular_close: datetime
    half_day: bool


class MarketClock:

    def __init__(self) -> None:
        self._logger = structlog.get_logger("MarketClock")

    def now_ny(self, moment: datetime | None = None) -> datetime:
        if moment is None:
            return datetime.now(NY)
        if moment.tzinfo is None:
            return moment.replace(tzinfo=ZoneInfo("UTC")).astimezone(NY)
        return moment.astimezone(NY)

    def state(self, moment: datetime | None = None) -> MarketState:
        local = self.now_ny(moment)
        day = local.date()

        if not is_trading_day(day):
            return MarketState.CLOSED

        current = local.time()
        half = is_half_day(day)
        close = HALF_DAY_CLOSE if half else REGULAR_CLOSE
        after_close = HALF_DAY_AFTER_HOURS_CLOSE if half else AFTER_HOURS_CLOSE

        if current < PRE_MARKET_OPEN:
            return MarketState.CLOSED
        if current < REGULAR_OPEN:
            return MarketState.PRE_MARKET
        if current < close:
            return MarketState.REGULAR
        if current < after_close:
            return MarketState.AFTER_HOURS
        return MarketState.CLOSED

    def can_trade(self, moment: datetime | None = None) -> bool:
        return self.state(moment).can_trade

    def can_analyze(self, moment: datetime | None = None) -> bool:
        return self.state(moment).can_analyze

    def next_open(self, moment: datetime | None = None) -> datetime:
        local = self.now_ny(moment)
        day = local.date()

        if is_trading_day(day) and local.time() < REGULAR_OPEN:
            target = day
        else:
            target = next_trading_day(day)

        return datetime.combine(target, REGULAR_OPEN, tzinfo=NY)

    def seconds_until_open(self, moment: datetime | None = None) -> float:
        local = self.now_ny(moment)
        if self.state(moment) is MarketState.REGULAR:
            return 0.0
        return max((self.next_open(moment) - local).total_seconds(), 0.0)

    def session_window(self, moment: datetime | None = None) -> SessionWindow:
        local = self.now_ny(moment)
        day = local.date()
        half = is_half_day(day)
        close = HALF_DAY_CLOSE if half else REGULAR_CLOSE

        return SessionWindow(
            state=self.state(moment),
            day=day,
            regular_open=datetime.combine(day, REGULAR_OPEN, tzinfo=NY),
            regular_close=datetime.combine(day, close, tzinfo=NY),
            half_day=half,
        )
