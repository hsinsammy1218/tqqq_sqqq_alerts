from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from market_hours import US_EASTERN, is_us_equity_market_open, market_closed_reason


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
