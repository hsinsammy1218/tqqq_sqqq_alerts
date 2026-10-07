"""Mandatory post-SELL broker refresh before a FLIP opposite BUY.

Never assume flat from the SELL response alone. Require FILLED sell status
AND a fresh Alpaca snapshot proving the exit symbol is flat, with cash/equity/BP,
no unresolved exit SELL, opposite not already held, and a usable opposite quote.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from alpaca_live_risk import AlpacaLiveLimits, quote_for
from brokers.types import (
    ORDER_ACCEPTED,
    ORDER_CANCELLED,
    ORDER_EXPIRED,
    ORDER_FILLED,
    ORDER_NEW,
    ORDER_PARTIALLY_FILLED,
    ORDER_REJECTED,
    ORDER_UNKNOWN,
    BrokerState,
)
from robinhood_flip import advance_flip

# Alpaca sometimes surfaces SUBMITTED before ACCEPTED; treat as non-fill.
ORDER_SUBMITTED = "SUBMITTED"

_NON_FILL_STATUSES = frozenset(
    {
        "",
        ORDER_NEW,
        ORDER_ACCEPTED,
        ORDER_SUBMITTED,
        ORDER_PARTIALLY_FILLED,
        ORDER_CANCELLED,
        ORDER_REJECTED,
        ORDER_EXPIRED,
        ORDER_UNKNOWN,
        "TIMEOUT",
        "PENDING_NEW",
        "PENDING_CANCEL",
        "PENDING_REPLACE",
        "STOPPED",
        "SUSPENDED",
        "DONE_FOR_DAY",
        "REPLACED",
        "NOT_SUBMITTED",
    }
)


@dataclass(frozen=True)
class FlipRefreshGate:
    allowed: bool
    reason: str
    checks: tuple[tuple[str, str], ...]
    refreshed_state: BrokerState | None = None
    exit_qty_remaining: float | None = None


def _as_utc(ts: datetime | None) -> datetime | None:
    if ts is None:
        return None
    if ts.tzinfo is None:
        return ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)


def _spread_bps(bid: float, ask: float) -> float | None:
    if bid <= 0 or ask <= 0 or ask < bid:
        return None
    mid = (bid + ask) / 2.0
    if mid <= 0:
        return None
    return (ask - bid) / mid * 10_000.0


def _qty(state: BrokerState, symbol: str) -> float:
    target = (symbol or "").upper()
    return sum(row.qty for row in state.positions if row.symbol.upper() == target)


def _open_sells(state: BrokerState, symbol: str) -> list[str]:
    target = (symbol or "").upper()
    bad: list[str] = []
    for order in state.open_orders:
        side = (order.side or "").lower()
        status = (order.status or "").upper()
        if order.symbol.upper() != target:
            continue
        if side != "sell":
            continue
        if status == ORDER_FILLED:
            continue
        bad.append(f"{order.client_order_id}:{status or 'UNKNOWN'}")
    return bad


def require_post_sell_flat_for_flip(
    *,
    exit_status: str | None,
    exit_symbol: str,
    entry_symbol: str,
    refreshed_state: BrokerState | None,
    refresh_error: str | None,
    limits: AlpacaLiveLimits,
    reader_present: bool,
) -> FlipRefreshGate:
    """Fail-closed gate: FILLED sell + fresh broker-confirmed flat + quote.

    Callers must pass a state obtained from a fresh reader snapshot after the
    SELL attempt — not the pre-sell book and not inferred solely from the
    SELL response body.
    """
    checks: list[tuple[str, str]] = []

    def fail(reason: str, *named: tuple[str, bool]) -> FlipRefreshGate:
        for name, ok in named:
            checks.append((name, "PASS" if ok else "FAIL"))
        return FlipRefreshGate(False, reason, tuple(checks), refreshed_state, None)

    status = (exit_status or "").strip().upper()
    if status in _NON_FILL_STATUSES or status != ORDER_FILLED:
        # Reuse advance_flip messaging for known statuses; cover SUBMITTED explicitly.
        if status == ORDER_SUBMITTED:
            return fail(
                "submitted sell is not a fill; opposite buy blocked",
                ("Sell status", False),
            )
        flip = advance_flip(
            exit_status=exit_status,
            exit_qty_remaining=None,
            broker_state_known=True,
        )
        return fail(flip.reason, ("Sell status", False))

    checks.append(("Sell status", "PASS"))

    if not reader_present:
        return fail(
            "FLIP opposite buy requires a fresh Alpaca reader snapshot; reader missing",
            ("Broker refresh", False),
        )
    if refresh_error:
        return fail(
            f"FLIP broker refresh failed; opposite buy blocked ({refresh_error})",
            ("Broker refresh", False),
        )
    if refreshed_state is None:
        return fail(
            "FLIP broker refresh returned no state; opposite buy blocked",
            ("Broker refresh", False),
        )
    if not refreshed_state.known:
        return fail(
            "FLIP broker refresh state unknown; opposite buy blocked",
            ("Broker refresh", False),
            ("Broker state", False),
        )
    checks.append(("Broker refresh", "PASS"))
    checks.append(("Broker state", "PASS"))

    account = refreshed_state.account
    if account is None:
        return fail("account missing after FLIP refresh", ("Account", False))
    if account.cash is None:
        return fail("cash missing after FLIP refresh", ("Cash", False))
    if account.equity is None:
        return fail("equity missing after FLIP refresh", ("Equity", False))
    if account.buying_power is None:
        return fail("buying power missing after FLIP refresh", ("Buying power", False))
    checks.extend(
        (
            ("Account", "PASS"),
            ("Cash", "PASS"),
            ("Equity", "PASS"),
            ("Buying power", "PASS"),
        )
    )

    old_qty = _qty(refreshed_state, exit_symbol)
    if old_qty > 1e-9:
        return fail(
            f"exit symbol {exit_symbol.upper()} qty {old_qty:g} still open after SELL; "
            "opposite buy blocked",
            ("Exit flat", False),
        )
    checks.append(("Exit flat", "PASS"))

    open_sells = _open_sells(refreshed_state, exit_symbol)
    if open_sells:
        return fail(
            f"unresolved open SELL on {exit_symbol.upper()} after refresh; opposite buy blocked",
            ("Open SELL", False),
        )
    checks.append(("Open SELL", "PASS"))

    opposite_qty = _qty(refreshed_state, entry_symbol)
    if opposite_qty > 1e-9:
        return fail(
            f"opposite ETF {entry_symbol.upper()} already held ({opposite_qty:g}); "
            "FLIP buy blocked",
            ("Opposite flat", False),
        )
    both = _qty(refreshed_state, "TQQQ") > 0 and _qty(refreshed_state, "SQQQ") > 0
    if both:
        return fail(
            "broker holds both TQQQ and SQQQ after FLIP refresh; opposite buy blocked",
            ("Single ETF book", False),
        )
    checks.append(("Opposite flat", "PASS"))
    checks.append(("Single ETF book", "PASS"))

    quote = quote_for(refreshed_state, entry_symbol)
    now = _as_utc(refreshed_state.now) or datetime.now(timezone.utc)
    if quote is None:
        return fail("opposite quote missing after FLIP refresh", ("Opposite quote", False))
    if quote.bid is None or quote.ask is None or quote.quote_time is None:
        return fail("opposite quote incomplete after FLIP refresh", ("Opposite quote", False))
    age = (now - _as_utc(quote.quote_time)).total_seconds()  # type: ignore[operator]
    if age > limits.max_quote_age_seconds or age < -1:
        return fail(
            f"opposite quote stale after FLIP refresh ({age:.1f}s)",
            ("Opposite quote", False),
        )
    spread = _spread_bps(float(quote.bid), float(quote.ask))
    if spread is None:
        return fail("opposite quote prices unusable after FLIP refresh", ("Opposite quote", False))
    if spread > limits.max_spread_bps:
        return fail(
            f"opposite quote spread {spread:.1f} bps exceeds {limits.max_spread_bps:g}",
            ("Opposite quote", True),
            ("Spread", False),
        )
    checks.append(("Opposite quote", "PASS"))
    checks.append(("Spread", "PASS"))

    # Final status machine confirmation with broker-confirmed remaining qty = 0.
    flip = advance_flip(
        exit_status=ORDER_FILLED,
        exit_qty_remaining=0.0,
        broker_state_known=True,
    )
    if not flip.entry_validation_allowed:
        return fail(flip.reason, ("Flip advance", False))
    checks.append(("Flip advance", "PASS"))

    return FlipRefreshGate(
        True,
        "broker-confirmed flat after FILLED sell; opposite buy may be risk-checked",
        tuple(checks),
        refreshed_state,
        0.0,
    )
