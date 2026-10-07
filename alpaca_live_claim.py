"""Atomic client_order_id claims for Alpaca Live Shadow.

Claims are local and optional Supabase-shaped. They never submit an order.
Crash recovery is broker-wins: an open broker order blocks; a claim without a
broker order stays NOT_SUBMITTED.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class ClaimResult:
    claimed: bool
    reason: str


class LiveShadowClaimStore:
    """Append-only unique claim log keyed by client_order_id."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def known_ids(self) -> set[str]:
        ids: set[str] = set()
        if not self.path.exists():
            return ids
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            client_id = row.get("client_order_id")
            if isinstance(client_id, str) and client_id:
                ids.add(client_id)
        return ids

    def claim(self, client_order_id: str, *, meta: dict[str, Any] | None = None) -> ClaimResult:
        text = (client_order_id or "").strip()
        if not text:
            return ClaimResult(False, "empty client_order_id")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if text in self.known_ids():
            return ClaimResult(False, "client_order_id already claimed")
        row: dict[str, Any] = {
            "client_order_id": text,
            "claimed_at": datetime.now(timezone.utc).isoformat(),
            "execution_status": "NOT_SUBMITTED",
            "execution_mode": "live_shadow",
            "broker": "alpaca",
        }
        if meta:
            row.update(meta)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, sort_keys=True) + "\n")
            handle.flush()
        return ClaimResult(True, "claimed")
