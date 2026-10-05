from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from datetime import datetime, timezone

from alpaca_paper import (
    PAPER_TRADING_BASE_URL,
    BrokerSnapshot,
    OrderIntent,
    OrderResult,
    broker_status_after_submit,
    build_limit_order_payload,
    build_order_intents,
    buy_notional_usd,
    execute_paper_orders,
    fetch_broker_snapshot,
    flip_entry_block_reason,
    is_live_trading_host,
    limit_price_from_trade,
    make_client_order_id,
    order_status_filled,
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
    assert is_live_trading_host("https://paper-api.alpaca.markets//") is False
    assert is_live_trading_host("https://api.alpaca.markets") is True
    assert is_live_trading_host("https://api.alpaca.markets/v2") is True
    assert is_live_trading_host("") is True
    assert is_live_trading_host("   ") is True
    assert is_live_trading_host("https://") is True
    assert is_live_trading_host("https:///") is True
    # Hosts that only contain the paper substring are not the paper host.
    assert is_live_trading_host("https://not-paper-api.alpaca.markets") is True
    assert is_live_trading_host("https://paper-api.alpaca.markets.evil.example") is True
    assert is_live_trading_host("https://evil.example/paper-api.alpaca.markets") is True
    assert is_live_trading_host("http://paper-api.alpaca.markets") is True
    assert is_live_trading_host("https://paper-api.alpaca.markets/v2") is True
    assert is_live_trading_host("https://user:pass@paper-api.alpaca.markets") is True


@pytest.mark.parametrize(
    "trading_url",
    [
        "https://api.alpaca.markets",
        "https://api.alpaca.markets/v2",
        "https://",
        "https:///",
        "https://not-paper-api.alpaca.markets",
        "https://paper-api.alpaca.markets.evil.example",
        "https://evil.example/paper-api.alpaca.markets",
        "http://paper-api.alpaca.markets",
        "https://paper-api.alpaca.markets/v2",
        "https://PAPER-API.ALPACA.MARKETS",
    ],
)
def test_load_settings_rejects_non_exact_paper_url(
    monkeypatch: pytest.MonkeyPatch, trading_url: str
):
    monkeypatch.setenv("ALPACA_API_KEY", "test-key")
    monkeypatch.setenv("ALPACA_API_SECRET", "test-secret")
    monkeypatch.setenv("ALPACA_TRADING_BASE_URL", trading_url)
    with pytest.raises(ConfigError, match="must be exactly"):
        load_settings()


def test_load_settings_accepts_paper_url_trailing_slash(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ALPACA_API_KEY", "test-key")
    monkeypatch.setenv("ALPACA_API_SECRET", "test-secret")
    monkeypatch.setenv("ALPACA_TRADING_BASE_URL", "https://paper-api.alpaca.markets/")
    monkeypatch.setenv("ALPACA_PAPER_TRADING", "false")
    monkeypatch.setenv("POSITION_STATE_BACKEND", "file")
    monkeypatch.setenv("TRADE_LOG_BACKEND", "file")
    settings = load_settings()
    assert settings.alpaca_trading_base_url == PAPER_TRADING_BASE_URL


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
    monkeypatch.delenv("ALPACA_TRADING_BASE_URL", raising=False)
    monkeypatch.delenv("TRADE_LOG_JSONL", raising=False)
    # Explicit values: load_dotenv() would otherwise refill from a local .env.
    monkeypatch.setenv("ALPACA_PAPER_TRADING", "false")
    monkeypatch.setenv("ALPACA_PAPER_NOTIONAL", "500")
    monkeypatch.setenv("ALPACA_PAPER_EQUITY_PCT", "0")
    monkeypatch.setenv("ALPACA_PAPER_VOL_SIZING", "false")
    settings = load_settings()
    assert settings.alpaca_paper_trading is False
    assert settings.alpaca_trading_base_url == PAPER_TRADING_BASE_URL
    assert settings.alpaca_paper_notional == 500
    assert settings.alpaca_paper_equity_pct == 0
    assert settings.trade_log_jsonl.name == "trades.jsonl"


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


def test_execute_paper_orders_buy_path_mocked(tmp_path):
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
            assert json["client_order_id"] == "entry-TQQQ-buy-2026-09-30"
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
        if "orders:client_order_id:" in url:
            resp.status_code = 404
            resp.text = "not found"
            resp.reason = "not found"
            return resp
        if "/quotes/latest" in url:
            resp.status_code = 404
            resp.text = "not found"
            resp.reason = "not found"
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
        trade_log_path=tmp_path / "trades.jsonl",
    )
    assert len(results) == 1
    assert results[0].ok is True
    assert results[0].order_id == "ord-1"
    assert results[0].payload["type"] == "limit"
    assert results[0].payload["side"] == "buy"


def test_execute_paper_orders_flip_mocked(tmp_path):
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
            status = "filled" if json and json.get("side") == "sell" else "accepted"
            resp.text = f'{{"id":"ord-x","status":"{status}"}}'
            resp.json.return_value = {"id": "ord-x", "status": status}
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
        if "orders:client_order_id:" in url:
            resp.status_code = 404
            resp.text = "not found"
            resp.reason = "not found"
            return resp
        if "/quotes/latest" in url:
            resp.status_code = 404
            resp.text = "not found"
            resp.reason = "not found"
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
        trade_log_path=tmp_path / "trades.jsonl",
    )
    assert len(results) == 2
    assert results[0].intent.side == "sell" and results[0].intent.symbol == "TQQQ"
    assert results[1].intent.side == "buy" and results[1].intent.symbol == "SQQQ"
    assert all(r.ok for r in results)


