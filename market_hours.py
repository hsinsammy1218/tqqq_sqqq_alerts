"""US equity regular-session hours (NYSE calendar, America/New_York)."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
from pandas.tseries.holiday import (
    AbstractHolidayCalendar,
    Holiday,
    GoodFriday,
    USLaborDay,
    USMartinLutherKingJr,
    USMemorialDay,
    USPresidentsDay,
    USThanksgivingDay,
    nearest_workday,
    sunday_to_monday,
)

US_EASTERN = ZoneInfo("America/New_York")
MARKET_OPEN = time(9, 30)
REGULAR_CLOSE = time(16, 0)
EARLY_CLOSE = time(13, 0)
# Cron / scan policy: skip 12:00–12:59 ET (lunch). Not a market-closed state —
# the session is open; we simply do not want scans in this window.
# EDT = UTC-4 → 16:00–16:59 UTC; winter EST = UTC-5 → 17:00–17:59 UTC.
LUNCH_BLACKOUT_START = time(12, 0)
LUNCH_BLACKOUT_END = time(13, 0)


class NYSEHolidayCalendar(AbstractHolidayCalendar):
    rules = [
        Holiday("New Year's Day", month=1, day=1, observance=sunday_to_monday),
        USMartinLutherKingJr,
        USPresidentsDay,
        GoodFriday,
        USMemorialDay,
        Holiday("Juneteenth", month=6, day=19, observance=nearest_workday),
        Holiday("Independence Day", month=7, day=4, observance=nearest_workday),
        USLaborDay,
        USThanksgivingDay,
        Holiday("Christmas", month=12, day=25, observance=sunday_to_monday),
    ]


_holiday_cache: dict[int, set[date]] = {}


def _nyse_holidays(year: int) -> set[date]:
    if year not in _holiday_cache:
        cal = NYSEHolidayCalendar()
        idx = cal.holidays(start=f"{year}-01-01", end=f"{year}-12-31")
        _holiday_cache[year] = {pd.Timestamp(ts).date() for ts in idx}
    return _holiday_cache[year]


def _thanksgiving(year: int) -> date:
    """Fourth Thursday of November (NYSE Thanksgiving)."""
    first = date(year, 11, 1)
    offset = (3 - first.weekday()) % 7
    return first + timedelta(days=offset + 21)


def _is_early_close_day(d: date) -> bool:
    if d.weekday() >= 5:
        return False
    if d in _nyse_holidays(d.year):
        return False
    if d.month == 12 and d.day == 24:
        return True
    if d == _thanksgiving(d.year) + timedelta(days=1):
        return True
    if d.month == 7 and d.day == 3 and d.weekday() < 5:
        july4 = date(d.year, 7, 4)
        if july4.weekday() in (1, 2, 3, 4):
            return True
    return False


def market_close_time_et(d: date) -> time:
    if _is_early_close_day(d):
        return EARLY_CLOSE
    return REGULAR_CLOSE


def _format_time_et(t: time) -> str:
    hour = t.hour % 12 or 12
    suffix = "AM" if t.hour < 12 else "PM"
    minute = f":{t.minute:02d}" if t.minute else ""
    return f"{hour}{minute} {suffix} ET"


def market_closed_reason(now: datetime | None = None) -> str | None:
    if now is None:
        now = datetime.now(US_EASTERN)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=US_EASTERN)
    else:
        now = now.astimezone(US_EASTERN)

    d = now.date()
    if d.weekday() >= 5:
        return "weekend"
    if d in _nyse_holidays(d.year):
        return "NYSE holiday"

    close_t = market_close_time_et(d)
    open_dt = datetime.combine(d, MARKET_OPEN, US_EASTERN)
    close_dt = datetime.combine(d, close_t, US_EASTERN)

    if now < open_dt:
        return f"before market open ({_format_time_et(MARKET_OPEN)})"
    if now >= close_dt:
        return f"after market close ({_format_time_et(close_t)})"
    return None


def is_us_equity_market_open(now: datetime | None = None) -> bool:
    return market_closed_reason(now) is None


def lunch_blackout_reason(now: datetime | None = None) -> str | None:
    """Return a skip reason when ``now`` falls in the 12:00–12:59 ET lunch window.

    Used by the ``--market-hours-only`` cron start path so a single broad UTC
    cron (``*/15 13-21 * * 1-5``, covering both EDT and EST) can no-op at noon
    without a second service. A cron fire is not permission to trade: callers
    must use ``cron_skip_reason``. Wall-clock is always America/New_York.
    """
    if now is None:
        now = datetime.now(US_EASTERN)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=US_EASTERN)
    else:
        now = now.astimezone(US_EASTERN)

    t = now.time()
    if LUNCH_BLACKOUT_START <= t < LUNCH_BLACKOUT_END:
        return "lunch blackout (12:00–1:00 PM ET)"
    return None


def cron_skip_reason(now: datetime | None = None) -> str | None:
    """Combined skip for cron runs: market closed, then lunch blackout."""
    closed = market_closed_reason(now)
    if closed is not None:
        return closed
    return lunch_blackout_reason(now)
