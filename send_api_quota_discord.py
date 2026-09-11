"""One-shot: detect KlickAnalytics monthly limit and post Discord notice (live)."""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

from alerts import send_discord_api_quota_alert
from api_quota_notify import maybe_notify_klickanalytics_quota_reached
from config import ConfigError, load_settings
from data import KlickAnalyticsQuotaError, load_candles_from_settings
from runtime_logging import format_utc_z

logging.basicConfig(level=logging.INFO)


def main() -> int:
    parser = argparse.ArgumentParser(description="Send Discord notice when KlickAnalytics monthly limit is hit.")
    parser.add_argument(
        "--force",
        action="store_true",
        help="Send even if a notice was already posted this calendar month.",
    )
    args = parser.parse_args()

    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"Config error: {exc}")
        return 1

    if settings.market_data_provider != "klickanalytics":
        print(
            f"MARKET_DATA_PROVIDER={settings.market_data_provider} — "
            "KlickAnalytics quota probe skipped."
        )
        return 0

    try:
        load_candles_from_settings(settings)
        print("KlickAnalytics fetch succeeded — monthly limit is not currently hit.")
        return 0
    except KlickAnalyticsQuotaError as exc:
        detail = str(exc)
        print(detail)
        if not settings.discord_webhook_url:
            print("No DISCORD_WEBHOOK_URL configured — cannot send Discord notice.")
            return 1
        if args.force:
            timestamp = format_utc_z(datetime.now(timezone.utc))
            send_discord_api_quota_alert(
                settings.discord_webhook_url,
                detail=detail,
                timestamp=timestamp,
                dry_run=False,
            )
            print("Discord API quota notice sent (forced).")
            return 0

        sent = maybe_notify_klickanalytics_quota_reached(
            webhook_url=settings.discord_webhook_url,
            dry_run=False,
            state_path=Path("logs/api_quota_notified.json"),
            detail=detail,
            logger=logging.getLogger("api_quota"),
        )
        if sent:
            print("Discord API quota notice sent.")
        else:
            print("Discord API quota notice skipped (already sent this month). Use --force to resend.")
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"Unexpected error: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
