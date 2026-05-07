from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from indicators import IndicatorSnapshot
from strategy_scoring import weighted_signal_breakdown


def _snapshot_with_volume(volume: float, vol_sma20: float) -> IndicatorSnapshot:
    return IndicatorSnapshot(
        daily_close=100.0,
        daily_ema20=95.0,
        daily_ema50=90.0,
        daily_ema20_prev=94.0,
        daily_ema50_prev=89.0,
        daily_rsi14=55.0,
        daily_macd=1.0,
        daily_macd_signal=0.5,
        daily_atr14=2.0,
        daily_weekly_vwap=99.0,
        daily_anchored_vwap=98.0,
        daily_volume=volume,
        daily_vol_sma20=vol_sma20,
        h4_close=100.0,
        h4_ema20=99.0,
        h4_ema50=98.0,
    )


def test_volume_above_average_counts_as_bull_only() -> None:
    snapshot = _snapshot_with_volume(volume=200.0, vol_sma20=100.0)
    result = weighted_signal_breakdown(snapshot, (1.0,) * 8)

    assert "volume > 20 avg (+1)" in result.bull_reasons
    assert "volume < 20 avg (+1)" not in result.bear_reasons


def test_volume_below_average_counts_as_bear_only() -> None:
    snapshot = _snapshot_with_volume(volume=80.0, vol_sma20=100.0)
    result = weighted_signal_breakdown(snapshot, (1.0,) * 8)

    assert "volume < 20 avg (+1)" in result.bear_reasons
    assert "volume > 20 avg (+1)" not in result.bull_reasons
