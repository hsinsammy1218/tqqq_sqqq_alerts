"""FLIP as two verified legs. Cash is acceptable. Both ETFs are not.

This module never calls a broker. It only says whether a second leg may be
validated after an exit. Validation is not submission.
"""

from __future__ import annotations

from dataclasses import dataclass

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
