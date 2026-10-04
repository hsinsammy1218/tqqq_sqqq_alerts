#!/usr/bin/env python3
"""Learning-only deeper cuts on full pre-seal ETF history (frozen weights).

Does not retune, does not run sealed OOS, does not change live wiring.

Writes JSON + markdown under ``reports/`` by default. Override with
``LEARNING_DEEPER_CUTS_JSON`` / ``LEARNING_DEEPER_CUTS_MD``.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config import load_settings, strategy_params_from_settings  # noqa: E402
from data import (  # noqa: E402
    DataError,
    MarketDataQuotaError,
    _fetch_bars,
    cli_calls_attempted,
    load_candles,
    load_daily_bars,
    reset_cli_call_count,
)
from etf_backtest import align_etf_data, buy_and_hold_close_to_close, run_etf_backtest  # noqa: E402
from event_calendar import load_merged_blackout_dates  # noqa: E402
from strategy import DecideOptions, PositionState, decide  # noqa: E402
from strategy_params import DEFAULT_SCORE_WEIGHTS  # noqa: E402

SLEEP_BETWEEN_CALLS_S = 0.75
SEALED_START = pd.Timestamp("2025-10-02", tz="UTC")
SEAL_PATH = ROOT / "reports" / "etf_sealed_oos.json"
OUT_JSON = Path(os.getenv("LEARNING_DEEPER_CUTS_JSON", "reports/learning_deeper_cuts.json"))
OUT_DOC = Path(os.getenv("LEARNING_DEEPER_CUTS_MD", "reports/learning_deeper_cuts.md"))

_api = {"calls": 0, "http_429s": 0}


def _paced_fetch(*args, **kwargs):
    time.sleep(SLEEP_BETWEEN_CALLS_S)
    before = cli_calls_attempted()
    try:
        frame = _ORIG_FETCH(*args, **kwargs)
    except MarketDataQuotaError:
        _api["http_429s"] += 1
        time.sleep(8)
        frame = _ORIG_FETCH(*args, **kwargs)
    after = cli_calls_attempted()
    _api["calls"] += max(0, after - before)
    return frame


import data as data_mod  # noqa: E402

_ORIG_FETCH = data_mod._fetch_bars
data_mo