"""Configurable parameters for weighted, regime-aware strategy logic."""

from __future__ import annotations

from dataclasses import dataclass

# Identity of the decision rules shipped with this tree. Bump only when rules
# change on purpose. Nothing in the runtime writes new weights from this value.
STRATEGY_VERSION = "1.0.0"

DEFAULT_SCORE_WEIGHTS: tuple[float, ...] = (1.5, 1.5, 0.5, 1.0, 1.5, 1.5, 1.0, 0.5)
"""Trend-tilted checklist: boost daily/4h EMA stack; cut RSI and volume noise.

Measured via ``--checklist-compare`` on 756 sessions (2023-09 → 2026-09):
net +4.4% vs equal-weight baseline −9.0%. Still fails the paper gate (WFE < 0).
"""


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
    # When False (default), never FLIP on reversal while regime is range/chop (exit via weaken/SL/TP/max hold).
    flip_in_range_regime: bool
    # Minimum normalized confidence (0-100 stack dominance) required for a flat BUY; 0 disables.
    # Default in load_settings matches walk-forward best row (see config.load_settings).
    min_confidence_to_trade: int
    # fixed: stop % + take-profit % exits. atr_trail: ATR trailing stop replaces the fixed TP exit.
    # Stretch take-profit is never an automatic exit in either mode.
    exit_mode: str = "fixed"
    atr_trail_mult: float = 2.0
    # Checklist redesign knobs (defaults preserve legacy decide() behavior).
    # Flat BUY only when symbol matches regime (TQQQ↔trend_up, SQQQ↔trend_down).
    entry_require_regime_align: bool = False
    # Flat BUY skipped entirely while regime is range/chop.
    block_range_entries: bool = False
    # When False, never FLIP; reverse signal is ignored (exit via weaken/SL/TP/max hold).
    allow_flips: bool = True
    # When reverse would FLIP into the adverse regime, SELL to flat instead of flipping.
    flip_adverse_becomes_exit: bool = False
    # Force SELL when hold is not regime-aligned (prior blunt filter; research only).
    exit_on_adverse_regime: bool = False
    # Extra weak-threshold weight while holding against the regime (exit sooner without blunt force).
    adverse_hold_weak_add: float = 0.0
    # Soft confidence / score-margin gate (research; all defaults keep live decide() unchanged).
    # Extra min_confidence_to_trade while regime is range/chop (0 disables).
    soft_gate_range_confidence_add: int = 0
    # When both bull and bear stacks are at least this weight, treat as component disagreement (0 disables).
    soft_gate_disagree_min_side: float = 0.0
    # Extra min_confidence_to_trade when components disagree (0 disables).
    soft_gate_disagree_confidence_add: int = 0
    # Minimum |bull − bear| weighted margin required for a flat BUY in range (0 disables).
    soft_gate_range_score_margin: float = 0.0

    def __post_init__(self) -> None:
        if len(self.score_weights) != 8:
            raise ValueError("score_weights must contain exactly 8 values.")
        if any(w < 0 for w in self.score_weights):
            raise ValueError("score_weights must be non-negative.")
        if self.entry_dominance_gap_weight < 0:
            raise ValueError("entry_dominance_gap_weight must be non-negative.")
        if not 0 <= self.min_confidence_to_trade <= 100:
            raise ValueError("min_confidence_to_trade must be between 0 and 100 inclusive.")
        if self.exit_mode not in {"fixed", "atr_trail"}:
            raise ValueError("exit_mode must be 'fixed' or 'atr_trail'.")
        if self.atr_trail_mult <= 0:
            raise ValueError("atr_trail_mult must be positive.")
        if self.adverse_hold_weak_add < 0:
            raise ValueError("adverse_hold_weak_add must be non-negative.")
        if self.soft_gate_range_confidence_add < 0:
            raise ValueError("soft_gate_range_confidence_add must be non-negative.")
        if self.soft_gate_disagree_min_side < 0:
            raise ValueError("soft_gate_disagree_min_side must be non-negative.")
        if self.soft_gate_disagree_confidence_add < 0:
            raise ValueError("soft_gate_disagree_confidence_add must be non-negative.")
        if self.soft_gate_range_score_margin < 0:
            raise ValueError("soft_gate_range_score_margin must be non-negative.")

    @property
    def weight_scale(self) -> float:
        return float(sum(self.score_weights))
