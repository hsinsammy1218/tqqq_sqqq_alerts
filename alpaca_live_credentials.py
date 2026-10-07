"""Paper vs live Alpaca credentials. No cross-fallback."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass

from brokers.alpaca_endpoints import (
    LIVE_TRADING_BASE_URL,
    PAPER_TRADING_BASE_URL,
    is_exact_live_trading_host,
    is_exact_paper_trading_host,
    normalize_trading_base_url,
)
from brokers.types import UnsafeBrokerConfiguration


@dataclass(frozen=True)
class AlpacaLiveCredentials:
    api_key: str
    api_secret: str
    trading_base_url: str
    data_base_url: str
    data_feed: str


def _required(source: Mapping[str, str], name: str) -> str:
    value = str(source.get(name, "") or "").strip()
    if not value:
        raise UnsafeBrokerConfiguration(
            f"{name} is required for alpaca_live_shadow. "
            "Paper keys are not reused."
        )
    return value


def load_live_credentials(env: Mapping[str, str] | None = None) -> AlpacaLiveCredentials:
    """Load live-only credentials. Never falls back to ALPACA_API_KEY."""
    source = os.environ if env is None else env
    key = _required(source, "ALPACA_LIVE_API_KEY")
    secret = _required(source, "ALPACA_LIVE_API_SECRET")
    paper_key = str(source.get("ALPACA_API_KEY", "") or "").strip()
    paper_secret = str(source.get("ALPACA_API_SECRET", "") or "").strip()
    if paper_key and key == paper_key:
        raise UnsafeBrokerConfiguration(
            "ALPACA_LIVE_API_KEY must not equal ALPACA_API_KEY "
            "(paper/live credential isolation)."
        )
    if paper_secret and secret == paper_secret:
        raise UnsafeBrokerConfiguration(
            "ALPACA_LIVE_API_SECRET must not equal ALPACA_API_SECRET "
            "(paper/live credential isolation)."
        )
    trading = normalize_trading_base_url(
        source.get("ALPACA_LIVE_TRADING_BASE_URL", LIVE_TRADING_BASE_URL)
        or LIVE_TRADING_BASE_URL
    )
    if not is_exact_live_trading_host(trading):
        raise UnsafeBrokerConfiguration(
            "ALPACA_LIVE_TRADING_BASE_URL must be exactly "
            f"{LIVE_TRADING_BASE_URL} (trailing slash ignored). "
            f"Got {trading!r}."
        )
    if is_exact_paper_trading_host(trading):
        raise UnsafeBrokerConfiguration(
            "alpaca_live_shadow refuses the paper trading host."
        )
    data = (
        str(source.get("ALPACA_DATA_BASE_URL", "https://data.alpaca.markets") or "").strip()
        or "https://data.alpaca.markets"
    ).rstrip("/")
    feed = (str(source.get("ALPACA_DATA_FEED", "iex") or "iex").strip() or "iex").lower()
    return AlpacaLiveCredentials(
        api_key=key,
        api_secret=secret,
        trading_base_url=trading,
        data_base_url=data,
        data_feed=feed,
    )


def assert_paper_url_isolated(paper_trading_base_url: str | None) -> None:
    """Paper path must stay on the paper host even when live shadow exists."""
    normalized = normalize_trading_base_url(paper_trading_base_url or PAPER_TRADING_BASE_URL)
    if not is_exact_paper_trading_host(normalized):
        raise UnsafeBrokerConfiguration(
            "Paper trading URL is not the exact paper host; refusing mixed config."
        )
