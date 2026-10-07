"""Alpaca Live Pilot: controlled real-money path under multi-key arming.

Default cron remains paper. Shadow remains zero-write. This module posts only
through AlpacaLiveExecutor.submit_validated_order after atomic claim + risk.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

from alpaca_live_audit import LivePilotAuditLog
from alpaca_live_claim import ClaimStore, claim_store_for_pilot
from alpaca_live_circuit import (
    CircuitStore,
    InMemoryCircuitStore,
    record_new_entry,
    trip_from_risk_reason,
)
from alpaca_live_credentials import load_live_credentials
from alpaca_live_entry_reservation import (
    EntryReservationStore,
    InMemoryEntryReservationStore,
    reservation_store_for_pilot,
    reserve_daily_entry,
)
from alpaca_live_flip_refresh import require_post_sell_flat_for_flip
from alpaca_live_reconcile import reconcile_live_books
from alpaca_live_risk import (
    AlpacaLiveLimits,
    action_block_reason,
    check_new_exposure,
    execution_symbol_block_reason,
    live_limits_from_env,
    pilot_arming_block_reason,
    quote_for,
    size_buy,
)
from alpaca_live_shadow import (
    _draft_intent,
    _position_from_broker,
    _price_for,
    load_live_state,
    live_shadow_client_order_id,
)
from alpaca_paper import build_order_intents
from brokers.alpaca_live_broker import ALPACA_LIVE_SUBMISSION_IMPLEMENTED as SHADOW_SUBMISSION_FLAG
from brokers.alpaca_live_executor import (
    ALPACA_LIVE_PILOT_SUBMISSION_IMPLEMENTED,
    AlpacaLiveExecutor,
    LiveOrderAmbiguous,
    LiveOrderRejected,
    LiveOrderTimeout,
    LivePilotError,
    SubmitResult,
    marketable_limit_price,
)
from brokers.alpaca_live_reader import AlpacaLiveReadClient, client_from_credentials
from brokers.mode import ALPACA_LIVE_PILOT, execution_broker_from_environ
from brokers.types import (
    EXECUTION_NOT_SUBMITTED,
    ORDER_FILLED,
    ORDER_PARTIALLY_FILLED,
    ORDER_UNKNOWN,
    BrokerState,
    TradeIntent,
)
from robinhood_audit import redact
from robinhood_flip import recovery_block_reason
from strategy_params import STRATEGY_VERSION
from strategy_types import AlertDecision, PositionState

_SIGNAL_SYMBOL = "QQQ"


@dataclass(frozen=True)
class LivePilotLeg:
    intent: TradeIntent
    risk_status: str
    risk_reason: str
    checks: tuple[tuple[str, str], ...]
    execution_status: str
    text: str
    submit: SubmitResult | None = None
    broker_order_id: str | None = None

    def audit_row(self, *, broker_state: str) -> dict[str, Any]:
        row = self.intent.as_audit()
        row.update(
            {
                "broker": "alpaca",
                "execution_mode": "live_pilot",
                "risk_status": self.risk_status,
                "risk_reason": self.risk_reason,
                "broker_state": broker_state,
                "execution_status": self.execution_status,
                "broker_order_id": self.broker_order_id,
                "real_money": True,
            }
        )
        if self.submit is not None:
            row["filled_qty"] = self.submit.filled_qty
            row["post_attempts"] = self.submit.post_attempts
            row["submit_detail"] = self.submit.detail
        return row


@dataclass(frozen=True)
class LivePilotRun:
    legs: tuple[LivePilotLeg, ...]
    text: str
    submission_attempts: int
    proposed: int = 0
    blocked: int = 0
    submitted: int = 0
    armed: bool = False


def _format_leg(leg: LivePilotLeg, *, armed: bool) -> str:
    price = leg.intent.estimated_price
    notional = leg.intent.estimated_notional
    price_txt = f"${price:.2f}" if price is not None else "n/a"
    notional_txt = f"${notional:.2f}" if notional is not None else "n/a"
    check_lines = "\n".join(f"{name}: {status}" for name, status in leg.checks) or "none"
    return "\n".join(
        (
            "ALPACA LIVE PILOT — REAL MONEY",
            "",
            f"Strategy: {leg.intent.strategy_version}",
            f"Signal: {leg.intent.signal_symbol}",
            f"Action: {leg.intent.action} {leg.intent.execution_symbol}",
            f"Confidence: {leg.intent.confidence}%",
            f"Regime: {leg.intent.regime or 'n/a'}",
            "",
            f"Quantity: {leg.intent.quantity}",
            f"Estimated Price: {price_txt}",
            f"Estimated Value: {notional_txt}",
            f"Client order id: {leg.intent.client_order_id}",
            f"Broker order id: {leg.broker_order_id or 'n/a'}",
            "",
            "Risk Checks:",
            check_lines,
            f"Risk: {leg.risk_status} — {leg.risk_reason}",
            "",
            "Execution:",
            "ALPACA LIVE PILOT MODE",
            "REAL MONEY TRADED WHEN SUBMITTED",
            f"multi_key_armed: {str(armed).lower()}",
            f"ALPACA_LIVE_PILOT_SUBMISSION_IMPLEMENTED: {ALPACA_LIVE_PILOT_SUBMISSION_IMPLEMENTED}",
            f"shadow_ALPACA_LIVE_SUBMISSION_IMPLEMENTED: {SHADOW_SUBMISSION_FLAG}",
            f"execution_status: {leg.execution_status}",
        )
    )


def _blocked(
    intent: TradeIntent,
    reason: str,
    checks: tuple[tuple[str, str], ...] = (),
    *,
    armed: bool,
    status: str = EXECUTION_NOT_SUBMITTED,
) -> LivePilotLeg:
    leg = LivePilotLeg(
        intent=intent,
        risk_status="BLOCKED",
        risk_reason=reason,
        checks=checks,
        execution_status=status,
        text="",
    )
    return replace(leg, text=_format_leg(leg, armed=armed))


def _refresh_state(
    reader: AlpacaLiveReadClient | None,
    *,
    now: datetime,
    data_bar_start: datetime | None,
) -> BrokerState:
    return load_live_state(reader, now=now, data_bar_start=data_bar_start)


def _maybe_submit(
    intent: TradeIntent,
    *,
    state: BrokerState,
    executor: AlpacaLiveExecutor | None,
    armed: bool,
) -> tuple[str, SubmitResult | None, str]:
    if not armed or executor is None:
        return EXECUTION_NOT_SUBMITTED, None, "not armed; not submitted"
    quote = quote_for(state, intent.execution_symbol)
    try:
        limit = marketable_limit_price(
            action=intent.action,
            bid=quote.bid if quote else None,
            ask=quote.ask if quote else None,
        )
        result = executor.submit_validated_order(intent, limit_price=limit)
    except (LiveOrderRejected, LiveOrderTimeout, LiveOrderAmbiguous, LivePilotError) as exc:
        return ORDER_UNKNOWN, None, str(exc)
    # Accepted ≠ filled. Surface broker status as-is.
    return result.status, result, result.detail


def evaluate_pilot_leg(
    alert: AlertDecision,
    *,
    symbol: str,
    action: str,
    purpose: str,
    state: BrokerState,
    limits: AlpacaLiveLimits,
    armed: bool,
    claim_store: ClaimStore,
    circuit: CircuitStore,
    reservation_store: EntryReservationStore,
    executor: AlpacaLiveExecutor | None = None,
    reader: AlpacaLiveReadClient | None = None,
    exit_status: str | None = None,
    exit_qty_remaining: float | None = None,
    exit_symbol: str | None = None,
    buy_block_reason: str | None = None,
    now: datetime | None = None,
    data_bar_start: datetime | None = None,
    flip_refresh_error: str | None = None,
) -> LivePilotLeg:
    clock = now or datetime.now(timezone.utc)
    symbol_reason = execution_symbol_block_reason(symbol)
    action_reason = action_block_reason(action)
    price = _price_for(action, quote_for(state, symbol), symbol)
    if symbol_reason or action_reason:
        intent = _draft_intent(
            alert,
            symbol=symbol or "INVALID",
            action=action or "INVALID",
            purpose=purpose,
            price=price,
            quantity=0,
        )
        return _blocked(
            intent,
            symbol_reason or action_reason or "rejected",
            (
                ("Symbol", "FAIL" if symbol_reason else "PASS"),
                ("Action", "FAIL" if action_reason else "PASS"),
            ),
            armed=armed,
        )

    client_id = live_shadow_client_order_id(
        strategy_version=STRATEGY_VERSION,
        timestamp=alert.timestamp,
        symbol=symbol,
        action=action,
        purpose=purpose,
    )
    recovery = recovery_block_reason(
        open_order_count=len(
            [o for o in state.open_orders if o.symbol.upper() in {"TQQQ", "SQQQ"}]
        ),
        proposed_client_order_id=client_id,
        known_client_order_ids=state.known_client_order_ids,
        broker_state_known=state.known,
    )

    if purpose == "flip_entry":
        # Never assume flat from the SELL response alone — mandatory broker refresh.
        gate = require_post_sell_flat_for_flip(
            exit_status=exit_status,
            exit_symbol=exit_symbol or "",
            entry_symbol=symbol,
            refreshed_state=state,
            refresh_error=flip_refresh_error,
            limits=limits,
            reader_present=reader is not None,
        )
        if not gate.allowed:
            intent = _draft_intent(
                alert, symbol=symbol, action="BUY", purpose=purpose, price=price, quantity=0
            )
            return _blocked(intent, gate.reason, gate.checks or (("Flip refresh", "FAIL"),), armed=armed)
        if gate.refreshed_state is not None:
            state = gate.refreshed_state
            price = _price_for("BUY", quote_for(state, symbol), symbol)
        exit_qty_remaining = gate.exit_qty_remaining

    if action == "SELL":
        held = 0.0
        if state.known:
            held = sum(row.qty for row in state.positions if row.symbol.upper() == symbol.upper())
        if not state.known:
            intent = _draft_intent(
                alert, symbol=symbol, action="SELL", purpose=purpose, price=price, quantity=0
            )
            return _blocked(
                intent, "unknown broker state; sell not sized", (("Broker state", "FAIL"),), armed=armed
            )
        if held <= 0:
            intent = _draft_intent(
                alert, symbol=symbol, action="SELL", purpose=purpose, price=price, quantity=0
            )
            return _blocked(intent, "no position to sell", (("Position", "FAIL"),), armed=armed)
        quantity = int(held) if float(held).is_integer() else 0
        if quantity < 1:
            intent = _draft_intent(
                alert, symbol=symbol, action="SELL", purpose=purpose, price=price, quantity=0
            )
            return _blocked(
                intent,
                "fractional residual is not a whole-share sell",
                (("Position", "FAIL"),),
                armed=armed,
            )
        if recovery:
            intent = _draft_intent(
                alert, symbol=symbol, action="SELL", purpose=purpose, price=price, quantity=quantity
            )
            return _blocked(intent, recovery, (("Recovery", "FAIL"),), armed=armed)
        intent = _draft_intent(
            alert, symbol=symbol, action="SELL", purpose=purpose, price=price, quantity=quantity
        )
        claim = claim_store.claim(
            intent.client_order_id,
            meta={
                "action": "SELL",
                "symbol": symbol,
                "purpose": purpose,
                "execution_mode": "live_pilot",
            },
        )
        if not claim.claimed:
            return _blocked(intent, claim.reason, (("Claim", "FAIL"),), armed=armed)
        status, submit, detail = _maybe_submit(
            intent, state=state, executor=executor, armed=armed
        )
        leg = LivePilotLeg(
            intent=intent,
            risk_status="PASS",
            risk_reason=detail,
            checks=(("Symbol", "PASS"), ("Action", "PASS"), ("Position", "PASS"), ("Claim", "PASS")),
            execution_status=status,
            text="",
            submit=submit,
            broker_order_id=submit.broker_order_id if submit else None,
        )
        return replace(leg, text=_format_leg(leg, armed=armed))

    # BUY path — new exposure
    circuit_state = circuit.load()
    # Kill / loss / drawdown only. Daily slot is atomic reservation (not entries_today).
    circuit_block = circuit_state.blocks_new_entry(
        today=clock.date(), max_entries_per_day=1, enforce_entries_today=False
    )
    if circuit_block:
        intent = _draft_intent(
            alert, symbol=symbol, action="BUY", purpose=purpose, price=price, quantity=0
        )
        return _blocked(intent, circuit_block, (("Circuit", "FAIL"),), armed=armed)

    if buy_block_reason:
        intent = _draft_intent(
            alert, symbol=symbol, action="BUY", purpose=purpose, price=price, quantity=0
        )
        return _blocked(intent, buy_block_reason, (("Reconcile", "FAIL"),), armed=armed)

    draft = _draft_intent(
        alert, symbol=symbol, action="BUY", purpose=purpose, price=price, quantity=0
    )
    if recovery:
        return _blocked(draft, recovery, (("Recovery", "FAIL"),), armed=armed)

    # Fresh preflight refresh before BUY when a reader is available.
    # For flip_entry the mandatory post-SELL refresh already ran above.
    if reader is not None and purpose != "flip_entry":
        state = _refresh_state(reader, now=clock, data_bar_start=data_bar_start)
        book = reconcile_live_books(None, state)
        if book.blocks_new_exposure:
            return _blocked(draft, book.reason, (("Preflight", "FAIL"),), armed=armed)
        price = _price_for("BUY", quote_for(state, symbol), symbol)
        draft = replace(draft, estimated_price=price)

    allowed, reason, checks = check_new_exposure(draft, state, limits)
    if not allowed:
        trip_from_risk_reason(circuit, reason)
        return _blocked(draft, reason, checks, armed=armed)

    quantity, notional = size_buy(float(price or 0), state, limits)
    if quantity < 1:
        return _blocked(draft, "sized zero shares", checks + (("Size", "FAIL"),), armed=armed)
    intent = replace(draft, quantity=quantity, estimated_price=price, estimated_notional=notional)

    # Atomic daily entry reservation BEFORE claim / real BUY.
    # Exits and FLIP SELL do not reach this path. Crash after reserve: do not auto-free.
    reservation = reserve_daily_entry(
        reservation_store,
        now=clock,
        meta={
            "action": "BUY",
            "symbol": symbol,
            "purpose": purpose,
            "client_order_id": intent.client_order_id,
            "signal_id": intent.signal_id,
            "execution_mode": "live_pilot",
        },
    )
    if not reservation.reserved:
        return _blocked(
            intent,
            reservation.reason,
            checks + (("Daily entry reservation", "FAIL"),),
            armed=armed,
        )

    claim = claim_store.claim(
        intent.client_order_id,
        meta={
            "action": "BUY",
            "symbol": symbol,
            "purpose": purpose,
            "execution_mode": "live_pilot",
        },
    )
    if not claim.claimed:
        return _blocked(
            intent,
            claim.reason,
            checks + (("Daily entry reservation", "PASS"), ("Claim", "FAIL")),
            armed=armed,
        )

    if not armed:
        leg = LivePilotLeg(
            intent=intent,
            risk_status="PASS",
            risk_reason="risk passed but multi-key arming incomplete; not submitted",
            checks=checks
            + (("Daily entry reservation", "PASS"), ("Claim", "PASS"), ("Arming", "FAIL")),
            execution_status=EXECUTION_NOT_SUBMITTED,
            text="",
        )
        return replace(leg, text=_format_leg(leg, armed=armed))

    status, submit, detail = _maybe_submit(intent, state=state, executor=executor, armed=armed)
    # Best-effort audit counter only — UNIQUE reservation is the concurrency authority.
    if status != ORDER_UNKNOWN or submit is not None:
        try:
            record_new_entry(circuit, today=clock.date())
        except Exception:  # noqa: BLE001
            pass

    leg = LivePilotLeg(
        intent=intent,
        risk_status="PASS",
        risk_reason=detail or reason,
        checks=checks
        + (("Daily entry reservation", "PASS"), ("Claim", "PASS"), ("Arming", "PASS")),
        execution_status=status,
        text="",
        submit=submit,
        broker_order_id=submit.broker_order_id if submit else None,
    )
    return replace(leg, text=_format_leg(leg, armed=armed))


def run_alpaca_live_pilot(
    alert: AlertDecision,
    position_before: PositionState | None,
    state: BrokerState,
    limits: AlpacaLiveLimits,
    *,
    armed: bool,
    claim_store: ClaimStore,
    circuit: CircuitStore,
    reservation_store: EntryReservationStore | None = None,
    executor: AlpacaLiveExecutor | None = None,
    reader: AlpacaLiveReadClient | None = None,
    buy_block_reason: str | None = None,
    now: datetime | None = None,
    data_bar_start: datetime | None = None,
) -> LivePilotRun:
    clock = now or datetime.now(timezone.utc)
    alert_type = (alert.alert_type or "").upper()
    symbol = (alert.symbol or "").upper()
    order_intents = build_order_intents(alert, position_before)
    legs: list[LivePilotLeg] = []
    exit_status: str | None = None
    exit_qty_remaining: float | None = None
    exit_symbol: str | None = None
    posts_before = executor.post_attempts if executor is not None else 0
    reservations: EntryReservationStore = reservation_store or InMemoryEntryReservationStore()

    if alert_type in {"BUY", "SELL", "FLIP"} and not order_intents:
        legs.append(
            evaluate_pilot_leg(
                alert,
                symbol=symbol,
                action="BUY" if alert_type != "SELL" else "SELL",
                purpose="rejected",
                state=state,
                limits=limits,
                armed=armed,
                claim_store=claim_store,
                circuit=circuit,
                reservation_store=reservations,
                executor=executor,
                reader=reader,
                buy_block_reason=buy_block_reason,
                now=clock,
                data_bar_start=data_bar_start,
            )
        )

    for intent in order_intents:
        action = "BUY" if intent.side == "buy" else "SELL"
        if intent.purpose == "flip_entry":
            # FLIP: mandatory post-SELL broker refresh before opposite BUY.
            if exit_status is None:
                exit_status = ORDER_UNKNOWN
                exit_qty_remaining = None
            prior_symbol = exit_symbol or next(
                (leg.intent.execution_symbol for leg in legs if leg.intent.action == "SELL"),
                "",
            )
            refresh_error: str | None = None
            if reader is None:
                refresh_error = "reader missing; cannot confirm flat after SELL"
            else:
                try:
                    state = _refresh_state(reader, now=clock, data_bar_start=data_bar_start)
                except Exception as exc:  # noqa: BLE001
                    refresh_error = str(exc)
                    state = BrokerState(known=False, detail=f"flip refresh failed: {exc}")
            leg = evaluate_pilot_leg(
                alert,
                symbol=intent.symbol,
                action=action,
                purpose=intent.purpose,
                state=state,
                limits=limits,
                armed=armed,
                claim_store=claim_store,
                circuit=circuit,
                reservation_store=reservations,
                executor=executor,
                reader=reader,
                exit_status=exit_status,
                exit_qty_remaining=exit_qty_remaining,
                exit_symbol=prior_symbol,
                buy_block_reason=buy_block_reason,
                now=clock,
                data_bar_start=data_bar_start,
                flip_refresh_error=refresh_error,
            )
            legs.append(leg)
            continue

        leg = evaluate_pilot_leg(
            alert,
            symbol=intent.symbol,
            action=action,
            purpose=intent.purpose,
            state=state,
            limits=limits,
            armed=armed,
            claim_store=claim_store,
            circuit=circuit,
            reservation_store=reservations,
            executor=executor,
            reader=reader,
            buy_block_reason=buy_block_reason,
            now=clock,
            data_bar_start=data_bar_start,
        )
        legs.append(leg)
        if intent.purpose in {"flip_exit", "exit"} and action == "SELL":
            exit_status = leg.execution_status
            exit_symbol = leg.intent.execution_symbol
            if leg.submit is not None and leg.execution_status == ORDER_FILLED:
                # Partial fills are authoritative — remaining qty must be zero.
                remaining = max(0.0, float(leg.intent.quantity) - float(leg.submit.filled_qty))
                if leg.submit.filled_qty + 1e-9 < float(leg.intent.quantity):
                    exit_status = ORDER_PARTIALLY_FILLED
                    exit_qty_remaining = remaining
                else:
                    exit_qty_remaining = 0.0
            elif leg.execution_status == ORDER_FILLED:
                exit_qty_remaining = 0.0
            else:
                # Any sell ambiguity blocks the opposite buy.
                # Do NOT treat SELL response alone as flat — refresh gate will re-check.
                exit_qty_remaining = None

    posts = (executor.post_attempts if executor is not None else 0) - posts_before
    proposed = sum(1 for leg in legs if leg.risk_status == "PASS")
    blocked = sum(1 for leg in legs if leg.risk_status == "BLOCKED")
    submitted = sum(
        1
        for leg in legs
        if leg.execution_status not in {EXECUTION_NOT_SUBMITTED, ""} and leg.submit is not None
    )
    text = (
        "\n\n".join(leg.text for leg in legs)
        if legs
        else "ALPACA LIVE PILOT\n\nNo order."
    )
    text += (
        f"\n\nCounters: proposed={proposed} blocked={blocked} "
        f"submitted={submitted} post_attempts={posts} armed={str(armed).lower()}"
    )
    return LivePilotRun(
        legs=tuple(legs),
        text=text,
        submission_attempts=posts,
        proposed=proposed,
        blocked=blocked,
        submitted=submitted,
        armed=armed,
    )


def _post_discord(webhook_url: str, text: str, *, dry_run: bool) -> None:
    payload = redact(
        {
            "username": "QQQ Swing Alerts",
            "content": (
                "🚨 ALPACA LIVE PILOT — REAL MONEY 🚨\n"
                "Orders may spend real cash. Review immediately if unexpected."
            ),
            "embeds": [
                {
                    "title": "Alpaca live pilot (REAL MONEY)",
                    "description": text[:3900],
                    "color": 0xDC2626,
                }
            ],
        }
    )
    if dry_run or not webhook_url:
        return
    try:
        response = requests.post(webhook_url, json=payload, timeout=15)
        if getattr(response, "status_code", 0) >= 400:
            print(f"[alpaca-live-pilot] Discord failed: {response.status_code}")
    except requests.RequestException:
        print("[alpaca-live-pilot] Discord error")


def run_alpaca_live_pilot_after_strategy(
    alert: AlertDecision,
    position_before: PositionState | None,
    *,
    dry_run: bool,
    webhook_url: str,
    data_bar_start: datetime | None,
    now: datetime | None,
    env: Mapping[str, str] | None = None,
    client: AlpacaLiveReadClient | None = None,
    transport=None,
    claim_store: ClaimStore | None = None,
    circuit: CircuitStore | None = None,
    reservation_store: EntryReservationStore | None = None,
    executor: AlpacaLiveExecutor | None = None,
) -> LivePilotRun | None:
    """Hook used by main. Submits only when fully armed and not dry_run."""
    source_map = os.environ if env is None else env
    source = dict(source_map)
    if execution_broker_from_environ(source) != ALPACA_LIVE_PILOT:
        return None

    arming_reason = pilot_arming_block_reason(
        source,
        submission_implemented=ALPACA_LIVE_PILOT_SUBMISSION_IMPLEMENTED,
        execution_broker=ALPACA_LIVE_PILOT,
    )
    # dry_run forces disarm even if env keys are set — safety for local rehearsals.
    armed = arming_reason is None and not dry_run

    clock = now or datetime.now(timezone.utc)
    reader = client
    creds = None
    try:
        creds = load_live_credentials(source)
    except Exception:  # noqa: BLE001
        creds = None
    if reader is None and creds is not None:
        try:
            reader = client_from_credentials(creds, transport=transport)
        except Exception:  # noqa: BLE001
            reader = None

    state = load_live_state(reader, now=clock, data_bar_start=data_bar_start)
    limits = live_limits_from_env(source)

    def _fail_closed(purpose: str, reason: str, check_name: str) -> LivePilotRun:
        audit = LivePilotAuditLog(
            Path(source.get("ALPACA_LIVE_PILOT_LOG", "logs/alpaca_live_pilot.jsonl"))
        )
        intent = _draft_intent(
            alert,
            symbol=(alert.symbol or "TQQQ").upper(),
            action="BUY",
            purpose=purpose,
            price=None,
            quantity=0,
        )
        leg = _blocked(intent, reason, ((check_name, "FAIL"),), armed=False)
        audit.append(leg.audit_row(broker_state="unknown"))
        _post_discord(webhook_url, leg.text, dry_run=dry_run)
        print(leg.text)
        return LivePilotRun(
            legs=(leg,),
            text=leg.text,
            submission_attempts=0,
            proposed=0,
            blocked=1,
            submitted=0,
            armed=False,
        )

    try:
        claims: ClaimStore = claim_store or claim_store_for_pilot(source)
    except Exception as exc:  # noqa: BLE001
        return _fail_closed(
            "claim_unavailable",
            f"atomic claim store unavailable; new exposure blocked ({exc})",
            "Claim store",
        )

    try:
        reservations: EntryReservationStore = reservation_store or reservation_store_for_pilot(
            source
        )
    except Exception as exc:  # noqa: BLE001
        return _fail_closed(
            "reservation_unavailable",
            f"atomic daily entry reservation unavailable; new exposure blocked ({exc})",
            "Daily entry reservation",
        )

    circuit_store: CircuitStore = circuit or InMemoryCircuitStore()
    # Prefer durable Supabase circuit when credentials exist and none injected.
    has_supabase = bool(
        (source.get("SUPABASE_URL") or source.get("NEXT_PUBLIC_SUPABASE_URL") or "").strip()
        and (source.get("SUPABASE_SERVICE_ROLE_KEY") or "").strip()
    )
    if circuit is None and has_supabase:
        try:
            from alpaca_live_circuit import SupabaseCircuitStore

            circuit_store = SupabaseCircuitStore()
        except Exception:  # noqa: BLE001
            circuit_store = InMemoryCircuitStore()
            from alpaca_live_circuit import CircuitState

            circuit_store.save(
                CircuitState(
                    bot_id="default",
                    day_key=None,
                    entries_today=0,
                    week_key=None,
                    kill_new_entries=True,
                    daily_loss_tripped=False,
                    weekly_loss_tripped=False,
                    drawdown_tripped=False,
                    detail="durable circuit unavailable",
                )
            )

    live_executor = executor
    if live_executor is None and armed and creds is not None:
        # Build executor with same transport for tests; real session otherwise.
        # Reader transport is GET-only shaped; executor needs its own POST transport.
        live_executor = AlpacaLiveExecutor(
            api_key=creds.api_key,
            api_secret=creds.api_secret,
            trading_base_url=creds.trading_base_url,
            armed=True,
        )

    if arming_reason and not dry_run:
        # Still evaluate/block clearly when misconfigured.
        pass

    book = reconcile_live_books(position_before, state)
    buy_block = book.reason if book.blocks_new_exposure else None
    if state.known:
        tqqq = sum(p.qty for p in state.positions if p.symbol.upper() == "TQQQ")
        sqqq = sum(p.qty for p in state.positions if p.symbol.upper() == "SQQQ")
        if tqqq > 0 and sqqq > 0:
            buy_block = "broker holds both TQQQ and SQQQ; new exposure blocked"

    intent_position = (
        _position_from_broker(state, position_before) if state.known else position_before
    )
    # If arming failed, force executor None so no POST can occur.
    if not armed:
        live_executor = None

    result = run_alpaca_live_pilot(
        alert,
        intent_position,
        state,
        limits,
        armed=armed,
        claim_store=claims,
        circuit=circuit_store,
        reservation_store=reservations,
        executor=live_executor,
        reader=reader,
        buy_block_reason=buy_block or (arming_reason if arming_reason and not dry_run else None),
        now=clock,
        data_bar_start=data_bar_start,
    )

    # Safety: shadow submission flag must remain false.
    if SHADOW_SUBMISSION_FLAG:
        raise RuntimeError("shadow ALPACA_LIVE_SUBMISSION_IMPLEMENTED must stay False")

    audit = LivePilotAuditLog(Path(source.get("ALPACA_LIVE_PILOT_LOG", "logs/alpaca_live_pilot.jsonl")))
    broker_state = "unknown" if not state.known else ("mismatch" if buy_block else "known")
    for leg in result.legs:
        audit.append(leg.audit_row(broker_state=broker_state))
    _post_discord(webhook_url, result.text, dry_run=dry_run)
    print(result.text)
    return result
