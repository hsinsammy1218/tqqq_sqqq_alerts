"""Stale intraday bar gate. No network."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from alpaca_paper import OrderIntent
from market_data_gate import assess_intraday_freshness, filter_intents_for_stale_data

NOW = datetime(2026, 7, 15, 18, 0, tzinfo=timezone.utc)


def test_fresh_bar_still_forming():
    start = NOW - timedelta(minutes=20)
    result = assess_intraday_freshness(start, NOW, max_age_minutes=90)
    assert result.state == "FRESH"
    assert result.blocks_new_exposure() is False


def test_stale_bar_blocks_buys_keeps_sell():
    start = NOW - timedelta(hours=5)
    result = assess_intraday_freshness(start, NOW, max_age_minutes=90)
    assert result.state == "STALE"
    intents = [
        OrderIntent(symbol="TQQQ", side="sell", purpose="exit"),
        OrderIntent(symbol="SQQQ", side="buy", purpose="flip_entry"),
    ]
    kept, skipped = filter_intents_for_stale_data(intents, result)
    assert kept == [intents[0]]
    assert len(skipped) == 1
    assert "flip_entry" in skipped[0]


def test_missing_timestamp_is_unknown():
    result = assess_intraday_freshness(None, NOW, max_age_minutes=90)
    assert result.state == "UNKNOWN"
    assert result.blocks_new_exposure() is True


def test_zero_max_age_disables_gate():
    result = assess_intraday_freshness(None, NOW, max_age_minutes=0)
    assert result.state == "FRESH"
