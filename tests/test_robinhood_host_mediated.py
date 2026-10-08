"""Phase 5R / 5R.1: host-mediated Robinhood pilot — mock transport only.

Never opens a real socket to agent.robinhood.com for mutating calls.
Thirty hardening cases cover Render PENDING, schema allowlist, idempotency,
reservation, FLIP, kills, equity baselines, capital, and Case C boundary.
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
from robinhood_host_equity import InMemoryEquityBaselineStore, merge_account_baselines
from robinhood_host_executor import (
    ROBINHOOD_HOST_SUBMISSION_IMPLEMENTED,
    HostMediatedClient,
    HostTransportError,
    execute_flip_second_leg_gate,
    place_args_for_intent,
    run_host_executor_once,
)
from robinhood_host_handoff import (
    HANDOFF_SUBMISSION_IMPLEMENTED,
    run_robinhood_host_handoff,
    run_robinhood_host_handoff_after_strategy,
)
from robinhood_host_risk import (
    check_handoff_prevalidation,
    check_host_new_exposure,
    host_arming_block_reason,
    host_limits_from_env,
    size_host_buy,
)
from robinhood_host_schema import (
    DOCS_CONFIRMED_CLIENT_ORDER_ID_PARAM,
    HostMcpCapabilities,
    assert_capabilities_ready,
    capabilities_from_tools_list,
    docs_confirmed_capabilities,
    filter_place_args,
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
        "ROBINHOOD_HOST_MAX_SLIPPAGE_BPS": "25",
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
    day_start: float | None = None,
    week_start: float | None = None,
    peak: float | None = None,
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
        day_start_equity=equity if day_start is None else day_start,
        week_start_equity=equity if week_start is None else week_start,
        peak_equity=equity if peak is None else peak,
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
        "ROBINHOOD_HOST_MAX_SLIPPAGE_BPS": "25",
    }
    base.update(overrides)
    return base


def _snapshot_payload(*, include_baselines: bool = True) -> dict[str, Any]:
    portfolio: dict[str, Any] = {
        "equity": 100.0,
        "buying_power": 100.0,
        "cash": 100.0,
    }
    if include_baselines:
        portfolio.update(
            {
                "day_start_equity": 100.0,
                "week_start_equity": 100.0,
                "peak_equity": 100.0,
            }
        )
    return {
        "get_accounts": {
            "accounts": [
                {
                    "account_number": "TESTAGT6650",
                    "nickname": "Agentic",
                    "type": "cash",
                    "brokerage_account_type": "individual",
                    "agentic_allowed": True,
                    "state": "active",
                    "status": "active",
                }
            ]
        },
        "get_portfolio": portfolio,
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
    def __init__(
        self,
        *,
        place_status: str = "FILLED",
        place_error: Exception | None = None,
        include_baselines: bool = True,
        positions: list[dict[str, Any]] | None = None,
    ) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.place_status = place_status
        self.place_error = place_error
        self.snapshot = _snapshot_payload(include_baselines=include_baselines)
        if positions is not None:
            self.snapshot["get_equity_positions"] = {"positions": positions}

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
            if self.place_status == "MALFORMED":
                return "not-a-dict"
            return {
                "status": self.place_status,
                "id": "rh-order-1",
                "filled_qty": arguments.get("quantity"),
            }
        raise AssertionError(f"unexpected tool {name}")


def _buy_intent(qty: int = 1) -> TradeIntent:
    intent = _draft_intent(
        _alert(),
        symbol="TQQQ",
        action="BUY",
        purpose="entry",
        price=50.10,
        quantity=qty,
    )
    return TradeIntent(
        **{
            **intent.as_audit(),
            "quantity": qty,
            "estimated_price": 50.10,
            "estimated_notional": round(qty * 50.10, 2),
        }
    )  # type: ignore[arg-type]


def _sell_intent(qty: int = 2) -> TradeIntent:
    return _draft_intent(
        _alert(alert_type="SELL", symbol="TQQQ"),
        symbol="TQQQ",
        action="SELL",
        purpose="exit",
        price=50.0,
        quantity=qty,
    )


# --- 1–3 baseline / Case C -------------------------------------------------


def test_01_baseline_flags_and_case_c():
    assert STRATEGY_VERSION == "1.0.0"
    assert LIVE_SUBMISSION_IMPLEMENTED is False
    assert HANDOFF_SUBMISSION_IMPLEMENTED is False
    assert AUTH_CASE == "C"
    assert UNATTENDED_AUTH_SUPPORTED is False
    assert transport_from_env({"ROBINHOOD_MCP_TOKEN": "x", "ROBINHOOD_MCP_URL": "https://example"}) is None
    assert parse_execution_broker("robinhood_host_handoff") == ROBINHOOD_HOST_HANDOFF
    with pytest.raises(UnsafeBrokerConfiguration):
        parse_execution_broker("robinhood_live")


def test_02_integrity_digest_stable_and_verified():
    intent = _buy_intent()
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


def test_03_atomic_claim_unique_and_cas():
    store = InMemoryIntentStore()
    intent = _buy_intent()
    durable = wrap_intent(intent, now=NOW)
    assert store.create_pending(durable).ok
    assert not store.create_pending(durable).ok
    first = store.claim_next_pending(claimed_by="host-a", now=NOW)
    assert first.ok and first.row is not None
    assert first.row["status"] == "CLAIMED"
    second = store.claim_by_client_order_id(intent.client_order_id, claimed_by="host-b", now=NOW)
    assert not second.ok


# --- 4–7 Render PENDING / unknown state ------------------------------------


def test_04_render_unknown_state_persists_pending_buy():
    """HIGH: BrokerState(known=False) → PENDING BUY without fabricating account."""
    store = InMemoryIntentStore()
    result = run_robinhood_host_handoff(
        _alert(),
        None,
        _state(known=False),
        _limits(),
        store=store,
        reservation_store=None,
        now=NOW,
    )
    assert result.submission_attempts == 0
    assert result.pending_created == 1
    row = next(iter(store.rows.values()))
    assert row["status"] == "PENDING"
    assert row["meta"].get("pending_not_approved") is True
    assert row["meta"].get("broker_state_known_at_handoff") is False


def test_05_pending_is_not_approval_host_blocks_unknown():
    store = InMemoryIntentStore()
    intent = _buy_intent()
    assert store.create_pending(wrap_intent(intent, now=NOW)).ok
    transport = FakeHostTransport()
    # Force unknown by breaking snapshot
    transport.snapshot = {"get_accounts": "bad"}

    class BrokenTransport(FakeHostTransport):
        def __call__(self, name: str, arguments: dict[str, Any]) -> Any:
            self.calls.append((name, dict(arguments)))
            raise HostTransportError("read failed")

    broken = BrokenTransport()
    result = run_host_executor_once(
        transport=broken,
        store=store,
        env=_armed_env(),
        now=NOW,
        data_bar_start=NOW - timedelta(minutes=5),
        client_order_id=intent.client_order_id,
        reservation_store=InMemoryRhEntryReservationStore(),
    )
    assert result.place_attempts == 0
    assert result.legs[0].execution_status == "BLOCKED"
    reason = result.legs[0].risk_reason.lower()
    assert "unknown" in reason or "read failed" in reason or "not approval" in reason


def test_06_after_strategy_unknown_path_creates_pending(tmp_path: Path):
    store = InMemoryIntentStore()
    result = run_robinhood_host_handoff_after_strategy(
        _alert(),
        None,
        dry_run=True,
        webhook_url="",
        data_bar_start=NOW - timedelta(minutes=5),
        now=NOW,
        env={
            "EXECUTION_BROKER": "robinhood_host_handoff",
            "ROBINHOOD_HOST_HANDOFF_LOG": str(tmp_path / "h.jsonl"),
            **{k: v for k, v in _armed_env().items() if k.startswith("ROBINHOOD_HOST_MAX")},
            "ROBINHOOD_HOST_CAPITAL_CEILING": "100",
            "ROBINHOOD_HOST_MAX_POSITION_PCT": "1.0",
            "ROBINHOOD_HOST_MAX_ORDER_NOTIONAL": "100",
            "ROBINHOOD_HOST_MAX_DAILY_LOSS_PCT": "0.05",
            "ROBINHOOD_HOST_MAX_WEEKLY_LOSS_PCT": "0.10",
            "ROBINHOOD_HOST_MAX_DRAWDOWN_PCT": "0.15",
            "ROBINHOOD_HOST_MAX_ORDERS_PER_DAY": "3",
            "ROBINHOOD_HOST_NEW_ENTRIES_ENABLED": "true",
            "ROBINHOOD_HOST_ENABLED": "true",
        },
        store=store,
    )
    assert result.pending_created >= 1
    assert result.submission_attempts == 0


def test_07_handoff_prevalidation_vs_host_exposure():
    intent = _buy_intent()
    ok, _, _ = check_handoff_prevalidation(intent, action="BUY")
    assert ok
    blocked, reason, _ = check_host_new_exposure(intent, _state(known=False), _limits())
    assert not blocked
    assert "unknown" in reason


# --- 8–11 schema allowlist -------------------------------------------------


def _bound_agentic():
    from robinhood_account_isolation import BoundAgenticAccount

    return BoundAgenticAccount(
        account_number="TESTAGT6650",
        nickname="Agentic",
        last4="6650",
        brokerage_account_type="individual",
        account_type="cash",
        source="test",
    )


def test_08_place_args_omit_client_order_id_and_use_limit():
    intent = _buy_intent(2)
    quote = QuoteView("TQQQ", 50.0, 50.10, NOW)
    args = place_args_for_intent(intent, quote=quote, slippage_bps=25, bound=_bound_agentic())
    assert "client_order_id" not in args
    assert args["order_type"] == "limit"
    assert args["account_number"] == "TESTAGT6650"
    assert args["symbol"] == "TQQQ"
    assert args["side"] == "buy"
    assert float(args["limit_price"]) >= 50.10


def test_09_docs_confirm_no_client_order_id_param():
    assert DOCS_CONFIRMED_CLIENT_ORDER_ID_PARAM is False
    caps = docs_confirmed_capabilities()
    assert caps.limit_orders is True
    assert caps.client_order_id_param is False
    assert_capabilities_ready(caps)


def test_10_empty_tools_list_fail_closed():
    caps = capabilities_from_tools_list([])
    with pytest.raises(UnsafeBrokerConfiguration, match="missing required"):
        assert_capabilities_ready(caps)


def test_11_filter_rejects_invented_client_order_id():
    with pytest.raises(UnsafeBrokerConfiguration, match="forbidden"):
        filter_place_args({"symbol": "TQQQ", "client_order_id": "x"})


# --- 12–16 idempotency -----------------------------------------------------


def test_12_unknown_place_does_not_resubmit(tmp_path: Path):
    store = InMemoryIntentStore()
    intent = _buy_intent()
    assert store.create_pending(wrap_intent(intent, now=NOW)).ok
    transport = FakeHostTransport(place_status="AMBIGUOUS")
    res = InMemoryRhEntryReservationStore()
    result = run_host_executor_once(
        transport=transport,
        store=store,
        env=_armed_env(ROBINHOOD_HOST_EXEC_LOG=str(tmp_path / "exec.jsonl")),
        now=NOW,
        data_bar_start=NOW - timedelta(minutes=5),
        client_order_id=intent.client_order_id,
        reservation_store=res,
    )
    assert result.legs[0].execution_status == "UNKNOWN"
    assert len([c for c in transport.calls if c[0] == "place_equity_order"]) == 1
    again = run_host_executor_once(
        transport=transport,
        store=store,
        env=_armed_env(ROBINHOOD_HOST_EXEC_LOG=str(tmp_path / "exec2.jsonl")),
        now=NOW,
        data_bar_start=NOW - timedelta(minutes=5),
        client_order_id=intent.client_order_id,
        reservation_store=res,
    )
    assert again.legs == ()
    assert len([c for c in transport.calls if c[0] == "place_equity_order"]) == 1


def test_13_timeout_place_marks_unknown_no_resubmit(tmp_path: Path):
    store = InMemoryIntentStore()
    intent = _buy_intent()
    assert store.create_pending(wrap_intent(intent, now=NOW)).ok
    transport = FakeHostTransport(place_error=HostTransportError("timeout talking to MCP"))
    res = InMemoryRhEntryReservationStore()
    result = run_host_executor_once(
        transport=transport,
        store=store,
        env=_armed_env(ROBINHOOD_HOST_EXEC_LOG=str(tmp_path / "t.jsonl")),
        now=NOW,
        data_bar_start=NOW - timedelta(minutes=5),
        client_order_id=intent.client_order_id,
        reservation_store=res,
    )
    assert result.legs[0].execution_status == "UNKNOWN"
    assert store.get(intent.client_order_id)["status"] == "UNKNOWN"
    again = run_host_executor_once(
        transport=FakeHostTransport(),
        store=store,
        env=_armed_env(ROBINHOOD_HOST_EXEC_LOG=str(tmp_path / "t2.jsonl")),
        now=NOW,
        data_bar_start=NOW - timedelta(minutes=5),
        client_order_id=intent.client_order_id,
        reservation_store=res,
    )
    assert again.legs == ()


def test_14_malformed_place_marks_unknown(tmp_path: Path):
    store = InMemoryIntentStore()
    intent = _buy_intent()
    assert store.create_pending(wrap_intent(intent, now=NOW)).ok
    transport = FakeHostTransport(place_status="MALFORMED")
    result = run_host_executor_once(
        transport=transport,
        store=store,
        env=_armed_env(ROBINHOOD_HOST_EXEC_LOG=str(tmp_path / "m.jsonl")),
        now=NOW,
        data_bar_start=NOW - timedelta(minutes=5),
        client_order_id=intent.client_order_id,
        reservation_store=InMemoryRhEntryReservationStore(),
    )
    assert result.legs[0].execution_status == "UNKNOWN"
    assert len([c for c in transport.calls if c[0] == "place_equity_order"]) == 1


def test_15_second_executor_cannot_claim():
    store = InMemoryIntentStore()
    intent = _buy_intent()
    assert store.create_pending(wrap_intent(intent, now=NOW)).ok
    first = store.claim_by_client_order_id(intent.client_order_id, claimed_by="host-a", now=NOW)
    assert first.ok
    second = store.claim_by_client_order_id(intent.client_order_id, claimed_by="host-b", now=NOW)
    assert not second.ok


def test_16_host_places_once_without_client_order_id_arg(tmp_path: Path):
    store = InMemoryIntentStore()
    intent = _buy_intent()
    assert store.create_pending(wrap_intent(intent, now=NOW)).ok
    transport = FakeHostTransport(place_status="FILLED")
    result = run_host_executor_once(
        transport=transport,
        store=store,
        env=_armed_env(ROBINHOOD_HOST_EXEC_LOG=str(tmp_path / "exec.jsonl")),
        now=NOW,
        data_bar_start=NOW - timedelta(minutes=5),
        client_order_id=intent.client_order_id,
        reservation_store=InMemoryRhEntryReservationStore(),
    )
    assert result.place_attempts == 1
    assert result.legs[0].execution_status == "FILLED"
    place_calls = [c for c in transport.calls if c[0] == "place_equity_order"]
    assert len(place_calls) == 1
    assert "client_order_id" not in place_calls[0][1]
    assert place_calls[0][1]["order_type"] == "limit"


# --- 17–18 price protection ------------------------------------------------


def test_17_limit_capability_missing_fail_closed():
    caps = HostMcpCapabilities(
        place_equity_order=True,
        review_equity_order=True,
        cancel_equity_order=True,
        limit_orders=False,
        client_order_id_param=False,
        source="test",
    )
    with pytest.raises(UnsafeBrokerConfiguration, match="limit"):
        place_args_for_intent(
            _buy_intent(),
            quote=QuoteView("TQQQ", 50.0, 50.10, NOW),
            capabilities=caps,
            bound=_bound_agentic(),
        )


def test_18_slippage_bps_applied_to_limit():
    args = place_args_for_intent(
        _buy_intent(),
        quote=QuoteView("TQQQ", 50.0, 50.0, NOW),
        slippage_bps=100,  # 1%
        bound=_bound_agentic(),
    )
    assert float(args["limit_price"]) == pytest.approx(50.50, abs=0.01)


# --- 19–23 reservation lifecycle -------------------------------------------


def test_19_handoff_does_not_consume_reservation_slot():
    store = InMemoryIntentStore()
    reservation = InMemoryRhEntryReservationStore()
    run_robinhood_host_handoff(
        _alert(),
        None,
        _state(known=False),
        _limits(),
        store=store,
        reservation_store=reservation,
        now=NOW,
    )
    assert reservation.rows == []
    # Slot still free for host.
    assert reserve_rh_daily_entry(reservation, now=NOW).reserved


def test_20_host_buy_reserves_then_second_buy_blocked(tmp_path: Path):
    store = InMemoryIntentStore()
    res = InMemoryRhEntryReservationStore()
    a = _buy_intent()
    b = _draft_intent(
        _alert(timestamp="2026-10-07T15:00:00Z"),
        symbol="TQQQ",
        action="BUY",
        purpose="entry",
        price=50.10,
        quantity=1,
    )
    assert a.client_order_id != b.client_order_id
    assert store.create_pending(wrap_intent(a, now=NOW)).ok
    assert store.create_pending(wrap_intent(b, now=NOW)).ok
    r1 = run_host_executor_once(
        transport=FakeHostTransport(),
        store=store,
        env=_armed_env(ROBINHOOD_HOST_EXEC_LOG=str(tmp_path / "a.jsonl")),
        now=NOW,
        data_bar_start=NOW - timedelta(minutes=5),
        client_order_id=a.client_order_id,
        reservation_store=res,
    )
    assert r1.legs[0].execution_status == "FILLED"
    r2 = run_host_executor_once(
        transport=FakeHostTransport(),
        store=store,
        env=_armed_env(ROBINHOOD_HOST_EXEC_LOG=str(tmp_path / "b.jsonl")),
        now=NOW,
        data_bar_start=NOW - timedelta(minutes=5),
        client_order_id=b.client_order_id,
        reservation_store=res,
    )
    assert r2.legs[0].execution_status == "BLOCKED"
    assert "reserv" in r2.legs[0].risk_reason.lower()


def test_21_sell_never_consumes_reservation(tmp_path: Path):
    store = InMemoryIntentStore()
    res = InMemoryRhEntryReservationStore()
    sell = _sell_intent()
    assert store.create_pending(wrap_intent(sell, now=NOW)).ok
    transport = FakeHostTransport(positions=[{"symbol": "TQQQ", "quantity": 2}])
    result = run_host_executor_once(
        transport=transport,
        store=store,
        env=_armed_env(ROBINHOOD_HOST_EXEC_LOG=str(tmp_path / "s.jsonl")),
        now=NOW,
        data_bar_start=NOW - timedelta(minutes=5),
        client_order_id=sell.client_order_id,
        reservation_store=res,
    )
    assert result.legs[0].execution_status == "FILLED"
    reservation_still_free = reserve_rh_daily_entry(res, now=NOW)
    assert reservation_still_free.reserved


def test_22_failed_intent_insert_does_not_consume_slot():
    store = InMemoryIntentStore()
    store.fail_next = "simulated insert failure"
    reservation = InMemoryRhEntryReservationStore()
    result = run_robinhood_host_handoff(
        _alert(),
        None,
        _state(known=False),
        _limits(),
        store=store,
        reservation_store=reservation,
        now=NOW,
    )
    assert result.pending_created == 0
    assert reservation.rows == []
    assert reserve_rh_daily_entry(reservation, now=NOW).reserved


def test_23_concurrent_hosts_one_entry_per_day():
    store = InMemoryRhEntryReservationStore()
    first = reserve_rh_daily_entry(store, now=NOW, meta={"client_order_id": "a"})
    second = reserve_rh_daily_entry(store, now=NOW, meta={"client_order_id": "b"})
    assert first.reserved
    assert not second.reserved


# --- 24–26 FLIP ------------------------------------------------------------


def test_24_handoff_flip_blocks_second_leg_without_fill():
    store = InMemoryIntentStore()
    result = run_robinhood_host_handoff(
        _alert(alert_type="FLIP", symbol="SQQQ"),
        PositionState(active_symbol="TQQQ"),
        _state(known=False, positions=()),
        _limits(),
        store=store,
        now=NOW,
    )
    assert any(leg.intent.action == "SELL" and leg.persisted for leg in result.legs)
    buys = [leg for leg in result.legs if leg.intent.action == "BUY"]
    assert buys and buys[0].risk_status == "BLOCKED"


def test_25_flip_gate_partial_and_unknown_block():
    ok, _ = execute_flip_second_leg_gate(
        exit_status="PARTIALLY_FILLED",
        exit_qty_remaining=1.0,
        broker_state_known=True,
    )
    assert not ok
    ok2, _ = execute_flip_second_leg_gate(
        exit_status="UNKNOWN",
        exit_qty_remaining=0.0,
        broker_state_known=True,
    )
    assert not ok2
    ok3, _ = execute_flip_second_leg_gate(
        exit_status="FILLED",
        exit_qty_remaining=0.0,
        broker_state_known=True,
    )
    assert ok3


def test_26_flip_gate_requires_known_broker():
    ok, reason = execute_flip_second_leg_gate(
        exit_status="FILLED",
        exit_qty_remaining=0.0,
        broker_state_known=False,
    )
    assert not ok
    assert "uncertainty" in reason or "unknown" in reason.lower() or "broker" in reason.lower()


# --- 27–28 kill switches ---------------------------------------------------


def test_27_new_entries_false_blocks_buy_allows_sell_arming(tmp_path: Path):
    # Arming succeeds without NEW_ENTRIES; BUY risk still blocks.
    reason = host_arming_block_reason(
        _armed_env(ROBINHOOD_HOST_NEW_ENTRIES_ENABLED="false"),
        submission_implemented=True,
        host_executor=True,
        require_new_entries=False,
    )
    assert reason is None

    store = InMemoryIntentStore()
    buy = _buy_intent()
    assert store.create_pending(wrap_intent(buy, now=NOW)).ok
    buy_run = run_host_executor_once(
        transport=FakeHostTransport(),
        store=store,
        env=_armed_env(
            ROBINHOOD_HOST_NEW_ENTRIES_ENABLED="false",
            ROBINHOOD_HOST_EXEC_LOG=str(tmp_path / "kb.jsonl"),
        ),
        now=NOW,
        data_bar_start=NOW - timedelta(minutes=5),
        client_order_id=buy.client_order_id,
        reservation_store=InMemoryRhEntryReservationStore(),
    )
    assert buy_run.place_attempts == 0
    assert "NEW_ENTRIES" in buy_run.legs[0].risk_reason

    sell_store = InMemoryIntentStore()
    sell = _sell_intent()
    assert sell_store.create_pending(wrap_intent(sell, now=NOW)).ok
    sell_run = run_host_executor_once(
        transport=FakeHostTransport(positions=[{"symbol": "TQQQ", "quantity": 2}]),
        store=sell_store,
        env=_armed_env(
            ROBINHOOD_HOST_NEW_ENTRIES_ENABLED="false",
            ROBINHOOD_HOST_EXEC_LOG=str(tmp_path / "ks.jsonl"),
        ),
        now=NOW,
        data_bar_start=NOW - timedelta(minutes=5),
        client_order_id=sell.client_order_id,
        reservation_store=InMemoryRhEntryReservationStore(),
    )
    assert sell_run.place_attempts == 1
    assert sell_run.legs[0].execution_status == "FILLED"


def test_28_kill_switches_default_and_render_block():
    assert host_arming_block_reason({}, submission_implemented=True, host_executor=True) is not None
    assert host_arming_block_reason(
        _armed_env(RENDER="true"),
        submission_implemented=True,
        host_executor=True,
    ) == "Render runtime cannot submit Robinhood orders under Case C"
    assert "Render" in (
        host_arming_block_reason(
            _armed_env(RENDER_SERVICE_TYPE="cron"),
            submission_implemented=ROBINHOOD_HOST_SUBMISSION_IMPLEMENTED,
            host_executor=True,
        )
        or ""
    )


# --- 29–30 equity baselines + capital --------------------------------------


def test_29_equity_baselines_never_fabricated_from_current():
    account = AccountView(
        available=True,
        status="ACTIVE",
        buying_power=100.0,
        equity=100.0,
        day_start_equity=None,
        week_start_equity=None,
        peak_equity=None,
        cash=100.0,
    )
    store = InMemoryEquityBaselineStore()
    merged, _ = merge_account_baselines(account, store, now=NOW)
    # Must NOT invent day/week from current equity.
    assert merged.day_start_equity is None
    assert merged.week_start_equity is None
    # Peak may track observed equity (high-water), which is not fabrication of day/week.
    assert merged.peak_equity == 100.0

    # Missing day_start → risk fail-closed.
    state = _state()
    state = BrokerState(
        known=True,
        account=AccountView(
            available=True,
            status="ACTIVE",
            buying_power=100.0,
            equity=100.0,
            day_start_equity=None,
            week_start_equity=100.0,
            peak_equity=100.0,
            cash=100.0,
        ),
        quotes=state.quotes,
        now=NOW,
        data_bar_start=NOW - timedelta(minutes=5),
        order_history_complete=True,
    )
    ok, reason, _ = check_host_new_exposure(_buy_intent(), state, _limits())
    assert not ok
    assert "daily" in reason.lower() or "day-start" in reason.lower()


def test_30_capital_ceiling_cash_sizing_and_disarmed():
    limits = _limits(ROBINHOOD_HOST_CAPITAL_CEILING="100")
    ok, reason, _ = check_host_new_exposure(_buy_intent(), _state(equity=150.0, cash=150.0, bp=150.0), limits)
    assert not ok and "capital ceiling" in reason
    qty, notional = size_host_buy(50.10, _state(equity=100.0, cash=50.0, bp=500.0), _limits())
    assert qty == 0 or notional <= 50.0 + 0.01

    store = InMemoryIntentStore()
    intent = _buy_intent()
    assert store.create_pending(wrap_intent(intent, now=NOW)).ok
    transport = FakeHostTransport()
    result = run_host_executor_once(
        transport=transport,
        store=store,
        env=_armed_env(ROBINHOOD_HOST_LIVE_SUBMISSION="false"),
        now=NOW,
        data_bar_start=NOW - timedelta(minutes=5),
        client_order_id=intent.client_order_id,
        reservation_store=InMemoryRhEntryReservationStore(),
    )
    assert result.place_attempts == 0
    assert not any(name == "place_equity_order" for name, _ in transport.calls)

    # Extra boundary assertions bundled into case 30.
    assert HostMediatedClient(FakeHostTransport(), allow_writes=False)
    with pytest.raises(Exception):
        HostMediatedClient(FakeHostTransport(), allow_writes=False).call(
            "place_equity_order", {"symbol": "TQQQ"}
        )
    broker = RobinhoodAgenticBroker(_state())
    with pytest.raises(Exception):
        broker.submit_order(_buy_intent())
    assert broker.submission_attempts == 1
    ok_q, _, _ = check_host_new_exposure(
        _draft_intent(_alert(symbol="QQQ"), symbol="QQQ", action="BUY", purpose="entry", price=400.0, quantity=1),
        _state(),
        _limits(),
    )
    assert not ok_q
    assert store  # expired claim still covered historically
    expired = InMemoryIntentStore()
    e_intent = _buy_intent()
    assert expired.create_pending(
        wrap_intent(e_intent, now=NOW - timedelta(hours=1), ttl_seconds=60)
    ).ok
    claim = expired.claim_next_pending(claimed_by="host", now=NOW)
    assert not claim.ok and "expired" in claim.reason
