"""Map Robinhood read payloads into BrokerState.

Schemas are not published. Parsing accepts a short alias list and refuses to
invent prices, buying power, or equity. Unrecognized required payloads mark
the broker unknown so new exposure stays blocked.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from brokers.types import (
    ORDER_ACCEPTED,
    ORDER_CANCELLED,
    ORDER_EXPIRED,
    ORDER_FILLED,
    ORDER_NEW,
    ORDER_PARTIALLY_FILLED,
    ORDER_REJECTED,
    ORDER_STATUSES,
    ORDER_UNKNOWN,
    AccountView,
    BrokerState,
    OpenOrderView,
    PositionView,
    QuoteView,
)

_OPEN = frozenset({ORDER_NEW, ORDER_ACCEPTED, ORDER_PARTIALLY_FILLED, ORDER_UNKNOWN})
_STATUS_MAP = {
    "new": ORDER_NEW,
    "open": ORDER_NEW,
    "pending": ORDER_NEW,
    "pending_review": ORDER_NEW,
    "accepted": ORDER_ACCEPTED,
    "queued": ORDER_ACCEPTED,
    "partially_filled": ORDER_PARTIALLY_FILLED,
    "partial": ORDER_PARTIALLY_FILLED,
    "filled": ORDER_FILLED,
    "cancelled": ORDER_CANCELLED,
    "canceled": ORDER_CANCELLED,
    "rejected": ORDER_REJECTED,
    "expired": ORDER_EXPIRED,
    "unknown": ORDER_UNKNOWN,
}


def _finite(value: Any) -> float | None:
    if isinstance(value, bool) or value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return number


def _num(row: dict[str, Any], *keys: str) -> float | None:
    for key in keys:
        if key in row and row[key] not in (None, ""):
            return _finite(row[key])
    return None


def _text(row: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = row.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _when(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _time_in(row: dict[str, Any], *keys: str) -> datetime | None:
    for key in keys:
        if key in row:
            return _when(row.get(key))
    return None


def _rows(payload: Any, wrappers: tuple[str, ...], hints: tuple[str, ...]) -> tuple[list[dict[str, Any]], bool]:
    """Return (rows, recognized). An empty list is a recognized empty result."""
    if payload is None:
        return [], True
    if isinstance(payload, list):
        if all(isinstance(item, dict) for item in payload):
            return list(payload), True
        return [], False
    if not isinstance(payload, dict):
        return [], False
    for key in wrappers:
        if key in payload:
            inner = payload[key]
            if isinstance(inner, list) and all(isinstance(item, dict) for item in inner):
                return list(inner), True
            return [], False
    if not payload:
        return [], True
    hinted: list[dict[str, Any]] = []
    for key, value in payload.items():
        if isinstance(value, dict) and any(hint in value for hint in hints):
            row = dict(value)
            row.setdefault("symbol", key)
            hinted.append(row)
    if hinted:
        return hinted, True
    if any(hint in payload for hint in hints):
        return [payload], True
    return [], False


def _first_mapping(payload: Any, wrappers: tuple[str, ...]) -> tuple[dict[str, Any], bool]:
    rows, ok = _rows(payload, wrappers, ("status", "account_status", "buying_power", "buyingPower", "equity"))
    if not ok:
        return {}, False
    if rows:
        return rows[0], True
    if isinstance(payload, dict):
        return payload, True
    return {}, True


def _map_status(raw: str) -> str:
    key = raw.strip().lower().replace("-", "_").replace(" ", "_")
    mapped = _STATUS_MAP.get(key, ORDER_UNKNOWN if key else ORDER_UNKNOWN)
    if mapped not in ORDER_STATUSES:
        return ORDER_UNKNOWN
    return mapped


def normalize_snapshot(
    payloads: dict[str, Any],
    *,
    now: datetime,
    data_bar_start: datetime | None,
) -> BrokerState:
    """Build a BrokerState. ``known=False`` when equity, buying power, or a book is unreadable."""
    problems: list[str] = []
    account_row, account_ok = _first_mapping(
        payloads.get("get_accounts"),
        ("accounts", "results", "items", "data"),
    )
    portfolio_row, portfolio_ok = _first_mapping(
        payloads.get("get_portfolio"),
        ("portfolio", "results", "data"),
    )
    if not account_ok or not portfolio_ok:
        problems.append("account payload unrecognized")

    equity = _num(portfolio_row, "equity", "total_value", "totalValue", "portfolio_value", "portfolioValue")
    if equity is None:
        equity = _num(account_row, "equity", "total_value", "totalValue", "portfolio_value")
    buying_power = _num(portfolio_row, "buying_power", "buyingPower", "available_buying_power")
    if buying_power is None:
        buying_power = _num(account_row, "buying_power", "buyingPower")
    cash = _num(portfolio_row, "cash", "cash_available", "cashAvailable")
    if cash is None:
        cash = _num(account_row, "cash", "cash_available")
    day_start = _num(
        portfolio_row,
        "day_start_equity",
        "dayStartEquity",
        "equity_previous_close",
        "previous_close_equity",
    )
    week_start = _num(portfolio_row, "week_start_equity", "weekStartEquity")
    peak = _num(portfolio_row, "peak_equity", "peakEquity", "high_water_equity")
    status = _text(account_row, "status", "account_status", "accountStatus") or "unknown"
    if equity is None or buying_power is None:
        problems.append("equity or buying power missing")

    position_rows, positions_ok = _rows(
        payloads.get("get_equity_positions"),
        ("positions", "results", "items", "data"),
        ("quantity", "qty", "shares", "symbol"),
    )
    positions: list[PositionView] = []
    if not positions_ok:
        problems.append("positions payload unrecognized")
    else:
        for row in position_rows:
            symbol = _text(row, "symbol", "ticker", "instrument").upper()
            if not symbol:
                continue
            qty = _num(row, "quantity", "qty", "shares", "open_quantity")
            if qty is None:
                problems.append(f"quantity missing for {symbol}")
                continue
            positions.append(PositionView(symbol=symbol, qty=qty))

    quote_rows, quotes_ok = _rows(
        payloads.get("get_equity_quotes"),
        ("quotes", "results", "items", "data"),
        ("bid", "bid_price", "bidPrice", "ask", "ask_price", "askPrice"),
    )
    quotes: list[QuoteView] = []
    if not quotes_ok:
        problems.append("quotes payload unrecognized")
    else:
        for row in quote_rows:
            symbol = _text(row, "symbol", "ticker", "instrument").upper()
            bid = _num(row, "bid", "bid_price", "bidPrice")
            ask = _num(row, "ask", "ask_price", "askPrice")
            quote_time = _time_in(row, "quote_time", "quoted_at", "timestamp", "as_of", "updated_at")
            if not symbol or bid is None or ask is None or quote_time is None:
                continue
            last = _num(row, "last", "last_price", "lastPrice")
            quotes.append(QuoteView(symbol=symbol, bid=bid, ask=ask, quote_time=quote_time, last=last))

    order_rows, orders_ok = _rows(
        payloads.get("get_equity_orders"),
        ("orders", "results", "items", "data"),
        ("status", "state", "symbol", "side"),
    )
    open_orders: list[OpenOrderView] = []
    known_ids: set[str] = set()
    orders_today = 0
    history_complete = orders_ok
    today = now.astimezone(timezone.utc).date()
    if not orders_ok:
        problems.append("orders payload unrecognized")
    else:
        for row in order_rows:
            symbol = _text(row, "symbol", "ticker", "instrument").upper()
            status = _map_status(_text(row, "status", "state"))
            side = _text(row, "side", "action").lower()
            qty = _num(row, "quantity", "qty", "shares")
            for key in ("client_order_id", "clientOrderId", "ref_id", "id", "order_id", "orderId"):
                found = _text(row, key)
                if found:
                    known_ids.add(found)
            client_id = _text(row, "client_order_id", "clientOrderId", "ref_id")
            if symbol in {"TQQQ", "SQQQ"} and status in _OPEN:
                open_orders.append(
                    OpenOrderView(
                        client_order_id=client_id,
                        symbol=symbol,
                        side=side,
                        status=status,
                        qty=qty,
                    )
                )
            stamp = _time_in(row, "created_at", "createdAt", "updated_at", "timestamp", "placed_at")
            if symbol in {"TQQQ", "SQQQ"}:
                if stamp is None:
                    history_complete = False
                elif stamp.astimezone(timezone.utc).date() == today:
                    orders_today += 1

    known = not problems
    detail = "; ".join(problems) if problems else "robinhood read normalized"
    return BrokerState(
        known=known,
        account=AccountView(
            available=status.strip().lower() in {"active", "ok", "good"},
            status=status,
            buying_power=buying_power,
            equity=equity,
            day_start_equity=day_start,
            week_start_equity=week_start,
            peak_equity=peak,
            cash=cash,
        ),
        positions=tuple(positions),
        open_orders=tuple(open_orders),
        known_client_order_ids=frozenset(known_ids),
        orders_today=orders_today,
        quote=None,
        quotes=tuple(quotes),
        data_bar_start=data_bar_start,
        now=now,
        detail=detail,
        order_history_complete=history_complete and known,
    )
