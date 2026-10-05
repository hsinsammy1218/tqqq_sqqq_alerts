"""4h aggregation must start at 09:30 America/New_York, not UTC midnight."""

from __future__ import annotations

from zoneinfo import ZoneInfo

import pandas as pd

from data import aggregate_four_hour_utc_clock, aggregate_session_four_hour

ET = ZoneInfo("America/New_York")


def _session_hours() -> pd.DataFrame:
    idx = pd.date_range("2026-07-15 09:30", periods=7, freq="1h", tz=ET)
    frame = pd.DataFrame(
        {
            "open": list(range(1, 8)),
            "high": list(range(2, 9)),
            "low": list(range(1, 8)),
            "close": list(range(1, 8)),
            "volume": [100] * 7,
        },
        index=idx.tz_convert("UTC"),
    )
    return frame


def test_legacy_utc_clock_bins_are_not_0930_et():
    legacy = aggregate_four_hour_utc_clock(_session_hours())
    labels = [ts.tz_convert(ET).strftime("%H:%M") for ts in legacy.index]
    assert labels == ["08:00", "12:00"]


def test_session_bins_anchor_at_0930_and_1330_et():
    session = aggregate_session_four_hour(_session_hours())
    labels = [ts.tz_convert(ET).strftime("%H:%M") for ts in session.index]
    assert labels == ["09:30", "13:30"]
    morning = session.iloc[0]
    afternoon = session.iloc[1]
    assert float(morning["open"]) == 1
    assert float(morning["close"]) == 4
    assert float(morning["volume"]) == 400
    assert float(afternoon["open"]) == 5
    assert float(afternoon["close"]) == 7
    assert float(afternoon["volume"]) == 300


def test_premarket_hour_is_dropped():
    idx = pd.DatetimeIndex(
        [
            pd.Timestamp("2026-01-14 08:30", tz=ET),
            pd.Timestamp("2026-01-14 09:30", tz=ET),
            pd.Timestamp("2026-01-14 10:30", tz=ET),
            pd.Timestamp("2026-01-14 11:30", tz=ET),
            pd.Timestamp("2026-01-14 12:30", tz=ET),
        ]
    ).tz_convert("UTC")
    frame = pd.DataFrame(
        {
            "open": [9, 1, 2, 3, 4],
            "high": [9, 1, 2, 3, 4],
            "low": [9, 1, 2, 3, 4],
            "close": [9, 1, 2, 3, 4],
            "volume": [50, 10, 10, 10, 10],
        },
        index=idx,
    )
    session = aggregate_session_four_hour(frame)
    assert len(session) == 1
    assert float(session.iloc[0]["open"]) == 1
    assert float(session.iloc[0]["volume"]) == 40
    assert session.index[0].tz_convert(ET).strftime("%H:%M") == "09:30"
