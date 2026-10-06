"""Select a quote by execution symbol.

``quotes[0]`` is never the execution quote. A missing symbol returns None so
the risk gate can block instead of pricing the other ETF.
"""

from __future__ import annotations

from brokers.types import BrokerState, QuoteView


def quote_for_symbol(state: BrokerState, symbol: str) -> QuoteView | None:
    """Return the quote for ``symbol``, or None when that symbol is absent."""
    wanted = (symbol or "").strip().upper()
    if not wanted:
        return None
    for quote in state.quotes:
        if quote.symbol.upper() == wanted:
            return quote
    legacy = state.quote
    if legacy is not None and legacy.symbol.upper() == wanted:
        return legacy
    return None
