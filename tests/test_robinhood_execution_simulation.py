"""Phase 5R.3 — Agentic execution safety + paper simulation (mock only).

Zero real Robinhood orders. Zero production Supabase mutations.
Simulated fills must carry fill_source=SIMULATED_MOCK.
Strategy 1.0.0 frozen; host risk controls validated, not retuned.
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from brokers.robinhood_agentic import LIVE_SUBMISSION_IMPLEMENTED
from brokers.types import (
    AccountView,
    BrokerState,
    PositionView,
    QuoteView,
    TradeIntent,
    UnsafeBrokerConfiguration,
)
from robinhood_account_isolation import (
    ENV_BOUND_ACCOUNT,
    PROTECTED_LAST4,
    AccountIsolationError,
    assert_account_allowed,
    resolve_agentic_account,
)
from robinhood_flip import advance_flip
from robinhood_host_entry import InMemoryRhEntryReservationStore, reserve_rh_daily_entry
from robinhood_host_equity import InMemoryEquityBaselineStore
from robinhood_host_executor import (
    HostMediatedClient,
    HostTransportError,
    execute_flip_second_leg_gate,
    run_host_executor_once,
)
from robinhood_host_handoff import HANDOFF_SUBMISSION_IMPLEMENTED
from robinhood_host_risk import (
    check_host_new_exposure,
    host_arming_block_reason,
    host_limits_from_env,
    size_host_buy,
)
from robinhood_intent import wrap_intent
from robinhood_intent_store import InMemoryIntentStore
from robinhood_mock_execution import (
    DEFAULT_AGENTIC_ACCOUNT,
    DEFAULT_PROTECTED_MARGIN,
    DEFAULT_PROTECTED_ROTH,
    FILL_SOURCE_SIMULATED,
    SIM_ORDER_ID_PREFIX,
    MockRobinhoodExecutionEngine,
    SimOutcome,
)
from robinhood_risk import action_block_reason, execution_symbol_block_reason
from robinhood_shadow import _draft_intent
from strategy_params import STRATEGY_VERSION
from strategy_types import AlertDecision, PositionState

NOW = datetime(2026, 10, 7, 15, 0, tzinfo=timezone.utc)
MIGRATIONS = (
    Path(__file__).resolve().parents[1]
    / "dashboard"
    / "supabase"
    / "migrations"
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
        timestamp="2026-10-07T14:30:00Z",
        notes="sim",
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
        "ROBINHOOD_HOST_CAPITAL_CEILING": "10000",
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


def _buy_intent(qty: int = 1, symbol: str = "TQQQ", *, cid: str | None = None) -> TradeIntent:
    intent = _draft_intent(
        _alert(symbol=symbol),
        symbol=symbol,
        action="BUY",
        purpose="entry",
        price=50.10,
        quantity=qty,
    )
    data = intent.as_audit()
    data.update(
        {
            "quantity": qty,
            "estimated_price": 50.10,
            "estimated_notional": round(qty * 50.10, 2),
            "execution_symbol": symbol,
        }
    )
    if cid:
        data["client_order_id"] = cid
    return TradeIntent(**data)  # type: ignore[arg-type]


def _sell_intent(qty: int = 2, symbol: str = "TQQQ", *, cid: str | None = None) -> TradeIntent:
    intent = _draft_intent(
        _alert(alert_type="SELL", symbol=symbol),
        symbol=symbol,
        action="SELL",
        purpose="exit",
        price=50.0,
        quantity=qty,
    )
    data = intent.as_audit()
    data.update({"quantity": qty, "execution_symbol": symbol})
    if cid:
        data["client_order_id"] = cid
    return TradeIntent(**data)  # type: ignore[arg-type]


def _seed_pending(store: InMemoryIntentStore, intent: TradeIntent) -> dict[str, Any]:
    durable = wrap_intent(intent, now=NOW, ttl_seconds=900, status="PENDING")
    created = store.create_pending(
        durable,
        meta={
            "handoff": True,
            "simulation": True,
            "fill_source": FILL_SOURCE_SIMULATED,
            "account_number": DEFAULT_AGENTIC_ACCOUNT,
        },
    )
    assert created.ok, created.reason
    return created.row  # type: ignore[return-value]


# --- 01 foundations ------------------------------------------------------------


def test_01_strategy_frozen_and_lives_disarmed() -> None:
    assert STRATEGY_VERSION == "1.0.0"
    assert LIVE_SUBMISSION_IMPLEMENTED is False
    assert HANDOFF_SUBMISSION_IMPLEMENTED is False
    from robinhood_host_executor import ROBINHOOD_HOST_SUBMISSION_IMPLEMENTED as host_flag

    assert host_flag is True  # host path exists but kill switches default false
    assert host_arming_block_reason(
        {},
        submission_implemented=True,
        host_executor=True,
    )


def test_02_mock_engine_never_real_money() -> None:
    eng = MockRobinhoodExecutionEngine()
    assert eng.REAL_MONEY is False
    assert eng.REAL_ORDERS is False
    assert eng.money_moved() == 0.0
    assert eng.real_orders_placed() == 0
    assert eng.real_rh_place_attempts == 0


# --- 03 account isolation across surfaces -------------------------------------


def test_03_isolation_portfolio_positions_orders_intents() -> None:
    eng = MockRobinhoodExecutionEngine()
    bound = resolve_agentic_account(eng.accounts_payload())
    assert bound.account_number == DEFAULT_AGENTIC_ACCOUNT
    assert bound.last4 == "6650"

    client = HostMediatedClient(eng, allow_writes=False, env=_armed_env())
    snap = client.read_snapshot(("TQQQ", "SQQQ"))
    assert client.bound is not None
    assert client.bound.account_number == DEFAULT_AGENTIC_ACCOUNT
    for tool in ("get_portfolio", "get_equity_positions", "get_equity_orders"):
        scoped = [a for n, a in eng.calls if n == tool]
        assert scoped and scoped[0]["account_number"] == DEFAULT_AGENTIC_ACCOUNT

    with pytest.raises((AccountIsolationError, Exception), match="protected|allowlisted"):
        client.call(
            "get_equity_positions",
            {"account_number": DEFAULT_PROTECTED_MARGIN},
        )
    with pytest.raises((AccountIsolationError, Exception), match="protected|allowlisted"):
        eng(
            "get_portfolio",
            {"account_number": DEFAULT_PROTECTED_ROTH},
        )
    del snap


def test_04_isolation_risk_routing_recon_recovery() -> None:
    eng = MockRobinhoodExecutionEngine()
    bound = eng.bound_agentic()
    for bad in (DEFAULT_PROTECTED_MARGIN, DEFAULT_PROTECTED_ROTH):
        assert bad[-4:] in PROTECTED_LAST4
        with pytest.raises(AccountIsolationError):
            assert_account_allowed(bad, bound)

    # Recovery: open order blocks new exposure.
    eng.set_position("TQQQ", 2)
    eng.open_order_ids.append("sim-open-1")
    reason = eng.recovery_gate(
        proposed_client_order_id="cid-new",
        known_client_order_ids=frozenset(),
    )
    assert reason is not None and "open order" in reason

    # Duplicate client_order_id blocked (after open orders clear).
    eng.open_order_ids.clear()
    reason2 = eng.recovery_gate(
        proposed_client_order_id="cid-dup",
        known_client_order_ids=frozenset({"cid-dup"}),
        broker_state_known=True,
    )
    assert reason2 is not None and "duplicate" in reason2.lower()


# --- 05–12 mock engine lifecycle ----------------------------------------------


def test_05_buy_sell_hold_cash_and_simulated_label() -> None:
    eng = MockRobinhoodExecutionEngine(cash=10_000.0)
    # HOLD / CASH: no place — book unchanged.
    cash_before = eng.book.cash
    assert eng.book.positions == {}

    buy = eng(
        "place_equity_order",
        {
            "account_number": DEFAULT_AGENTIC_ACCOUNT,
            "symbol": "TQQQ",
            "side": "buy",
            "order_type": "limit",
            "quantity": 2,
            "limit_price": 50.10,
        },
    )
    assert buy["status"] == "FILLED"
    assert buy["fill_source"] == FILL_SOURCE_SIMULATED
    assert buy["real_rh_fill"] is False
    assert buy["id"].startswith(SIM_ORDER_ID_PREFIX)
    assert eng.book.positions.get("TQQQ") == pytest.approx(2.0)
    assert eng.book.cash < cash_before

    sell = eng(
        "place_equity_order",
        {
            "account_number": DEFAULT_AGENTIC_ACCOUNT,
            "symbol": "TQQQ",
            "side": "sell",
            "order_type": "limit",
            "quantity": 2,
            "limit_price": 50.0,
        },
    )
    assert sell["status"] == "FILLED"
    assert sell["fill_source"] == FILL_SOURCE_SIMULATED
    assert eng.book.positions.get("TQQQ") in (None, 0)
    assert eng.real_orders_placed() == 0
    assert eng.money_moved() == 0.0


def test_06_market_and_limit_paths() -> None:
    eng = MockRobinhoodExecutionEngine()
    limit = eng(
        "place_equity_order",
        {
            "account_number": DEFAULT_AGENTIC_ACCOUNT,
            "symbol": "SQQQ",
            "side": "buy",
            "order_type": "limit",
            "type": "limit",
            "quantity": 1,
            "limit_price": 20.05,
        },
    )
    assert limit["status"] == "FILLED"
    eng2 = MockRobinhoodExecutionEngine()
    mkt = eng2(
        "place_equity_order",
        {
            "account_number": DEFAULT_AGENTIC_ACCOUNT,
            "symbol": "TQQQ",
            "side": "buy",
            "order_type": "market",
            "quantity": 1,
        },
    )
    assert mkt["status"] == "FILLED"
    assert mkt["fill_source"] == FILL_SOURCE_SIMULATED


def test_07_partial_reject_cancel_expire_delay() -> None:
    eng = MockRobinhoodExecutionEngine()
    eng.enqueue_outcome(SimOutcome.PARTIAL)
    partial = eng(
        "place_equity_order",
        {
            "account_number": DEFAULT_AGENTIC_ACCOUNT,
            "symbol": "TQQQ",
            "side": "buy",
            "order_type": "limit",
            "quantity": 4,
            "limit_price": 50.10,
        },
    )
    assert partial["status"] == "PARTIALLY_FILLED"
    assert partial["filled_qty"] == pytest.approx(2.0)
    assert partial["fill_source"] == FILL_SOURCE_SIMULATED

    eng.enqueue_outcome(SimOutcome.REJECT)
    rejected = eng(
        "place_equity_order",
        {
            "account_number": DEFAULT_AGENTIC_ACCOUNT,
            "symbol": "TQQQ",
            "side": "buy",
            "order_type": "limit",
            "quantity": 1,
            "limit_price": 50.10,
        },
    )
    assert rejected["status"] == "REJECTED"

    eng.enqueue_outcome(SimOutcome.EXPIRE)
    expired = eng(
        "place_equity_order",
        {
            "account_number": DEFAULT_AGENTIC_ACCOUNT,
            "symbol": "SQQQ",
            "side": "buy",
            "order_type": "limit",
            "quantity": 1,
            "limit_price": 20.05,
        },
    )
    assert expired["status"] == "EXPIRED"

    eng.enqueue_outcome(SimOutcome.CANCEL)
    cancelled = eng(
        "place_equity_order",
        {
            "account_number": DEFAULT_AGENTIC_ACCOUNT,
            "symbol": "SQQQ",
            "side": "buy",
            "order_type": "limit",
            "quantity": 1,
            "limit_price": 20.05,
        },
    )
    assert cancelled["status"] == "CANCELLED"

    eng.delay_seconds = 0.01
    eng.enqueue_outcome(SimOutcome.DELAY_THEN_FILL)
    delayed = eng(
        "place_equity_order",
        {
            "account_number": DEFAULT_AGENTIC_ACCOUNT,
            "symbol": "TQQQ",
            "side": "buy",
            "order_type": "limit",
            "quantity": 1,
            "limit_price": 50.10,
        },
    )
    assert delayed["status"] == "FILLED"


def test_08_insufficient_bp_market_closed_stale_network() -> None:
    eng = MockRobinhoodExecutionEngine(cash=10.0)
    eng.enqueue_outcome(SimOutcome.INSUFFICIENT_BP)
    bad_bp = eng(
        "place_equity_order",
        {
            "account_number": DEFAULT_AGENTIC_ACCOUNT,
            "symbol": "TQQQ",
            "side": "buy",
            "order_type": "limit",
            "quantity": 10,
            "limit_price": 50.10,
        },
    )
    assert bad_bp["status"] == "REJECTED"
    assert "buying power" in str(bad_bp.get("detail") or "").lower()

    eng.set_market_open(False)
    closed = eng(
        "place_equity_order",
        {
            "account_number": DEFAULT_AGENTIC_ACCOUNT,
            "symbol": "TQQQ",
            "side": "buy",
            "order_type": "limit",
            "quantity": 1,
            "limit_price": 50.10,
        },
    )
    assert closed["status"] == "REJECTED"
    eng.set_market_open(True)

    eng.force_stale_quotes = True
    stale = eng(
        "place_equity_order",
        {
            "account_number": DEFAULT_AGENTIC_ACCOUNT,
            "symbol": "TQQQ",
            "side": "buy",
            "order_type": "limit",
            "quantity": 1,
            "limit_price": 50.10,
        },
    )
    assert stale["status"] == "REJECTED"
    eng.force_stale_quotes = False

    eng.enqueue_outcome(SimOutcome.NETWORK_ERROR)
    with pytest.raises(HostTransportError, match="network|timeout"):
        eng(
            "place_equity_order",
            {
                "account_number": DEFAULT_AGENTIC_ACCOUNT,
                "symbol": "TQQQ",
                "side": "buy",
                "order_type": "limit",
                "quantity": 1,
                "limit_price": 50.10,
            },
        )


def test_09_duplicate_signal_fingerprint_rejected() -> None:
    eng = MockRobinhoodExecutionEngine()
    args = {
        "account_number": DEFAULT_AGENTIC_ACCOUNT,
        "symbol": "TQQQ",
        "side": "buy",
        "order_type": "limit",
        "quantity": 1,
        "limit_price": 50.10,
    }
    first = eng("place_equity_order", args)
    assert first["status"] == "FILLED"
    # Reset cash/positions so BP isn't the reason — duplicate fingerprint is.
    eng.book.cash = 10_000.0
    eng.book.positions.clear()
    dup = eng("place_equity_order", args)
    assert dup["status"] == "REJECTED"
    assert eng.duplicate_place_rejections == 1


# --- 10 FLIP safety ------------------------------------------------------------


def test_10_flip_requires_confirmed_flat_close() -> None:
    # Partial exit → second leg blocked.
    ok, reason = execute_flip_second_leg_gate(
        exit_status="PARTIALLY_FILLED",
        exit_qty_remaining=1.0,
        broker_state_known=True,
    )
    assert ok is False

    for status in ("REJECTED", "CANCELLED", "EXPIRED", "UNKNOWN", "ACCEPTED", "SUBMITTED"):
        ok2, _ = execute_flip_second_leg_gate(
            exit_status=status,
            exit_qty_remaining=0.0,
            broker_state_known=True,
        )
        assert ok2 is False, status

    ok3, _ = execute_flip_second_leg_gate(
        exit_status="FILLED",
        exit_qty_remaining=0.0,
        broker_state_known=False,
    )
    assert ok3 is False

    ok4, reason4 = execute_flip_second_leg_gate(
        exit_status="FILLED",
        exit_qty_remaining=0.0,
        broker_state_known=True,
    )
    assert ok4 is True
    assert "validated" in reason4.lower() or "entry" in reason4.lower()

    # Engine helper: still holding shares after "FILLED" → blocked.
    eng = MockRobinhoodExecutionEngine()
    eng.set_position("TQQQ", 2)
    allowed, _ = eng.flip_entry_allowed(exit_status="FILLED", exit_symbol="TQQQ")
    assert allowed is False
    eng.set_position("TQQQ", 0)
    allowed2, _ = eng.flip_entry_allowed(exit_status="FILLED", exit_symbol="TQQQ")
    assert allowed2 is True

    # FLIP is not a single action.
    assert action_block_reason("FLIP") is not None


def test_11_flip_e2e_sell_then_buy_no_duplicate() -> None:
    """Simulate TQQQ→SQQQ flip: close TQQQ fully, then buy SQQQ once."""
    eng = MockRobinhoodExecutionEngine(cash=9_900.0)
    eng.set_position("TQQQ", 2)  # MTM ~100 → equity ~10k
    # Align cash so mark-to-market equity stays under the validated ceiling.
    eng.book.day_start_equity = eng.book.equity
    eng.book.week_start_equity = eng.book.equity
    eng.book.peak_equity = eng.book.equity
    store = InMemoryIntentStore()
    res = InMemoryRhEntryReservationStore()
    eq = InMemoryEquityBaselineStore()
    env = _armed_env(ROBINHOOD_HOST_CAPITAL_CEILING="20000")

    sell = _sell_intent(2, "TQQQ", cid="sim-flip-sell-1")
    _seed_pending(store, sell)
    run_sell = run_host_executor_once(
        transport=eng,
        store=store,
        env=env,
        now=NOW,
        data_bar_start=NOW - timedelta(minutes=5),
        client_order_id="sim-flip-sell-1",
        reservation_store=res,
        equity_store=eq,
    )
    assert run_sell.legs
    sell_leg = run_sell.legs[0]
    assert sell_leg.execution_status == "FILLED"
    assert eng.book.positions.get("TQQQ") in (None, 0)

    gate_ok, _ = execute_flip_second_leg_gate(
        exit_status="FILLED",
        exit_qty_remaining=float(eng.book.positions.get("TQQQ") or 0.0),
        broker_state_known=True,
    )
    assert gate_ok is True

    buy = _buy_intent(2, "SQQQ", cid="sim-flip-buy-1")
    # Adjust draft price for SQQQ
    buy = TradeIntent(**{**buy.as_audit(), "estimated_price": 20.05, "estimated_notional": 40.10})
    _seed_pending(store, buy)
    run_buy = run_host_executor_once(
        transport=eng,
        store=store,
        env=env,
        now=NOW,
        data_bar_start=NOW - timedelta(minutes=5),
        client_order_id="sim-flip-buy-1",
        reservation_store=res,
        equity_store=eq,
    )
    assert run_buy.legs
    assert run_buy.legs[0].execution_status == "FILLED"
    assert eng.book.positions.get("SQQQ") == pytest.approx(2.0)
    assert eng.book.positions.get("TQQQ") in (None, 0)

    # Second claim of same buy intent must not re-place.
    claim2 = store.claim_by_client_order_id("sim-flip-buy-1", claimed_by="worker-b", now=NOW)
    assert claim2.ok is False
    places = [c for c in eng.calls if c[0] == "place_equity_order"]
    assert len(places) == 2  # one sell + one buy only
    assert all(p[1].get("fill_source") is None for p in places)  # args don't carry; responses do
    assert eng.real_orders_placed() == 0


# --- 12 risk controls (validate, do not retune) -------------------------------


def test_12_host_risk_controls_present_and_block() -> None:
    """Missing/failed controls are blockers — do not invent new thresholds."""
    limits = host_limits_from_env(_armed_env())
    assert limits.max_slippage_bps == 25.0
    assert limits.capital_ceiling == 10_000.0
    assert limits.block_borrowed_funds is True
    assert limits.max_daily_loss_pct == 0.05
    assert limits.max_weekly_loss_pct == 0.10
    assert limits.max_drawdown_pct == 0.15

    # Symbol / action allowlist unchanged.
    assert execution_symbol_block_reason("TQQQ") is None
    assert execution_symbol_block_reason("QQQ") is not None
    assert execution_symbol_block_reason("BTC-USD") is not None
    assert action_block_reason("BUY") is None
    assert action_block_reason("FLIP") is not None

    # Capital ceiling blocks when equity above ceiling (existing semantics).
    tight = host_limits_from_env(_armed_env(ROBINHOOD_HOST_CAPITAL_CEILING="50"))
    state = BrokerState(
        known=True,
        account=AccountView(
            available=True,
            status="ACTIVE",
            buying_power=100.0,
            equity=100.0,
            day_start_equity=100.0,
            week_start_equity=100.0,
            peak_equity=100.0,
            cash=100.0,
        ),
        positions=(),
        open_orders=(),
        quotes=(QuoteView("TQQQ", 50.0, 50.10, NOW, last=50.05),),
        now=NOW,
        data_bar_start=NOW - timedelta(minutes=5),
        order_history_complete=True,
    )
    intent = _buy_intent(1)
    allowed, reason, _checks = check_host_new_exposure(intent, state, tight)
    assert allowed is False
    assert "ceiling" in reason.lower() or "capital" in reason.lower()

    # Stale quote blocks.
    stale_state = BrokerState(
        known=True,
        account=state.account,
        positions=(),
        open_orders=(),
        quotes=(
            QuoteView(
                "TQQQ",
                50.0,
                50.10,
                NOW - timedelta(hours=2),
                last=50.05,
            ),
        ),
        now=NOW,
        data_bar_start=NOW - timedelta(minutes=5),
        order_history_complete=True,
    )
    ok2, reason2, _ = check_host_new_exposure(intent, stale_state, limits)
    assert ok2 is False
    assert "quote" in reason2.lower() or "age" in reason2.lower() or "stale" in reason2.lower() or "fresh" in reason2.lower()

    # Render cannot arm.
    render_block = host_arming_block_reason(
        _armed_env(RENDER="true"),
        submission_implemented=True,
        host_executor=True,
    )
    assert render_block is not None and "Render" in render_block

    # Disarmed defaults.
    assert host_arming_block_reason(
        {"ROBINHOOD_HOST_EXECUTOR": "false"},
        submission_implemented=True,
        host_executor=True,
    )


# --- 13–15 idempotency / concurrency / restart --------------------------------


def test_13_unique_intent_and_atomic_reservation() -> None:
    store = InMemoryIntentStore()
    intent = _buy_intent(1, cid="sim-unique-1")
    _seed_pending(store, intent)
    dup = store.create_pending(
        wrap_intent(intent, now=NOW, ttl_seconds=900),
        meta={"simulation": True},
    )
    assert dup.ok is False

    res = InMemoryRhEntryReservationStore()
    r1 = reserve_rh_daily_entry(res, now=NOW, meta={"client_order_id": "a"})
    r2 = reserve_rh_daily_entry(res, now=NOW, meta={"client_order_id": "b"})
    assert r1.reserved is True
    assert r2.reserved is False


def test_14_concurrent_workers_single_claim() -> None:
    store = InMemoryIntentStore()
    _seed_pending(store, _buy_intent(1, cid="sim-race-1"))
    results: list[bool] = []
    lock = threading.Lock()

    def worker(name: str) -> None:
        claimed = store.claim_by_client_order_id("sim-race-1", claimed_by=name, now=NOW)
        with lock:
            results.append(claimed.ok)

    with ThreadPoolExecutor(max_workers=4) as pool:
        futs = [pool.submit(worker, f"w{i}") for i in range(4)]
        for f in futs:
            f.result()
    assert sum(1 for ok in results if ok) == 1
    assert sum(1 for ok in results if not ok) == 3


def test_15_restart_recovery_blocks_duplicate_place() -> None:
    eng = MockRobinhoodExecutionEngine()
    store = InMemoryIntentStore()
    res = InMemoryRhEntryReservationStore()
    eq = InMemoryEquityBaselineStore()
    env = _armed_env()
    _seed_pending(store, _buy_intent(1, cid="sim-restart-1"))
    run1 = run_host_executor_once(
        transport=eng,
        store=store,
        env=env,
        now=NOW,
        data_bar_start=NOW - timedelta(minutes=5),
        client_order_id="sim-restart-1",
        reservation_store=res,
        equity_store=eq,
    )
    assert run1.legs[0].execution_status == "FILLED"
    row = store.get("sim-restart-1")
    assert row is not None
    assert row["status"] == "FILLED"

    # Simulate restart: attempt re-claim → fail; place attempts unchanged.
    places_before = eng.place_attempts
    reclaim = store.claim_by_client_order_id("sim-restart-1", claimed_by="restart", now=NOW)
    assert reclaim.ok is False
    assert eng.place_attempts == places_before

    # Ambiguous network → RECONCILIATION_REQUIRED, no resubmit.
    eng2 = MockRobinhoodExecutionEngine()
    eng2.enqueue_outcome(SimOutcome.NETWORK_ERROR)
    store2 = InMemoryIntentStore()
    _seed_pending(store2, _buy_intent(1, cid="sim-timeout-1"))
    run2 = run_host_executor_once(
        transport=eng2,
        store=store2,
        env=env,
        now=NOW,
        data_bar_start=NOW - timedelta(minutes=5),
        client_order_id="sim-timeout-1",
        reservation_store=InMemoryRhEntryReservationStore(),
        equity_store=InMemoryEquityBaselineStore(),
    )
    assert run2.legs[0].execution_status == "RECONCILIATION_REQUIRED"
    row2 = store2.get("sim-timeout-1")
    assert row2 is not None and row2["status"] == "RECONCILIATION_REQUIRED"
    reclaim2 = store2.claim_by_client_order_id("sim-timeout-1", claimed_by="w2", now=NOW)
    assert reclaim2.ok is False


# --- 16 DB schema mocks (no prod mutation) ------------------------------------


def test_16_schema_constraints_mirrored_in_memory_and_sql() -> None:
    sql = (MIGRATIONS / "20261007160000_bot_robinhood_execution_intents.sql").read_text(
        encoding="utf-8"
    )
    assert "client_order_id text not null unique" in sql
    assert "'PENDING'" in sql and "'CLAIMED'" in sql and "'UNKNOWN'" in sql
    assert "execution_symbol in ('TQQQ', 'SQQQ')" in sql
    assert "signal_symbol = 'QQQ'" in sql
    assert "No OAuth tokens" in sql

    res_sql = (MIGRATIONS / "20261007160100_bot_robinhood_entry_reservations.sql").read_text(
        encoding="utf-8"
    )
    assert "bot_robinhood_entry_reservations" in res_sql

    # In-memory store enforces unique client_order_id (no Supabase write).
    store = InMemoryIntentStore()
    intent = _buy_intent(1, cid="sim-schema-1")
    assert _seed_pending(store, intent)
    again = store.create_pending(wrap_intent(intent, now=NOW), meta={})
    assert again.ok is False


# --- 17 security negatives + e2e ----------------------------------------------


def test_17_protected_accounts_cannot_be_routed_on_place() -> None:
    eng = MockRobinhoodExecutionEngine()
    for bad in (DEFAULT_PROTECTED_MARGIN, DEFAULT_PROTECTED_ROTH):
        with pytest.raises(AccountIsolationError):
            eng(
                "place_equity_order",
                {
                    "account_number": bad,
                    "symbol": "TQQQ",
                    "side": "buy",
                    "order_type": "limit",
                    "quantity": 1,
                    "limit_price": 50.10,
                },
            )
    assert eng.real_orders_placed() == 0
    assert eng.money_moved() == 0.0


def test_18_e2e_simulated_lifecycle_buy_hold_sell() -> None:
    eng = MockRobinhoodExecutionEngine(cash=10_000.0)
    store = InMemoryIntentStore()
    res = InMemoryRhEntryReservationStore()
    eq = InMemoryEquityBaselineStore()
    env = _armed_env()

    # BUY
    _seed_pending(store, _buy_intent(2, cid="sim-e2e-buy"))
    buy_run = run_host_executor_once(
        transport=eng,
        store=store,
        env=env,
        now=NOW,
        data_bar_start=NOW - timedelta(minutes=5),
        client_order_id="sim-e2e-buy",
        reservation_store=res,
        equity_store=eq,
    )
    assert buy_run.legs[0].execution_status == "FILLED"
    assert eng.book.positions.get("TQQQ") == pytest.approx(2.0)

    # HOLD / CASH — no new intent; positions unchanged.
    held = dict(eng.book.positions)
    assert held == {"TQQQ": pytest.approx(2.0)} or held.get("TQQQ") == 2.0

    # SELL to flat
    _seed_pending(store, _sell_intent(2, cid="sim-e2e-sell"))
    sell_run = run_host_executor_once(
        transport=eng,
        store=store,
        env=env,
        now=NOW + timedelta(minutes=1),
        data_bar_start=NOW - timedelta(minutes=4),
        client_order_id="sim-e2e-sell",
        reservation_store=res,
        equity_store=eq,
    )
    assert sell_run.legs[0].execution_status == "FILLED"
    assert eng.book.positions.get("TQQQ") in (None, 0)

    # Every place response labeled simulated.
    for name, _args in eng.calls:
        if name != "place_equity_order":
            continue
    for order in eng.orders.values():
        assert order.fill_source == FILL_SOURCE_SIMULATED
        assert not order.order_id.startswith("rh-") or order.order_id.startswith(SIM_ORDER_ID_PREFIX)

    assert eng.real_orders_placed() == 0
    assert eng.money_moved() == 0.0
    assert STRATEGY_VERSION == "1.0.0"


def test_19_sizing_helper_respects_existing_limits() -> None:
    limits = host_limits_from_env(_armed_env(ROBINHOOD_HOST_MAX_ORDER_NOTIONAL="100"))
    state = BrokerState(
        known=True,
        account=AccountView(
            available=True,
            status="ACTIVE",
            buying_power=10_000.0,
            equity=10_000.0,
            day_start_equity=10_000.0,
            week_start_equity=10_000.0,
            peak_equity=10_000.0,
            cash=10_000.0,
        ),
        positions=(),
        open_orders=(),
        quotes=(QuoteView("TQQQ", 50.0, 50.10, NOW, last=50.05),),
        now=NOW,
        data_bar_start=NOW - timedelta(minutes=5),
        order_history_complete=True,
    )
    qty, notional = size_host_buy(50.10, state, limits)
    assert qty >= 1
    assert notional <= 100.0 + 1e-6


def test_20_advance_flip_parity_with_engine() -> None:
    gate = advance_flip(
        exit_status="FILLED",
        exit_qty_remaining=0.0,
        broker_state_known=True,
    )
    assert gate.entry_validation_allowed is True
    eng = MockRobinhoodExecutionEngine()
    ok, _ = eng.flip_entry_allowed(exit_status="FILLED", exit_symbol="SQQQ")
    assert ok is True


def test_21_normalize_preserves_account_status_after_orders() -> None:
    """Regression: order status must not overwrite account status (Phase 5R.3)."""
    from brokers.robinhood_normalize import normalize_snapshot

    eng = MockRobinhoodExecutionEngine()
    eng(
        "place_equity_order",
        {
            "account_number": DEFAULT_AGENTIC_ACCOUNT,
            "symbol": "TQQQ",
            "side": "buy",
            "order_type": "limit",
            "quantity": 1,
            "limit_price": 50.10,
        },
    )
    args = {"account_number": DEFAULT_AGENTIC_ACCOUNT}
    raw = {
        "get_accounts": eng("get_accounts", {}),
        "get_portfolio": eng("get_portfolio", args),
        "get_equity_positions": eng("get_equity_positions", args),
        "get_equity_quotes": eng("get_equity_quotes", {"symbols": ["TQQQ", "SQQQ"]}),
        "get_equity_orders": eng("get_equity_orders", args),
    }
    state = normalize_snapshot(raw, now=NOW, data_bar_start=NOW - timedelta(minutes=5))
    assert state.known is True
    assert state.account is not None
    assert state.account.status.lower() == "active"
    assert state.order_history_complete is True


def test_22_normalize_prefers_agentic_over_first_protected_row() -> None:
    """Regression: get_accounts[0] is often protected margin — prefer Agentic."""
    from brokers.robinhood_normalize import normalize_snapshot

    raw = {
        "get_accounts": {
            "accounts": [
                {
                    "account_number": "TESTMRG9384",
                    "nickname": "Margin",
                    "agentic_allowed": False,
                    "type": "margin",
                    "brokerage_account_type": "individual",
                    "status": "restricted",
                },
                {
                    "account_number": "TESTIRA5767",
                    "nickname": "Roth",
                    "agentic_allowed": False,
                    "type": "cash",
                    "brokerage_account_type": "ira_roth",
                    "status": "active",
                },
                {
                    "account_number": DEFAULT_AGENTIC_ACCOUNT,
                    "nickname": "Agentic",
                    "agentic_allowed": True,
                    "type": "cash",
                    "brokerage_account_type": "individual",
                    "status": "active",
                },
            ]
        },
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
                    "ask": 50.1,
                    "last": 50.05,
                    "timestamp": NOW.isoformat(),
                }
            ]
        },
        "get_equity_orders": {"orders": []},
    }
    state = normalize_snapshot(raw, now=NOW, data_bar_start=NOW - timedelta(minutes=5))
    assert state.known is True
    assert state.account is not None
    # Must not inherit protected margin status "restricted".
    assert state.account.status.lower() == "active"
