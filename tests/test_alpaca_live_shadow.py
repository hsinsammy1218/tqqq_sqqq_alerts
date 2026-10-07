"""Alpaca live shadow: real reads, zero live writes."""

from __future__ import annotations

import inspect
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from alpaca_live_audit import LiveShadowAuditLog
from alpaca_live_claim import LiveShadowClaimStore
from alpaca_live_credentials import load_live_credentials
from alpaca_live_reconcile import reconcile_live_books
from alpaca_live_risk import check_new_exposure, live_limits_from_env, size_buy
from alpaca_live_shadow import (
    run_alpaca_live_shadow,
    run_alpaca_live_shadow_after_strategy,
)
from alpaca_paper import PAPER_TRADING_BASE_URL, is_live_trading_host
from brokers.alpaca_endpoints import LIVE_TRADING_BASE_URL, is_exact_live_trading_host
from brokers.alpaca_live_broker import ALPACA_LIVE_SUBMISSION_IMPLEMENTED, AlpacaLiveBroker
from brokers.alpaca_live_normalize import normalize_snapshot
from brokers.alpaca_live_reader import (
    AlpacaLiveReadClient,
    AlpacaLiveWriteRejected,
    ReadOnlySession,
    assert_get_only,
)
from brokers.mode import ALPACA_LIVE_SHADOW, parse_execution_broker
from brokers.types import (
    AccountView,
    BrokerState,
    LiveSubmissionDisabled,
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


def _limits(**overrides: object):
    base = dict(
        ALPACA_LIVE_MAX_POSITION_PCT="0.10",
        ALPACA_LIVE_MAX_ORDER_NOTIONAL="200",
        ALPACA_LIVE_CAPITAL_CEILING="1000",
        ALPACA_LIVE_MAX_DAILY_LOSS_PCT="0.05",
        ALPACA_LIVE_MAX_WEEKLY_LOSS_PCT="0.10",
        ALPACA_LIVE_MAX_DRAWDOWN_PCT="0.15",
        ALPACA_LIVE_MAX_ORDERS_PER_DAY="3",
        ALPACA_LIVE_NEW_ENTRIES_ENABLED="true",
        ALPACA_LIVE_ENABLED="true",
        LIVE_TRADING_ENABLED="true",
        ALPACA_LIVE_MAX_QUOTE_AGE_SECONDS="60",
        ALPACA_LIVE_MAX_SPREAD_BPS="50",
        ALPACA_LIVE_MAX_DATA_AGE_MINUTES="90",
    )
    base.update({k: str(v) for k, v in overrides.items()})
    return live_limits_from_env(base)


def _state(
    *,
    known: bool = True,
    equity: float = 10_000.0,
    cash: float = 5_000.0,
    bp: float = 10_000.0,
    positions: tuple[PositionView, ...] = (),
    open_orders: tuple[OpenOrderView, ...] = (),
    quotes: tuple[QuoteView, ...] | None = None,
    day_start: float | None = 10_000.0,
    week_start: float | None = 10_000.0,
    peak: float | None = 10_000.0,
) -> BrokerState:
    if quotes is None:
        quotes = (
            QuoteView("TQQQ", 50.0, 50.10, NOW, last=50.05),
            QuoteView("SQQQ", 20.0, 20.05, NOW, last=20.02),
        )
    account = AccountView(
        available=True,
        status="ACTIVE",
        buying_power=bp,
        equity=equity,
        day_start_equity=day_start,
        week_start_equity=week_start,
        peak_equity=peak,
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


def _payloads(**overrides: object) -> dict:
    raw = {
        "account": {
            "status": "ACTIVE",
            "equity": "10000",
            "buying_power": "10000",
            "cash": "5000",
            "last_equity": "10000",
            "week_start_equity": "10000",
            "peak_equity": "10000",
        },
        "positions": [],
        "orders": [],
        "quotes": {
            "TQQQ": {
                "quote": {
                    "bp": 50.0,
                    "ap": 50.10,
                    "t": NOW.isoformat(),
                }
            },
            "SQQQ": {
                "quote": {
                    "bp": 20.0,
                    "ap": 20.05,
                    "t": NOW.isoformat(),
                }
            },
        },
    }
    raw.update(overrides)
    return raw


def _fake_transport(payloads: dict):
    calls: list[tuple[str, str]] = []

    def _call(method: str, url: str, headers: dict, params: dict | None):
        assert method == "GET"
        calls.append((method, url))
        if url.endswith("/v2/account"):
            return payloads["account"]
        if url.endswith("/v2/positions"):
            return payloads["positions"]
        if "/v2/orders" in url:
            return payloads["orders"]
        if "TQQQ" in url and "quotes" in url:
            return payloads["quotes"]["TQQQ"]
        if "SQQQ" in url and "quotes" in url:
            return payloads["quotes"]["SQQQ"]
        raise AssertionError(f"unexpected url {url}")

    return _call, calls


def test_mode_accepts_live_shadow_rejects_alpaca_live():
    from brokers.mode import ALPACA_LIVE_PILOT

    assert parse_execution_broker("alpaca_live_shadow") == ALPACA_LIVE_SHADOW
    assert parse_execution_broker("alpaca_live_pilot") == ALPACA_LIVE_PILOT
    with pytest.raises(UnsafeBrokerConfiguration):
        parse_execution_broker("alpaca_live")
    with pytest.raises(UnsafeBrokerConfiguration):
        parse_execution_broker("ALPACA_LIVE")


def test_submission_constant_false_and_env_flags_cannot_submit():
    assert ALPACA_LIVE_SUBMISSION_IMPLEMENTED is False
    assert STRATEGY_VERSION == "1.0.0"
    broker = AlpacaLiveBroker(_state())
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
        reason="test",
        signal_id="x",
        timestamp="2026-10-06T14:30:00Z",
        purpose="entry",
        client_order_id="altest",
    )
    with pytest.raises(LiveSubmissionDisabled):
        broker.submit_order(intent)
    with pytest.raises(LiveSubmissionDisabled):
        broker.cancel_order("abc")
    with pytest.raises(LiveSubmissionDisabled):
        broker.replace_order("abc", intent)
    assert broker.submission_attempts == 3


def test_reader_rejects_non_get_before_socket():
    with pytest.raises(AlpacaLiveWriteRejected):
        assert_get_only("POST")
    with pytest.raises(AlpacaLiveWriteRejected):
        assert_get_only("DELETE")
    with pytest.raises(AlpacaLiveWriteRejected):
        assert_get_only("PATCH")
    with pytest.raises(AlpacaLiveWriteRejected):
        assert_get_only("PUT")

    class BoomSession:
        def request(self, *args, **kwargs):
            raise AssertionError("socket opened")

        def get(self, *args, **kwargs):
            raise AssertionError("socket opened")

    session = ReadOnlySession(BoomSession())  # type: ignore[arg-type]
    with pytest.raises(AlpacaLiveWriteRejected):
        session.request("POST", "https://api.alpaca.markets/v2/orders")


def test_reader_has_no_mutating_api_and_write_methods_raise():
    source = inspect.getsource(AlpacaLiveReadClient)
    assert "def submit_order" in source
    client = AlpacaLiveReadClient(
        api_key="K",
        api_secret="S",
        trading_base_url=LIVE_TRADING_BASE_URL,
        transport=lambda *a, **k: (_ for _ in ()).throw(AssertionError("no net")),
    )
    with pytest.raises(AlpacaLiveWriteRejected):
        client.submit_order()
    with pytest.raises(AlpacaLiveWriteRejected):
        client.cancel_order()
    with pytest.raises(AlpacaLiveWriteRejected):
        client.replace_order()
    with pytest.raises(AlpacaLiveWriteRejected):
        client.close_all_positions()


def test_paper_host_gate_intact():
    assert is_live_trading_host(PAPER_TRADING_BASE_URL) is False
    assert is_live_trading_host(LIVE_TRADING_BASE_URL) is True
    assert is_exact_live_trading_host(LIVE_TRADING_BASE_URL) is True
    assert is_exact_live_trading_host(PAPER_TRADING_BASE_URL) is False
    assert is_exact_live_trading_host("https://api.alpaca.markets/") is True
    assert is_exact_live_trading_host("https://api.alpaca.markets/v2") is False


def test_credential_isolation_no_paper_fallback_or_reuse():
    with pytest.raises(UnsafeBrokerConfiguration):
        load_live_credentials(
            {
                "ALPACA_API_KEY": "paper-key",
                "ALPACA_API_SECRET": "paper-secret",
            }
        )
    with pytest.raises(UnsafeBrokerConfiguration):
        load_live_credentials(
            {
                "ALPACA_LIVE_API_KEY": "same",
                "ALPACA_LIVE_API_SECRET": "live-secret",
                "ALPACA_API_KEY": "same",
                "ALPACA_API_SECRET": "other",
            }
        )
    with pytest.raises(UnsafeBrokerConfiguration):
        load_live_credentials(
            {
                "ALPACA_LIVE_API_KEY": "live-key",
                "ALPACA_LIVE_API_SECRET": "live-secret",
                "ALPACA_LIVE_TRADING_BASE_URL": PAPER_TRADING_BASE_URL,
            }
        )
    creds = load_live_credentials(
        {
            "ALPACA_LIVE_API_KEY": "live-key",
            "ALPACA_LIVE_API_SECRET": "live-secret",
            "ALPACA_API_KEY": "paper-key",
            "ALPACA_API_SECRET": "paper-secret",
        }
    )
    assert creds.trading_base_url == LIVE_TRADING_BASE_URL
    assert creds.api_key == "live-key"


def test_normalize_and_read_snapshot_through_transport():
    payloads = _payloads()
    transport, calls = _fake_transport(payloads)
    client = AlpacaLiveReadClient(
        api_key="live-key",
        api_secret="live-secret",
        trading_base_url=LIVE_TRADING_BASE_URL,
        transport=transport,
    )
    raw = client.read_snapshot()
    state = normalize_snapshot(raw, now=NOW, data_bar_start=NOW - timedelta(minutes=5))
    assert state.known is True
    assert state.account is not None
    assert state.account.equity == 10_000.0
    assert all(method == "GET" for method, _ in calls)
    assert not any("POST" in method for method, _ in calls)


def test_buy_proposed_not_submitted_with_live_flags_on(tmp_path: Path):
    # Equity must be at/under capital ceiling or new exposure blocks.
    state = _state(equity=1_000.0, cash=1_000.0, bp=1_000.0, day_start=1_000.0, week_start=1_000.0, peak=1_000.0)
    limits = _limits()
    claims = LiveShadowClaimStore(tmp_path / "claims.jsonl")
    result = run_alpaca_live_shadow(
        _alert(),
        PositionState(active_symbol=None),
        state,
        limits,
        claim_store=claims,
    )
    assert result.submitted is False
    assert result.submission_attempts == 0
    assert result.legs[0].execution_status == "NOT_SUBMITTED"
    assert result.legs[0].risk_status == "PASS"
    assert result.legs[0].intent.quantity >= 1
    assert result.legs[0].intent.estimated_price == pytest.approx(50.10)
    # Live flags were true in limits; still not submitted.
    assert limits.live_enabled_flag is True


def test_missing_live_limits_block():
    state = _state()
    limits = live_limits_from_env(
        {
            "ALPACA_LIVE_NEW_ENTRIES_ENABLED": "true",
        }
    )
    result = run_alpaca_live_shadow(
        _alert(),
        PositionState(active_symbol=None),
        state,
        limits,
    )
    assert result.legs[0].risk_status == "BLOCKED"
    assert result.legs[0].execution_status == "NOT_SUBMITTED"


def test_borrowed_funds_blocked():
    # Equity at/under ceiling so the cash/margin gate is the one that fires.
    state = _state(equity=1_000.0, cash=10.0, bp=10_000.0, day_start=1_000.0, week_start=1_000.0, peak=1_000.0)
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
        client_order_id="alborrow",
    )
    ok, reason, _ = check_new_exposure(draft, state, limits)
    assert ok is False
    assert "borrowed" in reason or "zero shares" in reason or "cash" in reason.lower()


