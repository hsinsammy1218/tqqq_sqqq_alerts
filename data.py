from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass

import pandas as pd


class DataError(Exception):
    pass


class KlickAnalyticsQuotaError(DataError):
    """KlickAnalytics CLI reported monthly API usage limit reached."""


_MONTHLY_LIMIT_MARKERS = (
    "monthly limit",
    "monthly quota",
    "monthly api",
    "monthly usage",
    "monthly request",
    "monthly call",
    "monthly cli usage limit",
    "monthly_cli_limit_reached",
    "limit for the month",
    "reached for the month",
    "quota for the month",
    "this month's limit",
    "this month",
)


def _quota_error_code_in_payload(text: str) -> bool:
    start = text.find("{")
    if start < 0:
        return False
    try:
        payload = json.loads(text[start:])
    except json.JSONDecodeError:
        return False
    if not isinstance(payload, dict):
        return False
    code = str(payload.get("error_code", "")).lower()
    if "monthly" in code and ("limit" in code or "quota" in code):
        return True
    for key in ("stderr", "stdout", "message", "error"):
        value = payload.get(key)
        if isinstance(value, str) and is_klickanalytics_monthly_limit_message(value):
            return True
    return False


def is_klickanalytics_monthly_limit_message(text: str) -> bool:
    low = text.lower()
    if not low:
        return False
    if any(marker in low for marker in _MONTHLY_LIMIT_MARKERS):
        return True
    monthly = "monthly" in low or " per month" in low or "this month" in low
    limitish = any(word in low for word in ("limit", "quota", "usage", "calls", "requests"))
    exhausted = any(word in low for word in ("reached", "exceeded", "exhausted", "depleted", "used up"))
    return monthly and limitish and exhausted


@dataclass(frozen=True)
class CandleData:
    daily: pd.DataFrame
    four_hour: pd.DataFrame


def _normalize_ohlcv(frame: pd.DataFrame, ticker: str) -> pd.DataFrame:
    if frame is None or frame.empty:
        raise DataError(f"No data returned for {ticker}.")

    rename_map = {
        "Date": "timestamp",
        "Datetime": "timestamp",
        "px_date": "timestamp",
        "date": "timestamp",
        "datetime": "timestamp",
        "Open": "open",
        "High": "high",
        "Low": "low",
        "Close": "close",
        "Volume": "volume",
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


def _extract_records(payload: object) -> list[dict]:
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if isinstance(payload, dict):
        for key in ("results", "data", "rows", "items", "bars", "prices", "recent_bars"):
            value = payload.get(key)
            if isinstance(value, list):
                return [row for row in value if isinstance(row, dict)]
        if "result" in payload and isinstance(payload["result"], list):
            return [row for row in payload["result"] if isinstance(row, dict)]
    raise DataError("KlickAnalytics returned an unsupported JSON shape.")


def _run_ka_json(cli_command: str, args: list[str], api_key: str) -> object:
    env = os.environ.copy()
    env["KLICKANALYTICS_CLI_API_KEY"] = api_key
    cmd = [cli_command] + args
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=False,
            env=env,
            stdin=subprocess.DEVNULL,
            timeout=60,
        )
    except FileNotFoundError as exc:
        raise DataError(
            f"KlickAnalytics CLI not found: '{cli_command}'. Install with 'pip install klickanalytics-cli'."
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise DataError(f"KlickAnalytics CLI timed out for command: {' '.join(cmd)}") from exc

    if proc.returncode != 0:
        stderr = (proc.stderr or "").strip()
        stdout = (proc.stdout or "").strip()
        detail = "\n".join(part for part in (stderr, stdout) if part).strip() or "unknown error"
        message = f"KlickAnalytics CLI command failed: {detail}"
        if is_klickanalytics_monthly_limit_message(detail) or _quota_error_code_in_payload(detail):
            raise KlickAnalyticsQuotaError(message)
        raise DataError(message)

    raw = (proc.stdout or "").strip()
    if not raw:
        raise DataError("KlickAnalytics CLI returned empty output.")
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise DataError("KlickAnalytics CLI did not return valid JSON. Use -output json.") from exc


def _fetch_daily_prices(ticker: str, cli_command: str, api_key: str) -> pd.DataFrame:
    payload = _run_ka_json(
        cli_command=cli_command,
        args=["prices", "-s", ticker, "-l", "400", "-output", "json"],
        api_key=api_key,
    )
    records = _extract_records(payload)
    return _normalize_ohlcv(pd.DataFrame(records), ticker=ticker)


def _fetch_hourly_intraday(ticker: str, cli_command: str, api_key: str, *, bars: int) -> pd.DataFrame:
    payload = _run_ka_json(
        cli_command=cli_command,
        args=["intraday", "-s", ticker, "-tf", "1hour", "-bars", str(int(bars)), "-output", "json"],
        api_key=api_key,
    )
    records = _extract_records(payload)
    return _normalize_ohlcv(pd.DataFrame(records), ticker=ticker)


def load_candles(ticker: str, api_key: str, cli_command: str) -> CandleData:
    daily = _fetch_daily_prices(ticker=ticker, cli_command=cli_command, api_key=api_key)
    # Hourly history must cover the daily window (800 bars ~33d would otherwise blank older backtest dates).
    span_days = max(7, (daily.index[-1] - daily.index[0]).days + 14)
    hourly_bars = min(12000, max(800, span_days * 24))
    hourly = _fetch_hourly_intraday(
        ticker=ticker, cli_command=cli_command, api_key=api_key, bars=hourly_bars
    )

    # Build synthetic 4h candles from 1h bars from KlickAnalytics.
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
