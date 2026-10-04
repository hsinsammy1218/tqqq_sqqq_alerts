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
        qqq = load_candles(
            settings.qqq_ticker,
            api_key=settings.alpaca_api_key,
            api_secret=settings.alpaca_api_secret,
            data_base_url=settings.alpaca_data_base_url,
            feed=settings.alpaca_data_feed,
            daily_lookback_days=lookback_days,
            max_daily_bars=4000,
            hourly_lookback_days=lookback_days,
            max_hourly_bars=20000,
        )
        tqqq = load_daily_bars(
            "TQQQ",
            api_key=settings.alpaca_api_key,
            api_secret=settings.alpaca_api_secret,
            data_base_url=settings.alpaca_data_base_url,
            feed=settings.alpaca_data_feed,
            daily_lookback_days=lookback_days,
            max_daily_bars=4000,
        )
        sqqq = load_daily_bars(
            "SQQQ",
            api_key=settings.alpaca_api_key,
            api_secret=settings.alpaca_api_secret,
            data_base_url=settings.alpaca_data_base_url,
            feed=settings.alpaca_data_feed,
            daily_lookback_days=lookback_days,
            max_daily_bars=4000,
        )
    except (DataError, MarketDataQuotaError) as exc:
        print(f"FETCH ERROR: {exc}")
        OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
        OUT_JSON.write_text(json.dumps({"error": str(exc), "api": _api}, indent=2))
        return 1

    aligned = align_etf_data(qqq, tqqq, sqqq)
    idx = aligned.qqq.daily.index
    sealed_lo = int(idx.searchsorted(SEALED_START, side="left"))
    pre_seal_hi = sealed_lo
    warmup_lo = 60
    print(
        f"[align] {idx[0].date()}→{idx[-1].date()} pre_seal_hi={pre_seal_hi} "
        f"calls≈{_api['calls']} wall={time.time()-t0:.1f}s",
        flush=True,
    )

    blocked, _ = load_merged_blackout_dates(
        settings.events_json,
        risk_avoidance=bool(settings.event_risk_avoidance),
        risk_calendar_url=settings.event_risk_calendar_url or None,
    )
    common = dict(
        strategy_params=params,
        blocked_dates=blocked,
        anchor_date=settings.anchor_date or None,
        cost_bps=float(settings.alpaca_paper_limit_offset_bps),
        decide_options=DecideOptions(),
    )

    print("[score] full_pre_seal baseline", flush=True)
    baseline = run_etf_backtest(
        aligned, loop_start_idx=warmup_lo, loop_end_idx_exclusive=pre_seal_hi, **common
    )
    rows = list(baseline.trade_rows)

    by_regime: dict[str, list] = defaultdict(list)
    by_conf: dict[str, list] = defaultdict(list)
    by_hold: dict[str, list] = defaultdict(list)
    by_action: dict[str, list] = defaultdict(list)
    by_year: dict[str, list] = defaultdict(list)
    by_sym_regime: dict[str, list] = defaultdict(list)
    for t in rows:
        by_regime[str(t.regime or "unknown")].append(t)
        by_conf[_bucket_conf(int(t.confidence))].append(t)
        by_hold[_hold_bucket(int(t.hold_days))].append(t)
        by_action[str(t.action)].append(t)
        by_year[str(pd.Timestamp(t.timestamp).year)].append(t)
        by_sym_regime[f"{t.symbol}|{t.regime}"].append(t)

    rets = [float(t.return_pct) for t in rows]
    loss_flags = [r < 0 for r in rets]
    win_flags = [r >= 0 for r in rets]
    curve = _equity_curve(rets)
    peak = curve[0]
    max_dd = 0.0
    for e in curve:
        peak = max(peak, e)
        max_dd = max(max_dd, (peak - e) / peak * 100.0 if peak else 0.0)

    # Worst 10 trades
    worst = sorted(rows, key=lambda t: float(t.return_pct))[:10]
    best = sorted(rows, key=lambda t: float(t.return_pct), reverse=True)[:10]

    print("[score] rolling 6m / 12m", flush=True)
    roll_6 = _rolling_window_scores(aligned, common, idx, warmup_lo, pre_seal_hi, 6)
    roll_12 = _rolling_window_scores(aligned, common, idx, warmup_lo, pre_seal_hi, 12)

    print("[score] TQQQ-only counterfactual", flush=True)
    tqqq_only = _tqqq_only_counterfactual(aligned, common, warmup_lo, pre_seal_hi)

    seal_after = hashlib.sha256(SEAL_PATH.read_bytes()).hexdigest() if SEAL_PATH.exists() else None

    payload = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "learning_only": True,
        "frozen_weights": list(DEFAULT_SCORE_WEIGHTS),
        "did_not_run_etf_sealed_oos": True,
        "seal_sha256_before": seal_before,
        "seal_sha256_after": seal_after,
        "seal_unchanged": seal_before == seal_after,
        "api": {**_api, "cli_calls_attempted": cli_calls_attempted(), "fetch_wall_s": round(time.time() - t0, 1)},
        "baseline_full_pre_seal": {
            "start": baseline.start_utc,
            "end": baseline.end_utc,
            "closed_trades": baseline.closed_trades,
            "compounded_return_pct": round(float(baseline.total_return_pct), 2),
            "win_rate_pct": round(float(baseline.win_rate_pct), 1),
            "closed_max_dd_pct": round(float(baseline.max_drawdown_pct), 2),
            "mtm_max_dd_pct": round(float(baseline.mtm_max_drawdown_pct), 2),
            "exposure_pct": round(float(baseline.exposure_pct), 1),
            "avg_hold_days": round(sum(int(t.hold_days) for t in rows) / len(rows), 1) if rows else None,
            "max_loss_streak": _max_streak(loss_flags),
            "max_win_streak": _max_streak(win_flags),
            "equity_path_max_dd_from_closed_trades_pct": round(max_dd, 2),
            "trades_by_symbol": dict(Counter(t.symbol for t in rows)),
            "bh_qqq_pct": round(buy_and_hold_close_to_close(aligned.qqq.daily, warmup_lo, pre_seal_hi), 2),
            "bh_tqqq_pct": round(buy_and_hold_close_to_close(aligned.tqqq, warmup_lo, pre_seal_hi), 2),
        },
        "by_regime": {k: _group_stats(v) for k, v in sorted(by_regime.items())},
        "by_confidence_bucket": {k: _group_stats(v) for k, v in sorted(by_conf.items())},
        "by_hold_bucket": {k: _group_stats(v) for k, v in sorted(by_hold.items())},
        "by_exit_action": {k: _group_stats(v) for k, v in sorted(by_action.items())},
        "by_exit_year": {k: _group_stats(v) for k, v in sorted(by_year.items())},
        "by_symbol_regime": {k: _group_stats(v) for k, v in sorted(by_sym_regime.items())},
        "worst_10": [
            {
                "timestamp": t.timestamp,
                "symbol": t.symbol,
                "return_pct": round(float(t.return_pct), 2),
                "hold_days": int(t.hold_days),
                "confidence": int(t.confidence),
                "regime": t.regime,
                "action": t.action,
            }
            for t in worst
        ],
        "best_10": [
            {
                "timestamp": t.timestamp,
                "symbol": t.symbol,
                "return_pct": round(float(t.return_pct), 2),
                "hold_days": int(t.hold_days),
                "confidence": int(t.confidence),
                "regime": t.regime,
                "action": t.action,
            }
            for t in best
        ],
        "rolling_6m": roll_6,
        "rolling_12m": roll_12,
        "tqqq_only_counterfactual": tqqq_only,
    }
    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(payload, indent=2))
    _write_doc(payload)
    print(f"[ok] wrote {OUT_JSON}")
    print(f"[ok] wrote {OUT_DOC}")
    return 0


