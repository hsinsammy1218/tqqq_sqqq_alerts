"""Unit tests for trade_log_store (file + mocked Supabase HTTP)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from config import ConfigError
from trade_log import build_trade_record
from trade_log_report import run_trade_log_report
from trade_log_store import (
    DualTradeLogStore,
    FileTradeLogStore,
    SupabaseTradeLogStore,
    TradeLogStoreError,
    parse_supabase_trade_log_payload,
    record_to_supabase_row,
    supabase_row_to_record,
    trade_log_store_from_settings,
)


def _filled_buy(**kwargs):
    base = dict(
        symbol="TQQQ",
        side="buy",
        status="filled",
        source="strategy",
        qty="1",
        fill_price="100.10",
        filled_qty="1",
        order_id="ord-1",
        alert_type="BUY",
        confidence=80,
        regime="bull",
        paper_trading=True,
        dry_run=False,
        timestamp="2026-09-30T14:00:00Z",
    )
    base.update(kwargs)
    return build_trade_record(**base)


def test_record_to_supabase_row_maps_fields():
    row = record_to_supabase_row(_filled_buy(), bot_id="default")
    assert row["bot_id"] == "default"
    assert row["symbol"] == "TQQQ"
    assert row["side"] == "buy"
    assert row["order_id"] == "ord-1"
    assert row["confidence"] == 80
    assert row["paper_trading"] is True
    assert "id" not in row
    assert "created_at" not in row


def test_record_to_supabase_row_requires_timestamp():
    with pytest.raises(TradeLogStoreError, match="missing timestamp"):
        record_to_supabase_row({"symbol": "TQQQ"}, bot_id="default")


def test_supabase_row_roundtrip_strips_db_columns():
    record = supabase_row_to_record(
        {
            "id": "uuid-here",
            "bot_id": "default",
            "created_at": "2026-09-30T14:00:01Z",
            "timestamp": "2026-09-30T14:00:00Z",
            "symbol": "TQQQ",
            "side": "buy",
            "status": "filled",
            "source": "strategy",
            "confidence": 80,
        }
    )
    assert "id" not in record
    assert "bot_id" not in record
    assert "created_at" not in record
    assert record["symbol"] == "TQQQ"
    assert record["confidence"] == 80


def test_parse_supabase_trade_log_payload_ok():
    rows = parse_supabase_trade_log_payload(
        [{"timestamp": "2026-09-30T14:00:00Z", "symbol": "TQQQ", "status": "filled", "source": "strategy"}]
    )
    assert len(rows) == 1
    assert rows[0]["symbol"] == "TQQQ"


def test_parse_supabase_trade_log_payload_errors():
    with pytest.raises(TradeLogStoreError, match="no data payload"):
        parse_supabase_trade_log_payload(None)
    with pytest.raises(TradeLogStoreError, match="unexpected payload type"):
        parse_supabase_trade_log_payload({"symbol": "TQQQ"})
    with pytest.raises(TradeLogStoreError, match="was not an object"):
        parse_supabase_trade_log_payload(["x"])


def test_file_trade_log_store_append_and_read(tmp_path: Path):
    path = tmp_path / "trades.jsonl"
    store = FileTradeLogStore(path)
    store.append(_filled_buy())
    store.append(_filled_buy(side="sell", order_id="ord-2", alert_type="SELL"))
    rows = store.read_all()
    assert len(rows) == 2
    assert rows[0]["order_id"] == "ord-1"
    assert store.describe() == str(path)


def test_supabase_store_insert(monkeypatch: pytest.MonkeyPatch):
    captured: dict[str, object] = {}

    class _Query:
        def insert(self, row):
            captured["row"] = row
            return self

        def execute(self):
            return SimpleNamespace(data=[captured["row"]])

    class _Client:
        def table(self, name: str):
            captured["table"] = name
            return _Query()

    monkeypatch.setattr("trade_log_store.create_supabase_client", lambda: _Client())
    store = SupabaseTradeLogStore("default")
    store.append(_filled_buy())
    assert captured["table"] == "bot_trade_log"
    assert captured["row"]["bot_id"] == "default"
    assert captured["row"]["order_id"] == "ord-1"
    assert captured["row"]["symbol"] == "TQQQ"
    assert store.describe() == "supabase:bot_trade_log:default"


def test_supabase_store_insert_raises(monkeypatch: pytest.MonkeyPatch):
    class _Query:
        def insert(self, *_args, **_kwargs):
            return self

        def execute(self):
            raise RuntimeError("insert failed")

    class _Client:
        def table(self, _name: str):
            return _Query()

    monkeypatch.setattr("trade_log_store.create_supabase_client", lambda: _Client())
    with pytest.raises(TradeLogStoreError, match="Failed to insert"):
        SupabaseTradeLogStore("default").append(_filled_buy())


def test_supabase_store_read_all(monkeypatch: pytest.MonkeyPatch):
    class _Result:
        data = [
            {
                "timestamp": "2026-09-30T14:00:00Z",
                "symbol": "TQQQ",
                "side": "buy",
                "status": "filled",
                "source": "strategy",
                "order_id": "ord-1",
                "qty": "1",
                "fill_price": "100",
                "filled_qty": "1",
            },
            {
                "timestamp": "2026-09-30T15:00:00Z",
                "symbol": "TQQQ",
                "side": "sell",
                "status": "filled",
                "source": "strategy",
                "order_id": "ord-2",
                "qty": "1",
                "fill_price": "101",
                "filled_qty": "1",
            },
        ]

    class _Query:
        def select(self, *_args, **_kwargs):
            return self

        def eq(self, key, value):
            assert key == "bot_id"
            assert value == "default"
            return self

        def order(self, key):
            assert key == "timestamp"
            return self

        def execute(self):
            return _Result()

    class _Client:
        def table(self, name: str):
            assert name == "bot_trade_log"
            return _Query()

    monkeypatch.setattr("trade_log_store.create_supabase_client", lambda: _Client())
    rows = SupabaseTradeLogStore("default").read_all()
    assert len(rows) == 2
    assert rows[0]["order_id"] == "ord-1"
    assert rows[1]["side"] == "sell"


def test_supabase_store_read_raises(monkeypatch: pytest.MonkeyPatch):
    class _Query:
        def select(self, *_args, **_kwargs):
            return self

        def eq(self, *_args, **_kwargs):
            return self

        def order(self, *_args, **_kwargs):
            return self

        def execute(self):
            raise RuntimeError("network down")

    class _Client:
        def table(self, _name: str):
            return _Query()

    monkeypatch.setattr("trade_log_store.create_supabase_client", lambda: _Client())
    with pytest.raises(TradeLogStoreError, match="Failed to load"):
        SupabaseTradeLogStore("default").read_all()


def test_dual_store_writes_file_and_supabase(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    captured: list[dict] = []

    class _Query:
        def insert(self, row):
            captured.append(row)
            return self

        def execute(self):
            return SimpleNamespace(data=[captured[-1]])

        def select(self, *_args, **_kwargs):
            return self

        def eq(self, *_args, **_kwargs):
            return self

        def order(self, *_args, **_kwargs):
            return self

    class _Client:
        def table(self, _name: str):
            return _Query()

        # read path for Dual uses remote.read_all — wire execute on select chain
        def __init__(self):
            self._query = _Query()

    # Rebuild with read returning captured rows
    class _ReadQuery:
        def select(self, *_args, **_kwargs):
            return self

        def eq(self, *_args, **_kwargs):
            return self

        def order(self, *_args, **_kwargs):
            return self

        def insert(self, row):
            captured.append(row)
            return self

        def execute(self):
            if captured and "timestamp" in captured[-1] and len(captured) >= 1:
                # distinguish insert vs select by whether we just inserted
                return SimpleNamespace(data=list(captured))
            return SimpleNamespace(data=list(captured))

    class _ReadClient:
        def table(self, _name: str):
            return _ReadQuery()

    monkeypatch.setattr("trade_log_store.create_supabase_client", lambda: _ReadClient())
    path = tmp_path / "trades.jsonl"
    store = DualTradeLogStore(FileTradeLogStore(path), SupabaseTradeLogStore("default"))
    store.append(_filled_buy())
    assert path.exists()
    assert len(captured) == 1
    assert captured[0]["order_id"] == "ord-1"
    rows = store.read_all()
    assert len(rows) == 1
    assert "supabase:bot_trade_log:default" in store.describe()


def test_dual_store_attempts_both_on_partial_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    class _Query:
        def insert(self, *_args, **_kwargs):
            return self

        def execute(self):
            raise RuntimeError("supabase down")

    class _Client:
        def table(self, _name: str):
            return _Query()

    monkeypatch.setattr("trade_log_store.create_supabase_client", lambda: _Client())
    path = tmp_path / "trades.jsonl"
    store = DualTradeLogStore(FileTradeLogStore(path), SupabaseTradeLogStore("default"))
    with pytest.raises(TradeLogStoreError, match="supabase down"):
        store.append(_filled_buy())
    # File write still happened before remote failure
    assert path.exists()
    assert len(FileTradeLogStore(path).read_all()) == 1


def test_trade_log_store_from_settings_file(tmp_path: Path):
    settings = SimpleNamespace(
        trade_log_backend="file",
        trade_log_jsonl=tmp_path / "trades.jsonl",
        trade_log_bot_id="default",
    )
    store = trade_log_store_from_settings(settings)  # type: ignore[arg-type]
    assert isinstance(store, FileTradeLogStore)


def test_trade_log_store_from_settings_supabase(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        "trade_log_store.create_supabase_client",
        lambda: SimpleNamespace(table=lambda _n: None),
    )
    settings = SimpleNamespace(
        trade_log_backend="supabase",
        trade_log_jsonl=tmp_path / "trades.jsonl",
        trade_log_bot_id="default",
    )
    store = trade_log_store_from_settings(settings)  # type: ignore[arg-type]
    assert isinstance(store, DualTradeLogStore)


def test_trade_log_store_from_settings_rejects_unknown():
    settings = SimpleNamespace(
        trade_log_backend="s3",
        trade_log_jsonl=Path("logs/trades.jsonl"),
        trade_log_bot_id="default",
    )
    with pytest.raises(ConfigError, match="Unsupported TRADE_LOG_BACKEND"):
        trade_log_store_from_settings(settings)  # type: ignore[arg-type]


def test_run_trade_log_report_prefers_store(tmp_path: Path):
    class _Store:
        def describe(self) -> str:
            return "supabase:bot_trade_log:default"

        def read_all(self):
            return [
                _filled_buy(fill_price="100", filled_qty="1"),
                _filled_buy(
                    side="sell",
                    order_id="ord-2",
                    alert_type="SELL",
                    fill_price="110",
                    filled_qty="1",
                    timestamp="2026-09-30T15:00:00Z",
                ),
            ]

    out = tmp_path / "report.json"
    # Local file empty — store must supply rows
    payload = run_trade_log_report(
        trade_log_path=tmp_path / "empty.jsonl",
        report_path=out,
        research_max_dd_pct=19.06,
        trade_log_store=_Store(),
    )
    assert payload["row_count"] == 2
    assert payload["source_path"] == "supabase:bot_trade_log:default"
    assert payload["realized_pnl"]["strategy_round_trip_count"] == 1
    assert out.exists()


def test_execute_paper_orders_uses_trade_log_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from unittest.mock import MagicMock

    from alpaca_paper import PAPER_TRADING_BASE_URL, execute_paper_orders
    from strategy_types import AlertDecision

    appended: list[dict] = []

    class _Store:
        def describe(self) -> str:
            return "mock-store"

        def append(self, record):
            appended.append(record)

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
        if "orders:client_order_id:" in url:
            resp.status_code = 404
            resp.text = "not found"
            resp.reason = "not found"
            return resp
        raise AssertionError(f"unexpected GET {url}")

    session.request.side_effect = fake_request
    session.get.side_effect = fake_get

    alert = AlertDecision(
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
    results = execute_paper_orders(
        alert,
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
        trade_log_path=tmp_path / "unused.jsonl",
        trade_log_store=_Store(),
        source="strategy",
    )
    assert results[0].ok is True
    assert len(appended) == 1
    assert appended[0]["order_id"] == "ord-1"
    # When store is provided, file path is not used by store path
    assert not (tmp_path / "unused.jsonl").exists()
