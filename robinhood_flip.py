"""FLIP as two verified legs. Cash is acceptable. Both ETFs are not.

This module never calls a broker. It only says whether a second leg may be
validated after an exit. Validation is not submission.

Durable flip pairs are linked via intent meta (restart-safe):
``flip_pair_id``, ``flip_role`` (exit|entry), ``flip_exit_client_order_id``,
``flip_exit_symbol``.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Mapping

from brokers.types import (
    ORDER_ACCEPTED,
    ORDER_CANCELLED,
    ORDER_EXPIRED,
    ORDER_FILLED,
    ORDER_NEW,
    ORDER_PARTIALLY_FILLED,
    ORDER_REJECTED,
    ORDER_UNKNOWN,
)

FLIP_META_PAIR_ID = "flip_pair_id"
FLIP_META_ROLE = "flip_role"
FLIP_META_EXIT_CID = "flip_exit_client_order_id"
FLIP_META_EXIT_SYMBOL = "flip_exit_symbol"
FLIP_ROLE_EXIT = "exit"
FLIP_ROLE_ENTRY = "entry"


@dataclass(frozen=True)
class FlipAdvance:
    entry_validation_allowed: bool
    reason: str
    phase: str


def advance_flip(
    *,
    exit_status: str | None,
    exit_qty_remaining: float | None,
    broker_state_known: bool,
) -> FlipAdvance:
    """Allow the next risk check only after a verified full exit."""
    if not broker_state_known:
        return FlipAdvance(False, "broker uncertainty blocks the second leg", "blocked")
    status = (exit_status or "").strip().upper()
    if status in {"", ORDER_NEW}:
        return FlipAdvance(False, "exit has not been sent; second leg blocked", "need_exit")
    if status == ORDER_ACCEPTED:
        return FlipAdvance(False, "accepted sell is not a fill; second leg blocked", "wait_fill")
    if status == "SUBMITTED":
        return FlipAdvance(False, "submitted sell is not a fill; second leg blocked", "wait_fill")
    if status == ORDER_PARTIALLY_FILLED:
        return FlipAdvance(False, "partial fill does not permit the second leg", "wait_fill")
    if status == ORDER_REJECTED:
        return FlipAdvance(False, "rejected sell blocks the second leg", "blocked")
    if status == ORDER_EXPIRED:
        return FlipAdvance(False, "expired sell blocks the second leg", "blocked")
    if status == ORDER_CANCELLED:
        return FlipAdvance(False, "cancelled sell blocks the second leg", "blocked")
    if status == ORDER_UNKNOWN:
        return FlipAdvance(False, "unknown exit status blocks the second leg", "blocked")
    if status == "TIMEOUT":
        return FlipAdvance(False, "exit timeout blocks the second leg", "blocked")
    if status != ORDER_FILLED:
        return FlipAdvance(False, f"exit status {status} does not permit the second leg", "blocked")
    if exit_qty_remaining is None:
        return FlipAdvance(False, "exit fill was not confirmed flat", "blocked")
    if exit_qty_remaining > 0:
        return FlipAdvance(False, "exit symbol quantity is still open", "blocked")
    return FlipAdvance(
        True,
        "exit filled and quantity is zero; entry may be validated, not submitted",
        "ready_for_entry_check",
    )


def recovery_block_reason(
    *,
    open_order_count: int,
    proposed_client_order_id: str,
    known_client_order_ids: frozenset[str],
    broker_state_known: bool,
) -> str | None:
    """Block a new order when restart state is incomplete or already used."""
    if not broker_state_known:
        return "restart found unknown broker state; new exposure blocked"
    if open_order_count > 0:
        return "restart found an open order; new exposure blocked until it resolves"
    if proposed_client_order_id in known_client_order_ids:
        return "duplicate client_order_id already known; not creating another order"
    return None


def new_flip_pair_id() -> str:
    return f"flip-{uuid.uuid4().hex[:16]}"


def flip_exit_meta(*, pair_id: str, exit_symbol: str) -> dict[str, Any]:
    return {
        FLIP_META_PAIR_ID: pair_id,
        FLIP_META_ROLE: FLIP_ROLE_EXIT,
        FLIP_META_EXIT_SYMBOL: (exit_symbol or "").strip().upper(),
    }


def flip_entry_meta(
    *,
    pair_id: str,
    exit_client_order_id: str,
    exit_symbol: str,
) -> dict[str, Any]:
    return {
        FLIP_META_PAIR_ID: pair_id,
        FLIP_META_ROLE: FLIP_ROLE_ENTRY,
        FLIP_META_EXIT_CID: (exit_client_order_id or "").strip(),
        FLIP_META_EXIT_SYMBOL: (exit_symbol or "").strip().upper(),
    }


def is_flip_entry_purpose(purpose: str | None) -> bool:
    return (purpose or "").strip().lower() == "flip_entry"


def is_flip_exit_purpose(purpose: str | None) -> bool:
    return (purpose or "").strip().lower() == "flip_exit"


@dataclass(frozen=True)
class FlipEntryGateResult:
    allowed: bool
    reason: str
    terminal_status: str | None  # BLOCKED / RECONCILIATION_REQUIRED / None if ok
    exit_status: str | None


def gate_flip_entry_from_exit_row(
    *,
    exit_row: Mapping[str, Any] | None,
    exit_qty_remaining: float | None,
    broker_state_known: bool,
) -> FlipEntryGateResult:
    """Restart-safe second-leg gate using durable exit intent row + broker flatness."""
    if exit_row is None:
        return FlipEntryGateResult(
            False,
            "flip exit intent missing; second leg blocked",
            "BLOCKED",
            None,
        )
    exit_status = str(exit_row.get("status") or "").strip().upper()
    if exit_status in {"UNKNOWN", "RECONCILIATION_REQUIRED"}:
        return FlipEntryGateResult(
            False,
            f"flip exit status {exit_status}; reconciliation required before entry",
            "RECONCILIATION_REQUIRED",
            exit_status,
        )
    gate = advance_flip(
        exit_status=exit_status,
        exit_qty_remaining=exit_qty_remaining,
        broker_state_known=broker_state_known,
    )
    if not gate.entry_validation_allowed:
        return FlipEntryGateResult(
            False,
            gate.reason,
            "BLOCKED",
            exit_status,
        )
    return FlipEntryGateResult(True, gate.reason, None, exit_status)
