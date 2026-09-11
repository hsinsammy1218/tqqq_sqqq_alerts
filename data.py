from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any

import pandas as pd
import requests


class DataError(Exception):
    pass


class KlickAnalyticsQuotaError(DataError):
    """KlickAnalytics CLI reported monthly API usage limit reached."""


SUPPORTED_PROVIDERS = ("klickanalytics", "polygon", "alpaca", "yahoo")
# Delayed-only backends. Rejected when MARKET_DATA_REALTIME=true.
DELAYED_PROVIDERS = ("yahoo",)

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


_cli_calls_this_run = 0
_active_provider = "klickanalytics"


def reset_cli_call_count() -> None:
    global _cli_calls_this_run, _active_provider
    _cli_calls_this_run = 0
    _active_provider = "klickanalytics"


def cli_calls_attempted() -> int:
    return _cli_calls_this_run


def active_provider() -> str:
    return _active_provider


def format_cli_usage_line(
    *,
    reason: str | None = None,
    month_total: int | None = None,
    month_limit: int | None = None,
    provider: str | None = None,
) -> str:
    count = cli_calls_attempted()
    noun = "call" if count == 1 else "calls"
    suffix = f" ({reason})" if reason else ""
    label = (provider or _active_provider or "klickanalytics").strip().lower() or "klickanalytics"
    line = f"[{label}] {count} API {noun} attempted this run{suffix}"
    if month_total is not None and month_limit:
        line += f" · month {month_total}/{month_limit}"
    return line


def _record_cli_call() -> None:
    global _cli_calls_this_run
    _cli_calls_this_run += 1


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
    _record_cli_call()
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


def _synthesize_four_hour(hourly: pd.DataFrame) -> pd.DataFrame:
    four_hour = (
        hourly.resample("4h")
        .agg({"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"})
        .dropna()
    )
    return four_hour


def _finalize_candles(daily: pd.DataFrame, hourly: pd.DataFrame) -> CandleData:
    four_hour = _synthesize_four_hour(hourly)
    if len(daily) < 60:
        raise DataError("Not enough daily candles to compute indicators safely.")
    if len(four_hour) < 5:
        raise DataError("Not enough intraday candles to compute 4h context safely.")
    return CandleData(daily=daily, four_hour=four_hour)


def _load_candles_klickanalytics(ticker: str, api_key: str, cli_command: str) -> CandleData:
    daily = _fetch_daily_prices(ticker=ticker, cli_command=cli_command, api_key=api_key)
    # Hourly history must cover the daily window (800 bars ~33d would otherwise blank older backtest dates).
    span_days = max(7, (daily.index[-1] - daily.index[0]).days + 14)
    hourly_bars = min(12000, max(800, span_days * 24))
    hourly = _fetch_hourly_intraday(
        ticker=ticker, cli_command=cli_command, api_key=api_key, bars=hourly_bars
    )
    return _finalize_candles(daily, hourly)


def _polygon_base_url() -> str:
    return (os.getenv("POLYGON_API_BASE_URL") or "https://api.polygon.io").rstrip("/")


