
from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

from alerts import format_alert_message, send_discord
from alpaca_paper import (
    PaperTradingError,
    execute_paper_orders,
    fetch_broker_snapshot,
    should_submit_paper_orders,
)
from api_quota_notify import maybe_notify_quota_reached
from api_usage import track_and_maybe_warn_api_usage
from backtest import export_backtest_trades_csv, format_backtest_report, run_backtest
from backtest_sweep import (
    export_sweep_csv,
    format_sweep_report,
    run_parameter_sweep,
    small_research_grid,
    sweep_grid_from_settings,
)
from strategy_eval import (
    ResearchWindow,
    compare_checklist_variants,
    format_checklist_compare_report,
    format_strategy_eval_report,
    grid_neighbors_payload,
    resolve_research_bars,
    run_strategy_evaluation,
    write_json,
)
from cli_args import build_parser
from config import ConfigError, Settings, load_settings, strategy_params_from_settings
from data import (
    DataError,
    MarketDataQuotaError,
    cli_calls_attempted,
    format_cli_usage_line,
    load_candles,
    load_daily_bars,
)
from etf_backtest import (
    SEAL_PATH,
    evaluate_development,
    evaluate_sealed,
    format_phase_report,
    write_seal,
)
from event_calendar import load_merged_blackout_dates
from health_check import run_health_check
from indicators import build_snapshot
from journal import append_journal, load_last_journal_alert
from market_hours import cron_skip_reason
from runtime_logging import format_utc_z, log_event, setup_logger
from learner import format_learner_summary, run_learn_from_trades
from trade_log_report import (
    format_trade_log_report_summary,
    load_research_max_dd_pct,
    run_trade_log_report,
)
from walk_forward import (
    export_walk_forward_csv,
    format_walk_forward_report,
    run_walk_forward,
    walk_forward_fold_count,
    walk_forward_grid_from_settings,
)
from position_reconcile import reconcile_at_start, state_to_save
from position_store import PositionStoreError, position_store_from_settings
from trade_log_store import TradeLogStoreError, trade_log_store_from_settings
from strategy import (
    AlertDecision,
    DecideOptions,
    PositionState,
    RunTechnicalMeta,
    decide,
    format_technical_breakdown,
)