from __future__ import annotations

import json

import requests

from strategy import AlertDecision


def format_alert_message(alert: AlertDecision) -> str:
    return (
        f"Alert: {alert.alert_type}\n"
        f"Symbol: {alert.symbol}\n"
        f"QQQ trend: {alert.qqq_trend_reason}\n"
        f"Bullish score: {alert.bullish_score}/8\n"
        f"Bearish score: {alert.bearish_score}/8\n"
        f"Confidence: {alert.confidence_score}%\n"
        f"Entry zone: {alert.entry_zone_low:.2f} - {alert.entry_zone_high:.2f}\n"
        f"Stop loss: {alert.stop_loss:.2f}\n"
        f"Take profit: {alert.take_profit:.2f}\n"
        f"Stretch target: {alert.stretch_take_profit:.2f}\n"
        f"Max hold date: {alert.max_hold_date or 'N/A'}\n"
        f"Timestamp: {alert.timestamp}\n"
        f"Notes: {alert.notes}"
    )


def send_discord(webhook_url: str, message: str, dry_run: bool) -> None:
    if dry_run:
        print("[DRY RUN] Discord payload:")
        print(json.dumps({"content": message}, indent=2))
        return
    if not webhook_url:
        print("[Live] No DISCORD_WEBHOOK_URL - skipping Discord (journal + console only).")
        return
    response = requests.post(webhook_url, json={"content": message}, timeout=15)
    if response.status_code >= 400:
        raise RuntimeError(f"Discord webhook failed: {response.status_code} {response.text}")
