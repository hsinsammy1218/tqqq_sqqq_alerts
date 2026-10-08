"""Risk + arming for Robinhood host-mediated pilot.

Extends the Case C connected-shadow risk rules with capital ceiling, cash-only
sizing, and multi-key kill switches (default FALSE). Nothing here submits.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone

from brokers.types import (
    ORDER_FILLED,
    BrokerState,
    PositionView,
    QuoteView,
    TradeIntent,
    UnsafeBrokerConfiguration,
)
from market_data_gate import assess_intraday_freshness
from robinhood_account_isolation import (
    AccountIsolationError,
    BoundAgenticAccount,
    ENV_BOUND_ACCOUNT,
    env_account_pin,
    require_bound_account,
)
from robinhood_risk import (
    ALLOWED_ACTIONS,
    ALLOWED_SYMBOLS,
    action_block_reason,
    execution_symbol_block_reason,
    quote_for,
)

_ACTIVE_ACCOUNT = frozenset({"active", "ok", "good"})


@dataclass(frozen=True)
class RobinhoodHostLimits:
    max_position_pct: float | None
    max_order_notional: float | None
    capital_ceiling: float | None
    max_daily_loss_pct: float | None
    max_weekly_loss_pct: float | None
    max_drawdown_pct: float | None
    max_orders_per_day: int | None
    max_quote_age_seconds: float
    max_spread_bps: float
    max_data_age_minutes: float
    new_entries_enabled: bool
    host_enabled_flag: bool
    live_submission_flag: bool
    allow_fractional: bool
    block_borrowed_funds: bool
    max_slippage_bps: float = 25.0


def _as_utc(ts: datetime | None) -> datetime | None:
    if ts is None:
        return None
    if ts.tzinfo is None:
        return ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)


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


def host_arming_block_reason(
    env: Mapping[str, str] | None,
    *,
    submission_implemented: bool,
    host_executor: bool,
    execution_broker: str | None = None,
    require_new_entries: bool = False,
) -> str | None:
    """Return a block reason unless host-pilot arming keys are true.

    ``ROBINHOOD_HOST_NEW_ENTRIES_ENABLED`` is **not** required for arming by
    default: when false it blocks BUY exposure in ``check_host_new_exposure``
    but still allows risk-reducing SELL. Pass ``require_new_entries=True`` only
    when the caller is specifically gating a new-entry path before risk.

    Render handoff never calls this with host_executor=True. Case C unattended
    paths must stay blocked even if env flags are mistakenly set.
    """
    source: Mapping[str, str] = os.environ if env is None else env
    if not host_executor:
        return "ROBINHOOD_HOST_EXECUTOR is not true; refusing host submission"
    if not _flag(source.get("ROBINHOOD_HOST_EXECUTOR")):
        return "ROBINHOOD_HOST_EXECUTOR is not true"
    broker = (execution_broker or source.get("EXECUTION_BROKER") or "").strip().lower()
    # Host CLI may run without EXECUTION_BROKER (handoff already wrote intents).
    if broker and broker not in {"", "robinhood_host_handoff", "robinhood_host_pilot"}:
        return f"EXECUTION_BROKER={broker!r} is not a Robinhood host path"
    if not submission_implemented:
        return "ROBINHOOD_HOST_SUBMISSION_IMPLEMENTED is false; refusing place_equity_order"
    if not _flag(source.get("ROBINHOOD_HOST_ENABLED")):
        return "ROBINHOOD_HOST_ENABLED is not true"
    if require_new_entries and not _flag(source.get("ROBINHOOD_HOST_NEW_ENTRIES_ENABLED")):
        return "ROBINHOOD_HOST_NEW_ENTRIES_ENABLED is not true"
    if not _flag(source.get("ROBINHOOD_HOST_LIVE_SUBMISSION")):
        return "ROBINHOOD_HOST_LIVE_SUBMISSION is not true"
    if not _flag(source.get("LIVE_TRADING_ENABLED")):
        return "LIVE_TRADING_ENABLED is not true"
    # Refuse unattended Render as the submitter even if someone arms env vars.
    if _flag(source.get("RENDER")) or (source.get("RENDER_SERVICE_TYPE") or "").strip():
        return "Render runtime cannot submit Robinhood orders under Case C"
    return None


def host_limits_from_env(env: dict[str, str] | None = None) -> RobinhoodHostLimits:
    source = os.environ if env is None else env
    quote_age = float(source.get("ROBINHOOD_HOST_MAX_QUOTE_AGE_SECONDS", "15") or "15")
    spread = float(source.get("ROBINHOOD_HOST_MAX_SPREAD_BPS", "25") or "25")
    data_age = float(source.get("ROBINHOOD_HOST_MAX_DATA_AGE_MINUTES", "90") or "90")
    return RobinhoodHostLimits(
        max_position_pct=_optional_pct(
            "ROBINHOOD_HOST_MAX_POSITION_PCT", source.get("ROBINHOOD_HOST_MAX_POSITION_PCT")
        ),
        max_order_notional=_optional_money(
            "ROBINHOOD_HOST_MAX_ORDER_NOTIONAL", source.get("ROBINHOOD_HOST_MAX_ORDER_NOTIONAL")
        ),
        capital_ceiling=_optional_money(
            "ROBINHOOD_HOST_CAPITAL_CEILING", source.get("ROBINHOOD_HOST_CAPITAL_CEILING")
        ),
        max_daily_loss_pct=_optional_pct(
            "ROBINHOOD_HOST_MAX_DAILY_LOSS_PCT", source.get("ROBINHOOD_HOST_MAX_DAILY_LOSS_PCT")
        ),
        max_weekly_loss_pct=_optional_pct(
            "ROBINHOOD_HOST_MAX_WEEKLY_LOSS_PCT", source.get("ROBINHOOD_HOST_MAX_WEEKLY_LOSS_PCT")
        ),
        max_drawdown_pct=_optional_pct(
            "ROBINHOOD_HOST_MAX_DRAWDOWN_PCT", source.get("ROBINHOOD_HOST_MAX_DRAWDOWN_PCT")
        ),
        max_orders_per_day=_optional_count(
            "ROBINHOOD_HOST_MAX_ORDERS_PER_DAY", source.get("ROBINHOOD_HOST_MAX_ORDERS_PER_DAY")
        ),
        max_quote_age_seconds=quote_age,
        max_spread_bps=spread,
        max_data_age_minutes=data_age,
        new_entries_enabled=_flag(source.get("ROBINHOOD_HOST_NEW_ENTRIES_ENABLED")),
        host_enabled_flag=_flag(source.get("ROBINHOOD_HOST_ENABLED")),
        live_submission_flag=_flag(source.get("ROBINHOOD_HOST_LIVE_SUBMISSION")),
        allow_fractional=_flag(source.get("ROBINHOOD_HOST_ALLOW_FRACTIONAL")),
        block_borrowed_funds=True,
        max_slippage_bps=float(source.get("ROBINHOOD_HOST_MAX_SLIPPAGE_BPS", "25") or "25"),
    )


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


def require_agentic_bound(
    env: Mapping[str, str] | None = None,
    *,
    get_accounts_payload: object | None = None,
    allow_unverified_env_pin: bool = False,
) -> BoundAgenticAccount:
    """Fail closed unless Agentic is live-resolved (or unverified pin if allowed)."""
    try:
        return require_bound_account(
            get_accounts_payload=get_accounts_payload,
            env=env,
            allow_unverified_env_pin=allow_unverified_env_pin,
        )
    except AccountIsolationError as exc:
        raise UnsafeBrokerConfiguration(str(exc)) from exc


def check_handoff_prevalidation(
    intent: TradeIntent,
    *,
    action: str,
    env: Mapping[str, str] | None = None,
) -> tuple[bool, str, tuple[tuple[str, str], ...]]:
    """Render pre-handoff checks. Does **not** require known broker state.

    PENDING persistence is not execution approval. Host must still run
    ``check_host_new_exposure`` (BUY) or sell sizing against a fresh read.
    When ``env`` is provided, also require ``ROBINHOOD_AGENTIC_ACCOUNT_NUMBER``.
    """
    checks: list[tuple[str, str]] = []

    def add(name: str, ok: bool) -> None:
        checks.append((name, "PASS" if ok else "FAIL"))

    symbol_reason = execution_symbol_block_reason(intent.execution_symbol)
    action_reason = action_block_reason(action)
    add("Symbol", symbol_reason is None)
    add("Action", action_reason is None)
    if symbol_reason or action_reason:
        return False, symbol_reason or action_reason or "rejected", tuple(checks)
    if intent.signal_symbol.strip().upper() != "QQQ":
        add("Signal", False)
        return False, "signal symbol must be QQQ", tuple(checks)
    add("Signal", True)
    if intent.strategy_version.strip() == "":
        add("Strategy version", False)
        return False, "strategy version is unknown", tuple(checks)
    add("Strategy version", True)
    if env is not None:
        if not str(env.get(ENV_BOUND_ACCOUNT) or "").strip():
            add("Agentic account", False)
            return False, f"{ENV_BOUND_ACCOUNT} unset; refusing handoff", tuple(checks)
        try:
            # Case C: validate env pin shape only — host must live-resolve before place.
            env_account_pin(env)
        except AccountIsolationError as exc:
            add("Agentic account", False)
            return False, str(exc), tuple(checks)
        add("Agentic account", True)
    add("Broker state", True)  # unknown is OK for PENDING; host revalidates
    add("Pending≠approved", True)
    return True, "pre-handoff ok; host must revalidate before place", tuple(checks)


def check_host_new_exposure(
    intent: TradeIntent,
    state: BrokerState,
    limits: RobinhoodHostLimits,
) -> tuple[bool, str, tuple[tuple[str, str], ...]]:
    """BUY fails closed. Equity above capital ceiling blocks (no silent size-down).

    Requires known/fresh broker state. Render PENDING creation must use
    ``check_handoff_prevalidation`` instead — PENDING ≠ approved.
    """
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
        return False, "check_host_new_exposure is for BUY only", tuple(checks)
    if intent.signal_symbol.strip().upper() != "QQQ":
        add("Signal", False)
        return False, "signal symbol must be QQQ", tuple(checks)
    add("Signal", True)

    if not state.known or state.account is None:
        add("Account", False)
        add("Broker state", False)
        return False, "unknown broker state; new exposure blocked", tuple(checks)

    account = state.account
    account_ok = bool(account.available) and account.status.strip().lower() in _ACTIVE_ACCOUNT
    add("Account", account_ok)
    if not account_ok:
        return False, f"account status {account.status!r} is not usable", tuple(checks)

    if limits.capital_ceiling is None:
        add("Capital ceiling", False)
        return False, "ROBINHOOD_HOST_CAPITAL_CEILING is unset", tuple(checks)
    if account.equity is None or account.equity <= 0:
        add("Capital ceiling", False)
        return False, "equity is unknown", tuple(checks)
    if float(account.equity) > float(limits.capital_ceiling) + 1e-9:
        add("Capital ceiling", False)
        return False, (
            f"equity {account.equity:.2f} exceeds capital ceiling "
            f"{limits.capital_ceiling:.2f}; new exposure blocked"
        ), tuple(checks)
    add("Capital ceiling", True)

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
                add("Quote", True)
                add("Spread", False)
                return False, f"spread {spread:.1f} bps exceeds {limits.max_spread_bps:g}", tuple(checks)
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

    held_other = [
        sym
        for sym in ALLOWED_SYMBOLS
        if sym != intent.execution_symbol.upper() and _position_qty(state.positions, sym) > 0
    ]
    add("One position", not held_other)
    if held_other:
        return False, f"already holding {held_other[0]}; one position only", tuple(checks)

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
        return False, "ROBINHOOD_HOST_MAX_ORDERS_PER_DAY is unset", tuple(checks)
    if state.orders_today >= limits.max_orders_per_day:
        add("Order count", False)
        return False, "max orders per day reached", tuple(checks)
    add("Order count", True)

    if limits.max_order_notional is None or limits.max_position_pct is None:
        add("Position limit", False)
        return False, "position or order notional limit is unset", tuple(checks)

    cash = account.cash
    if cash is None:
        # Prefer cash when present; fall back to buying_power only if equal-or-less
        # conservative than equity and ceiling (no intentional margin).
        cash = account.buying_power
    if cash is None or account.buying_power is None:
        add("Buying power", False)
        return False, "cash or buying power is unknown", tuple(checks)
    if limits.block_borrowed_funds and account.buying_power is not None and cash is not None:
        if float(account.buying_power) > float(cash) + 0.01:
            # Inflated BP vs cash → size from cash only.
            pass

    if reference is None or reference <= 0:
        add("Price", False)
        return False, "estimated price is missing", tuple(checks)

    budget = min(
        float(limits.max_order_notional),
        float(account.equity) * float(limits.max_position_pct),
        float(limits.capital_ceiling),
        float(cash),
    )
    quantity = int(budget // reference)
    if not limits.allow_fractional and quantity < 1:
        add("Position limit", False)
        return False, "notional cap buys zero whole shares", tuple(checks)
    notional = round(quantity * reference, 2)
    if notional > float(cash) + 0.01:
        add("Buying power", False)
        return False, "insufficient cash; margin sizing refused", tuple(checks)
    if notional > float(limits.max_order_notional) + 0.01:
        add("Position limit", False)
        return False, "order notional exceeds ROBINHOOD_HOST_MAX_ORDER_NOTIONAL", tuple(checks)
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
        return False, "ROBINHOOD_HOST_NEW_ENTRIES_ENABLED is false", tuple(checks)
    add("New entries", True)

    if not limits.host_enabled_flag:
        add("Host enabled", False)
        return False, "ROBINHOOD_HOST_ENABLED is false", tuple(checks)
    add("Host enabled", True)

    return True, f"sized {quantity} @ {reference:.2f} notional {notional:.2f}", tuple(checks)


def size_host_buy(
    intent_price: float,
    state: BrokerState,
    limits: RobinhoodHostLimits,
) -> tuple[int, float]:
    account = state.account
    if (
        account is None
        or account.equity is None
        or limits.max_order_notional is None
        or limits.max_position_pct is None
        or limits.capital_ceiling is None
    ):
        return 0, 0.0
    if float(account.equity) > float(limits.capital_ceiling) + 1e-9:
        return 0, 0.0
    cash = account.cash if account.cash is not None else account.buying_power
    if cash is None or intent_price <= 0:
        return 0, 0.0
    budget = min(
        float(limits.max_order_notional),
        float(account.equity) * float(limits.max_position_pct),
        float(limits.capital_ceiling),
        float(cash),
    )
    quantity = int(budget // intent_price)
    return quantity, round(quantity * intent_price, 2)


def sell_qty_allowed(state: BrokerState, symbol: str, requested: int) -> tuple[int, str | None]:
    if not state.known:
        return 0, "unknown broker state; sell blocked"
    held = _position_qty(state.positions, symbol.upper())
    if held <= 0:
        return 0, "no broker position to sell"
    qty = min(int(requested), int(held))
    if qty < 1:
        return 0, "sell quantity rounds to zero whole shares"
    return qty, None


# Re-export for callers / tests
__all__ = [
    "ALLOWED_ACTIONS",
    "ALLOWED_SYMBOLS",
    "QuoteView",
    "RobinhoodHostLimits",
    "action_block_reason",
    "check_handoff_prevalidation",
    "check_host_new_exposure",
    "execution_symbol_block_reason",
    "host_arming_block_reason",
    "host_limits_from_env",
    "quote_for",
    "sell_qty_allowed",
    "size_host_buy",
]
