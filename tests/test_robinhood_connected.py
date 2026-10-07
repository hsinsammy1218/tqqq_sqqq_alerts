"""Connected shadow reads Robinhood and still cannot submit."""

from __future__ import annotations

import inspect
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from alpaca_paper import PAPER_TRADING_BASE_URL
from brokers.mode import ROBINHOOD_CONNECTED_SHADOW, parse_execution_broker
from brokers.robinhood_agentic import LIVE_SUBMISSION_IMPLEMENTED, RobinhoodAgenticBroker
from brokers.robinhood_normalize import normalize_snapshot
from brokers.quotes import quote_for_symbol
from brokers.robinhood_reader import (
    ALLOWED_READS,
    AUTH_CASE,
    READ_TOOLS,
    UNATTENDED_AUTH_SUPPORTED,
    McpReadTransport,
    RobinhoodReadClient,
    RobinhoodReadError,
    RobinhoodToolRejected,
    transport_from_env,
)
from brokers.types import LiveSubmissionDisabled, TradeIntent, UnsafeBrokerConfiguration
from robinhood_audit import ShadowAuditLog
from robinhood_connected import run_connected_shadow, run_connected_shadow_after_strategy
from robinhood_risk import limits_from_env
from strategy_params import STRATEGY_VERSION
from strategy_types import AlertDecision, PositionState

NOW = datetime(2026, 10, 5, 15, 0, tzinfo=timezone.utc)
WRITE_TOOLS = (
    "place_equity_order",
    "cancel_equity_order",
    "review_equity_order",
    "place_option_order",
    "cancel_option_order",
    "review_option_order",
    "submit_order",
    "cancel_order",
    "replace_order",
)


def _alert(**kwargs: object) -> AlertDecision:
    defaults = dict(
        alert_type="BUY",
        symbol="TQQQ",
        qqq_trend_reason="regime=trend_up",
        bullish_score=80,
        bearish_score=20,
        confidence_score=78,
        entry_zone_low=400.0,
        entry_zone_high=410.0,
        stop_loss=370.0,
        take_profit=460.0,
        stretch_take_profit=500.0,
        max_hold_date="2026-06-01",
        timestamp="2026-10-05T14:30:00Z",
        notes="Bull score exceeded threshold",
    )
    defaults.update(kwargs)
    return AlertDecision(**defaults)  # type: ignore[arg-type]


def _payloads(**overrides: object) -> dict:
    quote_time = NOW.isoformat()
    raw = {
        "get_accounts": {"accounts": [{"status": "active"}]},
        "get_portfolio": {
            "equity": 10_000.0,
            "buying_power": 10_000.0,
            "cash": 4_000.0,
            "day_start_equity": 10_000.0,
            "week_start_equity": 10_000.0,
            "peak_equity": 10_000.0,
        },
        "get_equity_positions": [],
        "get_equity_quotes": [
            {"symbol": "TQQQ", "bid": 52.10, "ask": 52.14, "quote_time": quote_time},
            {"symbol": "SQQQ", "bid": 20.00, "ask": 20.02, "quote_time": quote_time},
        ],
        "get_equity_orders": [],
    }
    raw.update(overrides)
    return raw


def _transport(payloads: dict, calls: list[str]):
    def _call(name: str, arguments: dict) -> object:
        calls.append(name)
        if name not in READ_TOOLS:
            raise AssertionError(f"transport saw {name}")
        if name == "get_equity_quotes":
            assert arguments.get("symbols") == ["TQQQ", "SQQQ"]
        return payloads[name]

    return _call


def _limits() -> object:
    return limits_from_env(
        {
            "ROBINHOOD_MAX_POSITION_PCT": "0.50",
            "ROBINHOOD_MAX_ORDER_NOTIONAL": "104.28",
            "ROBINHOOD_MAX_DAILY_LOSS_PCT": "0.02",
            "ROBINHOOD_MAX_WEEKLY_LOSS_PCT": "0.04",
            "ROBINHOOD_MAX_DRAWDOWN_PCT": "0.10",
            "ROBINHOOD_MAX_ORDERS_PER_DAY": "2",
            "ROBINHOOD_NEW_ENTRIES_ENABLED": "true",
            "ROBINHOOD_LIVE_ENABLED": "true",
        }
    )