def test_capital_ceiling_blocks_when_equity_above_ceiling():
    """Equity above the sleeve ceiling must not silently size down."""
    state = _state(equity=50_000.0, cash=50_000.0, bp=50_000.0)
    limits = live_limits_from_env(
        {
            "ALPACA_LIVE_MAX_POSITION_PCT": "1",
            "ALPACA_LIVE_MAX_ORDER_NOTIONAL": "10000",
            "ALPACA_LIVE_CAPITAL_CEILING": "100",
            "ALPACA_LIVE_MAX_DAILY_LOSS_PCT": "0.05",
            "ALPACA_LIVE_MAX_WEEKLY_LOSS_PCT": "0.10",
            "ALPACA_LIVE_MAX_DRAWDOWN_PCT": "0.15",
            "ALPACA_LIVE_MAX_ORDERS_PER_DAY": "3",
            "ALPACA_LIVE_NEW_ENTRIES_ENABLED": "true",
            "ALPACA_LIVE_MAX_QUOTE_AGE_SECONDS": "60",
            "ALPACA_LIVE_MAX_SPREAD_BPS": "50",
            "ALPACA_LIVE_MAX_DATA_AGE_MINUTES": "90",
        }
    )
    qty, notional = size_buy(50.0, state, limits)
    assert qty == 0
    assert notional == 0.0
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
        client_order_id="alceil",
    )
    ok, reason, _ = check_new_exposure(draft, state, limits)
    assert ok is False
    assert "capital" in reason.lower() or "ceiling" in reason.lower()