def test_execute_paper_orders_sell_skips_without_position(capfd, tmp_path):
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
        trade_log_path=tmp_path / "trades.jsonl",
    )
    assert len(results) == 1
    assert results[0].status == "skipped"
    assert "No Alpaca paper position" in capfd.readouterr().out


def test_client_order_id_is_stable_for_the_same_session():
    intent = OrderIntent(symbol="TQQQ", side="buy", purpose="entry")
    first = make_client_order_id(intent, signal_day="2026-09-30")
    second = make_client_order_id(intent, signal_day="2026-09-30")
    assert first == second == "entry-TQQQ-buy-2026-09-30"
    assert len(first) <= 48


def _paper_session_kwargs(tmp_path, session):
    return dict(
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
        trade_log_path=tmp_path / "trades.jsonl",
    )


def test_flip_blocks_second_leg_when_sell_is_only_accepted(tmp_path):
    session = MagicMock()
    posts: list[str] = []

    def fake_request(method, url, headers=None, json=None, timeout=None):
        resp = MagicMock()
        resp.status_code = 200
        if url.endswith("/v2/account"):
            resp.text = '{"equity":"50000"}'
            resp.json.return_value = {"equity": "50000"}
            return resp
        if url.endswith("/v2/orders") and method == "POST":
            posts.append(json["side"])
            resp.text = '{"id":"ord-sell","status":"accepted"}'
            resp.json.return_value = {"id": "ord-sell", "status": "accepted"}
            return resp
        raise AssertionError(f"unexpected {method} {url}")

    def fake_get(url, headers=None, params=None, timeout=None):
        resp = MagicMock()
        if url.endswith("/v2/positions/TQQQ"):
            resp.status_code = 200
            resp.text = '{"qty":"2"}'
            resp.json.return_value = {"qty": "2"}
            return resp
        if "/trades/latest" in url:
            resp.status_code = 200
            resp.text = '{"trade":{"p":50.0}}'
            resp.json.return_value = {"trade": {"p": 50.0}}
            return resp
        if "orders:client_order_id:" in url:
            resp.status_code = 404
            resp.text = "not found"
            resp.reason = "not found"
            return resp
        if "/quotes/latest" in url:
            resp.status_code = 404
            resp.text = "not found"
            resp.reason = "not found"
            return resp
        raise AssertionError(f"unexpected GET {url}")

    session.request.side_effect = fake_request
    session.get.side_effect = fake_get
    results = execute_paper_orders(
        _alert(alert_type="FLIP", symbol="SQQQ"),
        PositionState(active_symbol="TQQQ"),
        **_paper_session_kwargs(tmp_path, session),
    )
    assert posts == ["sell"]
    assert [r.status for r in results] == ["accepted", "blocked"]
    assert results[1].intent.purpose == "flip_entry"
    assert results[1].ok is False


