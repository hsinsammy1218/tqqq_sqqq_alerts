"""ATR trail is opt-in. Stretch take-profit is never an exit."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone

from indicators import IndicatorSnapshot
from strategy_decision import decide
from strategy_params import StrategyParams
from strategy_types import PositionState


def _params(**kwargs: object) -> StrategyParams:
    base = dict(
        bull_entry_threshold=5,
        bear_entry_threshold=5,
        weak_threshold=3,
        stop_loss_pct=0.08,
        take_profit_pct=0.15,
        stretch_take_profit_pct=0.25,
        max_hold_days=10,
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
        exit_mode="fixed",
        atr_trail_mult=2.0,
    )
    base.update(kwargs)
    return StrategyParams(**base)  # type: ignore[arg-type]


def _bull(close: float) -> IndicatorSnapshot:
    return IndicatorSnapshot(
        daily_close=close,
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
        h4_close=close,
        h4_ema20=106.0,
        h4_ema50=101.0,
    )


def _held(extreme: float | None = None) -> PositionState:
    return PositionState(
        active_symbol="TQQQ",
        entry_price=100.0,
        entry_timestamp="2026-05-05T14:30:00Z",
        favorable_extreme=extreme,
    )


def test_fixed_mode_exits_on_take_profit_not_on_stretch_alone():
    now = datetime(2026, 5, 6, 14, 30, tzinfo=timezone.utc)
    decision, _, _ = decide(_bull(120.0), _held(), set(), now, _params())
    assert decision.alert_type == "SELL"
    assert "take profit hit" in decision.notes
    assert "stretch" not in decision.notes.lower()

    # Past the 25% stretch (price +30%) but short of a 50% take-profit: still holding.
    wide = _params(take_profit_pct=0.50, stretch_take_profit_pct=0.25)
    held, _, _ = decide(_bull(130.0), _held(), set(), now, wide)
    assert held.alert_type == "CASH"
    assert "stretch" not in held.notes.lower()
    assert held.take_profit == 150.0
    assert held.stretch_take_profit == 125.0


def test_atr_trail_ignores_fixed_take_profit_until_trail_breaks():
    now = datetime(2026, 5, 6, 14, 30, tzinfo=timezone.utc)
    params = _params(exit_mode="atr_trail", atr_trail_mult=2.0)
    # +20% would hit the 15% target. Trail from entry 100 is 100 - 4 = 96, so price 120 holds.
    decision, position, _ = decide(_bull(120.0), _held(100.0), set(), now, params)
    assert decision.alert_type == "CASH"
    assert "take profit" not in decision.notes.lower()
    assert position.favorable_extreme == 120.0

    # Prior extreme 120, ATR 2, trail 116. Close 115 breaks it.
    stopped, flat, _ = decide(_bull(115.0), _held(120.0), set(), now, params)
    assert stopped.alert_type == "SELL"
    assert "ATR trailing stop hit" in stopped.notes
    assert "take profit" not in stopped.notes.lower()
    assert flat.active_symbol is None


def test_atr_trail_ratchets_without_using_today_as_the_extreme_first():
    now = datetime(2026, 5, 6, 14, 30, tzinfo=timezone.utc)
    params = _params(exit_mode="atr_trail", atr_trail_mult=2.0)
    snap = replace(_bull(120.0), daily_atr14=2.0)
    decision, position, _ = decide(snap, _held(110.0), set(), now, params)
    assert decision.alert_type == "CASH"
    assert position.favorable_extreme == 120.0
