"""Render-safe Robinhood host handoff: create PENDING intents only.

Case C: this process never opens a Robinhood write transport and never calls
place_equity_order / review_equity_order / cancel_equity_order.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from alpaca_paper import build_order_intents
from brokers.mode import ROBINHOOD_HOST_HANDOFF, execution_broker_from_environ
from brokers.robinhood_agentic import LIVE_SUBMISSION_IMPLEMENTED, RobinhoodAgenticBroker
from brokers.types import BrokerState, TradeIntent
from robinhood_flip import (
    flip_entry_meta,
    flip_exit_meta,
    new_flip_pair_id,
    recovery_block_reason,
)
from robinhood_host_audit import HostAuditLog, post_host_discord
from robinhood_account_isolation import (
    AccountIsolationError,
    ENV_BOUND_ACCOUNT,
    env_account_pin,
    mask_account_number,
)
from robinhood_host_risk import (
    check_handoff_prevalidation,
    host_limits_from_env,
    quote_for,
    sell_qty_allowed,
    size_host_buy,
)
from robinhood_intent import DEFAULT_INTENT_TTL_SECONDS, wrap_intent
from robinhood_intent_store import InMemoryIntentStore, IntentStore, StoreResult
from robinhood_shadow import _draft_intent
from strategy_params import STRATEGY_VERSION
from strategy_types import AlertDecision, PositionState

# Hard barrier: handoff never submits.
HANDOFF_SUBMISSION_IMPLEMENTED = False


@dataclass(frozen=True)
class HandoffLeg:
    intent: TradeIntent
    risk_status: str
    risk_reason: str
    checks: tuple[tuple[str, str], ...]
    execution_status: str
    text: str
    persisted: bool = False

    def audit_row(self) -> dict[str, Any]:
        row = self.intent.as_audit()
        row.update(
            {
                "broker": "robinhood",
                "execution_mode": "host_handoff",
                "risk_status": self.risk_status,
                "risk_reason": self.risk_reason,
                "execution_status": self.execution_status,
                "persisted": self.persisted,
                "real_money": False,
                "auth_case": "C",
            }
        )
        return row


@dataclass(frozen=True)
class HandoffRun:
    legs: tuple[HandoffLeg, ...]
    text: str
    submission_attempts: int
    pending_created: int = 0
    blocked: int = 0


def _format_leg(leg: HandoffLeg) -> str:
    price = leg.intent.estimated_price
    notional = leg.intent.estimated_notional
    price_txt = f"${price:.2f}" if price is not None else "n/a"
    notional_txt = f"${notional:.2f}" if notional is not None else "n/a"
    check_lines = "\n".join(f"{name}: {status}" for name, status in leg.checks) or "none"
    return "\n".join(
        (
            "ROBINHOOD HOST HANDOFF — PENDING INTENT ONLY",
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
            "",
            "Risk Checks:",
            check_lines,
            f"Risk: {leg.risk_status} — {leg.risk_reason}",
            "",
            "Execution:",
            "HOST HANDOFF MODE (CASE C)",
            "NO ROBINHOOD ORDER SUBMITTED BY RENDER",
            f"LIVE_SUBMISSION_IMPLEMENTED: {LIVE_SUBMISSION_IMPLEMENTED}",
            f"HANDOFF_SUBMISSION_IMPLEMENTED: {HANDOFF_SUBMISSION_IMPLEMENTED}",
            f"execution_status: {leg.execution_status}",
            f"persisted: {str(leg.persisted).lower()}",
        )
    )


def _blocked(
    intent: TradeIntent,
    reason: str,
    checks: tuple[tuple[str, str], ...] = (),
    *,
    status: str = "BLOCKED",
) -> HandoffLeg:
    leg = HandoffLeg(
        intent=intent,
        risk_status="BLOCKED",
        risk_reason=reason,
        checks=checks,
        execution_status=status,
        text="",
        persisted=False,
    )
    return replace(leg, text=_format_leg(leg))


def _price_for(state: BrokerState, symbol: str, action: str) -> float | None:
    quote = quote_for(state, symbol)
    if quote is None:
        return None
    if action == "BUY":
        return float(quote.ask) if quote.ask is not None else None
    return float(quote.bid) if quote.bid is not None else None


def evaluate_handoff_leg(
    alert: AlertDecision,
    *,
    symbol: str,
    action: str,
    purpose: str,
    state: BrokerState,
    limits,
    store: IntentStore,
    reservation_store: Any | None,
    now: datetime,
    ttl_seconds: int,
    env: Mapping[str, str] | None = None,
    extra_meta: Mapping[str, Any] | None = None,
) -> HandoffLeg:
    """Persist PENDING only. Pre-handoff ≠ host execution risk.

    Reservation is intentionally ignored here — authoritative daily entry
    reservation happens at the host BUY boundary after claim + risk.
    ``reservation_store`` is accepted for API compatibility and unused.
    When ``env`` includes ``ROBINHOOD_AGENTIC_ACCOUNT_NUMBER``, stamp it into
    intent meta (full ID for routing; masked for logs).
    """
    del reservation_store  # host BUY boundary owns reservation
    price = _price_for(state, symbol, action)
    # Optional hint sizing when Render somehow has known state (tests). Host
    # always re-sizes. Unknown state → quantity 0 (host fills in).
    quantity = 0
    notional: float | None = None
    if action == "BUY" and state.known and price is not None:
        quantity, notional = size_host_buy(float(price), state, limits)
    elif action == "SELL" and state.known:
        qty, _sell_reason = sell_qty_allowed(state, symbol, requested=10**9)
        quantity = qty
        if price is not None and qty > 0:
            notional = round(qty * float(price), 2)

    intent = _draft_intent(
        alert,
        symbol=symbol,
        action=action,
        purpose=purpose,
        price=price,
        quantity=quantity,
    )
    if notional is not None:
        intent = replace(intent, estimated_notional=notional)

    allowed, reason, checks = check_handoff_prevalidation(
        intent, action=action, env=env
    )
    if not allowed:
        return _blocked(intent, reason, checks)

    # When broker state is known, still refuse duplicate open-order collisions
    # as a soft Render guard. Unknown state skips — host reconciles.
    if state.known:
        dup = recovery_block_reason(
            open_order_count=len(state.open_orders),
            proposed_client_order_id=intent.client_order_id,
            known_client_order_ids=state.known_client_order_ids,
            broker_state_known=True,
        )
        if dup:
            return _blocked(intent, dup, checks + (("Recovery", "FAIL"),))

    meta: dict[str, Any] = {
        "handoff": True,
        "auth_case": "C",
        "strategy_version": STRATEGY_VERSION,
        "pending_not_approved": True,
        "broker_state_known_at_handoff": state.known,
    }
    if env is not None and str(env.get(ENV_BOUND_ACCOUNT) or "").strip():
        try:
            pin = env_account_pin(env)
        except AccountIsolationError as exc:
            return _blocked(intent, str(exc), checks + (("Agentic account", "FAIL"),))
        if pin:
            # Unverified pin for routing hint — host must live-resolve before place.
            meta["account_number"] = pin
            meta["account_number_masked"] = mask_account_number(pin)
            meta["account_pin_unverified"] = True
    if extra_meta:
        meta.update(dict(extra_meta))

    durable = wrap_intent(intent, now=now, ttl_seconds=ttl_seconds, status="PENDING")
    created: StoreResult = store.create_pending(
        durable,
        meta=meta,
    )
    if not created.ok:
        # Failed intent insert must not consume a daily entry slot (we never
        # reserved on this path).
        return _blocked(intent, created.reason, checks)

    leg = HandoffLeg(
        intent=intent,
        risk_status="PASS",
        risk_reason=(
            "PENDING persisted for host executor (not approved; host must "
            "revalidate with known/fresh broker state before place)"
        ),
        checks=checks + (("Persisted", "PENDING"),),
        execution_status="PENDING",
        text="",
        persisted=True,
    )
    return replace(leg, text=_format_leg(leg))


def run_robinhood_host_handoff(
    alert: AlertDecision,
    position_before: PositionState | None,
    state: BrokerState,
    limits,
    *,
    store: IntentStore,
    reservation_store: Any | None = None,
    now: datetime | None = None,
    ttl_seconds: int = DEFAULT_INTENT_TTL_SECONDS,
    broker: RobinhoodAgenticBroker | None = None,
    env: Mapping[str, str] | None = None,
) -> HandoffRun:
    """Draft and persist PENDING intents. Never calls submit_order."""
    del broker  # handoff never submits; accept for API symmetry
    clock = now or datetime.now(timezone.utc)
    if clock.tzinfo is None:
        clock = clock.replace(tzinfo=timezone.utc)
    source = env

    paper_intents = build_order_intents(alert, position_before)
    legs: list[HandoffLeg] = []

    # FLIP: persist both legs with durable pair meta. Host gates entry via
    # advance_flip after exit FILLED + flat — never place entry blindly.
    alert_type = (alert.alert_type or "").upper()
    if alert_type == "FLIP":
        sell_legs = [i for i in paper_intents if i.side.lower() == "sell"]
        buy_legs = [i for i in paper_intents if i.side.lower() == "buy"]
        pair_id = new_flip_pair_id()
        exit_leg: HandoffLeg | None = None
        if sell_legs:
            s = sell_legs[0]
            exit_leg = evaluate_handoff_leg(
                alert,
                symbol=s.symbol,
                action="SELL",
                purpose=s.purpose or "flip_exit",
                state=state,
                limits=limits,
                store=store,
                reservation_store=None,
                now=clock,
                ttl_seconds=ttl_seconds,
                env=source,
                extra_meta=flip_exit_meta(pair_id=pair_id, exit_symbol=s.symbol),
            )
            legs.append(exit_leg)
        if buy_legs:
            b = buy_legs[0]
            if exit_leg is None or not exit_leg.persisted:
                draft = _draft_intent(
                    alert,
                    symbol=b.symbol,
                    action="BUY",
                    purpose=b.purpose or "flip_entry",
                    price=_price_for(state, b.symbol, "BUY"),
                    quantity=0,
                )
                legs.append(
                    _blocked(
                        draft,
                        "flip exit not persisted; entry blocked",
                        (("FLIP", "FAIL"),),
                        status="BLOCKED",
                    )
                )
            else:
                legs.append(
                    evaluate_handoff_leg(
                        alert,
                        symbol=b.symbol,
                        action="BUY",
                        purpose=b.purpose or "flip_entry",
                        state=state,
                        limits=limits,
                        store=store,
                        reservation_store=None,
                        now=clock,
                        ttl_seconds=ttl_seconds,
                        env=source,
                        extra_meta=flip_entry_meta(
                            pair_id=pair_id,
                            exit_client_order_id=exit_leg.intent.client_order_id,
                            exit_symbol=exit_leg.intent.execution_symbol,
                        ),
                    )
                )
    else:
        for paper in paper_intents:
            action = paper.side.upper()
            if action not in {"BUY", "SELL"}:
                continue
            legs.append(
                evaluate_handoff_leg(
                    alert,
                    symbol=paper.symbol,
                    action=action,
                    purpose=paper.purpose or ("entry" if action == "BUY" else "exit"),
                    state=state,
                    limits=limits,
                    store=store,
                    reservation_store=reservation_store if action == "BUY" else None,
                    now=clock,
                    ttl_seconds=ttl_seconds,
                    env=source,
                )
            )

    if not legs:
        empty = HandoffRun((), "ROBINHOOD HOST HANDOFF — no actionable legs", 0)
        return empty

    text = "\n\n".join(leg.text for leg in legs)
    pending_created = sum(1 for leg in legs if leg.persisted)
    blocked = sum(1 for leg in legs if leg.risk_status == "BLOCKED")
    return HandoffRun(
        legs=tuple(legs),
        text=text,
        submission_attempts=0,
        pending_created=pending_created,
        blocked=blocked,
    )


def run_robinhood_host_handoff_after_strategy(
    alert: AlertDecision,
    position: PositionState | None,
    *,
    dry_run: bool,
    webhook_url: str,
    data_bar_start: datetime | None,
    now: datetime,
    env: Mapping[str, str] | None = None,
    store: IntentStore | None = None,
) -> HandoffRun:
    source = dict(os.environ if env is None else env)
    if execution_broker_from_environ(source) != ROBINHOOD_HOST_HANDOFF:
        raise RuntimeError("EXECUTION_BROKER is not robinhood_host_handoff")

    # Handoff does not read Robinhood on Render (Case C). Broker state unknown
    # unless a test injects a known state via store-only path — we use unknown
    # by default so sells that need broker qty block, and buys that need
    # account block unless tests pass a store + use run_robinhood_host_handoff
    # directly with a known state.
    state = BrokerState(
        known=False,
        detail="Case C handoff: Render does not read Robinhood MCP; host must execute",
        data_bar_start=data_bar_start,
        now=now,
        order_history_complete=False,
    )
    limits = host_limits_from_env(source)
    intent_store: IntentStore = store or InMemoryIntentStore()
    # Without Supabase on Render, persistence should fail closed for production.
    # Tests inject InMemoryIntentStore. Production callers should pass AtomicIntentStore
    # or set SUPABASE_* and use intent_store_from_env.
    if store is None:
        try:
            from robinhood_intent_store import intent_store_from_env

            intent_store = intent_store_from_env(source)
        except RuntimeError:
            # Fail closed: create in-memory only for dry_run local rehearsal.
            if not dry_run:
                raise
            intent_store = InMemoryIntentStore()

    # No reservation on Render. Authoritative daily entry reservation is at the
    # host BUY boundary after claim + full risk revalidation.
    # Pass ``env=source`` so Agentic account isolation stamps/validates
    # ROBINHOOD_AGENTIC_ACCOUNT_NUMBER on every PENDING handoff leg.
    result = run_robinhood_host_handoff(
        alert,
        position,
        state,
        limits,
        store=intent_store,
        reservation_store=None,
        now=now,
        env=source,
    )
    # Unknown BrokerState may still create PENDING (not approved). Host must
    # revalidate with a known/fresh read before any place_equity_order.
    log_path = Path(source.get("ROBINHOOD_HOST_HANDOFF_LOG") or "logs/robinhood_host_handoff.jsonl")
    audit = HostAuditLog(log_path)
    for leg in result.legs:
        audit.append(leg.audit_row())
    post_host_discord(
        webhook_url,
        result.text,
        dry_run=dry_run,
        title="Robinhood host handoff",
        banner="ROBINHOOD HOST HANDOFF — NO REAL MONEY TRADED ON RENDER (CASE C)",
        color=0x6B7280,
    )
    print(result.text)
    return result
