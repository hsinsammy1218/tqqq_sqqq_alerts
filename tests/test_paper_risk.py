"""Unit tests for env-gated paper risk controls (kill / buy cap / TQQQ-only)."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from alpaca_paper import (
    PAPER_TRADING_BASE_URL,
    OrderIntent,
    execute_paper_orders,
    select_buy_notional,
)
from config import ConfigError, load_settings
from paper_risk import (
    RISK_PASS,
    RISK_TRIPPED,
    RISK_UNKNOWN,
    KillStatus,
    PaperRiskError,
    PaperRiskLimits,
    apply_buy_notional_cap,
    assess_kill_from_broker,
    evaluate_loss_kill,
    filter_intents_for_kill,
    filter_intents_for_tqqq_only,
    parse_equity_fields,
)
from strategy_types import AlertDecision, PositionState


def _alert(**kwargs) -> AlertDecision:
    base = dict(
        alert_type="BUY",
        symbol="TQQQ",
        qqq_trend_reason="regime=bull",
        bullish_score=70,
        bearish_score=20,
        confidence_score=80,
        entry_zone_low=1.0,
        entry_zone_high=2.0,
        stop_loss=0.9,
        take_profit=2.5,
        stretch_take_profit=3.0,
        max_hold_date="2026-10-10",
        timestamp="2026-09-30T00:00:00Z",
        notes="test",
    )
    base.update(kwargs)
    return AlertDecision(**base)


def test_apply_buy_notional_cap_off_and_on():
    assert apply_buy_notional_cap(500, 0) == 500
    assert apply_buy_notional_cap(500, -1) == 500
    assert apply_buy_notional_cap(500, 200) == 200
    assert apply_buy_notional_cap(100, 200) == 100


def test_select_buy_notional_respects_cap():
    notional, mode = select_buy_notional(
        vol_sizing=False,
        fixed_notional=500,
        equity_pct=0,
        equity=100_000,
        max_buy_notional=200,
    )
    assert notional == 200
    assert mode == "fixed_notional_capped"


def test_select_buy_notional_equity_pct_is_fraction_then_usd_cap():
    """ALPACA_PAPER_EQUITY_PCT is a fraction (0.15 = 15%), then PAPER_MAX_BUY_NOTIONAL clamps."""
    under, under_mode = select_buy_notional(
        vol_sizing=False,
        fixed_notional=500,
        equity_pct=0.15,
        equity=1_000,
        max_buy_notional=300,
    )
    assert under == pytest.approx(150)
    assert under_mode == "equity_pct"

    capped, capped_mode = select_buy_notional(
        vol_sizing=False,
        fixed_notional=500,
        equity_pct=0.15,
        equity=10_000,
        max_buy_notional=300,
    )
    assert capped == 300
    assert capped_mode == "equity_pct_capped"


def test_select_buy_notional_vol_fallback_notional_still_used():
    """ALPACA_PAPER_NOTIONAL remains the fallback when vol-sizing cannot read equity."""
    fallback, fallback_mode = select_buy_notional(
        vol_sizing=True,
        fixed_notional=500,
        equity_pct=0.15,
        equity=None,
        risk_fraction=0.01,
        stop_loss_pct=0.08,
        max_buy_notional=300,
    )
    assert fallback == 300
    assert fallback_mode == "fallback_notional_capped"


def test_filter_tqqq_only_blocks_sqqq_buys_keeps_sells():
    intents = [
        OrderIntent(symbol="TQQQ", side="sell", purpose="flip_exit"),
        OrderIntent(symbol="SQQQ", side="buy", purpose="flip_entry"),
    ]
    kept, skipped = filter_intents_for_tqqq_only(intents, tqqq_only=True)
    assert kept == [intents[0]]
    assert len(skipped) == 1
    assert "SQQQ" in skipped[0]

    kept_off, skipped_off = filter_intents_for_tqqq_only(intents, tqqq_only=False)
    assert kept_off == intents
    assert skipped_off == []


def test_evaluate_loss_kill_daily_and_weekly():
    ok = evaluate_loss_kill(
        equity=1000,
        last_equity=1010,
        week_start_equity=1050,
        max_daily_loss_usd=50,
        max_weekly_loss_usd=100,
    )
    assert ok.tripped is False
    assert ok.daily_pnl == pytest.approx(-10)

    daily = evaluate_loss_kill(
        equity=940,
        last_equity=1000,
        week_start_equity=1000,
        max_daily_loss_usd=50,
        max_weekly_loss_usd=100,
    )
    assert daily.tripped is True
    assert "daily loss" in daily.reason

    weekly = evaluate_loss_kill(
        equity=890,
        last_equity=900,
        week_start_equity=1000,
        max_daily_loss_usd=50,
        max_weekly_loss_usd=100,
    )
    assert weekly.tripped is True
    assert "weekly loss" in weekly.reason

    off = evaluate_loss_kill(
        equity=500,
        last_equity=1000,
        week_start_equity=1000,
        max_daily_loss_usd=0,
        max_weekly_loss_usd=0,
    )
    assert off.tripped is False


def test_filter_intents_for_kill_blocks_buys():
    kill = KillStatus(tripped=True, reason="daily loss -55")
    intents = [
        OrderIntent(symbol="TQQQ", side="sell", purpose="exit"),
        OrderIntent(symbol="TQQQ", side="buy", purpose="entry"),
    ]
    kept, skipped = filter_intents_for_kill(intents, kill=kill)
    assert kept == [intents[0]]
    assert len(skipped) == 1


def test_parse_equity_fields():
    equity, last = parse_equity_fields({"equity": "12345.67", "last_equity": "12000"})
    assert equity == pytest.approx(12345.67)
    assert last == pytest.approx(12000)


def test_paper_risk_limits_any_enabled():
    assert PaperRiskLimits().any_enabled() is False
    assert PaperRiskLimits(max_buy_notional=200).any_enabled() is True
    assert PaperRiskLimits(tqqq_only=True).any_enabled() is True


def test_load_settings_paper_risk_defaults_off(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ALPACA_API_KEY", "test-key")
    monkeypatch.setenv("ALPACA_API_SECRET", "test-secret")
    monkeypatch.setenv("PAPER_MAX_BUY_NOTIONAL", "0")
    monkeypatch.setenv("PAPER_TQQQ_ONLY", "false")
    monkeypatch.setenv("PAPER_MAX_DAILY_LOSS_USD", "0")
    monkeypatch.setenv("PAPER_MAX_WEEKLY_LOSS_USD", "0")
    settings = load_settings()
    assert settings.paper_max_buy_notional == 0
    assert settings.paper_tqqq_only is False
    assert settings.paper_max_daily_loss_usd == 0
    assert settings.paper_max_weekly_loss_usd == 0


def test_load_settings_equity_pct_is_fraction_not_whole_percent(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ALPACA_API_KEY", "test-key")
    monkeypatch.setenv("ALPACA_API_SECRET", "test-secret")
    monkeypatch.setenv("ALPACA_PAPER_EQUITY_PCT", "0.15")
    settings = load_settings()
    assert settings.alpaca_paper_equity_pct == pytest.approx(0.15)

    monkeypatch.setenv("ALPACA_PAPER_EQUITY_PCT", "15")
    with pytest.raises(ConfigError, match="fraction"):
        load_settings()


def test_load_settings_paper_risk_rejects_negative(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ALPACA_API_KEY", "test-key")
    monkeypatch.setenv("ALPACA_API_SECRET", "test-secret")
    monkeypatch.setenv("PAPER_MAX_BUY_NOTIONAL", "-1")
    with pytest.raises(ConfigError, match="PAPER_MAX_BUY_NOTIONAL"):
        load_settings()


def test_execute_paper_orders_tqqq_only_blocks_sqqq_buy(tmp_path, capfd):
    session = MagicMock()

    def fake_request(method, url, headers=None, json=None, timeout=None):
        resp = MagicMock()
        resp.status_code = 200
        if url.endswith("/v2/account"):
            resp.text = '{"equity":"100000","last_equity":"100000"}'
            resp.json.return_value = {"equity": "100000", "last_equity": "100000"}
        else:
            raise AssertionError(f"unexpected {method} {url}")
        return resp

    session.request.side_effect = fake_request
    session.get.side_effect = AssertionError("should not GET for tqqq-only block")

    results = execute_paper_orders(
        _alert(alert_type="BUY", symbol="SQQQ"),
        None,
        paper_trading=True,
        dry_run=False,
        api_key="k",
        api_secret="s",
        trading_base_url=PAPER_TRADING_BASE_URL,
        data_base_url="https://data.alpaca.markets",
        feed="iex",
        fixed_notional=500,
        equity_pct=0,
        limit_offset_bps=10,
        tqqq_only=True,
        logger=MagicMock(),
        session=session,
        trade_log_path=tmp_path / "trades.jsonl",
    )
    assert len(results) == 1
    assert results[0].status == "blocked"
    assert "TQQQ_ONLY" in results[0].detail or "tqqq" in results[0].detail.lower()
    assert "PAPER_TQQQ_ONLY" in capfd.readouterr().out


def test_execute_paper_orders_kill_blocks_buy(tmp_path, monkeypatch):
    session = MagicMock()

    def fake_get(url, headers=None, params=None, timeout=None):
        resp = MagicMock()
        resp.status_code = 200
        if url.rstrip("/").endswith("/v2/account"):
            resp.text = '{"equity":"900","last_equity":"1000"}'
            resp.json.return_value = {"equity": "900", "last_equity": "1000"}
            return resp
        if "portfolio/history" in url:
            resp.text = '{"equity":[1000,950,900]}'
            resp.json.return_value = {"equity": [1000, 950, 900]}
            return resp
        if "/quotes/latest" in url:
            resp.status_code = 404
            resp.text = "not found"
            resp.reason = "not found"
            return resp
        raise AssertionError(f"unexpected GET {url}")

    session.get.side_effect = fake_get

    def fake_request(method, url, headers=None, json=None, timeout=None):
        # Initial equity fetch in execute_paper_orders uses request().
        resp = MagicMock()
        resp.status_code = 200
        if url.endswith("/v2/account"):
            resp.text = '{"equity":"900","last_equity":"1000"}'
            resp.json.return_value = {"equity": "900", "last_equity": "1000"}
            return resp
        raise AssertionError(f"unexpected {method} {url}")

    session.request.side_effect = fake_request

    results = execute_paper_orders(
        _alert(alert_type="BUY", symbol="TQQQ"),
        None,
        paper_trading=True,
        dry_run=False,
        api_key="k",
        api_secret="s",
        trading_base_url=PAPER_TRADING_BASE_URL,
        data_base_url="https://data.alpaca.markets",
        feed="iex",
        fixed_notional=500,
        equity_pct=0,
        limit_offset_bps=10,
        max_daily_loss_usd=50,
        max_weekly_loss_usd=0,
        logger=MagicMock(),
        session=session,
        trade_log_path=tmp_path / "trades.jsonl",
        discord_webhook_url="",
    )
    assert any(r.status == "blocked" and "kill" in r.detail.lower() for r in results)
    assert not any(r.ok and r.status not in {"blocked", "skipped"} for r in results)


def _limits(**kwargs) -> PaperRiskLimits:
    base = dict(max_daily_loss_usd=25, max_weekly_loss_usd=60)
    base.update(kwargs)
    return PaperRiskLimits(**base)


def test_evaluate_missing_mark_is_unknown_not_pass():
    unknown = evaluate_loss_kill(
        equity=1000,
        last_equity=None,
        week_start_equity=1000,
        max_daily_loss_usd=25,
        max_weekly_loss_usd=0,
    )
    assert unknown.state == RISK_UNKNOWN
    assert unknown.tripped is False
    assert unknown.blocks_new_exposure() is True


def test_assess_account_down_is_unknown(monkeypatch):
    session = MagicMock()
    session.get.side_effect = __import__("requests").RequestException("down")
    status = assess_kill_from_broker(
        api_key="k",
        api_secret="s",
        trading_base_url=PAPER_TRADING_BASE_URL,
        limits=_limits(),
        session=session,
        logger=MagicMock(),
    )
    assert status.state == RISK_UNKNOWN
    assert "account unavailable" in status.reason


def test_assess_history_down_is_unknown():
    session = MagicMock()

    def fake_get(url, headers=None, params=None, timeout=None):
        resp = MagicMock()
        if url.rstrip("/").endswith("/v2/account"):
            resp.status_code = 200
            resp.text = '{"equity":"1000","last_equity":"1000"}'
            resp.json.return_value = {"equity": "1000", "last_equity": "1000"}
            return resp
        if "portfolio/history" in url:
            raise __import__("requests").RequestException("history down")
        raise AssertionError(url)

    session.get.side_effect = fake_get
    status = assess_kill_from_broker(
        api_key="k",
        api_secret="s",
        trading_base_url=PAPER_TRADING_BASE_URL,
        limits=_limits(max_daily_loss_usd=0, max_weekly_loss_usd=60),
        session=session,
        logger=MagicMock(),
    )
    assert status.state == RISK_UNKNOWN
    assert "history" in status.reason


def test_assess_malformed_account_is_unknown():
    session = MagicMock()

    def fake_get(url, headers=None, params=None, timeout=None):
        resp = MagicMock()
        resp.status_code = 200
        resp.text = '{"equity":"nope"}'
        resp.json.return_value = {"equity": "nope"}
        return resp

    session.get.side_effect = fake_get
    status = assess_kill_from_broker(
        api_key="k",
        api_secret="s",
        trading_base_url=PAPER_TRADING_BASE_URL,
        limits=_limits(max_weekly_loss_usd=0),
        session=session,
        logger=MagicMock(),
    )
    assert status.state == RISK_UNKNOWN
    assert "missing" in status.reason


def test_unknown_blocks_buy_and_flip_entry_allows_sell():
    kill = KillStatus(tripped=False, state=RISK_UNKNOWN, reason="account unavailable")
    intents = [
        OrderIntent(symbol="TQQQ", side="sell", purpose="flip_exit"),
        OrderIntent(symbol="SQQQ", side="buy", purpose="flip_entry"),
        OrderIntent(symbol="TQQQ", side="buy", purpose="entry"),
    ]
    kept, skipped = filter_intents_for_kill(intents, kill=kill)
    assert kept == [intents[0]]
    assert len(skipped) == 2
    assert all("UNKNOWN" in line for line in skipped)
    assert kill.state == RISK_UNKNOWN
    assert RISK_PASS != kill.state
    assert RISK_TRIPPED != kill.state


def test_execute_unknown_blocks_buy_allows_sell(tmp_path):
    def _session(*, sell: bool):
        session = MagicMock()
        posts: list[str] = []

        def fake_get(url, headers=None, params=None, timeout=None):
            resp = MagicMock()
            if url.rstrip("/").endswith("/v2/account"):
                raise __import__("requests").RequestException("account down")
            if "/quotes/latest" in url:
                resp.status_code = 404
                resp.text = "not found"
                resp.reason = "not found"
                return resp
            if "/trades/latest" in url:
                resp.status_code = 200
                resp.json.return_value = {"trade": {"p": 50.0}}
                resp.text = '{"trade":{"p":50.0}}'
                return resp
            if url.endswith("/v2/positions/TQQQ"):
                resp.status_code = 200
                resp.json.return_value = {"qty": "2"}
                resp.text = '{"qty":"2"}'
                return resp
            if "orders:client_order_id:" in url:
                resp.status_code = 404
                resp.text = "not found"
                resp.reason = "not found"
                return resp
            raise AssertionError(url)

        def fake_request(method, url, headers=None, json=None, timeout=None):
            resp = MagicMock()
            if url.endswith("/v2/account"):
                raise __import__("requests").RequestException("account down")
            if url.endswith("/v2/orders") and method == "POST":
                posts.append(json["side"])
                resp.status_code = 200
                resp.text = '{"id":"ord","status":"accepted"}'
                resp.json.return_value = {"id": "ord", "status": "accepted"}
                return resp
            raise AssertionError(f"{method} {url}")

        session.get.side_effect = fake_get
        session.request.side_effect = fake_request
        return session, posts

    buy_session, buy_posts = _session(sell=False)
    buy = execute_paper_orders(
        _alert(alert_type="BUY", symbol="TQQQ"),
        None,
        paper_trading=True,
        dry_run=False,
        api_key="k",
        api_secret="s",
        trading_base_url=PAPER_TRADING_BASE_URL,
        data_base_url="https://data.alpaca.markets",
        feed="iex",
        fixed_notional=500,
        equity_pct=0,
        limit_offset_bps=10,
        max_daily_loss_usd=25,
        logger=MagicMock(),
        session=buy_session,
        trade_log_path=tmp_path / "buy.jsonl",
        discord_webhook_url="",
    )
    assert buy_posts == []
    assert any(r.status == "blocked" and "UNKNOWN" in r.detail for r in buy)

    sell_session, sell_posts = _session(sell=True)
    sell = execute_paper_orders(
        _alert(alert_type="SELL", symbol="TQQQ"),
        PositionState(active_symbol="TQQQ"),
        paper_trading=True,
        dry_run=False,
        api_key="k",
        api_secret="s",
        trading_base_url=PAPER_TRADING_BASE_URL,
        data_base_url="https://data.alpaca.markets",
        feed="iex",
        fixed_notional=500,
        equity_pct=0,
        limit_offset_bps=10,
        max_daily_loss_usd=25,
        logger=MagicMock(),
        session=sell_session,
        trade_log_path=tmp_path / "sell.jsonl",
        discord_webhook_url="",
    )
    assert sell_posts == ["sell"]
    assert sell[0].status == "accepted"
    assert sell[0].intent.purpose == "exit"
