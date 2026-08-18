"""
Batch-publish technical snapshots to Supabase for the Next.js dashboard.

Uses the same data path as the alert bot (KlickAnalytics CLI via data.load_candles)
and the same scoring stack (build_snapshot, regime, weighted checklist, dominance confidence).

Does not run trading logic or alerts. Configure SUPABASE_URL + SUPABASE_SERVICE_ROLE_KEY
and TECHNICAL_UNIVERSE (comma-separated symbols).

Usage (from repo root, with .env loaded via dotenv in config):

    python publish_technical_dashboard.py

Optional: DASHBOARD_BASE_URL + CRON_SECRET to POST /api/technical/revalidate after success.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any

import pandas as pd

from config import ConfigError, load_settings, strategy_params_from_settings
from data import DataError, load_candles
from indicators import IndicatorSnapshot, build_snapshot
from strategy import score_signals
from strategy_params import StrategyParams
from strategy_scoring import (
    detect_market_regime,
    normalized_confidence_score,
    weighted_signal_breakdown,
)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _ts_to_iso(ts: Any) -> str | None:
    if ts is None:
        return None
    try:
        return pd.Timestamp(ts).isoformat()
    except (TypeError, ValueError, OSError):
        return str(ts)


def _scorer_version() -> str:
    env_v = os.getenv("SCORER_VERSION", "").strip()
    if env_v:
        return env_v
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
            cwd=os.path.dirname(os.path.abspath(__file__)),
            timeout=5,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            return proc.stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return "unknown"


def _params_blob(params: StrategyParams) -> dict[str, Any]:
    d = asdict(params)
    d["score_weights"] = list(params.score_weights)
    return d


def _indicator_blob(s: IndicatorSnapshot) -> dict[str, Any]:
    return asdict(s)


def _supabase_client():  # type: ignore[no-untyped-def]
    from supabase_client import create_supabase_client

    return create_supabase_client()


def _universe_symbols() -> list[str]:
    raw = os.getenv("TECHNICAL_UNIVERSE", "").strip()
    if not raw:
        raw = os.getenv("NEWS_INGEST_TICKERS", "QQQ,SPY,AAPL,MSFT,NVDA").strip()
    parts = [p.strip().upper() for p in raw.split(",") if p.strip()]
    # de-dupe preserve order
    seen: set[str] = set()
    out: list[str] = []
    for p in parts:
        if p not in seen:
            seen.add(p)
            out.append(p)
    return out


def _breakdown_payload(
    snapshot: IndicatorSnapshot,
    bd: Any,
    bull: int,
    bear: int,
    confidence: int,
    regime: str,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "regime": regime,
        "bull_strength": bull,
        "bear_strength": bear,
        "confidence": confidence,
        "indicator_snapshot": _indicator_blob(snapshot),
        "weighted_bull": bd.weighted_bull,
        "weighted_bear": bd.weighted_bear,
        "max_weight_total": bd.max_weight_total,
        "bull_reasons": list(bd.bull_reasons),
        "bear_reasons": list(bd.bear_reasons),
    }


def _maybe_revalidate_dashboard() -> None:
    base = (os.getenv("DASHBOARD_BASE_URL") or "").strip().rstrip("/")
    secret = (os.getenv("CRON_SECRET") or "").strip()
    if not base or not secret:
        return
    import urllib.request

    url = f"{base}/api/technical/revalidate"
    req = urllib.request.Request(
        url,
        method="POST",
        headers={
            "Authorization": f"Bearer {secret}",
            "Content-Type": "application/json",
        },
        data=b"{}",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            if resp.status not in (200, 204):
                print(f"revalidate warning: HTTP {resp.status}", file=sys.stderr)
    except Exception as exc:
        print(f"revalidate skipped/failed: {exc}", file=sys.stderr)


def run_publish(*, dry_run: bool) -> int:
    symbols = _universe_symbols()
    if not symbols:
        print("No symbols in TECHNICAL_UNIVERSE / NEWS_INGEST_TICKERS.", file=sys.stderr)
        return 1

    if dry_run:
        print(f"Dry run: would score {len(symbols)} symbols (scorer_version={_scorer_version()})")
        for sym in symbols:
            print(f"  - {sym}")
        return 0

    settings = load_settings()
    params = strategy_params_from_settings(settings)
    scorer_ver = _scorer_version()
    params_json = _params_blob(params)

    supabase = _supabase_client()

    try:
        run_row = (
            supabase.table("dashboard_runs")
            .insert(
                {
                    "status": "running",
                    "tickers_requested": len(symbols),
                    "tickers_succeeded": 0,
                    "scorer_version": scorer_ver,
                    "params_json": params_json,
                    "source": "python_batch",
                }
            )
            .select("id")
            .single()
            .execute()
        )
    except Exception as exc:
        print(f"Failed to insert dashboard_run: {exc}", file=sys.stderr)
        return 1
    run_data = getattr(run_row, "data", None)
    if not run_data or not isinstance(run_data, dict):
        print("Failed to insert dashboard_run (empty response).", file=sys.stderr)
        return 1
    run_id = run_data["id"]

    anchor = settings.anchor_date or None
    failures: list[str] = []
    snapshot_rows: list[dict[str, Any]] = []

    for sym in symbols:
        try:
            candles = load_candles(sym, settings.klickanalytics_api_key, settings.klickanalytics_cli_command)
            snap = build_snapshot(candles.daily, candles.four_hour, anchor)
            regime = detect_market_regime(snap, params)
            bd = weighted_signal_breakdown(snap, params.score_weights)
            bull, bear, _, _ = score_signals(snap, params.score_weights)
            conf = normalized_confidence_score(bd.weighted_bull, bd.weighted_bear, bd.max_weight_total)
            breakdown = _breakdown_payload(snap, bd, bull, bear, conf, regime)
            snapshot_rows.append(
                {
                    "dashboard_run_id": run_id,
                    "symbol": sym,
                    "computed_at": _utc_now_iso(),
                    "daily_bar_end": _ts_to_iso(candles.daily.index[-1]),
                    "h4_bar_end": _ts_to_iso(candles.four_hour.index[-1]),
                    "regime": regime,
                    "bull_strength": bull,
                    "bear_strength": bear,
                    "confidence": conf,
                    "breakdown_json": breakdown,
                }
            )
        except (DataError, ConfigError, ValueError, OSError) as exc:
            failures.append(f"{sym}: {exc}")

    succeeded = len(snapshot_rows)
    finished_iso = _utc_now_iso()
    err_msg = "\n".join(failures) if failures else None
    final_status = "ok" if succeeded > 0 else "error"

    if snapshot_rows:
        try:
            supabase.table("stock_snapshots").insert(snapshot_rows).execute()
        except Exception as exc:
            print(f"Insert snapshots failed: {exc}", file=sys.stderr)
            supabase.table("dashboard_runs").update(
                {
                    "finished_at": finished_iso,
                    "status": "error",
                    "tickers_succeeded": 0,
                    "error_message": f"stock_snapshots insert failed: {exc}",
                }
            ).eq("id", run_id).execute()
            return 1

    supabase.table("dashboard_runs").update(
        {
            "finished_at": finished_iso,
            "status": final_status,
            "tickers_succeeded": succeeded,
            "error_message": err_msg,
        }
    ).eq("id", run_id).execute()

    if failures:
        print(f"Completed with {succeeded}/{len(symbols)} OK. Failures:\n{err_msg}", file=sys.stderr)
    else:
        print(f"Published {succeeded} snapshots (run {run_id}).")

    if final_status == "ok":
        _maybe_revalidate_dashboard()

    return 0 if succeeded > 0 else 1


def main() -> None:
    parser = argparse.ArgumentParser(description="Publish technical snapshots to Supabase.")
    parser.add_argument("--dry-run", action="store_true", help="List symbols only; no network.")
    args = parser.parse_args()
    try:
        code = run_publish(dry_run=args.dry_run)
    except ConfigError as exc:
        print(str(exc), file=sys.stderr)
        code = 1
    raise SystemExit(code)


if __name__ == "__main__":
    main()