def _run(payloads: dict, alert: AlertDecision | None = None, position: PositionState | None = None):
    calls: list[str] = []
    client = RobinhoodReadClient(_transport(payloads, calls))
    state = normalize_snapshot(
        client.read_snapshot(),
        now=NOW,
        data_bar_start=NOW - timedelta(minutes=20),
    )
    result, broker_state = run_connected_shadow(alert or _alert(), position, state, _limits())
    return result, broker_state, calls, state


def test_mode_accepts_connected_shadow_and_still_rejects_live():
    assert parse_execution_broker("robinhood_connected_shadow") == ROBINHOOD_CONNECTED_SHADOW
    for raw in ("robinhood_live", "live", "robinhood_connected_shadow_live", "alpaca_live"):
        with pytest.raises(UnsafeBrokerConfiguration):
            parse_execution_broker(raw)


def test_write_tools_never_reach_the_transport():
    calls: list[str] = []

    def _call(name: str, arguments: dict) -> object:
        calls.append(name)
        return {}

    client = RobinhoodReadClient(_call)
    for name in WRITE_TOOLS:
        with pytest.raises(RobinhoodToolRejected):
            client.call(name, {"symbol": "TQQQ", "quantity": 1})
    assert calls == []
    assert not any(name in READ_TOOLS for name in WRITE_TOOLS)
    source = inspect.getsource(RobinhoodReadClient)
    assert "def submit_order" not in source
    assert "def cancel_order" not in source
    assert "def replace_order" not in source


