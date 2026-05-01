"""Weighted signal scoring and market regime utilities."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from indicators import IndicatorSnapshot
from strategy_params import StrategyParams

MarketRegime = Literal["trend_up", "trend_down", "range"]


@dataclass(frozen=True)
class WeightedSignalBreakdown:
    weighted_bull: float
    weighted_bear: float
    max_weight_total: float
    bull_reasons: tuple[str, ...]
    bear_reasons: tuple[str, ...]


def weighted_signal_breakdown(snapshot: IndicatorSnapshot, weights: tuple[float, ...]) -> WeightedSignalBreakdown:
    """Eight paired bull/bear checklist items with configurable weights."""
    w = weights
    wb = 0.0
    wbear = 0.0
    bull_reasons: list[str] = []
    bear_reasons: list[str] = []

    def bull_hit(cond: bool, idx: int, label: str) -> None:
        nonlocal wb
        if cond:
            wb += w[idx]
            bull_reasons.append(f"{label} (+{w[idx]:g})")

    def bear_hit(cond: bool, idx: int, label: str) -> None:
        nonlocal wbear
        if cond:
            wbear += w[idx]
            bear_reasons.append(f"{label} (+{w[idx]:g})")

    i = 0
    bull_hit(snapshot.daily_close > snapshot.daily_ema20, i, "daily close > EMA20")
    bear_hit(snapshot.daily_close < snapshot.daily_ema20, i, "daily close < EMA20")
    i += 1
    bull_hit(snapshot.daily_ema20 > snapshot.daily_ema50, i, "daily EMA20 > EMA50")
    bear_hit(snapshot.daily_ema20 < snapshot.daily_ema50, i, "daily EMA20 < EMA50")
    i += 1
    bull_hit(snapshot.daily_rsi14 > 50, i, "daily RSI > 50")
    bear_hit(snapshot.daily_rsi14 < 50, i, "daily RSI < 50")
    i += 1
    bull_hit(snapshot.daily_macd > snapshot.daily_macd_signal, i, "MACD > signal")
    bear_hit(snapshot.daily_macd < snapshot.daily_macd_signal, i, "MACD < signal")
    i += 1
    bull_hit(snapshot.h4_close > snapshot.h4_ema20, i, "4h close > EMA20")
    bear_hit(snapshot.h4_close < snapshot.h4_ema20, i, "4h close < EMA20")
    i += 1
    bull_hit(snapshot.h4_ema20 > snapshot.h4_ema50, i, "4h EMA20 > EMA50")
    bear_hit(snapshot.h4_ema20 < snapshot.h4_ema50, i, "4h EMA20 < EMA50")
    i += 1
    bull_hit(snapshot.daily_close > snapshot.daily_weekly_vwap, i, "price > weekly VWAP")
    bear_hit(snapshot.daily_close < snapshot.daily_weekly_vwap, i, "price < weekly VWAP")
    i += 1
    bull_hit(snapshot.daily_volume > snapshot.daily_vol_sma20, i, "volume > 20 avg")
    bear_hit(snapshot.daily_volume > snapshot.daily_vol_sma20, i, "volume > 20 avg")

    max_total = float(sum(weights))
    return WeightedSignalBreakdown(
        weighted_bull=wb,
        weighted_bear=wbear,
        max_weight_total=max_total,
        bull_reasons=tuple(bull_reasons),
        bear_reasons=tuple(bear_reasons),
    )


def detect_market_regime(snapshot: IndicatorSnapshot, params: StrategyParams) -> MarketRegime:
    """Classify trending vs range using EMA separation/slope vs ATR (prior-bar slope)."""
    atr = max(snapshot.daily_atr14, 1e-9)
    sep = (snapshot.daily_ema20 - snapshot.daily_ema50) / atr
    slope = (snapshot.daily_ema20 - snapshot.daily_ema20_prev) / atr
    sep_lim = params.regime_sep_atr_mult
    slope_lim = params.regime_slope_atr_mult

    trending_up = sep >= sep_lim and slope >= slope_lim
    trending_down = sep <= -sep_lim and slope <= -slope_lim

    if trending_up and not trending_down:
        return "trend_up"
    if trending_down and not trending_up:
        return "trend_down"
    if trending_up and trending_down:
        return "range"
    chop_sep = sep_lim * 0.65
    chop_slope = slope_lim * 0.65
    if abs(sep) < chop_sep and abs(slope) < chop_slope:
        return "range"
    if sep > 0 and slope >= 0:
        return "trend_up"
    if sep < 0 and slope <= 0:
        return "trend_down"
    return "range"


def threshold_weight_targets(params: StrategyParams) -> tuple[float, float, float]:
    """Convert legacy 0–8 style thresholds into weighted-score targets."""
    scale = max(params.weight_scale, 1e-9)
    bull_tgt = (params.bull_entry_threshold / 8.0) * scale
    bear_tgt = (params.bear_entry_threshold / 8.0) * scale
    weak_tgt = (params.weak_threshold / 8.0) * scale
    return bull_tgt, bear_tgt, weak_tgt


def effective_thresholds(
    regime: MarketRegime,
    params: StrategyParams,
) -> tuple[float, float, float]:
    """Apply regime-aware adjustments to entry and weak weighted thresholds."""
    bull_tgt, bear_tgt, weak_tgt = threshold_weight_targets(params)
    add = params.regime_ranging_threshold_weight_add
    fav = params.regime_trend_favorable_delta

    if regime == "range":
        return bull_tgt + add, bear_tgt + add, weak_tgt

    if regime == "trend_up":
        return max(0.0, bull_tgt - fav), bear_tgt + fav, weak_tgt

    if regime == "trend_down":
        return bull_tgt + fav, max(0.0, bear_tgt - fav), weak_tgt

    return bull_tgt, bear_tgt, weak_tgt


def normalized_confidence_score(wb: float, wbear: float, max_scale: float) -> int:
    """0–100 dominance score from opposing weighted stacks."""
    scale = max(max_scale, 1e-9)
    norm_bull = wb / scale
    norm_bear = wbear / scale
    raw = abs(norm_bull - norm_bear)
    return int(round(min(100.0, max(0.0, raw * 100.0))))


def trading_days_between_inclusive(start_day: object, end_day: object) -> int:
    """Count weekdays strictly after start_day through end_day."""
    from datetime import timedelta

    if start_day >= end_day:
        return 0
    count = 0
    d = start_day + timedelta(days=1)
    while d <= end_day:
        if d.weekday() < 5:
            count += 1
        d += timedelta(days=1)
    return count
