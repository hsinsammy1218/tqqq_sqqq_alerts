"""Exact Alpaca trading hosts. Paper and live never share a URL."""

from __future__ import annotations

PAPER_TRADING_BASE_URL = "https://paper-api.alpaca.markets"
LIVE_TRADING_BASE_URL = "https://api.alpaca.markets"
DATA_BASE_URL = "https://data.alpaca.markets"


def normalize_trading_base_url(base_url: str | None) -> str:
    return (base_url or "").strip().rstrip("/")


def is_exact_paper_trading_host(base_url: str | None) -> bool:
    return normalize_trading_base_url(base_url) == PAPER_TRADING_BASE_URL


def is_exact_live_trading_host(base_url: str | None) -> bool:
    return normalize_trading_base_url(base_url) == LIVE_TRADING_BASE_URL
