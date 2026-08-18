from __future__ import annotations

import json
import logging
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from api_quota_notify import maybe_notify_klickanalytics_quota_reached
from config import ConfigError
from data import KlickAnalyticsQuotaError, cli_calls_attempted, format_cli_usage_line, load_candles
from klickanalytics_usage import track_and_maybe_warn_cli_usage
from position_store import PositionStoreError, position_store_from_settings
from runtime_logging import format_utc_z, log_event
from strategy import PositionState
from strategy_position import position_state_to_dict


def _position_to_dict(state: PositionState) -> dict[str, Any]:
    return position_state_to_dict(state)


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


def run_health_check(settings: Any, dry_run: bool, logger: logging.Logger) -> int:
    failures: list[str] = []
    run_ts = format_utc_z(datetime.now(timezone.utc))
    log_event(logger, logging.INFO, "Health check started", run_timestamp=run_ts, dry_run=dry_run)

    ok, msg = _check_cli_available(settings.klickanalytics_cli_command)
    print(f"[health] {'PASS' if ok else 'FAIL'} - {msg}")
    log_event(logger, logging.INFO if ok else logging.ERROR, "KlickAnalytics CLI check", ok=ok, detail=msg)
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
        log_event(
            logger,
            logging.INFO,
            "Data fetch health check",
            ok=True,
            qqq_ticker=settings.qqq_ticker,
            latest_daily_candle=d_label,
            latest_h4_candle=h4_label,
            klickanalytics_cli_calls=cli_calls_attempted(),
        )
        snapshot = track_and_maybe_warn_cli_usage(
            calls=cli_calls_attempted(),
            webhook_url=settings.discord_webhook_url,
            dry_run=dry_run,
            monthly_limit=settings.klickanalytics_monthly_limit,
            warn_pct=settings.klickanalytics_usage_warn_pct,
            logger=logger,
        )
        print(
            format_cli_usage_line(
                month_total=snapshot.total_calls if snapshot else None,
                month_limit=settings.klickanalytics_monthly_limit if snapshot else None,
            )
        )
    except KlickAnalyticsQuotaError as exc:
        msg = f"Data fetch failed: {exc}"
        print(f"[health] FAIL - {msg}")
        snapshot = track_and_maybe_warn_cli_usage(
            calls=cli_calls_attempted(),
            webhook_url=settings.discord_webhook_url,
            dry_run=dry_run,
            monthly_limit=settings.klickanalytics_monthly_limit,
            warn_pct=settings.klickanalytics_usage_warn_pct,
            logger=logger,
            quota_error_detail=str(exc),
        )
        print(
            format_cli_usage_line(
                month_total=snapshot.total_calls if snapshot else None,
                month_limit=settings.klickanalytics_monthly_limit if snapshot else None,
            )
        )
        log_event(
            logger,
            logging.ERROR,
            "Data fetch health check",
            ok=False,
            error=str(exc),
            quota_exhausted=True,
            klickanalytics_cli_calls=cli_calls_attempted(),
        )
        failures.append(msg)
        try:
            maybe_notify_klickanalytics_quota_reached(
                webhook_url=settings.discord_webhook_url,
                dry_run=dry_run,
                state_path=Path("logs/api_quota_notified.json"),
                detail=str(exc),
                logger=logger,
            )
        except Exception as notify_exc:  # noqa: BLE001
            print(f"[health] API quota Discord notice failed: {notify_exc}")
            log_event(logger, logging.ERROR, "API quota Discord notice failed", error=str(notify_exc))
    except Exception as exc:  # noqa: BLE001
        msg = f"Data fetch failed: {exc}"
        print(f"[health] FAIL - {msg}")
        snapshot = track_and_maybe_warn_cli_usage(
            calls=cli_calls_attempted(),
            webhook_url=settings.discord_webhook_url,
            dry_run=dry_run,
            monthly_limit=settings.klickanalytics_monthly_limit,
            warn_pct=settings.klickanalytics_usage_warn_pct,
            logger=logger,
        )
        print(
            format_cli_usage_line(
                month_total=snapshot.total_calls if snapshot else None,
                month_limit=settings.klickanalytics_monthly_limit if snapshot else None,
            )
        )
        log_event(logger, logging.ERROR, "Data fetch health check", ok=False, error=str(exc), klickanalytics_cli_calls=cli_calls_attempted())
        failures.append(msg)

    try:
        position_store = position_store_from_settings(settings)
        state, warnings = position_store.load()
        position_label = position_store.describe()
    except (ConfigError, PositionStoreError) as exc:
        msg = f"Position store unavailable: {exc}"
        print(f"[health] FAIL - {msg}")
        log_event(logger, logging.ERROR, "Position state health check", ok=False, error=str(exc))
        failures.append(msg)
    else:
        if warnings:
            msg = f"Recoverable state issue(s): {'; '.join(warnings)}"
            print(f"[health] WARN - {msg}")
            log_event(
                logger,
                logging.WARNING,
                "Position state recovered with warnings",
                ok=True,
                warnings=warnings,
                position_state_path=position_label,
                recovered_state=_position_to_dict(state),
            )
        else:
            msg = f"Position state readable at {position_label}."
            print(f"[health] PASS - {msg}")
            log_event(
                logger,
                logging.INFO,
                "Position state health check",
                ok=True,
                position_state_path=position_label,
                recovered_state=_position_to_dict(state),
            )

    if settings.events_json.exists():
        try:
            raw = settings.events_json.read_text(encoding="utf-8")
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                print(f"[health] PASS - events file readable at {settings.events_json}.")
                log_event(
                    logger,
                    logging.INFO,
                    "Events file health check",
                    ok=True,
                    events_path=str(settings.events_json),
                )
            else:
                msg = f"events file must be a JSON object: {settings.events_json}"
                print(f"[health] FAIL - {msg}")
                log_event(
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
            log_event(
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
        log_event(
            logger,
            logging.INFO,
            "Events file health check",
            ok=True,
            events_path=str(settings.events_json),
            detail="optional file not present",
        )

    if dry_run:
        print("[health] PASS - Discord webhook not required in dry-run mode.")
        log_event(logger, logging.INFO, "Discord configuration health check", ok=True, dry_run=True)
    else:
        if settings.discord_webhook_url:
            print("[health] PASS - Discord webhook configured for live mode.")
            log_event(logger, logging.INFO, "Discord configuration health check", ok=True, dry_run=False)
        else:
            msg = "DISCORD_WEBHOOK_URL is required when not in dry-run mode."
            print(f"[health] FAIL - {msg}")
            log_event(logger, logging.ERROR, "Discord configuration health check", ok=False, dry_run=False)
            failures.append(msg)

    if failures:
        print(f"[health] Completed with {len(failures)} failure(s).")
        log_event(
            logger,
            logging.ERROR,
            "Health check completed with failures",
            failure_count=len(failures),
            failures=failures,
        )
        return 1

    print("[health] All checks passed.")
    log_event(logger, logging.INFO, "Health check passed", failure_count=0)
    return 0