def test_http_transport_rejects_writes_before_the_socket():
    class Session:
        def post(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            raise AssertionError("socket opened for a write")

    transport = McpReadTransport(
        "https://agent.robinhood.com/mcp/trading",
        "super-secret-token",
        session=Session(),
    )
    with pytest.raises(RobinhoodToolRejected):
        transport("place_equity_order", {"symbol": "TQQQ"})


def test_static_bearer_transport_does_not_open_a_socket():
    class Session:
        def post(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            raise AssertionError("socket opened for a static bearer read")

    transport = McpReadTransport(
        "https://agent.robinhood.com/mcp/trading",
        "super-secret-token",
        session=Session(),
    )
    assert AUTH_CASE == "C"
    assert UNATTENDED_AUTH_SUPPORTED is False
    assert transport_from_env(
        {
            "ROBINHOOD_MCP_URL": "https://agent.robinhood.com/mcp/trading",
            "ROBINHOOD_MCP_TOKEN": "super-secret-token",
        }
    ) is None
    with pytest.raises(RobinhoodReadError, match="unsupported"):
        transport("get_accounts", {})
    assert not hasattr(transport, "_token")
    source = inspect.getsource(McpReadTransport)
    assert "requests" not in source
    assert "Authorization" not in source
    assert "get_accounts" in ALLOWED_READS


def test_buy_uses_real_quote_and_does_not_submit():
    result, broker_state, calls, state = _run(_payloads())
    assert broker_state == "known"
    assert state.account is not None and state.account.cash == 4000
    assert calls == [
        "get_accounts",
        "get_portfolio",
        "get_equity_positions",
        "get_equity_quotes",
        "get_equity_orders",
    ]
    assert result.submission_attempts == 0
    assert result.submitted is False
    leg = result.legs[0]
    assert leg.risk_status == "PASS"
    assert leg.intent.quantity == 2
    assert leg.intent.estimated_price == 52.14
    assert leg.execution_status == "NOT_SUBMITTED"
    assert "CONNECTED SHADOW" in result.text
    assert "NO REAL MONEY TRADED" in result.text
    assert "SHADOW MODE" not in result.text
    assert STRATEGY_VERSION == "1.0.0"
    assert PAPER_TRADING_BASE_URL == "https://paper-api.alpaca.markets"


def test_live_flag_still_cannot_submit():
    assert LIVE_SUBMISSION_IMPLEMENTED is False
    _result, _broker_state, _calls, state = _run(_payloads())
    broker = RobinhoodAgenticBroker(state)
    intent = TradeIntent(
        strategy_version=STRATEGY_VERSION,
        signal_symbol="QQQ",
        execution_symbol="TQQQ",
        action="BUY",
        quantity=1,
        estimated_price=52.14,
        estimated_notional=52.14,
        confidence=78,
        regime="trend_up",
        reason="test",
        signal_id="test",
        timestamp="2026-10-05T14:30:00Z",
        purpose="entry",
        client_order_id="rh-test",
    )
    with pytest.raises(LiveSubmissionDisabled):
        broker.submit_order(intent)
    with pytest.raises(LiveSubmissionDisabled):
        broker.cancel_order("order-1")
    assert broker.submission_attempts == 2


def test_read_failure_blocks_without_a_fabricated_book(tmp_path: Path):
    calls: list[str] = []

    def _call(name: str, arguments: dict) -> object:
        calls.append(name)
        raise RuntimeError("mcp down")

    result = run_connected_shadow_after_strategy(
        _alert(),
        None,
        dry_run=True,
        webhook_url="https://discord.example/webhook/secret-token",
        data_bar_start=NOW,
        now=NOW,
        env={
            "EXECUTION_BROKER": "robinhood_connected_shadow",
            "ROBINHOOD_NEW_ENTRIES_ENABLED": "true",
            "ROBINHOOD_MAX_POSITION_PCT": "0.50",
            "ROBINHOOD_MAX_ORDER_NOTIONAL": "104.28",
            "ROBINHOOD_MAX_DAILY_LOSS_PCT": "0.02",
            "ROBINHOOD_MAX_WEEKLY_LOSS_PCT": "0.04",
            "ROBINHOOD_MAX_DRAWDOWN_PCT": "0.10",
            "ROBINHOOD_MAX_ORDERS_PER_DAY": "1",
            "ROBINHOOD_SHADOW_LOG": str(tmp_path / "failed.jsonl"),
        },
        transport=_call,
    )
    assert result is not None
    assert result.legs[0].risk_status == "BLOCKED"
    assert "unknown" in result.legs[0].risk_reason or "blocked" in result.legs[0].risk_reason
    assert result.legs[0].execution_status == "NOT_SUBMITTED"
    assert result.submission_attempts == 0
    assert "place_equity_order" not in calls


def test_missing_quote_is_not_fabricated(tmp_path: Path):
    payloads = _payloads(
        get_equity_quotes=[{"symbol": "TQQQ", "bid": 52.10, "quote_time": NOW.isoformat()}]
    )
    result, broker_state, _calls, state = _run(payloads)
    assert state.quote is None
    assert state.quotes == ()
    assert broker_state == "known"
    assert result.legs[0].risk_status == "BLOCKED"
    assert "quote" in result.legs[0].risk_reason
    assert result.legs[0].intent.quantity == 0
    assert result.legs[0].intent.estimated_price is None


def test_zero_shares_block():
    payloads = _payloads(
        get_portfolio={
            "equity": 100.0,
            "buying_power": 100.0,
            "cash": 100.0,
            "day_start_equity": 100.0,
            "week_start_equity": 100.0,
            "peak_equity": 100.0,
        }
    )
    calls: list[str] = []
    client = RobinhoodReadClient(_transport(payloads, calls))
    state = normalize_snapshot(client.read_snapshot(), now=NOW, data_bar_start=NOW - timedelta(minutes=5))
    limits = limits_from_env(
        {
            "ROBINHOOD_MAX_POSITION_PCT": "0.10",
            "ROBINHOOD_MAX_ORDER_NOTIONAL": "10",
            "ROBINHOOD_MAX_DAILY_LOSS_PCT": "0.02",
            "ROBINHOOD_MAX_WEEKLY_LOSS_PCT": "0.05",
            "ROBINHOOD_MAX_DRAWDOWN_PCT": "0.10",
            "ROBINHOOD_MAX_ORDERS_PER_DAY": "1",
            "ROBINHOOD_NEW_ENTRIES_ENABLED": "true",
        }
    )
    result, _broker_state = run_connected_shadow(_alert(), None, state, limits)
    assert result.legs[0].risk_status == "BLOCKED"
    assert "zero" in result.legs[0].risk_reason
    assert result.legs[0].execution_status == "NOT_SUBMITTED"


def test_unknown_order_status_blocks():
    payloads = _payloads(
        get_equity_orders=[
            {
                "symbol": "TQQQ",
                "side": "buy",
                "status": "mystery",
                "created_at": NOW.isoformat(),
            }
        ]
    )
    result, broker_state, _calls, state = _run(payloads)
    assert state.open_orders[0].status == "UNKNOWN"
    assert state.open_orders[0].client_order_id == ""
    assert broker_state == "mismatch"
    assert result.legs[0].risk_status == "BLOCKED"
    assert "unknown order status" in result.legs[0].risk_reason


def test_local_book_mismatch_blocks_new_exposure():
    payloads = _payloads(get_equity_positions=[{"symbol": "SQQQ", "quantity": "1"}])
    result, broker_state, _calls, _state = _run(
        payloads,
        position=PositionState(active_symbol="TQQQ"),
    )
    assert broker_state == "mismatch"
    assert result.legs[0].risk_status == "BLOCKED"
    assert "does not match" in result.legs[0].risk_reason
    assert result.submission_attempts == 0


def test_flip_proposes_real_sell_and_blocks_leg_two():
    payloads = _payloads(get_equity_positions=[{"symbol": "TQQQ", "qty": 2}])
    result, broker_state, calls, _state = _run(
        payloads,
        alert=_alert(alert_type="FLIP", symbol="SQQQ", notes="Regime flipped"),
        position=PositionState(active_symbol="TQQQ"),
    )
    assert broker_state == "known"
    assert [leg.intent.action for leg in result.legs] == ["SELL", "BUY"]
    sell, buy = result.legs
    assert sell.risk_status == "PASS"
    assert sell.intent.quantity == 2
    assert sell.intent.estimated_price == 52.10
    assert sell.execution_status == "NOT_SUBMITTED"
    assert buy.risk_status == "BLOCKED"
    assert "second leg" in buy.risk_reason
    assert buy.execution_status == "NOT_SUBMITTED"
    assert result.submission_attempts == 0
    assert set(calls) <= set(READ_TOOLS)
    assert "exit_status" not in inspect.signature(run_connected_shadow).parameters


def test_audit_row_is_connected_shadow_and_redacts_secrets(tmp_path: Path):
    log_path = tmp_path / "shadow.jsonl"
    env = {
        "EXECUTION_BROKER": "robinhood_connected_shadow",
        "ROBINHOOD_NEW_ENTRIES_ENABLED": "true",
        "ROBINHOOD_MAX_POSITION_PCT": "0.50",
        "ROBINHOOD_MAX_ORDER_NOTIONAL": "104.28",
        "ROBINHOOD_MAX_DAILY_LOSS_PCT": "0.02",
        "ROBINHOOD_MAX_WEEKLY_LOSS_PCT": "0.04",
        "ROBINHOOD_MAX_DRAWDOWN_PCT": "0.10",
        "ROBINHOOD_MAX_ORDERS_PER_DAY": "2",
        "ROBINHOOD_LIVE_ENABLED": "true",
        "ROBINHOOD_SHADOW_LOG": str(log_path),
        "ROBINHOOD_MCP_TOKEN": "should-not-be-logged",
    }
    result = run_connected_shadow_after_strategy(
        _alert(),
        None,
        dry_run=True,
        webhook_url="",
        data_bar_start=NOW - timedelta(minutes=10),
        now=NOW,
        env=env,
        transport=_transport(_payloads(), []),
    )
    assert result is not None
    rows = ShadowAuditLog(log_path).read()
    assert len(rows) == 1
    assert rows[0]["execution_mode"] == "connected_shadow"
    assert rows[0]["execution_status"] == "NOT_SUBMITTED"
    assert "should-not-be-logged" not in log_path.read_text(encoding="utf-8")
    poisoned = ShadowAuditLog(tmp_path / "poison.jsonl")
    poisoned.append({"execution_mode": "connected_shadow", "api_key": "sekret", "client_order_id": "abc"})
    stored = poisoned.read()[0]
    assert stored["api_key"] == "[redacted]"
    assert stored["execution_status"] == "NOT_SUBMITTED"


def test_missing_buying_power_blocks():
    payloads = _payloads(
        get_portfolio={
            "equity": 10_000.0,
            "cash": 1_000.0,
            "day_start_equity": 10_000.0,
            "week_start_equity": 10_000.0,
            "peak_equity": 10_000.0,
        }
    )
    result, broker_state, _calls, state = _run(payloads)
    assert state.known is False
    assert state.account is not None and state.account.buying_power is None
    assert broker_state == "unknown"
    assert result.legs[0].risk_status == "BLOCKED"
    assert "buying power" in result.legs[0].risk_reason
    assert result.legs[0].execution_status == "NOT_SUBMITTED"


def test_duplicate_client_order_id_blocks(tmp_path: Path):
    log_path = tmp_path / "shadow.jsonl"
    env = {
        "EXECUTION_BROKER": "robinhood_connected_shadow",
        "ROBINHOOD_NEW_ENTRIES_ENABLED": "true",
        "ROBINHOOD_MAX_POSITION_PCT": "0.50",
        "ROBINHOOD_MAX_ORDER_NOTIONAL": "104.28",
        "ROBINHOOD_MAX_DAILY_LOSS_PCT": "0.02",
        "ROBINHOOD_MAX_WEEKLY_LOSS_PCT": "0.04",
        "ROBINHOOD_MAX_DRAWDOWN_PCT": "0.10",
        "ROBINHOOD_MAX_ORDERS_PER_DAY": "2",
        "ROBINHOOD_SHADOW_LOG": str(log_path),
    }
    kwargs = dict(
        dry_run=True,
        webhook_url="",
        data_bar_start=NOW - timedelta(minutes=10),
        now=NOW,
        env=env,
        transport=_transport(_payloads(), []),
    )
    first = run_connected_shadow_after_strategy(_alert(), None, **kwargs)
    second = run_connected_shadow_after_strategy(_alert(), None, **kwargs)
    assert first is not None and first.legs[0].risk_status == "PASS"
    assert second is not None and second.legs[0].risk_status == "BLOCKED"
    assert "duplicate" in second.legs[0].risk_reason
    assert second.submission_attempts == 0


def test_discord_payload_has_no_secret(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    seen: dict = {}

    class Response:
        status_code = 204

    def _post(url, json, timeout):  # type: ignore[no-untyped-def]
        seen["url"] = url
        seen["json"] = json
        return Response()

    monkeypatch.setattr("robinhood_connected.requests.post", _post)
    result = run_connected_shadow_after_strategy(
        _alert(),
        None,
        dry_run=False,
        webhook_url="https://discord.example/api/webhooks/secret-token",
        data_bar_start=NOW - timedelta(minutes=10),
        now=NOW,
        env={
            "EXECUTION_BROKER": "robinhood_connected_shadow",
            "ROBINHOOD_NEW_ENTRIES_ENABLED": "false",
            "ROBINHOOD_MAX_POSITION_PCT": "0.50",
            "ROBINHOOD_MAX_ORDER_NOTIONAL": "104.28",
            "ROBINHOOD_MAX_DAILY_LOSS_PCT": "0.02",
            "ROBINHOOD_MAX_WEEKLY_LOSS_PCT": "0.04",
            "ROBINHOOD_MAX_DRAWDOWN_PCT": "0.10",
            "ROBINHOOD_MAX_ORDERS_PER_DAY": "1",
            "ROBINHOOD_SHADOW_LOG": str(tmp_path / "discord.jsonl"),
            "ROBINHOOD_MCP_TOKEN": "super-secret-token",
        },
        transport=_transport(_payloads(), []),
    )
    assert result is not None
    body = str(seen["json"])
    assert "NO REAL MONEY TRADED" in body
    assert "CONNECTED SHADOW" in body
    assert "super-secret-token" not in body
    assert "secret-token" not in body


def test_static_token_env_blocks_without_http(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    def _boom(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("HTTP was attempted")

    monkeypatch.setattr("requests.post", _boom)
    result = run_connected_shadow_after_strategy(
        _alert(),
        None,
        dry_run=True,
        webhook_url="",
        data_bar_start=NOW,
        now=NOW,
        env={
            "EXECUTION_BROKER": "robinhood_connected_shadow",
            "ROBINHOOD_NEW_ENTRIES_ENABLED": "true",
            "ROBINHOOD_MAX_POSITION_PCT": "0.50",
            "ROBINHOOD_MAX_ORDER_NOTIONAL": "100",
            "ROBINHOOD_MAX_DAILY_LOSS_PCT": "0.02",
            "ROBINHOOD_MAX_WEEKLY_LOSS_PCT": "0.04",
            "ROBINHOOD_MAX_DRAWDOWN_PCT": "0.10",
            "ROBINHOOD_MAX_ORDERS_PER_DAY": "1",
            "ROBINHOOD_SHADOW_LOG": str(tmp_path / "token.jsonl"),
            "ROBINHOOD_MCP_URL": "https://agent.robinhood.com/mcp/trading",
            "ROBINHOOD_MCP_TOKEN": "super-secret-token",
        },
    )
    assert result is not None
    assert result.legs[0].risk_status == "BLOCKED"
    assert "unsupported" in result.legs[0].risk_reason
    assert "not configured" in result.legs[0].risk_reason
    assert result.legs[0].execution_status == "NOT_SUBMITTED"
    assert result.submission_attempts == 0
    text = (tmp_path / "token.jsonl").read_text(encoding="utf-8")
    assert "super-secret-token" not in text


def test_reversed_quotes_price_the_execution_symbol():
    payloads = _payloads(
        get_equity_quotes=[
            {"symbol": "SQQQ", "bid": 20.00, "ask": 20.50, "quote_time": NOW.isoformat()},
            {"symbol": "TQQQ", "bid": 52.10, "ask": 52.14, "quote_time": NOW.isoformat()},
        ]
    )
    result, broker_state, _calls, state = _run(payloads)
    assert broker_state == "known"
    assert state.quote is None
    assert [quote.symbol for quote in state.quotes] == ["SQQQ", "TQQQ"]
    selected = quote_for_symbol(state, result.legs[0].intent.execution_symbol)
    assert selected is not None and selected.symbol == "TQQQ"
    assert selected.ask == 52.14
    broker = RobinhoodAgenticBroker(state)
    assert broker.get_quote("TQQQ") is not None and broker.get_quote("TQQQ").ask == 52.14
    assert broker.get_quote("SQQQ") is not None and broker.get_quote("SQQQ").ask == 20.50
    assert result.legs[0].risk_status == "PASS"
    assert result.legs[0].intent.estimated_price == 52.14
    assert result.legs[0].intent.estimated_price != 20.50


def test_missing_execution_quote_blocks():
    payloads = _payloads(
        get_equity_quotes=[
            {"symbol": "SQQQ", "bid": 20.00, "ask": 20.02, "quote_time": NOW.isoformat()},
        ]
    )
    result, broker_state, _calls, state = _run(payloads)
    assert broker_state == "known"
    assert quote_for_symbol(state, "TQQQ") is None
    assert len(state.quotes) == 1 and state.quotes[0].symbol == "SQQQ"
    leg = result.legs[0]
    assert leg.intent.execution_symbol == "TQQQ"
    assert leg.risk_status == "BLOCKED"
    assert "quote" in leg.risk_reason
    assert leg.intent.quantity == 0
    assert leg.intent.estimated_price is None
    assert leg.execution_status == "NOT_SUBMITTED"


def test_stale_execution_quote_blocks_when_the_other_etf_is_fresh():
    stale = (NOW - timedelta(seconds=120)).isoformat()
    payloads = _payloads(
        get_equity_quotes=[
            {"symbol": "SQQQ", "bid": 20.00, "ask": 20.02, "quote_time": NOW.isoformat()},
            {"symbol": "TQQQ", "bid": 52.10, "ask": 52.14, "quote_time": stale},
        ]
    )
    result, _broker_state, _calls, state = _run(payloads)
    assert quote_for_symbol(state, "TQQQ") is not None
    assert quote_for_symbol(state, "SQQQ") is not None
    leg = result.legs[0]
    assert leg.risk_status == "BLOCKED"
    assert "stale" in leg.risk_reason
    assert leg.intent.estimated_price != 20.02
    assert leg.intent.quantity == 0
    assert leg.execution_status == "NOT_SUBMITTED"


def test_spread_is_isolated_to_the_execution_symbol():
    tight_tqqq = {"symbol": "TQQQ", "bid": 52.10, "ask": 52.14, "quote_time": NOW.isoformat()}
    wide_sqqq = {"symbol": "SQQQ", "bid": 20.00, "ask": 21.00, "quote_time": NOW.isoformat()}
    wide_tqqq = {"symbol": "TQQQ", "bid": 52.00, "ask": 53.00, "quote_time": NOW.isoformat()}
    tight_sqqq = {"symbol": "SQQQ", "bid": 20.00, "ask": 20.02, "quote_time": NOW.isoformat()}

    wide_other, _broker_state, _calls, _state = _run(
        _payloads(get_equity_quotes=[wide_sqqq, tight_tqqq])
    )
    assert wide_other.legs[0].intent.execution_symbol == "TQQQ"
    assert wide_other.legs[0].risk_status == "PASS"
    assert wide_other.legs[0].intent.estimated_price == 52.14

    wide_execution, _broker_state, _calls, _state = _run(
        _payloads(get_equity_quotes=[tight_sqqq, wide_tqqq])
    )
    assert wide_execution.legs[0].risk_status == "BLOCKED"
    assert "spread" in wide_execution.legs[0].risk_reason
    assert wide_execution.legs[0].intent.quantity == 0

    sqqq_buy, _broker_state, _calls, _state = _run(
        _payloads(get_equity_quotes=[tight_tqqq, wide_sqqq]),
        alert=_alert(symbol="SQQQ", alert_type="BUY"),
    )
    assert sqqq_buy.legs[0].intent.execution_symbol == "SQQQ"
    assert sqqq_buy.legs[0].risk_status == "BLOCKED"
    assert "spread" in sqqq_buy.legs[0].risk_reason
    assert sqqq_buy.legs[0].intent.estimated_price != 52.14


def test_unconfigured_reader_blocks(tmp_path: Path):
    result = run_connected_shadow_after_strategy(
        _alert(),
        None,
        dry_run=True,
        webhook_url="",
        data_bar_start=NOW,
        now=NOW,
        env={
            "EXECUTION_BROKER": "robinhood_connected_shadow",
            "ROBINHOOD_NEW_ENTRIES_ENABLED": "true",
            "ROBINHOOD_MAX_POSITION_PCT": "0.50",
            "ROBINHOOD_MAX_ORDER_NOTIONAL": "100",
            "ROBINHOOD_MAX_DAILY_LOSS_PCT": "0.02",
            "ROBINHOOD_MAX_WEEKLY_LOSS_PCT": "0.04",
            "ROBINHOOD_MAX_DRAWDOWN_PCT": "0.10",
            "ROBINHOOD_MAX_ORDERS_PER_DAY": "1",
            "ROBINHOOD_SHADOW_LOG": str(tmp_path / "none.jsonl"),
        },
    )
    assert result is not None
    assert result.legs[0].risk_status == "BLOCKED"
    assert "not configured" in result.legs[0].risk_reason
    assert result.submission_attempts == 0


def test_production_modules_do_not_name_write_tools():
    root = Path(__file__).resolve().parents[1]
    watched = [
        root / "brokers" / "robinhood_reader.py",
        root / "brokers" / "robinhood_normalize.py",
        root / "robinhood_connected.py",
        root / "robinhood_reconcile.py",
        root / "main.py",
    ]
    for path in watched:
        text = path.read_text(encoding="utf-8")
        for name in WRITE_TOOLS:
            assert re.search(rf"\b{re.escape(name)}\b", text) is None, f"{path.name} mentions {name}"
