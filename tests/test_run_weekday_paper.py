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


def test_parse_slots_default():
    slots = parse_slots(None)
    assert [(s.hour, s.minute) for s in slots] == [(10, 0), (12, 30), (15, 30)]


def test_parse_slots_custom():
    slots = parse_slots("09:15, 14:00")
    assert slots == (Slot(9, 15), Slot(14, 0))


def test_next_slot_same_weekday_afternoon():
    # Wednesday 2026-09-30 12:40 ET → next is 15:30 same day
    now = datetime(2026, 9, 30, 12, 40, tzinfo=ET)
    nxt = next_slot_after(now, parse_slots(None))
    assert nxt == datetime(2026, 9, 30, 15, 30, tzinfo=ET)


def test_next_slot_skips_weekend():
    # Friday after last slot → Monday 10:00
    now = datetime(2026, 10, 2, 16, 0, tzinfo=ET)  # Friday
    nxt = next_slot_after(now, parse_slots(None))
    assert nxt == datetime(2026, 10, 5, 10, 0, tzinfo=ET)  # Monday


def test_utc_cron_edt():
    assert utc_cron_for_eastern_slot(10, 0, edt=True) == "0 14 * * 1-5"
    assert utc_cron_for_eastern_slot(12, 30, edt=True) == "30 16 * * 1-5"
    assert utc_cron_for_eastern_slot(15, 30, edt=True) == "30 19 * * 1-5"