def _fetch_polygon_aggs(
    ticker: str,
    *,
    api_key: str,
    multiplier: int,
    timespan: str,
    start: date,
    end: date,
) -> pd.DataFrame:
    _record_cli_call()
    url = (
        f"{_polygon_base_url()}/v2/aggs/ticker/{ticker.upper()}/range/"
        f"{multiplier}/{timespan}/{start.isoformat()}/{end.isoformat()}"
    )
    params = {
        "adjusted": "true",
        "sort": "asc",
        "limit": "50000",
        "apiKey": api_key,
    }
    try:
        response = requests.get(url, params=params, timeout=60)
    except requests.RequestException as exc:
        raise DataError(f"Polygon request failed for {ticker}: {exc}") from exc

    if response.status_code == 401:
        raise DataError("Polygon authentication failed. Check POLYGON_API_KEY / MASSIVE_API_KEY.")
    if response.status_code == 403:
        raise DataError(
            "Polygon denied this request (plan may lack intraday aggregates). "
            "Starter+ is required for hourly bars; free Basic is end-of-day only."
        )
    if response.status_code == 429:
        raise DataError("Polygon rate limit exceeded. Wait and retry, or upgrade the plan.")
    if response.status_code >= 400:
        detail = (response.text or "").strip()[:500] or response.reason
        raise DataError(f"Polygon HTTP {response.status_code} for {ticker}: {detail}")

    try:
        payload = response.json()
    except ValueError as exc:
        raise DataError("Polygon returned non-JSON response.") from exc

    status = str(payload.get("status") or "").upper()
    results = payload.get("results")
    if status in {"ERROR", "NOT_AUTHORIZED"}:
        raise DataError(f"Polygon error for {ticker}: {payload.get('error') or payload}")
    if not isinstance(results, list) or not results:
        # Polygon returns OK with empty results when the window has no bars.
        raise DataError(f"Polygon returned no aggregate bars for {ticker} ({timespan}).")

    rows = []
    for item in results:
        if not isinstance(item, dict):
            continue
        ts_ms = item.get("t")
        if ts_ms is None:
            continue
        rows.append(
            {
                "timestamp": datetime.fromtimestamp(float(ts_ms) / 1000.0, tz=timezone.utc),
                "open": item.get("o"),
                "high": item.get("h"),
                "low": item.get("l"),
                "close": item.get("c"),
                "volume": item.get("v", 0),
            }
        )
    return _normalize_ohlcv(pd.DataFrame(rows), ticker=ticker)


def _load_candles_polygon(ticker: str, api_key: str, *, require_realtime: bool) -> CandleData:
    if require_realtime:
        # Fail fast if the key cannot access live snapshots (Advanced-tier entitlement).
        _fetch_polygon_snapshot(ticker, api_key=api_key)
    end = datetime.now(timezone.utc).date()
    daily_start = end - timedelta(days=800)
    daily = _fetch_polygon_aggs(
        ticker,
        api_key=api_key,
        multiplier=1,
        timespan="day",
        start=daily_start,
        end=end,
    )
    span_days = max(7, (daily.index[-1] - daily.index[0]).days + 14)
    # Cap hourly lookback; Polygon pages at 50k bars and free/basic may refuse intraday.
    hourly_days = min(span_days, 730)
    hourly_start = end - timedelta(days=hourly_days)
    hourly = _fetch_polygon_aggs(
        ticker,
        api_key=api_key,
        multiplier=1,
        timespan="hour",
        start=hourly_start,
        end=end,
    )
    return _finalize_candles(daily, hourly)


def _alpaca_data_base_url() -> str:
    return (os.getenv("ALPACA_DATA_BASE_URL") or "https://data.alpaca.markets").rstrip("/")


def _alpaca_headers(api_key: str, api_secret: str) -> dict[str, str]:
    return {
        "APCA-API-KEY-ID": api_key,
        "APCA-API-SECRET-KEY": api_secret,
        "Accept": "application/json",
    }