def test_capital_ceiling_sizes_when_equity_at_or_below_ceiling():
    state = _state(equity=100.0, cash=100.0, bp=100.0, day_start=100.0, week_start=100.0, peak=100.0)
    limits = live_limits_from_env(
        {
            "ALPACA_LIVE_MAX_POSITION_PCT": "1",
            "ALPACA_LIVE_MAX_ORDER_NOTIONAL": "10000",
            "ALPACA_LIVE_CAPITAL_CEILING": "100",
            "ALPACA_LIVE_MAX_DAILY_LOSS_PCT": "0.05",
            "ALPACA_LIVE_MAX_WEEKLY_LOSS_PCT": "0.10",
            "ALPACA_LIVE_MAX_DRAWDOWN_PCT": "0.15",
            "ALPACA_LIVE_MAX_ORDERS_PER_DAY": "3",
            "ALPACA_LIVE_NEW_ENTRIES_ENABLED": "true",
            "ALPACA_LIVE_MAX_QUOTE_AGE_SECONDS": "60",
            "ALPACA_LIVE_MAX_SPREAD_BPS": "50",
            "ALPACA_LIVE_MAX_DATA_AGE_MINUTES": "90",
        }
    )
    qty, notional = size_buy(50.0, state, limits)
    assert notional <= 100.0 + 1e-6
    assert qty == 2


