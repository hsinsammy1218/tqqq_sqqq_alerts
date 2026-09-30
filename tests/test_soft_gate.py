"""Unit coverage for soft confidence / score-margin entry gates."""

from datetime import datetime, timezone
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from indicators import IndicatorSnapshot
from strategy import PositionState, decide
from strategy_params import StrategyParams
from strategy_scoring import (
    checklist_components_disagree,
    effective_min_confidence_to_trade,
    soft_gate_range_margin_blocks,
)


def _params(**overrides: object) -> StrategyParams:
    base = dict(
        bull_entry_threshold=5,
        bear_entry_threshold=5,
        weak_threshold=3,
        stop_loss_pct=0.08,
        take_profit_pct=0.12,
        stretch_take_profit_pct=0.2,
        max_hold_days=5,
        entry_atr_multiplier=0.5,
        score_weights=(1.0,) * 8,
        regime_sep_atr_mult=0.4,
        regime_slope_atr_mult=0.05,
        regime_ranging_threshold_weight_add=0.0,
        regime_trend_favorable_delta=0.0,
        flip_min_hold_trading_days=0,
        flip_margin_weight=0.0,
        entry_dominance_gap_weight=0.0,
        flip_in_range_regime=True,
        min_confidence_to_trade=50,
    )
    base.update(overrides)
    return StrategyParams(**base)  # type: ignore[arg-type]


def _snapshot_range_bullish() -> IndicatorSnapshot:
    # Flat EMAs → range; bull checklist mostly on.
    return IndicatorSnapshot(
        daily_close=100.0,
        daily_ema20=100.0,
        daily_ema50=100.05,
        daily_ema20_prev=100.0,
        daily_ema50_prev=100.05,
        daily_rsi14=55.0,
        daily_macd=1.0,
        daily_macd_signal=0.5,
        daily_atr14=2.0,
        daily_weekly_vwap=99.0,
        daily_anchored_vwap=99.0,
        daily_volume=200.0,
        daily_vol_sma20=100.0,
        h4_close=101.0,
        h4_ema20=100.5,
        h4_ema50=100.0,
    )


def _snapshot_trend_bull() -> IndicatorSnapshot:
    return IndicatorSnapshot(
        daily_close=110.0,
        daily_ema20=105.0,
        daily_ema50=100.0,
        daily_ema20_prev=104.0,
        daily_ema50_prev=99.5,
        daily_rsi14=60.0,
        daily_macd=2.0,
        daily_macd_signal=1.0,
        daily_atr14=2.0,
        daily_weekly_vwap=103.0,
        daily_anchored_vwap=102.0,
        daily_volume=200.0,
        daily_vol_sma20=100.0,
        h4_close=111.0,
        h4_ema20=106.0,
        h4_ema50=101.0,
    )


def test_defaults_leave_soft_gate_off() -> None:
    p = _params()
    assert p.soft_gate_range_confidence_add == 0
    assert p.soft_gate_disagree_min_side == 0.0
    assert p.soft_gate_disagree_confidence_add == 0
    assert p.soft_gate_range_score_margin == 0.0
    assert effective_min_confidence_to_trade("range", 6.0, 2.0, p) == 50
    assert not soft_gate_range_margin_blocks("range", 6.0, 2.0, p)


def test_checklist_components_disagree() -> None:
    assert not checklist_components_disagree(5.0, 1.0, disagree_min_side=2.0)
    assert checklist_components_disagree(5.0, 2.0, disagree_min_side=2.0)
    assert not checklist_components_disagree(5.0, 2.0, disagree_min_side=0.0)


def test_effective_min_confidence_range_and_disagree_add() -> None:
    p = _params(
        soft_gate_range_confidence_add=8,
        soft_gate_disagree_min_side=2.0,
        soft_gate_disagree_confidence_add=8,
    )
    assert effective_min_confidence_to_trade("trend_up", 7.0, 0.0, p) == 50
    assert effective_min_confidence_to_trade("range", 7.0, 0.0, p) == 58
    assert effective_min_confidence_to_trade("trend_up", 5.0, 2.5, p) == 58
    assert effective_min_confidence_to_trade("range", 5.0, 2.5, p) == 66


def test_soft_gate_range_margin_blocks_helper() -> None:
    p = _params(soft_gate_range_score_margin=6.5)
    assert soft_gate_range_margin_blocks("range", 6.0, 0.5, p)
    assert not soft_gate_range_margin_blocks("range", 7.0, 0.0, p)
    assert not soft_gate_range_margin_blocks("trend_up", 6.0, 0.5, p)


def test_decide_soft_conf_range_skips_marginal_range_entry() -> None:
    now = datetime(2026, 5, 6, 14, 30, tzinfo=timezone.utc)
    # Without soft gate, range bullish snapshot should BUY under low thresholds.
    decision_base, _, dbg = decide(
        _snapshot_range_bullish(),
        PositionState(),
        set(),
        now,
        _params(min_confidence_to_trade=0, bull_entry_threshold=4, bear_entry_threshold=5),
    )
    assert dbg.regime == "range"
    assert decision_base.alert_type == "BUY"

    decision_soft, _, _ = decide(
        _snapshot_range_bullish(),
        PositionState(),
        set(),
        now,
        _params(
            min_confidence_to_trade=0,
            bull_entry_threshold=4,
            bear_entry_threshold=5,
            soft_gate_range_confidence_add=80,
        ),
    )
    assert decision_soft.alert_type == "CASH"
    assert decision_soft.notes_kind == "entry_skipped_confidence"
    assert "soft gate" in decision_soft.notes


def test_decide_soft_margin_range_skips() -> None:
    now = datetime(2026, 5, 6, 14, 30, tzinfo=timezone.utc)
    decision, _, dbg = decide(
        _snapshot_range_bullish(),
        PositionState(),
        set(),
        now,
        _params(
            min_confidence_to_trade=0,
            bull_entry_threshold=4,
            bear_entry_threshold=5,
            soft_gate_range_score_margin=20.0,
        ),
    )
    assert dbg.regime == "range"
    assert decision.alert_type == "CASH"
    assert decision.notes_kind == "entry_skipped_soft_gate"


def test_decide_soft_gate_does_not_block_strong_trend() -> None:
    now = datetime(2026, 5, 6, 14, 30, tzinfo=timezone.utc)
    decision, _, dbg = decide(
        _snapshot_trend_bull(),
        PositionState(),
        set(),
        now,
        _params(
            min_confidence_to_trade=50,
            soft_gate_range_confidence_add=13,
            soft_gate_disagree_min_side=2.0,
            soft_gate_disagree_confidence_add=13,
            soft_gate_range_score_margin=7.5,
        ),
    )
    assert dbg.regime == "trend_up"
    assert decision.alert_type == "BUY"
