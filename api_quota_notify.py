from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from alerts import send_discord_api_quota_alert
from runtime_logging import format_utc_z, log_event


def _load_notified_month(path: Path) -> str | None:
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    month = raw.get("notified_month")
    return month if isinstance(month, str) and month else None


def _save_notified_month(path: Path, month: str, detail: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "notified_month": month,
        "notified_at": format_utc_z(datetime.now(timezone.utc)),
        "detail": detail[:2000],
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def maybe_notify_klickanalytics_quota_reached(
    *,
    webhook_url: str,
    dry_run: bool,
    state_path: Path,
    detail: str,
    logger: logging.Logger,
) -> bool:
    """Send at most one Discord notice per calendar month. Returns True if a notice was sent."""
    month_key = datetime.now(timezone.utc).strftime("%Y-%m")
    if _load_notified_month(state_path) == month_key:
        log_event(
            logger,
            logging.INFO,
            "KlickAnalytics quota Discord notice skipped (already sent this month)",
            notified_month=month_key,
            state_path=str(state_path),
        )
        return False

    timestamp = format_utc_z(datetime.now(timezone.utc))
    send_discord_api_quota_alert(
        webhook_url,
        detail=detail,
        timestamp=timestamp,
        dry_run=dry_run,
    )
    if not dry_run and webhook_url:
        _save_notified_month(state_path, month_key, detail)

    log_event(
        logger,
        logging.INFO,
        "KlickAnalytics quota Discord notice sent",
        notified_month=month_key,
        dry_run=dry_run,
        webhook_configured=bool(webhook_url),
        state_path=str(state_path),
    )
    return True
