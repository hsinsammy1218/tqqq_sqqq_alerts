"""Authenticated MCP-host executor for pending Robinhood intents.

Only runs when ROBINHOOD_HOST_EXECUTOR=true and a host transport is injected.
Case C: Render must never call this with a live transport. Static bearer env
vars are not used. Tests inject a fake transport; production host supplies MCP.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from brokers.robinhood_normalize import normalize_snapshot
from brokers.robinhood_reader import (
    ALLOWED_READS,
    RobinhoodReadClient,
    RobinhoodReadError,
    RobinhoodToolRejected,
    assert_read_only,
)
from brokers.types import (
    ORDER_FILLED,
    ORDER_PARTIALLY_FILLED,
    ORDER_UNKNOWN,
    BrokerState,
    TradeIntent,
)
from robinhood_flip import advance_flip
from robinhood_host_audit import HostAuditLog, post_host_discord
from robinhood_host_risk import (
    check_host_new_exposure,
    host_arming_block_reason,
    host_limits_from_env,
    quote_for,
    sell_qty_allowed,
    size_host_buy,
)
from robinhood_intent import intent_from_row, verify_integrity
from robinhood_intent_store import IntentStore, StoreResult
from robinhood_reconcile import reconcile_books
from strategy_types import PositionState

# Host submission path exists in this module. Unattended Render still cannot
# arm it (host_arming_block_reason + require injected transport).
ROBINHOOD_HOST_SUBMISSION_IMPLEMENTED = True

WRITE_TOOLS = frozenset(
    {
        "place_equity_order",
        "review_equity_order",
        "cancel_equity_order",
    }
)

HostTransport = Callable[[str, dict[str, Any]], Any]


class HostTransportError(Exception):
    """Host MCP transport failed."""


class HostOrderAmbiguous(Exception):
    """Place result is UNKNOWN — do not resubmit."""


@dataclass(frozen=True)
class HostSubmitResult:
    status: str
    broker_order_id: str | None
    filled_qty: float | None
    detail: str
    place_attempts: int


@dataclass(frozen=True)
class HostExecLeg:
    intent: TradeIntent
    risk_status: str
    risk_reason: str
    checks: tuple[tuple[str, str], ...]
    execution_status: str
    text: str
    submit: HostSubmitResult | None = None

    def audit_row(self) -> dict[str, Any]:
        row = self.intent.as_audit()
        row.update(
            {
                "broker": "robinhood",
                "execution_mode": "host_mediated",
                "risk_status": self.risk_status,
                "risk_reason": self.risk_reason,
                "execution_status": self.execution_status,
                "real_money": self.execution_status
                in {"SUBMITTED", "FILLED", "PARTIALLY_FILLED", "UNKNOWN"},
                "auth_case": "C",
            }
        )
        if self.submit is not None:
            row["broker_order_id"] = self.submit.broker_order_id
            row["filled_qty"] = self.submit.filled_qty
            row["submit_detail"] = self.submit.detail
            row["place_attempts"] = self.submit.place_attempts
        return row


@dataclass(frozen=True)
class HostExecRun:
    legs: tuple[HostExecLeg, ...]
    text: str
    place_attempts: int
    armed: bool


def assert_host_tool(name: str, *, allow_writes: bool) -> str:
    tool = (name or "").strip()
    if tool in ALLOWED_READS:
        return assert_read_only(tool)
    if allow_writes and tool in WRITE_TOOLS:
        return tool
    raise RobinhoodToolRejected(f"refusing Robinhood tool {tool!r} on host executor")


class HostMediatedClient:
    """Read + optional write through an injected authenticated host transport."""

    def __init__(self, transport: HostTransport, *, allow_writes: bool) -> None:
        self._transport = transport
        self.allow_writes = allow_writes
        self.invocations: list[str] = []
        self.place_attempts = 0

    def call(self, name: str, arguments: dict[str, Any] | None = None) -> Any:
        tool = assert_host_tool(name, allow_writes=self.allow_writes)
        self.invocations.append(tool)
        if tool == "place_equity_order":
            self.place_attempts += 1
        try:
            return self._transport(tool, dict(arguments or {}))
        except (RobinhoodToolRejected, RobinhoodReadError, HostTransportError, HostOrderAmbiguous):
            raise
        except Exception as exc:  # noqa: BLE001
            raise HostTransportError(f"{tool} failed: {exc}") from None

    def read_snapshot(self, symbols: tuple[str, ...] = ("TQQQ", "SQQQ")) -> dict[str, Any]:
        reader = RobinhoodReadClient(self.call)
        return reader.read_snapshot(symbols)


def place_args_for_intent(intent: TradeIntent) -> dict[str, Any]:
    """Conservative place/review args. Confirm against live tools/list before real use.

    Official docs list market share-based and dollar-based orders. Client order
    id field is not published — we pass ``client_order_id`` when present and
    always reconcile-before-retry on UNKNOWN.
    """
    side = "buy" if intent.action.upper() == "BUY" else "sell"
    args: dict[str, Any] = {
        "symbol": intent.execution_symbol.upper(),
        "side": side,
        "order_type": "market",
        "time_in_force": "gfd",
        "client_order_id": intent.client_order_id,
    }
    if side == "sell":
        args["quantity"] = int(intent.quantity)
        # Prefer quantity flatten; sell_all is an alternate documented shape.
        if intent.quantity <= 0:
            args.pop("quantity", None)
            args["sell_all"] = True
    else:
        args["quantity"] = int(intent.quantity)
    return args


def _normalize_place_status(payload: Any) -> HostSubmitResult:
    if not isinstance(payload, dict):
        raise HostOrderAmbiguous("place_equity_order returned non-object; marked UNKNOWN")
    status = str(
        payload.get("status")
        or payload.get("order_status")
        or payload.get("state")
        or ""
    ).strip().upper()
    order_id = payload.get("id") or payload.get("order_id") or payload.get("broker_order_id")
    filled = payload.get("filled_qty") or payload.get("cumulative_quantity")
    try:
        filled_f = float(filled) if filled is not None else None
    except (TypeError, ValueError):
        filled_f = None
    if status in {"", "UNKNOWN"}:
        raise HostOrderAmbiguous("place status unknown; do not resubmit")
    if status in {"FILLED", "COMPLETE", "COMPLETED"}:
        return HostSubmitResult(ORDER_FILLED, str(order_id) if order_id else None, filled_f, "filled", 1)
    if status in {"PARTIALLY_FILLED", "PARTIAL"}:
        return HostSubmitResult(
            ORDER_PARTIALLY_FILLED, str(order_id) if order_id else None, filled_f, "partial", 1
        )
    if status in {"REJECTED", "CANCELLED", "CANCELED", "EXPIRED"}:
        return HostSubmitResult(status.replace("CANCELED", "CANCELLED"), str(order_id) if order_id else None, filled_f, status.lower(), 1)
    if status in {"SUBMITTED", "ACCEPTED", "NEW", "PENDING", "QUEUED", "CONFIRMED"}:
        return HostSubmitResult("SUBMITTED", str(order_id) if order_id else None, filled_f, "accepted", 1)
    raise HostOrderAmbiguous(f"unrecognized place status {status!r}; do not resubmit")


def _format_leg(leg: HostExecLeg, *, armed: bool) -> str:
    price = leg.intent.estimated_price
    price_txt = f"${price:.2f}" if price is not None else "n/a"
    check_lines = "\n".join(f"{name}: {status}" for name, status in leg.checks) or "none"
    return "\n".join(
        (
            "ROBINHOOD HOST EXECUTOR — REAL MONEY WHEN SUBMITTED",
            "",
            f"Strategy: {leg.intent.strategy_version}",
            f"Action: {leg.intent.action} {leg.intent.execution_symbol}",
            f"Quantity: {leg.intent.quantity}",
            f"Estimated Price: {price_txt}",
            f"Client order id: {leg.intent.client_order_id}",
            "",
            "Risk Checks:",
            check_lines,
            f"Risk: {leg.risk_status} — {leg.risk_reason}",
            "",
            f"armed: {str(armed).lower()}",
            f"ROBINHOOD_HOST_SUBMISSION_IMPLEMENTED: {ROBINHOOD_HOST_SUBMISSION_IMPLEMENTED}",
            f"execution_status: {leg.execution_status}",
        )
    )


def _blocked(intent: TradeIntent, reason: str, checks: tuple[tuple[str, str], ...] = (), *, armed: bool, status: str = "BLOCKED") -> HostExecLeg:
    leg = HostExecLeg(
        intent=intent,
        risk_status="BLOCKED",
        risk_reason=reason,
        checks=checks,
        execution_status=status,
        text="",
    )
    return replace(leg, text=_format_leg(leg, armed=armed))


def load_host_state(
    client: HostMediatedClient,
    *,
    now: datetime,
    data_bar_start: datetime | None,
) -> BrokerState:
    try:
        raw = client.read_snapshot()
        return normalize_snapshot(raw, now=now, data_bar_start=data_bar_start)
    except (RobinhoodReadError, RobinhoodToolRejected, HostTransportError, ValueError, TypeError, KeyError):
        return BrokerState(
            known=False,
            detail="host Robinhood read failed",
            data_bar_start=data_bar_start,
            now=now,
            order_history_complete=False,
        )


def reconcile_before_retry(
    state: BrokerState,
    client_order_id: str,
) -> HostSubmitResult | None:
    """If an open/history order already matches client_order_id, do not place again."""
    for order in state.open_orders:
        if order.client_order_id == client_order_id:
            status = order.status.upper()
            if status == ORDER_UNKNOWN:
                raise HostOrderAmbiguous("existing order status UNKNOWN; no resubmit")
            return HostSubmitResult(
                status if status else ORDER_UNKNOWN,
                order.client_order_id,
                None,
                "reconciled existing order; skipped place",
                0,
            )
    if client_order_id in state.known_client_order_ids:
        raise HostOrderAmbiguous("client_order_id already known; no resubmit")
    return None


def execute_claimed_intent(
    row: dict[str, Any],
    *,
    client: HostMediatedClient,
    store: IntentStore,
    limits,
    env: Mapping[str, str],
    now: datetime,
    data_bar_start: datetime | None,
    position_before: PositionState | None = None,
) -> HostExecLeg:
    armed_reason = host_arming_block_reason(
        env,
        submission_implemented=ROBINHOOD_HOST_SUBMISSION_IMPLEMENTED,
        host_executor=True,
    )
    armed = armed_reason is None
    durable = intent_from_row(row)
    intent = durable.intent
    if not verify_integrity(intent, durable.integrity_digest):
        store.update_status(
            intent.client_order_id,
            from_statuses={"CLAIMED"},
            to_status="BLOCKED",
            fields={"detail": "integrity_digest mismatch"},
        )
        return _blocked(intent, "integrity_digest mismatch", armed=armed)

    if durable.is_expired(now):
        store.update_status(
            intent.client_order_id,
            from_statuses={"CLAIMED"},
            to_status="EXPIRED_INTENT",
            fields={"detail": "expired after claim"},
        )
        return _blocked(intent, "intent expired after claim", armed=armed, status="EXPIRED_INTENT")

    state = load_host_state(client, now=now, data_bar_start=data_bar_start)
    book = reconcile_books(position_before, state)
    if book.blocks_new_exposure and intent.action == "BUY":
        store.update_status(
            intent.client_order_id,
            from_statuses={"CLAIMED"},
            to_status="BLOCKED",
            fields={"detail": book.reason},
        )
        return _blocked(intent, book.reason, (("Reconcile", "FAIL"),), armed=armed)

    if intent.action == "BUY":
        # Re-size at execution time; refuse if stored qty exceeds fresh size.
        allowed, reason, checks = check_host_new_exposure(intent, state, limits)
        if allowed:
            quote = quote_for(state, intent.execution_symbol)
            ask = float(quote.ask) if quote and quote.ask is not None else None
            fresh_qty, fresh_notional = size_host_buy(ask or 0.0, state, limits)
            if intent.quantity > fresh_qty:
                allowed = False
                reason = f"stored qty {intent.quantity} exceeds fresh size {fresh_qty}"
                checks = checks + (("Execution revalidation", "FAIL"),)
            else:
                intent = TradeIntent(
                    strategy_version=intent.strategy_version,
                    signal_symbol=intent.signal_symbol,
                    execution_symbol=intent.execution_symbol,
                    action=intent.action,
                    quantity=intent.quantity if intent.quantity > 0 else fresh_qty,
                    estimated_price=ask,
                    estimated_notional=fresh_notional if intent.quantity <= 0 else round(intent.quantity * (ask or 0), 2),
                    confidence=intent.confidence,
                    regime=intent.regime,
                    reason=intent.reason,
                    signal_id=intent.signal_id,
                    timestamp=intent.timestamp,
                    purpose=intent.purpose,
                    client_order_id=intent.client_order_id,
                )
                checks = checks + (("Execution revalidation", "PASS"),)
        if not allowed:
            store.update_status(
                intent.client_order_id,
                from_statuses={"CLAIMED"},
                to_status="BLOCKED",
                fields={"risk_status": "BLOCKED", "risk_reason": reason},
            )
            return _blocked(intent, reason, checks, armed=armed)
    else:
        qty, sell_reason = sell_qty_allowed(state, intent.execution_symbol, intent.quantity or 10**9)
        checks = (("Sell", "PASS" if sell_reason is None else "FAIL"),)
        if sell_reason:
            store.update_status(
                intent.client_order_id,
                from_statuses={"CLAIMED"},
                to_status="BLOCKED",
                fields={"detail": sell_reason},
            )
            return _blocked(intent, sell_reason, checks, armed=armed)
        intent = TradeIntent(
            strategy_version=intent.strategy_version,
            signal_symbol=intent.signal_symbol,
            execution_symbol=intent.execution_symbol,
            action=intent.action,
            quantity=qty,
            estimated_price=intent.estimated_price,
            estimated_notional=round(qty * float(intent.estimated_price), 2) if intent.estimated_price else None,
            confidence=intent.confidence,
            regime=intent.regime,
            reason=intent.reason,
            signal_id=intent.signal_id,
            timestamp=intent.timestamp,
            purpose=intent.purpose,
            client_order_id=intent.client_order_id,
        )

    if not armed:
        store.update_status(
            intent.client_order_id,
            from_statuses={"CLAIMED"},
            to_status="BLOCKED",
            fields={"detail": armed_reason},
        )
        return _blocked(intent, armed_reason or "not armed", armed=False)

    if not client.allow_writes:
        store.update_status(
            intent.client_order_id,
            from_statuses={"CLAIMED"},
            to_status="BLOCKED",
            fields={"detail": "host transport is read-only"},
        )
        return _blocked(intent, "host transport is read-only", armed=armed)

    # Idempotency: reconcile before place.
    try:
        existing = reconcile_before_retry(state, intent.client_order_id)
    except HostOrderAmbiguous as exc:
        store.update_status(
            intent.client_order_id,
            from_statuses={"CLAIMED"},
            to_status="UNKNOWN",
            fields={"detail": str(exc)},
        )
        return _blocked(intent, str(exc), armed=armed, status="UNKNOWN")
    if existing is not None:
        store.update_status(
            intent.client_order_id,
            from_statuses={"CLAIMED"},
            to_status=existing.status if existing.status in {
                "SUBMITTED", "FILLED", "PARTIALLY_FILLED", "REJECTED", "CANCELLED", "EXPIRED", "UNKNOWN"
            } else "UNKNOWN",
            fields={"detail": existing.detail, "broker_order_id": existing.broker_order_id},
        )
        leg = HostExecLeg(
            intent=intent,
            risk_status="PASS",
            risk_reason="reconciled; place skipped",
            checks=(("Idempotency", "PASS"),),
            execution_status=existing.status,
            text="",
            submit=existing,
        )
        return replace(leg, text=_format_leg(leg, armed=armed))

    args = place_args_for_intent(intent)
    try:
        # Optional review first (documented simulate tool).
        client.call("review_equity_order", args)
        raw = client.call("place_equity_order", args)
        submit = _normalize_place_status(raw)
    except HostOrderAmbiguous as exc:
        store.update_status(
            intent.client_order_id,
            from_statuses={"CLAIMED", "SUBMITTED"},
            to_status="UNKNOWN",
            fields={"detail": str(exc)},
        )
        return _blocked(intent, str(exc), armed=armed, status="UNKNOWN")
    except (HostTransportError, RobinhoodToolRejected, RobinhoodReadError) as exc:
        store.update_status(
            intent.client_order_id,
            from_statuses={"CLAIMED"},
            to_status="REJECTED",
            fields={"detail": str(exc)},
        )
        return _blocked(intent, str(exc), armed=armed, status="REJECTED")

    to_status = submit.status if submit.status in {
        "SUBMITTED", "FILLED", "PARTIALLY_FILLED", "REJECTED", "CANCELLED", "EXPIRED", "UNKNOWN"
    } else "SUBMITTED"
    store.update_status(
        intent.client_order_id,
        from_statuses={"CLAIMED"},
        to_status=to_status,
        fields={
            "broker_order_id": submit.broker_order_id,
            "filled_qty": submit.filled_qty,
            "submitted_at": now.isoformat(),
            "detail": submit.detail,
        },
    )
    leg = HostExecLeg(
        intent=intent,
        risk_status="PASS",
        risk_reason="host place completed",
        checks=(("Arming", "PASS"), ("Place", "PASS")),
        execution_status=to_status,
        text="",
        submit=submit,
    )
    return replace(leg, text=_format_leg(leg, armed=armed))


def execute_flip_second_leg_gate(
    *,
    exit_status: str,
    exit_qty_remaining: float | None,
    broker_state_known: bool,
) -> tuple[bool, str]:
    gate = advance_flip(
        exit_status=exit_status,
        exit_qty_remaining=exit_qty_remaining,
        broker_state_known=broker_state_known,
    )
    return gate.entry_validation_allowed, gate.reason


def run_host_executor_once(
    *,
    transport: HostTransport,
    store: IntentStore,
    env: Mapping[str, str],
    now: datetime | None = None,
    data_bar_start: datetime | None = None,
    client_order_id: str | None = None,
    allow_writes: bool = True,
    position_before: PositionState | None = None,
    dry_run: bool = False,
    webhook_url: str = "",
) -> HostExecRun:
    clock = now or datetime.now(timezone.utc)
    if clock.tzinfo is None:
        clock = clock.replace(tzinfo=timezone.utc)
    source = dict(env)
    limits = host_limits_from_env(source)
    armed_reason = host_arming_block_reason(
        source,
        submission_implemented=ROBINHOOD_HOST_SUBMISSION_IMPLEMENTED,
        host_executor=True,
    )
    armed = armed_reason is None and not dry_run
    client = HostMediatedClient(transport, allow_writes=allow_writes and armed and not dry_run)

    if client_order_id:
        claim: StoreResult = store.claim_by_client_order_id(
            client_order_id, claimed_by="host_executor", now=clock
        )
    else:
        claim = store.claim_next_pending(claimed_by="host_executor", now=clock)
    if not claim.ok or claim.row is None:
        text = f"ROBINHOOD HOST EXECUTOR — no claim ({claim.reason})"
        return HostExecRun((), text, 0, armed=armed)

    leg = execute_claimed_intent(
        claim.row,
        client=client,
        store=store,
        limits=limits,
        env=source,
        now=clock,
        data_bar_start=data_bar_start,
        position_before=position_before,
    )
    log_path = Path(source.get("ROBINHOOD_HOST_EXEC_LOG") or "logs/robinhood_host_exec.jsonl")
    HostAuditLog(log_path).append(leg.audit_row())
    banner = (
        "ROBINHOOD HOST EXECUTOR — REAL MONEY PATH"
        if leg.execution_status in {"SUBMITTED", "FILLED", "PARTIALLY_FILLED"}
        else "ROBINHOOD HOST EXECUTOR — NO FILL"
    )
    post_host_discord(
        webhook_url,
        leg.text,
        dry_run=dry_run or not webhook_url,
        title="Robinhood host executor",
        banner=banner,
        color=0xB45309,
    )
    return HostExecRun((leg,), leg.text, client.place_attempts, armed=armed)
