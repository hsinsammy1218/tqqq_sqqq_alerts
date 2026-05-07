"""Strategy facade that re-exports split, testable modules."""

from strategy_decision import decide, hold_exit_summary_line, score_signals
from strategy_formatting import format_technical_breakdown
from strategy_position import load_blocked_dates, load_position, save_position
from strategy_types import (
    HIGH_SIGNAL_QUALITY_THRESHOLD,
    AlertDecision,
    DecideOptions,
    PositionState,
    RunTechnicalMeta,
    StrategyDebug,
)

__all__ = [
    "HIGH_SIGNAL_QUALITY_THRESHOLD",
    "PositionState",
    "AlertDecision",
    "StrategyDebug",
    "DecideOptions",
    "RunTechnicalMeta",
    "load_position",
    "save_position",
    "load_blocked_dates",
    "score_signals",
    "hold_exit_summary_line",
    "decide",
    "format_technical_breakdown",
]
