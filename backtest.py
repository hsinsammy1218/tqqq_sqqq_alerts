from __future__ import annotations

import csv
import math
import statistics
from collections import Counter
from dataclasses import dataclass, field, fields
from datetime import datetime
from pathlib import Path

import pandas as pd

from data import CandleData
from indicators import IndicatorSnapshot, build_snapshot
from strategy_scoring import trading_days_between_inclusive
from strategy_params import StrategyParams
from strategy import DecideOptions, PositionState, decide


@dataclass(frozen=True)
class BacktestResult:
    bars_tested: int
    start_utc: str
    end_utc: str
    buys: int
    sells: int
    flips: int
    cash_no_trade_periods: int
    total_trades: int
    closed_trades: int
    winning_trades: int
    losing_trades: int
    win_rate_pct: float
    average_return_per_trade_pct: float
    median_return_per_trade_pct: float | None
    best_trade_pct: float | None
    worst_trade_pct: float | None
    max_drawdown_pct: float
    average_hold_days: float
    equity_start: float
    equity_end: float
    total_return_pct: float
    open_symbol: str | None
    open_entry_price: float | None
    open_unrealized_pct: float | None
    trade_rows: tuple["BacktestTradeRow", ...]


@dataclass
class _OpenTrade:
    symbol: str
    entry_price: float
    entry_timestamp: str


@dataclass(frozen=True)
class BacktestTradeRow:
    timestamp: str
    action: str
    symbol: str
    entry_price: float
    exit_price: float
    return_pct: float
    hold_days: int
    bull_strength: int
    bear_strength: int
    confidence: int
    regime: str
    reason: str
    # Flat-BUY decision time for the closed lot (ETF path sets this; QQQ path may leave blank).
    entry_timestamp: str = ""
