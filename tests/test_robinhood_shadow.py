"""Robinhood shadow must be unable to submit a real order."""

from __future__ import annotations

import inspect
from datetime import datetime, timedelta, timezone

import pytest

from brokers.alpaca_adapter import AlpacaPaperBroker
from brokers.mode import execution_broker_from_environ, parse_execution_broker
from brokers.robinhood_agentic import LIVE_SUBMISSION_IMPLEMENTED, RobinhoodAgenticBroker
from brokers.types import (
    EXECUTION_NOT_SUBMITTED,
    AccountView,
    BrokerState,
    LiveSubmissionDisabled,
    OpenOrderView,
    PositionView,
    QuoteView,
    RobinhoodLimits,
    TradeIntent,
    UnsafeBrokerConfiguration,
)
from robinhood_audit import ShadowAuditLog, redact
from robinhood_flip import advance_flip, recovery_block_reason
from robinhood_risk import execution_symbol_block_reason, limits_from_env
from robinhood_shadow import run_robinhood_shadow, shadow_client_order_id
from strategy_params import STRATEGY_VERSION
from strategy_types import AlertDecision, PositionState


NOW = datetime(2026, 10, 5, 15, 0, tzinfo=timezone.utc)


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
        notes_kind="buy_bull",
    )
    defaults.update(kwargs)
    return AlertDecision(**defaults)  # type: ignore[arg-type]


def _limits(**overrides: object) -> RobinhoodLimits:
    raw = dict(
        max_position_pct=0.50,
        max_order_notional=104.28,
        max_daily_loss_pct=0.02,
        max_weekly_loss_pct=0.04,
        max_drawdown_pct=0.10,
        max_orders_per_day=2,
        max_quote_age_seconds=30,
        max_spread_bps=30,
        max_data_age_minutes=90,
        new_entries_enabled=True,
        live_enabled_flag=True,
    )
    raw.update(overrides)
    return RobinhoodLimits(**raw)  # type: ignore[arg-type]


def _state(**overrides: object) -> BrokerState:
    account = AccountView(
        available=True,
        status="active",
        buying_power=10_000.0,
        equity=10_000.0,
        day_start_equity=10_000.0,
        week_start_equity=10_000.0,
        peak_equity=10_000.0,
    )
    quote = QuoteView("TQQQ", bid=52.10, ask=52.14, quote_time=NOW, last=52.12)
    raw = dict(
        known=True,
        account=account,
        positions=(),
        open_orders=(),
        known_client_order_ids=frozenset(),
        orders_today=0,
        quote=quote,
        data_bar_start=NOW - timedelta(minutes=30),
        now=NOW,
    )
    raw.update(overrides)
    return BrokerState(**raw)  # type: ignore[arg-type]


def _run(alert: AlertDecision, state: BrokerState | None = None, limits: RobinhoodLimits | None = None, **kwargs: object):
    broker = RobinhoodAgenticBroker(state or _state())

    def _boom(self, order):  # type: ignore[no-untyped-def]
        raise AssertionError(f"submit_order called for {order.execution_symbol}")

    monkey = kwargs.pop("monkeypatch", None)
    if monkey is not None:
        monkey.setattr(RobinhoodAgenticBroker, "submit_order", _boom)
    return run_robinhood_shadow(alert, kwargs.pop("position", None), state or _state(), limits or _limits(), broker=broker, **kwargs)


def test_modes():
    assert parse_execution_broker("alpaca_paper") == "alpaca_paper"
    assert parse_execution_broker(" robinhood_shadow ") == "robinhood_shadow"
    assert execution_broker_from_environ({}) == "alpaca_paper"
    for raw in ("", "   ", None, "robinhood_live", "live", "nope", "robinhood"):
        with pytest.raises(UnsafeBrokerConfiguration):
            parse_execution_broker(raw)
    with pytest.raises(UnsafeBrokerConfiguration):
        execution_broker_from_environ({"EXECUTION_BROKER": ""})


def test_symbol_allowlist():
    assert execution_symbol_block_reason("tqqq") is None
    assert execution_symbol_block_reason("SQQQ") is None
    assert "signal" in (execution_symbol_block_reason("QQQ") or "")
    assert "not on the Robinhood allowlist" in (execution_symbol_block_reason("AAPL") or "")
    assert "options" in (execution_symbol_block_reason("TQQQ 251219C00050000") or "")
    assert "crypto" in (execution_symbol_block_reason("BTC-USD") or "")
    assert execution_symbol_block_reason("") == "malformed symbol"
    assert execution_symbol_block_reason("@@@")


