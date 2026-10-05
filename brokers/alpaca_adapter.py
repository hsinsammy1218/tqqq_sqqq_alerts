"""Read-only view of the existing Alpaca paper book.

This adapter does not submit orders. Paper orders stay on
``alpaca_paper.execute_paper_orders``, which already refuses every host
except ``https://paper-api.alpaca.markets``.
"""

from __future__ import annotations

from brokers.types import (
    AccountView,
    BrokerState,
    LiveSubmissionDisabled,
    OpenOrderView,
    PositionView,
    QuoteView,
    TradeIntent,
)


class AlpacaPaperBroker:
    """Facade over a caller-supplied paper snapshot. No HTTP of its own."""

    def __init__(self, state: BrokerState | None = None) -> None:
        self._state = state or BrokerState(known=False, detail="no alpaca snapshot injected")

    def get_account(self) -> AccountView | None:
        return self._state.account

    def get_positions(self) -> tuple[PositionView, ...]:
        return self._state.positions

    def get_open_orders(self) -> tuple[OpenOrderView, ...]:
        return self._state.open_orders

    def get_quote(self, symbol: str) -> QuoteView | None:
        quote = self._state.quote
        if quote is None:
            return None
        if quote.symbol.upper() != (symbol or "").upper():
            return None
        return quote

    def submit_order(self, order: TradeIntent) -> str:
        raise LiveSubmissionDisabled(
            "AlpacaPaperBroker does not post orders. "
            "Paper submission stays in execute_paper_orders."
        )

    def cancel_order(self, order_id: str) -> None:
        raise LiveSubmissionDisabled(
            "AlpacaPaperBroker does not cancel orders from this path."
        )

    def get_order(self, order_id: str) -> OpenOrderView | None:
        for order in self._state.open_orders:
            if order.client_order_id == order_id:
                return order
        return None

    def snapshot(self) -> BrokerState:
        return self._state
