from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from alpaca_paper import (
    PAPER_TRADING_BASE_URL,
    OrderIntent,
    build_limit_order_payload,
    build_order_intents,
    buy_notional_usd,
    execute_paper_orders,
    is_live_trading_host,
    limit_price_from_trade,
    qty_for_notional,
    should_submit_paper_orders,
)
from config import ConfigError, load_settings
from strategy_types import AlertDecision, PositionState


def _alert(**kwargs) -> AlertDecision:
    base = dict(
        alert_type="BUY",
        symbol="TQQQ",
        qqq_trend_reason="regime=bull",
        bullish_score=70,
        bearish_score=20,
        confidence_score=80,
        entry_zone_low=1.0,
        entry_zone_high=2.0,
        stop_loss=0.9,
        take_profit=2.5,
        stretch_take_profit=3.0,
        max_hold_date="2026-10-10",
        timestamp="2026-09-30T00:00:00Z",
        notes="test",
    )
    base.update(kwargs)
    return AlertDecision(**base)


def test_should_submit_paper_orders_gating():
    assert should_submit_paper_orders(paper_trading=True, dry_run=False) is True
    assert should_submit_paper_orders(paper_trading=False, dry_run=False) is False
    assert should_submit_paper_orders(paper_trading=True, dry_run=True) is False
    assert should_submit_paper_orders(paper_trading=False, dry_run=True) is False


def test_is_live_trading_host():
    assert is_live_trading_host(PAPER_TRADING_BASE_URL) is False
    assert is_live_trading_host("https://paper-api.alpaca.markets/") is False
    assert is_live_trading_host("https://api.alpaca.markets") is True
    assert is_live_trading_host("https://api.alpaca.markets/v2") is True


def test_build_order_intents_buy_sell_flip():
    buy = build_order_intents(_alert(alert_type="BUY", symbol="TQQQ"), None)
    assert buy == [OrderIntent(symbol="TQQQ", side="buy", purpose="entry")]

    sell = build_order_intents(_alert(alert_type="SELL", symbol="SQQQ"), None)
    assert sell == [OrderIntent(symbol="SQQQ", side="sell", purpose="exit")]

    flip = build_order_intents(
        _alert(alert_type="FLIP", symbol="SQQQ"),
        PositionState(active_symbol="TQQQ"),
    )
    assert flip == [
        OrderIntent(symbol="TQQQ", side="sell", purpose="flip_exit"),
        OrderIntent(symbol="SQQQ", side="buy", purpose="flip_entry"),
    ]

    assert build_order_intents(_alert(alert_type="CASH", symbol="CASH"), None) == []


def test_limit_order_payload_shape():
    intent = OrderIntent(symbol="TQQQ", side="buy", purpose="entry")
    payload = build_limit_order_payload(intent, qty="1.25", limit_price="72.15")
    assert payload == {
        "symbol": "TQQQ",
        "qty": "1.25",
        "side": "buy",
        "type": "limit",
        "time_in_force": "day",
        "limit_price": "72.15",
    }


def test_qty_and_limit_helpers():
    assert qty_for_notional(500, 100) == "5"
    assert float(qty_for_notional(500, 72.15)) == pytest.approx(6.93, abs=0.001)
    assert limit_price_from_trade(100.0, side="buy", offset_bps=10) == "100.10"
    assert limit_price_from_trade(100.0, side="sell", offset_bps=10) == "99.90"
    assert buy_notional_usd(fixed_notional=500, equity_pct=0, equity=100_000) == 500
    assert buy_notional_usd(fixed_notional=500, equity_pct=0.02, equity=100_000) == 2000


