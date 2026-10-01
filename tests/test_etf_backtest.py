from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd
import pytest

from data import CandleData
from etf_backtest import (
    align_etf_data,
    compute_research_split,
    day_limit_fill,
    evaluate_development,
    evaluate_sealed,
    format_phase_report,
    limit_from_reference,
    phase_d_allowed,
    run_etf_backtest,
)
from strategy import DecideOptions, PositionState, StrategyDebug, decide
from strategy_params import DEFAULT_SCORE_WEIGHTS, StrategyParams


def _params() -> StrategyParams:
    return StrategyParams(
        bull_entry_threshold=5,
        bear_entry_threshold=5,
        weak_threshold=3,
        stop_loss_pct=0.08,
        take_profit_pct=0.15,
        stretch_take_profit_pct=0.25,
        max_hold_days=10,
        entry_atr_multiplier=0.5,
        score_weights=DEFAULT_SCORE_WEIGHTS,
        regime_sep_atr_mult=0.12,
        regime_slope_atr_mult=0.03,
        regime_ranging_threshold_weight_add=0.0,
        regime_trend_favorable_delta=0.0,
        flip_min_hold_trading_days=3,
        flip_margin_weight=1.25,
        entry_dominance_gap_weight=1.25,
        flip_in_range_regime=False,
        min_confidence_to_trade=62,
    )


def _debug() -> StrategyDebug:
    return StrategyDebug(
        regime="trend_up",
        weighted_bull=8.0,
        weighted_bear=1.0,
        effective_bull_entry=5.0,
        effective_bear_entry=5.0,
        effective_weak=3.0,
        normalized_confidence=80,
        flip_suppressed=False,
    )


def _alert(alert_type: str, symbol: str, now: datetime, *, reason: str = "regime=trend_up"):
    from strategy_types import AlertDecision

    return AlertDecision(
        alert_type=alert_type,
        symbol=symbol,
        qqq_trend_reason=reason,
        bullish_score=80,
        bearish_score=10,
        confidence_score=80,
        entry_zone_low=1.0,
        entry_zone_high=2.0,
        stop_loss=0.9,
        take_profit=1.2,
        stretch_take_profit=1.4,
        max_hold_date="2026-12-31",
        timestamp=now.replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        notes="test",
    )


def _frames(n: int = 180):
    idx = pd.bdate_range("2021-01-04", periods=n, tz="UTC")
    qqq_close = pd.Series(100.0, index=idx)
    tqqq_close = pd.Series(50.0, index=idx)
    sqqq_close = pd.Series(40.0, index=idx)

    def pack(close: pd.Series) -> pd.DataFrame:
        return pd.DataFrame(
            {
                "open": close.to_numpy(),
                "high": (close + 1.0).to_numpy(),
                "low": (close - 1.0).to_numpy(),
                "close": close.to_numpy(),
                "volume": 1_000_000.0,
            },
            index=idx,
        )

    qqq = pack(qqq_close)
    return CandleData(daily=qqq, four_hour=qqq.copy()), pack(tqqq_close), pack(sqqq_close), idx


def _run(candles, tqqq, sqqq, decide_fn, *, lo: int = 60, hi: int = 100):
    data = align_etf_data(candles, tqqq, sqqq)
    return run_etf_backtest(
        data,
        strategy_params=_params(),
        blocked_dates=set(),
        anchor_date=None,
        loop_start_idx=lo,
        loop_end_idx_exclusive=hi,
        cost_bps=10.0,
        decide_fn=decide_fn,
    )


def test_live_score_weights_stay_at_the_adopted_default():
    assert DEFAULT_SCORE_WEIGHTS == (1.5, 1.5, 0.5, 1.0, 1.5, 1.5, 1.0, 0.5)


def test_day_limit_fill_and_miss():
    buy_limit = limit_from_reference(100.0, "buy", 10.0)
    assert buy_limit == pytest.approx(100.1)
    assert day_limit_fill(100.05, 101.0, 99.0, buy_limit, "buy") == pytest.approx(100.05)
    assert day_limit_fill(101.0, 102.0, 100.1, buy_limit, "buy") == pytest.approx(100.1)
    assert day_limit_fill(101.0, 102.0, 100.2, buy_limit, "buy") is None
    sell_limit = limit_from_reference(110.0, "sell", 10.0)
    assert sell_limit == pytest.approx(109.89)
    assert day_limit_fill(110.2, 111.0, 109.0, sell_limit, "sell") == pytest.approx(110.2)
    assert day_limit_fill(109.0, 109.89, 108.0, sell_limit, "sell") == pytest.approx(109.89)
    assert day_limit_fill(109.0, 109.5, 108.0, sell_limit, "sell") is None


