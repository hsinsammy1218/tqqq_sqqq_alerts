"""Robinhood Agentic adapter.

This phase has no Robinhood HTTP client. Account, positions, quotes, and
orders come only from a snapshot the caller already trusts. ``submit_order``
and ``cancel_order`` always refuse, including when ``ROBINHOOD_LIVE_ENABLED``
is true. Turning on live trading requires editing ``LIVE_SUBMISSION_IMPLEMENTED``.
"""

from __future__ import annotations

from brokers.quotes import quote_for_symbol
from brokers.types import (
    AccountView,
    BrokerState,
    LiveSubmissionDisabled,
    OpenOrderView,
    PositionView,
    QuoteView,
    TradeIntent,
)

# Deliberate gate. A future live pilot must change this constant in code.
# Environment variables cannot flip it.
LIVE_SUBMISSION_IMPLEMENTED = False


class RobinhoodAgenticBroker:
    def __init__(self, state: BrokerState | None = None) -> None:
        self._state = state or BrokerState(
            known=False,
            detail="no robinhood snapshot; new exposure blocked",
        )
        self.submission_attempts = 0

    def get_account(self) -> AccountView | None:
        if not self._state.known:
            return None
        return self._state.account

    def get_positions(self) -> tuple[PositionView, ...]:
        if not self._state.known:
            return ()
        return self._state.positions

    def get_open_orders(self) -> tuple[OpenOrderView, ...]:
        if not self._state.known:
            return ()
        return self._state.open_orders

    def get_quote(self, symbol: str) -> QuoteView | None:
        if not self._state.known:
            return None
        return quote_for_symbol(self._state, symbol)

    def submit_order(self, order: TradeIntent) -> str:
        self.submission_attempts += 1
        if not LIVE_SUBMISSION_IMPLEMENTED:
            raise LiveSubmissionDisabled(
                "Robinhood order submission is disabled. "
                "LIVE_SUBMISSION_IMPLEMENTED is false. "
                f"Refused {order.action} {order.execution_symbol} "
                f"client_order_id={order.client_order_id}."
            )
        raise LiveSubmissionDisabled(
            "Live submission flag was edited without an implementation. Refusing."
        )

    def cancel_order(self, order_id: str) -> None:
        self.submission_attempts += 1
        raise LiveSubmissionDisabled(
            f"Robinhood cancel is disabled in this build (order_id={order_id})."
        )

    def get_order(self, order_id: str) -> OpenOrderView | None:
        if not self._state.known:
            return None
        for order in self._state.open_orders:
            if order.client_order_id == order_id:
                return order
        return None

    def snapshot(self) -> BrokerState:
        return self._state
