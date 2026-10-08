"""Phase 5R.4.1 — independent verification of HIGH remediations H1–H5.

Mock / fixture schemas only. Zero real RH mutating tools. Zero money.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from brokers.types import TradeIntent
from robinhood_account_isolation import (
    ENV_BOUND_ACCOUNT,
    AccountIsolationError,
    require_bound_account,
)
from robinhood_flip import FLIP_META_EXIT_CID, FLIP_META_EXIT_SYMBOL, flip_entry_meta, new_flip_pair_id
from robinhood_host_entry import InMemoryRhEntryReservationStore
from robinhood_host_equity import InMemoryEquityBaselineStore
from robinhood_host_executor import (
    HostMediatedClient,
    HostTransportError,
    parse_review_decision,
    run_host_executor_once,
)
from robinhood_host_handoff import run_robinhood_host_handoff
from robinhood_host_risk import host_limits_from_env
from robinhood_intent import wrap_intent
from robinhood_intent_store import InMemoryIntentStore
from robinhood_mock_execution import (
    DEFAULT_AGENTIC_ACCOUNT,
    FILL_SOURCE_SIMULATED,
    MockRobinhoodExecutionEngine,
)
from robinhood_shadow import _draft_intent
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
        notes="test",
    )
    defaults.update(kwargs)
    return AlertDecision(**defaults)  # type: ignore[arg-type]


def _armed_env(**overrides: str) -> dict[str, str]:
    base = {
        "ROBINHOOD_HOST_EXECUTOR": "true",
        "ROBINHOOD_HOST_ENABLED": "true",
        "ROBINHOOD_HOST_NEW_ENTRIES_ENABLED": "true",
        "ROBINHOOD_HOST_LIVE_SUBMISSION": "true",
        "LIVE_TRADING_ENABLED": "true",
        "ROBINHOOD_HOST_MAX_POSITION_PCT": "1.0",
        "ROBINHOOD_HOST_MAX_ORDER_NOTIONAL": "500",
        "ROBINHOOD_HOST_CAPITAL_CEILING": "20000",
        "ROBINHOOD_HOST_MAX_DAILY_LOSS_PCT": "0.05",
        "ROBINHOOD_HOST_MAX_WEEKLY_LOSS_PCT": "0.10",
        "ROBINHOOD_HOST_MAX_DRAWDOWN_PCT": "0.15",
        "ROBINHOOD_HOST_MAX_ORDERS_PER_DAY": "10",
        "ROBINHOOD_HOST_MAX_QUOTE_AGE_SECONDS": "60",
        "ROBINHOOD_HOST_MAX_SPREAD_BPS": "50",
        "ROBINHOOD_HOST_MAX_DATA_AGE_MINUTES": "90",
        "ROBINHOOD_HOST_MAX_SLIPPAGE_BPS": "25",
        ENV_BOUND_ACCOUNT: DEFAULT_AGENTIC_ACCOUNT,
    }
    base.update(overrides)
    return base


def _intent(
    *,
    action: str,
    symbol: str,
    purpose: str,
    qty: int,
    cid: str,
    price: float,
) -> TradeIntent:
    draft = _draft_intent(
        _alert(alert_type=action, symbol=symbol),
        symbol=symbol,
        action=action,
        purpose=purpose,
        price=price,
        quantity=qty,
    )
    data = draft.as_audit()
    data.update(
        {
            "quantity": qty,
            "estimated_price": price,
            "estimated_notional": round(qty * price, 2),
            "execution_symbol": symbol,
            "client_order_id": cid,
            "purpose": purpose,
        }
    )
    return TradeIntent(**data)  # type: ignore[arg-type]


def _seed(store: InMemoryIntentStore, intent: TradeIntent, meta: dict[str, Any] | None = None) -> None:
    durable = wrap_intent(intent, now=NOW, ttl_seconds=900, status="PENDING")
    created = store.create_pending(
        durable,
        meta={
            "handoff": True,
            "simulation": True,
            "fill_source": FILL_SOURCE_SIMULATED,
            "account_number": DEFAULT_AGENTIC_ACCOUNT,
            **(meta or {}),
        },
    )
    assert created.ok, created.reason


# --- H1 --------------------------------------------------------------------


def test_h1_env_pin_alone_refuses_host_bind() -> None:
    with pytest.raises(AccountIsolationError, match="live get_accounts|not authority"):
        require_bound_account(env={ENV_BOUND_ACCOUNT: "EVILACC6650"})


def test_h1_host_client_refuses_scoped_call_without_live_resolve() -> None:
    eng = MockRobinhoodExecutionEngine()
    client = HostMediatedClient(eng, allow_writes=True, env=_armed_env())
    with pytest.raises(Exception, match="live-resolved|not bound|refusing"):
        client.call("get_portfolio", {})


def test_h1_live_resolve_still_binds_agentic() -> None:
    eng = MockRobinhoodExecutionEngine()
    client = HostMediatedClient(eng, allow_writes=True, env=_armed_env())
    snap = client.read_snapshot()
    assert client.bound is not None
    assert client.bound.account_number == DEFAULT_AGENTIC_ACCOUNT
    assert client.bound.source == "get_accounts"
    assert "get_portfolio" in snap


# --- H2 --------------------------------------------------------------------


def test_h2_flip_entry_blocked_until_exit_filled(tmp_path: Path) -> None:
    eng = MockRobinhoodExecutionEngine(cash=9_900.0)
    eng.set_position("TQQQ", 2)
    eng.book.day_start_equity = eng.book.equity
    eng.book.week_start_equity = eng.book.equity
    eng.book.peak_equity = eng.book.equity
    store = InMemoryIntentStore()
    pair = new_flip_pair_id()
    sell = _intent(action="SELL", symbol="TQQQ", purpose="flip_exit", qty=2, cid="h2-exit", price=50.0)
    buy = _intent(action="BUY", symbol="SQQQ", purpose="flip_entry", qty=2, cid="h2-entry", price=20.05)
    _seed(store, sell, {"flip_pair_id": pair, "flip_role": "exit", FLIP_META_EXIT_SYMBOL: "TQQQ"})
    _seed(
        store,
        buy,
        flip_entry_meta(pair_id=pair, exit_client_order_id="h2-exit", exit_symbol="TQQQ"),
    )
    env = _armed_env(ROBINHOOD_HOST_EXEC_LOG=str(tmp_path / "h2a.jsonl"))
    # Entry before exit fill → BLOCKED
    early = run_host_executor_once(
        transport=eng,
        store=store,
        env=env,
        now=NOW,
        data_bar_start=NOW - timedelta(minutes=5),
        client_order_id="h2-entry",
        reservation_store=InMemoryRhEntryReservationStore(),
        equity_store=InMemoryEquityBaselineStore(),
    )
    assert early.legs[0].execution_status == "BLOCKED"
    assert early.place_attempts == 0

    # Re-seed entry as PENDING after block for post-exit run.
    store.rows["h2-entry"]["status"] = "PENDING"
    store.rows["h2-entry"].pop("terminal_at", None)

    run_host_executor_once(
        transport=eng,
        store=store,
        env=env,
        now=NOW,
        data_bar_start=NOW - timedelta(minutes=5),
        client_order_id="h2-exit",
        reservation_store=InMemoryRhEntryReservationStore(),
        equity_store=InMemoryEquityBaselineStore(),
    )
    assert store.get("h2-exit")["status"] == "FILLED"
    assert eng.book.positions.get("TQQQ") in (None, 0)

    entry = run_host_executor_once(
        transport=eng,
        store=store,
        env=env,
        now=NOW,
        data_bar_start=NOW - timedelta(minutes=5),
        client_order_id="h2-entry",
        reservation_store=InMemoryRhEntryReservationStore(),
        equity_store=InMemoryEquityBaselineStore(),
    )
    assert entry.legs[0].execution_status == "FILLED"
    places = [c for c in eng.calls if c[0] == "place_equity_order"]
    assert len(places) == 2


def test_h2_flip_unknown_exit_requires_reconciliation(tmp_path: Path) -> None:
    eng = MockRobinhoodExecutionEngine()
    store = InMemoryIntentStore()
    pair = new_flip_pair_id()
    sell = _intent(action="SELL", symbol="TQQQ", purpose="flip_exit", qty=1, cid="h2u-exit", price=50.0)
    buy = _intent(action="BUY", symbol="SQQQ", purpose="flip_entry", qty=1, cid="h2u-entry", price=20.05)
    _seed(store, sell, {"flip_pair_id": pair, "flip_role": "exit"})
    store.rows["h2u-exit"]["status"] = "UNKNOWN"
    _seed(
        store,
        buy,
        flip_entry_meta(pair_id=pair, exit_client_order_id="h2u-exit", exit_symbol="TQQQ"),
    )
    result = run_host_executor_once(
        transport=eng,
        store=store,
        env=_armed_env(ROBINHOOD_HOST_EXEC_LOG=str(tmp_path / "h2u.jsonl")),
        now=NOW,
        data_bar_start=NOW - timedelta(minutes=5),
        client_order_id="h2u-entry",
        reservation_store=InMemoryRhEntryReservationStore(),
        equity_store=InMemoryEquityBaselineStore(),
    )
    assert result.legs[0].execution_status == "RECONCILIATION_REQUIRED"
    assert result.place_attempts == 0


def test_h2_handoff_flip_both_directions_persist() -> None:
    store = InMemoryIntentStore()
    limits = host_limits_from_env(_armed_env())
    from brokers.types import BrokerState

    state = BrokerState(known=False, detail="case c", now=NOW, order_history_complete=False)
    for symbol, active in (("SQQQ", "TQQQ"), ("TQQQ", "SQQQ")):
        store = InMemoryIntentStore()
        result = run_robinhood_host_handoff(
            _alert(alert_type="FLIP", symbol=symbol),
            PositionState(active_symbol=active),
            state,
            limits,
            store=store,
            now=NOW,
            env={ENV_BOUND_ACCOUNT: DEFAULT_AGENTIC_ACCOUNT},
        )
        assert result.pending_created >= 2
        purposes = {r["purpose"] for r in store.rows.values()}
        assert "flip_exit" in purposes and "flip_entry" in purposes


# --- H3 --------------------------------------------------------------------


def test_h3_http_502_after_place_is_reconciliation_not_rejected(tmp_path: Path) -> None:
    eng = MockRobinhoodExecutionEngine()

    class Flaky502:
        def __call__(self, name: str, arguments: dict[str, Any]) -> Any:
            if name == "place_equity_order":
                raise HostTransportError("HTTP 502 bad gateway from MCP")
            return eng(name, arguments)

    store = InMemoryIntentStore()
    buy = _intent(action="BUY", symbol="TQQQ", purpose="entry", qty=1, cid="h3-502", price=50.10)
    _seed(store, buy)
    result = run_host_executor_once(
        transport=Flaky502(),
        store=store,
        env=_armed_env(ROBINHOOD_HOST_EXEC_LOG=str(tmp_path / "h3.jsonl")),
        now=NOW,
        data_bar_start=NOW - timedelta(minutes=5),
        client_order_id="h3-502",
        reservation_store=InMemoryRhEntryReservationStore(),
        equity_store=InMemoryEquityBaselineStore(),
    )
    assert result.legs[0].execution_status == "RECONCILIATION_REQUIRED"
    assert store.get("h3-502")["status"] == "RECONCILIATION_REQUIRED"
    # No auto resubmit on re-claim.
    again = run_host_executor_once(
        transport=MockRobinhoodExecutionEngine(),
        store=store,
        env=_armed_env(ROBINHOOD_HOST_EXEC_LOG=str(tmp_path / "h3b.jsonl")),
        now=NOW,
        data_bar_start=NOW - timedelta(minutes=5),
        client_order_id="h3-502",
        reservation_store=InMemoryRhEntryReservationStore(),
        equity_store=InMemoryEquityBaselineStore(),
    )
    assert again.legs == ()


# --- H4 --------------------------------------------------------------------


@pytest.mark.parametrize(
    "decision,expected_status,places",
    [
        ("REJECTED", "REJECTED", 0),
        ("PENDING", "BLOCKED", 0),
        ("EXPIRED", "BLOCKED", 0),
        ("UNKNOWN", "UNKNOWN", 0),
        ("APPROVED", "FILLED", 1),
    ],
)
def test_h4_review_decision_honored(
    tmp_path: Path, decision: str, expected_status: str, places: int
) -> None:
    eng = MockRobinhoodExecutionEngine()
    eng.force_review_decision = decision
    store = InMemoryIntentStore()
    buy = _intent(
        action="BUY",
        symbol="TQQQ",
        purpose="entry",
        qty=1,
        cid=f"h4-{decision.lower()}",
        price=50.10,
    )
    _seed(store, buy)
    result = run_host_executor_once(
        transport=eng,
        store=store,
        env=_armed_env(ROBINHOOD_HOST_EXEC_LOG=str(tmp_path / f"h4-{decision}.jsonl")),
        now=NOW,
        data_bar_start=NOW - timedelta(minutes=5),
        client_order_id=buy.client_order_id,
        reservation_store=InMemoryRhEntryReservationStore(),
        equity_store=InMemoryEquityBaselineStore(),
    )
    assert result.legs[0].execution_status == expected_status
    assert len([c for c in eng.calls if c[0] == "place_equity_order"]) == places


def test_h4_parse_review_fixture_schemas() -> None:
    assert parse_review_decision({"ok": True}).decision == "APPROVED"
    assert parse_review_decision({"ok": False, "warnings": ["x"]}).decision == "REJECTED"
    assert parse_review_decision({"decision": "PENDING"}).decision == "PENDING"
    assert parse_review_decision({"status": "EXPIRED"}).decision == "EXPIRED"
    assert parse_review_decision(None).decision == "UNKNOWN"


# --- H5 --------------------------------------------------------------------


def test_h5_disarmed_does_not_burn_reservation(tmp_path: Path) -> None:
    from robinhood_host_entry import reserve_rh_daily_entry

    eng = MockRobinhoodExecutionEngine()
    store = InMemoryIntentStore()
    res = InMemoryRhEntryReservationStore()
    buy = _intent(action="BUY", symbol="TQQQ", purpose="entry", qty=1, cid="h5-disarm", price=50.10)
    _seed(store, buy)
    result = run_host_executor_once(
        transport=eng,
        store=store,
        env=_armed_env(
            ROBINHOOD_HOST_LIVE_SUBMISSION="false",
            ROBINHOOD_HOST_EXEC_LOG=str(tmp_path / "h5.jsonl"),
        ),
        now=NOW,
        data_bar_start=NOW - timedelta(minutes=5),
        client_order_id="h5-disarm",
        reservation_store=res,
        equity_store=InMemoryEquityBaselineStore(),
    )
    assert result.legs[0].execution_status == "BLOCKED"
    assert result.place_attempts == 0
    assert res._keys == set()
    assert res.rows == []
    # Slot still available after disarmed path.
    reserved = reserve_rh_daily_entry(res, now=NOW, meta={"client_order_id": "later"})
    assert reserved.reserved is True