def test_buy_tqqq_shadow_passes_risk_and_does_not_submit():
    assert LIVE_SUBMISSION_IMPLEMENTED is False
    assert "requests" not in inspect.getsource(RobinhoodAgenticBroker)
    result = _run(_alert())
    assert result.submitted is False
    assert result.submission_attempts == 0
    leg = result.legs[0]
    assert leg.execution_status == EXECUTION_NOT_SUBMITTED
    assert leg.risk_status == "PASS"
    assert leg.intent.execution_symbol == "TQQQ"
    assert leg.intent.action == "BUY"
    assert leg.intent.signal_symbol == "QQQ"
    assert leg.intent.quantity == 2
    assert leg.intent.estimated_price == pytest.approx(52.14)
    assert leg.intent.estimated_notional == pytest.approx(104.28)
    assert leg.intent.strategy_version == STRATEGY_VERSION == "1.0.0"
    assert "NO REAL MONEY TRADED" in result.text
    assert "SHADOW MODE" in result.text
    broker = RobinhoodAgenticBroker(_state())
    with pytest.raises(LiveSubmissionDisabled):
        broker.submit_order(leg.intent)
    with pytest.raises(LiveSubmissionDisabled):
        broker.cancel_order("abc")


def test_new_entries_flag_blocks_without_submitting():
    result = run_robinhood_shadow(_alert(), None, _state(), _limits(new_entries_enabled=False))
    assert result.legs[0].risk_status == "BLOCKED"
    assert result.legs[0].execution_status == EXECUTION_NOT_SUBMITTED
    assert "NEW_ENTRIES" in result.legs[0].risk_reason


def test_missing_limits_block_buy():
    blocked = run_robinhood_shadow(
        _alert(),
        None,
        _state(),
        _limits(max_order_notional=None, max_daily_loss_pct=None),
    )
    assert blocked.legs[0].risk_status == "BLOCKED"
    assert blocked.submitted is False


def test_stale_quote_stale_data_wide_spread_and_buying_power():
    stale_quote = _state(quote=QuoteView("TQQQ", 52.0, 52.1, NOW - timedelta(seconds=120)))
    assert "stale quote" in run_robinhood_shadow(_alert(), None, stale_quote, _limits()).legs[0].risk_reason
    stale_bar = _state(data_bar_start=NOW - timedelta(hours=5))
    assert run_robinhood_shadow(_alert(), None, stale_bar, _limits()).legs[0].risk_status == "BLOCKED"
    wide = _state(quote=QuoteView("TQQQ", 50.0, 52.14, NOW))
    assert "spread" in run_robinhood_shadow(_alert(), None, wide, _limits()).legs[0].risk_reason
    poor = _state(
        account=AccountView(True, "active", buying_power=10.0, equity=10_000, day_start_equity=10_000, week_start_equity=10_000, peak_equity=10_000)
    )
    assert "buying power" in run_robinhood_shadow(_alert(), None, poor, _limits()).legs[0].risk_reason


def test_loss_locks_and_unknown_broker():
    down = AccountView(True, "active", 10_000, equity=9_000, day_start_equity=10_000, week_start_equity=10_000, peak_equity=10_000)
    assert "daily loss" in run_robinhood_shadow(_alert(), None, _state(account=down), _limits()).legs[0].risk_reason
    week = AccountView(True, "active", 10_000, equity=9_500, day_start_equity=9_500, week_start_equity=10_000, peak_equity=10_000)
    assert "weekly loss" in run_robinhood_shadow(_alert(), None, _state(account=week), _limits(max_weekly_loss_pct=0.04)).legs[0].risk_reason
    peak = AccountView(True, "active", 10_000, equity=8_000, day_start_equity=8_000, week_start_equity=8_000, peak_equity=10_000)
    assert "drawdown" in run_robinhood_shadow(_alert(), None, _state(account=peak), _limits()).legs[0].risk_reason
    unknown = run_robinhood_shadow(_alert(), None, BrokerState(known=False, now=NOW), _limits())
    assert "unknown broker" in unknown.legs[0].risk_reason
    assert unknown.submitted is False


def test_duplicate_intent_and_restart():
    first = run_robinhood_shadow(_alert(), None, _state(), _limits())
    client_id = first.legs[0].intent.client_order_id
    again = run_robinhood_shadow(
        _alert(),
        None,
        _state(known_client_order_ids=frozenset({client_id})),
        _limits(),
    )
    assert "duplicate" in again.legs[0].risk_reason
    same = shadow_client_order_id(
        strategy_version=STRATEGY_VERSION,
        timestamp=_alert().timestamp,
        symbol="TQQQ",
        action="BUY",
        purpose="entry",
    )
    assert same == client_id
    assert recovery_block_reason(
        open_order_count=1,
        proposed_client_order_id=same,
        known_client_order_ids=frozenset(),
        broker_state_known=True,
    )
    assert recovery_block_reason(
        open_order_count=0,
        proposed_client_order_id=same,
        known_client_order_ids=frozenset({same}),
        broker_state_known=True,
    )
    assert recovery_block_reason(
        open_order_count=0,
        proposed_client_order_id="other",
        known_client_order_ids=frozenset(),
        broker_state_known=False,
    )


