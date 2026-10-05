"""Robinhood shadow execution.

The strategy still decides the trade. This module writes the order it would
send, runs the risk gate, and stops. It does not call ``submit_order``.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

from alpaca_paper import build_order_intents
from brokers.mode import ROBINHOOD_SHADOW, execution_broker_from_environ
from brokers.robinhood_agentic import RobinhoodAgenticBroker
from brokers.types import (
    EXECUTION_NOT_SUBMITTED,
    BrokerState,
    QuoteView,
    RobinhoodLimits,
    TradeIntent,
)
from robinhood_audit import ShadowAuditLog
from robinhood_flip import advance_flip, recovery_block_reason
from robinhood_risk import (
    action_block_reason,
    check_new_exposure,
    execution_symbol_block_reason,
    limits_from_env,
    size_buy,
)
from strategy_params import STRATEGY_VERSION
from strategy_types import AlertDecision, PositionState
from trade_log import extract_regime

_SIGNAL_SYMBOL = "QQQ"


@dataclass(frozen=True)
class ShadowLeg:
    intent: TradeIntent
    risk_status: str
    risk_reason: str
    checks: tuple[tuple[str, str], ...]
    execution_status: str
    text: str

    def audit_row(self, *, broker_state: str) -> dict[str, Any]:
        row = self.intent.as_audit()
        row.update(
            {
                "broker": "robinhood",
                "execution_mode": "shadow",
                "risk_status": self.risk_status,
                "risk_reason": self.risk_reason,
                "broker_state": broker_state,
                "execution_status": EXECUTION_NOT_SUBMITTED,
            }
        )
        return row


@dataclass(frozen=True)
class ShadowRun:
    legs: tuple[ShadowLeg, ...]
    text: str
    submission_attempts: int

    @property
    def submitted(self) -> bool:
        return self.submission_attempts != 0 or any(
            leg.execution_status != EXECUTION_NOT_SUBMITTED for leg in self.legs
        )


def shadow_client_order_id(
    *,
    strategy_version: str,
    timestamp: str,
    symbol: str,
    action: str,
    purpose: str,
) -> str:
    raw = "|".join(
        (
            strategy_version.strip(),
            timestamp.strip(),
            symbol.strip().upper(),
            action.strip().upper(),
            purpose.strip(),
        )
    )
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]
    return f"rh{digest}"


def _draft_intent(
    alert: AlertDecision,
    *,
    symbol: str,
    action: str,
    purpose: str,
    price: float | None,
    quantity: int,
) -> TradeIntent:
    regime = extract_regime(alert.qqq_trend_reason) or ""
    notional = round(quantity * price, 2) if price is not None and quantity else None
    return TradeIntent(
        strategy_version=STRATEGY_VERSION,
        signal_symbol=_SIGNAL_SYMBOL,
        execution_symbol=symbol.upper(),
        action=action,
        quantity=quantity,
        estimated_price=price,
        estimated_notional=notional,
        confidence=int(alert.confidence_score),
        regime=regime,
        reason=alert.notes or alert.qqq_trend_reason,
        signal_id=f"{alert.timestamp}|{alert.alert_type}|{symbol.upper()}|{purpose}",
        timestamp=alert.timestamp,
        purpose=purpose,
        client_order_id=shadow_client_order_id(
            strategy_version=STRATEGY_VERSION,
            timestamp=alert.timestamp,
            symbol=symbol,
            action=action,
            purpose=purpose,
        ),
    )


def _price_for(action: str, quote: QuoteView | None, symbol: str) -> float | None:
    if quote is None or quote.symbol.upper() != symbol.upper():
        return None
    if action == "BUY":
        return float(quote.ask) if quote.ask is not None else None
    if quote.bid is not None:
        return float(quote.bid)
    if quote.last is not None:
        return float(quote.last)
    return None


def _format_leg(leg: ShadowLeg, *, live_flag: bool) -> str:
    price = leg.intent.estimated_price
    notional = leg.intent.estimated_notional
    price_txt = f"${price:.2f}" if price is not None else "n/a"
    notional_txt = f"${notional:.2f}" if notional is not None else "n/a"
    check_lines = "\n".join(f"{name}: {status}" for name, status in leg.checks) or "none"
    return "\n".join(
        (
            "ROBINHOOD SHADOW TRADE",
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
            "SHADOW MODE",
            "NO REAL MONEY TRADED",
            f"ROBINHOOD_LIVE_ENABLED flag: {str(live_flag).lower()} (ignored)",
            "execution_status: NOT_SUBMITTED",
        )
    )


def _blocked_leg(
    intent: TradeIntent,
    reason: str,
    checks: tuple[tuple[str, str], ...] = (),
) -> ShadowLeg:
    leg = ShadowLeg(
        intent=intent,
        risk_status="BLOCKED",
        risk_reason=reason,
        checks=checks,
        execution_status=EXECUTION_NOT_SUBMITTED,
        text="",
    )
    return replace(leg, text="")


def evaluate_leg(
    alert: AlertDecision,
    *,
    symbol: str,
    action: str,
    purpose: str,
    state: BrokerState,
    limits: RobinhoodLimits,
    exit_status: str | None = None,
    exit_qty_remaining: float | None = None,
) -> ShadowLeg:
    symbol_reason = execution_symbol_block_reason(symbol)
    action_reason = action_block_reason(action)
    price = _price_for(action, state.quote, symbol)
    if symbol_reason or action_reason:
        intent = _draft_intent(
            alert,
            symbol=symbol or "INVALID",
            action=action or "INVALID",
            purpose=purpose,
            price=price,
            quantity=0,
        )
        reason = symbol_reason or action_reason or "rejected"
        checks: tuple[tuple[str, str], ...] = (
            ("Symbol", "FAIL" if symbol_reason else "PASS"),
            ("Action", "FAIL" if action_reason else "PASS"),
        )
        leg = _blocked_leg(intent, reason, checks)
        return replace(leg, text=_format_leg(leg, live_flag=limits.live_enabled_flag))

    recovery = recovery_block_reason(
        open_order_count=len(state.open_orders),
        proposed_client_order_id=shadow_client_order_id(
            strategy_version=STRATEGY_VERSION,
            timestamp=alert.timestamp,
            symbol=symbol,
            action=action,
            purpose=purpose,
        ),
        known_client_order_ids=state.known_client_order_ids,
        broker_state_known=state.known,
    )

    if purpose == "flip_entry":
        flip = advance_flip(
            exit_status=exit_status,
            exit_qty_remaining=exit_qty_remaining,
            broker_state_known=state.known,
        )
        if not flip.entry_validation_allowed:
            intent = _draft_intent(
                alert, symbol=symbol, action="BUY", purpose=purpose, price=price, quantity=0
            )
            leg = _blocked_leg(intent, flip.reason, (("Flip exit", "FAIL"),))
            return replace(leg, text=_format_leg(leg, live_flag=limits.live_enabled_flag))

    if action == "SELL":
        held = 0.0
        if state.known:
            held = sum(row.qty for row in state.positions if row.symbol.upper() == symbol.upper())
        if not state.known:
            intent = _draft_intent(
                alert, symbol=symbol, action="SELL", purpose=purpose, price=price, quantity=0
            )
            leg = _blocked_leg(intent, "unknown broker state; sell not sized", (("Broker state", "FAIL"),))
            return replace(leg, text=_format_leg(leg, live_flag=limits.live_enabled_flag))
        if held <= 0:
            intent = _draft_intent(
                alert, symbol=symbol, action="SELL", purpose=purpose, price=price, quantity=0
            )
            leg = _blocked_leg(intent, "no position to sell", (("Position", "FAIL"),))
            return replace(leg, text=_format_leg(leg, live_flag=limits.live_enabled_flag))
        quantity = int(held) if float(held).is_integer() else 0
        if quantity < 1:
            intent = _draft_intent(
                alert, symbol=symbol, action="SELL", purpose=purpose, price=price, quantity=0
            )
            leg = _blocked_leg(intent, "fractional residual is not a whole-share sell", (("Position", "FAIL"),))
            return replace(leg, text=_format_leg(leg, live_flag=limits.live_enabled_flag))
        if recovery:
            intent = _draft_intent(
                alert, symbol=symbol, action="SELL", purpose=purpose, price=price, quantity=quantity
            )
            leg = _blocked_leg(intent, recovery, (("Recovery", "FAIL"),))
            return replace(leg, text=_format_leg(leg, live_flag=limits.live_enabled_flag))
        intent = _draft_intent(
            alert, symbol=symbol, action="SELL", purpose=purpose, price=price, quantity=quantity
        )
        leg = ShadowLeg(
            intent=intent,
            risk_status="PASS",
            risk_reason="risk-reducing sell sized from the broker position; not submitted",
            checks=(("Symbol", "PASS"), ("Action", "PASS"), ("Position", "PASS")),
            execution_status=EXECUTION_NOT_SUBMITTED,
            text="",
        )
        return replace(leg, text=_format_leg(leg, live_flag=limits.live_enabled_flag))

    draft = _draft_intent(
        alert, symbol=symbol, action="BUY", purpose=purpose, price=price, quantity=0
    )
    if recovery:
        leg = _blocked_leg(draft, recovery, (("Recovery", "FAIL"),))
        return replace(leg, text=_format_leg(leg, live_flag=limits.live_enabled_flag))
    allowed, reason, checks = check_new_exposure(draft, state, limits)
    if not allowed:
        leg = _blocked_leg(draft, reason, checks)
        return replace(leg, text=_format_leg(leg, live_flag=limits.live_enabled_flag))
    quantity, notional = size_buy(float(price or 0), state, limits)
    intent = replace(draft, quantity=quantity, estimated_price=price, estimated_notional=notional)
    leg = ShadowLeg(
        intent=intent,
        risk_status="PASS",
        risk_reason=reason,
        checks=checks,
        execution_status=EXECUTION_NOT_SUBMITTED,
        text="",
    )
    return replace(leg, text=_format_leg(leg, live_flag=limits.live_enabled_flag))


def run_robinhood_shadow(
    alert: AlertDecision,
    position_before: PositionState | None,
    state: BrokerState,
    limits: RobinhoodLimits,
    *,
    exit_status: str | None = None,
    exit_qty_remaining: float | None = None,
    broker: RobinhoodAgenticBroker | None = None,
) -> ShadowRun:
    """Plan shadow legs. ``broker.submit_order`` is never called."""
    adapter = broker or RobinhoodAgenticBroker(state)
    before = adapter.submission_attempts
    alert_type = (alert.alert_type or "").upper()
    symbol = (alert.symbol or "").upper()
    order_intents = build_order_intents(alert, position_before)
    legs: list[ShadowLeg] = []
    if alert_type in {"BUY", "SELL", "FLIP"} and not order_intents:
        legs.append(
            evaluate_leg(
                alert,
                symbol=symbol,
                action="BUY" if alert_type != "SELL" else "SELL",
                purpose="rejected",
                state=state,
                limits=limits,
            )
        )
    for intent in order_intents:
        action = "BUY" if intent.side == "buy" else "SELL"
        legs.append(
            evaluate_leg(
                alert,
                symbol=intent.symbol,
                action=action,
                purpose=intent.purpose,
                state=state,
                limits=limits,
                exit_status=exit_status if intent.purpose == "flip_entry" else None,
                exit_qty_remaining=exit_qty_remaining if intent.purpose == "flip_entry" else None,
            )
        )
    if adapter.submission_attempts != before:
        raise RuntimeError("Robinhood shadow called submit_order.")
    text = "\n\n".join(leg.text for leg in legs) if legs else "ROBINHOOD SHADOW\n\nNo order. NO REAL MONEY TRADED"
    return ShadowRun(legs=tuple(legs), text=text, submission_attempts=adapter.submission_attempts)


def _post_discord(webhook_url: str, text: str, *, dry_run: bool) -> None:
    payload = {
        "username": "QQQ Swing Alerts",
        "content": "ROBINHOOD SHADOW — NO REAL MONEY TRADED",
        "embeds": [
            {
                "title": "Robinhood shadow",
                "description": text[:3900],
                "color": 0x6B7280,
            }
        ],
    }
    if dry_run or not webhook_url:
        return
    try:
        response = requests.post(webhook_url, json=payload, timeout=15)
        if response.status_code >= 400:
            print(f"[robinhood-shadow] Discord failed: {response.status_code}")
    except requests.RequestException as exc:
        print(f"[robinhood-shadow] Discord error: {exc}")


def run_live_shadow_after_strategy(
    alert: AlertDecision,
    position_before: PositionState | None,
    *,
    dry_run: bool,
    webhook_url: str,
    data_bar_start: datetime | None,
    now: datetime | None,
    env: dict[str, str] | None = None,
) -> ShadowRun | None:
    """Hook used by main. Unknown Robinhood state blocks new buys. Never submits."""
    source = os.environ if env is None else env
    if execution_broker_from_environ(source) != ROBINHOOD_SHADOW:
        return None
    state = BrokerState(
        known=False,
        detail="Robinhood account was not read; shadow blocks new exposure",
        data_bar_start=data_bar_start,
        now=now or datetime.now(timezone.utc),
    )
    limits = limits_from_env(source)
    result = run_robinhood_shadow(alert, position_before, state, limits)
    log_path = Path(source.get("ROBINHOOD_SHADOW_LOG", "logs/robinhood_shadow.jsonl"))
    audit = ShadowAuditLog(log_path)
    for leg in result.legs:
        audit.append(leg.audit_row(broker_state="unknown"))
    _post_discord(webhook_url, result.text, dry_run=dry_run)
    print(result.text)
    return result
