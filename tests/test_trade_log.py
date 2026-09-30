from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from alpaca_paper import PAPER_TRADING_BASE_URL, execute_paper_orders
from strategy_types import AlertDecision, PositionState
from trade_log import (
    append_trade_record,
    build_trade_record,
    extract_regime,
    read_trade_records,
)


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
        signal_quality="HIGH",
    )
    base.update(kwargs)
    return AlertDecision(**base)


def test_extract_regime():
    assert extract_regime("regime=bull") == "bull"
    assert extract_regime("QQQ up, regime=range chop") == "range"
    assert extract_regime("no regime tag") is None


def test_append_trade_record_grows_jsonl(tmp_path: Path):
    path = tmp_path / "trades.jsonl"
    first = build_trade_record(
        symbol="TQQQ",
        side="buy",
        qty="1",
        limit_price="78.64",
        fill_price="78.64",
        order_id="ord-a",
        status="filled",
        alert_type="BUY",
        confidence=80,
        regime="bull",
        paper_trading=True,
        dry_run=False,
        source="manual_test",
    )
    append_trade_record(path, first)
    append_trade_record(
        path,
        build_trade_record(
            symbol="TQQQ",
            side="sell",
            qty="1",
            limit_price="77.08",
            order_id="ord-b",
            status="filled",
            source="manual_test",
            paper_trading=True,
            dry_run=False,
        ),
    )
    rows = read_trade_records(path)
    assert len(rows) == 2
    assert rows[0]["order_id"] == "ord-a"
    assert rows[0]["source"] == "manual_test"
    assert rows[0]["confidence"] == 80
    assert rows[1]["side"] == "sell"
    assert path.read_text(encoding="utf-8").count("\n") == 2


def test_execute_paper_orders_appends_trade_log(tmp_path: Path):
    log_path = tmp_path / "trades.jsonl"
    session = MagicMock()

    def fake_request(method, url, headers=None, json=None, timeout=None):
        resp = MagicMock()
        resp.status_code = 200
        if url.endswith("/v2/account"):
            resp.text = '{"equity":"100000"}'
            resp.json.return_value = {"equity": "100000"}
        elif url.endswith("/v2/orders") and method == "POST":
            resp.text = '{"id":"ord-1","status":"filled","filled_avg_price":"100.10","filled_qty":"5"}'
            resp.json.return_value = {
                "id": "ord-1",
                "status": "filled",
                "filled_avg_price": "100.10",
                "filled_qty": "5",
            }
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

    assert not log_path.exists()
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
        trade_log_path=log_path,
        source="strategy",
    )
    assert len(results) == 1
    assert results[0].ok is True
    rows = read_trade_records(log_path)
    assert len(rows) == 1
    row = rows[0]
    assert row["symbol"] == "TQQQ"
    assert row["side"] == "buy"
    assert row["qty"] == "5"
    assert row["limit_price"] == "100.10"
    assert row["fill_price"] == "100.10"
    assert row["order_id"] == "ord-1"
    assert row["status"] == "filled"
    assert row["alert_type"] == "BUY"
    assert row["confidence"] == 80
    assert row["regime"] == "bull"
    assert row["paper_trading"] is True
    assert row["dry_run"] is False
    assert row["source"] == "strategy"


def test_execute_paper_orders_logs_error_and_skipped(tmp_path: Path):
    log_path = tmp_path / "trades.jsonl"
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
        trade_log_path=log_path,
        source="manual_test",
    )
    assert results[0].status == "skipped"
    rows = read_trade_records(log_path)
    assert len(rows) == 1
    assert rows[0]["status"] == "skipped"
    assert rows[0]["source"] == "manual_test"
    assert rows[0]["error"] == "no_position"
