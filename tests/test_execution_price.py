"""Limit selection from bid/ask/mid freshness. No network."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from execution_price import limit_from_quote_or_trade

NOW = datetime(2026, 7, 15, 15, 0, tzinfo=timezone.utc)


def _limit(**kwargs):
    base = dict(
        side="buy",
        bid=99.90,
        ask=100.00,
        trade=100.0,
        quote_time=NOW - timedelta(seconds=5),
        trade_time=NOW - timedelta(seconds=5),
        now=NOW,
        offset_bps=10,
        max_spread_bps=100,
        max_quote_age_seconds=180,
        max_trade_age_seconds=900,
    )
    base.update(kwargs)
    return limit_from_quote_or_trade(**base)


def test_normal_quote_buy_at_ask_sell_at_bid():
    buy = _limit(side="buy")
    sell = _limit(side="sell")
    assert buy.mode == "ask"
    assert buy.limit_price == "100.00"
    assert buy.blocked is False
    assert sell.mode == "bid"
    assert sell.limit_price == "99.90"


def test_wide_spread_blocks_buy_and_sells_at_bid():
    buy = _limit(bid=90.0, ask=110.0)
    sell = _limit(side="sell", bid=90.0, ask=110.0)
    assert buy.blocked is True
    assert "spread" in buy.reason
    assert sell.blocked is False
    assert sell.limit_price == "90.00"
    assert sell.mode == "bid"


def test_stale_quote_blocks_buy_even_with_fresh_trade():
    buy = _limit(quote_time=NOW - timedelta(minutes=30))
    assert buy.blocked is True
    assert "stale" in buy.reason
    sell = _limit(side="sell", quote_time=NOW - timedelta(minutes=30))
    assert sell.blocked is False
    assert sell.mode == "trade_fallback"
    assert sell.limit_price == "99.90"


def test_missing_quote_falls_back_to_trade_offset():
    buy = _limit(bid=None, ask=None, quote_time=None)
    sell = _limit(side="sell", bid=None, ask=None, quote_time=None)
    assert buy.blocked is False
    assert buy.mode == "trade_fallback"
    assert buy.limit_price == "100.10"
    assert sell.limit_price == "99.90"


def test_missing_quote_and_trade_blocks_buy():
    buy = _limit(bid=None, ask=None, quote_time=None, trade=None, trade_time=None)
    assert buy.blocked is True
    sell = _limit(side="sell", bid=None, ask=None, quote_time=None, trade=None, trade_time=None)
    assert sell.blocked is True


def test_malformed_crossed_quote_blocks_buy():
    buy = _limit(bid=101.0, ask=100.0)
    assert buy.blocked is True
    assert "malformed" in buy.reason
    sell = _limit(side="sell", bid=101.0, ask=100.0)
    assert sell.blocked is False
    assert sell.mode == "trade_fallback"


def test_malformed_non_positive_quote_blocks_buy():
    buy = _limit(bid=0.0, ask=100.0)
    assert buy.blocked is True
    assert "malformed" in buy.reason or "missing" in buy.reason
