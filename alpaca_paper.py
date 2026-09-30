"""Optional Alpaca *paper* limit-order execution for actionable TQQQ/SQQQ alerts.

Orders are submitted only when ``ALPACA_PAPER_TRADING=true`` and ``DRY_RUN=false``.
The trading host is hard-gated to the paper API; live trading is not supported.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin

import requests

from data import alpaca_auth_headers
from runtime_logging import log_event
from strategy_types import AlertDecision, PositionState

PAPER_TRADING_BASE_URL = "https://paper-api.alpaca.markets"
LIVE_TRADING_HOST_MARKER = "api.alpaca.markets"
ACTIONABLE_ALERTS = frozenset({"BUY", "SELL", "FLIP"})


class PaperTradingError(Exception):
    """Non-fatal paper-trading failure (logged; does not abort the alert run)."""


@dataclass(frozen=True)
class OrderIntent:
    symbol: str
    side: str  # buy | sell
    purpose: str  # entry | exit | flip_exit | flip_entry


@dataclass(frozen=True)
class OrderResult:
    intent: OrderIntent
    ok: bool
    status: str
    detail: str
    order_id: str | None = None
    payload: dict[str, Any] | None = None


def is_live_trading_host(base_url: str) -> bool:
    host = (base_url or "").strip().lower().rstrip("/")
    if not host:
        return False
    # paper-api.alpaca.markets contains the substring api.alpaca.markets — exclude paper first.
    if "paper-api.alpaca.markets" in host:
        return False
    return LIVE_TRADING_HOST_MARKER in host


def should_submit_paper_orders(*, paper_trading: bool, dry_run: bool) -> bool:
    return bool(paper_trading) and not bool(dry_run)


def build_order_intents(
    alert: AlertDecision,
    position_before: PositionState | None,
) -> list[OrderIntent]:
    alert_type = (alert.alert_type or "").upper()
    symbol = (alert.symbol or "").upper()

    if alert_type == "BUY":
        if symbol not in {"TQQQ", "SQQQ"}:
            return []
        return [OrderIntent(symbol=symbol, side="buy", purpose="entry")]

    if alert_type == "SELL":
        if symbol not in {"TQQQ", "SQQQ"}:
            return []
        return [OrderIntent(symbol=symbol, side="sell", purpose="exit")]

    if alert_type == "FLIP":
        intents: list[OrderIntent] = []
        held = None
        if position_before and position_before.active_symbol:
            held = position_before.active_symbol.upper()
        if held in {"TQQQ", "SQQQ"} and held != symbol:
            intents.append(OrderIntent(symbol=held, side="sell", purpose="flip_exit"))
        if symbol in {"TQQQ", "SQQQ"}:
            intents.append(OrderIntent(symbol=symbol, side="buy", purpose="flip_entry"))
        return intents

    return []


def buy_notional_usd(*, fixed_notional: float, equity_pct: float, equity: float) -> float:
    if equity_pct and equity_pct > 0:
        if equity <= 0:
            raise PaperTradingError("Paper account equity is not positive; cannot size by equity %.")
        return float(equity) * float(equity_pct)
    if fixed_notional <= 0:
        raise PaperTradingError("ALPACA_PAPER_NOTIONAL must be positive when equity % is unset.")
    return float(fixed_notional)


def qty_for_notional(notional: float, price: float) -> str:
    if price <= 0:
        raise PaperTradingError(f"Invalid price for sizing: {price}")
    if notional <= 0:
        raise PaperTradingError(f"Invalid notional for sizing: {notional}")
    qty = math.floor((notional / price) * 10_000) / 10_000
    if qty <= 0:
        raise PaperTradingError(
            f"Computed qty is zero for notional={notional:.2f} price={price:.4f}; increase size knobs."
        )
    text = f"{qty:.4f}".rstrip("0").rstrip(".")
    return text or "0"


def limit_price_from_trade(price: float, *, side: str, offset_bps: int) -> str:
    if price <= 0:
        raise PaperTradingError(f"Invalid latest trade price: {price}")
    bps = max(0, int(offset_bps))
    mult = 1.0 + (bps / 10_000.0) if side == "buy" else 1.0 - (bps / 10_000.0)
    limited = round(price * mult, 2)
    if limited <= 0:
        raise PaperTradingError(f"Limit price non-positive after offset: {limited}")
    return f"{limited:.2f}"


def build_limit_order_payload(
    intent: OrderIntent,
    *,
    qty: str,
    limit_price: str,
    time_in_force: str = "day",
) -> dict[str, Any]:
    return {
        "symbol": intent.symbol,
        "qty": qty,
        "side": intent.side,
        "type": "limit",
        "time_in_force": time_in_force,
        "limit_price": limit_price,
    }


def _request_json(
    method: str,
    url: str,
    *,
    headers: dict[str, str],
    session: requests.Session | None = None,
    json_body: dict[str, Any] | None = None,
    timeout: int = 30,
) -> Any:
    client = session or requests
    try:
        response = client.request(method, url, headers=headers, json=json_body, timeout=timeout)
    except requests.RequestException as exc:
        raise PaperTradingError(f"Alpaca request failed ({method} {url}): {exc}") from exc
    body = (response.text or "").strip()
    if response.status_code >= 400:
        raise PaperTradingError(
            f"Alpaca paper API error ({response.status_code}) {method} {url}: {body or response.reason}"
        )
    if not body:
        return {}
    try:
        return response.json()
    except ValueError as exc:
        raise PaperTradingError(f"Alpaca paper API returned non-JSON for {method} {url}.") from exc


def fetch_paper_equity(
    *,
    api_key: str,
    api_secret: str,
    trading_base_url: str,
    session: requests.Session | None = None,
) -> float:
    url = urljoin(trading_base_url.rstrip("/") + "/", "v2/account")
    payload = _request_json("GET", url, headers=alpaca_auth_headers(api_key, api_secret), session=session)
    if not isinstance(payload, dict):
        raise PaperTradingError("Unexpected account payload from Alpaca paper API.")
    raw = payload.get("equity") or payload.get("cash")
    try:
        equity = float(raw)
    except (TypeError, ValueError) as exc:
        raise PaperTradingError(f"Could not parse paper equity from account payload: {raw!r}") from exc
    return equity


def fetch_position_qty(
    symbol: str,
    *,
    api_key: str,
    api_secret: str,
    trading_base_url: str,
    session: requests.Session | None = None,
) -> str | None:
    url = urljoin(trading_base_url.rstrip("/") + "/", f"v2/positions/{symbol.upper()}")
    headers = alpaca_auth_headers(api_key, api_secret)
    client = session or requests
    try:
        response = client.get(url, headers=headers, timeout=30)
    except requests.RequestException as exc:
        raise PaperTradingError(f"Alpaca position lookup failed for {symbol}: {exc}") from exc
    if response.status_code == 404:
        return None
    body = (response.text or "").strip()
    if response.status_code >= 400:
        raise PaperTradingError(
            f"Alpaca position lookup error ({response.status_code}) for {symbol}: {body or response.reason}"
        )
    try:
        payload = response.json()
    except ValueError as exc:
        raise PaperTradingError(f"Non-JSON position payload for {symbol}.") from exc
    if not isinstance(payload, dict):
        raise PaperTradingError(f"Unexpected position payload for {symbol}.")
    qty = payload.get("qty")
    if qty is None:
        return None
    text = str(qty).strip()
    try:
        if float(text) <= 0:
            return None
    except ValueError:
        return None
    return text


def fetch_latest_trade_price(
    symbol: str,
    *,
    api_key: str,
    api_secret: str,
    data_base_url: str,
    feed: str,
    session: requests.Session | None = None,
) -> float:
    url = urljoin(data_base_url.rstrip("/") + "/", f"v2/stocks/{symbol.upper()}/trades/latest")
    headers = alpaca_auth_headers(api_key, api_secret)
    client = session or requests
    try:
        response = client.get(url, headers=headers, params={"feed": feed}, timeout=30)
    except requests.RequestException as exc:
        raise PaperTradingError(f"Latest trade fetch failed for {symbol}: {exc}") from exc
    body = (response.text or "").strip()
    if response.status_code >= 400:
        raise PaperTradingError(
            f"Latest trade error ({response.status_code}) for {symbol}: {body or response.reason}"
        )
    try:
        payload = response.json()
    except ValueError as exc:
        raise PaperTradingError(f"Non-JSON latest trade for {symbol}.") from exc
    trade = payload.get("trade") if isinstance(payload, dict) else None
    if not isinstance(trade, dict):
        raise PaperTradingError(f"Missing trade object for {symbol}.")
    try:
        price = float(trade.get("p"))
    except (TypeError, ValueError) as exc:
        raise PaperTradingError(f"Invalid trade price for {symbol}: {trade.get('p')!r}") from exc
    if price <= 0:
        raise PaperTradingError(f"Non-positive trade price for {symbol}: {price}")
    return price


def submit_limit_order(
    payload: dict[str, Any],
    *,
    api_key: str,
    api_secret: str,
    trading_base_url: str,
    session: requests.Session | None = None,
) -> dict[str, Any]:
    url = urljoin(trading_base_url.rstrip("/") + "/", "v2/orders")
    result = _request_json(
        "POST",
        url,
        headers=alpaca_auth_headers(api_key, api_secret),
        session=session,
        json_body=payload,
    )
    if not isinstance(result, dict):
        raise PaperTradingError("Unexpected order response from Alpaca paper API.")
    return result


def resolve_order_qty(
    intent: OrderIntent,
    *,
    api_key: str,
    api_secret: str,
    trading_base_url: str,
    data_base_url: str,
    feed: str,
    fixed_notional: float,
    equity_pct: float,
    equity: float | None,
    session: requests.Session | None = None,
) -> tuple[str, float]:
    """Return (qty, reference_price)."""
    price = fetch_latest_trade_price(
        intent.symbol,
        api_key=api_key,
        api_secret=api_secret,
        data_base_url=data_base_url,
        feed=feed,
        session=session,
    )
    if intent.side == "sell":
        held_qty = fetch_position_qty(
            intent.symbol,
            api_key=api_key,
            api_secret=api_secret,
            trading_base_url=trading_base_url,
            session=session,
        )
        if held_qty:
            return held_qty, price
        # No broker position — size a conservative sell from notional (still may reject at API).
        if equity is None:
            equity = fetch_paper_equity(
                api_key=api_key,
                api_secret=api_secret,
                trading_base_url=trading_base_url,
                session=session,
            )
        notional = buy_notional_usd(fixed_notional=fixed_notional, equity_pct=equity_pct, equity=equity)
        return qty_for_notional(notional, price), price

    if equity is None:
        equity = fetch_paper_equity(
            api_key=api_key,
            api_secret=api_secret,
            trading_base_url=trading_base_url,
            session=session,
        )
    notional = buy_notional_usd(fixed_notional=fixed_notional, equity_pct=equity_pct, equity=equity)
    return qty_for_notional(notional, price), price


def execute_paper_orders(
    alert: AlertDecision,
    position_before: PositionState | None,
    *,
    paper_trading: bool,
    dry_run: bool,
    api_key: str,
    api_secret: str,
    trading_base_url: str,
    data_base_url: str,
    feed: str,
    fixed_notional: float,
    equity_pct: float,
    limit_offset_bps: int,
    logger: logging.Logger | None = None,
    session: requests.Session | None = None,
) -> list[OrderResult]:
    """Gate, build intents, and submit paper limit orders. Never raises for API failures."""
    log = logger or logging.getLogger("tqqq_sqqq_alerts")
    alert_type = (alert.alert_type or "").upper()

    if alert_type not in ACTIONABLE_ALERTS:
        return []

    if not should_submit_paper_orders(paper_trading=paper_trading, dry_run=dry_run):
        reason = "ALPACA_PAPER_TRADING=false" if not paper_trading else "DRY_RUN=true"
        print(f"[alpaca-paper] Skipped actionable {alert_type} ({reason}).")
        log_event(
            log,
            logging.INFO,
            "Alpaca paper trading skipped",
            reason=reason,
            alert_type=alert_type,
            symbol=alert.symbol,
        )
        return []

    if is_live_trading_host(trading_base_url):
        msg = f"Refusing live trading host: {trading_base_url}"
        print(f"[alpaca-paper] ERROR: {msg}")
        log_event(log, logging.ERROR, "Alpaca paper trading refused live host", trading_base_url=trading_base_url)
        return [
            OrderResult(
                intent=OrderIntent(symbol=alert.symbol, side="buy", purpose="blocked"),
                ok=False,
                status="refused",
                detail=msg,
            )
        ]

    intents = build_order_intents(alert, position_before)
    if not intents:
        print(f"[alpaca-paper] No order intents for {alert_type} {alert.symbol}.")
        return []

    results: list[OrderResult] = []
    equity: float | None = None
    try:
        equity = fetch_paper_equity(
            api_key=api_key,
            api_secret=api_secret,
            trading_base_url=trading_base_url,
            session=session,
        )
    except PaperTradingError as exc:
        print(f"[alpaca-paper] Account equity fetch failed (will retry per order): {exc}")
        log_event(log, logging.WARNING, "Alpaca paper equity fetch failed", error=str(exc))

    for intent in intents:
        try:
            if intent.side == "sell":
                held = fetch_position_qty(
                    intent.symbol,
                    api_key=api_key,
                    api_secret=api_secret,
                    trading_base_url=trading_base_url,
                    session=session,
                )
                if not held:
                    detail = f"No Alpaca paper position for {intent.symbol}; skip sell."
                    print(f"[alpaca-paper] {detail}")
                    log_event(
                        log,
                        logging.INFO,
                        "Alpaca paper sell skipped",
                        symbol=intent.symbol,
                        purpose=intent.purpose,
                        reason="no_position",
                    )
                    results.append(
                        OrderResult(intent=intent, ok=True, status="skipped", detail=detail)
                    )
                    continue

            qty, ref_price = resolve_order_qty(
                intent,
                api_key=api_key,
                api_secret=api_secret,
                trading_base_url=trading_base_url,
                data_base_url=data_base_url,
                feed=feed,
                fixed_notional=fixed_notional,
                equity_pct=equity_pct,
                equity=equity,
                session=session,
            )
            limit_price = limit_price_from_trade(
                ref_price, side=intent.side, offset_bps=limit_offset_bps
            )
            payload = build_limit_order_payload(intent, qty=qty, limit_price=limit_price)
            print(
                f"[alpaca-paper] Submitting {intent.side} {intent.symbol} qty={qty} "
                f"limit={limit_price} ({intent.purpose})"
            )
            log_event(
                log,
                logging.INFO,
                "Alpaca paper order attempt",
                symbol=intent.symbol,
                side=intent.side,
                purpose=intent.purpose,
                qty=qty,
                limit_price=limit_price,
                ref_price=ref_price,
            )
            order = submit_limit_order(
                payload,
                api_key=api_key,
                api_secret=api_secret,
                trading_base_url=trading_base_url,
                session=session,
            )
            order_id = str(order.get("id") or "") or None
            status = str(order.get("status") or "submitted")
            detail = f"order_id={order_id or 'n/a'} status={status}"
            print(f"[alpaca-paper] OK {intent.side} {intent.symbol}: {detail}")
            log_event(
                log,
                logging.INFO,
                "Alpaca paper order result",
                ok=True,
                symbol=intent.symbol,
                side=intent.side,
                purpose=intent.purpose,
                order_id=order_id,
                status=status,
            )
            results.append(
                OrderResult(
                    intent=intent,
                    ok=True,
                    status=status,
                    detail=detail,
                    order_id=order_id,
                    payload=payload,
                )
            )
        except PaperTradingError as exc:
            print(f"[alpaca-paper] FAILED {intent.side} {intent.symbol}: {exc}")
            log_event(
                log,
                logging.ERROR,
                "Alpaca paper order failed",
                symbol=intent.symbol,
                side=intent.side,
                purpose=intent.purpose,
                error=str(exc),
            )
            results.append(
                OrderResult(intent=intent, ok=False, status="error", detail=str(exc))
            )
        except Exception as exc:  # noqa: BLE001
            print(f"[alpaca-paper] FAILED {intent.side} {intent.symbol}: unexpected {exc}")
            log_event(
                log,
                logging.ERROR,
                "Alpaca paper order unexpected failure",
                symbol=intent.symbol,
                side=intent.side,
                purpose=intent.purpose,
                error=str(exc),
            )
            results.append(
                OrderResult(intent=intent, ok=False, status="error", detail=str(exc))
            )

    return results
