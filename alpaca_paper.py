"""Optional Alpaca *paper* limit-order execution for actionable TQQQ/SQQQ alerts.

Orders are submitted only when ``ALPACA_PAPER_TRADING=true`` and ``DRY_RUN=false``.
The trading host is hard-gated to the paper API; live trading is not supported.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

PAPER_TRADING_BASE_URL = "https://paper-api.alpaca.markets"
ACTIONABLE_ALERTS = frozenset({"BUY", "SELL", "FLIP"})


class PaperTradingError(Exception):
    """Non-fatal paper-trading failure (logged; does not abort the alert run)."""


@dataclass(frozen=True)
class OrderIntent:
    symbol: str
    side: str
    purpose: str


@dataclass(frozen=True)
class OrderResult:
    intent: OrderIntent
    ok: bool
    status: str
    detail: str
    order_id: str | None = None
    payload: dict[str, Any] | None = None


def is_live_trading_host(base_url: str) -> bool:
    """Refuse every trading URL except the exact Alpaca paper host."""
    normalized = (base_url or "").strip().rstrip("/")
    return normalized != PAPER_TRADING_BASE_URL


def should_submit_paper_orders(*, paper_trading: bool, dry_run: bool) -> bool:
    return bool(paper_trading) and not bool(dry_run)