def _fetch_alpaca_bars(
    ticker: str,
    *,
    api_key: str,
    api_secret: str,
    timeframe: str,
    start: datetime,
    end: datetime,
    feed: str,
) -> pd.DataFrame:
    """Fetch stock bars from Alpaca Market Data API v2.

    ``feed=sip`` is consolidated real-time (paid). ``feed=iex`` is free/delayed.
    """
    _record_cli_call()
    url = f"{_alpaca_data_base_url()}/v2/stocks/{ticker.upper()}/bars"
    headers = _alpaca_headers(api_key, api_secret)
    params: dict[str, str | int] = {
        "timeframe": timeframe,
        "start": start.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "end": end.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "adjustment": "raw",
        "feed": feed,
        "limit": 10000,
        "sort": "asc",
    }
    rows: list[dict[str, Any]] = []
    page_token: str | None = None
    while True:
        page_params = dict(params)
        if page_token:
            page_params["page_token"] = page_token
        try:
            response = requests.get(url, headers=headers, params=page_params, timeout=60)
        except requests.RequestException as exc:
            raise DataError(f"Alpaca request failed for {ticker}: {exc}") from exc

        if response.status_code == 401:
            raise DataError("Alpaca authentication failed. Check ALPACA_API_KEY / ALPACA_API_SECRET.")
        if response.status_code == 403:
            raise DataError(
                f"Alpaca denied bars for feed={feed!r}. "
                "Real-time SIP requires a paid market-data subscription; "
                "free accounts only get delayed IEX (feed=iex)."
            )
        if response.status_code == 429:
            raise DataError("Alpaca rate limit exceeded. Wait and retry.")
        if response.status_code >= 400:
            detail = (response.text or "").strip()[:500] or response.reason
            raise DataError(f"Alpaca HTTP {response.status_code} for {ticker}: {detail}")

        try:
            payload = response.json()
        except ValueError as exc:
            raise DataError("Alpaca returned non-JSON response.") from exc

        bars = payload.get("bars")
        if isinstance(bars, list):
            for item in bars:
                if not isinstance(item, dict):
                    continue
                ts = item.get("t")
                if not ts:
                    continue
                rows.append(
                    {
                        "timestamp": pd.to_datetime(ts, utc=True),
                        "open": item.get("o"),
                        "high": item.get("h"),
                        "low": item.get("l"),
                        "close": item.get("c"),
                        "volume": item.get("v", 0),
                    }
                )
        page_token = payload.get("next_page_token")
        if not page_token:
            break
        _record_cli_call()

    if not rows:
        raise DataError(f"Alpaca returned no {timeframe} bars for {ticker} (feed={feed}).")
    return _normalize_ohlcv(pd.DataFrame(rows), ticker=ticker)


def _fetch_alpaca_latest_trade(
    ticker: str,
    *,
    api_key: str,
    api_secret: str,
    feed: str,
) -> dict[str, Any] | None:
    """Probe real-time access via latest trade (fails closed on SIP without entitlement)."""
    _record_cli_call()
    url = f"{_alpaca_data_base_url()}/v2/stocks/{ticker.upper()}/trades/latest"
    try:
        response = requests.get(
            url,
            headers=_alpaca_headers(api_key, api_secret),
            params={"feed": feed},
            timeout=30,
        )
    except requests.RequestException as exc:
        raise DataError(f"Alpaca latest-trade request failed for {ticker}: {exc}") from exc
    if response.status_code == 403:
        raise DataError(
            f"Alpaca real-time feed={feed!r} is not entitled on this key. "
            "Subscribe to SIP market data (paid) or set MARKET_DATA_REALTIME=false "
            "with ALPACA_DATA_FEED=iex for delayed data."
        )
    if response.status_code >= 400:
        detail = (response.text or "").strip()[:400] or response.reason
        raise DataError(f"Alpaca latest-trade HTTP {response.status_code}: {detail}")
    try:
        payload = response.json()
    except ValueError as exc:
        raise DataError("Alpaca latest-trade returned non-JSON.") from exc
    trade = payload.get("trade")
    return trade if isinstance(trade, dict) else None


