"""Compare strategy memory to the Alpaca paper book before and after a cron.

This module does not change ``decide()`` rules. It only decides which position
memory is safe to feed into a decision and which decision is safe to save.
"""

from __future__ import annotations

from dataclasses import dataclass

from strategy_types import PositionState

_QTY_EPS = 1e-9


@dataclass(frozen=True)
class BrokerSnapshot:
    """Paper inventory the cron can see. Quantities are absolute shares."""

    tqqq_qty: float = 0.0
    sqqq_qty: float = 0.0
    open_order_count: int = 0

    def held_symbol(self) -> str | None:
        """``TQQQ``, ``SQQQ``, ``BOTH``, or None when flat."""
        long_t = self.tqqq_qty > _QTY_EPS
        long_s = self.sqqq_qty > _QTY_EPS
        if long_t and long_s:
            return "BOTH"
        if long_t:
            return "TQQQ"
        if long_s:
            return "SQQQ"
        return None


@dataclass(frozen=True)
class StartReconcile:
    position_for_decide: PositionState
    block_new_orders: bool
    reason: str


def reconcile_at_start(memory: PositionState, broker: BrokerSnapshot) -> StartReconcile:
    """Align the position passed to ``decide()`` with the broker, or refuse to trade.

    Open orders and a two-ETF book block new orders and leave saved memory alone.
    A single broker position the memory does not name is what ``decide()`` sees,
    so a later cron cannot buy a second copy of a fill the previous save missed.
    A broker-flat account is flat for the decision even if memory still names a symbol.
    """
    held = broker.held_symbol()
    if held == "BOTH":
        return StartReconcile(
            position_for_decide=memory,
            block_new_orders=True,
            reason="broker holds both TQQQ and SQQQ; not submitting orders",
        )
    if broker.open_order_count > 0:
        return StartReconcile(
            position_for_decide=memory,
            block_new_orders=True,
            reason="open TQQQ/SQQQ order still working; not submitting another",
        )
    memory_symbol = memory.active_symbol
    if memory_symbol == held:
        return StartReconcile(
            position_for_decide=memory,
            block_new_orders=False,
            reason="memory matches broker",
        )
    if held is None:
        return StartReconcile(
            position_for_decide=PositionState(),
            block_new_orders=False,
            reason="broker is flat; decision starts flat",
        )
    same_symbol = memory_symbol == held
    adopted = PositionState(
        active_symbol=held,
        entry_price=memory.entry_price if same_symbol else None,
        entry_timestamp=memory.entry_timestamp if same_symbol else None,
        last_signal=memory.last_signal,
        updated_at=memory.updated_at,
        favorable_extreme=memory.favorable_extreme if same_symbol else None,
    )
    return StartReconcile(
        position_for_decide=adopted,
        block_new_orders=False,
        reason=f"broker holds {held}; decision uses that inventory",
    )


def position_matches_broker(position: PositionState, broker: BrokerSnapshot) -> bool:
    """True only when the symbol we would save is the symbol the broker holds."""
    held = broker.held_symbol()
    if held == "BOTH":
        return False
    return (position.active_symbol or None) == held


def state_to_save(proposed: PositionState, broker: BrokerSnapshot) -> PositionState | None:
    """Return ``proposed`` only when the broker already matches it. Otherwise save nothing."""
    if position_matches_broker(proposed, broker):
        return proposed
    return None
