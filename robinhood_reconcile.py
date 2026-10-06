"""Compare the local book with Robinhood. The broker is authoritative.

A mismatch blocks new exposure. This module never places a corrective order.
"""

from __future__ import annotations

from dataclasses import dataclass

from brokers.types import (
    ORDER_CANCELLED,
    ORDER_EXPIRED,
    ORDER_FILLED,
    ORDER_REJECTED,
    ORDER_UNKNOWN,
    BrokerState,
)
from strategy_types import PositionState

_TERMINAL = frozenset({ORDER_FILLED, ORDER_CANCELLED, ORDER_REJECTED, ORDER_EXPIRED})
_FLAT = frozenset({"", "NONE", "FLAT", "CASH"})


@dataclass(frozen=True)
class ReconcileResult:
    blocks_new_exposure: bool
    reason: str


def _qty(state: BrokerState, symbol: str) -> float:
    total = 0.0
    for row in state.positions:
        if row.symbol.upper() == symbol:
            total += float(row.qty)
    return total


def reconcile_books(local: PositionState | None, state: BrokerState) -> ReconcileResult:
    if not state.known:
        detail = state.detail or "broker state unknown"
        return ReconcileResult(True, f"{detail}; new exposure blocked")
    for order in state.open_orders:
        if order.symbol.upper() not in {"TQQQ", "SQQQ"}:
            continue
        if order.status == ORDER_UNKNOWN or order.status not in _TERMINAL:
            if order.status == ORDER_UNKNOWN:
                return ReconcileResult(True, "unknown order status; new exposure blocked")
            return ReconcileResult(True, "open Robinhood order; new exposure blocked")
    if not state.order_history_complete:
        return ReconcileResult(True, "order history is incomplete; new exposure blocked")
    tqqq = _qty(state, "TQQQ")
    sqqq = _qty(state, "SQQQ")
    if tqqq > 0 and sqqq > 0:
        return ReconcileResult(True, "broker holds both TQQQ and SQQQ; new exposure blocked")
    local_symbol = ""
    if local is not None and local.active_symbol:
        local_symbol = local.active_symbol.strip().upper()
    if local_symbol in _FLAT:
        if tqqq > 0 or sqqq > 0:
            return ReconcileResult(
                True,
                "local book is flat but Robinhood holds a position; new exposure blocked",
            )
        return ReconcileResult(False, "local book matches Robinhood")
    if local_symbol not in {"TQQQ", "SQQQ"}:
        return ReconcileResult(True, "local symbol is not TQQQ or SQQQ; new exposure blocked")
    held = tqqq if local_symbol == "TQQQ" else sqqq
    other = sqqq if local_symbol == "TQQQ" else tqqq
    if held <= 0 or other > 0:
        return ReconcileResult(True, "local position does not match Robinhood; new exposure blocked")
    return ReconcileResult(False, "local book matches Robinhood")
