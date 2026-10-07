"""Alpaca Live Shadow: real live reads, realistic intent, no submission.

FLIP proposes SELL as NOT_SUBMITTED and blocks the second leg. Env flags
cannot unlock live writes.
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

from alpaca_live_audit import LiveShadowAuditLog
from alpaca_live_claim import LiveShadowClaimStore
from alpaca_live_credentials import load_live_credentials
from alpaca_live_reconcile import reconcile_live_books
from alpaca_live_risk import (
    action_block_reason,
    check_new_exposure,
    execution_symbol_block_reason,
    live_limits_from_env,
    quote_for,
    size_buy,
    AlpacaLiveLimits,
)
from alpaca_paper import build_order_intents
from brokers.alpaca_live_broker import ALPACA_LIVE_SUBMISSION_IMPLEMENTED, AlpacaLiveBroker
from brokers.alpaca_live_normalize import normalize_snapshot
from brokers.alpaca_live_reader import (
    AlpacaLiveReadClient,
    AlpacaLiveReadError,
    AlpacaLiveWriteRejected,
    client_from_credentials,
)
from brokers.mode import ALPACA_LIVE_SHADOW, execution_broker_from_environ
from brokers.types import EXECUTION_NOT_SUBMITTED, BrokerState, QuoteView, TradeIntent
from robinhood_audit import redact
from robinhood_flip import advance_flip, recovery_block_reason
from strategy_params import STRATEGY_VERSION
from strategy_types import AlertDecision, PositionState
from trade_log import extract_regime

_SIGNAL_SYMBOL = "QQQ"


@dataclass(frozen=True)
class LiveShadowLeg:
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
                "broker": "alpaca",
                "execution_mode": "live_shadow",
                "risk_status": self.risk_status,
                "risk_reason": self.risk_reason,
                "broker_state": broker_state,
                "execution_status": EXECUTION_NOT_SUBMITTED,
            }
        )
        return row


@dataclass(frozen=True)
class LiveShadowRun:
    legs: tuple[LiveShadowLeg, ...]
    text: str
    submission_attempts: int
    write_rejects: int = 0
    proposed: int = 0
    blocked: int = 0

    @property
    def submitted(self) -> bool:
        return self.submission_attempts != 0 or any(
            leg.execution_status != EXECUTION_NOT_SUBMITTED for leg in self.legs
        )


def live_shadow_client_order_id(
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
    return f"al{digest}"


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
        client_order_id=live_shadow_client_order_id(
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
    return None


def _format_leg(leg: LiveShadowLeg, *, live_flag: bool) -> str:
    price = leg.intent.estimated_price
    notional = leg.intent.estimated_notional
    price_txt = f"${price:.2f}" if price is not None else "n/a"
    notional_txt = f"${notional:.2f}" if notional is not None else "n/a"
    check_lines = "\n".join(f"{name}: {status}" for name, status in leg.checks) or "none"
    return "\n".join(
        (
            "ALPACA LIVE SHADOW TRADE",
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
            "LIVE SHADOW MODE",
            "NO REAL MONEY TRADED",
            f"ALPACA_LIVE_ENABLED flag: {str(live_flag).lower()} (ignored)",
            f"ALPACA_LIVE_SUBMISSION_IMPLEMENTED: {ALPACA_LIVE_SUBMISSION_IMPLEMENTED}",
            "execution_status: NOT_SUBMITTED",
        )
    )


def _blocked_leg(
    intent: TradeIntent,
    reason: str,
    checks: tuple[tuple[str, str], ...] = (),
) -> LiveShadowLeg:
    return LiveShadowLeg(
        intent=intent,
        risk_status="BLOCKED",
        risk_reason=reason,
        checks=checks,
        execution_status=EXECUTION_NOT_SUBMITTED,
        text="",
    )


def evaluate_live_leg(
    alert: AlertDecision,
    *,
    symbol: str,
    action: str,
    purpose: str,
    state: BrokerState,
    limits: AlpacaLiveLimits,
    exit_status: str | None = None,
    exit_qty_remaining: float | None = None,
    buy_block_reason: str | None = None,
    claim_store: LiveShadowClaimStore | None = None,
) -> LiveShadowLeg:
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
        reason = symbol_reason or action_reason or "rejected"
        checks: tuple[tuple[str, str], ...] = (
            ("Symbol", "FAIL" if symbol_reason else "PASS"),
            ("Action", "FAIL" if action_reason else "PASS"),
        )
        leg = _blocked_leg(intent, reason, checks)
        return replace(leg, text=_format_leg(leg, live_flag=limits.live_enabled_flag))

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
        if claim_store is not None:
            claim = claim_store.claim(
                intent.client_order_id,
                meta={"action": "SELL", "symbol": symbol, "purpose": purpose},
            )
            if not claim.claimed:
                leg = _blocked_leg(intent, claim.reason, (("Claim", "FAIL"),))
                return replace(leg, text=_format_leg(leg, live_flag=limits.live_enabled_flag))
        leg = LiveShadowLeg(
            intent=intent,
            risk_status="PASS",
            risk_reason="risk-reducing sell sized from the broker position; not submitted",
            checks=(("Symbol", "PASS"), ("Action", "PASS"), ("Position", "PASS")),
            execution_status=EXECUTION_NOT_SUBMITTED,
            text="",
        )
        return replace(leg, text=_format_leg(leg, live_flag=limits.live_enabled_flag))

    if buy_block_reason:
        intent = _draft_intent(
            alert, symbol=symbol, action="BUY", purpose=purpose, price=price, quantity=0
        )
        leg = _blocked_leg(intent, buy_block_reason, (("Reconcile", "FAIL"),))
        return replace(leg, text=_format_leg(leg, live_flag=limits.live_enabled_flag))

    draft = _draft_intent(
        alert, symbol=symbol, action="BUY", purpose=purpose, price=price, quantity=0
    )
    if recovery:
        reason = buy_block_reason if buy_block_reason and not state.known else recovery
        leg = _blocked_leg(draft, reason, (("Recovery", "FAIL"),))
        return replace(leg, text=_format_leg(leg, live_flag=limits.live_enabled_flag))
    allowed, reason, checks = check_new_exposure(draft, state, limits)
    if not allowed:
        leg = _blocked_leg(draft, reason, checks)
        return replace(leg, text=_format_leg(leg, live_flag=limits.live_enabled_flag))
    quantity, notional = size_buy(float(price or 0), state, limits)
    intent = replace(draft, quantity=quantity, estimated_price=price, estimated_notional=notional)
    if claim_store is not None:
        claim = claim_store.claim(
            intent.client_order_id,
            meta={"action": "BUY", "symbol": symbol, "purpose": purpose},
        )
        if not claim.claimed:
            leg = _blocked_leg(intent, claim.reason, checks + (("Claim", "FAIL"),))
            return replace(leg, text=_format_leg(leg, live_flag=limits.live_enabled_flag))
    leg = LiveShadowLeg(
        intent=intent,
        risk_status="PASS",
        risk_reason=reason,
        checks=checks,
        execution_status=EXECUTION_NOT_SUBMITTED,
        text="",
    )
    return replace(leg, text=_format_leg(leg, live_flag=limits.live_enabled_flag))


def run_alpaca_live_shadow(
    alert: AlertDecision,
    position_before: PositionState | None,
    state: BrokerState,
    limits: AlpacaLiveLimits,
    *,
    exit_status: str | None = None,
    exit_qty_remaining: float | None = None,
    broker: AlpacaLiveBroker | None = None,
    buy_block_reason: str | None = None,
    claim_store: LiveShadowClaimStore | None = None,
) -> LiveShadowRun:
    """Plan live-shadow legs. ``broker.submit_order`` is never called."""
    adapter = broker or AlpacaLiveBroker(state)
    before = adapter.submission_attempts
    alert_type = (alert.alert_type or "").upper()
    symbol = (alert.symbol or "").upper()
    order_intents = build_order_intents(alert, position_before)
    legs: list[LiveShadowLeg] = []
    if alert_type in {"BUY", "SELL", "FLIP"} and not order_intents:
        legs.append(
            evaluate_live_leg(
                alert,
                symbol=symbol,
                action="BUY" if alert_type != "SELL" else "SELL",
                purpose="rejected",
                state=state,
                limits=limits,
                buy_block_reason=buy_block_reason,
                claim_store=claim_store,
            )
        )
    for intent in order_intents:
        action = "BUY" if intent.side == "buy" else "SELL"
        legs.append(
            evaluate_live_leg(
                alert,
                symbol=intent.symbol,
                action=action,
                purpose=intent.purpose,
                state=state,
                limits=limits,
                exit_status=exit_status if intent.purpose == "flip_entry" else None,
                exit_qty_remaining=exit_qty_remaining if intent.purpose == "flip_entry" else None,
                buy_block_reason=buy_block_reason,
                claim_store=claim_store,
            )
        )
    if adapter.submission_attempts != before:
        raise RuntimeError("Alpaca live shadow called submit_order.")
    proposed = sum(1 for leg in legs if leg.risk_status == "PASS")
    blocked = sum(1 for leg in legs if leg.risk_status == "BLOCKED")
    text = (
        "\n\n".join(leg.text for leg in legs)
        if legs
        else "ALPACA LIVE SHADOW\n\nNo order. NO REAL MONEY TRADED"
    )
    text += f"\n\nCounters: proposed={proposed} blocked={blocked} write_rejects=0"
    return LiveShadowRun(
        legs=tuple(legs),
        text=text,
        submission_attempts=adapter.submission_attempts,
        proposed=proposed,
        blocked=blocked,
    )


def _position_from_broker(state: BrokerState, local: PositionState | None) -> PositionState | None:
    held = [
        row.symbol.upper()
        for row in state.positions
        if row.symbol.upper() in {"TQQQ", "SQQQ"} and row.qty > 0
    ]
    unique = sorted(set(held))
    if len(unique) == 1:
        return PositionState(active_symbol=unique[0])
    return local


def _unknown(detail: str, *, data_bar_start: datetime | None, now: datetime) -> BrokerState:
    return BrokerState(
        known=False,
        detail=detail,
        data_bar_start=data_bar_start,
        now=now,
        order_history_complete=False,
    )


def load_live_state(
    client: AlpacaLiveReadClient | None,
    *,
    now: datetime,
    data_bar_start: datetime | None,
) -> BrokerState:
    if client is None:
        return _unknown(
            "alpaca live reader is not configured",
            data_bar_start=data_bar_start,
            now=now,
        )
    try:
        raw = client.read_snapshot()
        return normalize_snapshot(raw, now=now, data_bar_start=data_bar_start)
    except (AlpacaLiveReadError, AlpacaLiveWriteRejected, ValueError, TypeError, KeyError):
        return _unknown(
            "Alpaca Live read failed",
            data_bar_start=data_bar_start,
            now=now,
        )


def _post_discord(webhook_url: str, text: str, *, dry_run: bool) -> None:
    payload = redact(
        {
            "username": "QQQ Swing Alerts",
            "content": "ALPACA LIVE SHADOW — NO REAL MONEY TRADED",
            "embeds": [
                {
                    "title": "Alpaca live shadow",
                    "description": text[:3900],
                    "color": 0x6B7280,
                }
            ],
        }
    )
    if dry_run or not webhook_url:
        return
    try:
        response = requests.post(webhook_url, json=payload, timeout=15)
        if getattr(response, "status_code", 0) >= 400:
            print(f"[alpaca-live-shadow] Discord failed: {response.status_code}")
    except requests.RequestException:
        print("[alpaca-live-shadow] Discord error")


def run_alpaca_live_shadow_after_strategy(
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
) -> LiveShadowRun | None:
    """Hook used by main. Reads live account when configured. Never submits."""
    source = os.environ if env is None else env
    if execution_broker_from_environ(source) != ALPACA_LIVE_SHADOW:
        return None
    clock = now or datetime.now(timezone.utc)
    reader = client
    if reader is None and transport is not None:
        try:
            creds = load_live_credentials(source)
            reader = client_from_credentials(creds, transport=transport)
        except Exception:  # noqa: BLE001
            reader = None
    if reader is None:
        try:
            creds = load_live_credentials(source)
            reader = client_from_credentials(creds)
        except Exception:  # noqa: BLE001
            reader = None
    state = load_live_state(reader, now=clock, data_bar_start=data_bar_start)
    limits = live_limits_from_env(dict(source))
    log_path = Path(source.get("ALPACA_LIVE_SHADOW_LOG", "logs/alpaca_live_shadow.jsonl"))
    claim_path = Path(source.get("ALPACA_LIVE_CLAIM_LOG", "logs/alpaca_live_claims.jsonl"))
    audit = LiveShadowAuditLog(log_path)
    claims = LiveShadowClaimStore(claim_path)
    known_ids = set(state.known_client_order_ids) | claims.known_ids()
    for row in audit.read():
        client_id = row.get("client_order_id")
        if isinstance(client_id, str) and client_id:
            known_ids.add(client_id)
    # Count prior claims today for order-cap when broker does not provide it.
    orders_today = sum(
        1
        for row in audit.read()
        if str(row.get("timestamp") or "").startswith(clock.date().isoformat())
    )
    state = replace(
        state,
        known_client_order_ids=frozenset(known_ids),
        orders_today=max(state.orders_today, orders_today),
    )
    book = reconcile_live_books(position_before, state)
    buy_block = book.reason if book.blocks_new_exposure else None
    intent_position = (
        _position_from_broker(state, position_before) if state.known else position_before
    )
    result = run_alpaca_live_shadow(
        alert,
        intent_position,
        state,
        limits,
        buy_block_reason=buy_block,
        claim_store=claims,
    )
    if any(leg.execution_status != EXECUTION_NOT_SUBMITTED for leg in result.legs):
        raise RuntimeError("live shadow produced a status other than NOT_SUBMITTED")
    if result.submission_attempts != 0:
        raise RuntimeError("live shadow attempted a broker submission")
    broker_state = "unknown" if not state.known else ("mismatch" if buy_block else "known")
    for leg in result.legs:
        audit.append(leg.audit_row(broker_state=broker_state))
    _post_discord(webhook_url, result.text, dry_run=dry_run)
    print(result.text)
    return result
