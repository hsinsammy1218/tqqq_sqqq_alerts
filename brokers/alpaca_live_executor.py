"""Narrow Alpaca Live pilot executor.

Only ``submit_validated_order`` may POST. Timeout → UNKNOWN + lookup by
client_order_id. Never blind-retry POST. Accepted ≠ filled.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable
from urllib.parse import urljoin

import requests

from brokers.alpaca_endpoints import (
    LIVE_TRADING_BASE_URL,
    is_exact_live_trading_host,
    normalize_trading_base_url,
)
from brokers.alpaca_live_normalize import map_order_status
from brokers.types import (
    ORDER_UNKNOWN,
    TradeIntent,
    UnsafeBrokerConfiguration,
)
from data import alpaca_auth_headers

# Hard-implemented pilot path. Env alone cannot invent this constant.
ALPACA_LIVE_PILOT_SUBMISSION_IMPLEMENTED = True

Transport = Callable[[str, str, dict[str, str], dict[str, Any] | None, dict[str, Any] | None], Any]


class LivePilotError(Exception):
    """Structured live-pilot execution failure."""


class LiveOrderTimeout(LivePilotError):
    """POST timed out; order state is UNKNOWN until lookup."""


class LiveOrderRejected(LivePilotError):
    """Broker or local validation rejected the order."""


class LiveOrderAmbiguous(LivePilotError):
    """Could not determine whether the order exists after a timeout."""


@dataclass(frozen=True)
class SubmitResult:
    status: str
    broker_order_id: str | None
    client_order_id: str
    filled_qty: float
    qty: float
    raw: dict[str, Any] | None
    detail: str
    post_attempts: int


def _money(price: float) -> str:
    return f"{round(float(price), 2):.2f}"


def marketable_limit_price(*, action: str, bid: float | None, ask: float | None) -> str:
    side = (action or "").strip().upper()
    if side == "BUY":
        if ask is None or ask <= 0:
            raise LiveOrderRejected("buy limit requires a positive ask")
        return _money(ask)
    if side == "SELL":
        if bid is None or bid <= 0:
            raise LiveOrderRejected("sell limit requires a positive bid")
        return _money(bid)
    raise LiveOrderRejected(f"unsupported action {action!r}")


def build_limit_payload(
    intent: TradeIntent,
    *,
    limit_price: str,
) -> dict[str, Any]:
    if intent.quantity < 1:
        raise LiveOrderRejected("quantity must be a positive whole share count")
    action = intent.action.strip().upper()
    if action not in {"BUY", "SELL"}:
        raise LiveOrderRejected(f"unsupported action {action!r}")
    symbol = intent.execution_symbol.strip().upper()
    if symbol not in {"TQQQ", "SQQQ"}:
        raise LiveOrderRejected(f"symbol {symbol} is not allowlisted")
    return {
        "symbol": symbol,
        "qty": str(int(intent.quantity)),
        "side": action.lower(),
        "type": "limit",
        "time_in_force": "day",
        "limit_price": limit_price,
        "client_order_id": intent.client_order_id,
    }


class AlpacaLiveExecutor:
    """Single write path for validated TradeIntent objects."""

    def __init__(
        self,
        *,
        api_key: str,
        api_secret: str,
        trading_base_url: str = LIVE_TRADING_BASE_URL,
        transport: Transport | None = None,
        session: requests.Session | None = None,
        post_timeout_seconds: float = 30.0,
        armed: bool = False,
    ) -> None:
        trading = normalize_trading_base_url(trading_base_url)
        if not is_exact_live_trading_host(trading):
            raise UnsafeBrokerConfiguration(
                "AlpacaLiveExecutor requires the exact live trading host."
            )
        if not ALPACA_LIVE_PILOT_SUBMISSION_IMPLEMENTED:
            raise UnsafeBrokerConfiguration(
                "ALPACA_LIVE_PILOT_SUBMISSION_IMPLEMENTED is false."
            )
        self.api_key = api_key
        self.api_secret = api_secret
        self.trading_base_url = trading
        self._transport = transport
        self._session = session or requests.Session()
        self.post_timeout_seconds = float(post_timeout_seconds)
        self.armed = bool(armed)
        self.post_attempts = 0
        self.get_attempts = 0
        self.calls: list[tuple[str, str]] = []

    def _headers(self) -> dict[str, str]:
        return alpaca_auth_headers(self.api_key, self.api_secret)

    def _request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> Any:
        verb = method.strip().upper()
        self.calls.append((verb, url))
        if self._transport is not None:
            return self._transport(verb, url, self._headers(), params, json_body)
        try:
            response = self._session.request(
                verb,
                url,
                headers=self._headers(),
                params=params,
                json=json_body,
                timeout=timeout if timeout is not None else 30,
            )
        except requests.Timeout as exc:
            raise LiveOrderTimeout(f"{verb} timed out: {exc}") from exc
        except requests.RequestException as exc:
            raise LivePilotError(f"{verb} failed: {exc}") from exc
        if response.status_code == 404 and verb == "GET":
            return None
        if response.status_code >= 400:
            raise LiveOrderRejected(
                f"{verb} HTTP {response.status_code}: {(response.text or '')[:300]}"
            )
        if not response.content:
            return {}
        try:
            return response.json()
        except ValueError as exc:
            raise LivePilotError(f"{verb} returned non-JSON") from exc

    def find_order_by_client_id(self, client_order_id: str) -> dict[str, Any] | None:
        text = (client_order_id or "").strip()
        if not text:
            raise LiveOrderRejected("empty client_order_id")
        url = f"{self.trading_base_url}/v2/orders:client_order_id:{text}"
        self.get_attempts += 1
        result = self._request("GET", url, timeout=30)
        if result is None:
            return None
        if not isinstance(result, dict):
            raise LivePilotError("unexpected client-order lookup payload")
        return result

    def _result_from_order(self, order: dict[str, Any], *, detail: str, posts: int) -> SubmitResult:
        status = map_order_status(order.get("status"))
        filled = float(order.get("filled_qty") or 0)
        qty = float(order.get("qty") or order.get("filled_qty") or 0)
        return SubmitResult(
            status=status,
            broker_order_id=str(order.get("id") or "") or None,
            client_order_id=str(order.get("client_order_id") or ""),
            filled_qty=filled,
            qty=qty,
            raw=order,
            detail=detail,
            post_attempts=posts,
        )

    def submit_validated_order(
        self,
        intent: TradeIntent,
        *,
        limit_price: str,
    ) -> SubmitResult:
        """POST once. On timeout, look up — never POST again for the same id."""
        if not self.armed:
            raise LiveOrderRejected(
                "executor is not armed; multi-key arming failed closed"
            )
        if not ALPACA_LIVE_PILOT_SUBMISSION_IMPLEMENTED:
            raise LiveOrderRejected("pilot submission constant is false")
        payload = build_limit_payload(intent, limit_price=limit_price)
        url = urljoin(self.trading_base_url + "/", "v2/orders")
        self.post_attempts += 1
        posts = self.post_attempts
        try:
            raw = self._request(
                "POST",
                url,
                json_body=payload,
                timeout=self.post_timeout_seconds,
            )
        except LiveOrderTimeout:
            # Never blind-retry POST. Recover by client_order_id lookup.
            try:
                existing = self.find_order_by_client_id(intent.client_order_id)
            except Exception as lookup_exc:  # noqa: BLE001
                raise LiveOrderAmbiguous(
                    f"POST timed out and lookup failed for "
                    f"{intent.client_order_id}: {lookup_exc}"
                ) from lookup_exc
            if existing is None:
                return SubmitResult(
                    status=ORDER_UNKNOWN,
                    broker_order_id=None,
                    client_order_id=intent.client_order_id,
                    filled_qty=0.0,
                    qty=float(intent.quantity),
                    raw=None,
                    detail="POST timed out; order not found at broker (UNKNOWN)",
                    post_attempts=posts,
                )
            return self._result_from_order(
                existing,
                detail="POST timed out; recovered existing order by client_order_id",
                posts=posts,
            )
        if not isinstance(raw, dict):
            raise LivePilotError("unexpected order POST payload")
        return self._result_from_order(raw, detail="accepted by broker POST", posts=posts)
