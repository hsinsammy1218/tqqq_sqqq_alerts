from __future__ import annotations

from dataclasses import dataclass

# Flat BUY signal quality: HIGH at or above this normalized confidence; MEDIUM up to it but ≥ MIN_CONFIDENCE_TO_TRADE.
HIGH_SIGNAL_QUALITY_THRESHOLD = 75


@dataclass
class PositionState:
    active_symbol: str | None = None
    entry_price: float | None = None
    entry_timestamp: str | None = None
    last_signal: str | None = None
    updated_at: str | None = None


@dataclass
class AlertDecision:
    alert_type: str
    symbol: str
    qqq_trend_reason: str
    bullish_score: int
    bearish_score: int
    confidence_score: int
    entry_zone_low: float
    entry_zone_high: float
    stop_loss: float
    take_profit: float
    stretch_take_profit: float
    max_hold_date: str
    timestamp: str
    notes: str = ""
    # Presentation category set in decide(); avoids parsing notes in alerts.py.
    notes_kind: str = "other"
    flip_suppressed: bool = False
    # Set only for flat BUY after confidence gates (HIGH ≥75%, MEDIUM otherwise above minimum).
    signal_quality: str | None = None


@dataclass(frozen=True)
class StrategyDebug:
    regime: str
    weighted_bull: float
    weighted_bear: float
    effective_bull_entry: float
    effective_bear_entry: float
    effective_weak: float
    normalized_confidence: int
    flip_suppressed: bool


@dataclass(frozen=True)
class DecideOptions:
    """Optional hooks for research/backtest; live runs pass None (same as defaults)."""

    debug_sanity_dominate: bool = False
    # When True, flat BUY requires normalized confidence ≥ HIGH_SIGNAL_QUALITY_THRESHOLD (75%).
    high_confidence_only: bool = False


@dataclass(frozen=True)
class RunTechnicalMeta:
    """Console-only context for the technical breakdown block."""

    qqq_ticker: str
    run_utc_iso: str
    daily_bar_end: str | None
    h4_bar_end: str | None
    blocked_today: bool
    regime: str
    base_bull_entry_threshold: int
    base_bear_entry_threshold: int
    base_weak_threshold: int
    effective_bull_entry: float
    effective_bear_entry: float
    effective_weak: float
    score_weights: tuple[float, ...]
    weighted_bull: float
    weighted_bear: float
    stop_loss_pct: float
    take_profit_pct: float
    stretch_take_profit_pct: float
    max_hold_days: int
    entry_atr_multiplier: float
    anchor_date_label: str
    flip_in_range_regime: bool
    min_confidence_to_trade: int
    high_confidence_only: bool
