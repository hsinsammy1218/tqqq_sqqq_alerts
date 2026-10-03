
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
        args.strategy_eval
        or args.small_grid
        or args.checklist_compare
        or args.trade_log_report
        or args.learn_from_trades
    ):
        print(
            "Config error: --strategy-eval / --small-grid / --checklist-compare / "
            "--trade-log-report / --learn-from-trades cannot be combined with "
            "--backtest or --backtest-sweep."
        )
        return 1
    if args.trade_log_report and (
        args.strategy_eval
        or args.small_grid
        or args.checklist_compare
        or args.health_check
        or args.learn_from_trades
    ):
        print(
            "Config error: --trade-log-report cannot be combined with "
            "--strategy-eval, --small-grid, --checklist-compare, --health-check, "
            "or --learn-from-trades."
        )
        return 1
    if args.learn_from_trades and (
        args.strategy_eval or args.small_grid or args.checklist_compare or args.health_check
    ):
        print(
            "Config error: --learn-from-trades cannot be combined with "
            "--strategy-eval, --small-grid, --checklist-compare, or --health-check."
        )
        return 1
    if args.learn_discord and not args.learn_from_trades:
        print("Config error: --learn-discord requires --learn-from-trades.")
        return 1
    if args.debug_strategy_sanity and not args.debug_strategy:
        print("Config error: --debug-strategy-sanity requires --debug-strategy.")
        return 1
    logger = setup_logger(args.log_level)
    run_ts = format_utc_z(datetime.now(timezone.utc))
    log_event(
        logger,
        logging.INFO,
        "Run started",
        run_timestamp=run_ts,
        dry_run_arg=bool(args.dry_run),
        health_check=bool(args.health_check),
        no_technical=bool(args.no_technical),
        backtest_report_csv=args.backtest_report_csv,
        backtest_sweep=args.backtest_sweep,
        backtest_sweep_csv=args.backtest_sweep_csv,
        walk_forward=args.walk_forward,
        walk_forward_csv=args.walk_forward_csv,
        strategy_eval=args.strategy_eval,
        small_grid=args.small_grid,
        checklist_compare=args.checklist_compare,
        trade_log_report=args.trade_log_report,
        learn_from_trades=args.learn_from_trades,
        learn_discord=args.learn_discord,
        debug_strategy=args.debug_strategy,
        debug_strategy_sanity=args.debug_strategy_sanity,
        high_confidence_only=args.high_confidence_only,
        market_hours_only=bool(args.market_hours_only),
    )

    if args.set_position is not None and args.entry_price is not None and args.entry_price <= 0:
        print("Config error: --entry-price must be positive when provided.")
        log_event(logger, logging.ERROR, "Invalid CLI args", error="entry-price must be positive")
        return 1

    try:
        settings = load_settings()
        if args.dry_run:
            settings = settings.__class__(**{**settings.__dict__, "dry_run": True})
        strategy_params = strategy_params_from_settings(settings)
        log_event(
            logger,
            logging.INFO,
            "Settings loaded",
            qqq_ticker=settings.qqq_ticker,
            position_state_backend=settings.position_state_backend,
            position_state_json=str(settings.position_state_json),
            position_state_bot_id=settings.position_state_bot_id,
            events_json=str(settings.events_json),
            dry_run=settings.dry_run,
            alpaca_paper_trading=settings.alpaca_paper_trading,
            alpaca_trading_base_url=settings.alpaca_trading_base_url,
            log_level=args.log_level,
        )
    except ConfigError as exc:
        print(f"Config error: {exc}")
        log_event(logger, logging.ERROR, "Config load failed", error=str(exc))
        return 1

    try:
        position_store = position_store_from_settings(settings)
    except ConfigError as exc:
        print(f"Config error: {exc}")
        log_event(logger, logging.ERROR, "Position store init failed", error=str(exc))
        return 1
    position_state_label = position_store.describe()

    stamp = format_utc_z(datetime.now(timezone.utc))
    if args.flat:
        try:
            position_store.save(
                PositionState(
                    active_symbol=None,
                    entry_price=None,
                    entry_timestamp=None,
                    last_signal="MANUAL_FLAT",
                    updated_at=stamp,
                )
            )
        except PositionStoreError as exc:
            print(f"Position store error: {exc}")
            log_event(logger, logging.ERROR, "Manual flat save failed", error=str(exc))
            return 1
        print("Position reset: flat (no active TQQQ/SQQQ in bot memory).")
        log_event(
            logger,
            logging.INFO,
            "Manual flat applied",
            position_state_path=position_state_label,
            position_after={
                "symbol": None,
                "entry_price": None,
                "entry_time": None,
                "last_signal": "MANUAL_FLAT",
                "updated_at": stamp,
            },
        )
        return 0
    elif args.set_position is not None:
        entry_price_f = float(args.entry_price) if args.entry_price is not None else None
        entry_ts_str: str | None = None
        if args.entry_time:
            try:
                entry_ts_str = format_utc_z(_parse_entry_time_arg(args.entry_time))
            except ValueError:
                print("Config error: --entry-time must be valid ISO8601 (e.g. 2026-05-01T16:00:00Z).")
                log_event(logger, logging.ERROR, "Invalid entry time argument", entry_time=args.entry_time)
                return 1
        try:
            position_store.save(
                PositionState(
                    active_symbol=args.set_position,
                    entry_price=entry_price_f,
                    entry_timestamp=entry_ts_str,
                    last_signal="MANUAL_SET",
                    updated_at=stamp,
                )
            )
        except PositionStoreError as exc:
            print(f"Position store error: {exc}")
            log_event(logger, logging.ERROR, "Manual position set failed", error=str(exc))
            return 1
        extra = []
        if entry_price_f is not None:
            extra.append(f"@ {entry_price_f}")
        else:
            extra.append("entry price unset (exit levels use QQQ daily close as proxy)")
        if entry_ts_str:
            extra.append(f"opened {entry_ts_str}")
        else:
            extra.append("entry time unset (max hold counts from first successful bot run)")
        print(f"Position set: {args.set_position} - " + "; ".join(extra) + ".")
        log_event(
            logger,
            logging.INFO,
            "Manual position set",
            position_state_path=position_state_label,
            position_after={
                "symbol": args.set_position,
                "entry_price": entry_price_f,
                "entry_time": entry_ts_str,
                "last_signal": "MANUAL_SET",
                "updated_at": stamp,
            },
        )
        return 0

    if args.health_check:
        return run_health_check(settings, settings.dry_run, logger)

    if args.discord_test:
        return _run_discord_test(settings, logger)

    if args.trade_log_report:
        research_dd = load_research_max_dd_pct()
        try:
            trade_log_store = trade_log_store_from_settings(settings)
            payload = run_trade_log_report(
                trade_log_path=settings.trade_log_jsonl,
                report_path=args.trade_log_report_json,
                research_max_dd_pct=research_dd,
                trade_log_store=trade_log_store,
            )
        except (OSError, TradeLogStoreError, ConfigError) as exc:
            print(f"Trade log report error: {exc}")
            log_event(logger, logging.ERROR, "Trade log report failed", error=str(exc))
            return 1
        print(format_trade_log_report_summary(payload))
        print(f"Trade log report JSON written: {args.trade_log_report_json}")
        print(f"Trade log source: {payload.get('source_path')}")
        log_event(
            logger,
            logging.INFO,
            "Trade log report written",
            report_path=args.trade_log_report_json,
            source_path=payload.get("source_path"),
            row_count=payload.get("row_count"),
            enough_data_for_rule_changes=payload.get("enough_data_for_rule_changes"),
        )
        return 0

    if args.learn_from_trades:
        research_dd = load_research_max_dd_pct()
        try:
            trade_log_store = trade_log_store_from_settings(settings)
            result = run_learn_from_trades(
                trade_log_path=settings.trade_log_jsonl,
                digest_path=args.learn_digest_json,
                proposals_path=args.learn_proposals_md,
                research_max_dd_pct=research_dd,
                trade_log_store=trade_log_store,
                send_discord=bool(args.learn_discord),
                discord_webhook_url=settings.discord_webhook_url or "",
                discord_dry_run=bool(settings.dry_run),
            )
        except (OSError, TradeLogStoreError, ConfigError, RuntimeError) as exc:
            print(f"Learner error: {exc}")
            log_event(logger, logging.ERROR, "Learner failed", error=str(exc))
            return 1
        digest = result["digest"]
        print(format_learner_summary(digest, result["proposals"]))
        print(f"Learner digest JSON written: {args.learn_digest_json}")
        print(f"Learner proposals written: {args.learn_proposals_md}")
        print(f"Learner source: {digest.get('source_path')}")
        if args.learn_discord:
            print("Learner Discord summary requested (--learn-discord).")
        log_event(
            logger,
            logging.INFO,
            "Learner digest written",
            digest_path=args.learn_digest_json,
            proposals_path=args.learn_proposals_md,
            source_path=digest.get("source_path"),
            row_count=digest.get("row_count"),
            enough_data=digest.get("enough_data"),
            learn_discord=bool(args.learn_discord),
        )
        return 0

    is_research = any(research_flags)
    webhook_err = _missing_live_webhook_message(settings)
    if webhook_err and not is_research:
        print(f"Config error: {webhook_err}")
        log_event(logger, logging.ERROR, "Live Discord webhook missing", error=webhook_err)
        return 1
    if args.market_hours_only and not is_research:
        skip_reason = cron_skip_reason()
        if skip_reason is not None:
            # Lunch blackout is a scan policy (session still open); market-closed covers the rest.
            if "lunch" in skip_reason:
                print(f"Skipped: {skip_reason}.")
                usage_reason = "lunch blackout"
                log_label = "Lunch blackout skip"
            else:
                print(f"Skipped: US equity market is closed ({skip_reason}).")
                usage_reason = "market closed"
                log_label = "Market hours skip"
            print(format_cli_usage_line(reason=usage_reason))
            log_event(logger, logging.INFO, log_label, reason=skip_reason, alpaca_api_calls=0)
            return 0

    long_research = bool(
        args.strategy_eval
        or args.small_grid
        or args.checklist_compare
        or args.etf_backtest
        or args.etf_sealed_oos
    )
    research_window = ResearchWindow() if long_research else None
    try:
        candle_kwargs: dict[str, int] = {}
        if research_window is not None:
            candle_kwargs = {
                "daily_lookback_days": research_window.daily_lookback_days,
                "max_daily_bars": research_window.max_daily_bars,
                "hourly_lookback_days": research_window.hourly_lookback_days,
                "max_hourly_bars": research_window.max_hourly_bars,
            }
        candles = load_candles(
            ticker=settings.qqq_ticker,
            api_key=settings.alpaca_api_key,
            api_secret=settings.alpaca_api_secret,
            data_base_url=settings.alpaca_data_base_url,
            feed=settings.alpaca_data_feed,
            **candle_kwargs,
        )
        last_daily = candles.daily.index[-1]
        last_h4 = candles.four_hour.index[-1]
        daily_fetch_label = last_daily.isoformat() if hasattr(last_daily, "isoformat") else str(last_daily)
        h4_fetch_label = last_h4.isoformat() if hasattr(last_h4, "isoformat") else str(last_h4)
        log_event(
            logger,
            logging.INFO,
            "Data fetch succeeded",
            qqq_ticker=settings.qqq_ticker,
            latest_daily_candle=daily_fetch_label,
            latest_h4_candle=h4_fetch_label,
            alpaca_api_calls=cli_calls_attempted(),
        )
        _log_cli_usage(settings, logger)
    except MarketDataQuotaError as exc:
        print(f"Data error: {exc}")
        _log_cli_usage(settings, logger, quota_error_detail=str(exc))
        log_event(
            logger,
            logging.ERROR,
            "Data fetch failed",
            error=str(exc),
            quota_exhausted=True,
            alpaca_api_calls=cli_calls_attempted(),
        )
        try:
            maybe_notify_quota_reached(
                webhook_url=settings.discord_webhook_url,
                dry_run=settings.dry_run,
                state_path=Path("logs/api_quota_notified.json"),
                detail=str(exc),
                logger=logger,
            )
        except Exception as notify_exc:  # noqa: BLE001
            print(f"API quota Discord notice failed: {notify_exc}")
            log_event(logger, logging.ERROR, "API quota Discord notice failed", error=str(notify_exc))
        return 1
    except DataError as exc:
        print(f"Data error: {exc}")
        _log_cli_usage(settings, logger)
        log_event(
            logger,
            logging.ERROR,
            "Data fetch failed",
            error=str(exc),
            alpaca_api_calls=cli_calls_attempted(),
        )
        return 1
    except Exception as exc:  # noqa: BLE001
        print(f"Unexpected data/indicator failure: {exc}")
        log_event(logger, logging.ERROR, "Unexpected data failure", error=str(exc))
        return 1

    now_utc = datetime.now(timezone.utc)
    blocked_dates, risk_notes = load_merged_blackout_dates(
        settings.events_json,
        risk_avoidance=settings.event_risk_avoidance,
        risk_calendar_url=settings.event_risk_calendar_url or None,
    )
    for msg in risk_notes:
        print(f"[events] {msg}")
    log_event(
        logger,
        logging.INFO,
        "Event risk evaluated",
        risk_avoidance=settings.event_risk_avoidance,
        blocked_dates_count=len(blocked_dates),
        risk_notes=risk_notes,
    )
    if args.debug_strategy and not is_research:
        print(
            "[debug-strategy] Ignored unless combined with --backtest, --backtest-sweep, "
            "--walk-forward, --strategy-eval, --small-grid, or --checklist-compare."
        )

    research_decide_options = DecideOptions(
        debug_sanity_dominate=args.debug_strategy_sanity,
        high_confidence_only=args.high_confidence_only,
    )

    if args.etf_backtest or args.etf_sealed_oos:
        window = research_window or ResearchWindow()
        try:
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
            etf_kwargs = dict(
                strategy_params=strategy_params,
                blocked_dates=blocked_dates,
                anchor_date=settings.anchor_date or None,
                requested_bars=args.backtest_bars,
                expand_default=True,
                cost_bps=float(settings.alpaca_paper_limit_offset_bps),
                decide_options=research_decide_options,
            )
            if args.etf_sealed_oos:
                if SEAL_PATH.exists():
                    print(
                        f"Sealed ETF window already recorded at {SEAL_PATH}. "
                        "Refusing a second look."
                    )
                    return 1
                evaluation = evaluate_sealed(candles, tqqq_daily, sqqq_daily, **etf_kwargs)
                print(format_phase_report(evaluation))
                write_seal(SEAL_PATH, evaluation)
                print(f"Sealed look recorded: {SEAL_PATH}")
            else:
                evaluation = evaluate_development(candles, tqqq_daily, sqqq_daily, **etf_kwargs)
                print(format_phase_report(evaluation))
        except (ValueError, DataError) as exc:
            print(f"ETF backtest error: {exc}")
            return 1
        return 0

    if args.walk_forward:
        try:
            wf_grid = walk_forward_grid_from_settings(settings)
            wf_folds = walk_forward_fold_count()
            n_wf = wf_grid.combination_count
            if n_wf > 200:
                print(f"Warning: walk-forward grid has {n_wf} combinations - expect a long run.")
            if args.debug_strategy:
                print("[debug-strategy] Walk-forward: verbose diagnostics for the first fold of the first grid combination only.")
            wf_rows = run_walk_forward(
                candles,
                anchor_date=settings.anchor_date or None,
                blocked_dates=blocked_dates,
                base_params=strategy_params,
                bars=args.backtest_bars,
                grid=wf_grid,
                n_folds=wf_folds,
                debug_strategy=args.debug_strategy,
                decide_options=research_decide_options,
                entry_slippage_bps=settings.backtest_entry_slippage_bps,
                exit_slippage_bps=settings.backtest_exit_slippage_bps,
            )
        except ConfigError as exc:
            print(f"Walk-forward config error: {exc}")
            return 1
        except ValueError as exc:
            print(f"Walk-forward error: {exc}")
            return 1
        print(
            format_walk_forward_report(
                wf_rows,
                ticker=settings.qqq_ticker,
                bars=args.backtest_bars,
                combo_count=n_wf,
                n_folds=wf_folds,
            )
        )
        try:
            export_walk_forward_csv(args.walk_forward_csv, wf_rows)
            print(f"Walk-forward CSV written: {args.walk_forward_csv}")
        except OSError as exc:
            print(f"Walk-forward export error: {exc}")
            return 1
        return 0

    if args.strategy_eval or args.small_grid or args.checklist_compare:
        eval_bars = resolve_research_bars(
            args.backtest_bars,
            len(candles.daily),
            expand_default=True,
        )
        data_window = {
            "daily_bars_loaded": len(candles.daily),
            "daily_start": candles.daily.index[0].isoformat() if len(candles.daily) else None,
            "daily_end": candles.daily.index[-1].isoformat() if len(candles.daily) else None,
            "four_hour_bars_loaded": len(candles.four_hour),
            "bars_requested": eval_bars,
            "cli_backtest_bars": args.backtest_bars,
            "lookback_days_requested": research_window.daily_lookback_days if research_window else None,
            "note": (
                "QQQ directional proxy. Live loads stay on the shorter default window. "
                "Older dates may use a daily-derived 4h fallback when hourly history is shorter. "
                "Stretch take-profit is not an exit."
            ),
        }
        if args.checklist_compare:
            try:
                compare_payload = compare_checklist_variants(
                    candles,
                    anchor_date=settings.anchor_date or None,
                    blocked_dates=blocked_dates,
                    strategy_params=strategy_params,
                    bars=eval_bars,
                    decide_options=research_decide_options,
                    data_window=data_window,
                    entry_slippage_bps=settings.backtest_entry_slippage_bps,
                    exit_slippage_bps=settings.backtest_exit_slippage_bps,
                )
            except ValueError as exc:
                print(f"Checklist compare error: {exc}")
                return 1
            print(format_checklist_compare_report(compare_payload))
            try:
                write_json(args.checklist_compare_json, compare_payload)
                print(f"Checklist compare JSON written: {args.checklist_compare_json}")
            except OSError as exc:
                print(f"Checklist compare export error: {exc}")
                return 1
            if not args.strategy_eval and not args.small_grid:
                return 0
        plateau_rows = None
        if args.small_grid:
            try:
                grid = small_research_grid(settings)
                print(
                    f"Small grid: {grid.combination_count} combinations on {eval_bars} bars "
                    f"({data_window['daily_start']} -> {data_window['daily_end']}, "
                    f"{data_window['daily_bars_loaded']} daily bars loaded)."
                )
                plateau_rows = run_parameter_sweep(
                    candles,
                    anchor_date=settings.anchor_date or None,
                    blocked_dates=blocked_dates,
                    base_params=strategy_params,
                    bars=eval_bars,
                    grid=grid,
                    debug_strategy=args.debug_strategy,
                    decide_options=research_decide_options,
                    entry_slippage_bps=settings.backtest_entry_slippage_bps,
                    exit_slippage_bps=settings.backtest_exit_slippage_bps,
                )
            except ValueError as exc:
                print(f"Small grid error: {exc}")
                return 1
            grid_payload = grid_neighbors_payload(
                plateau_rows,
                ticker=settings.qqq_ticker,
                bars=eval_bars,
                data_window=data_window,
            )
            share = grid_payload.get("profitable_neighbor_share")
            share_txt = "n/a" if share is None else f"{float(share) * 100:.1f}%"
            print(
                f"Small grid neighbor share around best row: {share_txt} "
                f"({grid_payload.get('profitable_neighbors')}/{grid_payload.get('neighbor_count')})."
            )
            try:
                write_json(args.small_grid_json, grid_payload)
                print(f"Small grid JSON written: {args.small_grid_json}")
            except OSError as exc:
                print(f"Small grid export error: {exc}")
                return 1
            if not args.strategy_eval:
                return 0
        try:
            eval_payload = run_strategy_evaluation(
                candles,
                anchor_date=settings.anchor_date or None,
                blocked_dates=blocked_dates,
                strategy_params=strategy_params,
                bars=eval_bars,
                decide_options=research_decide_options,
                plateau_rows=plateau_rows,
                data_window=data_window,
                entry_slippage_bps=settings.backtest_entry_slippage_bps,
                exit_slippage_bps=settings.backtest_exit_slippage_bps,
            )
        except ValueError as exc:
            print(f"Strategy eval error: {exc}")
