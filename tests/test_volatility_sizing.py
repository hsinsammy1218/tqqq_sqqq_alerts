"""Volatility sizing is off unless ALPACA_PAPER_VOL_SIZING is set. No network."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from alpaca_paper import (
    PAPER_TRADING_BASE_URL,
    execute_paper_orders,
    select_buy_notional,
    stop_distance_pct,
    volatility_notional_usd,
)
from config import ConfigError, load_settings
from strategy_types import AlertDecision


def _alert() -> AlertDecision:
    return AlertDecision(
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


def test_volatility_notional_risk_over_stop_then_cap():
    assert stop_distance_pct(mode="stop_pct", stop_loss_pct=0.08) == pytest.approx(0.08)
    assert stop_distance_pct(mode="atr", stop_loss_pct=0.08, atr=2.0, atr_price=100.0, atr_mult=2.0) == pytest.approx(
        0.04
    )
    raw = volatility_notional_usd(equity=100_000, risk_fraction=0.01, stop_distance=0.08, notional_cap=50_000)
    assert raw == pytest.approx(12_500)
    capped = volatility_notional_usd(equity=100_000, risk_fraction=0.01, stop_distance=0.08, notional_cap=500)
    assert capped == pytest.approx(500)


def test_select_buy_notional_defaults_to_fixed_path():
    notional, mode = select_buy_notional(
        vol_sizing=False,
        fixed_notional=200,
        equity_pct=0,
        equity=100_000,
    )
    assert notional == 200
    assert mode == "fixed_notional"

    sized, sized_mode = select_buy_notional(
        vol_sizing=True,
        fixed_notional=20_000,
        equity_pct=0.5,
        equity=100_000,
        risk_fraction=0.01,
        stop_loss_pct=0.08,
        vol_stop_mode="stop_pct",
    )
    assert sized == pytest.approx(12_500)
    assert sized_mode == "volatility"

    fallback, fallback_mode = select_buy_notional(
        vol_sizing=True,
        fixed_notional=200,
        equity_pct=0,
        equity=None,
        risk_fraction=0.01,
        stop_loss_pct=0.08,
    )
    assert fallback == 200
    assert fallback_mode == "fallback_notional"


def test_load_settings_vol_sizing_defaults_off(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ALPACA_API_KEY", "test-key")
    monkeypatch.setenv("ALPACA_API_SECRET", "test-secret")
    monkeypatch.setenv("ALPACA_PAPER_VOL_SIZING", "false")
    monkeypatch.setenv("ALPACA_PAPER_RISK_FRACTION", "0.0075")
    monkeypatch.setenv("EXIT_MODE", "fixed")
    settings = load_settings()
    assert settings.alpaca_paper_vol_sizing is False
    assert settings.alpaca_paper_risk_fraction == pytest.approx(0.0075)
    assert settings.alpaca_paper_vol_stop == "stop_pct"
    assert settings.exit_mode == "fixed"
    assert settings.atr_trail_mult == pytest.approx(2.0)


def test_load_settings_rejects_oversized_risk_and_bad_exit_mode(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ALPACA_API_KEY", "test-key")
    monkeypatch.setenv("ALPACA_API_SECRET", "test-secret")
    monkeypatch.setenv("ALPACA_PAPER_RISK_FRACTION", "0.5")
    with pytest.raises(ConfigError, match="ALPACA_PAPER_RISK_FRACTION"):
        load_settings()
    monkeypatch.setenv("ALPACA_PAPER_RISK_FRACTION", "0.01")
    monkeypatch.setenv("EXIT_MODE", "martingale")
    with pytest.raises(ConfigError, match="EXIT_MODE"):
        load_settings()


def test_execute_paper_orders_vol_sizing_changes_qty(tmp_path):
    session = MagicMock()

    def fake_request(method, url, headers=None, json=None, timeout=None):
        resp = MagicMock()
        resp.status_code = 200
        if url.endswith("/v2/account"):
            resp.text = '{"equity":"100000"}'
            resp.json.return_value = {"equity": "100000"}
        elif url.endswith("/v2/orders") and method == "POST":
            # 100000 * 0.01 / 0.08 = 12500, under the 20000 cap, price 100 → qty 125.
            assert json["qty"] == "125"
            assert json["side"] == "buy"
            resp.text = '{"id":"ord-vol","status":"accepted"}'
            resp.json.return_value = {"id": "ord-vol", "status": "accepted"}
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
        _alert(),
        None,
        paper_trading=True,
        dry_run=False,
        api_key="k",
        api_secret="s",
        trading_base_url=PAPER_TRADING_BASE_URL,
        data_base_url="https://data.alpaca.markets",
        feed="iex",
        fixed_notional=20_000,
        equity_pct=0,
        limit_offset_bps=10,
        logger=MagicMock(),
        session=session,
        trade_log_path=tmp_path / "trades.jsonl",
        vol_sizing=True,
        risk_fraction=0.01,
        vol_stop_mode="stop_pct",
        stop_loss_pct=0.08,
    )
    assert len(results) == 1
    assert results[0].ok is True
    assert results[0].payload["qty"] == "125"
