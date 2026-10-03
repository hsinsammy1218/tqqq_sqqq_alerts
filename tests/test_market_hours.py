from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from market_hours import (
    US_EASTERN,
    cron_skip_reason,
    is_us_equity_market_open,
    lunch_blackout_reason,
    market_closed_reason,
)


def _et(y: int, m: int, d: int, h: int, minute: int = 0) -> datetime:
    return datetime(y, m, d, h, minute, tzinfo=US_EASTERN)


def test_open_mid_session_weekday():
    assert is_us_equity_market_open(_et(2026, 5, 19, 10, 0))


def test_closed_weekend():
    assert market_closed_reason(_et(2026, 5, 16, 10, 0)) == "weekend"


def test_closed_before_open():
    reason = market_closed_reason(_et(2026, 5, 19, 9, 0))
    assert reason is not None
    assert "before market open" in reason


def test_closed_after_regular_close():
    reason = market_closed_reason(_et(2026, 5, 19, 16, 0))
    assert reason is not None
    assert "after market close" in reason


def test_closed_nyse_holiday():
    reason = market_closed_reason(_et(2026, 1, 1, 12, 0))
    assert reason == "NYSE holiday"


def test_early_close_day_afternoon():
    reason = market_closed_reason(_et(2026, 12, 24, 14, 0))
    assert reason is not None
    assert "after market close" in reason
    assert is_us_equity_market_open(_et(2026, 12, 24, 12, 0))


def test_lunch_blackout_noon_hour():
    assert lunch_blackout_reason(_et(2026, 5, 19, 12, 0)) is not None
    assert lunch_blackout_reason(_et(2026, 5, 19, 12, 30)) is not None
    assert lunch_blackout_reason(_et(2026, 5, 19, 12, 59)) is not None
    assert lunch_blackout_reason(_et(2026, 5, 19, 13, 0)) is None
    assert lunch_blackout_reason(_et(2026, 5, 19, 11, 45)) is None


def test_lunch_does_not_mean_market_closed():
    # Market is open at noon; lunch is a separate scan-policy skip.
    assert is_us_equity_market_open(_et(2026, 5, 19, 12, 15))
    assert market_closed_reason(_et(2026, 5, 19, 12, 15)) is None


def test_cron_skip_prefers_market_closed_then_lunch():
    assert cron_skip_reason(_et(2026, 5, 16, 12, 0)) == "weekend"
    assert "lunch" in (cron_skip_reason(_et(2026, 5, 19, 12, 15)) or "")
    assert cron_skip_reason(_et(2026, 5, 19, 10, 0)) is None
    assert cron_skip_reason(_et(2026, 5, 19, 13, 0)) is None
