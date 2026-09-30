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
        "--strategy-eval",
        action="store_true",
        help=(
            "Score the QQQ-proxy backtest (trade count, profit factor, drawdowns, "
            "reward/risk, PROM, walk-forward efficiency, exit comparison) and write "
            "reports/strategy_eval.json. Does not submit orders."
        ),
    )
    parser.add_argument(
        "--strategy-eval-json",
        default="reports/strategy_eval.json",
        metavar="PATH",
        help="Path for --strategy-eval JSON (default: reports/strategy_eval.json).",
    )
    parser.add_argument(
        "--small-grid",
        action="store_true",
        help=(
            "Run the small 2-level grid over confidence, dominance gap, range add, "
            "flip hold, stop, and take-profit. Writes neighbor share to "
            "reports/grid_neighbors.json. Does not submit orders."
        ),
    )
    parser.add_argument(
        "--small-grid-json",
        default="reports/grid_neighbors.json",
        metavar="PATH",
        help="Path for --small-grid JSON (default: reports/grid_neighbors.json).",
    )
    parser.add_argument(
        "--checklist-compare",
        action="store_true",
        help=(
            "Compare checklist redesign variants vs baseline on the strategy-eval window. "
            "Writes reports/checklist_compare.json. Does not submit orders or change live defaults."
        ),
    )
    parser.add_argument(
        "--checklist-compare-json",
        default="reports/checklist_compare.json",
        metavar="PATH",
        help="Path for --checklist-compare JSON (default: reports/checklist_compare.json).",
    )
    parser.add_argument(
        "--trade-log-report",
        action="store_true",
        help=(
            "Summarize the paper trade journal (status/source/alert/regime/confidence buckets, "
            "inferred fill P&L, paper DD vs research ~19%). Writes reports/trade_log_report.json. "
            "Prefers Supabase bot_trade_log when TRADE_LOG_BACKEND=supabase; else logs/trades.jsonl. "
            "No live orders."
        ),
    )
    parser.add_argument(
        "--trade-log-report-json",
        default="reports/trade_log_report.json",
        metavar="PATH",
        help="Path for --trade-log-report JSON (default: reports/trade_log_report.json).",
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
    parser.add_argument(
        "--market-hours-only",
        action="store_true",
        help=(
            "Skip the live alert run when the US equity market is closed "
            "(weekends, NYSE holidays, outside regular session in US/Eastern)."
        ),
    )
    pos = parser.add_mutually_exclusive_group()
    pos.add_argument(
        "--flat",
        action="store_true",
        help="Reset tracked position to flat and exit (no alert run). Use when you have zero broker positions.",
    )
    pos.add_argument(
        "--set-position",
        choices=["TQQQ", "SQQQ"],
        metavar="SYMBOL",
        help="Save that you hold this ETF and exit (no alert run). Optional: --entry-price / --entry-time.",
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