def test_rejected_symbols_do_not_submit():
    for symbol in ("QQQ", "AAPL", "BTC-USD", "TQQQ 251219C00050000", ""):
        result = run_robinhood_shadow(_alert(symbol=symbol), None, _state(), _limits())
        assert result.submitted is False
        assert result.legs[0].risk_status == "BLOCKED"
        assert result.legs[0].execution_status == EXECUTION_NOT_SUBMITTED


def test_flip_second_leg_gates():
    held = _state(
        positions=(PositionView("TQQQ", 2),),
        quote=QuoteView("SQQQ", bid=20.0, ask=20.02, quote_time=NOW),
    )
    # Quote on the entry symbol only; exit still plans from the position.
    fresh = run_robinhood_shadow(
        _alert(alert_type="FLIP", symbol="SQQQ", notes_kind="flip"),
        PositionState(active_symbol="TQQQ"),
        held,
        _limits(),
    )
    assert [leg.intent.action for leg in fresh.legs] == ["SELL", "BUY"]
    assert fresh.legs[0].intent.execution_symbol == "TQQQ"
    assert fresh.legs[0].risk_status == "PASS"
    assert fresh.legs[1].risk_status == "BLOCKED"
    assert "second leg" in fresh.legs[1].risk_reason
    assert fresh.submitted is False

    cases = [
        ("ACCEPTED", 2.0, "accepted"),
        ("PARTIALLY_FILLED", 1.0, "partial"),
        ("REJECTED", 2.0, "rejected"),
        ("TIMEOUT", 2.0, "timeout"),
        ("UNKNOWN", 2.0, "unknown"),
        ("FILLED", 1.0, "still open"),
    ]
    for status, qty, needle in cases:
        step = advance_flip(exit_status=status, exit_qty_remaining=qty, broker_state_known=True)
        assert step.entry_validation_allowed is False
        assert needle in step.reason
    assert advance_flip(exit_status="FILLED", exit_qty_remaining=0, broker_state_known=False).entry_validation_allowed is False
    ready = advance_flip(exit_status="FILLED", exit_qty_remaining=0, broker_state_known=True)
    assert ready.entry_validation_allowed is True
    verified = run_robinhood_shadow(
        _alert(alert_type="FLIP", symbol="SQQQ", notes_kind="flip"),
        PositionState(active_symbol="TQQQ"),
        _state(
            positions=(),
            quote=QuoteView("SQQQ", bid=20.0, ask=20.02, quote_time=NOW),
        ),
        _limits(max_order_notional=40.04),
        exit_status="FILLED",
        exit_qty_remaining=0,
    )
    assert verified.legs[1].risk_status == "PASS"
    assert verified.legs[1].execution_status == EXECUTION_NOT_SUBMITTED
    assert verified.legs[1].intent.quantity == 2
    assert verified.submitted is False


def test_open_order_conflict_blocks_buy():
    state = _state(
        open_orders=(OpenOrderView("rh-old", "TQQQ", "buy", "ACCEPTED"),)
    )
    result = run_robinhood_shadow(_alert(), None, state, _limits())
    assert result.legs[0].risk_status == "BLOCKED"


def test_audit_redacts_and_marks_not_submitted(tmp_path):
    result = run_robinhood_shadow(_alert(), None, _state(), _limits())
    log = ShadowAuditLog(tmp_path / "shadow.jsonl")
    row = result.legs[0].audit_row(broker_state="known")
    row["api_key"] = "super-secret"
    log.append(row)
    stored = log.read()[0]
    assert stored["execution_status"] == "NOT_SUBMITTED"
    assert stored["execution_mode"] == "shadow"
    assert stored["broker"] == "robinhood"
    assert stored["api_key"] == "[redacted]"
    assert redact({"password": "x"})["password"] == "[redacted]"


def test_limits_parser_fail_closed():
    parsed = limits_from_env({})
    assert parsed.max_order_notional is None
    assert parsed.new_entries_enabled is False
    assert parsed.live_enabled_flag is False
    with pytest.raises(UnsafeBrokerConfiguration):
        limits_from_env({"ROBINHOOD_MAX_POSITION_PCT": "15"})
    with pytest.raises(UnsafeBrokerConfiguration):
        limits_from_env({"ROBINHOOD_MAX_ORDER_NOTIONAL": "0"})


def test_alpaca_adapter_does_not_submit():
    broker = AlpacaPaperBroker(_state())
    intent = TradeIntent(
        strategy_version="1.0.0",
        signal_symbol="QQQ",
        execution_symbol="TQQQ",
        action="BUY",
        quantity=1,
        estimated_price=1,
        estimated_notional=1,
        confidence=1,
        regime="",
        reason="",
        signal_id="x",
        timestamp="t",
        purpose="entry",
        client_order_id="abc",
    )
    with pytest.raises(LiveSubmissionDisabled):
        broker.submit_order(intent)


def test_action_flip_string_is_rejected_as_a_broker_action():
    from robinhood_risk import action_block_reason

    assert "FLIP" in (action_block_reason("FLIP") or "")
    assert action_block_reason("BUY") is None
    assert action_block_reason("SELL") is None
    assert action_block_reason("short")
