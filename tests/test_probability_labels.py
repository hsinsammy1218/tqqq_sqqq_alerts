"""Unit tests for Phase 1 probability label construction (synthetic trades)."""

from __future__ import annotations

import pytest

from backtest import BacktestTradeRow
from indicators import IndicatorSnapshot
from probability_labels import (
    CHECKLIST_NAMES,
    ChecklistHits,
    EntryFeatureVector,
    FEATURE_SCHEMA_VERSION,
    applied_checklist_weights,
    build_entry_feature_vector,
    checklist_hit_bits,
    dominance_bucket,
    label_profitable_round_trip,
    label_row_from_trade,
    label_rows_from_trades,
    summarize_labels,
    write_label_artifacts,
)
from strategy_params import DEFAULT_SCORE_WEIGHTS, StrategyParams
from strategy_types import StrategyDebug


def _params(**overrides) -> StrategyParams:
    base = dict(
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
    base.update(overrides)
    return StrategyParams(**base)


def _snapshot(**overrides) -> IndicatorSnapshot:
    vals = dict(
        daily_close=100.0,
        daily_ema20=98.0,
        daily_ema50=95.0,
        daily_ema20_prev=97.5,
        daily_ema50_prev=94.5,
        daily_rsi14=55.0,
        daily_macd=0.4,
        daily_macd_signal=0.2,
        daily_atr14=2.0,
        daily_weekly_vwap=99.0,
        daily_anchored_vwap=97.0,
        daily_volume=1_000_000.0,
        daily_vol_sma20=800_000.0,
        h4_close=100.5,
        h4_ema20=99.0,
        h4_ema50=97.0,
    )
    vals.update(overrides)
    return IndicatorSnapshot(**vals)


def _debug(**overrides) -> StrategyDebug:
    vals = dict(
        regime="trend_up",
        weighted_bull=7.5,
        weighted_bear=1.0,
        effective_bull_entry=5.0,
        effective_bear_entry=5.0,
        effective_weak=3.0,
        normalized_confidence=72,
        flip_suppressed=False,
    )
    vals.update(overrides)
    return StrategyDebug(**vals)


def _trade(**overrides) -> BacktestTradeRow:
    vals = dict(
        timestamp="2024-06-10T20:00:00Z",
        action="SELL",
        symbol="TQQQ",
        entry_price=50.0,
        exit_price=55.0,
        return_pct=10.0,
        hold_days=5,
        bull_strength=80,
        bear_strength=10,
        confidence=72,
        regime="trend_up",
        reason="test",
        entry_timestamp="2024-06-03T20:00:00Z",
    )
    vals.update(overrides)
    return BacktestTradeRow(**vals)


def test_label_profitable_round_trip_strict_positive():
    assert label_profitable_round_trip(0.01) is True
    assert label_profitable_round_trip(0.0) is False
    assert label_profitable_round_trip(-0.01) is False


def test_checklist_hit_bits_bullish_snapshot():
    hits = checklist_hit_bits(_snapshot())
    assert hits.bull == (1, 1, 1, 1, 1, 1, 1, 1)
    assert hits.bear == (0, 0, 0, 0, 0, 0, 0, 0)
    assert len(CHECKLIST_NAMES) == 8


def test_checklist_hit_bits_mixed():
    snap = _snapshot(
        daily_close=90.0,
        daily_ema20=95.0,
        daily_ema50=100.0,
        daily_rsi14=40.0,
        daily_macd=-0.2,
        daily_macd_signal=0.1,
        daily_weekly_vwap=92.0,
        daily_volume=500_000.0,
        daily_vol_sma20=800_000.0,
        h4_close=89.0,
        h4_ema20=91.0,
        h4_ema50=93.0,
    )
    hits = checklist_hit_bits(snap)
    assert hits.bull == (0, 0, 0, 0, 0, 0, 0, 0)
    assert hits.bear == (1, 1, 1, 1, 1, 1, 1, 1)


def test_applied_checklist_weights_match_score_weights():
    hits = ChecklistHits(bull=(1, 0, 1, 0, 1, 0, 1, 0), bear=(0, 1, 0, 1, 0, 1, 0, 1))
    bull_w, bear_w = applied_checklist_weights(hits, DEFAULT_SCORE_WEIGHTS)
    assert bull_w == (1.5, 0.0, 0.5, 0.0, 1.5, 0.0, 1.0, 0.0)
    assert bear_w == (0.0, 1.5, 0.0, 1.0, 0.0, 1.5, 0.0, 0.5)


def test_build_entry_feature_vector_uses_debug_dominance():
    feats = build_entry_feature_vector(
        _snapshot(),
        params=_params(),
        entry_timestamp="2024-06-03T20:00:00Z",
        symbol="TQQQ",
        dbg=_debug(normalized_confidence=68, regime="trend_up"),
    )
    assert feats.schema_version == FEATURE_SCHEMA_VERSION
    assert feats.dominance == 68
    assert feats.regime_trend_up == 1
    assert feats.regime_range == 0
    assert feats.symbol == "TQQQ"
    assert sum(feats.bull_hits) == 8


def test_label_row_from_trade_joins_and_labels():
    feats = build_entry_feature_vector(
        _snapshot(),
        params=_params(),
        entry_timestamp="2024-06-03T20:00:00Z",
        symbol="TQQQ",
        dbg=_debug(),
    )
    win = label_row_from_trade(_trade(return_pct=3.5), feats, cost_bps=10.0)
    assert win.label_profitable is True
    assert win.cost_bps == 10.0
    assert win.features.entry_timestamp == "2024-06-03T20:00:00Z"

    loss = label_row_from_trade(_trade(return_pct=-1.2, exit_price=49.0), feats, cost_bps=10.0)
    assert loss.label_profitable is False


def test_label_row_rejects_timestamp_mismatch():
    feats = build_entry_feature_vector(
        _snapshot(),
        params=_params(),
        entry_timestamp="2024-01-01T00:00:00Z",
        symbol="TQQQ",
        dbg=_debug(),
    )
    with pytest.raises(ValueError, match="entry_timestamp mismatch"):
        label_row_from_trade(_trade(), feats)


def test_label_rows_from_trades_requires_features():
    feats = build_entry_feature_vector(
        _snapshot(),
        params=_params(),
        entry_timestamp="2024-06-03T20:00:00Z",
        symbol="TQQQ",
        dbg=_debug(),
    )
    rows = label_rows_from_trades(
        [_trade()],
        {"2024-06-03T20:00:00Z": feats},
        cost_bps=10.0,
    )
    assert len(rows) == 1
    assert rows[0].label_profitable is True

    with pytest.raises(KeyError, match="missing entry features"):
        label_rows_from_trades([_trade(entry_timestamp="missing")], {})


def test_dominance_bucket_and_summary():
    assert dominance_bucket(40) == "lt_50"
    assert dominance_bucket(62) == "62_74"
    assert dominance_bucket(80) == "75_plus"

    feats_hi = build_entry_feature_vector(
        _snapshot(),
        params=_params(),
        entry_timestamp="2024-06-03T20:00:00Z",
        symbol="TQQQ",
        dbg=_debug(normalized_confidence=80),
    )
    feats_lo = build_entry_feature_vector(
        _snapshot(),
        params=_params(),
        entry_timestamp="2024-06-04T20:00:00Z",
        symbol="SQQQ",
        dbg=_debug(normalized_confidence=55, regime="trend_down"),
    )
    rows = [
        label_row_from_trade(
            _trade(return_pct=2.0, entry_timestamp=feats_hi.entry_timestamp, symbol="TQQQ"),
            feats_hi,
        ),
        label_row_from_trade(
            _trade(
                return_pct=-3.0,
                entry_timestamp=feats_lo.entry_timestamp,
                symbol="SQQQ",
                timestamp="2024-06-11T20:00:00Z",
            ),
            feats_lo,
        ),
    ]
    summary = summarize_labels(rows)
    assert summary["label_count"] == 2
    assert summary["wins"] == 1
    assert summary["win_rate"] == 0.5
    assert summary["by_dominance_bucket"]["75_plus"]["n"] == 1
    assert summary["by_dominance_bucket"]["50_61"]["n"] == 1
    assert summary["by_symbol"]["TQQQ"]["wins"] == 1


def test_write_label_artifacts_csv(tmp_path):
    from etf_backtest import EtfBacktestResult, FillStats, PhaseEvaluation

    feats = build_entry_feature_vector(
        _snapshot(),
        params=_params(),
        entry_timestamp="2024-06-03T20:00:00Z",
        symbol="TQQQ",
        dbg=_debug(),
    )
    row = label_row_from_trade(_trade(), feats, cost_bps=10.0)
    result_bt = EtfBacktestResult(
        bars_tested=10,
        start_utc="2024-01-01T00:00:00Z",
        end_utc="2024-06-30T00:00:00Z",
        cost_bps=10.0,
        closed_trades=1,
        winning_trades=1,
        losing_trades=0,
        win_rate_pct=100.0,
        average_return_per_trade_pct=10.0,
        median_return_per_trade_pct=10.0,
        best_trade_pct=10.0,
        worst_trade_pct=10.0,
        max_drawdown_pct=0.0,
        total_return_pct=10.0,
        mtm_total_return_pct=10.0,
        mtm_max_drawdown_pct=0.0,
        exposure_pct=50.0,
        average_entry_cost_bps=10.0,
        open_symbol=None,
        open_unrealized_pct=None,
        fill_stats=FillStats(),
        trade_rows=( _trade(), ),
    )
    evaluation = PhaseEvaluation(
        role="development",
        result=result_bt,
        metrics={},
        wfe={},
        gate={},
        buy_hold_qqq_pct=1.0,
        buy_hold_tqqq_pct=2.0,
        development_start_utc="2023-09-27T00:00:00Z",
        development_end_utc="2025-09-30T00:00:00Z",
        holdout_start_utc="2025-10-01T00:00:00Z",
        holdout_end_utc="2026-10-01T00:00:00Z",
        dates_dropped=0,
        score_weights=DEFAULT_SCORE_WEIGHTS,
    )
    from probability_labels import LabelBuildResult

    build = LabelBuildResult(rows=[row], evaluation=evaluation, cost_bps=10.0, notes=["test"])
    paths = write_label_artifacts(build, reports_dir=tmp_path)
    assert paths["csv"].exists()
    text = paths["csv"].read_text(encoding="utf-8")
    assert "label_profitable" in text
    assert "bull_hit_daily_close_vs_ema20" in text
    summary = paths["summary_json"].read_text(encoding="utf-8")
    assert '"sealed_peek": false' in summary
    assert "2025-10-01" in summary
