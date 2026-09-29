from __future__ import annotations

import data


def test_api_call_counter_tracks_attempts(monkeypatch):
    bars = [
        {
            "t": "2024-01-02T05:00:00Z",
            "o": 1.0,
            "h": 2.0,
            "l": 0.5,
            "c": 1.5,
            "v": 100,
        }
    ]

    class Resp:
        status_code = 200
        text = ""
        reason = "OK"

        def json(self):
            return {"bars": bars, "next_page_token": None}

    monkeypatch.setattr(data.requests, "get", lambda *args, **kwargs: Resp())
    data.reset_cli_call_count()
    data._fetch_bars(
        "QQQ",
        timeframe="1Day",
        start=__import__("datetime").datetime(2024, 1, 1, tzinfo=__import__("datetime").timezone.utc),
        api_key="key",
        api_secret="secret",
        data_base_url="https://data.alpaca.markets",
        feed="iex",
        max_bars=10,
    )
    data._fetch_bars(
        "QQQ",
        timeframe="1Hour",
        start=__import__("datetime").datetime(2024, 1, 1, tzinfo=__import__("datetime").timezone.utc),
        api_key="key",
        api_secret="secret",
        data_base_url="https://data.alpaca.markets",
        feed="iex",
        max_bars=10,
    )
    assert data.cli_calls_attempted() == 2
    assert data.format_cli_usage_line() == "[alpaca] 2 API calls attempted this run"


def test_format_cli_usage_line_with_reason_and_month():
    data.reset_cli_call_count()
    line = data.format_cli_usage_line(reason="market closed", month_total=120, month_limit=500)
    assert "market closed" in line
    assert "month 120/500" in line
    assert "[alpaca]" in line


def test_rate_limit_raises_quota_error(monkeypatch):
    class Resp:
        status_code = 429
        text = "too many requests"
        reason = "Too Many Requests"

        def json(self):
            return {}

    monkeypatch.setattr(data.requests, "get", lambda *args, **kwargs: Resp())
    with __import__("pytest").raises(data.MarketDataQuotaError):
        data._fetch_bars(
            "QQQ",
            timeframe="1Day",
            start=__import__("datetime").datetime(2024, 1, 1, tzinfo=__import__("datetime").timezone.utc),
            api_key="key",
            api_secret="secret",
            data_base_url="https://data.alpaca.markets",
            feed="iex",
            max_bars=10,
        )


def test_generic_http_error(monkeypatch):
    class Resp:
        status_code = 400
        text = "invalid symbol"
        reason = "Bad Request"

        def json(self):
            return {}

    monkeypatch.setattr(data.requests, "get", lambda *args, **kwargs: Resp())
    with __import__("pytest").raises(data.DataError):
        data._fetch_bars(
            "QQQ",
            timeframe="1Day",
            start=__import__("datetime").datetime(2024, 1, 1, tzinfo=__import__("datetime").timezone.utc),
            api_key="key",
            api_secret="secret",
            data_base_url="https://data.alpaca.markets",
            feed="iex",
            max_bars=10,
        )
