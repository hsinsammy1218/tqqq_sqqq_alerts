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
from robinhood_account_isolation import (
    AccountIsolationError,
    BoundAgenticAccount,
    assert_account_allowed,
    enforce_tool_account_arg,
    require_bound_account,
)
from brokers.types import (
    ORDER_FILLED,
    ORDER_PARTIALLY_FILLED,
    ORDER_UNKNOWN,
    BrokerState,
    QuoteView,
    TradeIntent,
    UnsafeBrokerConfiguration,
)
from robinhood_flip import (
    FLIP_META_EXIT_CID,
    FLIP_META_EXIT_SYMBOL,
    advance_flip,
    gate_flip_entry_from_exit_row,
    is_flip_entry_purpose,
)
from robinhood_host_audit import HostAuditLog, post_host_discord
from robinhood_host_entry import (
    InMemoryRhEntryReservationStore,
    RhEntryReservationStore,
    reserve_rh_daily_entry,
)
from robinhood_host_equity import (
    EquityBaselineStore,
    InMemoryEquityBaselineStore,
    merge_account_baselines,
)
from robinhood_host_risk import (
    check_host_new_exposure,
    host_arming_block_reason,
    host_limits_from_env,
    quote_for,
    sell_qty_allowed,
    size_host_buy,
)
from robinhood_host_schema import (
    HostMcpCapabilities,
    assert_capabilities_ready,
    build_place_args,
    docs_confirmed_capabilities,
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
    """Read + optional write through an injected authenticated host transport.

    All account-scoped tools are pinned to the bound Agentic account.
    """

    def __init__(
        self,
        transport: HostTransport,
        *,
        allow_writes: bool,
        bound: BoundAgenticAccount | None = None,
        env: Mapping[str, str] | None = None,
    ) -> None:
        self._transport = transport
        self.allow_writes = allow_writes
        self.bound = bound
        self._env = env
        self.invocations: list[str] = []
        self.place_attempts = 0

    def call(self, name: str, arguments: dict[str, Any] | None = None) -> Any:
        tool = assert_host_tool(name, allow_writes=self.allow_writes)
        try:
            # H1: never lazy-bind from env pin alone — live resolve via read_snapshot.
            if tool != "get_accounts" and self.bound is None:
                raise AccountIsolationError(
                    f"refusing {tool}: Agentic not live-resolved; call read_snapshot first"
                )
            args = enforce_tool_account_arg(tool, arguments, self.bound)
        except AccountIsolationError as exc:
            raise RobinhoodToolRejected(str(exc)) from exc
        self.invocations.append(tool)
        if tool == "place_equity_order":
            self.place_attempts += 1
        try:
            return self._transport(tool, dict(args))
        except (RobinhoodToolRejected, RobinhoodReadError, HostTransportError, HostOrderAmbiguous):
            raise
        except Exception as exc:  # noqa: BLE001
            raise HostTransportError(f"{tool} failed: {exc}") from None

    def read_snapshot(self, symbols: tuple[str, ...] = ("TQQQ", "SQQQ")) -> dict[str, Any]:
        # Bind Agentic before account-scoped tools. Do not delegate to
        # RobinhoodReadClient.read_snapshot here — that would re-enter
        # host.call before ``self.bound`` is set.
        if len(symbols) > 20:
            raise RobinhoodReadError("get_equity_quotes accepts at most 20 symbols")
        accounts = self.call("get_accounts", {})
        try:
            self.bound = require_bound_account(
                get_accounts_payload=accounts,
                env=self._env,
            )
        except AccountIsolationError as exc:
            raise RobinhoodReadError(str(exc)) from exc
        return {
            "get_accounts": accounts,
            "get_portfolio": self.call("get_portfolio", {}),
            "get_equity_positions": self.call("get_equity_positions", {}),
            "get_equity_quotes": self.call(
                "get_equity_quotes", {"symbols": list(symbols)}
            ),
            "get_equity_orders": self.call("get_equity_orders", {}),
        }


def place_args_for_intent(
    intent: TradeIntent,
    *,
    quote: QuoteView | None = None,
    capabilities: HostMcpCapabilities | None = None,
    slippage_bps: float = 25.0,
    bound: BoundAgenticAccount | None = None,
) -> dict[str, Any]:
    """Allowlisted place/review args. Never invents ``client_order_id``.

    Uses marketable LIMIT when capabilities confirm limit support; otherwise
    fail closed (no silent market fallback). Internal UNIQUE ``client_order_id``
    stays in Supabase for idempotency — it is not sent to MCP. Requires bound
    Agentic ``account_number``.
    """
    caps = capabilities or docs_confirmed_capabilities()
    return build_place_args(
        intent,
        quote=quote,
        capabilities=caps,
        slippage_bps=slippage_bps,
        bound=bound,
    )


# Review decisions honored before place (H4). Mock/fixture schemas only.
REVIEW_APPROVED = "APPROVED"
REVIEW_REJECTED = "REJECTED"
REVIEW_PENDING = "PENDING"
REVIEW_EXPIRED = "EXPIRED"
REVIEW_UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class ReviewDecision:
    decision: str
    detail: str


def parse_review_decision(payload: Any) -> ReviewDecision:
    """Map review_equity_order payload → APPROVED|REJECTED|PENDING|EXPIRED|UNKNOWN."""
    if payload is None:
        return ReviewDecision(REVIEW_UNKNOWN, "review returned empty payload")
    if not isinstance(payload, dict):
        return ReviewDecision(REVIEW_UNKNOWN, "review returned non-object")
    raw = (
        payload.get("decision")
        or payload.get("status")
        or payload.get("review_status")
        or payload.get("state")
        or ""
    )
    text = str(raw).strip().upper()
    if text in {REVIEW_APPROVED, "OK", "PASS", "PASSED", "ALLOW", "ALLOWED"}:
        return ReviewDecision(REVIEW_APPROVED, "review approved")
    if text in {REVIEW_REJECTED, "DENY", "DENIED", "BLOCK", "BLOCKED", "FAIL", "FAILED"}:
        return ReviewDecision(REVIEW_REJECTED, "review rejected")
    if text in {REVIEW_PENDING, "QUEUED", "AWAITING", "AWAITING_APPROVAL"}:
        return ReviewDecision(REVIEW_PENDING, "review pending")
    if text in {REVIEW_EXPIRED, "TIMEOUT"}:
        return ReviewDecision(REVIEW_EXPIRED, "review expired")
    if text in {REVIEW_UNKNOWN, "AMBIGUOUS"}:
        return ReviewDecision(REVIEW_UNKNOWN, "review unknown")
    # Fixture/mock ok boolean (Phase 5R.3 engine).
    if "ok" in payload:
        if payload.get("ok") is True:
            return ReviewDecision(REVIEW_APPROVED, "review ok=true")
        if payload.get("ok") is False:
            warnings = payload.get("warnings") or []
            detail = "review ok=false"
            if warnings:
                detail = f"review rejected: {warnings}"
            return ReviewDecision(REVIEW_REJECTED, detail)
    if text:
        return ReviewDecision(REVIEW_UNKNOWN, f"unrecognized review status {text!r}")
    return ReviewDecision(REVIEW_UNKNOWN, "review missing decision/status/ok")


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
    equity_store: EquityBaselineStore | None = None,
) -> BrokerState:
    try:
        raw = client.read_snapshot()
        state = normalize_snapshot(raw, now=now, data_bar_start=data_bar_start)
    except (RobinhoodReadError, RobinhoodToolRejected, HostTransportError, ValueError, TypeError, KeyError):
        return BrokerState(
            known=False,
            detail="host Robinhood read failed",
            data_bar_start=data_bar_start,
            now=now,
            order_history_complete=False,
        )
    if state.account is None or equity_store is None:
        return state
    merged_account, _ = merge_account_baselines(state.account, equity_store, now=now)
    return replace(state, account=merged_account)


