"""Paper limit prices from bid/ask when the quote is fresh, else a trade fallback.

New exposure is blocked when the quote is stale, missing, or malformed and the
trade fallback is also unusable. Risk-reducing sells may still use a bid or a
last trade. This module does not submit orders and does not change strategy rules.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone


@dataclass(frozen=True)
class ExecutionLimit:
    limit_price: str | None
    reference_price: float | None
    blocked: bool
    reason: str
    mode: str


def _as_utc(ts: datetime | None) -> datetime | None:
    if ts is None:
        return None
    if ts.tzinfo is None:
        return ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)


def _age_seconds(stamp: datetime | None, now: datetime) -> float | None:
    parsed = _as_utc(stamp)
    if parsed is None:
        return None
    return (_as_utc(now) - parsed).total_seconds()


def _money(price: float) -> str:
    return f"{round(float(price), 2):.2f}"


def _offset(price: float, *, side: str, offset_bps: int) -> float:
    bps = max(0, int(offset_bps))
    mult = 1.0 + (bps / 10_000.0) if side == "buy" else 1.0 - (bps / 10_000.0)
    return round(price * mult, 2)


def limit_from_quote_or_trade(
    *,
    side: str,
    bid: float | None,
    ask: float | None,
    trade: float | None,
    quote_time: datetime | None,
    trade_time: datetime | None,
    now: datetime,
    offset_bps: int,
    max_spread_bps: float,
    max_quote_age_seconds: float,
    max_trade_age_seconds: float,
) -> ExecutionLimit:
    """Choose a limit. BUY uses the ask; SELL uses the bid when the quote qualifies.

    A quote with no timestamp is not fresh. A trade with no timestamp is treated
    as usable so paper runs that only have a last price stay predictable.
    """
    order_side = (side or "").strip().lower()
    if order_side not in {"buy", "sell"}:
        return ExecutionLimit(None, None, True, f"unsupported side {side!r}", "blocked")

    quote_error = _quote_problem(bid, ask)
    quote_age = _age_seconds(quote_time, now)
    quote_fresh = (
        quote_error is None
        and quote_time is not None
        and quote_age is not None
        and quote_age <= float(max_quote_age_seconds)
    )
    spread_bps = _spread_bps(bid, ask) if quote_error is None else None
    wide = (
        quote_fresh
        and spread_bps is not None
        and spread_bps > float(max_spread_bps)
    )

    trade_ok = trade is not None and trade > 0
    trade_age = _age_seconds(trade_time, now)
    # No timestamp: keep the historical last-trade path (tests and quiet feeds).
    trade_fresh = trade_ok and (trade_time is None or (trade_age is not None and trade_age <= float(max_trade_age_seconds)))

    if quote_fresh and not wide and bid is not None and ask is not None:
        if order_side == "buy":
            return ExecutionLimit(_money(ask), float(ask), False, "buy limit at ask", "ask")
        return ExecutionLimit(_money(bid), float(bid), False, "sell limit at bid", "bid")

    if order_side == "buy":
        if wide:
            return ExecutionLimit(
                None,
                None,
                True,
                f"spread {spread_bps:.1f} bps exceeds max {max_spread_bps:g}",
                "blocked",
            )
        if quote_error == "quote malformed":
            return ExecutionLimit(None, None, True, "quote malformed", "blocked")
        quote_stale = quote_error is None and quote_time is not None and not quote_fresh
        if quote_stale:
            return ExecutionLimit(None, None, True, "quote stale", "blocked")
        if not trade_fresh:
            return ExecutionLimit(None, None, True, "quote missing; no fresh trade", "blocked")
        limited = _offset(float(trade), side="buy", offset_bps=offset_bps)
        if limited <= 0:
            return ExecutionLimit(None, None, True, "trade fallback limit non-positive", "blocked")
        return ExecutionLimit(
            _money(limited),
            float(trade),
            False,
            "buy limit from last trade plus offset",
            "trade_fallback",
        )

    # Sells are risk-reducing: prefer a fresh bid, else a trade even if the quote is bad.
    if quote_fresh and wide and bid is not None and bid > 0:
        return ExecutionLimit(
            _money(bid),
            float(bid),
            False,
            f"wide spread {spread_bps:.1f} bps; sell at bid",
            "bid",
        )
    if trade_ok:
        limited = _offset(float(trade), side="sell", offset_bps=offset_bps)
        if limited <= 0:
            return ExecutionLimit(None, None, True, "sell trade fallback non-positive", "blocked")
        note = "sell limit from last trade minus offset"
        if trade_time is not None and not trade_fresh:
            note = "sell limit from stale last trade minus offset"
        return ExecutionLimit(_money(limited), float(trade), False, note, "trade_fallback")
    if bid is not None and bid > 0:
        return ExecutionLimit(_money(bid), float(bid), False, "sell limit at bid without a trade", "bid")
    return ExecutionLimit(None, None, True, "no bid or trade for sell limit", "blocked")


def _quote_problem(bid: float | None, ask: float | None) -> str | None:
    if bid is None or ask is None:
        return "quote missing"
    if bid <= 0 or ask <= 0:
        return "quote malformed"
    if ask < bid:
        return "quote malformed"
    return None


def _spread_bps(bid: float | None, ask: float | None) -> float | None:
    if bid is None or ask is None or bid <= 0 or ask <= 0:
        return None
    mid = (bid + ask) / 2.0
    if mid <= 0:
        return None
    return (ask - bid) / mid * 10_000.0


def parse_px(raw: object) -> float | None:
    if raw is None or raw == "":
        return None
    try:
        value = float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if value <= 0:
        return None
    return value
