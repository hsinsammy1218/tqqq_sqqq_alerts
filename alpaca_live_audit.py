"""Append-only Alpaca live-shadow audit. Rows are never live broker orders."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from robinhood_audit import redact


class LiveShadowAuditLog:
    def __init__(self, path: Path) -> None:
        self.path = path

    def append(self, row: dict[str, Any]) -> None:
        safe = redact(row)
        safe["broker"] = "alpaca"
        safe["execution_mode"] = "live_shadow"
        safe["execution_status"] = "NOT_SUBMITTED"
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
