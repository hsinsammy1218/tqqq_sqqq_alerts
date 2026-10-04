
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from pathlib import Path

from alerts import format_alert_message, send_discord
from rh_agent import build_rh_playbook, format_rh_playbook
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


def _sample_discord_preview_alert() -> AlertDecision:
    return AlertDecision(
        alert_type="BUY",
        symbol="TQQQ",
        qqq_trend_reason="daily close > EMA20; daily EMA20 > EMA50 | regime=trend_up",
        bullish_score=75,
        bearish_score=25,
        confidence_score=78,
        entry_zone_low=432.10,
        entry_zone_high=439.70,
        stop_loss=400.65,
        take_profit=502.13,
        stretch_take_profit=545.79,
        max_hold_date="2026-05-15",
        timestamp=format_utc_z(datetime.now(timezone.utc)),
        notes="Bullish QQQ setup (Discord preview sample).",
        notes_kind="buy_bull",
        signal_quality="HIGH",
    )


def _run_rh_preview(logger: logging.Logger) -> int:
    """Print a review-only Robinhood agent playbook. Never calls Robinhood.

    Does not load Alpaca settings. A missing market-data key must not block this.
    """
    from dotenv import find_dotenv, load_dotenv

    load_dotenv(find_dotenv(usecwd=True) or None)
    journal_alert = load_last_journal_alert(Path(os.getenv("JOURNAL_CSV", "alerts_journal.csv")))
    if journal_alert is not None:
        alert = journal_alert
        source = "journal"
    else:
        alert = _sample_discord_preview_alert()
        source = "sample"
    tqqq_only = os.getenv("RH_AGENT_TQQQ_ONLY", "true").strip().lower() in {"1", "true", "yes", "on"}
    try:
        max_notional = float(os.getenv("RH_AGENT_MAX_NOTIONAL", "200"))
    except ValueError:
        print("Config error: RH_AGENT_MAX_NOTIONAL must be a number.")
        return 1
    playbook = build_rh_playbook(
        alert,
        max_notional_usd=max_notional,
        tqqq_only=tqqq_only,
        allow_place=False,
    )
    print(f"Robinhood agent preview from {source}: {alert.alert_type} {alert.symbol}")
    print(format_rh_playbook(playbook))
    print("Review only — no Robinhood call, no paper order, no position change.")
    log_event(
        logger,
        logging.INFO,
        "Robinhood agent preview",
        alert_type=alert.alert_type,
        symbol=alert.symbol,
        source=source,
        status=playbook["status"],
    )
    return 0


def _run_discord_test(settings: Settings, logger: logging.Logger) -> int:
    """Post one preview embed; never runs paper trading or updates position."""
    if not settings.discord_webhook_url:
        print("Config error: DISCORD_WEBHOOK_URL is required for --discord-test.")
        return 1
    journal_alert = load_last_journal_alert(settings.journal_csv)
    if journal_alert is not None:
        alert = journal_alert
        source = "journal"
    else:
        alert = _sample_discord_preview_alert()
        source = "sample"
    print(f"Discord preview from {source}: {alert.alert_type} {alert.symbol}")
    try:
        send_discord(
            settings.discord_webhook_url,
            alert,
            dry_run=False,
            preview=True,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"Discord preview failed: {exc}")
        log_event(logger, logging.ERROR, "Discord preview failed", error=str(exc))
        return 1
    print("Discord preview posted (no paper orders, no position change).")
    log_event(
        logger,
        logging.INFO,
        "Discord preview posted",
        alert_type=alert.alert_type,
        symbol=alert.symbol,
        source=source,
    )
    return 0


def _parse_entry_time_arg(value: str) -> datetime:
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _position_to_dict(state: PositionState) -> dict[str, object]:
    return {
        "symbol": state.active_symbol,
        "entry_price": state.entry_price,
        "entry_time": state.entry_timestamp,
        "last_signal": state.last_signal,
        "updated_at": state.updated_at,
    }


def _missing_live_webhook_message(settings: Settings) -> str | None:
    if settings.dry_run or settings.discord_webhook_url:
        return None
    return "DISCORD_WEBHOOK_URL is required when not in dry-run mode."


def _log_cli_usage(
    settings: Settings,
    logger: logging.Logger,
    *,
    reason: str | None = None,
    quota_error_detail: str | None = None,
) -> None:
    calls = cli_calls_attempted()
    snapshot = track_and_maybe_warn_api_usage(
        calls=calls,
        webhook_url=settings.discord_webhook_url,
        dry_run=settings.dry_run,
        monthly_limit=settings.alpaca_monthly_limit,
        warn_pct=settings.alpaca_usage_warn_pct,
        logger=logger,
        quota_error_detail=quota_error_detail,
    )
    month_total = snapshot.total_calls if snapshot else None
    month_limit = settings.alpaca_monthly_limit if snapshot else None
    print(
        format_cli_usage_line(
            reason=reason,
            month_total=month_total,
            month_limit=month_limit,
        )
    )


def run() -> int:
    parser = build_parser()
    args = parser.parse_args()
    research_flags = (
        args.backtest,
        args.backtest_sweep,
        args.walk_forward,
        args.strategy_eval,
        args.small_grid,
        args.checklist_compare,
        args.trade_log_report,
        args.learn_from_trades,
        args.etf_backtest,
        args.etf_sealed_oos,
    )
    if args.etf_backtest and args.etf_sealed_oos:
        print("Config error: run --etf-backtest and --etf-sealed-oos as separate commands.")
        return 1
    if (args.etf_backtest or args.etf_sealed_oos) and (
        args.backtest
        or args.backtest_sweep
        or args.walk_forward
        or args.strategy_eval
        or args.small_grid
        or args.checklist_compare
        or args.trade_log_report
        or args.learn_from_trades
    ):
        print("Config error: the ETF backtest cannot be combined with other research commands.")
        return 1
    if args.walk_forward and (
        args.backtest
        or args.backtest_sweep
        or args.strategy_eval
        or args.small_grid
        or args.checklist_compare
        or args.trade_log_report
        or args.learn_from_trades
    ):
        print(
            "Config error: --walk-forward cannot be combined with --backtest, "
            "--backtest-sweep, --strategy-eval, --small-grid, --checklist-compare, "
            "--trade-log-report, or --learn-from-trades."
        )
        return 1
    if sum(1 for flag in (args.backtest, args.backtest_sweep) if flag) and (