def test_next_bar_etf_fill_is_not_the_signal_close():
    candles, tqqq, sqqq, idx = _frames(120)
    buy_i, sell_i = 70, 74
    tqqq.iloc[buy_i, tqqq.columns.get_loc("close")] = 100.0
    tqqq.iloc[buy_i + 1, tqqq.columns.get_loc("open")] = 100.05
    tqqq.iloc[buy_i + 1, tqqq.columns.get_loc("high")] = 102.0
    tqqq.iloc[buy_i + 1, tqqq.columns.get_loc("low")] = 99.0
    tqqq.iloc[sell_i, tqqq.columns.get_loc("close")] = 110.0
    tqqq.iloc[sell_i + 1, tqqq.columns.get_loc("open")] = 110.2
    tqqq.iloc[sell_i + 1, tqqq.columns.get_loc("high")] = 112.0
    tqqq.iloc[sell_i + 1, tqqq.columns.get_loc("low")] = 109.0

    def decider(snapshot, position, blocked, now, params, decide_options=None):
        if position.active_symbol is None and now == idx[buy_i].to_pydatetime():
            alert = _alert("BUY", "TQQQ", now)
            return alert, PositionState(
                active_symbol="TQQQ",
                entry_price=snapshot.daily_close,
                entry_timestamp=alert.timestamp,
            ), _debug()
        if position.active_symbol == "TQQQ" and now == idx[sell_i].to_pydatetime():
            return _alert("SELL", "TQQQ", now), PositionState(), _debug()
        return _alert("CASH", "CASH", now), position, _debug()

    result = _run(candles, tqqq, sqqq, decider, lo=60, hi=90)
    assert result.closed_trades == 1
    trade = result.trade_rows[0]
    assert trade.symbol == "TQQQ"
    assert trade.entry_price == pytest.approx(100.05)
    assert trade.exit_price == pytest.approx(110.2)
    assert trade.entry_price != 100.0
    assert trade.return_pct == pytest.approx((110.2 / 100.05 - 1.0) * 100.0)
    assert result.total_return_pct == pytest.approx(trade.return_pct)
    assert result.fill_stats.buy_fills == 1
    assert result.fill_stats.buy_misses == 0


def test_missed_day_limit_does_not_open_a_position():
    candles, tqqq, sqqq, idx = _frames(120)
    buy_i = 70
    tqqq.iloc[buy_i, tqqq.columns.get_loc("close")] = 100.0
    tqqq.iloc[buy_i + 1, tqqq.columns.get_loc("open")] = 101.0
    tqqq.iloc[buy_i + 1, tqqq.columns.get_loc("low")] = 100.5
    tqqq.iloc[buy_i + 1, tqqq.columns.get_loc("high")] = 102.0

    def decider(snapshot, position, blocked, now, params, decide_options=None):
        if position.active_symbol is None and now == idx[buy_i].to_pydatetime():
            alert = _alert("BUY", "TQQQ", now)
            return alert, PositionState(active_symbol="TQQQ", entry_price=100.0, entry_timestamp=alert.timestamp), _debug()
        return _alert("CASH", "CASH", now), position, _debug()

    result = _run(candles, tqqq, sqqq, decider, lo=60, hi=90)
    assert result.closed_trades == 0
    assert result.total_return_pct == pytest.approx(0.0)
    assert result.open_symbol is None
    assert result.fill_stats.buy_misses == 1
    assert result.fill_stats.buy_fills == 0


def test_sqqq_pnl_is_long_the_etf():
    candles, tqqq, sqqq, idx = _frames(120)
    buy_i, sell_i = 70, 74
    sqqq.iloc[buy_i, sqqq.columns.get_loc("close")] = 20.0
    sqqq.iloc[buy_i + 1, sqqq.columns.get_loc("open")] = 20.0
    sqqq.iloc[buy_i + 1, sqqq.columns.get_loc("low")] = 19.0
    sqqq.iloc[sell_i, sqqq.columns.get_loc("close")] = 22.0
    sqqq.iloc[sell_i + 1, sqqq.columns.get_loc("open")] = 22.0
    sqqq.iloc[sell_i + 1, sqqq.columns.get_loc("high")] = 23.0

    def decider(snapshot, position, blocked, now, params, decide_options=None):
        if position.active_symbol is None and now == idx[buy_i].to_pydatetime():
            alert = _alert("BUY", "SQQQ", now)
            return alert, PositionState(active_symbol="SQQQ", entry_price=100.0, entry_timestamp=alert.timestamp), _debug()
        if position.active_symbol == "SQQQ" and now == idx[sell_i].to_pydatetime():
            return _alert("SELL", "SQQQ", now), PositionState(), _debug()
        return _alert("CASH", "CASH", now), position, _debug()

    result = _run(candles, tqqq, sqqq, decider, lo=60, hi=90)
    assert result.trade_rows[0].return_pct == pytest.approx(10.0)
    assert result.trade_rows[0].return_pct > 0


