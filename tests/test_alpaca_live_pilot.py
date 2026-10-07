"""Alpaca live pilot: multi-key arming, atomic claims, fake-transport only.

Never opens a real socket to api.alpaca.markets for mutating calls.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from alpaca_live_claim import AtomicLiveClaimStore, InMemoryAtomicClaimStore
from alpaca_live_circuit import CircuitState, InMemoryCircuitStore
from alpaca_live_entry_reservation import (
    InMemoryEntryReservationStore,
    trading_day_america_new_york,
)
from alpaca_live_flip_refresh import require_post_sell_flat_for_flip
from alpaca_live_pilot import (
    evaluate_pilot_leg,
    run_alpaca_live_pilot,
    run_alpaca_live_pilot_after_strategy,
)
from alpaca_live_risk import check_new_exposure, live_limits_from_env, pilot_arming_block_reason
from brokers.alpaca_endpoints import LIVE_TRADING_BASE_URL
from brokers.alpaca_live_broker import ALPACA_LIVE_SUBMISSION_IMPLEMENTED
from brokers.alpaca_live_executor import (
    ALPACA_LIVE_PILOT_SUBMISSION_IMPLEMENTED,
    AlpacaLiveExecutor,
    LiveOrderTimeout,
    marketable_limit_price,
)
from brokers.mode import ALPACA_LIVE_PILOT, parse_execution_broker
from brokers.types import (
    EXECUTION_NOT_SUBMITTED,
    ORDER_FILLED,
    ORDER_UNKNOWN,
    AccountView,
    BrokerState,
    OpenOrderView,
    PositionView,
    QuoteView,
    TradeIntent,
    UnsafeBrokerConfiguration,
)
from strategy_params import STRATEGY_VERSION
from strategy_types import AlertDecision, PositionState

NOW = datetime(2026, 10, 6, 15, 0, tzinfo=timezone.utc)


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
        timestamp="2026-10-06T14:30:00Z",
        notes="Bull score exceeded threshold",
    )
    defaults.update(kwargs)
    return AlertDecision(**defaults)  # type: ignore[arg-type]


def _limits(**overrides: str):
    base = {
        "ALPACA_LIVE_MAX_POSITION_PCT": "0.10",
        "ALPACA_LIVE_MAX_ORDER_NOTIONAL": "200",
        "ALPACA_LIVE_CAPITAL_CEILING": "1000",
        "ALPACA_LIVE_MAX_DAILY_LOSS_PCT": "0.05",
        "ALPACA_LIVE_MAX_WEEKLY_LOSS_PCT": "0.10",
        "ALPACA_LIVE_MAX_DRAWDOWN_PCT": "0.15",
        "ALPACA_LIVE_MAX_ORDERS_PER_DAY": "3",
        "ALPACA_LIVE_NEW_ENTRIES_ENABLED": "true",
        "ALPACA_LIVE_ENABLED": "true",
        "LIVE_TRADING_ENABLED": "true",
        "ALPACA_LIVE_MAX_QUOTE_AGE_SECONDS": "60",
        "ALPACA_LIVE_MAX_SPREAD_BPS": "50",
        "ALPACA_LIVE_MAX_DATA_AGE_MINUTES": "90",
    }
    base.update(overrides)
    return live_limits_from_env(base)


def _state(
    *,
    known: bool = True,
    equity: float = 1_000.0,
    cash: float = 1_000.0,
    bp: float = 2_000.0,
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
        quote=None,
        data_bar_start=NOW - timedelta(minutes=5),
        now=NOW,
        order_history_complete=True,
        detail="test",
    )


def _armed_env(**extra: str) -> dict[str, str]:
    env = {
        "EXECUTION_BROKER": "alpaca_live_pilot",
        "ALPACA_LIVE_ENABLED": "true",
        "ALPACA_LIVE_NEW_ENTRIES_ENABLED": "true",
        "LIVE_TRADING_ENABLED": "true",
        "ALPACA_LIVE_API_KEY": "live-key-test",
        "ALPACA_LIVE_API_SECRET": "live-secret-test",
        "ALPACA_API_KEY": "paper-key-test",
        "ALPACA_API_SECRET": "paper-secret-test",
        "ALPACA_LIVE_TRADING_BASE_URL": LIVE_TRADING_BASE_URL,
        "SUPABASE_URL": "https://example.supabase.co",
        "SUPABASE_SERVICE_ROLE_KEY": "service-role-test",
        "ALPACA_LIVE_MAX_POSITION_PCT": "0.10",
        "ALPACA_LIVE_MAX_ORDER_NOTIONAL": "200",
        "ALPACA_LIVE_CAPITAL_CEILING": "1000",
        "ALPACA_LIVE_MAX_DAILY_LOSS_PCT": "0.05",
        "ALPACA_LIVE_MAX_WEEKLY_LOSS_PCT": "0.10",
        "ALPACA_LIVE_MAX_DRAWDOWN_PCT": "0.15",
        "ALPACA_LIVE_MAX_ORDERS_PER_DAY": "3",
    }
    env.update(extra)
    return env


class FakeTradingTransport:
    """In-memory Alpaca trading API for tests. No real network."""

    def __init__(self) -> None:
        self.posts: list[dict[str, Any]] = []
        self.gets: list[str] = []
        self.orders_by_client: dict[str, dict[str, Any]] = {}
        self.post_behavior: str = "accept"  # accept | timeout | reject
        self.accept_status: str = "accepted"
        self.filled_qty_on_accept: float | None = None

    def __call__(
        self,
        method: str,
        url: str,
        headers: dict[str, str],
        params: dict[str, Any] | None,
        json_body: dict[str, Any] | None,
    ) -> Any:
        verb = method.upper()
        if verb == "POST" and url.rstrip("/").endswith("/v2/orders"):
            if self.post_behavior == "timeout":
                raise LiveOrderTimeout("simulated timeout")
            if self.post_behavior == "reject":
                raise UnsafeBrokerConfiguration("simulated reject")  # mapped by caller? use Runtime
            assert json_body is not None
            self.posts.append(dict(json_body))
            client_id = str(json_body["client_order_id"])
            filled = (
                self.filled_qty_on_accept
                if self.filled_qty_on_accept is not None
                else float(json_body["qty"])
            )
            order = {
                "id": f"broker-{len(self.posts)}",
                "client_order_id": client_id,
                "status": self.accept_status,
                "qty": json_body["qty"],
                "filled_qty": str(filled) if self.accept_status == "filled" else "0",
                "symbol": json_body["symbol"],
                "side": json_body["side"],
            }
            self.orders_by_client[client_id] = order
            return order
        if verb == "GET" and "orders:client_order_id:" in url:
            self.gets.append(url)
            client_id = url.rsplit(":", 1)[-1]
            return self.orders_by_client.get(client_id)
        raise AssertionError(f"unexpected transport call {verb} {url}")


def test_mode_allows_pilot_rejects_bare_live():
    assert parse_execution_broker("alpaca_live_pilot") == ALPACA_LIVE_PILOT
    with pytest.raises(UnsafeBrokerConfiguration):
        parse_execution_broker("alpaca_live")


def test_multi_key_arming_requires_all_flags():
    assert (
        pilot_arming_block_reason(
            _armed_env(),
            submission_implemented=True,
            execution_broker="alpaca_live_pilot",
        )
        is None
    )
    for key in (
        "ALPACA_LIVE_ENABLED",
        "ALPACA_LIVE_NEW_ENTRIES_ENABLED",
        "LIVE_TRADING_ENABLED",
    ):
        env = _armed_env(**{key: "false"})
        reason = pilot_arming_block_reason(
            env, submission_implemented=True, execution_broker="alpaca_live_pilot"
        )
        assert reason is not None
        assert key in reason
    assert (
        pilot_arming_block_reason(
            _armed_env(),
            submission_implemented=False,
            execution_broker="alpaca_live_pilot",
        )
        is not None
    )


def test_shadow_submission_flag_still_false():
    assert ALPACA_LIVE_SUBMISSION_IMPLEMENTED is False
    assert ALPACA_LIVE_PILOT_SUBMISSION_IMPLEMENTED is True


def test_atomic_claim_unique_violation_blocks():
    store = InMemoryAtomicClaimStore()
    first = store.claim("alpilot1", meta={"action": "BUY"})
    second = store.claim("alpilot1", meta={"action": "BUY"})
    assert first.claimed is True
    assert second.claimed is False
    assert "unique" in second.reason.lower() or "already" in second.reason.lower()


def test_supabase_claim_unique_and_unavailable():
    class FakeTable:
        def __init__(self, behavior: str) -> None:
            self.behavior = behavior
            self.inserted: list[dict[str, Any]] = []

        def insert(self, row: dict[str, Any]) -> FakeTable:
            if self.behavior == "unique":
                raise Exception("duplicate key value violates unique constraint 23505")
            if self.behavior == "down":
                raise Exception("connection refused")
            self.inserted.append(row)
            return self

        def execute(self) -> object:
            return object()

    class FakeClient:
        def __init__(self, behavior: str) -> None:
            self.table_obj = FakeTable(behavior)

        def table(self, name: str) -> FakeTable:
            assert name == "bot_live_order_claims"
            return self.table_obj

    ok_store = AtomicLiveClaimStore(FakeClient("ok"))
    assert ok_store.claim("alx").claimed is True
    uniq = AtomicLiveClaimStore(FakeClient("unique"))
    assert uniq.claim("aly").claimed is False
    down = AtomicLiveClaimStore(FakeClient("down"))
    result = down.claim("alz")
    assert result.claimed is False
    assert "unavailable" in result.reason or "blocked" in result.reason


def test_armed_buy_posts_once_via_fake_transport():
    transport = FakeTradingTransport()
    transport.accept_status = "accepted"
    executor = AlpacaLiveExecutor(
        api_key="k",
        api_secret="s",
        transport=transport,
        armed=True,
    )
    claims = InMemoryAtomicClaimStore()
    circuit = InMemoryCircuitStore()
    state = _state()
    limits = _limits()
    result = run_alpaca_live_pilot(
        _alert(),
        PositionState(active_symbol=None),
        state,
        limits,
        armed=True,
        claim_store=claims,
        circuit=circuit,
        executor=executor,
    )
    assert result.submission_attempts == 1
    assert len(transport.posts) == 1
    assert transport.posts[0]["symbol"] == "TQQQ"
    assert transport.posts[0]["side"] == "buy"
    assert transport.posts[0]["type"] == "limit"
    assert result.legs[0].execution_status != EXECUTION_NOT_SUBMITTED
    assert "REAL MONEY" in result.text


def test_timeout_looks_up_never_blind_retries():
    posts: list[dict[str, Any]] = []
    gets: list[str] = []
    orders: dict[str, dict[str, Any]] = {}

    def transport(method, url, headers, params, json_body):
        verb = method.upper()
        if verb == "POST":
            posts.append(dict(json_body))
            client_id = str(json_body["client_order_id"])
            orders[client_id] = {
                "id": "recovered-1",
                "client_order_id": client_id,
                "status": "accepted",
                "qty": json_body["qty"],
                "filled_qty": "0",
                "symbol": json_body["symbol"],
                "side": json_body["side"],
            }
            raise LiveOrderTimeout("simulated")
        if verb == "GET":
            gets.append(url)
            client_id = url.rsplit(":", 1)[-1]
            return orders.get(client_id)
        raise AssertionError(f"unexpected {verb} {url}")

    executor = AlpacaLiveExecutor(
        api_key="k",
        api_secret="s",
        transport=transport,
        armed=True,
    )
    intent = TradeIntent(
        strategy_version=STRATEGY_VERSION,
        signal_symbol="QQQ",
        execution_symbol="TQQQ",
        action="BUY",
        quantity=1,
        estimated_price=50.1,
        estimated_notional=50.1,
        confidence=78,
        regime="trend_up",
        reason="t",
        signal_id="s",
        timestamp="2026-10-06T14:30:00Z",
        purpose="entry",
        client_order_id="altimeoutrecover01",
    )
    result = executor.submit_validated_order(intent, limit_price="50.10")
    assert executor.post_attempts == 1
    assert executor.get_attempts == 1
    assert len(posts) == 1
    assert len(gets) == 1
    assert result.broker_order_id == "recovered-1"
    assert result.post_attempts == 1
    # Second submit must not happen for recovery — only one POST.
    assert len(posts) == 1


def test_unarmed_env_never_posts():
    transport = FakeTradingTransport()
    executor = AlpacaLiveExecutor(
        api_key="k",
        api_secret="s",
        transport=transport,
        armed=False,
    )
    claims = InMemoryAtomicClaimStore()
    circuit = InMemoryCircuitStore()
    result = run_alpaca_live_pilot(
        _alert(),
        PositionState(active_symbol=None),
        _state(),
        _limits(),
        armed=False,
        claim_store=claims,
        circuit=circuit,
        executor=executor,
    )
    assert result.submission_attempts == 0
    assert transport.posts == []
    assert result.legs[0].execution_status == EXECUTION_NOT_SUBMITTED


def test_dry_run_after_strategy_disarms(tmp_path: Path):
    transport_posts: list[Any] = []

    def boom(*_a, **_k):
        transport_posts.append(1)
        raise AssertionError("executor must not be built for dry_run path posts")

    env = _armed_env(ALPACA_LIVE_PILOT_LOG=str(tmp_path / "p.jsonl"))
    result = run_alpaca_live_pilot_after_strategy(
        _alert(),
        PositionState(active_symbol=None),
        dry_run=True,
        webhook_url="",
        data_bar_start=NOW - timedelta(minutes=5),
        now=NOW,
        env=env,
        claim_store=InMemoryAtomicClaimStore(),
        circuit=InMemoryCircuitStore(),
        reservation_store=InMemoryEntryReservationStore(),
        executor=AlpacaLiveExecutor(
            api_key="k",
            api_secret="s",
            transport=lambda *a, **k: (_ for _ in ()).throw(AssertionError("no post")),
            armed=True,
        ),
    )
    assert result is not None
    assert result.armed is False
    assert result.submission_attempts == 0


def test_equity_above_ceiling_blocks_new_exposure():
    state = _state(equity=5_000.0, cash=5_000.0)
    limits = _limits(ALPACA_LIVE_CAPITAL_CEILING="100")
    draft = TradeIntent(
        strategy_version=STRATEGY_VERSION,
        signal_symbol="QQQ",
        execution_symbol="TQQQ",
        action="BUY",
        quantity=0,
        estimated_price=50.1,
        estimated_notional=None,
        confidence=78,
        regime="trend_up",
        reason="t",
        signal_id="s",
        timestamp="2026-10-06T14:30:00Z",
        purpose="entry",
        client_order_id="alceil2",
    )
    ok, reason, _ = check_new_exposure(draft, state, limits)
    assert ok is False
    assert "ceiling" in reason.lower()


def test_margin_blocked_when_notional_exceeds_cash():
    state = _state(equity=1_000.0, cash=10.0, bp=5_000.0)
    limits = _limits()
    draft = TradeIntent(
        strategy_version=STRATEGY_VERSION,
        signal_symbol="QQQ",
        execution_symbol="TQQQ",
        action="BUY",
        quantity=0,
        estimated_price=50.1,
        estimated_notional=None,
        confidence=78,
        regime="trend_up",
        reason="t",
        signal_id="s",
        timestamp="2026-10-06T14:30:00Z",
        purpose="entry",
        client_order_id="almargin",
    )
    ok, reason, _ = check_new_exposure(draft, state, limits)
    assert ok is False
    assert "borrowed" in reason.lower() or "cash" in reason.lower() or "zero" in reason.lower()


def test_both_etfs_block_no_autoliquidate():
    transport = FakeTradingTransport()
    executor = AlpacaLiveExecutor(api_key="k", api_secret="s", transport=transport, armed=True)
    state = _state(positions=(PositionView("TQQQ", 2), PositionView("SQQQ", 1)))
    result = run_alpaca_live_pilot(
        _alert(),
        PositionState(active_symbol="TQQQ"),
        state,
        _limits(),
        armed=True,
        claim_store=InMemoryAtomicClaimStore(),
        circuit=InMemoryCircuitStore(),
        executor=executor,
        buy_block_reason="broker holds both TQQQ and SQQQ; new exposure blocked",
    )
    assert result.legs[0].risk_status == "BLOCKED"
    assert transport.posts == []


def test_kill_switch_blocks_entry_allows_exit_sizing():
    circuit = InMemoryCircuitStore(
        CircuitState(
            bot_id="default",
            day_key=None,
            entries_today=0,
            week_key=None,
            kill_new_entries=True,
            daily_loss_tripped=False,
            weekly_loss_tripped=False,
            drawdown_tripped=False,
        )
    )
    transport = FakeTradingTransport()
    executor = AlpacaLiveExecutor(api_key="k", api_secret="s", transport=transport, armed=True)
    buy = run_alpaca_live_pilot(
        _alert(),
        PositionState(active_symbol=None),
        _state(),
        _limits(),
        armed=True,
        claim_store=InMemoryAtomicClaimStore(),
        circuit=circuit,
        executor=executor,
    )
    assert buy.legs[0].risk_status == "BLOCKED"
    assert "kill" in buy.legs[0].risk_reason.lower()
    assert transport.posts == []

    sell = evaluate_pilot_leg(
        _alert(alert_type="SELL", symbol="TQQQ"),
        symbol="TQQQ",
        action="SELL",
        purpose="exit",
        state=_state(positions=(PositionView("TQQQ", 3),)),
        limits=_limits(),
        armed=True,
        claim_store=InMemoryAtomicClaimStore(),
        circuit=circuit,
        reservation_store=InMemoryEntryReservationStore(),
        executor=executor,
    )
    assert sell.risk_status == "PASS"
    assert sell.intent.quantity == 3
    assert len(transport.posts) == 1
    assert transport.posts[0]["side"] == "sell"


def test_flip_ambiguous_sell_blocks_buy():
    transport = FakeTradingTransport()
    transport.accept_status = "accepted"  # accepted ≠ filled
    executor = AlpacaLiveExecutor(api_key="k", api_secret="s", transport=transport, armed=True)
    result = run_alpaca_live_pilot(
        _alert(alert_type="FLIP", symbol="SQQQ"),
        PositionState(active_symbol="TQQQ"),
        _state(positions=(PositionView("TQQQ", 2),)),
        _limits(),
        armed=True,
        claim_store=InMemoryAtomicClaimStore(),
        circuit=InMemoryCircuitStore(),
        executor=executor,
    )
    assert len(result.legs) == 2
    assert result.legs[0].intent.action == "SELL"
    assert result.legs[1].risk_status == "BLOCKED"
    assert result.legs[1].intent.action == "BUY"
    # Only the sell may have posted; never the opposite buy while sell unfilled.
    assert all(p["side"] == "sell" for p in transport.posts)


def test_flip_filled_flat_allows_entry_post():
    transport = FakeTradingTransport()
    transport.accept_status = "filled"
    transport.filled_qty_on_accept = None  # full qty

    class RefreshReader:
        def __init__(self) -> None:
            self.calls = 0

        def read_snapshot(self):
            self.calls += 1
            return {
                "account": {
                    "status": "ACTIVE",
                    "equity": "1000",
                    "buying_power": "1000",
                    "cash": "1000",
                    "last_equity": "1000",
                    "week_start_equity": "1000",
                    "peak_equity": "1000",
                },
                "positions": [],
                "orders": [],
                "quotes": {
                    "TQQQ": {"quote": {"bp": 50.0, "ap": 50.10, "t": NOW.isoformat()}},
                    "SQQQ": {"quote": {"bp": 20.0, "ap": 20.05, "t": NOW.isoformat()}},
                },
            }

    reader = RefreshReader()
    executor = AlpacaLiveExecutor(api_key="k", api_secret="s", transport=transport, armed=True)
    result = run_alpaca_live_pilot(
        _alert(alert_type="FLIP", symbol="SQQQ"),
        PositionState(active_symbol="TQQQ"),
        _state(positions=(PositionView("TQQQ", 2),), equity=1000, cash=1000, bp=1000),
        _limits(),
        armed=True,
        claim_store=InMemoryAtomicClaimStore(),
        circuit=InMemoryCircuitStore(),
        reservation_store=InMemoryEntryReservationStore(),
        executor=executor,
        reader=reader,  # type: ignore[arg-type]
        now=NOW,
        data_bar_start=NOW - timedelta(minutes=5),
    )
    assert result.legs[0].execution_status == ORDER_FILLED
    assert result.legs[1].intent.action == "BUY"
    assert reader.calls >= 1
    sides = [p["side"] for p in transport.posts]
    assert sides[0] == "sell"
    # Success path requires broker-confirmed flat + fresh quote + risk pass.
    assert result.legs[1].risk_status == "PASS", result.legs[1].risk_reason
    assert "buy" in sides


def test_one_entry_per_day_durable():
    """Atomic reservation UNIQUE slot — not racy circuit entries_today."""
    reservations = InMemoryEntryReservationStore()
    day = trading_day_america_new_york(NOW)
    assert reservations.reserve(trading_day=day, meta={"purpose": "prior"}).reserved
    transport = FakeTradingTransport()
    executor = AlpacaLiveExecutor(api_key="k", api_secret="s", transport=transport, armed=True)
    result = run_alpaca_live_pilot(
        _alert(),
        PositionState(active_symbol=None),
        _state(),
        _limits(),
        armed=True,
        claim_store=InMemoryAtomicClaimStore(),
        circuit=InMemoryCircuitStore(),
        reservation_store=reservations,
        executor=executor,
        now=NOW,
    )
    assert result.legs[0].risk_status == "BLOCKED"
    assert "reserv" in result.legs[0].risk_reason.lower()
    assert transport.posts == []


def test_marketable_limit_from_ask_bid():
    assert marketable_limit_price(action="BUY", bid=50.0, ask=50.10) == "50.10"
    assert marketable_limit_price(action="SELL", bid=50.0, ask=50.10) == "50.00"


def test_accepted_not_treated_as_filled_for_flip():
    from robinhood_flip import advance_flip

    flip = advance_flip(
        exit_status="ACCEPTED",
        exit_qty_remaining=0.0,
        broker_state_known=True,
    )
    assert flip.entry_validation_allowed is False


def test_partial_fill_blocks_flip_entry():
    from robinhood_flip import advance_flip

    flip = advance_flip(
        exit_status=ORDER_UNKNOWN if False else "PARTIALLY_FILLED",
        exit_qty_remaining=1.0,
        broker_state_known=True,
    )
    assert flip.entry_validation_allowed is False


def test_strategy_version_frozen():
    assert STRATEGY_VERSION == "1.0.0"


def test_claim_store_required_message_when_supabase_missing(tmp_path: Path):
    env = _armed_env(ALPACA_LIVE_PILOT_LOG=str(tmp_path / "p.jsonl"))
    env.pop("SUPABASE_URL", None)
    env.pop("NEXT_PUBLIC_SUPABASE_URL", None)
    env.pop("SUPABASE_SERVICE_ROLE_KEY", None)
    # Inject no claim store → after_strategy tries claim_store_for_pilot and blocks.
    result = run_alpaca_live_pilot_after_strategy(
        _alert(),
        PositionState(active_symbol=None),
        dry_run=False,
        webhook_url="",
        data_bar_start=NOW - timedelta(minutes=5),
        now=NOW,
        env=env,
        # force missing claim by not injecting and removing supabase — but executor
        # would also try real network; inject a dead executor and no client.
        executor=AlpacaLiveExecutor(
            api_key="k",
            api_secret="s",
            transport=FakeTradingTransport(),
            armed=True,
        ),
        client=None,
    )
    assert result is not None
    assert result.blocked >= 1
    assert result.submission_attempts == 0
    assert "claim" in result.legs[0].risk_reason.lower()


def _flat_refresh_state(**overrides: object) -> BrokerState:
    base = _state(positions=(), equity=1000, cash=1000, bp=1000)
    if not overrides:
        return base
    # Rebuild with selective overrides via dataclass-like fields on BrokerState.
    return BrokerState(
        known=bool(overrides.get("known", True)),
        account=overrides.get("account", base.account),  # type: ignore[arg-type]
        positions=overrides.get("positions", ()),  # type: ignore[arg-type]
        open_orders=overrides.get("open_orders", ()),  # type: ignore[arg-type]
        quotes=overrides.get("quotes", base.quotes),  # type: ignore[arg-type]
        quote=None,
        data_bar_start=NOW - timedelta(minutes=5),
        now=NOW,
        order_history_complete=True,
        detail="refresh-test",
        known_client_order_ids=frozenset(),
        orders_today=0,
    )


@pytest.mark.parametrize(
    "exit_status",
    [
        "SUBMITTED",
        "ACCEPTED",
        "PARTIALLY_FILLED",
        "CANCELLED",
        "REJECTED",
        "EXPIRED",
        "UNKNOWN",
        "NEW",
        "",
    ],
)
def test_flip_refresh_blocks_non_filled_sell_statuses(exit_status: str):
    gate = require_post_sell_flat_for_flip(
        exit_status=exit_status or None,
        exit_symbol="TQQQ",
        entry_symbol="SQQQ",
        refreshed_state=_flat_refresh_state(),
        refresh_error=None,
        limits=_limits(),
        reader_present=True,
    )
    assert gate.allowed is False


def test_flip_refresh_blocks_reader_missing_and_refresh_error():
    missing = require_post_sell_flat_for_flip(
        exit_status=ORDER_FILLED,
        exit_symbol="TQQQ",
        entry_symbol="SQQQ",
        refreshed_state=_flat_refresh_state(),
        refresh_error=None,
        limits=_limits(),
        reader_present=False,
    )
    assert missing.allowed is False
    assert "reader" in missing.reason.lower()

    failed = require_post_sell_flat_for_flip(
        exit_status=ORDER_FILLED,
        exit_symbol="TQQQ",
        entry_symbol="SQQQ",
        refreshed_state=None,
        refresh_error="timeout talking to Alpaca",
        limits=_limits(),
        reader_present=True,
    )
    assert failed.allowed is False
    assert "refresh" in failed.reason.lower()


def test_flip_refresh_blocks_qty_open_sell_both_etfs_missing_cash():
    qty = require_post_sell_flat_for_flip(
        exit_status=ORDER_FILLED,
        exit_symbol="TQQQ",
        entry_symbol="SQQQ",
        refreshed_state=_flat_refresh_state(positions=(PositionView("TQQQ", 1),)),
        refresh_error=None,
        limits=_limits(),
        reader_present=True,
    )
    assert qty.allowed is False
    assert "qty" in qty.reason.lower() or "open" in qty.reason.lower()

    open_sell = require_post_sell_flat_for_flip(
        exit_status=ORDER_FILLED,
        exit_symbol="TQQQ",
        entry_symbol="SQQQ",
        refreshed_state=_flat_refresh_state(
            open_orders=(
                OpenOrderView(
                    client_order_id="sell-open",
                    symbol="TQQQ",
                    side="sell",
                    status="ACCEPTED",
                    qty=1,
                ),
            )
        ),
        refresh_error=None,
        limits=_limits(),
        reader_present=True,
    )
    assert open_sell.allowed is False
    assert "sell" in open_sell.reason.lower()

    both = require_post_sell_flat_for_flip(
        exit_status=ORDER_FILLED,
        exit_symbol="TQQQ",
        entry_symbol="SQQQ",
        refreshed_state=_flat_refresh_state(
            positions=(PositionView("TQQQ", 0), PositionView("SQQQ", 2))
        ),
        refresh_error=None,
        limits=_limits(),
        reader_present=True,
    )
    # Opposite already held (SQQQ) blocks even if exit qty is 0.
    assert both.allowed is False

    cashless = _state(positions=(), equity=1000, cash=1000, bp=1000)
    assert cashless.account is not None
    no_cash_account = AccountView(
        available=True,
        status="ACTIVE",
        buying_power=1000,
        equity=1000,
        day_start_equity=1000,
        week_start_equity=1000,
        peak_equity=1000,
        cash=None,
    )
    missing_cash = require_post_sell_flat_for_flip(
        exit_status=ORDER_FILLED,
        exit_symbol="TQQQ",
        entry_symbol="SQQQ",
        refreshed_state=_flat_refresh_state(account=no_cash_account),
        refresh_error=None,
        limits=_limits(),
        reader_present=True,
    )
    assert missing_cash.allowed is False
    assert "cash" in missing_cash.reason.lower()


def test_flip_refresh_blocks_stale_and_wide_quote():
    stale_quotes = (
        QuoteView("TQQQ", 50.0, 50.10, NOW - timedelta(minutes=30), last=50.05),
        QuoteView("SQQQ", 20.0, 20.05, NOW - timedelta(minutes=30), last=20.02),
    )
    stale = require_post_sell_flat_for_flip(
        exit_status=ORDER_FILLED,
        exit_symbol="TQQQ",
        entry_symbol="SQQQ",
        refreshed_state=_flat_refresh_state(quotes=stale_quotes),
        refresh_error=None,
        limits=_limits(ALPACA_LIVE_MAX_QUOTE_AGE_SECONDS="5"),
        reader_present=True,
    )
    assert stale.allowed is False
    assert "stale" in stale.reason.lower()

    wide_quotes = (
        QuoteView("TQQQ", 50.0, 50.10, NOW, last=50.05),
        QuoteView("SQQQ", 20.0, 25.0, NOW, last=22.0),
    )
    wide = require_post_sell_flat_for_flip(
        exit_status=ORDER_FILLED,
        exit_symbol="TQQQ",
        entry_symbol="SQQQ",
        refreshed_state=_flat_refresh_state(quotes=wide_quotes),
        refresh_error=None,
        limits=_limits(ALPACA_LIVE_MAX_SPREAD_BPS="10"),
        reader_present=True,
    )
    assert wide.allowed is False
    assert "spread" in wide.reason.lower()


def test_flip_refresh_success_and_inverse_sqqq_to_tqqq():
    ok = require_post_sell_flat_for_flip(
        exit_status=ORDER_FILLED,
        exit_symbol="TQQQ",
        entry_symbol="SQQQ",
        refreshed_state=_flat_refresh_state(),
        refresh_error=None,
        limits=_limits(),
        reader_present=True,
    )
    assert ok.allowed is True
    assert ok.exit_qty_remaining == 0.0

    inverse = require_post_sell_flat_for_flip(
        exit_status=ORDER_FILLED,
        exit_symbol="SQQQ",
        entry_symbol="TQQQ",
        refreshed_state=_flat_refresh_state(),
        refresh_error=None,
        limits=_limits(),
        reader_present=True,
    )
    assert inverse.allowed is True


def test_flip_without_reader_never_posts_opposite_buy():
    transport = FakeTradingTransport()
    transport.accept_status = "filled"
    executor = AlpacaLiveExecutor(api_key="k", api_secret="s", transport=transport, armed=True)
    result = run_alpaca_live_pilot(
        _alert(alert_type="FLIP", symbol="SQQQ"),
        PositionState(active_symbol="TQQQ"),
        _state(positions=(PositionView("TQQQ", 2),)),
        _limits(),
        armed=True,
        claim_store=InMemoryAtomicClaimStore(),
        circuit=InMemoryCircuitStore(),
        reservation_store=InMemoryEntryReservationStore(),
        executor=executor,
        reader=None,
    )
    assert result.legs[1].risk_status == "BLOCKED"
    assert all(p["side"] == "sell" for p in transport.posts)


def test_daily_reservation_same_day_second_signal_blocked():
    reservations = InMemoryEntryReservationStore()
    transport = FakeTradingTransport()
    transport.accept_status = "filled"
    executor = AlpacaLiveExecutor(api_key="k", api_secret="s", transport=transport, armed=True)
    first = run_alpaca_live_pilot(
        _alert(timestamp="2026-10-06T14:30:00Z"),
        PositionState(active_symbol=None),
        _state(),
        _limits(),
        armed=True,
        claim_store=InMemoryAtomicClaimStore(),
        circuit=InMemoryCircuitStore(),
        reservation_store=reservations,
        executor=executor,
        now=NOW,
    )
    assert first.legs[0].risk_status == "PASS"
    second = run_alpaca_live_pilot(
        _alert(timestamp="2026-10-06T15:00:00Z"),
        PositionState(active_symbol=None),
        _state(),
        _limits(),
        armed=True,
        claim_store=InMemoryAtomicClaimStore(),
        circuit=InMemoryCircuitStore(),
        reservation_store=reservations,
        executor=executor,
        now=NOW,
    )
    assert second.legs[0].risk_status == "BLOCKED"
    assert "reserv" in second.legs[0].risk_reason.lower()


def test_daily_reservation_restart_keeps_slot_next_day_new_slot():
    reservations = InMemoryEntryReservationStore()
    day = trading_day_america_new_york(NOW)
    assert reservations.reserve(trading_day=day).reserved
    # Restart simulation: same store state survives (durable UNIQUE).
    assert reservations.has_reservation(trading_day=day) is True
    transport = FakeTradingTransport()
    executor = AlpacaLiveExecutor(api_key="k", api_secret="s", transport=transport, armed=True)
    blocked = run_alpaca_live_pilot(
        _alert(),
        PositionState(active_symbol=None),
        _state(),
        _limits(),
        armed=True,
        claim_store=InMemoryAtomicClaimStore(),
        circuit=InMemoryCircuitStore(),
        reservation_store=reservations,
        executor=executor,
        now=NOW,
    )
    assert blocked.legs[0].risk_status == "BLOCKED"

    next_day = NOW + timedelta(days=1)
    allowed = run_alpaca_live_pilot(
        _alert(timestamp="2026-10-07T14:30:00Z"),
        PositionState(active_symbol=None),
        _state(),
        _limits(),
        armed=True,
        claim_store=InMemoryAtomicClaimStore(),
        circuit=InMemoryCircuitStore(),
        reservation_store=reservations,
        executor=executor,
        now=next_day,
    )
    assert allowed.legs[0].risk_status == "PASS"


def test_exit_does_not_consume_daily_reservation_flip_buy_does():
    reservations = InMemoryEntryReservationStore()
    transport = FakeTradingTransport()
    transport.accept_status = "filled"
    executor = AlpacaLiveExecutor(api_key="k", api_secret="s", transport=transport, armed=True)
    sell = run_alpaca_live_pilot(
        _alert(alert_type="SELL", symbol="TQQQ"),
        PositionState(active_symbol="TQQQ"),
        _state(positions=(PositionView("TQQQ", 2),)),
        _limits(),
        armed=True,
        claim_store=InMemoryAtomicClaimStore(),
        circuit=InMemoryCircuitStore(),
        reservation_store=reservations,
        executor=executor,
        now=NOW,
    )
    assert sell.legs[0].risk_status == "PASS"
    assert reservations.rows == []

    class RefreshReader:
        def read_snapshot(self):
            return {
                "account": {
                    "status": "ACTIVE",
                    "equity": "1000",
                    "buying_power": "1000",
                    "cash": "1000",
                    "last_equity": "1000",
                    "week_start_equity": "1000",
                    "peak_equity": "1000",
                },
                "positions": [],
                "orders": [],
                "quotes": {
                    "TQQQ": {"quote": {"bp": 50.0, "ap": 50.10, "t": NOW.isoformat()}},
                    "SQQQ": {"quote": {"bp": 20.0, "ap": 20.05, "t": NOW.isoformat()}},
                },
            }

    flip_transport = FakeTradingTransport()
    flip_transport.accept_status = "filled"
    flip = run_alpaca_live_pilot(
        _alert(alert_type="FLIP", symbol="SQQQ"),
        PositionState(active_symbol="TQQQ"),
        _state(positions=(PositionView("TQQQ", 2),), equity=1000, cash=1000, bp=1000),
        _limits(),
        armed=True,
        claim_store=InMemoryAtomicClaimStore(),
        circuit=InMemoryCircuitStore(),
        reservation_store=reservations,
        executor=AlpacaLiveExecutor(
            api_key="k", api_secret="s", transport=flip_transport, armed=True
        ),
        reader=RefreshReader(),  # type: ignore[arg-type]
        now=NOW,
        data_bar_start=NOW - timedelta(minutes=5),
    )
    assert flip.legs[0].intent.action == "SELL"
    assert flip.legs[1].intent.action == "BUY"
    assert flip.legs[1].risk_status == "PASS", flip.legs[1].risk_reason
    assert len(reservations.rows) == 1
    assert reservations.rows[0]["purpose"] == "flip_entry"


def test_reservation_store_required_when_supabase_missing(tmp_path: Path):
    env = _armed_env(ALPACA_LIVE_PILOT_LOG=str(tmp_path / "r.jsonl"))
    env.pop("SUPABASE_URL", None)
    env.pop("NEXT_PUBLIC_SUPABASE_URL", None)
    env.pop("SUPABASE_SERVICE_ROLE_KEY", None)
    result = run_alpaca_live_pilot_after_strategy(
        _alert(),
        PositionState(active_symbol=None),
        dry_run=False,
        webhook_url="",
        data_bar_start=NOW - timedelta(minutes=5),
        now=NOW,
        env=env,
        claim_store=InMemoryAtomicClaimStore(),
        executor=AlpacaLiveExecutor(
            api_key="k",
            api_secret="s",
            transport=FakeTradingTransport(),
            armed=True,
        ),
        client=None,
    )
    assert result is not None
    assert result.blocked >= 1
    assert result.submission_attempts == 0
    assert "reserv" in result.legs[0].risk_reason.lower()


def test_submitted_sell_blocked_in_advance_flip():
    from robinhood_flip import advance_flip

    flip = advance_flip(
        exit_status="SUBMITTED",
        exit_qty_remaining=0.0,
        broker_state_known=True,
    )
    assert flip.entry_validation_allowed is False


def test_shadow_submission_flag_still_false():
    assert ALPACA_LIVE_SUBMISSION_IMPLEMENTED is False
    assert ALPACA_LIVE_PILOT_SUBMISSION_IMPLEMENTED is True
