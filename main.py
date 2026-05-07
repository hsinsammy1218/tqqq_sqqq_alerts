from __future__ import annotations

import logging
from datetime import datetime, timezone

from alerts import format_alert_message, send_discord
from backtest import export_backtest_trades_csv, format_backtest_report, run_backtest
from backtest_sweep import export_sweep_csv, format_sweep_report, run_parameter_sweep, sweep_grid_from_settings
from cli_args import build_parser
from config import ConfigError, load_settings, strategy_params_from_settings
from data import DataError, load_candles
from event_calendar import load_merged_blackout_dates
from health_check import run_health_check
from indicators import build_snapshot
from journal import append_journal
from runtime_logging import format_utc_z, log_event, setup_logger
from walk_forward import (
    export_walk_forward_csv,
    format_walk_forward_report,
    run_walk_forward,
    walk_forward_fold_count,
    walk_forward_grid_from_settings,
)
from strategy import (
    DecideOptions,
    PositionState,
    RunTechnicalMeta,
    decide,
    format_technical_breakdown,
    load_position,
    save_position,
)


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


def run() -> int:
    parser = build_parser()
    args = parser.parse_args()
    if args.walk_forward and (args.backtest or args.backtest_sweep):
        print("Config error: --walk-forward cannot be combined with --backtest or --backtest-sweep.")
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
        debug_strategy=args.debug_strategy,
        debug_strategy_sanity=args.debug_strategy_sanity,
        high_confidence_only=args.high_confidence_only,
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
            position_state_json=str(settings.position_state_json),
            events_json=str(settings.events_json),
            dry_run=settings.dry_run,
            log_level=args.log_level,
        )
    except ConfigError as exc:
        print(f"Config error: {exc}")
        log_event(logger, logging.ERROR, "Config load failed", error=str(exc))
        return 1

    stamp = format_utc_z(datetime.now(timezone.utc))
    if args.flat:
        save_position(
            settings.position_state_json,
            PositionState(
                active_symbol=None,
                entry_price=None,
                entry_timestamp=None,
                last_signal="MANUAL_FLAT",
                updated_at=stamp,
            ),
        )
        print("Position reset: flat (no active TQQQ/SQQQ in bot memory).")
        log_event(
            logger,
            logging.INFO,
            "Manual flat applied",
            position_state_path=str(settings.position_state_json),
            position_after={
                "symbol": None,
                "entry_price": None,
                "entry_time": None,
                "last_signal": "MANUAL_FLAT",
                "updated_at": stamp,
            },
        )
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
        save_position(
            settings.position_state_json,
            PositionState(
                active_symbol=args.set_position,
                entry_price=entry_price_f,
                entry_timestamp=entry_ts_str,
                last_signal="MANUAL_SET",
                updated_at=stamp,
            ),
        )
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
            position_state_path=str(settings.position_state_json),
            position_after={
                "symbol": args.set_position,
                "entry_price": entry_price_f,
                "entry_time": entry_ts_str,
                "last_signal": "MANUAL_SET",
                "updated_at": stamp,
            },
        )

    if args.health_check:
        return run_health_check(settings, settings.dry_run, logger)

    try:
        candles = load_candles(
            ticker=settings.qqq_ticker,
            api_key=settings.klickanalytics_api_key,
            cli_command=settings.klickanalytics_cli_command,
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
        )
    except DataError as exc:
        print(f"Data error: {exc}")
        log_event(logger, logging.ERROR, "Data fetch failed", error=str(exc))
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
    if args.debug_strategy and not args.backtest and not args.backtest_sweep and not args.walk_forward:
        print("[debug-strategy] Ignored unless combined with --backtest, --backtest-sweep, or --walk-forward.")

    research_decide_options = DecideOptions(
        debug_sanity_dominate=args.debug_strategy_sanity,
        high_confidence_only=args.high_confidence_only,
    )

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

    if args.backtest_sweep:
        try:
            grid = sweep_grid_from_settings(settings)
            n_combo = grid.combination_count
            if n_combo > 400:
                print(f"Warning: sweep has {n_combo} combinations - expect a long run.")
            if args.debug_strategy:
                print("[debug-strategy] Sweep: verbose diagnostics for the first grid combination only.")
            sweep_rows = run_parameter_sweep(
                candles,
                anchor_date=settings.anchor_date or None,
                blocked_dates=blocked_dates,
                base_params=strategy_params,
                bars=args.backtest_bars,
                grid=grid,
                debug_strategy=args.debug_strategy,
                decide_options=research_decide_options,
            )
        except ConfigError as exc:
            print(f"Sweep config error: {exc}")
            return 1
        except ValueError as exc:
            print(f"Backtest error: {exc}")
            return 1
        print(
            format_sweep_report(
                sweep_rows,
                ticker=settings.qqq_ticker,
                bars=args.backtest_bars,
                combo_count=n_combo,
            )
        )
        if args.backtest_sweep_csv:
            try:
                export_sweep_csv(args.backtest_sweep_csv, sweep_rows)
                print(f"Backtest sweep CSV written: {args.backtest_sweep_csv}")
            except OSError as exc:
                print(f"Backtest sweep export error: {exc}")
                return 1
        return 0

    if args.backtest:
        try:
            bt = run_backtest(
                candles,
                anchor_date=settings.anchor_date or None,
                blocked_dates=blocked_dates,
                strategy_params=strategy_params,
                bars=args.backtest_bars,
                debug_strategy=args.debug_strategy,
                decide_options=research_decide_options,
            )
        except ValueError as exc:
            print(f"Backtest error: {exc}")
            return 1
        print(format_backtest_report(bt, settings.qqq_ticker))
        if args.backtest_report_csv:
            try:
                export_backtest_trades_csv(args.backtest_report_csv, bt.trade_rows)
                print(f"Backtest trade CSV written: {args.backtest_report_csv}")
            except OSError as exc:
                print(f"Backtest report export error: {exc}")
                return 1
        return 0

    try:
        snapshot = build_snapshot(candles.daily, candles.four_hour, settings.anchor_date or None)
    except Exception as exc:  # noqa: BLE001
        print(f"Unexpected indicator failure: {exc}")
        log_event(logger, logging.ERROR, "Indicator build failed", error=str(exc))
        return 1

    position, position_warnings = load_position(settings.position_state_json)
    for msg in position_warnings:
        print(f"[position] {msg}")
    log_event(
        logger,
        logging.WARNING if position_warnings else logging.INFO,
        "Position state loaded",
        position_state_path=str(settings.position_state_json),
        warnings=position_warnings,
        position_before=_position_to_dict(position),
    )
    alert, new_position, dbg = decide(
        snapshot=snapshot,
        position=position,
        blocked_dates=blocked_dates,
        now_utc=now_utc,
        params=strategy_params,
        decide_options=research_decide_options,
    )

    last_daily = candles.daily.index[-1]
    last_h4 = candles.four_hour.index[-1]
    daily_label = last_daily.isoformat() if hasattr(last_daily, "isoformat") else str(last_daily)
    h4_label = last_h4.isoformat() if hasattr(last_h4, "isoformat") else str(last_h4)
    year = getattr(last_daily, "year", None)
    anchor_label = settings.anchor_date if settings.anchor_date else f"January 1 ({year})" if year else "January 1 (year of last daily bar)"
    today_iso = now_utc.date().isoformat()
    tech_meta = RunTechnicalMeta(
        qqq_ticker=settings.qqq_ticker,
        run_utc_iso=now_utc.replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        daily_bar_end=daily_label,
        h4_bar_end=h4_label,
        blocked_today=today_iso in blocked_dates,
        regime=dbg.regime,
        base_bull_entry_threshold=settings.bull_entry_threshold,
        base_bear_entry_threshold=settings.bear_entry_threshold,
        base_weak_threshold=settings.weak_score_threshold,
        effective_bull_entry=dbg.effective_bull_entry,
        effective_bear_entry=dbg.effective_bear_entry,
        effective_weak=dbg.effective_weak,
        score_weights=strategy_params.score_weights,
        weighted_bull=dbg.weighted_bull,
        weighted_bear=dbg.weighted_bear,
        stop_loss_pct=settings.stop_loss_pct,
        take_profit_pct=settings.take_profit_pct,
        stretch_take_profit_pct=settings.stretch_take_profit_pct,
        max_hold_days=settings.max_hold_trading_days,
        entry_atr_multiplier=settings.entry_atr_multiplier,
        anchor_date_label=anchor_label,
        flip_in_range_regime=strategy_params.flip_in_range_regime,
        min_confidence_to_trade=strategy_params.min_confidence_to_trade,
        high_confidence_only=args.high_confidence_only,
    )

    message = format_alert_message(alert)
    print(message)
    log_event(
        logger,
        logging.INFO,
        "Signal decided",
        decision={
            "alert_type": alert.alert_type,
            "symbol": alert.symbol,
            "notes": alert.notes,
            "confidence": alert.confidence_score,
            "signal_quality": alert.signal_quality,
            "bull_score": alert.bullish_score,
            "bear_score": alert.bearish_score,
            "timestamp": alert.timestamp,
        },
        position_before=_position_to_dict(position),
        position_after=_position_to_dict(new_position),
        latest_daily_candle=daily_label,
        latest_h4_candle=h4_label,
    )

    if not args.no_technical:
        print()
        print(format_technical_breakdown(snapshot, alert, position, tech_meta, today_iso))

    try:
        append_journal(settings.journal_csv, alert)
        log_event(
            logger,
            logging.INFO,
            "Journal append succeeded",
            journal_path=str(settings.journal_csv),
            alert_type=alert.alert_type,
            symbol=alert.symbol,
        )
        save_position(settings.position_state_json, new_position)
        log_event(
            logger,
            logging.INFO,
            "Position state saved",
            position_state_path=str(settings.position_state_json),
            position_after=_position_to_dict(new_position),
        )
        send_discord(
            settings.discord_webhook_url,
            alert,
            settings.dry_run,
            snapshot=snapshot,
            position_before=position,
            technical_meta=tech_meta,
            today_iso=today_iso,
        )
        log_event(
            logger,
            logging.INFO,
            "Discord send completed",
            dry_run=settings.dry_run,
            webhook_configured=bool(settings.discord_webhook_url),
            alert_type=alert.alert_type,
            symbol=alert.symbol,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"Output error: {exc}")
        log_event(logger, logging.ERROR, "Output stage failed", error=str(exc))
        return 1
    log_event(logger, logging.INFO, "Run completed", exit_code=0)
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
