#!/usr/bin/env python3
"""Build Phase 1 probability labels from the ETF development window.

Offline only. Does not set PROBABILITY_CONFIDENCE, change Discord/Render, or
peek at the sealed holdout for fitting.

Usage (repo root, with Alpaca keys in .env):

    python3 scripts/build_probability_labels.py
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config import ConfigError, load_settings, strategy_params_from_settings
from data import DataError, format_cli_usage_line, load_candles, load_daily_bars
from etf_backtest import DEFAULT_ETF_COST_BPS
from event_calendar import load_merged_blackout_dates
from probability_labels import (
    collect_development_labels,
    summarize_labels,
    write_label_artifacts,
)
from strategy_eval import ResearchWindow
from strategy_types import DecideOptions


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Phase 1: rebuild development ETF trade labels + entry features."
    )
    parser.add_argument(
        "--bars",
        type=int,
        default=180,
        help="Same as main.py --backtest-bars (default 180 expands to research window).",
    )
    parser.add_argument(
        "--reports-dir",
        type=Path,
        default=Path("reports"),
        help="Output directory (gitignored artifacts).",
    )
    parser.add_argument(
        "--high-confidence-only",
        action="store_true",
        help="Match live --high-confidence-only (default off for Phase 1 labels).",
    )
    args = parser.parse_args(argv)

    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        return 1

    strategy_params = strategy_params_from_settings(settings)
    # Explicit freeze: Phase 1 does not retune weights or confidence floors.
    print(
        f"Using frozen score_weights={tuple(strategy_params.score_weights)} "
        f"min_confidence_to_trade={strategy_params.min_confidence_to_trade} "
        f"(PROBABILITY_CONFIDENCE not enabled)."
    )

    window = ResearchWindow()
    blocked_dates, risk_notes = load_merged_blackout_dates(
        settings.events_json,
        risk_avoidance=settings.event_risk_avoidance,
        risk_calendar_url=settings.event_risk_calendar_url or None,
    )
    for msg in risk_notes:
        print(f"[events] {msg}")

    try:
        candles = load_candles(
            settings.qqq_ticker,
            api_key=settings.alpaca_api_key,
            api_secret=settings.alpaca_api_secret,
            data_base_url=settings.alpaca_data_base_url,
            feed=settings.alpaca_data_feed,
            daily_lookback_days=window.daily_lookback_days,
            max_daily_bars=window.max_daily_bars,
        )
        tqqq_daily = load_daily_bars(
            "TQQQ",
            api_key=settings.alpaca_api_key,
            api_secret=settings.alpaca_api_secret,
            data_base_url=settings.alpaca_data_base_url,
            feed=settings.alpaca_data_feed,
            daily_lookback_days=window.daily_lookback_days,
            max_daily_bars=window.max_daily_bars,
        )
        sqqq_daily = load_daily_bars(
            "SQQQ",
            api_key=settings.alpaca_api_key,
            api_secret=settings.alpaca_api_secret,
            data_base_url=settings.alpaca_data_base_url,
            feed=settings.alpaca_data_feed,
            daily_lookback_days=window.daily_lookback_days,
            max_daily_bars=window.max_daily_bars,
        )
    except DataError as exc:
        text = str(exc)
        if "401" in text or "Unauthorized" in text or "authentication" in text.lower():
            print(f"Alpaca auth failed (401): {exc}", file=sys.stderr)
            return 2
        print(f"Data error: {exc}", file=sys.stderr)
        return 1

    cost_bps = float(settings.alpaca_paper_limit_offset_bps or DEFAULT_ETF_COST_BPS)
    try:
        result = collect_development_labels(
            candles,
            tqqq_daily,
            sqqq_daily,
            strategy_params=strategy_params,
            blocked_dates=blocked_dates,
            anchor_date=settings.anchor_date or None,
            requested_bars=args.bars,
            expand_default=True,
            cost_bps=cost_bps,
            decide_options=DecideOptions(high_confidence_only=args.high_confidence_only),
        )
    except (ValueError, KeyError, RuntimeError) as exc:
        print(f"Label build error: {exc}", file=sys.stderr)
        return 1

    paths = write_label_artifacts(result, reports_dir=args.reports_dir)
    summary = summarize_labels(result.rows)
    print("--- Phase 1 probability labels ---")
    for note in result.notes:
        print(f"  {note}")
    print(f"  label_count={summary['label_count']}")
    print(f"  win_rate={summary['win_rate']}")
    print(f"  by_dominance_bucket={summary['by_dominance_bucket']}")
    print(f"  wrote {paths['csv']}")
    print(f"  wrote {paths['summary_json']}")
    print(format_cli_usage_line())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
