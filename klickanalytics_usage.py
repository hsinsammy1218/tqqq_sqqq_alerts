from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from alerts import parse_api_quota_detail, send_discord_api_usage_warning
from runtime_logging import format_utc_z, log_event

USAGE_PATH = Path("logs/klickanalytics_monthly_usage.json")
WARN_NOTIFIED_PATH = Path("logs/api_usage_warn_notified.json")


@dataclass(frozen=True)
class MonthlyUsageSnapshot:
    month: str
    total_calls: int
    monthly_limit: int
    warn_threshold: int
    remaining: int


def _month_key() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m")


def _warn_threshold(monthly_limit: int, warn_pct: int) -> int:
    if monthly_limit <= 0 or warn_pct <= 0:
        return 0
    return max(1, int(round(monthly_limit * warn_pct / 100)))


def _load_usage(path: Path) -> dict[str, object]:
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _load_warn_notified_month(path: Path) -> str | None:
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    month = raw.get("notified_month")
    return month if isinstance(month, str) and month else None


def _save_warn_notified_month(path: Path, month: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "notified_month": month,
        "notified_at": format_utc_z(datetime.now(timezone.utc)),
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def record_monthly_cli_usage(
    calls: int,
    *,
    monthly_limit: int,
    warn_pct: int,
    usage_path: Path | None = None,
    server_total: int | None = None,
) -> MonthlyUsageSnapshot | None:
    if monthly_limit <= 0:
        return None

    path = usage_path if usage_path is not None else USAGE_PATH
    month = _month_key()
    state = _load_usage(path)
    if state.get("month") != month:
        total = 0
    else:
        raw_total = state.get("total_calls", 0)
        total = int(raw_total) if isinstance(raw_total, int) else 0

    total += max(0, calls)
    if server_total is not None:
        total = max(total, server_total)

    warn_at = _warn_threshold(monthly_limit, warn_pct)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "month": month,
                "total_calls": total,
                "monthly_limit": monthly_limit,
                "warn_threshold": warn_at,
                "updated_at": format_utc_z(datetime.now(timezone.utc)),
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return MonthlyUsageSnapshot(
        month=month,
        total_calls=total,
        monthly_limit=monthly_limit,
        warn_threshold=warn_at,
        remaining=max(0, monthly_limit - total),
    )


def maybe_notify_klickanalytics_usage_warning(
    snapshot: MonthlyUsageSnapshot,
    *,
    webhook_url: str,
    dry_run: bool,
    warn_pct: int,
    logger: logging.Logger,
    warn_notified_path: Path | None = None,
) -> bool:
    if warn_pct <= 0:
        return False
    if snapshot.warn_threshold <= 0:
        return False
    if snapshot.total_calls < snapshot.warn_threshold:
        return False
    if snapshot.total_calls >= snapshot.monthly_limit:
        return False
    warn_path = warn_notified_path if warn_notified_path is not None else WARN_NOTIFIED_PATH
    if _load_warn_notified_month(warn_path) == snapshot.month:
        log_event(
            logger,
            logging.INFO,
            "KlickAnalytics usage warning skipped (already sent this month)",
            notified_month=snapshot.month,
        )
        return False

    timestamp = format_utc_z(datetime.now(timezone.utc))
    send_discord_api_usage_warning(
        webhook_url,
        total_calls=snapshot.total_calls,
        monthly_limit=snapshot.monthly_limit,
        warn_threshold=snapshot.warn_threshold,
        timestamp=timestamp,
        dry_run=dry_run,
    )
    if not dry_run and webhook_url:
        _save_warn_notified_month(warn_path, snapshot.month)

    log_event(
        logger,
        logging.INFO,
        "KlickAnalytics usage warning sent",
        notified_month=snapshot.month,
        total_calls=snapshot.total_calls,
        monthly_limit=snapshot.monthly_limit,
        warn_threshold=snapshot.warn_threshold,
        dry_run=dry_run,
        webhook_configured=bool(webhook_url),
    )
    return True


def track_and_maybe_warn_cli_usage(
    *,
    calls: int,
    webhook_url: str,
    dry_run: bool,
    monthly_limit: int,
    warn_pct: int,
    logger: logging.Logger,
    quota_error_detail: str | None = None,
) -> MonthlyUsageSnapshot | None:
    server_total: int | None = None
    if quota_error_detail:
        info = parse_api_quota_detail(quota_error_detail)
        hits = info.get("total_hits")
        if isinstance(hits, int):
            server_total = hits

    snapshot = record_monthly_cli_usage(
        calls,
        monthly_limit=monthly_limit,
        warn_pct=warn_pct,
        server_total=server_total,
    )
    if snapshot is None:
        return None

    maybe_notify_klickanalytics_usage_warning(
        snapshot,
        webhook_url=webhook_url,
        dry_run=dry_run,
        warn_pct=warn_pct,
        logger=logger,
    )
    return snapshot


def server_total_from_quota_detail(detail: str) -> int | None:
    hits = parse_api_quota_detail(detail).get("total_hits")
    return hits if isinstance(hits, int) else None