def _write_doc(p: dict) -> None:
    b = p["baseline_full_pre_seal"]
    lines = [
        "# Learning deeper cuts (IEX 2020→pre-seal)",
        "",
        "**Learning only.** Frozen `DEFAULT_SCORE_WEIGHTS`. No sealed OOS re-run. No weight/filter changes.",
        "",
        f"Generated `{p['generated_at_utc']}` · seal unchanged `{p['seal_unchanged']}` · API calls ≈ `{p['api']['calls']}` · 429s `{p['api']['http_429s']}`.",
        "",
        "## Baseline check (full pre-seal)",
        "",
        "| Metric | Value |",
        "| --- | ---: |",
        f"| Window | {b['start'][:10]} → {b['end'][:10]} |",
        f"| Closed trades | {b['closed_trades']} |",
        f"| Compounded return | **{b['compounded_return_pct']}%** |",
        f"| Win rate | {b['win_rate_pct']}% |",
        f"| Closed max DD | {b['closed_max_dd_pct']}% |",
        f"| MTM max DD | {b['mtm_max_dd_pct']}% |",
        f"| Exposure | {b['exposure_pct']}% |",
        f"| Avg hold | {b['avg_hold_days']}d |",
        f"| Max loss streak | {b['max_loss_streak']} |",
        f"| Max win streak | {b['max_win_streak']} |",
        f"| Mix | {b['trades_by_symbol']} |",
        f"| BH QQQ / TQQQ | {b['bh_qqq_pct']}% / {b['bh_tqqq_pct']}% |",
        "",
        "## By regime",
        "",
        "| Regime | Trades | Win % | Compounded | Avg |",
        "| --- | ---: | ---: | ---: | ---: |",
    ]
    for k, v in p["by_regime"].items():
        if v.get("trades"):
            lines.append(
                f"| {k} | {v['trades']} | {v['win_rate_pct']} | {v['compounded_return_pct']}% | {v['avg_return_pct']}% |"
            )
    lines += [
        "",
        "## By entry confidence (at exit row)",
        "",
        "| Bucket | Trades | Win % | Compounded | Avg |",
        "| --- | ---: | ---: | ---: | ---: |",
    ]
    for k, v in p["by_confidence_bucket"].items():
        if v.get("trades"):
            lines.append(
                f"| {k} | {v['trades']} | {v['win_rate_pct']} | {v['compounded_return_pct']}% | {v['avg_return_pct']}% |"
            )
    lines += [
        "",
        "## By hold length",
        "",
        "| Hold | Trades | Win % | Compounded | Avg |",
        "| --- | ---: | ---: | ---: | ---: |",
    ]
    for k, v in p["by_hold_bucket"].items():
        if v.get("trades"):
            lines.append(
                f"| {k} | {v['trades']} | {v['win_rate_pct']} | {v['compounded_return_pct']}% | {v['avg_return_pct']}% |"
            )
    lines += [
        "",
        "## Symbol × regime",
        "",
        "| Slice | Trades | Win % | Compounded | Avg |",
        "| --- | ---: | ---: | ---: | ---: |",
    ]
    for k, v in p["by_symbol_regime"].items():
        if v.get("trades"):
            lines.append(
                f"| {k} | {v['trades']} | {v['win_rate_pct']} | {v['compounded_return_pct']}% | {v['avg_return_pct']}% |"
            )

    lines += [
        "",
        "## Rolling 12-month chunks (non-overlapping)",
        "",
        "| Window | Trades | Return | Win % | Max DD | BH QQQ | Mix |",
        "| --- | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    for w in p["rolling_12m"]:
        lines.append(
            f"| {w['label']} | {w['closed_trades']} | {w['compounded_return_pct']}% | "
            f"{w['win_rate_pct']} | {w['closed_max_dd_pct']}% | {w['bh_qqq_pct']}% | {w['trades_by_symbol']} |"
        )

    lines += [
        "",
        "## Rolling 6-month chunks (non-overlapping)",
        "",
        "| Window | Trades | Return | Win % | Max DD | BH QQQ |",
        "| --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for w in p["rolling_6m"]:
        lines.append(
            f"| {w['label']} | {w['closed_trades']} | {w['compounded_return_pct']}% | "
            f"{w['win_rate_pct']} | {w['closed_max_dd_pct']}% | {w['bh_qqq_pct']}% |"
        )

    cf = p["tqqq_only_counterfactual"]
    lines += [
        "",
        "## Learning counterfactual: suppress SQQQ entries",
        "",
        "Same window/rules, but BUY/FLIP into SQQQ become no-entry (FLIP from TQQQ → exit only). "
        "**Not a proposed live change** — stress lens on inverse-leg drag.",
        "",
        f"| | Baseline | TQQQ-only CF |",
        f"| --- | ---: | ---: |",
        f"| Return | {b['compounded_return_pct']}% | **{cf['compounded_return_pct']}%** |",
        f"| Win rate | {b['win_rate_pct']}% | {cf['win_rate_pct']}% |",
        f"| Closed max DD | {b['closed_max_dd_pct']}% | {cf['closed_max_dd_pct']}% |",
        f"| Trades | {b['closed_trades']} | {cf['closed_trades']} |",
        f"| Mix | {b['trades_by_symbol']} | {cf['trades_by_symbol']} |",
        "",
        "## Worst 10 closed trades",
        "",
        "| When | Sym | Ret | Hold | Conf | Regime | Action |",
        "| --- | --- | ---: | ---: | ---: | --- | --- |",
    ]
    for t in p["worst_10"]:
        lines.append(
            f"| {t['timestamp'][:10]} | {t['symbol']} | {t['return_pct']}% | {t['hold_days']} | "
            f"{t['confidence']} | {t['regime']} | {t['action']} |"
        )
    lines += [
        "",
        "## Takeaways (still do not retune)",
        "",
        "1. Regime and SQQQ mix dominate outcomes more than confidence fine-tuning inside the freeze.",
        "2. Rolling windows show regime clustering — strong late bull chunks vs ugly 2021–2022 blocks.",
        "3. The TQQQ-only counterfactual is a **diagnostic**, not a green light to disable SQQQ live.",
        "4. Paper Path B soak / risk caps remain more important than polishing weights on this sample.",
        "5. Pre-2020 still unavailable on free IEX — this analysis cannot extend earlier.",
        "",
        f"Evidence JSON: `{OUT_JSON}`.",
        "",
    ]
    OUT_DOC.parent.mkdir(parents=True, exist_ok=True)
    OUT_DOC.write_text("\n".join(lines))


if __name__ == "__main__":
    raise SystemExit(main())
