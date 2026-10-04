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
data_mod._fetch_bars = _paced_fetch  # type: ignore[assignment]


def _compound(returns: list[float]) -> float:
    eq = 1.0
    for r in returns:
        eq *= 1.0 + r / 100.0
    return (eq - 1.0) * 100.0


def _bucket_conf(c: int) -> str:
    if c < 62:
        return "<62"
    if c < 70:
        return "62-69"
    if c < 80:
        return "70-79"
    return "80+"


def _hold_bucket(d: int) -> str:
    if d <= 2:
        return "1-2d"
    if d <= 5:
        return "3-5d"
    if d <= 10:
        return "6-10d"
    return "11d+"


def _group_stats(rows: list) -> dict:
    rets = [float(r.return_pct) for r in rows]
    if not rets:
        return {"trades": 0}
    wins = sum(1 for r in rets if r >= 0)
    return {
        "trades": len(rets),
        "win_rate_pct": round(100.0 * wins / len(rets), 1),
        "compounded_return_pct": round(_compound(rets), 2),
        "avg_return_pct": round(sum(rets) / len(rets), 2),
        "median_return_pct": round(float(pd.Series(rets).median()), 2),
        "best_pct": round(max(rets), 2),
        "worst_pct": round(min(rets), 2),
    }


def _max_streak(flags: list[bool]) -> int:
    best = cur = 0
    for f in flags:
        if f:
            cur += 1
            best = max(best, cur)
        else:
            cur = 0
    return best


def _equity_curve(rets: list[float]) -> list[float]:
    eq = 1.0
    out = [1.0]
    for r in rets:
        eq *= 1.0 + r / 100.0
        out.append(eq)
    return out


def _rolling_window_scores(aligned, common, idx, warmup_lo: int, pre_seal_hi: int, months: int) -> list[dict]:
    """Non-overlapping calendar chunks of ``months`` length inside pre-seal."""
    out: list[dict] = []
    start = idx[warmup_lo]
    cursor = pd.Timestamp(year=start.year, month=start.month, day=1, tz="UTC")
    while cursor < SEALED_START:
        end = cursor + pd.DateOffset(months=months) - pd.Timedelta(days=1)
        lo = max(warmup_lo, int(idx.searchsorted(cursor, side="left")))
        hi = min(pre_seal_hi, int(idx.searchsorted(end, side="right")))
        if hi - lo >= 20:
            result = run_etf_backtest(
                aligned, loop_start_idx=lo, loop_end_idx_exclusive=hi, **common
            )
            out.append(
                {
                    "label": f"{cursor.date()}→{idx[hi - 1].date()}",
                    "months": months,
                    "sessions": hi - lo,
                    "closed_trades": result.closed_trades,
                    "compounded_return_pct": round(float(result.total_return_pct), 2),
                    "win_rate_pct": round(float(result.win_rate_pct), 1),
                    "closed_max_dd_pct": round(float(result.max_drawdown_pct), 2),
                    "bh_qqq_pct": round(buy_and_hold_close_to_close(aligned.qqq.daily, lo, hi), 2),
                    "trades_by_symbol": dict(Counter(t.symbol for t in result.trade_rows)),
                }
            )
        cursor = cursor + pd.DateOffset(months=months)
    return out


def _tqqq_only_counterfactual(aligned, common, lo: int, hi: int) -> dict:
    """Learning counterfactual: drop SQQQ BUY/FLIP entries (stay flat / exit only).

    Uses decide_fn so SQQQ entries become CASH; FLIP TQQQ→SQQQ becomes SELL TQQQ.
    Not a proposed live change.
    """
    from dataclasses import replace

    real_decide = decide

    def decide_tqqq_only(snapshot, position, *args, **kwargs):
        alert, new_pos, dbg = real_decide(snapshot, position, *args, **kwargs)
        if alert.alert_type in ("BUY", "FLIP") and alert.symbol == "SQQQ":
            flat = PositionState()
            if alert.alert_type == "FLIP" and position.active_symbol == "TQQQ":
                sell = replace(
                    alert,
                    alert_type="SELL",
                    symbol="TQQQ",
                    notes="Learning counterfactual: flip→exit TQQQ only.",
                    notes_kind="exit",
                    signal_quality=None,
                )
                return sell, flat, dbg
            cash = replace(
                alert,
                alert_type="CASH",
                symbol="CASH",
                notes="Learning counterfactual: SQQQ entry suppressed.",
                notes_kind="entry_skipped_counterfactual",
                signal_quality=None,
            )
            return cash, position, dbg
        return alert, new_pos, dbg

    result = run_etf_backtest(
        aligned,
        loop_start_idx=lo,
        loop_end_idx_exclusive=hi,
        decide_fn=decide_tqqq_only,
        **common,
    )
    rows = list(result.trade_rows)
    return {
        "closed_trades": result.closed_trades,
        "compounded_return_pct": round(float(result.total_return_pct), 2),
        "win_rate_pct": round(float(result.win_rate_pct), 1),
        "closed_max_dd_pct": round(float(result.max_drawdown_pct), 2),
        "mtm_return_pct": round(float(result.mtm_total_return_pct), 2),
        "mtm_max_dd_pct": round(float(result.mtm_max_drawdown_pct), 2),
        "exposure_pct": round(float(result.exposure_pct), 1),
        "trades_by_symbol": dict(Counter(t.symbol for t in rows)),
        "symbol_performance": {
            sym: _group_stats([t for t in rows if t.symbol == sym]) for sym in sorted({t.symbol for t in rows})
        },
        "note": "Learning counterfactual only — not a live/filter change.",
    }


def main() -> int:
    seal_before = hashlib.sha256(SEAL_PATH.read_bytes()).hexdigest() if SEAL_PATH.exists() else None
    settings = load_settings()
    params = strategy_params_from_settings(settings)
    if tuple(params.score_weights) != tuple(DEFAULT_SCORE_WEIGHTS):
        print("ABORT: weights != DEFAULT_SCORE_WEIGHTS")
        return 2

    now = datetime.now(timezone.utc)
    lookback_days = max(120, (now - datetime(2016, 1, 1, tzinfo=timezone.utc)).days + 14)
    reset_cli_call_count()
    t0 = time.time()
    print(f"[fetch] lookback_days={lookback_days} feed={settings.alpaca_data_feed}", flush=True)
    try:
     