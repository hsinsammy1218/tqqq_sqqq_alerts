from datetime import datetime, timezone
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from indicators import IndicatorSnapshot
from strategy import PositionState, decide
from strategy_params import StrategyParams


def _params() -> StrategyParams:
    return StrategyParams(
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


def _snapshot_neutral() -> IndicatorSnapshot:
    return IndicatorSnapshot(
        daily_close=100.0,
        daily_ema20=100.0,
        daily_ema50=100.0,
        daily_ema20_prev=100.0,
        daily_ema50_prev=100.0,
        daily_rsi14=50.0,
        daily_macd=0.0,
        daily_macd_signal=0.0,
        daily_atr14=2.0,
        daily_weekly_vwap=100.0,
        daily_anchored_vwap=100.0,
        daily_volume=100.0,
        daily_vol_sma20=100.0,
        h4_close=100.0,
        h4_ema20=100.0,
        h4_ema50=100.0,
    )


def test_transition_flat_to_buy_tqqq() -> None:
    now = datetime(2026, 5, 6, 14, 30, tzinfo=timezone.utc)
    decision, new_position, _ = decide(_snapshot_bull(), PositionState(), set(), now, _params())
    assert decision.alert_type == "BUY"
    assert decision.symbol == "TQQQ"
    assert new_position.active_symbol == "TQQQ"


def test_transition_hold_tqqq_to_sell_on_weakening() -> None:
    now = datetime(2026, 5, 6, 14, 30, tzinfo=timezone.utc)
    position = PositionState(active_symbol="TQQQ", entry_price=110.0, entry_timestamp="2026-05-05T14:30:00Z")
    decision, new_position, _ = decide(_snapshot_neutral(), position, set(), now, _params())
    assert decision.alert_type == "SELL"
    assert new_position.active_symbol is None


def test_transition_hold_tqqq_to_flip_on_bear_reversal() -> None:
    now = datetime(2026, 5, 6, 14, 30, tzinfo=timezone.utc)
    position = PositionState(active_symbol="TQQQ", entry_price=110.0, entry_timestamp="2026-05-05T14:30:00Z")
    decision, new_position, _ = decide(_snapshot_bear(), position, set(), now, _params())
    assert decision.alert_type == "FLIP"
    assert decision.symbol == "SQQQ"
    assert new_position.active_symbol == "SQQQ"


def test_transition_flat_stays_cash_when_blocked() -> None:
    now = datetime(2026, 5, 6, 14, 30, tzinfo=timezone.utc)
    blocked = {now.date().isoformat()}
    decision, new_position, _ = decide(_snapshot_bull(), PositionState(), blocked, now, _params())
    assert decision.alert_type == "CASH"
    assert "blocked" in decision.notes.lower()
    assert new_position.active_symbol is None
