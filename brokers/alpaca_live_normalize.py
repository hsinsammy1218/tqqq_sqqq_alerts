"""Normalize Alpaca Live read payloads into BrokerState."""

from __future__ import annotations

from datetime import datetime, timezone
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
    AccountView,
    BrokerState,
    OpenOrderView,
    PositionView,
    QuoteView,
)

_STATUS_MAP = {
    "new": ORDER_NEW,
    "accepted": ORDER_ACCEPTED,
    "pending_new": ORDER_ACCEPTED,
    "accepted_for_bidding": ORDER_ACCEPTED,
    "stopped": ORDER_ACCEPTED,
    "suspended": ORDER_ACCEPTED,
    "calculated": ORDER_ACCEPTED,
    "partially_filled": ORDER_PARTIALLY_FILLED,
    "filled": ORDER_FILLED,
    "canceled": ORDER_CANCELLED,
    "cancelled": ORDER_CANCELLED,
    "expired": ORDER_EXPIRED,
    "rejected": ORDER_REJECTED,
    "done_for_day": ORDER_EXPIRED,
    "replaced": ORDER_CANCELLED,
    "pending_cancel": ORDER_ACCEPTED,
    "pending_replace": ORDER_ACCEPTED,
}


def _as_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_utc(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _map_status(raw: Any) -> str:
    key = str(raw or "").strip().lower()
    if not key:
        return ORDER_UNKNOWN
    return _STATUS_MAP.get(key, ORDER_UNKNOWN)


def map_order_status(raw: Any) -> str:
    """Public alias used by the live pilot executor."""
    return _map_status(raw)


def _parse_account(raw: Any) -> AccountView | None:
    if not isinstance(raw, dict):
        return None
    equity = _as_float(raw.get("equity"))
    buying_power = _as_float(raw.get("buying_power"))
    cash = _as_float(raw.get("cash"))
    if equity is None or buying_power is None:
        return None
    status = str(raw.get("status") or "ACTIVE").strip()
    # Alpaca does not always expose day/week/peak marks; leave None → risk blocks.
    return AccountView(
        available=status.upper() in {"ACTIVE", "OK", "GOOD", "APPROVED"},
        status=status,
        buying_power=buying_power,
        equity=equity,
        day_start_equity=_as_float(raw.get("last_equity") or raw.get("day_start_equity")),
        week_start_equity=_as_float(raw.get("week_start_equity")),
        peak_equity=_as_float(raw.get("peak_equity")),
        cash=cash,
    )


def _parse_positions(raw: Any) -> tuple[PositionView, ...]:
    rows = raw if isinstance(raw, list) else []
    out: list[PositionView] = []
    for item in rows:
        if not isinstance(item, dict):
            continue
        symbol = str(item.get("symbol") or "").strip().upper()
        qty = _as_float(item.get("qty") if item.get("qty") is not None else item.get("qty_available"))
        if not symbol or qty is None:
            continue
        out.append(PositionView(symbol=symbol, qty=qty))
    return tuple(out)


def _parse_orders(raw: Any) -> tuple[OpenOrderView, ...]:
    rows = raw if isinstance(raw, list) else []
    out: list[OpenOrderView] = []
    for item in rows:
        if not isinstance(item, dict):
            continue
        symbol = str(item.get("symbol") or "").strip().upper()
        side = str(item.get("side") or "").strip().lower()
        client_id = str(
            item.get("client_order_id")
            or item.get("clientOrderId")
            or item.get("id")
            or ""
        ).strip()
        status = _map_status(item.get("status"))
        qty = _as_float(item.get("qty") if item.get("qty") is not None else item.get("filled_qty"))
        if not symbol:
            continue
        out.append(
            OpenOrderView(
                client_order_id=client_id,
                symbol=symbol,
                side=side or "unknown",
                status=status,
                qty=qty,
            )
        )
    return tuple(out)


def _parse_quote(symbol: str, raw: Any) -> QuoteView | None:
    if not isinstance(raw, dict):
        return None
    # Alpaca latest quote may nest under "quote"
    body = raw.get("quote") if isinstance(raw.get("quote"), dict) else raw
    if not isinstance(body, dict):
        return None
    bid = _as_float(body.get("bp") if body.get("bp") is not None else body.get("bid"))
    ask = _as_float(body.get("ap") if body.get("ap") is not None else body.get("ask"))
    last = _as_float(body.get("p") if body.get("p") is not None else body.get("last"))
    quote_time = _as_utc(
        body.get("t")
        or body.get("timestamp")
        or body.get("quote_time")
        or raw.get("t")
    )
    if bid is None and ask is None and last is None:
        return None
    return QuoteView(
        symbol=symbol.upper(),
        bid=bid,
        ask=ask,
        quote_time=quote_time,
        last=last,
    )


def normalize_snapshot(
    raw: Mapping[str, Any] | Any,
    *,
    now: datetime,
    data_bar_start: datetime | None,
) -> BrokerState:
    """Map a reader snapshot. Missing equity/BP → known=False."""
    if not isinstance(raw, dict):
        return BrokerState(
            known=False,
            detail="live snapshot is not an object",
            data_bar_start=data_bar_start,
            now=now,
            order_history_complete=False,
        )
    account = _parse_account(raw.get("account"))
    if account is None:
        return BrokerState(
            known=False,
            detail="live account equity or buying power missing",
            data_bar_start=data_bar_start,
            now=now,
            order_history_complete=False,
        )
    positions = _parse_positions(raw.get("positions"))
    orders = _parse_orders(raw.get("orders"))
    quotes_raw = raw.get("quotes") if isinstance(raw.get("quotes"), dict) else {}
    quotes: list[QuoteView] = []
    for symbol, payload in quotes_raw.items():
        parsed = _parse_quote(str(symbol), payload)
        if parsed is not None:
            quotes.append(parsed)
    known_ids = frozenset(
        order.client_order_id for order in orders if order.client_order_id
    )
    return BrokerState(
        known=True,
        account=account,
        positions=positions,
        open_orders=orders,
        known_client_order_ids=known_ids,
        orders_today=0,  # filled from claim/audit by the runner when available
        quote=None,
        data_bar_start=data_bar_start,
        now=now,
        detail="alpaca live snapshot",
        quotes=tuple(quotes),
        order_history_complete=True,
    )
