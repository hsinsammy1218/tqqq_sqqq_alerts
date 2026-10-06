"""Append-only shadow audit. A shadow row is never a live broker order."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

_SECRET_KEY = re.compile(
    r"(token|secret|password|cookie|authorization|api[_-]?key|session)",
    re.IGNORECASE,
)


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        cleaned: dict[str, Any] = {}
        for key, item in value.items():
            if _SECRET_KEY.search(str(key)):
                cleaned[str(key)] = "[redacted]"
            else:
                cleaned[str(key)] = redact(item)
        return cleaned
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, str) and _SECRET_KEY.search(value) and "=" in value:
        return "[redacted]"
    return value


class ShadowAuditLog:
    def __init__(self, path: Path) -> None:
        self.path = path

    def append(self, row: dict[str, Any]) -> None:
        safe = redact(row)
        safe["broker"] = "robinhood"
        safe["execution_mode"] = "shadow"
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