def test_reconcile_mismatch_and_both_etfs_block():
    state = _state(positions=(PositionView("TQQQ", 5), PositionView("SQQQ", 2)))
    book = reconcile_live_books(PositionState(active_symbol="TQQQ"), state)
    assert book.blocks_new_exposure is True
    state2 = _state(positions=(PositionView("TQQQ", 5),))
    book2 = reconcile_live_books(PositionState(active_symbol=None), state2)
    assert book2.blocks_new_exposure is True


def test_open_and_unknown_orders_block():
    state = _state(
        open_orders=(
            OpenOrderView("c1", "TQQQ", "buy", "ACCEPTED", qty=1),
        )
    )
    book = reconcile_live_books(PositionState(active_symbol=None), state)
    assert book.blocks_new_exposure is True
    state_u = _state(
        open_orders=(OpenOrderView("c2", "TQQQ", "buy", "UNKNOWN", qty=1),)
    )
    book_u = reconcile_live_books(PositionState(active_symbol=None), state_u)
    assert "unknown" in book_u.reason


def test_flip_proposes_sell_blocks_entry():
    state = _state(positions=(PositionView("TQQQ", 3),))
    limits = _limits()
    result = run_alpaca_live_shadow(
        _alert(alert_type="FLIP", symbol="SQQQ"),
        PositionState(active_symbol="TQQQ"),
        state,
        limits,
    )
    assert len(result.legs) == 2
    assert result.legs[0].intent.action == "SELL"
    assert result.legs[0].execution_status == "NOT_SUBMITTED"
    assert result.legs[1].risk_status == "BLOCKED"
    assert result.submitted is False


