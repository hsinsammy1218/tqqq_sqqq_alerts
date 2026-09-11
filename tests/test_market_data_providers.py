from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pandas as pd
import pytest

import data
from config import ConfigError, load_settings


def _ohlcv_frame(rows: int = 80, *, freq: str = "1D") -> pd.DataFrame:
    idx = pd.date_range("2024-01-01", periods=rows, freq=freq, tz="UTC")
    return pd.DataFrame(
        {
            "open": [100.0 + i * 0.1 for i in range(rows)],
            "high": [101.0 + i * 0.1 for i in range(rows)],
            "low": [99.0 + i * 0.1 for i in range(rows)],
            "close": [100.5 + i * 0.1 for i in range(rows)],
            "volume": [1_000_000 + i for i in range(rows)],
        },
        index=idx,
    )


def test_load_settings_requires_polygon_key(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("MARKET_DATA_PROVIDER", "polygon")
    monkeypatch.delenv("POLYGON_API_KEY", raising=False)
    monkeypatch.delenv("MASSIVE_API_KEY", raising=False)
    monkeypatch.delenv("KLICKANALYTICS_CLI_API_KEY", raising=False)
    with pytest.raises(ConfigError, match="POLYGON_API_KEY"):
        load_settings()


def test_load_settings_yahoo_does_not_require_klick_key(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("MARKET_DATA_PROVIDER", "yahoo")
    monkeypatch.delenv("KLICKANALYTICS_CLI_API_KEY", raising=False)
    monkeypatch.delenv("POLYGON_API_KEY", raising=False)
    settings = load_settings()
    assert settings.market_data_provider == "yahoo"
    assert settings.klickanalytics_api_key == ""


def test_load_settings_rejects_unknown_provider(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("MARKET_DATA_PROVIDER", "iex")
    monkeypatch.setenv("KLICKANALYTICS_CLI_API_KEY", "x")
    with pytest.raises(ConfigError, match="MARKET_DATA_PROVIDER must be"):
        load_settings()


def test_load_candles_polygon_uses_aggs(monkeypatch: pytest.MonkeyPatch):
    daily = _ohlcv_frame(100, freq="1D")
    hourly = _ohlcv_frame(200, freq="1h")
    calls: list[str] = []

    def fake_aggs(ticker, *, api_key, multiplier, timespan, start, end):
        calls.append(timespan)
        assert api_key == "poly-key"
        assert ticker == "QQQ"
        return daily if timespan == "day" else hourly

    monkeypatch.setattr(data, "_fetch_polygon_aggs", fake_aggs)
    candles = data.load_candles("QQQ", provider="polygon", polygon_api_key="poly-key")
    assert calls == ["day", "hour"]
    assert len(candles.daily) == 100
    assert len(candles.four_hour) >= 5
    assert data.cli_calls_attempted() == 0  # fake_aggs does not record; real path records inside


def test_load_candles_from_settings_routes_provider(monkeypatch: pytest.MonkeyPatch):
    sentinel = data.CandleData(daily=_ohlcv_frame(80), four_hour=_ohlcv_frame(20, freq="4h"))
    seen: dict[str, object] = {}

    def fake_load(ticker, api_key="", cli_command="ka", *, provider="klickanalytics", polygon_api_key=""):
        seen["ticker"] = ticker
        seen["provider"] = provider
        seen["polygon_api_key"] = polygon_api_key
        return sentinel

    monkeypatch.setattr(data, "load_candles", fake_load)
    settings = SimpleNamespace(
        qqq_ticker="QQQ",
        market_data_provider="polygon",
        klickanalytics_api_key="",
        klickanalytics_cli_command="ka",
        polygon_api_key="pk",
    )
    out = data.load_candles_from_settings(settings)
    assert out is sentinel
    assert seen == {"ticker": "QQQ", "provider": "polygon", "polygon_api_key": "pk"}


def test_format_cli_usage_line_provider_label():
    data.reset_cli_call_count()
    data._record_cli_call()
    line = data.format_cli_usage_line(provider="yahoo")
    assert line == "[yahoo] 1 API call attempted this run"


def test_polygon_http_maps_results(monkeypatch: pytest.MonkeyPatch):
    ts = int(datetime(2024, 6, 3, 14, tzinfo=timezone.utc).timestamp() * 1000)

    class Resp:
        status_code = 200
        text = ""
        reason = "OK"

        @staticmethod
        def json():
            return {
                "status": "OK",
                "results": [
                    {"t": ts, "o": 10, "h": 11, "l": 9, "c": 10.5, "v": 1000},
                    {"t": ts + 3_600_000, "o": 10.5, "h": 11.5, "l": 10, "c": 11, "v": 1100},
                ],
            }

    monkeypatch.setattr(data.requests, "get", lambda *args, **kwargs: Resp())
    frame = data._fetch_polygon_aggs(
        "QQQ",
        api_key="k",
        multiplier=1,
        timespan="hour",
        start=datetime(2024, 6, 1).date(),
        end=datetime(2024, 6, 4).date(),
    )
    assert len(frame) == 2
    assert list(frame.columns) == ["open", "high", "low", "close", "volume"]
