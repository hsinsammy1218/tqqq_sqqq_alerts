"""Robinhood risk gate. New exposure fails closed on any unknown input.

Risk-reducing sells are still described when the symbol and action are valid,
even if a buy would be blocked. Nothing in this module submits an order.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone

from brokers.types import (
    ORDER_FILLED,
    BrokerState,
    PositionView,
    QuoteView,
    RobinhoodLimits,
    TradeIntent,
    UnsafeBrokerConfiguration,
)
from market_data_gate import assess_intraday_freshness

ALLOWED_SYMBOLS = frozenset({"TQQQ", "SQQQ"})
ALLOWED_ACTIONS = frozenset({"BUY", "SELL"})
_ACTIVE_ACCOUNT = frozenset({"active", "ok", "good"})


def _as_utc(ts: datetime | None) -> datetime | None:
    if ts is None:
        return None
    if ts.tzinfo is None:
        return ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)


def execution_symbol_block_reason(symbol: str) -> str | None:
    text = (symbol or "").strip().upper()
    if text in ALLOWED_SYMBOLS:
        return None
    if not text:
        return "malformed symbol"
    if text == "QQQ":
        return "QQQ is the signal asset and cannot be executed"
    if "-" in text or text.endswith("USD"):
        return f"crypto is rejected ({text})"
    if " " in text or any(ch.isdigit() for ch in text):
        return f"options are rejected ({text})"
    return f"symbol {text} is not on the Robinhood allowlist"


def action_block_reason(action: str) -> str | None:
    text = (action or "").strip().upper()
    if text in ALLOWED_ACTIONS:
        return None
    if text == "FLIP":
        return "FLIP is not a Robinhood action; split it into SELL then BUY"
    return f"unsupported action {action!r}"


def _optional_pct(name: str, raw: str | None) -> float | None:
    if raw is None or str(raw).strip() == "":
        return None
    try:
        value = float(raw)
    except ValueError as exc:
        raise UnsafeBrokerConfiguration(f"{name} must be a fraction.") from exc
    if value <= 0 or value > 1:
        raise UnsafeBrokerConfiguration(
            f"{name} must be a fraction in (0, 1]. {value} would not be a safe percent."
        )
    return value


def _optional_money(name: str, raw: str | None) -> float | None:
    if raw is None or str(raw).strip() == "":
        return None
    try:
        value = float(raw)
    except ValueError as exc:
        raise UnsafeBrokerConfiguration(f"{name} must be a number of dollars.") from exc
    if value <= 0:
        raise UnsafeBrokerConfiguration(f"{name} must be positive when set.")
    return value


def _optional_count(name: str, raw: str | None) -> int | None:
    if raw is None or str(raw).strip() == "":
        return None
    try:
        value = int(raw)
    except ValueError as exc:
        raise UnsafeBrokerConfiguration(f"{name} must be an integer.") from exc
    if value <= 0:
        raise UnsafeBrokerConfiguration(f"{name} must be positive when set.")
    return value


def _flag(raw: str | None) -> bool:
    return (raw or "").strip().lower() in {"1", "true", "yes", "on"}


def limits_from_env(env: dict[str, str] | None = None) -> RobinhoodLimits:
    source = os.environ if env is None else env
    quote_age = float(source.get("ROBINHOOD_MAX_QUOTE_AGE_SECONDS", "15") or "15")
    spread = float(source.get("ROBINHOOD_MAX_SPREAD_BPS", "25") or "25")
    data_age = float(source.get("ROBINHOOD_MAX_DATA_AGE_MINUTES", "90") or "90")
    return RobinhoodLimits(
        max_position_pct=_optional_pct(
            "ROBINHOOD_MAX_POSITION_PCT", source.get("ROBINHOOD_MAX_POSITION_PCT")
        ),
        max_order_notional=_optional_money(
            "ROBINHOOD_MAX_ORDER_NOTIONAL", source.get("ROBINHOOD_MAX_ORDER_NOTIONAL")
        ),
        max_daily_loss_pct=_optional_pct(
            "ROBINHOOD_MAX_DAILY_LOSS_PCT", source.get("ROBINHOOD_MAX_DAILY_LOSS_PCT")
        ),
        max_weekly_loss_pct=_optional_pct(
            "ROBINHOOD_MAX_WEEKLY_LOSS_PCT", source.get("ROBINHOOD_MAX_WEEKLY_LOSS_PCT")
        ),
        max_drawdown_pct=_optional_pct(
            "ROBINHOOD_MAX_DRAWDOWN_PCT", source.get("ROBINHOOD_MAX_DRAWDOWN_PCT")
        ),
        max_orders_per_day=_optional_count(
            "ROBINHOOD_MAX_ORDERS_PER_DAY", source.get("ROBINHOOD_MAX_ORDERS_PER_DAY")
        ),
        max_quote_age_seconds=quote_age,
        max_spread_bps=spread,
        max_data_age_minutes=data_age,
        new_entries_enabled=_flag(source.get("ROBINHOOD_NEW_ENTRIES_ENABLED")),
        live_enabled_flag=_flag(source.get("ROBINHOOD_LIVE_ENABLED")),
    )


def quote_for(state: BrokerState, symbol: str) -> QuoteView | None:
    """Quote for this symbol. A quote for a different symbol is not reused."""
    wanted = (symbol or "").upper()
    for quote in state.quotes:
        if quote.symbol.upper() == wanted:
            return quote
    quote = state.quote
    if quote is not None and quote.symbol.upper() == wanted:
        return quote
    return None


def _position_qty(positions: tuple[PositionView, ...], symbol: str) -> float:
    total = 0.0
    for row in positions:
        if row.symbol.upper() == symbol:
            total += float(row.qty)
    return total


def _spread_bps(bid: float, ask: float) -> float | None:
    if bid <= 0 or ask <= 0 or ask < bid:
        return None
    mid = (bid + ask) / 2.0
    if mid <= 0:
        return None
    return (ask - bid) / mid * 10_000.0


def _loss_pct(start: float | None, equity: float | None) -> float | None:
    if start is None or equity is None or start <= 0:
        return None
    return max(0.0, (start - equity) / start)


def check_new_exposure(
    intent: TradeIntent,
    state: BrokerState,
    limits: RobinhoodLimits,
) -> tuple[bool, str, tuple[tuple[str, str], ...]]:
    """Return (allowed, reason, named checks). BUY fails closed. SELL is separate."""
    checks: list[tuple[str, str]] = []

    def add(name: str, ok: bool) -> None:
        checks.append((name, "PASS" if ok else "FAIL"))

    symbol_reason = execution_symbol_block_reason(intent.execution_symbol)
    action_reason = action_block_reason(intent.action)
    add("Symbol", symbol_reason is None)
    add("Action", action_reason is None)
    if symbol_reason or action_reason:
        return False, symbol_reason or action_reason or "rejected", tuple(checks)
    if intent.action != "BUY":
        return False, "check_new_exposure is for BUY only", tuple(checks)

    if not state.known or state.account is None:
        add("Account", False)
        add("Broker state", False)
        return False, "unknown broker state; new exposure blocked", tuple(checks)

    account = state.account
    account_ok = bool(account.available) and account.status.strip().lower() in _ACTIVE_ACCOUNT
    add("Account", account_ok)
    if not account_ok:
        return False, f"account status {account.status!r} is not usable", tuple(checks)

    now = _as_utc(state.now) or datetime.now(timezone.utc)
    freshness = assess_intraday_freshness(
        state.data_bar_start,
        now,
        max_age_minutes=limits.max_data_age_minutes,
    )
    data_ok = not freshness.blocks_new_exposure()
    add("Market data", data_ok)
    if not data_ok:
        return False, freshness.reason, tuple(checks)

    quote = quote_for(state, intent.execution_symbol)
    quote_ok = False
    quote_reason = "quote missing"
    reference: float | None = None
    if quote is None or quote.symbol.upper() != intent.execution_symbol.upper():
        quote_reason = "quote missing for execution symbol"
    elif quote.bid is None or quote.ask is None or quote.quote_time is None:
        quote_reason = "quote is incomplete"
    else:
        age = (now - _as_utc(quote.quote_time)).total_seconds()  # type: ignore[operator]
        if age > limits.max_quote_age_seconds or age < -1:
            quote_reason = f"stale quote ({age:.1f}s)"
        else:
            spread = _spread_bps(float(quote.bid), float(quote.ask))
            if spread is None:
                quote_reason = "quote prices are unusable"
            elif spread > limits.max_spread_bps:
                quote_reason = f"spread {spread:.1f} bps exceeds {limits.max_spread_bps:g}"
                add("Quote", True)
                add("Spread", False)
                return False, quote_reason, tuple(checks)
            else:
                quote_ok = True
                reference = float(quote.ask)
                add("Quote", True)
                add("Spread", True)
    if not quote_ok:
        add("Quote", False)
        return False, quote_reason, tuple(checks)

    both = _position_qty(state.positions, "TQQQ") > 0 and _position_qty(state.positions, "SQQQ") > 0
    add("Single ETF book", not both)
    if both:
        return False, "broker holds both TQQQ and SQQQ", tuple(checks)

    open_conflict = [
        order
        for order in state.open_orders
        if order.symbol.upper() in ALLOWED_SYMBOLS and order.status.upper() != ORDER_FILLED
    ]
    add("Open orders", not open_conflict)
    if open_conflict:
        return False, "conflicting open order; new exposure blocked", tuple(checks)

    if intent.client_order_id in state.known_client_order_ids:
        add("Duplicate intent", False)
        return False, "duplicate client_order_id; not creating another order", tuple(checks)
    add("Duplicate intent", True)

    if limits.max_orders_per_day is None:
        add("Order count", False)
        return False, "ROBINHOOD_MAX_ORDERS_PER_DAY is unset", tuple(checks)
    if state.orders_today >= limits.max_orders_per_day:
        add("Order count", False)
        return False, "max orders per day reached", tuple(checks)
    add("Order count", True)

    if limits.max_order_notional is None or limits.max_position_pct is None:
        add("Position limit", False)
        return False, "position or order notional limit is unset", tuple(checks)
    if account.equity is None or account.equity <= 0 or account.buying_power is None:
        add("Buying power", False)
        return False, "equity or buying power is unknown", tuple(checks)

    cap = min(float(limits.max_order_notional), float(account.equity) * float(limits.max_position_pct))
    if reference is None or reference <= 0:
        add("Price", False)
        return False, "estimated price is missing", tuple(checks)
    quantity = int(cap // reference)
    if quantity < 1:
        add("Position limit", False)
        return False, "notional cap buys zero shares", tuple(checks)
    notional = round(quantity * reference, 2)
    if notional > float(limits.max_order_notional) + 0.01:
        add("Position limit", False)
        return False, "order notional exceeds ROBINHOOD_MAX_ORDER_NOTIONAL", tuple(checks)
    if account.buying_power + 1e-6 < notional:
        add("Buying power", False)
        return False, "insufficient buying power", tuple(checks)
    add("Buying power", True)
    add("Position limit", True)

    daily = _loss_pct(account.day_start_equity, account.equity)
    weekly = _loss_pct(account.week_start_equity, account.equity)
    drawdown = _loss_pct(account.peak_equity, account.equity)
    if limits.max_daily_loss_pct is None or daily is None:
        add("Daily loss", False)
        return False, "daily loss limit or day-start equity is unset", tuple(checks)
    if daily >= limits.max_daily_loss_pct:
        add("Daily loss", False)
        return False, "daily loss lock", tuple(checks)
    add("Daily loss", True)
    if limits.max_weekly_loss_pct is None or weekly is None:
        add("Weekly loss", False)
        return False, "weekly loss limit or week-start equity is unset", tuple(checks)
    if weekly >= limits.max_weekly_loss_pct:
        add("Weekly loss", False)
        return False, "weekly loss lock", tuple(checks)
    add("Weekly loss", True)
    if limits.max_drawdown_pct is None or drawdown is None:
        add("Drawdown", False)
        return False, "drawdown limit or peak equity is unset", tuple(checks)
    if drawdown >= limits.max_drawdown_pct:
        add("Drawdown", False)
        return False, "drawdown lock", tuple(checks)
    add("Drawdown", True)

    if intent.strategy_version.strip() == "":
        add("Strategy version", False)
        return False, "strategy version is unknown", tuple(checks)
    add("Strategy version", True)

    if not limits.new_entries_enabled:
        add("New entries", False)
        return False, "ROBINHOOD_NEW_ENTRIES_ENABLED is false", tuple(checks)
    add("New entries", True)

    if intent.quantity != quantity:
        # Caller may pass 0 as "size me". The gate reports the sized quantity via reason.
        pass
    return True, f"sized {quantity} @ {reference:.2f} notional {notional:.2f}", tuple(checks)


def size_buy(intent_price: float, state: BrokerState, limits: RobinhoodLimits) -> tuple[int, float]:
    account = state.account
    if account is None or account.equity is None or limits.max_order_notional is None or limits.max_position_pct is None:
        return 0, 0.0
    cap = min(float(limits.max_order_notional), float(account.equity) * float(limits.max_position_pct))
    if intent_price <= 0:
        return 0, 0.0
    quantity = int(cap // intent_price)
    return quantity, round(quantity * intent_price, 2)
