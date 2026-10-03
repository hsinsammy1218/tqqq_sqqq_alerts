"""Unit tests for local weekday paper scheduler helpers (no network)."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from weekday_paper_schedule import (
    Slot,
    next_slot_after,
    parse_slots,
    utc_cron_for_eastern_slot,
)

ET = ZoneInfo("America/New_York")

# Morning 9:45–11:45 + afternoon 13:00–15:45 every 15m (lunch skipped).
DEFAULT_HM = [
    (9, 45),
    (10, 0),
    (10, 15),
    (10, 30),
    (10, 45),
    (11, 0),
    (11, 15),
    (11, 30),
    (11, 45),
    (13, 0),
    (13, 15),
    (13, 30),
    (13, 45),
    (14, 0),
    (14, 15),
    (14, 30),
    (14, 45),
    (15, 0),
    (15, 15),
    (15, 30),
    (15, 45),
]


def test_parse_slots_default():
    slots = parse_slots(None)
    assert [(s.hour, s.minute) for s in slots] == DEFAULT_HM
    # No lunch-hour slots.
    assert all(not (12 <= h < 13) for h, _m in DEFAULT_HM)


def test_parse_slots_custom():
    slots = parse_slots("09:15, 14:00")
    assert slots == (Slot(9, 15), Slot(14, 0))


def test_next_slot_same_weekday_afternoon():
    # Wednesday 2026-09-30 12:40 ET (lunch) → next is 13:00 same day
    now = datetime(2026, 9, 30, 12, 40, tzinfo=ET)
    nxt = next_slot_after(now, parse_slots(None))
    assert nxt == datetime(2026, 9, 30, 13, 0, tzinfo=ET)


def test_next_slot_quarter_hour():
    # Wednesday 2026-09-30 10:05 ET → next is 10:15 same day
    now = datetime(2026, 9, 30, 10, 5, tzinfo=ET)
    nxt = next_slot_after(now, parse_slots(None))
    assert nxt == datetime(2026, 9, 30, 10, 15, tzinfo=ET)


def test_next_slot_after_1145_skips_lunch():
    now = datetime(2026, 9, 30, 11, 50, tzinfo=ET)
    nxt = next_slot_after(now, parse_slots(None))
    assert nxt == datetime(2026, 9, 30, 13, 0, tzinfo=ET)


def test_next_slot_at_1545():
    now = datetime(2026, 9, 30, 15, 45, tzinfo=ET)
    nxt = next_slot_after(now, parse_slots(None))
    assert nxt == datetime(2026, 9, 30, 15, 45, tzinfo=ET)


def test_next_slot_skips_weekend():
    # Friday after last slot → Monday 9:45
    now = datetime(2026, 10, 2, 16, 0, tzinfo=ET)  # Friday
    nxt = next_slot_after(now, parse_slots(None))
    assert nxt == datetime(2026, 10, 5, 9, 45, tzinfo=ET)  # Monday


def test_utc_cron_edt():
    assert utc_cron_for_eastern_slot(9, 45, edt=True) == "45 13 * * 1-5"
    assert utc_cron_for_eastern_slot(11, 45, edt=True) == "45 15 * * 1-5"
    assert utc_cron_for_eastern_slot(13, 0, edt=True) == "0 17 * * 1-5"
    assert utc_cron_for_eastern_slot(15, 45, edt=True) == "45 19 * * 1-5"
