from __future__ import annotations

import argparse


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "QQQ-driven TQQQ/SQQQ alert-only system: emits BUY/SELL/FLIP/CASH signals "
            "for manual review; does not place orders or connect to brokers."
        ),
    )
    parser.add_argument("--dry-run", action="store_true", help="Print payloads without sending Discord alerts.")
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Structured file log verbosity (default: INFO).",
    )
    parser.add_argument(
        "--no-technical",
        action="store_true",
        help="Skip the detailed technical breakdown block after the alert summary.",
    )
    parser.add_argument(
        "--health-check",
        action="store_true",
        help="Run non-destructive environment checks and exit.",
    )
    parser.add_argument(
        "--backtest",
        action="store_true",
        help="Run a lightweight historical backtest on QQQ rules and exit.",
    )
    parser.add_argument(
        "--backtest-bars",
        type=int,
        default=180,
        metavar="N",
        help="Number of recent daily bars to evaluate in --backtest mode (default: 180).",
    )
    parser.add_argument(
        "--backtest-report-csv",
        default=None,
        metavar="PATH",
        help="Optional path to write per-trade backtest CSV report.",
    )
    parser.add_argument(
        "--backtest-sweep",
        action="store_true",
        help="Run backtests over BACKTEST_SWEEP_* grids (e.g. BACKTEST_SWEEP_BULL; see README).",
    )
    parser.add_argument(
        "--backtest-sweep-csv",
        default=None,
        metavar="PATH",
        help="Optional path to write ranked sweep results CSV.",
    )
    parser.add_argument(
        "--walk-forward",
        action="store_true",
        help=(
            "Walk-forward grid search over WALK_FORWARD_GRID_* env lists; ranks by mean "
            "balanced score on chronological validation folds."
        ),
    )
    parser.add_argument(
        "--walk-forward-csv",
        default="reports/walk_forward_results.csv",
        metavar="PATH",
        help="Path for ranked walk-forward CSV (default: reports/walk_forward_results.csv).",
    )
    parser.add_argument(
        "--debug-strategy",
        action="store_true",
        help="With --backtest, --backtest-sweep, or --walk-forward: print score/regime/threshold diagnostics.",
    )
    parser.add_argument(
        "--debug-strategy-sanity",
        action="store_true",
        help="Requires --debug-strategy: flat entries use weighted dominance only (debug; not for live).",
    )
    parser.add_argument(
        "--high-confidence-only",
        action="store_true",
        help="Flat BUY only when normalized confidence ≥75% (after MIN_CONFIDENCE_TO_TRADE). Applies to live and research modes.",
    )
    pos = parser.add_mutually_exclusive_group()
    pos.add_argument(
        "--flat",
        action="store_true",
        help="Reset tracked position to flat (no TQQQ/SQQQ). Use when you have zero broker positions.",
    )
    pos.add_argument(
        "--set-position",
        choices=["TQQQ", "SQQQ"],
        metavar="SYMBOL",
        help="Tell the bot you hold this ETF. Optional: --entry-price / --entry-time.",
    )
    parser.add_argument(
        "--entry-price",
        type=float,
        default=None,
        help="Your average ETF fill (optional). If omitted, exits use QQQ daily close as a proxy.",
    )
    parser.add_argument(
        "--entry-time",
        default=None,
        metavar="ISO",
        help="Open time ISO8601 (optional). If omitted, max-hold anchors from the first bot run after --set-position.",
    )
    return parser
