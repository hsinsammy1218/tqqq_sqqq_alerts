"""Read-only Alpaca Live client.

GET account / positions / open orders / quotes only. There is no submit,
cancel, replace, or close method. A live flag cannot add one.

``ALPACA_LIVE_SUBMISSION_IMPLEMENTED`` lives on the broker adapter. This
reader additionally rejects every non-GET before a socket opens.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any
from urllib.parse import urljoin, urlparse

import requests

from brokers.alpaca_endpoints import (
    LIVE_TRADING_BASE_URL,
    is_exact_live_trading_host,
    normalize_trading_base_url,
)
from brokers.types import UnsafeBrokerConfiguration
from data import alpaca_auth_headers

ALLOWED_GET_PATHS = frozenset(
    {
        "/v2/account",
        "/v2/positions",
        "/v2/orders",
    }
)
# Quotes use the data host; path prefix checked separately.
DATA_QUOTE_PATH_PREFIX = "/v2/stocks/"

Transport = Callable[[str, str, dict[str, str], dict[str, Any] | None], Any]


class AlpacaLiveWriteRejected(Exception):
    """The caller asked for a mutating Alpaca Live call."""


class AlpacaLiveReadError(Exception):
    """A required live read failed. Callers must treat state as unknown."""


def assert_get_only(method: str) -> str:
    verb = (method or "").strip().upper()
    if verb != "GET":
        raise AlpacaLiveWriteRejected(
            f"refusing Alpaca Live {verb or 'EMPTY'}; this client is read-only"
        )
    return verb


def assert_read_path(url: str, *, trading_base_url: str) -> str:
    parsed = urlparse(url)
    path = parsed.path or ""
    host_url = f"{parsed.scheme}://{parsed.netloc}"
    trading = normalize_trading_base_url(trading_base_url)
    if is_exact_live_trading_host(host_url) or host_url.rstrip("/") == trading:
        if path.rstrip("/") in {p.rstrip("/") for p in ALLOWED_GET_PATHS}:
            return path
        # Allow /v2/orders/{id} GET only
        if path.startswith("/v2/orders/") and path.count("/") == 3:
            return path
        raise AlpacaLiveWriteRejected(
            f"refusing Alpaca Live path {path!r}; not on the read allowlist"
        )
    # Data host quotes
    if path.startswith(DATA_QUOTE_PATH_PREFIX) and path.endswith("/quotes/latest"):
        return path
    raise AlpacaLiveWriteRejected(
        f"refusing Alpaca Live URL host/path {host_url}{path}"
    )


class ReadOnlySession:
    """Wraps requests.Session and rejects mutating verbs before send."""

    def __init__(self, inner: requests.Session | None = None) -> None:
        self._inner = inner or requests.Session()
        self.calls: list[tuple[str, str]] = []

    def request(
        self,
        method: str,
        url: str,
        **kwargs: Any,
    ) -> requests.Response:
        verb = assert_get_only(method)
        self.calls.append((verb, url))
        return self._inner.request(verb, url, **kwargs)

    def get(self, url: str, **kwargs: Any) -> requests.Response:
        return self.request("GET", url, **kwargs)


class AlpacaLiveReadClient:
    """Calls allowlisted GET endpoints through an injected transport or session."""

    def __init__(
        self,
        *,
        api_key: str,
        api_secret: str,
        trading_base_url: str = LIVE_TRADING_BASE_URL,
        data_base_url: str = "https://data.alpaca.markets",
        data_feed: str = "iex",
        session: ReadOnlySession | requests.Session | None = None,
        transport: Transport | None = None,
    ) -> None:
        if not is_exact_live_trading_host(trading_base_url):
            raise UnsafeBrokerConfiguration(
                "AlpacaLiveReadClient requires the exact live trading host."
            )
        self.api_key = api_key
        self.api_secret = api_secret
        self.trading_base_url = normalize_trading_base_url(trading_base_url)
        self.data_base_url = data_base_url.rstrip("/")
        self.data_feed = data_feed
        self._session = session or ReadOnlySession()
        self._transport = transport
        self.invocations: list[str] = []

    def _headers(self) -> dict[str, str]:
        return alpaca_auth_headers(self.api_key, self.api_secret)

    def _get(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        label: str,
    ) -> Any:
        assert_get_only("GET")
        assert_read_path(url, trading_base_url=self.trading_base_url)
        self.invocations.append(label)
        if self._transport is not None:
            try:
                return self._transport("GET", url, self._headers(), params)
            except (AlpacaLiveWriteRejected, AlpacaLiveReadError, UnsafeBrokerConfiguration):
                raise
            except Exception as exc:  # noqa: BLE001
                raise AlpacaLiveReadError(f"{label} failed") from exc
        try:
            response = self._session.get(
                url,
                headers=self._headers(),
                params=params,
                timeout=30,
            )
        except (AlpacaLiveWriteRejected, UnsafeBrokerConfiguration):
            raise
        except Exception as exc:  # noqa: BLE001
            raise AlpacaLiveReadError(f"{label} failed") from exc
        if getattr(response, "status_code", 500) >= 400:
            raise AlpacaLiveReadError(
                f"{label} HTTP {getattr(response, 'status_code', '?')}"
            )
        try:
            return response.json()
        except Exception as exc:  # noqa: BLE001
            raise AlpacaLiveReadError(f"{label} returned non-JSON") from exc

    def get_account(self) -> Any:
        url = urljoin(self.trading_base_url + "/", "v2/account")
        return self._get(url, label="get_account")

    def get_positions(self) -> Any:
        url = urljoin(self.trading_base_url + "/", "v2/positions")
        return self._get(url, label="get_positions")

    def get_open_orders(self) -> Any:
        url = urljoin(self.trading_base_url + "/", "v2/orders")
        return self._get(
            url,
            params={"status": "open", "nested": "true", "limit": 100},
            label="get_open_orders",
        )

    def get_latest_quote(self, symbol: str) -> Any:
        sym = (symbol or "").strip().upper()
        if sym not in {"TQQQ", "SQQQ"}:
            raise AlpacaLiveReadError(f"quote symbol {sym!r} is not allowlisted")
        url = urljoin(self.data_base_url + "/", f"v2/stocks/{sym}/quotes/latest")
        return self._get(
            url,
            params={"feed": self.data_feed},
            label=f"get_quote:{sym}",
        )

    def read_snapshot(self, symbols: tuple[str, ...] = ("TQQQ", "SQQQ")) -> dict[str, Any]:
        quotes: dict[str, Any] = {}
        for symbol in symbols:
            quotes[symbol.upper()] = self.get_latest_quote(symbol)
        return {
            "account": self.get_account(),
            "positions": self.get_positions(),
            "orders": self.get_open_orders(),
            "quotes": quotes,
        }

    # Explicit absences — keep attribute errors obvious in audits.
    def submit_order(self, *args: Any, **kwargs: Any) -> None:
        raise AlpacaLiveWriteRejected("AlpacaLiveReadClient has no submit_order")

    def cancel_order(self, *args: Any, **kwargs: Any) -> None:
        raise AlpacaLiveWriteRejected("AlpacaLiveReadClient has no cancel_order")

    def replace_order(self, *args: Any, **kwargs: Any) -> None:
        raise AlpacaLiveWriteRejected("AlpacaLiveReadClient has no replace_order")

    def close_position(self, *args: Any, **kwargs: Any) -> None:
        raise AlpacaLiveWriteRejected("AlpacaLiveReadClient has no close_position")

    def close_all_positions(self, *args: Any, **kwargs: Any) -> None:
        raise AlpacaLiveWriteRejected("AlpacaLiveReadClient has no close_all_positions")


def client_from_credentials(creds, *, transport: Transport | None = None) -> AlpacaLiveReadClient:
    return AlpacaLiveReadClient(
        api_key=creds.api_key,
        api_secret=creds.api_secret,
        trading_base_url=creds.trading_base_url,
        data_base_url=creds.data_base_url,
        data_feed=creds.data_feed,
        transport=transport,
    )
