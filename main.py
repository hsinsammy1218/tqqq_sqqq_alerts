from __future__ import annotations

import argparse
from datetime import datetime, timezone

from alerts import format_alert_message, send_discord
from config import ConfigError, load_settings
from data import DataError, load_candles
from indicators import build_snapshot
from journal import append_journal
from strategy import decide, load_blocked_dates, load_position, save_position


def run() -> int:
    parser = argparse.ArgumentParser(description="QQQ-driven TQQQ/SQQQ alert-only system")
    parser.add_argument("--dry-run", action="store_true", help="Print payloads without sending Discord alerts.")
    args = parser.parse_args()

    try:
        settings = load_settings()
        if args.dry_run:
            settings = settings.__class__(**{**settings.__dict__, "dry_run": True})
    except ConfigError as exc:
        print(f"Config error: {exc}")
        return 1

    try:
        candles = load_candles(
            ticker=settings.qqq_ticker,
            api_key=settings.klickanalytics_api_key,
            cli_command=settings.klickanalytics_cli_command,
        )
        snapshot = build_snapshot(candles.daily, candles.four_hour, settings.anchor_date or None)
    except DataError as exc:
        print(f"Data error: {exc}")
        return 1
    except Exception as exc:  # noqa: BLE001
        print(f"Unexpected data/indicator failure: {exc}")
        return 1

    now_utc = datetime.now(timezone.utc)
    blocked_dates = load_blocked_dates(settings.events_json)
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

    message = format_alert_message(alert)
    print(message)

    try:
        append_journal(settings.journal_csv, alert)
        save_position(settings.position_state_json, new_position)
        send_discord(settings.discord_webhook_url, message, settings.dry_run)
    except Exception as exc:  # noqa: BLE001
        print(f"Output error: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