def _load_candles_alpaca(
    ticker: str,
    *,
    api_key: str,
    api_secret: str,
    feed: str,
    require_realtime: bool,
) -> CandleData:
    normalized_feed = (feed or ("sip" if require_realtime else "iex")).strip().lower() or "iex"
    if require_realtime and normalized_feed != "sip":
        raise DataError(
            f"MARKET_DATA_REALTIME=true requires ALPACA_DATA_FEED=sip (got {normalized_feed!r}). "
            "IEX is delayed-only on free Alpaca plans."
        )
    # Confirm entitlement before pulling history (clearer error than empty bars).
    if require_realtime or normalized_feed == "sip":
        _fetch_alpaca_latest_trade(
            ticker, api_key=api_key, api_secret=api_secret, feed=normalized_feed
        )

    end = datetime.now(timezone.utc)
    daily_start = end - timedelta(days=800)
    daily = _fetch_alpaca_bars(
        ticker,
        api_key=api_key,
        api_secret=api_secret,
        timeframe="1Day",
        start=daily_start,
        end=end,
        feed=normalized_feed,
    )
    span_days = max(7, (daily.index[-1] - daily.index[0]).days + 14)
    hourly_days = min(span_days, 730)
    hourly_start = end - timedelta(days=hourly_days)
    hourly = _fetch_alpaca_bars(
        ticker,
        api_key=api_key,
        api_secret=api_secret,
        timeframe="1Hour",
        start=hourly_start,
        end=end,
        feed=normalized_feed,
    )
    return _finalize_candles(daily, hourly)


def _fetch_polygon_snapshot(ticker: str, *, api_key: str) -> dict[str, Any]:
    """Stock snapshot — requires a real-time (or delayed snapshot) Polygon entitlement."""
    _record_cli_call()
    url = f"{_polygon_base_url()}/v2/snapshot/locale/us/markets/stocks/tickers/{ticker.upper()}"
    try:
        response = requests.get(url, params={"apiKey": api_key}, timeout=30)
    except requests.RequestException as exc:
        raise DataError(f"Polygon snapshot request failed for {ticker}: {exc}") from exc
    if response.status_code == 401:
        raise DataError("Polygon authentication failed on snapshot. Check POLYGON_API_KEY.")
    if response.status_code == 403:
        raise DataError(
            "Polygon snapshot denied. Real-time US stock data needs Stocks Advanced "
            "(~$199/mo). Starter ($29) is 15-minute delayed and is not real-time."
        )
    if response.status_code >= 400:
        detail = (response.text or "").strip()[:500] or response.reason
        raise DataError(f"Polygon snapshot HTTP {response.status_code}: {detail}")
    try:
        payload = response.json()
    except ValueError as exc:
        raise DataError("Polygon snapshot returned non-JSON.") from exc
    ticker_payload = payload.get("ticker")
    if not isinstance(ticker_payload, dict):
        raise DataError(f"Polygon snapshot missing ticker payload for {ticker}.")
    return ticker_payload


def _load_candles_yahoo(ticker: str) -> CandleData:
    try:
        import yfinance as yf
    except ImportError as exc:
        raise DataError(
            "Yahoo provider requires yfinance. Install with 'pip install yfinance'."
        ) from exc

    reset_cli_call_count()
    _record_cli_call()
    try:
        daily_raw = yf.download(
            ticker,
            period="2y",
            interval="1d",
            auto_adjust=False,
            progress=False,
            threads=False,
        )
    except Exception as exc:  # noqa: BLE001
        raise DataError(f"Yahoo daily download failed for {ticker}: {exc}") from exc

    _record_cli_call()
    try:
        # Yahoo limits 1h history (~60 days). Enough for live 4h context; not for deep backtests.
        hourly_raw = yf.download(
            ticker,
            period="60d",
            interval="1h",
            auto_adjust=False,
            progress=False,
            threads=False,
        )
    except Exception as exc:  # noqa: BLE001
        raise DataError(f"Yahoo hourly download failed for {ticker}: {exc}") from exc

    def _frame_from_yahoo(raw: Any) -> pd.DataFrame:
        if raw is None or getattr(raw, "empty", True):
            raise DataError(f"Yahoo returned empty data for {ticker}.")
        frame = raw.copy()
        if isinstance(frame.columns, pd.MultiIndex):
            frame.columns = [str(col[0]).title() if isinstance(col, tuple) else str(col) for col in frame.columns]
        frame = frame.reset_index()
        return _normalize_ohlcv(frame, ticker=ticker)

    daily = _frame_from_yahoo(daily_raw)
    hourly = _frame_from_yahoo(hourly_raw)
    return _finalize_candles(daily, hourly)


