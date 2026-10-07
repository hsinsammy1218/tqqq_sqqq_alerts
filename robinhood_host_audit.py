"""Audit + Discord helpers for Robinhood host-mediated pilot."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import requests

from robinhood_audit import redact


class HostAuditLog:
    """Append-only JSONL. Does not force NOT_SUBMITTED (host path may advance)."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def append(self, row: dict[str, Any]) -> None:
        safe = redact(row)
        safe["broker"] = "robinhood"
        safe.setdefault("execution_mode", "host_mediated")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(safe, sort_keys=True) + "\n")

    def read(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        rows: list[dict[str, Any]] = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                rows.append(json.loads(line))
        return rows


def post_host_discord(
    webhook_url: str,
    text: str,
    *,
    dry_run: bool,
    title: str,
    banner: str,
    color: int = 0xB45309,
) -> None:
    payload = redact(
        {
            "username": "QQQ Swing Alerts",
            "content": banner,
            "embeds": [
                {
                    "title": title,
                    "description": text[:3900],
                    "color": color,
                }
            ],
        }
    )
    if dry_run or not webhook_url:
        return
    try:
        response = requests.post(webhook_url, json=payload, timeout=15)
        if getattr(response, "status_code", 0) >= 400:
            print(f"[robinhood-host] Discord failed: {response.status_code}")
    except requests.RequestException:
        print("[robinhood-host] Discord request failed")