def test_flip_blocks_second_leg_when_sell_errors(tmp_path):
    session = MagicMock()
    posts: list[str] = []

    def fake_request(method, url, headers=None, json=None, timeout=None):
        resp = MagicMock()
        if url.endswith("/v2/account"):
            resp.status_code = 200
            resp.text = '{"equity":"50000"}'
            resp.json.return_value = {"equity": "50000"}
            return resp
        if url.endswith("/v2/orders") and method == "POST":
            posts.append(json["side"])
            resp.status_code = 500
            resp.text = "sell rejected"
            resp.reason = "error"
            return resp
        raise AssertionError(f"unexpected {method} {url}")

    def fake_get(url, headers=None, params=None, timeout=None):
        resp = MagicMock()
        if url.endswith("/v2/positions/TQQQ"):
            resp.status_code = 200
            resp.text = '{"qty":"2"}'
            resp.json.return_value = {"qty": "2"}
            return resp
        if "/trades/latest" in url:
            resp.status_code = 200
            resp.text = '{"trade":{"p":50.0}}'
            resp.json.return_value = {"trade": {"p": 50.0}}
            return resp
        if "orders:client_order_id:" in url:
            resp.status_code = 404
            resp.text = "not found"
            resp.reason = "not found"
            return resp
        if "/quotes/latest" in url:
            resp.status_code = 404
            resp.text = "not found"
            resp.reason = "not found"
            return resp
        raise AssertionError(f"unexpected GET {url}")

    session.request.side_effect = fake_request
    session.get.side_effect = fake_get
    results = execute_paper_orders(
        _alert(alert_type="FLIP", symbol="SQQQ"),
        PositionState(active_symbol="TQQQ"),
        **_paper_session_kwargs(tmp_path, session),
    )
    assert posts == ["sell"]
    assert results[0].ok is False
    assert results[1].status == "blocked"


def test_repeat_client_order_id_does_not_post_again(tmp_path):
    session = MagicMock()

    def fake_request(method, url, headers=None, json=None, timeout=None):
        resp = MagicMock()
        resp.status_code = 200
        if url.endswith("/v2/account"):
            resp.text = '{"equity":"100000"}'
            resp.json.return_value = {"equity": "100000"}
            return resp
        raise AssertionError(f"unexpected second submit {method} {url}")

    def fake_get(url, headers=None, params=None, timeout=None):
        resp = MagicMock()
        if "orders:client_order_id:entry-TQQQ-buy-2026-09-30" in url:
            resp.status_code = 200
            resp.text = '{"id":"existing","status":"accepted"}'
            resp.json.return_value = {"id": "existing", "status": "accepted"}
            return resp
        if "/quotes/latest" in url:
            resp.status_code = 404
            resp.text = "not found"
            resp.reason = "not found"
            return resp
        raise AssertionError(f"unexpected GET {url}")

    session.request.side_effect = fake_request
    session.get.side_effect = fake_get
    results = execute_paper_orders(
        _alert(alert_type="BUY", symbol="TQQQ"),
        None,
        **_paper_session_kwargs(tmp_path, session),
    )
    assert len(results) == 1
    assert results[0].ok is True
    assert results[0].order_id == "existing"
    assert results[0].status == "accepted"
    assert "idempotent" in results[0].detail


def test_submit_does_not_invent_filled_status():
    assert broker_status_after_submit({}) == "submitted"
    assert broker_status_after_submit({"status": "accepted"}) == "accepted"
    assert broker_status_after_submit({"status": "partially_filled"}) == "partially_filled"
    assert order_status_filled("accepted") is False
    assert order_status_filled("partially_filled") is False
    assert order_status_filled("filled") is True
    assert is_live_trading_host("https://api.alpaca.markets") is True
    assert is_live_trading_host(PAPER_TRADING_BASE_URL) is False


def test_flip_entry_requires_filled_exit_or_confirmed_flat():
    accepted = OrderResult(
        intent=OrderIntent("TQQQ", "sell", "flip_exit"),
        ok=True,
        status="accepted",
        detail="",
    )
    partial = OrderResult(
        intent=OrderIntent("TQQQ", "sell", "flip_exit"),
        ok=True,
        status="partially_filled",
        detail="",
    )
    filled = OrderResult(
        intent=OrderIntent("TQQQ", "sell", "flip_exit"),
        ok=True,
        status="filled",
        detail="",
    )
    assert flip_entry_block_reason(accepted, None, None) is not None
    assert flip_entry_block_reason(partial, None, None) is not None
    assert flip_entry_block_reason(filled, None, None) is None
    assert flip_entry_block_reason(None, BrokerSnapshot(), None) is None
    assert flip_entry_block_reason(None, BrokerSnapshot(tqqq_qty=1), None) is not None
    assert flip_entry_block_reason(None, BrokerSnapshot(sqqq_qty=1), None) is not None
    assert flip_entry_block_reason(None, BrokerSnapshot(tqqq_qty=1, sqqq_qty=1), None) is not None
    assert flip_entry_block_reason(None, BrokerSnapshot(open_order_count=1), None) is not None
    assert flip_entry_block_reason(None, None, "timeout") is not None


def test_fetch_broker_snapshot_down_raises():
    session = MagicMock()
    session.get.side_effect = __import__("requests").RequestException("broker down")
    with pytest.raises(Exception, match="broker|positions|lookup|failed"):
        fetch_broker_snapshot(
            api_key="k",
            api_secret="s",
            trading_base_url=PAPER_TRADING_BASE_URL,
            session=session,
        )


