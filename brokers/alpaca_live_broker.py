"""Alpaca Live adapter. Reads from a snapshot; submission is hard-disabled.

Environment flags such as ``ALPACA_LIVE_ENABLED`` cannot unlock submission.
A future pilot must edit ``ALPACA_LIVE_SUBMISSION_IMPLEMENTED`` in code.
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

# Deliberate gate. No env var may flip this. Phase 4 would change it in code.
ALPACA_LIVE_SUBMISSION_IMPLEMENTED = False


class AlpacaLiveBroker:
    def __init__(self, state: BrokerState | None = None) -> None:
        self._state = state or BrokerState(
            known=False,
            detail="no alpaca live snapshot; new exposure blocked",
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
        if not ALPACA_LIVE_SUBMISSION_IMPLEMENTED:
            raise LiveSubmissionDisabled(
                "Alpaca Live order submission is disabled. "
                "ALPACA_LIVE_SUBMISSION_IMPLEMENTED is false. "
                f"Refused {order.action} {order.execution_symbol} "
                f"client_order_id={order.client_order_id}."
            )
        raise LiveSubmissionDisabled(
            "Live submission flag was edited without an implementation. Refusing."
        )

    def cancel_order(self, order_id: str) -> None:
        self.submission_attempts += 1
        raise LiveSubmissionDisabled(
            f"Alpaca Live cancel is disabled in this build (order_id={order_id})."
        )

    def replace_order(self, order_id: str, order: TradeIntent) -> str:
        self.submission_attempts += 1
        raise LiveSubmissionDisabled(
            f"Alpaca Live replace is disabled in this build (order_id={order_id})."
        )

    def get_order(self, order_id: str) -> OpenOrderView | None:
        if not self._state.known:
            return None
        for row in self._state.open_orders:
            if row.client_order_id == order_id:
                return row
        return None

    def snapshot(self) -> BrokerState:
        return self._state