def load_candles(
    ticker: str,
    api_key: str = "",
    cli_command: str = "ka",
    *,
    provider: str = "klickanalytics",
    polygon_api_key: str = "",
    alpaca_api_key: str = "",
    alpaca_api_secret: str = "",
    alpaca_data_feed: str = "sip",
    require_realtime: bool = True,
) -> CandleData:
    """Fetch daily + synthetic 4h candles for ``ticker``.

    Providers (from https://www.timestored.com/data/realtime-stock-data-apis):
    - ``alpaca`` — Alpaca Market Data (``feed=sip`` = real-time paid; ``iex`` = delayed free)
    - ``polygon`` — Polygon.io / Massive (real-time needs Stocks Advanced ~$199; Starter is delayed)
    - ``klickanalytics`` — existing CLI (tight monthly quota)
    - ``yahoo`` — delayed/free via yfinance (blocked when MARKET_DATA_REALTIME=true)
    """
    global _active_provider
    normalized = (provider or "klickanalytics").strip().lower()
    if normalized not in SUPPORTED_PROVIDERS:
        raise DataError(
            f"Unsupported MARKET_DATA_PROVIDER={provider!r}. "
            f"Choose one of: {', '.join(SUPPORTED_PROVIDERS)}."
        )
    if require_realtime and normalized in DELAYED_PROVIDERS:
        raise DataError(
            f"MARKET_DATA_PROVIDER={normalized!r} is delayed-only. "
            "For real-time data set MARKET_DATA_PROVIDER=alpaca (SIP) or polygon "
            "(Stocks Advanced), or set MARKET_DATA_REALTIME=false to allow delayed feeds."
        )
    _active_provider = normalized
    reset_cli_call_count()

    if normalized == "klickanalytics":
        if not api_key:
            raise DataError("KLICKANALYTICS_CLI_API_KEY is required for MARKET_DATA_PROVIDER=klickanalytics.")
        return _load_candles_klickanalytics(ticker, api_key=api_key, cli_command=cli_command or "ka")

    if normalized == "polygon":
        key = (polygon_api_key or api_key or "").strip()
        if not key:
            raise DataError("POLYGON_API_KEY (or MASSIVE_API_KEY) is required for MARKET_DATA_PROVIDER=polygon.")
        return _load_candles_polygon(ticker, api_key=key, require_realtime=require_realtime)

    if normalized == "alpaca":
        key = (alpaca_api_key or "").strip()
        secret = (alpaca_api_secret or "").strip()
        if not key or not secret:
            raise DataError(
                "ALPACA_API_KEY and ALPACA_API_SECRET are required for MARKET_DATA_PROVIDER=alpaca."
            )
        return _load_candles_alpaca(
            ticker,
            api_key=key,
            api_secret=secret,
            feed=alpaca_data_feed,
            require_realtime=require_realtime,
        )

    return _load_candles_yahoo(ticker)


def load_candles_from_settings(settings: Any) -> CandleData:
    return load_candles(
        ticker=settings.qqq_ticker,
        api_key=getattr(settings, "klickanalytics_api_key", "") or "",
        cli_command=getattr(settings, "klickanalytics_cli_command", "ka") or "ka",
        provider=getattr(settings, "market_data_provider", "klickanalytics") or "klickanalytics",
        polygon_api_key=getattr(settings, "polygon_api_key", "") or "",
        alpaca_api_key=getattr(settings, "alpaca_api_key", "") or "",
        alpaca_api_secret=getattr(settings, "alpaca_api_secret", "") or "",
        alpaca_data_feed=getattr(settings, "alpaca_data_feed", "sip") or "sip",
        require_realtime=bool(getattr(settings, "market_data_realtime", True)),
    )