def test_flip_blocks_second_leg_when_sell_is_partial(tmp_path):
    session = MagicMock()
    posts: list[str] = []

    def fake_request(method, url, headers=None, json=None, timeout=None):
        resp = MagicMock()
        resp.status_code = 200
        if url.endswith("/v2/account"):
            resp.text = '{"equity":"50000"}'
            resp.json.return_value = {"equity": "50000"}
            return resp
        if url.endswith("/v2/orders") and method == "POST":
            posts.append(json["side"])
            resp.text = '{"id":"ord-sell","status":"partially_filled"}'
            resp.json.return_value = {"id": "ord-sell", "status": "partially_filled"}
            return resp
        raise AssertionError(f"unexpected {method} {url}")

    def fake_get(url, headers=None, params=None, timeout=None):
        resp = MagicMock()
        if url.endswith("/v2/positions/TQQQ"):
            resp.status_code = 200
            resp.text = '{"qty":"2"}'
            resp.json.return_value = {"qty": "2"}
            return resp
        if "/quotes/latest" in url:
            resp.status_code = 404
            resp.text = "not found"
            resp.reason = "not found"
            return resp
        if "/trades/latest" in url:
            resp.status_code = 200
            resp.text = '{"trade":{"p":50.0}}'
            resp.json.return_value = {"trade": {"p": 50.0}}
            return resp
        if "orders:client_order_id:" in url:
            resp.status_code = 404
            resp.text = "not found"
            resp.reason = "not found"
            return resp
        raise AssertionError(url)

    session.request.side_effect = fake_request
    session.get.side_effect = fake_get
    results = execute_paper_orders(
        _alert(alert_type="FLIP", symbol="SQQQ"),
        PositionState(active_symbol="TQQQ"),
        **_paper_session_kwargs(tmp_path, session),
    )
    assert posts == ["sell"]
    assert results[0].status == "partially_filled"
    assert results[1].status == "blocked"
    assert results[1].intent.purpose == "flip_entry"


def test_buy_uses_fresh_ask_not_trade_offset(tmp_path):
    session = MagicMock()
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    def fake_request(method, url, headers=None, json=None, timeout=None):
        resp = MagicMock()
        resp.status_code = 200
        if url.endswith("/v2/account"):
            resp.text = '{"equity":"100000"}'
            resp.json.return_value = {"equity": "100000"}
            return resp
        if url.endswith("/v2/orders") and method == "POST":
            assert json["limit_price"] == "100.05"
            assert json["side"] == "buy"
            resp.text = '{"id":"ord-q","status":"accepted"}'
            resp.json.return_value = {"id": "ord-q", "status": "accepted"}
            return resp
        raise AssertionError(f"{method} {url}")

    def fake_get(url, headers=None, params=None, timeout=None):
        resp = MagicMock()
        if "/quotes/latest" in url:
            resp.status_code = 200
            resp.json.return_value = {"quote": {"bp": 100.0, "ap": 100.05, "t": now}}
            resp.text = "{}"
            return resp
        if "/trades/latest" in url:
            resp.status_code = 200
            resp.json.return_value = {"trade": {"p": 90.0, "t": now}}
            resp.text = "{}"
            return resp
        if "orders:client_order_id:" in url:
            resp.status_code = 404
            resp.text = "not found"
            resp.reason = "not found"
            return resp
        raise AssertionError(url)

    session.request.side_effect = fake_request
    session.get.side_effect = fake_get
    results = execute_paper_orders(
        _alert(alert_type="BUY", symbol="TQQQ"),
        None,
        **_paper_session_kwargs(tmp_path, session),
    )
    assert results[0].status == "accepted"
    assert results[0].payload["limit_price"] == "100.05"


def test_stale_bar_blocks_buy_in_execute(tmp_path):
    session = MagicMock()
    posts: list[str] = []

    def fake_request(method, url, headers=None, json=None, timeout=None):
        resp = MagicMock()
        if url.endswith("/v2/orders"):
            posts.append("post")
        resp.status_code = 200
        resp.text = '{"equity":"1"}'
        resp.json.return_value = {"equity": "1"}
        return resp

    session.request.side_effect = fake_request
    session.get.side_effect = AssertionError("no broker call")
    results = execute_paper_orders(
        _alert(alert_type="BUY", symbol="TQQQ"),
        None,
        market_data_bar_start=datetime(2020, 1, 1, tzinfo=timezone.utc),
        market_data_max_age_minutes=90,
        **_paper_session_kwargs(tmp_path, session),
    )
    assert posts == []
    assert results[0].status == "blocked"
    assert "STALE" in results[0].detail
