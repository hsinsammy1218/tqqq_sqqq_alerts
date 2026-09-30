"""Unit coverage for checklist redesign knobs (defaults preserve legacy behavior)."""

from datetime import datetime, timezone
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from indicators import IndicatorSnapshot
from strategy import PositionState, decide
from strategy_decision import hold_is_regime_aligned, regime_aligned_symbol
from strategy_params import StrategyParams


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
        min_confidence_to_trade=0,
    )
    base.update(overrides)
    return StrategyParams(**base)  # type: ignore[arg-type]


def _snapshot_bull() -> IndicatorSnapshot:
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


def _snapshot_bear() -> IndicatorSnapshot:
    return IndicatorSnapshot(
        daily_close=90.0,
        daily_ema20=95.0,
        daily_ema50=100.0,
        daily_ema20_prev=96.0,
        daily_ema50_prev=100.5,
        daily_rsi14=40.0,
        daily_macd=-2.0,
        daily_macd_signal=-1.0,
        daily_atr14=2.0,
        daily_weekly_vwap=97.0,
        daily_anchored_vwap=98.0,
        daily_volume=80.0,
        daily_vol_sma20=100.0,
        h4_close=89.0,
        h4_ema20=94.0,
        h4_ema50=99.0,
    )


def _snapshot_range() -> IndicatorSnapshot:
    # sep and slope near zero → range under sep_lim=0.4 / slope_lim=0.05
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


def test_regime_aligned_helpers() -> None:
    assert regime_aligned_symbol("trend_up") == "TQQQ"
    assert regime_aligned_symbol("trend_down") == "SQQQ"
    assert regime_aligned_symbol("range") is None
    assert hold_is_regime_aligned("TQQQ", "trend_up")
    assert not hold_is_regime_aligned("SQQQ", "trend_up")


def test_aligned_entry_blocks_bull_in_range() -> None:
    now = datetime(2026, 5, 6, 14, 30, tzinfo=timezone.utc)
    decision, _, dbg = decide(
        _snapshot_range(),
        PositionState(),
        set(),
        now,
        _params(entry_require_regime_align=True),
    )
    assert dbg.regime == "range"
    assert decision.alert_type == "CASH"
    assert decision.notes_kind == "entry_skipped_regime"


def test_block_range_entries() -> None:
    now = datetime(2026, 5, 6, 14, 30, tzinfo=timezone.utc)
    decision, _, dbg = decide(
        _snapshot_range(),
        PositionState(),
        set(),
        now,
        _params(block_range_entries=True),
    )
    assert dbg.regime == "range"
    assert decision.alert_type == "CASH"
    assert decision.notes_kind == "entry_skipped_regime"


def test_no_flip_holds_instead_of_reversing() -> None:
    now = datetime(2026, 5, 6, 14, 30, tzinfo=timezone.utc)
    position = PositionState(
        active_symbol="TQQQ",
        entry_price=110.0,
        entry_timestamp="2026-05-05T14:30:00Z",
    )
    decision, new_position, _ = decide(
        _snapshot_bear(),
        position,
        set(),
        now,
        _params(allow_flips=False),
    )
    assert decision.alert_type != "FLIP"
    # Bear stack is strong so weaken may still sell; either hold or sell is fine — just no flip.
    assert new_position.active_symbol in {None, "TQQQ"}


def test_flip_adverse_becomes_exit_flattens_in_range() -> None:
    now = datetime(2026, 5, 6, 14, 30, tzinfo=timezone.utc)
    position = PositionState(
        active_symbol="TQQQ",
        entry_price=110.0,
        entry_timestamp="2026-05-05T14:30:00Z",
    )
    # Flat EMAs → range, but bear checklist strong enough to reverse.
    snap = IndicatorSnapshot(
        daily_close=90.0,
        daily_ema20=100.0,
        daily_ema50=100.1,
        daily_ema20_prev=100.0,
        daily_ema50_prev=100.1,
        daily_rsi14=35.0,
        daily_macd=-2.0,
        daily_macd_signal=-1.0,
        daily_atr14=2.0,
        daily_weekly_vwap=97.0,
        daily_anchored_vwap=98.0,
        daily_volume=80.0,
        daily_vol_sma20=100.0,
        h4_close=89.0,
        h4_ema20=94.0,
        h4_ema50=99.0,
    )
    decision, new_position, dbg = decide(
        snap,
        position,
        set(),
        now,
        _params(flip_adverse_becomes_exit=True, flip_in_range_regime=True),
    )
    assert dbg.regime == "range"
    assert decision.alert_type == "SELL"
    assert new_position.active_symbol is None
    assert "adverse regime" in decision.notes


def test_exit_on_adverse_regime() -> None:
    now = datetime(2026, 5, 6, 14, 30, tzinfo=timezone.utc)
    position = PositionState(
        active_symbol="TQQQ",
        entry_price=100.0,
        entry_timestamp="2026-05-05T14:30:00Z",
    )
    decision, new_position, dbg = decide(
        _snapshot_bear(),
        position,
        set(),
        now,
        _params(exit_on_adverse_regime=True, allow_flips=False, weak_threshold=0),
    )
    assert dbg.regime == "trend_down"
    assert decision.alert_type == "SELL"
    assert new_position.active_symbol is None
    assert "adverse regime" in decision.notes