def reconcile_before_retry(
    state: BrokerState,
    client_order_id: str,
    *,
    store_row: dict[str, Any] | None = None,
) -> HostSubmitResult | None:
    """Idempotency without relying on broker ``client_order_id``.

    Authority order:
    1. Our durable intent row already SUBMITTED/FILLED/UNKNOWN → never place again
    2. Open order matched by broker_order_id we previously stored
    3. Open order matched by symbol+side (ambiguous → UNKNOWN, no resubmit)
    4. Internal id appearing in known_client_order_ids (rare; treat as ambiguous)
    """
    if store_row is not None:
        status = str(store_row.get("status") or "").upper()
        if status in {
            "SUBMITTED",
            "FILLED",
            "PARTIALLY_FILLED",
            "UNKNOWN",
            "REJECTED",
            "CANCELLED",
            "EXPIRED",
        }:
            raise HostOrderAmbiguous(
                f"intent already terminal/in-flight as {status}; no resubmit"
            )
        broker_oid = store_row.get("broker_order_id")
        if broker_oid:
            for order in state.open_orders:
                # OpenOrderView.client_order_id may hold broker id when RH omits client id
                if order.client_order_id == str(broker_oid):
                    st = order.status.upper()
                    if st == ORDER_UNKNOWN:
                        raise HostOrderAmbiguous("existing broker order UNKNOWN; no resubmit")
                    return HostSubmitResult(
                        st if st else ORDER_UNKNOWN,
                        str(broker_oid),
                        None,
                        "reconciled by broker_order_id; skipped place",
                        0,
                    )

    # Symbol+side collision with an open order → ambiguous (no client_order_id bridge)
    intent_side = None
    intent_symbol = None
    if store_row is not None:
        intent_side = str(store_row.get("action") or "").lower()
        intent_symbol = str(store_row.get("execution_symbol") or "").upper()
    if intent_side and intent_symbol:
        conflicts = [
            o
            for o in state.open_orders
            if o.symbol.upper() == intent_symbol and o.side.lower() == intent_side
        ]
        if conflicts:
            raise HostOrderAmbiguous(
                "open order on same symbol/side; reconcile manually; no resubmit"
            )

    if client_order_id in state.known_client_order_ids:
        raise HostOrderAmbiguous("internal client_order_id already known at broker; no resubmit")
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
    reservation_store: RhEntryReservationStore | None = None,
    equity_store: EquityBaselineStore | None = None,
    capabilities: HostMcpCapabilities | None = None,
) -> HostExecLeg:
    armed_reason = host_arming_block_reason(
        env,
        submission_implemented=ROBINHOOD_HOST_SUBMISSION_IMPLEMENTED,
        host_executor=True,
        # NEW_ENTRIES is enforced only on BUY via check_host_new_exposure so
        # risk-reducing SELL remains possible when new entries are killed.
        require_new_entries=False,
    )
    armed = armed_reason is None
    durable = intent_from_row(row)
    intent = durable.intent
    caps = capabilities or docs_confirmed_capabilities()
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

    state = load_host_state(
        client,
        now=now,
        data_bar_start=data_bar_start,
        equity_store=equity_store,
    )
    book = reconcile_books(position_before, state)
    if book.blocks_new_exposure and intent.action == "BUY":
        store.update_status(
            intent.client_order_id,
            from_statuses={"CLAIMED"},
            to_status="BLOCKED",
            fields={"detail": book.reason},
        )
        return _blocked(intent, book.reason, (("Reconcile", "FAIL"),), armed=armed)

    quote = quote_for(state, intent.execution_symbol)
    checks: tuple[tuple[str, str], ...] = ()
    row_meta = row.get("meta") if isinstance(row.get("meta"), dict) else {}

    # H2: flip_entry requires confirmed exit close (durable store + broker flat).
    if is_flip_entry_purpose(intent.purpose):
        exit_cid = str(row_meta.get(FLIP_META_EXIT_CID) or "").strip()
        exit_symbol = str(
            row_meta.get(FLIP_META_EXIT_SYMBOL) or ""
        ).strip().upper()
        exit_row = store.get(exit_cid) if exit_cid else None
        exit_qty_remaining: float | None = None
        if state.known and exit_symbol:
            held = 0.0
            for pos in state.positions:
                if pos.symbol.upper() == exit_symbol:
                    held += float(pos.qty)
            exit_qty_remaining = held
        flip_gate = gate_flip_entry_from_exit_row(
            exit_row=exit_row,
            exit_qty_remaining=exit_qty_remaining,
            broker_state_known=state.known,
        )
        if not flip_gate.allowed:
            to_status = flip_gate.terminal_status or "BLOCKED"
            store.update_status(
                intent.client_order_id,
                from_statuses={"CLAIMED"},
                to_status=to_status,
                fields={"detail": flip_gate.reason},
            )
            return _blocked(
                intent,
                flip_gate.reason,
                (("FLIP", "FAIL"),),
                armed=armed,
                status=to_status,
            )
        checks = checks + (("FLIP", "PASS"),)

    if intent.action == "BUY":
        # Cannot execute without known/fresh state — PENDING ≠ approved.
        if not state.known:
            store.update_status(
                intent.client_order_id,
                from_statuses={"CLAIMED"},
                to_status="BLOCKED",
                fields={"detail": "unknown broker state; PENDING is not approval"},
            )
            return _blocked(
                intent,
                "unknown broker state; cannot execute PENDING without known/fresh state",
                (("Broker state", "FAIL"), ("Pending≠approved", "PASS")),
                armed=armed,
            )
        allowed, reason, exposure_checks = check_host_new_exposure(intent, state, limits)
        checks = checks + exposure_checks
        if allowed:
            ask = float(quote.ask) if quote and quote.ask is not None else None
            fresh_qty, fresh_notional = size_host_buy(ask or 0.0, state, limits)
            if intent.quantity > fresh_qty:
                allowed = False
                reason = f"stored qty {intent.quantity} exceeds fresh size {fresh_qty}"
                checks = checks + (("Execution revalidation", "FAIL"),)
            else:
                qty = intent.quantity if intent.quantity > 0 else fresh_qty
                intent = TradeIntent(
                    strategy_version=intent.strategy_version,
                    signal_symbol=intent.signal_symbol,
                    execution_symbol=intent.execution_symbol,
                    action=intent.action,
                    quantity=qty,
                    estimated_price=ask,
                    estimated_notional=(
                        fresh_notional
                        if intent.quantity <= 0
                        else round(qty * (ask or 0), 2)
                    ),
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

        # H5: arming gate BEFORE daily reservation — disarmed must not burn slots.
        if not armed:
            store.update_status(
                intent.client_order_id,
                from_statuses={"CLAIMED"},
                to_status="BLOCKED",
                fields={"detail": armed_reason},
            )
            return _blocked(intent, armed_reason or "not armed", checks, armed=False)

        # Authoritative daily entry reservation at host BUY boundary (not Render).
        res_store = reservation_store or InMemoryRhEntryReservationStore()
        reserved = reserve_rh_daily_entry(
            res_store,
            now=now,
            meta={
                "client_order_id": intent.client_order_id,
                "purpose": intent.purpose,
                "execution_symbol": intent.execution_symbol,
                "signal_id": intent.signal_id,
            },
        )
        if not reserved.reserved:
            store.update_status(
                intent.client_order_id,
                from_statuses={"CLAIMED"},
                to_status="BLOCKED",
                fields={"detail": reserved.reason},
            )
            return _blocked(
                intent,
                reserved.reason,
                checks + (("Daily entry reservation", "FAIL"),),
                armed=armed,
            )
        checks = checks + (("Daily entry reservation", "PASS"),)
    else:
        if not state.known:
            store.update_status(
                intent.client_order_id,
                from_statuses={"CLAIMED"},
                to_status="BLOCKED",
                fields={"detail": "unknown broker state; sell blocked"},
            )
            return _blocked(
                intent,
                "unknown broker state; sell blocked",
                (("Broker state", "FAIL"),),
                armed=armed,
            )
        qty, sell_reason = sell_qty_allowed(
            state, intent.execution_symbol, intent.quantity or 10**9
        )
        checks = (("Sell", "PASS" if sell_reason is None else "FAIL"),)
        if sell_reason:
            store.update_status(
                intent.client_order_id,
                from_statuses={"CLAIMED"},
                to_status="BLOCKED",
                fields={"detail": sell_reason},
            )
            return _blocked(intent, sell_reason, checks, armed=armed)
        bid = float(quote.bid) if quote and quote.bid is not None else intent.estimated_price
        intent = TradeIntent(
            strategy_version=intent.strategy_version,
            signal_symbol=intent.signal_symbol,
            execution_symbol=intent.execution_symbol,
            action=intent.action,
            quantity=qty,
            estimated_price=bid,
            estimated_notional=round(qty * float(bid), 2) if bid else None,
            confidence=intent.confidence,
            regime=intent.regime,
            reason=intent.reason,
            signal_id=intent.signal_id,
            timestamp=intent.timestamp,
            purpose=intent.purpose,
            client_order_id=intent.client_order_id,
        )
        # H5: arming before any place (SELL has no reservation).
        if not armed:
            store.update_status(
                intent.client_order_id,
                from_statuses={"CLAIMED"},
                to_status="BLOCKED",
                fields={"detail": armed_reason},
            )
            return _blocked(intent, armed_reason or "not armed", checks, armed=False)

    if not client.allow_writes:
        store.update_status(
            intent.client_order_id,
            from_statuses={"CLAIMED"},
            to_status="BLOCKED",
            fields={"detail": "host transport is read-only"},
        )
        return _blocked(intent, "host transport is read-only", armed=armed)

    try:
        assert_capabilities_ready(caps)
        if client.bound is None:
            raise UnsafeBrokerConfiguration(
                "Agentic account not bound after host read; refusing place"
            )
        args = place_args_for_intent(
            intent,
            quote=quote,
            capabilities=caps,
            slippage_bps=float(limits.max_slippage_bps),
            bound=client.bound,
        )
        if "client_order_id" in args:
            raise UnsafeBrokerConfiguration(
                "place args included undocumented client_order_id"
            )
    except UnsafeBrokerConfiguration as exc:
        store.update_status(
            intent.client_order_id,
            from_statuses={"CLAIMED"},
            to_status="BLOCKED",
            fields={"detail": str(exc)},
        )
        return _blocked(intent, str(exc), checks + (("Schema", "FAIL"),), armed=armed)

    # Idempotency: reconcile before place (internal id + store status authority).
    try:
        existing = reconcile_before_retry(
            state, intent.client_order_id, store_row=store.get(intent.client_order_id)
        )
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

    # H4: honor review decision before place (mock/fixture schemas only).
    place_started = False
    try:
        review_raw = client.call("review_equity_order", args)
        review = parse_review_decision(review_raw)
        if review.decision != REVIEW_APPROVED:
            if review.decision == REVIEW_REJECTED:
                to = "REJECTED"
            elif review.decision in {REVIEW_PENDING, REVIEW_EXPIRED}:
                to = "BLOCKED"
            else:
                to = "UNKNOWN"
            store.update_status(
                intent.client_order_id,
                from_statuses={"CLAIMED"},
                to_status=to,
                fields={"detail": review.detail},
            )
            return _blocked(
                intent,
                review.detail,
                checks + (("Review", "FAIL"),),
                armed=armed,
                status=to,
            )
        checks = checks + (("Review", "PASS"),)
        place_started = True
        raw = client.call("place_equity_order", args)
        submit = _normalize_place_status(raw)
    except HostOrderAmbiguous as exc:
        # Ambiguous place outcome → reconciliation, never auto-resubmit.
        to = "RECONCILIATION_REQUIRED" if place_started else "UNKNOWN"
        store.update_status(
            intent.client_order_id,
            from_statuses={"CLAIMED", "SUBMITTED"},
            to_status=to,
            fields={"detail": str(exc)},
        )
        return _blocked(intent, str(exc), armed=armed, status=to)
    except RobinhoodToolRejected as exc:
        # Explicit tool rejection before/without successful place accept.
        detail = str(exc)
        to = "RECONCILIATION_REQUIRED" if place_started else "REJECTED"
        store.update_status(
            intent.client_order_id,
            from_statuses={"CLAIMED"},
            to_status=to,
            fields={"detail": detail},
        )
        return _blocked(intent, detail, armed=armed, status=to)
    except (HostTransportError, RobinhoodReadError) as exc:
        # H3: any transport/read error after place is invoked → UNKNOWN /
        # RECONCILIATION_REQUIRED (never REJECTED). Pre-place transport errors
        # during review also stay non-REJECTED when ambiguous.
        detail = str(exc)
        if place_started:
            to = "RECONCILIATION_REQUIRED"
        else:
            to = "UNKNOWN"
        store.update_status(
            intent.client_order_id,
            from_statuses={"CLAIMED"},
            to_status=to,
            fields={"detail": detail},
        )
        return _blocked(intent, detail, armed=armed, status=to)

    to_status = submit.status if submit.status in {
        "SUBMITTED",
        "FILLED",
        "PARTIALLY_FILLED",
        "REJECTED",
        "CANCELLED",
        "EXPIRED",
        "UNKNOWN",
        "RECONCILIATION_REQUIRED",
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
        checks=checks + (("Arming", "PASS"), ("Schema", "PASS"), ("Place", "PASS")),
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
    reservation_store: RhEntryReservationStore | None = None,
    equity_store: EquityBaselineStore | None = None,
    capabilities: HostMcpCapabilities | None = None,
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
        require_new_entries=False,
    )
    armed = armed_reason is None and not dry_run
    client = HostMediatedClient(
        transport,
        allow_writes=allow_writes and armed and not dry_run,
        env=source,
    )
    res_store = reservation_store if reservation_store is not None else InMemoryRhEntryReservationStore()
    eq_store = equity_store if equity_store is not None else InMemoryEquityBaselineStore()
    caps = capabilities or docs_confirmed_capabilities()

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
        reservation_store=res_store,
        equity_store=eq_store,
        capabilities=caps,
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
