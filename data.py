from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urljoin

import pandas as pd
import requests


class DataError(Exception):
    pass


class MarketDataQuotaError(DataError):
    """Alpaca (or market-data provider) reported rate limit / quota exhaustion."""


# Backward-compatible alias for older imports/tests during the KA → Alpaca cutover.
KlickAnalyticsQuotaError = MarketDataQuotaError

DEFAULT_DATA_BASE_URL = "https://data.alpaca.markets"
DEFAULT_DATA_FEED = "iex"


@dataclass(frozen=True)
class CandleData:
    daily: pd.DataFrame
    four_hour: pd.DataFrame


_api_calls_this_run = 0


def reset_cli_call_count() -> None:
    """Reset per-run market-data API call counter (name kept for call-site compatibility)."""
    global _api_calls_this_run
    _api_calls_this_run = 0


def cli_calls_attempted() -> int:
    return _api_calls_this_run


def format_cli_usage_line(
    *,
    reason: str | None = None,
    month_total: int | None = None,
    month_limit: int | None = None,
) -> str:
    count = cli_calls_attempted()
    noun = "call" if count == 1 else "calls"
    suffix = f" ({reason})" if reason else ""
    line = f"[alpaca] {count} API {noun} attempted this run{suffix}"
    if month_total is not None and month_limit:
        line += f" · month {month_total}/{month_limit}"
    return line


def _record_api_call() -> None:
    global _api_calls_this_run
    _api_calls_this_run += 1


def is_rate_limit_message(text: str) -> bool:
    low = (text or "").lower()
    if not low:
        return False
    markers = (
        "rate limit",
        "too many requests",
        "429",
        "quota exceeded",
        "quota exhausted",
        "request limit",
        "monthly limit",
        "monthly quota",
        "monthly api limit",
        "usage limit",
    )
    if any(marker in low for marker in markers):
        return True
    monthly = "monthly" in low or "this month" in low
    limitish = any(word in low for word in ("limit", "quota", "usage"))
    exhausted = any(word in low for word in ("reached", "exceeded", "exhausted", "depleted"))
    return monthly and limitish and exhausted


# Kept for older tests / Discord parsers that still call the KA-era name.
def is_klickanalytics_monthly_limit_message(text: str) -> bool:
    return is_rate_limit_message(text)


def _normalize_ohlcv(frame: pd.DataFrame, ticker: str) -> pd.DataFrame:
    if frame is None or frame.empty:
        raise DataError(f"No data returned for {ticker}.")

    rename_map = {
        "Date": "timestamp",
        "Datetime": "timestamp",
        "t": "timestamp",
        "timestamp": "timestamp",
        "date": "timestamp",
        "datetime": "timestamp",
        "Open": "open",
        "High": "high",
        "Low": "low",
        "Close": "close",
        "Volume": "volume",
        "o": "open",
        "h": "high",
        "l": "low",
        "c": "close",
        "v": "volume",
        "open": "open",
        "high": "high",
        "low": "low",
        "close": "close",
        "volume": "volume",
    }
    frame = frame.rename(columns=rename_map)
    if "timestamp" in frame.columns:
        frame["timestamp"] = pd.to_datetime(frame["timestamp"], errors="coerce", utc=True)
        frame = frame.dropna(subset=["timestamp"]).set_index("timestamp")

    required = ["open", "high", "low", "close", "volume"]
    missing = sorted(set(required) - set(frame.columns))
    if missing:
        raise DataError(
            f"Missing required columns: {missing}. Available columns: {list(frame.columns)}"
        )

    frame = frame[required].copy()
    for column in required:
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    frame = frame.dropna()
    frame = frame[(frame["open"] > 0) & (frame["high"] > 0) & (frame["low"] > 0) & (frame["close"] > 0)]
    frame = frame[frame["volume"] >= 0]
    if frame.empty:
        raise DataError(f"Data invalid/empty after cleaning for {ticker}.")
    return frame.sort_index()


def _auth_headers(api_key: str, api_secret: str) -> dict[str, str]:
    return {
        "APCA-API-KEY-ID": api_key,
        "APCA-API-SECRET-KEY": api_secret,
        "Accept": "application/json",
    }