def test_quote_by_execution_symbol_not_list_order():
    state = _state(
        quotes=(
            QuoteView("SQQQ", 19.0, 30.0, NOW),  # wide
            QuoteView("TQQQ", 50.0, 50.05, NOW),
        )
    )
    limits = _limits()
    result = run_alpaca_live_shadow(
        _alert(symbol="TQQQ"),
        PositionState(active_symbol=None),
        state,
        limits,
    )
    assert result.legs[0].intent.estimated_price == pytest.approx(50.05)


def test_idempotent_claim(tmp_path: Path):
    store = LiveShadowClaimStore(tmp_path / "c.jsonl")
    first = store.claim("alabc", meta={"action": "BUY"})
    second = store.claim("alabc", meta={"action": "BUY"})
    assert first.claimed is True
    assert second.claimed is False


def test_audit_forces_not_submitted_and_redacts(tmp_path: Path):
    log = LiveShadowAuditLog(tmp_path / "a.jsonl")
    log.append(
        {
            "client_order_id": "al1",
            "execution_mode": "should_be_overwritten",
            "execution_status": "FILLED",
            "api_key": "super-secret",
            "broker": "wrong",
        }
    )
    rows = log.read()
    assert rows[0]["execution_mode"] == "live_shadow"
    assert rows[0]["execution_status"] == "NOT_SUBMITTED"
    assert rows[0]["broker"] == "alpaca"
    assert rows[0]["api_key"] == "[redacted]"


