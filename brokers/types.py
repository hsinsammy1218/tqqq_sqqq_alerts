"""Immutable trade intent and broker-view types.

The strategy decides what it wants. These objects describe that decision
for a broker. They do not place orders.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


ORDER_NEW = "NEW"
ORDER_ACCEPTED = "ACCEPTED"
ORDER_PARTIALLY_FILLED = "PARTIALLY_FILLED"
ORDER_FILLED = "FILLED"
ORDER_CANCELLED = "CANCELLED"
ORDER_REJECTED = "REJECTED"
ORDER_EXPIRED = "EXPIRED"
ORDER_UNKNOWN = "UNKNOWN"

ORDER_STATUSES = frozenset(
    {
        ORDER_NEW,
        ORDER_ACCEPTED,
        ORDER_PARTIALLY_FILLED,
        ORDER_FILLED,
        ORDER_CANCELLED,
        ORDER_REJECTED,
        ORDER_EXPIRED,
        ORDER_UNKNOWN,
    }
)

EXECUTION_NOT_SUBMITTED = "NOT_SUBMITTED"


class UnsafeBrokerConfiguration(Exception):
    """Broker mode is missing, unknown, or would require real-money trading."""


class LiveSubmissionDisabled(Exception):
    """Real Robinhood order submission is not implemented in this build."""


@dataclass(frozen=True)
class TradeIntent:
    strategy_version: str
    signal_symbol: str
    execution_symbol: str
    action: str
    quantity: int
    estimated_price: float | None
    estimated_notional: float | None
    confidence: int
    regime: str
    reason: str
    signal_id: str
    timestamp: str
    purpose: str
    client_order_id: str

    def as_audit(self) -> dict[str, Any]:
        return {
            "strategy_version": self.strategy_version,
            "signal_symbol": self.signal_symbol,
            "execution_symbol": self.execution_symbol,
            "action": self.action,
            "quantity": self.quantity,
            "estimated_price": self.estimated_price,
            "estimated_notional": self.estimated_notional,
            "confidence": self.confidence,
            "regime": self.regime,
            "reason": self.reason,
            "signal_id": self.signal_id,
            "timestamp": self.timestamp,
            "purpose": self.purpose,
            "client_order_id": self.client_order_id,
        }


@dataclass(frozen=True)
class QuoteView:
    symbol: str
    bid: float | None
    ask: float | None
    quote_time: datetime | None
    last: float | None = None


@dataclass(frozen=True)
class AccountView:
    available: bool
    status: str
    buying_power: float | None
    equity: float | None
    day_start_equity: float | None
    week_start_equity: float | None
    peak_equity: float | None


@dataclass(frozen=True)
class PositionView:
    symbol: str
    qty: float


@dataclass(frozen=True)
class OpenOrderView:
    client_order_id: str
    symbol: str
    side: str
    status: str
    qty: float | None = None


@dataclass(frozen=True)
class BrokerState:
    """What a shadow run is allowed to know. ``known=False`` blocks new exposure."""

    known: bool
    account: AccountView | None = None
    positions: tuple[PositionView, ...] = ()
    open_orders: tuple[OpenOrderView, ...] = ()
    known_client_order_ids: frozenset[str] = field(default_factory=frozenset)
    orders_today: int = 0
    quote: QuoteView | None = None
    data_bar_start: datetime | None = None
    now: datetime | None = None
    detail: str = ""


@dataclass(frozen=True)
class RobinhoodLimits:
    max_position_pct: float | None
    max_order_notional: float | None
    max_daily_loss_pct: float | None
    max_weekly_loss_pct: float | None
    max_drawdown_pct: float | None
    max_orders_per_day: int | None
    max_quote_age_seconds: float
    max_spread_bps: float
    max_data_age_minutes: float
    new_entries_enabled: bool
    live_enabled_flag: bool
