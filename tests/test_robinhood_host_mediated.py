"""Phase 5R: host-mediated Robinhood pilot — mock transport only.

Never opens a real socket to agent.robinhood.com for mutating calls.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from brokers.mode import ROBINHOOD_HOST_HANDOFF, parse_execution_broker
from brokers.robinhood_agentic import LIVE_SUBMISSION_IMPLEMENTED, RobinhoodAgenticBroker
from brokers.robinhood_reader import AUTH_CASE, UNATTENDED_AUTH_SUPPORTED, transport_from_env
from brokers.types import (
    AccountView,
    BrokerState,
    OpenOrderView,
    PositionView,
    QuoteView,
    TradeIntent,
    UnsafeBrokerConfiguration,
)
from robinhood_host_entry import InMemoryRhEntryReservationStore, reserve_rh_daily_entry
from robinhood_host_executor import (
    ROBINHOOD_HOST_SUBMISSION_IMPLEMENTED,
    HostMediatedClient,
    execute_flip_second_leg_gate,
    place_args_for_intent,
    run_host_executor_once,
)
from robinhood_host_handoff import (
    HANDOFF_SUBMISSION_IMPLEMENTED,
    run_robinhood_host_handoff,
)
from robinhood_host_risk import (
    check_host_new_exposure,
    host_arming_block_reason,
    host_limits_from_env,
    size_host_buy,
)
from robinhood_intent import integrity_digest, intent_from_row, verify_integrity, wrap_intent
from robinhood_intent_store import InMemoryIntentStore
from robinhood_shadow import _draft_intent
from strategy_params import STRATEGY_VERSION
from strategy_types import AlertDecision, PositionState

NOW = datetime(2026, 10, 7, 15, 0, tzinfo=timezone.utc)


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
        timestamp="2026-10-07T14:30:00Z",
        notes="Bull score exceeded threshold",
    )
    defaults.update(kwargs)
    return AlertDecision(**defaults)  # type: ignore[arg-type]


def _limits(**overrides: str):
    # $100 sleeve must afford ≥1 whole share at ~$50 ask → pct near 1.0 in tests.
    base = {
        "ROBINHOOD_HOST_MAX_POSITION_PCT": "1.0",
        "ROBINHOOD_HOST_MAX_ORDER_NOTIONAL": "100",
        "ROBINHOOD_HOST_CAPITAL_CEILING": "100",
        "ROBINHOOD_HOST_MAX_DAILY_LOSS_PCT": "0.05",
        "ROBINHOOD_HOST_MAX_WEEKLY_LOSS_PCT": "0.10",
        "ROBINHOOD_HOST_MAX_DRAWDOWN_PCT": "0.15",
        "ROBINHOOD_HOST_MAX_ORDERS_PER_DAY": "3",
        "ROBINHOOD_HOST_NEW_ENTRIES_ENABLED": "true",
        "ROBINHOOD_HOST_ENABLED": "true",
        "ROBINHOOD_HOST_LIVE_SUBMISSION": "true",
        "ROBINHOOD_HOST_MAX_QUOTE_AGE_SECONDS": "60",
        "ROBINHOOD_HOST_MAX_SPREAD_BPS": "50",
        "ROBINHOOD_HOST_MAX_DATA_AGE_MINUTES": "90",
    }
    base.update(overrides)
    return host_limits_from_env(base)


def _state(
    *,
    known: bool = True,
    equity: float = 100.0,
    cash: float = 100.0,
    bp: float = 100.0,
    positions: tuple[PositionView, ...] = (),
    open_orders: tuple[OpenOrderView, ...] = (),
) -> BrokerState:
    quotes = (
        QuoteView("TQQQ", 50.0, 50.10, NOW, last=50.05),
        QuoteView("SQQQ", 20.0, 20.05, NOW, last=20.02),
    )
    account = AccountView(
        available=True,
        status="ACTIVE",
        buying_power=bp,
        equity=equity,
        day_start_equity=equity,
        week_start_equity=equity,
        peak_equity=equity,
        cash=cash,
    )
    return BrokerState(
        known=known,
        account=account if known else None,
        positions=positions,
        open_orders=open_orders,
        quotes=quotes,
        now=NOW,
        data_bar_start=NOW - timedelta(minutes=5),
        order_history_complete=True,
    )


def _armed_env(**overrides: str) -> dict[str, str]:
    base = {
        "ROBINHOOD_HOST_EXECUTOR": "true",
        "ROBINHOOD_HOST_ENABLED": "true",
        "ROBINHOOD_HOST_NEW_ENTRIES_ENABLED": "true",
        "ROBINHOOD_HOST_LIVE_SUBMISSION": "true",
        "LIVE_TRADING_ENABLED": "true",
        "ROBINHOOD_HOST_MAX_POSITION_PCT": "1.0",
        "ROBINHOOD_HOST_MAX_ORDER_NOTIONAL": "100",
        "ROBINHOOD_HOST_CAPITAL_CEILING": "100",
        "ROBINHOOD_HOST_MAX_DAILY_LOSS_PCT": "0.05",
        "ROBINHOOD_HOST_MAX_WEEKLY_LOSS_PCT": "0.10",
        "ROBINHOOD_HOST_MAX_DRAWDOWN_PCT": "0.15",
        "ROBINHOOD_HOST_MAX_ORDERS_PER_DAY": "3",
        "ROBINHOOD_HOST_MAX_QUOTE_AGE_SECONDS": "60",
        "ROBINHOOD_HOST_MAX_SPREAD_BPS": "50",
        "ROBINHOOD_HOST_MAX_DATA_AGE_MINUTES": "90",
    }
    base.update(overrides)
    return base


def _snapshot_payload() -> dict[str, Any]:
    return {
        "get_accounts": {"accounts": [{"status": "active", "account_number": "mcp1"}]},
        "get_portfolio": {
            "equity": 100.0,
            "buying_power": 100.0,
            "cash": 100.0,
            "day_start_equity": 100.0,
            "week_start_equity": 100.0,
            "peak_equity": 100.0,
        },
        "get_equity_positions": {"positions": []},
        "get_equity_quotes": {
            "quotes": [
                {
                    "symbol": "TQQQ",
                    "bid": 50.0,
                    "ask": 50.10,
                    "last": 50.05,
                    "quote_time": NOW.isoformat(),
                },
                {
                    "symbol": "SQQQ",
                    "bid": 20.0,
                    "ask": 20.05,
                    "last": 20.02,
                    "quote_time": NOW.isoformat(),
                },
            ]
        },
        "get_equity_orders": {"orders": []},
    }


class FakeHostTransport:
    def __init__(self, *, place_status: str = "FILLED", place_error: Exception | None = None) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.place_status = place_status
        self.place_error = place_error
        self.snapshot = _snapshot_payload()

    def __call__(self, name: str, arguments: dict[str, Any]) -> Any:
        self.calls.append((name, dict(arguments)))
        if name in self.snapshot:
            return self.snapshot[name]
        if name == "review_equity_order":
            return {"ok": True, "warnings": []}
        if name == "place_equity_order":
            if self.place_error is not None:
                raise self.place_error
            if self.place_status == "AMBIGUOUS":
                return {"status": "UNKNOWN"}
            return {
                "status": self.place_status,
                "id": "rh-order-1",
                "filled_qty": arguments.get("quantity"),
            }
        raise AssertionError(f"unexpected tool {name}")


def test_baseline_flags_and_case_c():
    assert STRATEGY_VERSION == "1.0.0"
    assert LIVE_SUBMISSION_IMPLEMENTED is False
    assert HANDOFF_SUBMISSION_IMPLEMENTED is False
    assert AUTH_CASE == "C"
    assert UNATTENDED_AUTH_SUPPORTED is False
    assert transport_from_env({"ROBINHOOD_MCP_TOKEN": "x", "ROBINHOOD_MCP_URL": "https://example"}) is None
    assert parse_execution_broker("robinhood_host_handoff") == ROBINHOOD_HOST_HANDOFF
    with pytest.raises(UnsafeBrokerConfiguration):
        parse_execution_broker("robinhood_live")


def test_integrity_digest_stable_and_verified():
    intent = _draft_intent(
        _alert(),
        symbol="TQQQ",
        action="BUY",
        purpose="entry",
        price=50.10,
        quantity=1,
    )
    digest = integrity_digest(intent)
    assert verify_integrity(intent, digest)
    durable = wrap_intent(intent, now=NOW, ttl_seconds=600)
    assert durable.integrity_digest == digest
    row = durable.as_row()
    again = intent_from_row(row)
    assert again.intent.client_order_id == intent.client_order_id
    row["quantity"] = 99
    with pytest.raises(ValueError, match="integrity"):
        intent_from_row(row)


def test_atomic_claim_unique_and_cas():
    store = InMemoryIntentStore()
    intent = _draft_intent(
        _alert(),
        symbol="TQQQ",
        action="BUY",
        purpose="entry",
        price=50.10,
        quantity=1,
    )
    durable = wrap_intent(intent, now=NOW)
    assert store.create_pending(durable).ok
    assert not store.create_pending(durable).ok
    first = store.claim_next_pending(claimed_by="host-a", now=NOW)
    assert first.ok and first.row is not None
    assert first.row["status"] == "CLAIMED"
    second = store.claim_by_client_order_id(intent.client_order_id, claimed_by="host-b", now=NOW)
    assert not second.ok


def test_expired_intent_not_claimable():
    store = InMemoryIntentStore()
    intent = _draft_intent(
        _alert(),
        symbol="TQQQ",
        action="BUY",
        purpose="entry",
        price=50.10,
        quantity=1,
    )
    durable = wrap_intent(intent, now=NOW - timedelta(hours=1), ttl_seconds=60)
    assert store.create_pending(durable).ok
    claim = store.claim_next_pending(claimed_by="host", now=NOW)
    assert not claim.ok
    assert "expired" in claim.reason


def test_capital_ceiling_blocks_above_equity():
    limits = _limits(ROBINHOOD_HOST_CAPITAL_CEILING="100")
    state = _state(equity=150.0, cash=150.0, bp=150.0)
    intent = _draft_intent(
        _alert(),
        symbol="TQQQ",
        action="BUY",
        purpose="entry",
        price=50.10,
        quantity=1,
    )
    ok, reason, _ = check_host_new_exposure(intent, state, limits)
    assert not ok
    assert "capital ceiling" in reason


def test_size_uses_cash_not_inflated_bp():
    limits = _limits()
    state = _state(equity=100.0, cash=50.0, bp=500.0)
    qty, notional = size_host_buy(50.10, state, limits)
    assert qty == 0 or notional <= 50.0 + 0.01


def test_kill_switches_default_block_arming():
    reason = host_arming_block_reason(
        {},
        submission_implemented=True,
        host_executor=True,
    )
    assert reason is not None
    assert host_arming_block_reason(
        _armed_env(RENDER="true"),
        submission_implemented=True,
        host_executor=True,
    ) == "Render runtime cannot submit Robinhood orders under Case C"


def test_handoff_persists_pending_never_submits():
    store = InMemoryIntentStore()
    reservation = InMemoryRhEntryReservationStore()
    broker = RobinhoodAgenticBroker(_state())
    result = run_robinhood_host_handoff(
        _alert(),
        None,
        _state(),
        _limits(),
        store=store,
        reservation_store=reservation,
        now=NOW,
        broker=broker,
    )
    assert result.submission_attempts == 0
    assert result.pending_created == 1
    assert broker.submission_attempts == 0
    row = next(iter(store.rows.values()))
    assert row["status"] == "PENDING"
    assert row["integrity_digest"]


def test_handoff_flip_blocks_second_leg_without_fill():
    store = InMemoryIntentStore()
    result = run_robinhood_host_handoff(
        _alert(alert_type="FLIP", symbol="SQQQ"),
        PositionState(active_symbol="TQQQ"),
        _state(positions=(PositionView("TQQQ", 2),)),
        _limits(),
        store=store,
        reservation_store=InMemoryRhEntryReservationStore(),
        now=NOW,
    )
    assert any(leg.intent.action == "SELL" for leg in result.legs)
    buys = [leg for leg in result.legs if leg.intent.action == "BUY"]
    assert buys
    assert buys[0].risk_status == "BLOCKED"
    assert all(leg.execution_status != "SUBMITTED" for leg in result.legs)


def test_daily_entry_reservation_blocks_second_buy():
    store = InMemoryRhEntryReservationStore()
    first = reserve_rh_daily_entry(store, now=NOW, meta={"client_order_id": "a"})
    second = reserve_rh_daily_entry(store, now=NOW, meta={"client_order_id": "b"})
    assert first.reserved
    assert not second.reserved


def test_host_executor_places_once_with_mock_transport(tmp_path: Path):
    store = InMemoryIntentStore()
    intent = _draft_intent(
        _alert(),
        symbol="TQQQ",
        action="BUY",
        purpose="entry",
        price=50.10,
        quantity=1,
    )
    # Align qty with what risk will allow under $100 ceiling.
    intent = TradeIntent(
        **{
            **intent.as_audit(),
            "quantity": 1,
            "estimated_price": 50.10,
            "estimated_notional": 50.10,
        }
    )  # type: ignore[arg-type]
    assert store.create_pending(wrap_intent(intent, now=NOW)).ok
    transport = FakeHostTransport(place_status="FILLED")
    env = _armed_env(ROBINHOOD_HOST_EXEC_LOG=str(tmp_path / "exec.jsonl"))
    result = run_host_executor_once(
        transport=transport,
        store=store,
        env=env,
        now=NOW,
        data_bar_start=NOW - timedelta(minutes=5),
        client_order_id=intent.client_order_id,
    )
    assert result.place_attempts == 1
    assert result.legs[0].execution_status == "FILLED"
    place_calls = [c for c in transport.calls if c[0] == "place_equity_order"]
    assert len(place_calls) == 1
    assert place_calls[0][1]["client_order_id"] == intent.client_order_id
    assert store.get(intent.client_order_id)["status"] == "FILLED"


def test_unknown_place_does_not_resubmit(tmp_path: Path):
    store = InMemoryIntentStore()
    intent = _draft_intent(
        _alert(),
        symbol="TQQQ",
        action="BUY",
        purpose="entry",
        price=50.10,
        quantity=1,
    )
    assert store.create_pending(wrap_intent(intent, now=NOW)).ok
    transport = FakeHostTransport(place_status="AMBIGUOUS")
    result = run_host_executor_once(
        transport=transport,
        store=store,
        env=_armed_env(ROBINHOOD_HOST_EXEC_LOG=str(tmp_path / "exec.jsonl")),
        now=NOW,
        data_bar_start=NOW - timedelta(minutes=5),
        client_order_id=intent.client_order_id,
    )
    assert result.legs[0].execution_status == "UNKNOWN"
    assert len([c for c in transport.calls if c[0] == "place_equity_order"]) == 1
    # Second claim must fail (not PENDING).
    again = run_host_executor_once(
        transport=transport,
        store=store,
        env=_armed_env(ROBINHOOD_HOST_EXEC_LOG=str(tmp_path / "exec2.jsonl")),
        now=NOW,
        data_bar_start=NOW - timedelta(minutes=5),
        client_order_id=intent.client_order_id,
    )
    assert again.legs == ()
    assert len([c for c in transport.calls if c[0] == "place_equity_order"]) == 1


def test_disarmed_host_never_places(tmp_path: Path):
    store = InMemoryIntentStore()
    intent = _draft_intent(
        _alert(),
        symbol="TQQQ",
        action="BUY",
        purpose="entry",
        price=50.10,
        quantity=1,
    )
    assert store.create_pending(wrap_intent(intent, now=NOW)).ok
    transport = FakeHostTransport()
    env = _armed_env(
        ROBINHOOD_HOST_LIVE_SUBMISSION="false",
        ROBINHOOD_HOST_EXEC_LOG=str(tmp_path / "exec.jsonl"),
    )
    result = run_host_executor_once(
        transport=transport,
        store=store,
        env=env,
        now=NOW,
        data_bar_start=NOW - timedelta(minutes=5),
        client_order_id=intent.client_order_id,
    )
    assert result.place_attempts == 0
    assert not any(name == "place_equity_order" for name, _ in transport.calls)
    assert result.legs[0].execution_status == "BLOCKED"


def test_render_env_cannot_arm_submission():
    reason = host_arming_block_reason(
        _armed_env(RENDER_SERVICE_TYPE="cron"),
        submission_implemented=ROBINHOOD_HOST_SUBMISSION_IMPLEMENTED,
        host_executor=True,
    )
    assert reason is not None
    assert "Render" in reason


def test_qqq_and_crypto_rejected():
    limits = _limits()
    state = _state()
    bad = _draft_intent(
        _alert(symbol="QQQ"),
        symbol="QQQ",
        action="BUY",
        purpose="entry",
        price=400.0,
        quantity=1,
    )
    ok, reason, _ = check_host_new_exposure(bad, state, limits)
    assert not ok
    assert "QQQ" in reason or "allowlist" in reason or "signal" in reason


def test_flip_gate_requires_flat_fill():
    allowed, reason = execute_flip_second_leg_gate(
        exit_status="SUBMITTED",
        exit_qty_remaining=1.0,
        broker_state_known=True,
    )
    assert not allowed
    allowed2, _ = execute_flip_second_leg_gate(
        exit_status="FILLED",
        exit_qty_remaining=0.0,
        broker_state_known=True,
    )
    assert allowed2


def test_place_args_include_client_order_id():
    intent = _draft_intent(
        _alert(),
        symbol="TQQQ",
        action="BUY",
        purpose="entry",
        price=50.10,
        quantity=2,
    )
    args = place_args_for_intent(intent)
    assert args["client_order_id"] == intent.client_order_id
    assert args["symbol"] == "TQQQ"
    assert args["side"] == "buy"


def test_host_client_rejects_unlisted_write_when_disarmed():
    client = HostMediatedClient(FakeHostTransport(), allow_writes=False)
    with pytest.raises(Exception):
        client.call("place_equity_order", {"symbol": "TQQQ"})


def test_agentic_broker_still_refuses_submit():
    broker = RobinhoodAgenticBroker(_state())
    intent = _draft_intent(
        _alert(),
        symbol="TQQQ",
        action="BUY",
        purpose="entry",
        price=50.10,
        quantity=1,
    )
    with pytest.raises(Exception):
        broker.submit_order(intent)
    assert broker.submission_attempts == 1