def test_after_strategy_hook_unknown_without_creds(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("ALPACA_LIVE_API_KEY", raising=False)
    monkeypatch.delenv("ALPACA_LIVE_API_SECRET", raising=False)
    env = {
        "EXECUTION_BROKER": "alpaca_live_shadow",
        "ALPACA_LIVE_SHADOW_LOG": str(tmp_path / "shadow.jsonl"),
        "ALPACA_LIVE_CLAIM_LOG": str(tmp_path / "claims.jsonl"),
        "ALPACA_LIVE_MAX_POSITION_PCT": "0.1",
        "ALPACA_LIVE_MAX_ORDER_NOTIONAL": "200",
        "ALPACA_LIVE_CAPITAL_CEILING": "1000",
        "ALPACA_LIVE_MAX_DAILY_LOSS_PCT": "0.05",
        "ALPACA_LIVE_MAX_WEEKLY_LOSS_PCT": "0.1",
        "ALPACA_LIVE_MAX_DRAWDOWN_PCT": "0.15",
        "ALPACA_LIVE_MAX_ORDERS_PER_DAY": "1",
        "ALPACA_LIVE_NEW_ENTRIES_ENABLED": "true",
    }
    result = run_alpaca_live_shadow_after_strategy(
        _alert(),
        PositionState(active_symbol=None),
        dry_run=True,
        webhook_url="",
        data_bar_start=NOW - timedelta(minutes=5),
        now=NOW,
        env=env,
    )
    assert result is not None
    assert result.submitted is False
    assert result.legs[0].risk_status == "BLOCKED"
    assert result.legs[0].execution_status == "NOT_SUBMITTED"


def test_network_write_guard_on_full_path(tmp_path: Path):
    payloads = _payloads(
        positions=[{"symbol": "TQQQ", "qty": "0"}],
    )
    # Provide day marks for risk
    payloads["account"]["week_start_equity"] = "10000"
    payloads["account"]["peak_equity"] = "10000"
    transport, calls = _fake_transport(payloads)

    mutating = []

    def guarded(method, url, headers, params):
        if method.upper() != "GET":
            mutating.append((method, url))
            raise AssertionError(f"mutating call {method} {url}")
        return transport(method, url, headers, params)

    env = {
        "EXECUTION_BROKER": "alpaca_live_shadow",
        "ALPACA_LIVE_API_KEY": "live-key",
        "ALPACA_LIVE_API_SECRET": "live-secret",
        "ALPACA_API_KEY": "paper-key",
        "ALPACA_API_SECRET": "paper-secret",
        "ALPACA_LIVE_SHADOW_LOG": str(tmp_path / "shadow.jsonl"),
        "ALPACA_LIVE_CLAIM_LOG": str(tmp_path / "claims.jsonl"),
        "ALPACA_LIVE_MAX_POSITION_PCT": "0.1",
        "ALPACA_LIVE_MAX_ORDER_NOTIONAL": "200",
        "ALPACA_LIVE_CAPITAL_CEILING": "1000",
        "ALPACA_LIVE_MAX_DAILY_LOSS_PCT": "0.05",
        "ALPACA_LIVE_MAX_WEEKLY_LOSS_PCT": "0.1",
        "ALPACA_LIVE_MAX_DRAWDOWN_PCT": "0.15",
        "ALPACA_LIVE_MAX_ORDERS_PER_DAY": "3",
        "ALPACA_LIVE_NEW_ENTRIES_ENABLED": "true",
        "ALPACA_LIVE_ENABLED": "true",
        "LIVE_TRADING_ENABLED": "true",
        "ALPACA_LIVE_MAX_QUOTE_AGE_SECONDS": "120",
        "ALPACA_LIVE_MAX_SPREAD_BPS": "50",
        "ALPACA_LIVE_MAX_DATA_AGE_MINUTES": "90",
    }
    client = AlpacaLiveReadClient(
        api_key="live-key",
        api_secret="live-secret",
        trading_base_url=LIVE_TRADING_BASE_URL,
        transport=guarded,
    )
    result = run_alpaca_live_shadow_after_strategy(
        _alert(),
        PositionState(active_symbol=None),
        dry_run=True,
        webhook_url="",
        data_bar_start=NOW - timedelta(minutes=5),
        now=NOW,
        env=env,
        client=client,
    )
    assert result is not None
    assert mutating == []
    assert all(m == "GET" for m, _ in calls)
    assert result.submission_attempts == 0
    assert all(leg.execution_status == "NOT_SUBMITTED" for leg in result.legs)
    # No live key leaked into audit
    audit_text = (tmp_path / "shadow.jsonl").read_text(encoding="utf-8")
    assert "live-secret" not in audit_text
    assert "paper-secret" not in audit_text


def test_production_modules_do_not_post_live_orders():
    for path in (
        Path("alpaca_live_risk.py"),
        Path("alpaca_live_reconcile.py"),
        Path("brokers/alpaca_live_reader.py"),
        Path("brokers/alpaca_live_broker.py"),
        Path("alpaca_live_shadow.py"),
    ):
        text = path.read_text(encoding="utf-8")
        assert "submit_limit_order" not in text
        assert "api.alpaca.markets/v2/orders" not in text or "GET" in text
    reader = Path("brokers/alpaca_live_reader.py").read_text(encoding="utf-8")
    broker = Path("brokers/alpaca_live_broker.py").read_text(encoding="utf-8")
    assert "requests.post" not in reader
    assert "requests.delete" not in reader
    assert "requests.post" not in broker
    # Discord webhook POST is allowed in the runner; it must not target Alpaca.
    shadow = Path("alpaca_live_shadow.py").read_text(encoding="utf-8")
    assert "requests.post(webhook_url" in shadow
    assert re.search(r"requests\.post\([^)]*alpaca", shadow, re.I) is None
    assert ALPACA_LIVE_SUBMISSION_IMPLEMENTED is False


def test_paper_notional_defaults_not_used_as_live_hardcode():
    # Ensure live risk source does not reference 100/500 as defaults for size.
    source = Path("alpaca_live_risk.py").read_text(encoding="utf-8")
    assert "500" not in source
    assert '="100"' not in source and "default=100" not in source
