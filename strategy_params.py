"""Configurable parameters for weighted, regime-aware strategy logic."""

from __future__ import annotations

from dataclasses import dataclass


DEFAULT_SCORE_WEIGHTS: tuple[float, ...] = (1.0,) * 8


@dataclass(frozen=True)
class StrategyParams:
    """Immutable inputs shared by live runs and backtests."""

    bull_entry_threshold: int
    bear_entry_threshold: int
    weak_threshold: int
    stop_loss_pct: float
    take_profit_pct: float
    stretch_take_profit_pct: float
    max_hold_days: int
    entry_atr_multiplier: float
    score_weights: tuple[float, ...]
    regime_sep_atr_mult: float
    regime_slope_atr_mult: float
    regime_ranging_threshold_weight_add: float
    regime_trend_favorable_delta: float
    flip_min_hold_trading_days: int
    flip_margin_weight: float
    # Weighted-units gap between bull stack and bear stack required for a dominance fallback
    # entry when strict threshold gates fail. Set to 0 to disable (legacy behavior).
    entry_dominance_gap_weight: float

    def __post_init__(self) -> None:
        if len(self.score_weights) != 8:
            raise ValueError("score_weights must contain exactly 8 values.")
        if any(w < 0 for w in self.score_weights):
            raise ValueError("score_weights must be non-negative.")
        if self.entry_dominance_gap_weight < 0:
            raise ValueError("entry_dominance_gap_weight must be non-negative.")

    @property
    def weight_scale(self) -> float:
        return float(sum(self.score_weights))
