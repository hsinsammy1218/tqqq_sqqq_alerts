"""One-shot: detect Alpaca market-data rate/quota failures and post Discord notice (live)."""
from __future__ import annotations

import argparse
import logging
from datetime import datetime, timezone
from pathlib import Path

from alerts import send_discord_api_quota_alert
from api_quota_notify import maybe_notify_quota_reached
from config import ConfigError, load_settings
from data import MarketDataQuotaError, load_candles
from runtime_logging import format_utc_z

logging.basicConfig(level=logging.INFO)


def main() -> int:
    parser = argparse.ArgumentParser(description="Send Discord notice when Alpaca market-data quota/rate limit is hit.")
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

    try:
        load_candles(
            settings.qqq_ticker,
            api_key=settings.alpaca_api_key,
            api_secret=settings.alpaca_api_secret,
            data_base_url=settings.alpaca_data_base_url,
            feed=settings.alpaca_data_feed,
        )
        print("Alpaca fetch succeeded — rate/quota limit is not currently blocking.")
        return 0
    except MarketDataQuotaError as exc:
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

        sent = maybe_notify_quota_reached(
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