def test_load_settings_rejects_live_trading_host(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ALPACA_API_KEY", "test-key")
    monkeypatch.setenv("ALPACA_API_SECRET", "test-secret")
    monkeypatch.setenv("ALPACA_TRADING_BASE_URL", "https://api.alpaca.markets")
    with pytest.raises(ConfigError, match="Live Alpaca trading host is not enabled"):
        load_settings()


def test_load_settings_paper_defaults(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ALPACA_API_KEY", "test-key")
    monkeypatch.setenv("ALPACA_API_SECRET", "test-secret")
    monkeypatch.delenv("ALPACA_PAPER_TRADING", raising=False)
    monkeypatch.delenv("ALPACA_TRADING_BASE_URL", raising=False)
    settings = load_settings()
    assert settings.alpaca_paper_trading is False
    assert settings.alpaca_trading_base_url == PAPER_TRADING_BASE_URL
    assert settings.alpaca_paper_notional == 500
    assert settings.alpaca_paper_equity_pct == 0


def test_execute_paper_orders_skipped_when_dry_run(capfd):
    results = execute_paper_orders(
        _alert(alert_type="BUY", symbol="TQQQ"),
        None,
        paper_trading=True,
        dry_run=True,
        api_key="k",
        api_secret="s",
        trading_base_url=PAPER_TRADING_BASE_URL,
        data_base_url="https://data.alpaca.markets",
        feed="iex",
        fixed_notional=500,
        equity_pct=0,
        limit_offset_bps=10,
        logger=MagicMock(),
    )
    assert results == []
    out = capfd.readouterr().out
    assert "DRY_RUN=true" in out


def test_execute_paper_orders_skipped_when_flag_off(capfd):
    results = execute_paper_orders(
        _alert(alert_type="BUY", symbol="TQQQ"),
        None,
        paper_trading=False,
        dry_run=False,
        api_key="k",
        api_secret="s",
        trading_base_url=PAPER_TRADING_BASE_URL,
        data_base_url="https://data.alpaca.markets",
        feed="iex",
        fixed_notional=500,
        equity_pct=0,
        limit_offset_bps=10,
        logger=MagicMock(),
    )
    assert results == []
    assert "ALPACA_PAPER_TRADING=false" in capfd.readouterr().out


def test_execute_paper_orders_buy_path_mocked():
    session = MagicMock()

    def fake_request(method, url, headers=None, json=None, timeout=None):
        resp = MagicMock()
        resp.status_code = 200
        if url.endswith("/v2/account"):
            resp.text = '{"equity":"100000"}'
            resp.json.return_value = {"equity": "100000"}
        elif url.endswith("/v2/orders") and method == "POST":
            assert json["symbol"] == "TQQQ"
            assert json["side"] == "buy"
            assert json["type"] == "limit"
            assert json["time_in_force"] == "day"
            assert json["qty"] == "5"
            assert json["limit_price"] == "100.10"
            resp.text = '{"id":"ord-1","status":"accepted"}'
            resp.json.return_value = {"id": "ord-1", "status": "accepted"}
        else:
            raise AssertionError(f"unexpected {method} {url}")
        return resp

    def fake_get(url, headers=None, params=None, timeout=None):
        resp = MagicMock()
        if "/trades/latest" in url:
            resp.status_code = 200
            resp.text = '{"trade":{"p":100.0}}'
            resp.json.return_value = {"trade": {"p": 100.0}}
            return resp
        raise AssertionError(f"unexpected GET {url}")

    session.request.side_effect = fake_request
    session.get.side_effect = fake_get

    results = execute_paper_orders(
        _alert(alert_type="BUY", symbol="TQQQ"),
        None,
        paper_trading=True,
        dry_run=False,
        api_key="k",
        api_secret="s",
        trading_base_url=PAPER_TRADING_BASE_URL,
        data_base_url="https://data.alpaca.markets",
        feed="iex",
        fixed_notional=500,
        equity_pct=0,
        limit_offset_bps=10,
        logger=MagicMock(),
        session=session,
    )
    assert len(results) == 1
    assert results[0].ok is True
    assert results[0].order_id == "ord-1"
    assert results[0].payload["type"] == "limit"
    assert results[0].payload["side"] == "buy"


def test_execute_paper_orders_flip_mocked():
    session = MagicMock()
    calls: list[tuple[str, str]] = []

    def fake_request(method, url, headers=None, json=None, timeout=None):
        calls.append((method, url))
        resp = MagicMock()
        resp.status_code = 200
        if url.endswith("/v2/account"):
            resp.text = '{"equity":"50000"}'
            resp.json.return_value = {"equity": "50000"}
        elif "/trades/latest" in url:
            resp.text = '{"trade":{"p":50.0}}'
            resp.json.return_value = {"trade": {"p": 50.0}}
        elif url.endswith("/v2/orders") and method == "POST":
            resp.text = '{"id":"ord-x","status":"accepted"}'
            resp.json.return_value = {"id": "ord-x", "status": "accepted"}
        else:
            raise AssertionError(f"unexpected {method} {url}")
        return resp

    def fake_get(url, headers=None, params=None, timeout=None):
        resp = MagicMock()
        if url.endswith("/v2/positions/TQQQ"):
            resp.status_code = 200
            resp.text = '{"qty":"2"}'
            resp.json.return_value = {"qty": "2"}
            return resp
        if "/trades/latest" in url:
            # fetch_latest_trade_price uses session.get
            resp.status_code = 200
            resp.text = '{"trade":{"p":50.0}}'
            resp.json.return_value = {"trade": {"p": 50.0}}
            return resp
        raise AssertionError(f"unexpected GET {url}")

    session.request.side_effect = fake_request
    session.get.side_effect = fake_get

    results = execute_paper_orders(
        _alert(alert_type="FLIP", symbol="SQQQ"),
        PositionState(active_symbol="TQQQ"),
        paper_trading=True,
        dry_run=False,
        api_key="k",
        api_secret="s",
        trading_base_url=PAPER_TRADING_BASE_URL,
        data_base_url="https://data.alpaca.markets",
        feed="iex",
        fixed_notional=500,
        equity_pct=0,
        limit_offset_bps=10,
        logger=MagicMock(),
        session=session,
    )
    assert len(results) == 2
    assert results[0].intent.side == "sell" and results[0].intent.symbol == "TQQQ"
    assert results[1].intent.side == "buy" and results[1].intent.symbol == "SQQQ"
    assert all(r.ok for r in results)


def test_execute_paper_orders_sell_skips_without_position(capfd):
    session = MagicMock()

    def fake_request(method, url, headers=None, json=None, timeout=None):
        resp = MagicMock()
        resp.status_code = 200
        if url.endswith("/v2/account"):
            resp.text = '{"equity":"10000"}'
            resp.json.return_value = {"equity": "10000"}
            return resp
        raise AssertionError(f"unexpected {method} {url}")

    def fake_get(url, headers=None, params=None, timeout=None):
        resp = MagicMock()
        resp.status_code = 404
        resp.text = "not found"
        return resp

    session.request.side_effect = fake_request
    session.get.side_effect = fake_get

    results = execute_paper_orders(
        _alert(alert_type="SELL", symbol="TQQQ"),
        PositionState(active_symbol="TQQQ"),
        paper_trading=True,
        dry_run=False,
        api_key="k",
        api_secret="s",
        trading_base_url=PAPER_TRADING_BASE_URL,
        data_base_url="https://data.alpaca.markets",
        feed="iex",
        fixed_notional=500,
        equity_pct=0,
        limit_offset_bps=10,
        logger=MagicMock(),
        session=session,
    )
    assert len(results) == 1
    assert results[0].status == "skipped"
    assert "No Alpaca paper position" in capfd.readouterr().out
