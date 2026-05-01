from __future__ import annotations

import argparse
from datetime import datetime, timezone

from alerts import format_alert_message, send_discord
from backtest import format_backtest_report, run_backtest
from config import ConfigError, load_settings
from data import DataError, load_candles
from indicators import build_snapshot
from journal import append_journal
from strategy import (
    PositionState,
    RunTechnicalMeta,
    decide,
    format_technical_breakdown,
    load_blocked_dates,
    load_position,
    save_position,
)


def _format_utc_z(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


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
        "--no-technical",
        action="store_true",
        help="Skip the detailed technical breakdown block after the alert summary.",
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

    if args.set_position is not None and args.entry_price is not None and args.entry_price <= 0:
        print("Config error: --entry-price must be positive when provided.")
        return 1

    try:
        settings = load_settings()
        if args.dry_run:
            settings = settings.__class__(**{**settings.__dict__, "dry_run": True})
    except ConfigError as exc:
        print(f"Config error: {exc}")
        return 1

    if args.flat:
        save_position(settings.position_state_json, PositionState())
        print("Position reset: flat (no active TQQQ/SQQQ in bot memory).")
    elif args.set_position is not None:
        entry_price_f = float(args.entry_price) if args.entry_price is not None else None
        entry_ts_str: str | None = None
        if args.entry_time:
            try:
                entry_ts_str = _format_utc_z(_parse_entry_time_arg(args.entry_time))
            except ValueError:
                print("Config error: --entry-time must be valid ISO8601 (e.g. 2026-05-01T16:00:00Z).")
                return 1
        save_position(
            settings.position_state_json,
            PositionState(active_symbol=args.set_position, entry_price=entry_price_f, entry_timestamp=entry_ts_str),
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

    try:
        candles = load_candles(
            ticker=settings.qqq_ticker,
            api_key=settings.klickanalytics_api_key,
            cli_command=settings.klickanalytics_cli_command,
        )
    except DataError as exc:
        print(f"Data error: {exc}")
        return 1
    except Exception as exc:  # noqa: BLE001
        print(f"Unexpected data/indicator failure: {exc}")
        return 1

    now_utc = datetime.now(timezone.utc)
    blocked_dates = load_blocked_dates(settings.events_json)
    if args.backtest:
        try:
            bt = run_backtest(
                candles,
                anchor_date=settings.anchor_date or None,
                blocked_dates=blocked_dates,
                bull_entry_threshold=settings.bull_entry_threshold,
                bear_entry_threshold=settings.bear_entry_threshold,
                weak_threshold=settings.weak_score_threshold,
                stop_loss_pct=settings.stop_loss_pct,
                take_profit_pct=settings.take_profit_pct,
                stretch_take_profit_pct=settings.stretch_take_profit_pct,
                max_hold_days=settings.max_hold_trading_days,
                entry_atr_multiplier=settings.entry_atr_multiplier,
                bars=args.backtest_bars,
            )
        except ValueError as exc:
            print(f"Backtest error: {exc}")
            return 1
        print(format_backtest_report(bt, settings.qqq_ticker))
        return 0

    try:
        snapshot = build_snapshot(candles.daily, candles.four_hour, settings.anchor_date or None)
    except Exception as exc:  # noqa: BLE001
        print(f"Unexpected indicator failure: {exc}")
        return 1

    position = load_position(settings.position_state_json)
    alert, new_position = decide(
        snapshot=snapshot,
        position=position,
        blocked_dates=blocked_dates,
        now_utc=now_utc,
        bull_entry_threshold=settings.bull_entry_threshold,
        bear_entry_threshold=settings.bear_entry_threshold,
        weak_threshold=settings.weak_score_threshold,
        stop_loss_pct=settings.stop_loss_pct,
        take_profit_pct=settings.take_profit_pct,
        stretch_take_profit_pct=settings.stretch_take_profit_pct,
        max_hold_days=settings.max_hold_trading_days,
        entry_atr_multiplier=settings.entry_atr_multiplier,
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
        bull_entry_threshold=settings.bull_entry_threshold,
        bear_entry_threshold=settings.bear_entry_threshold,
        weak_threshold=settings.weak_score_threshold,
        stop_loss_pct=settings.stop_loss_pct,
        take_profit_pct=settings.take_profit_pct,
        stretch_take_profit_pct=settings.stretch_take_profit_pct,
        max_hold_days=settings.max_hold_trading_days,
        entry_atr_multiplier=settings.entry_atr_multiplier,
        anchor_date_label=anchor_label,
    )

    message = format_alert_message(alert)
    print(message)

    if not args.no_technical:
        print()
        print(format_technical_breakdown(snapshot, alert, position, tech_meta, today_iso))

    try:
        append_journal(settings.journal_csv, alert)
        save_position(settings.position_state_json, new_position)
        send_discord(
            settings.discord_webhook_url,
            alert,
            settings.dry_run,
            snapshot=snapshot,
            position_before=position,
            technical_meta=tech_meta,
            today_iso=today_iso,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"Output error: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
