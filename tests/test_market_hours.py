from __future__ import annotations

from datetime import datetime, timezone
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


def _utc(y: int, m: int, d: int, h: int, minute: int = 0) -> datetime:
    return datetime(y, m, d, h, minute, tzinfo=timezone.utc)


def test_edt_regular_session_and_est_same_utc_clock():
    # 14:00 UTC in July is 10:00 EDT (open). In January it is 09:00 EST (pre-open).
    assert cron_skip_reason(_utc(2026, 7, 15, 14, 0)) is None
    pre = cron_skip_reason(_utc(2026, 1, 14, 14, 0))
    assert pre is not None and "before market open" in pre
    # 20:30 UTC is 16:30 EDT (closed) and 15:30 EST (still open).
    after = cron_skip_reason(_utc(2026, 7, 15, 20, 30))
    assert after is not None and "after market close" in after
    assert cron_skip_reason(_utc(2026, 1, 14, 20, 30)) is None


def test_est_regular_session_midmorning():
    assert is_us_equity_market_open(_utc(2026, 1, 14, 15, 30))
    assert cron_skip_reason(_et(2026, 1, 14, 10, 30)) is None


def test_weekend_and_holiday_block_even_inside_utc_cron_window():
    assert cron_skip_reason(_utc(2026, 5, 16, 15, 0)) == "weekend"
    assert cron_skip_reason(_utc(2026, 1, 1, 16, 0)) == "NYSE holiday"


def test_premarket_after_close_lunch_and_regular():
    assert "before market open" in (cron_skip_reason(_et(2026, 5, 19, 9, 15)) or "")
    assert "after market close" in (cron_skip_reason(_et(2026, 5, 19, 16, 15)) or "")
    assert "lunch" in (cron_skip_reason(_et(2026, 5, 19, 12, 30)) or "")
    assert cron_skip_reason(_et(2026, 5, 19, 10, 15)) is None


def test_early_close_christmas_eve_and_black_friday():
    eve = cron_skip_reason(_et(2026, 12, 24, 14, 0))
    assert eve is not None and "after market close" in eve
    assert cron_skip_reason(_et(2026, 12, 24, 11, 0)) is None
    # 2026-11-27 is the Friday after Thanksgiving.
    black = cron_skip_reason(_et(2026, 11, 27, 14, 0))
    assert black is not None and "after market close" in black
    assert is_us_equity_market_open(_et(2026, 11, 27, 11, 0))
    # 2025-07-03 is the Thursday before a Friday July 4.
    july = cron_skip_reason(_et(2025, 7, 3, 14, 0))
    assert july is not None and "after market close" in july
