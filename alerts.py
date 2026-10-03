from __future__ import annotations

import json
import re
from typing import Any, Sequence

import requests

from indicators import IndicatorSnapshot
from strategy import AlertDecision, PositionState, RunTechnicalMeta, hold_exit_summary_line, score_signals
from strategy_types import HIGH_SIGNAL_QUALITY_THRESHOLD

# Embed accent colors (readable on mobile; avoid purple glow).
_COLOR_BUY = 0x22A06B
_COLOR_SELL = 0xC9372C
_COLOR_FLIP = 0xD97706
_COLOR_HOLD = 0x2F6FED
_COLOR_CASH = 0x6B7280
_COLOR_SKIP = 0xB45309
_COLOR_BLOCKED = 0x9CA3AF

# Internal decision/symbol id stays "CASH"; this is display-only for Discord/console.
_NO_POSITION_LABEL = "No position"
