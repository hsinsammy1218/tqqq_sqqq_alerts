from __future__ import annotations

import argparse
import json
import logging
import subprocess
from datetime import datetime, timezone
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from typing import Any

from alerts import format_alert_message, send_discord
from backtest import export_backtest_trades_csv, format_backtest_report, run_backtest
from backtest_sweep import export_sweep_csv, format_sweep_report, run_parameter_sweep, sweep_grid_from_settings
from config import ConfigError, load_settings, strategy_params_from_settings
from data import DataError, load_candles
from event_calendar import load_merged_blackout_dates
from indicators import build_snapshot
from journal import append_journal
from strategy import (
    PositionState,
    RunTechnicalMeta,
    decide,
    format_technical_breakdown,
    load_position,
    save_position,
)


def _format_utc_z(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _setup_logger(level_name: str) -> logging.Logger:
    level = getattr(logging, level_name.upper(), logging.INFO)
    logs_dir = Path("logs")
    logs_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("tqqq_alert_bot")
    logger.setLevel(level)
    logger.propagate = False
    if logger.handlers:
        logger.handlers.clear()

    class JsonFormatter(logging.Formatter):
        def format(self, record: logging.LogRecord) -> str:  # noqa: D401
            payload: dict[str, Any] = {
                "timestamp": _format_utc_z(datetime.now(timezone.utc)),
                "level": record.levelname,
                "message": record.getMessage(),
            }
            extra = getattr(record, "event", None)
            if isinstance(extra, dict):
                payload.update(extra)
            return json.dumps(payload, ensure_ascii=True)

    file_handler = TimedRotatingFileHandler(
        filename=logs_dir / "bot.log",
        when="midnight",
        interval=1,
        backupCount=30,
        encoding="utf-8",
        utc=True,
    )
    file_handler.setFormatter(JsonFormatter())
    logger.addHandler(file_handler)
    return logger


def _log_event(logger: logging.Logger, level: int, message: str, **event: Any) -> None:
    logger.log(level, message, extra={"event": event})


def _position_to_dict(state: PositionState) -> dict[str, Any]:
    return {
        "symbol": state.active_symbol,
        "entry_price": state.entry_price,
        "entry_time": state.entry_timestamp,
        "last_signal": state.last_signal,
        "updated_at": state.updated_at,
    }


def _check_cli_available(cli_command: str) -> tuple[bool, str]:
    try:
        proc = subprocess.run(
            [cli_command, "--help"],
            capture_output=True,
            text=True,
            check=False,
            timeout=20,
            stdin=subprocess.DEVNULL,
        )
    except FileNotFoundError:
        return False, f"CLI '{cli_command}' not found."
    except subprocess.TimeoutExpired:
        return False, f"CLI '{cli_command}' timed out on --help."
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        return False, f"CLI '{cli_command}' returned {proc.returncode}: {detail or 'unknown error'}"
    return True, f"CLI '{cli_command}' is available."


def _run_health_check(settings: Any, dry_run: bool, logger: logging.Logger) -> int:
    failures: list[str] = []
    run_ts = _format_utc_z(datetime.now(timezone.utc))
    _log_event(logger, logging.INFO, "Health check started", run_timestamp=run_ts, dry_run=dry_run)

    ok, msg = _check_cli_available(settings.klickanalytics_cli_command)
    print(f"[health] {'PASS' if ok else 'FAIL'} - {msg}")
    _log_event(logger, logging.INFO if ok else logging.ERROR, "KlickAnalytics CLI check", ok=ok, detail=msg)
    if not ok:
        failures.append(msg)

    try:
        candles = load_candles(
            ticker=settings.qqq_ticker,
            api_key=settings.klickanalytics_api_key,
            cli_command=settings.klickanalytics_cli_command,
        )
        last_daily = candles.daily.index[-1]
        last_h4 = candles.four_hour.index[-1]
        d_label = last_daily.isoformat() if hasattr(last_daily, "isoformat") else str(last_daily)
        h4_label = last_h4.isoformat() if hasattr(last_h4, "isoformat") else str(last_h4)
        msg = f"Fetched data (daily={d_label}, h4={h4_label})."
        print(f"[health] PASS - {msg}")
        _log_event(
            logger,
            logging.INFO,
            "Data fetch health check",
            ok=True,
            qqq_ticker=settings.qqq_ticker,
            latest_daily_candle=d_label,
            latest_h4_candle=h4_label,
        )
    except Exception as exc:  # noqa: BLE001
        msg = f"Data fetch failed: {exc}"
        print(f"[health] FAIL - {msg}")
        _log_event(logger, logging.ERROR, "Data fetch health check", ok=False, error=str(exc))
        failures.append(msg)

    state, warnings = load_position(settings.position_state_json)
    if warnings:
        msg = f"Recoverable state issue(s): {'; '.join(warnings)}"
        print(f"[health] WARN - {msg}")
        _log_event(
            logger,
            logging.WARNING,
            "Position state recovered with warnings",
            ok=True,
            warnings=warnings,
            position_state_path=str(settings.position_state_json),
            recovered_state=_position_to_dict(state),
        )
    else:
        msg = f"Position state readable at {settings.position_state_json}."
        print(f"[health] PASS - {msg}")
        _log_event(
            logger,
            logging.INFO,
            "Position state health check",
            ok=True,
            position_state_path=str(settings.position_state_json),
            recovered_state=_position_to_dict(state),
        )

    if settings.events_json.exists():
        try:
            raw = settings.events_json.read_text(encoding="utf-8")
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                print(f"[health] PASS - events file readable at {settings.events_json}.")
                _log_event(
                    logger,
                    logging.INFO,
                    "Events file health check",
                    ok=True,
                    events_path=str(settings.events_json),
                )
            else:
                msg = f"events file must be a JSON object: {settings.events_json}"
                print(f"[health] FAIL - {msg}")
                _log_event(
                    logger,
                    logging.ERROR,
                    "Events file health check",
                    ok=False,
                    events_path=str(settings.events_json),
                    detail=msg,
                )
                failures.append(msg)
        except Exception as exc:  # noqa: BLE001
            msg = f"events file unreadable ({settings.events_json}): {exc}"
            print(f"[health] FAIL - {msg}")
            _log_event(
                logger,
                logging.ERROR,
                "Events file health check",
                ok=False,
                events_path=str(settings.events_json),
                error=str(exc),
            )
            failures.append(msg)
    else:
        print(f"[health] PASS - events file not present (optional): {settings.events_json}")
        _log_event(
            logger,
            logging.INFO,
            "Events file health check",
            ok=True,
            events_path=str(settings.events_json),
            detail="optional file not present",
        )

    if dry_run:
        print("[health] PASS - Discord webhook not required in dry-run mode.")
        _log_event(logger, logging.INFO, "Discord configuration health check", ok=True, dry_run=True)
    else:
        if settings.discord_webhook_url:
            print("[health] PASS - Discord webhook configured for live mode.")
            _log_event(logger, logging.INFO, "Discord configuration health check", ok=True, dry_run=False)
        else:
            msg = "DISCORD_WEBHOOK_URL is required when not in dry-run mode."
            print(f"[health] FAIL - {msg}")
            _log_event(logger, logging.ERROR, "Discord configuration health check", ok=False, dry_run=False)
            failures.append(msg)

    if failures:
        print(f"[health] Completed with {len(failures)} failure(s).")
        _log_event(
            logger,
            logging.ERROR,
            "Health check completed with failures",
            failure_count=len(failures),
            failures=failures,
        )
        return 1

    print("[health] All checks passed.")
    _log_event(logger, logging.INFO, "Health check passed", failure_count=0)
    return 0


def _parse_entry_time_arg(value: str) -> datetime:
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def run() -> int:
    parser = argparse.ArgumentParser(description="QQQ-driven TQQQ/SQQQ alert-only system")
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
        help="Run backtests over BACKTEST_SWEEP_* parameter grids (see README).",
    )
    parser.add_argument(
        "--backtest-sweep-csv",
        default=None,
        metavar="PATH",
        help="Optional path to write ranked sweep results CSV.",
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
    args = parser.parse_args()
    logger = _setup_logger(args.log_level)
    run_ts = _format_utc_z(datetime.now(timezone.utc))
    _log_event(
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
    )

    if args.set_position is not None and args.entry_price is not None and args.entry_price <= 0:
        print("Config error: --entry-price must be positive when provided.")
        _log_event(logger, logging.ERROR, "Invalid CLI args", error="entry-price must be positive")
        return 1

    try:
        settings = load_settings()
        if args.dry_run:
            settings = settings.__class__(**{**settings.__dict__, "dry_run": True})
        strategy_params = strategy_params_from_settings(settings)
        _log_event(
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
        _log_event(logger, logging.ERROR, "Config load failed", error=str(exc))
        return 1

    stamp = _format_utc_z(datetime.now(timezone.utc))
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
        _log_event(
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
                entry_ts_str = _format_utc_z(_parse_entry_time_arg(args.entry_time))
            except ValueError:
                print("Config error: --entry-time must be valid ISO8601 (e.g. 2026-05-01T16:00:00Z).")
                _log_event(logger, logging.ERROR, "Invalid entry time argument", entry_time=args.entry_time)
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
        _log_event(
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
        return _run_health_check(settings, settings.dry_run, logger)

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
        _log_event(
            logger,
            logging.INFO,
            "Data fetch succeeded",
            qqq_ticker=settings.qqq_ticker,
            latest_daily_candle=daily_fetch_label,
            latest_h4_candle=h4_fetch_label,
        )
    except DataError as exc:
        print(f"Data error: {exc}")
        _log_event(logger, logging.ERROR, "Data fetch failed", error=str(exc))
        return 1
    except Exception as exc:  # noqa: BLE001
        print(f"Unexpected data/indicator failure: {exc}")
        _log_event(logger, logging.ERROR, "Unexpected data failure", error=str(exc))
        return 1

    now_utc = datetime.now(timezone.utc)
    blocked_dates, risk_notes = load_merged_blackout_dates(
        settings.events_json,
        risk_avoidance=settings.event_risk_avoidance,
        risk_calendar_url=settings.event_risk_calendar_url or None,
    )
    for msg in risk_notes:
        print(f"[events] {msg}")
    _log_event(
        logger,
        logging.INFO,
        "Event risk evaluated",
        risk_avoidance=settings.event_risk_avoidance,
        blocked_dates_count=len(blocked_dates),
        risk_notes=risk_notes,
    )
    if args.backtest_sweep:
        try:
            grid = sweep_grid_from_settings(settings)
            n_combo = grid.combination_count
            if n_combo > 400:
                print(f"Warning: sweep has {n_combo} combinations — expect a long run.")
            sweep_rows = run_parameter_sweep(
                candles,
                anchor_date=settings.anchor_date or None,
                blocked_dates=blocked_dates,
                base_params=strategy_params,
                bars=args.backtest_bars,
                grid=grid,
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
        _log_event(logger, logging.ERROR, "Indicator build failed", error=str(exc))
        return 1

    position, position_warnings = load_position(settings.position_state_json)
    for msg in position_warnings:
        print(f"[position] {msg}")
    _log_event(
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
    )

    message = format_alert_message(alert)
    print(message)
    _log_event(
        logger,
        logging.INFO,
        "Signal decided",
        decision={
            "alert_type": alert.alert_type,
            "symbol": alert.symbol,
            "notes": alert.notes,
            "confidence": alert.confidence_score,
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
        _log_event(
            logger,
            logging.INFO,
            "Journal append succeeded",
            journal_path=str(settings.journal_csv),
            alert_type=alert.alert_type,
            symbol=alert.symbol,
        )
        save_position(settings.position_state_json, new_position)
        _log_event(
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
        _log_event(
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
        _log_event(logger, logging.ERROR, "Output stage failed", error=str(exc))
        return 1
    _log_event(logger, logging.INFO, "Run completed", exit_code=0)
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
