from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from typing import Any
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import pandas as pd
import requests

US_EASTERN = ZoneInfo("America/New_York")
_RTH_OPEN = time(9, 30)
_RTH_CLOSE = time(16, 0)
_FOUR_HOURS = pd.Timedelta(hours=4)


class DataError(Exception):
    pass


class MarketDataQuotaError(DataError):
    """Alpaca (or market-data provider) reported rate limit / quota exhaustion."""


DEFAULT_DATA_BASE_URL = "https://data.alpaca.markets"
DEFAULT_DATA_FEED = "iex"


@dataclass(frozen=True)
class CandleData:
    daily: pd.DataFrame
    four_hour: pd.DataFrame
    # Start timestamp of the newest hourly bar (UTC). Used by the stale-data gate.
    latest_intraday: pd.Timestamp | None = None


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


def alpaca_auth_headers(api_key: str, api_secret: str) -> dict[str, str]:
    """Public auth headers for Alpaca Market Data and Trading APIs."""
    return _auth_headers(api_key, api_secret)


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


def aggregate_four_hour_utc_clock(hourly: pd.DataFrame) -> pd.DataFrame:
    """Legacy 4h bins: pandas ``resample('4h')`` from UTC midnight.

    On a regular session those labels fall at 08:00 ET and 12:00 ET, so the
    09:30 open is mixed into a bin that starts before the cash open. Kept so
    tests can show the old alignment. Live and research paths use
    ``aggregate_session_four_hour``.
    """
    if hourly.empty:
        return hourly
    return (
        hourly.resample("4h")
        .agg({"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"})
        .dropna()
    )


def aggregate_session_four_hour(hourly: pd.DataFrame) -> pd.DataFrame:
    """Aggregate hourly bars into 4h buckets anchored at 09:30 America/New_York.

    Regular session only (09:30–16:00 ET). Bucket starts are 09:30 and 13:30 ET.
    The afternoon bucket ends at the 16:00 cash close, so it is shorter than 4h.
    Returned index is UTC, labeled at the bucket start.
    """
    if hourly.empty:
        return hourly.iloc[0:0]
    frame = hourly
    if frame.index.tz is None:
        frame = frame.tz_localize("UTC")
    et = frame.tz_convert(US_EASTERN).sort_index()
    bucket_ids: list[pd.Timestamp] = []
    keep_at: list[pd.Timestamp] = []
    for ts in et.index:
        clock = ts.timetz().replace(tzinfo=None)
        if clock < _RTH_OPEN or clock >= _RTH_CLOSE:
            continue
        open_dt = ts.normalize() + pd.Timedelta(hours=9, minutes=30)
        elapsed = ts - open_dt
        if elapsed < pd.Timedelta(0):
            continue
        slot = int(elapsed // _FOUR_HOURS)
        bucket_ids.append(open_dt + slot * _FOUR_HOURS)
        keep_at.append(ts)
    if not keep_at:
        return frame.iloc[0:0]
    subset = et.loc[keep_at].copy()
    subset["_bucket"] = bucket_ids
    grouped = subset.groupby("_bucket", sort=True)
    out = grouped.agg(
        open=("open", "first"),
        high=("high", "max"),
        low=("low", "min"),
        close=("close", "last"),
        volume=("volume", "sum"),
    )
    out.index = pd.DatetimeIndex(out.index).tz_convert("UTC")
    out.index.name = frame.index.name
    return out.dropna()


def load_candles(
    ticker: str,
    *,
    api_key: str,
    api_secret: str,
    data_base_url: str = DEFAULT_DATA_BASE_URL,
    feed: str = DEFAULT_DATA_FEED,
    daily_lookback_days: int = 650,
    max_daily_bars: int = 400,
    hourly_lookback_days: int | None = None,
    max_hourly_bars: int | None = None,
) -> CandleData:
    """Load daily + session 4h candles for ``ticker`` from Alpaca Market Data.

    Defaults match the live alert path (~400 daily bars). Research commands may
    request a longer daily window (about 2–4 years). Hourly history is still
    capped; older daily bars fall back to a daily-derived 4h series in backtests.

    4h bars are aggregated in America/New_York from 09:30 (then 13:30), not from
    UTC midnight. The previous ``resample('4h')`` bins started at 08:00 ET and
    12:00 ET and mixed the cash open into the pre-open bin.
    """
    if not api_key or not api_secret:
        raise DataError("ALPACA_API_KEY and ALPACA_API_SECRET are required to load candles.")

    reset_cli_call_count()
    now = datetime.now(timezone.utc)
    daily_start = now - timedelta(days=max(120, int(daily_lookback_days)))
    daily = _fetch_bars(
        ticker,
        timeframe="1Day",
        start=daily_start,
        api_key=api_key,
        api_secret=api_secret,
        data_base_url=data_base_url,
        feed=feed,
        max_bars=max(80, int(max_daily_bars)),
    )

    # Hourly history must cover the daily window used for indicators/backtests.
    span_days = max(7, (daily.index[-1] - daily.index[0]).days + 14)
    hourly_cap_days = 730 if hourly_lookback_days is None else max(7, int(hourly_lookback_days))
    hard_cap = 20000 if max_hourly_bars is not None else 12000
    hourly_bars = min(hard_cap, max(800, span_days * 24))
    if max_hourly_bars is not None:
        hourly_bars = min(hard_cap, max(800, int(max_hourly_bars)))
    hourly_start = now - timedelta(days=min(span_days + 14, hourly_cap_days))
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

    four_hour = aggregate_session_four_hour(hourly)
    if len(daily) < 60:
        raise DataError("Not enough daily candles to compute indicators safely.")
    if len(four_hour) < 5:
        raise DataError("Not enough intraday candles to compute 4h context safely.")
    latest = hourly.index[-1] if len(hourly.index) else None
    return CandleData(daily=daily, four_hour=four_hour, latest_intraday=latest)


def load_daily_bars(
    ticker: str,
    *,
    api_key: str,
    api_secret: str,
    data_base_url: str = DEFAULT_DATA_BASE_URL,
    feed: str = DEFAULT_DATA_FEED,
    daily_lookback_days: int = 1460,
    max_daily_bars: int = 1100,
) -> pd.DataFrame:
    """Daily bars only. Research path for TQQQ/SQQQ fills; live alerts still use ``load_candles``."""
    if not api_key or not api_secret:
        raise DataError("ALPACA_API_KEY and ALPACA_API_SECRET are required to load candles.")
    now = datetime.now(timezone.utc)
    start = now - timedelta(days=max(120, int(daily_lookback_days)))
    return _fetch_bars(
        ticker,
        timeframe="1Day",
        start=start,
        api_key=api_key,
        api_secret=api_secret,
        data_base_url=data_base_url,
        feed=feed,
        max_bars=max(80, int(max_daily_bars)),
    )