def _raise_for_alpaca_response(response: requests.Response, *, context: str) -> None:
    if response.status_code < 400:
        return
    body = (response.text or "").strip()
    message = f"Alpaca market data {context} failed ({response.status_code}): {body or response.reason}"
    if response.status_code == 429 or is_rate_limit_message(body):
        raise MarketDataQuotaError(message)
    raise DataError(message)


def _fetch_bars(
    ticker: str,
    *,
    timeframe: str,
    start: datetime,
    api_key: str,
    api_secret: str,
    data_base_url: str,
    feed: str,
    max_bars: int,
    session: requests.Session | None = None,
) -> pd.DataFrame:
    base = data_base_url.rstrip("/") + "/"
    path = f"v2/stocks/{ticker}/bars"
    url = urljoin(base, path)
    headers = _auth_headers(api_key, api_secret)
    params: dict[str, Any] = {
        "timeframe": timeframe,
        "start": start.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "limit": min(10000, max(1, max_bars)),
        "adjustment": "split",
        "feed": feed,
        "sort": "asc",
    }

    rows: list[dict[str, Any]] = []
    page_token: str | None = None
    client = session or requests

    while True:
        call_params = dict(params)
        if page_token:
            call_params["page_token"] = page_token
        _record_api_call()
        try:
            response = client.get(url, headers=headers, params=call_params, timeout=60)
        except requests.RequestException as exc:
            raise DataError(f"Alpaca market data request failed for {ticker} ({timeframe}): {exc}") from exc

        _raise_for_alpaca_response(response, context=f"{ticker} {timeframe}")
        try:
            payload = response.json()
        except ValueError as exc:
            raise DataError(f"Alpaca market data returned non-JSON for {ticker} ({timeframe}).") from exc

        if not isinstance(payload, dict):
            raise DataError(f"Alpaca market data returned an unsupported JSON shape for {ticker}.")

        bars = payload.get("bars") or []
        if not isinstance(bars, list):
            raise DataError(f"Alpaca market data returned an unsupported bars payload for {ticker}.")
        rows.extend(row for row in bars if isinstance(row, dict))

        if len(rows) >= max_bars:
            rows = rows[:max_bars]
            break

        next_token = payload.get("next_page_token")
        if not next_token:
            break
        page_token = str(next_token)

    return _normalize_ohlcv(pd.DataFrame(rows), ticker=ticker)


def load_candles(
    ticker: str,
    *,
    api_key: str,
    api_secret: str,
    data_base_url: str = DEFAULT_DATA_BASE_URL,
    feed: str = DEFAULT_DATA_FEED,
) -> CandleData:
    """Load daily + synthetic 4h candles for ``ticker`` from Alpaca Market Data."""
    if not api_key or not api_secret:
        raise DataError("ALPACA_API_KEY and ALPACA_API_SECRET are required to load candles.")

    reset_cli_call_count()
    now = datetime.now(timezone.utc)
    daily_start = now - timedelta(days=650)
    daily = _fetch_bars(
        ticker,
        timeframe="1Day",
        start=daily_start,
        api_key=api_key,
        api_secret=api_secret,
        data_base_url=data_base_url,
        feed=feed,
        max_bars=400,
    )

    # Hourly history must cover the daily window (same approach as the prior KA loader).
    span_days = max(7, (daily.index[-1] - daily.index[0]).days + 14)
    hourly_bars = min(12000, max(800, span_days * 24))
    hourly_start = now - timedelta(days=min(span_days + 14, 730))
    hourly = _fetch_bars(
        ticker,
        timeframe="1Hour",
        start=hourly_start,
        api_key=api_key,
        api_secret=api_secret,
        data_base_url=data_base_url,
        feed=feed,
        max_bars=hourly_bars,
    )

    four_hour = (
        hourly.resample("4h")
        .agg({"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"})
        .dropna()
    )
    if len(daily) < 60:
        raise DataError("Not enough daily candles to compute indicators safely.")
    if len(four_hour) < 5:
        raise DataError("Not enough intraday candles to compute 4h context safely.")
    return CandleData(daily=daily, four_hour=four_hour)