def test_flip_does_not_buy_the_second_leg_when_the_sell_misses():
    candles, tqqq, sqqq, idx = _frames(120)
    buy_i, flip_i = 70, 74
    tqqq.iloc[buy_i, tqqq.columns.get_loc("close")] = 100.0
    tqqq.iloc[buy_i + 1, tqqq.columns.get_loc("open")] = 100.0
    tqqq.iloc[buy_i + 1, tqqq.columns.get_loc("low")] = 99.0
    tqqq.iloc[flip_i, tqqq.columns.get_loc("close")] = 100.0
    # Sell limit is 99.9. Keep the next bar entirely below it.
    tqqq.iloc[flip_i + 1, tqqq.columns.get_loc("open")] = 99.0
    tqqq.iloc[flip_i + 1, tqqq.columns.get_loc("high")] = 99.4
    tqqq.iloc[flip_i + 1, tqqq.columns.get_loc("low")] = 98.0
    sqqq.iloc[flip_i + 1, sqqq.columns.get_loc("open")] = 10.0
    sqqq.iloc[flip_i + 1, sqqq.columns.get_loc("low")] = 9.0

    def decider(snapshot, position, blocked, now, params, decide_options=None):
        if position.active_symbol is None and now == idx[buy_i].to_pydatetime():
            alert = _alert("BUY", "TQQQ", now)
            return alert, PositionState(active_symbol="TQQQ", entry_price=100.0, entry_timestamp=alert.timestamp), _debug()
        if position.active_symbol == "TQQQ" and now == idx[flip_i].to_pydatetime():
            alert = _alert("FLIP", "SQQQ", now)
            return alert, PositionState(active_symbol="SQQQ", entry_price=100.0, entry_timestamp=alert.timestamp), _debug()
        return _alert("CASH", "CASH", now), position, _debug()

    result = _run(candles, tqqq, sqqq, decider, lo=60, hi=90)
    assert result.fill_stats.flip_second_leg_blocked == 1
    assert result.fill_stats.flip_buy_fills == 0
    assert result.closed_trades == 0
    assert result.open_symbol == "TQQQ"


def test_development_window_does_not_evaluate_the_holdout(monkeypatch):
    candles, tqqq, sqqq, idx = _frames(180)
    aligned = align_etf_data(candles, tqqq, sqqq)
    split = compute_research_split(len(aligned.qqq.daily), 120)
    seen: list[datetime] = []

    def decider(snapshot, position, blocked, now, params, decide_options=None):
        seen.append(now)
        return _alert("CASH", "CASH", now), position, _debug()

    def _sealed_must_not_run(*args, **kwargs):
        raise AssertionError("development evaluation called the sealed window")

    monkeypatch.setattr("etf_backtest.evaluate_sealed", _sealed_must_not_run)
    evaluation = evaluate_development(
        candles,
        tqqq,
        sqqq,
        strategy_params=_params(),
        blocked_dates=set(),
        anchor_date=None,
        requested_bars=180,
        expand_default=False,
        decide_fn=decider,
    )
    holdout_start = idx[split.holdout_lo].to_pydatetime()
    assert seen
    assert all(stamp < holdout_start for stamp in seen)
    assert evaluation.role == "development"
    assert evaluation.result.end_utc == evaluation.development_end_utc
    report = format_phase_report(evaluation)
    assert "not evaluated" in report
    assert "Holdout return" not in report
    assert evaluation.score_weights == DEFAULT_SCORE_WEIGHTS


def test_sealed_runner_stays_inside_the_final_block():
    candles, tqqq, sqqq, idx = _frames(180)
    aligned = align_etf_data(candles, tqqq, sqqq)
    split = compute_research_split(len(aligned.qqq.daily), 120)
    holdout_start = idx[split.holdout_lo].to_pydatetime()

    def decider(snapshot, position, blocked, now, params, decide_options=None):
        if now < holdout_start:
            raise AssertionError(f"sealed runner saw {now}")
        return _alert("CASH", "CASH", now), position, _debug()

    evaluation = evaluate_sealed(
        candles,
        tqqq,
        sqqq,
        strategy_params=_params(),
        blocked_dates=set(),
        anchor_date=None,
        requested_bars=180,
        expand_default=False,
        decide_fn=decider,
    )
    assert evaluation.role == "sealed"
    assert evaluation.result.start_utc == evaluation.holdout_start_utc
    assert evaluation.buy_hold_qqq_pct == pytest.approx(0.0)


def test_phase_d_stops_when_either_window_is_flat_or_unmeasured():
    assert phase_d_allowed(
        development_measured=True,
        development_return_pct=4.0,
        development_trades=10,
        sealed_measured=True,
        sealed_return_pct=1.0,
        sealed_trades=4,
    )
    assert not phase_d_allowed(
        development_measured=True,
        development_return_pct=4.0,
        development_trades=10,
        sealed_measured=True,
        sealed_return_pct=0.0,
        sealed_trades=4,
    )
    assert not phase_d_allowed(
        development_measured=True,
        development_return_pct=-1.0,
        development_trades=10,
        sealed_measured=True,
        sealed_return_pct=3.0,
        sealed_trades=4,
    )
    assert not phase_d_allowed(
        development_measured=False,
        development_return_pct=None,
        development_trades=0,
        sealed_measured=False,
        sealed_return_pct=None,
        sealed_trades=0,
    )


def test_default_decider_is_the_live_decide_function():
    assert decide.__module__ == "strategy_decision"
    options = DecideOptions()
    assert options.high_confidence_only is False
