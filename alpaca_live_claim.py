"""Atomic client_order_id claims for Alpaca Live.

Production concurrency authority is Postgres UNIQUE(client_order_id) via
Supabase insert. Insert wins; unique violation → BLOCK. No check-then-insert.

Local JSON (LiveShadowClaimStore) remains for offline shadow rehearsal only.
Pilot submission must use AtomicLiveClaimStore (or an injected equivalent).
Supabase unavailable → BLOCK new exposure.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol


@dataclass(frozen=True)
class ClaimResult:
    claimed: bool
    reason: str


class ClaimStore(Protocol):
    def claim(self, client_order_id: str, *, meta: dict[str, Any] | None = None) -> ClaimResult: ...

    def known_ids(self) -> set[str]: ...


class LiveShadowClaimStore:
    """Append-only unique claim log keyed by client_order_id (shadow / tests only)."""

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


class InMemoryAtomicClaimStore:
    """Process-local stand-in that mimics UNIQUE(client_order_id) for tests."""

    def __init__(self) -> None:
        self._ids: set[str] = set()
        self.rows: list[dict[str, Any]] = []
        self.fail_next: str | None = None

    def known_ids(self) -> set[str]:
        return set(self._ids)

    def claim(self, client_order_id: str, *, meta: dict[str, Any] | None = None) -> ClaimResult:
        text = (client_order_id or "").strip()
        if not text:
            return ClaimResult(False, "empty client_order_id")
        if self.fail_next:
            reason = self.fail_next
            self.fail_next = None
            return ClaimResult(False, reason)
        if text in self._ids:
            return ClaimResult(False, "client_order_id already claimed (unique violation)")
        self._ids.add(text)
        row: dict[str, Any] = {
            "client_order_id": text,
            "claimed_at": datetime.now(timezone.utc).isoformat(),
            "execution_status": "CLAIMED",
            "execution_mode": (meta or {}).get("execution_mode", "live_pilot"),
            "broker": "alpaca",
        }
        if meta:
            row.update(meta)
        self.rows.append(row)
        return ClaimResult(True, "claimed")


def _is_unique_violation(exc: BaseException) -> bool:
    text = str(exc).lower()
    code = getattr(exc, "code", None) or getattr(getattr(exc, "args", [None])[0], "get", lambda *_: None)("code")
    if code == "23505":
        return True
    markers = (
        "duplicate key",
        "unique constraint",
        "unique_violation",
        "23505",
        "already exists",
    )
    return any(marker in text for marker in markers)


class AtomicLiveClaimStore:
    """Supabase/Postgres insert-claim. Unique violation → BLOCK. Errors → BLOCK."""

    TABLE = "bot_live_order_claims"

    def __init__(
        self,
        client: Any | None = None,
        *,
        bot_id: str = "default",
        execution_mode: str = "live_pilot",
    ) -> None:
        self._client = client
        self.bot_id = bot_id
        self.execution_mode = execution_mode
        self._known: set[str] = set()

    def known_ids(self) -> set[str]:
        return set(self._known)

    def _ensure_client(self) -> Any:
        if self._client is not None:
            return self._client
        try:
            from supabase_client import create_supabase_client
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"Supabase client unavailable: {exc}") from exc
        self._client = create_supabase_client()
        return self._client

    def claim(self, client_order_id: str, *, meta: dict[str, Any] | None = None) -> ClaimResult:
        text = (client_order_id or "").strip()
        if not text:
            return ClaimResult(False, "empty client_order_id")
        row: dict[str, Any] = {
            "client_order_id": text,
            "bot_id": self.bot_id,
            "broker": "alpaca",
            "execution_mode": self.execution_mode,
            "execution_status": "CLAIMED",
            "action": (meta or {}).get("action"),
            "execution_symbol": (meta or {}).get("symbol") or (meta or {}).get("execution_symbol"),
            "purpose": (meta or {}).get("purpose"),
            "meta": meta or {},
        }
        try:
            client = self._ensure_client()
            client.table(self.TABLE).insert(row).execute()
        except Exception as exc:  # noqa: BLE001
            if _is_unique_violation(exc):
                return ClaimResult(False, "client_order_id already claimed (unique violation)")
            return ClaimResult(False, f"claim store unavailable; new exposure blocked ({exc})")
        self._known.add(text)
        return ClaimResult(True, "claimed")


def claim_store_for_pilot(
    env: dict[str, str] | None = None,
    *,
    injected: ClaimStore | None = None,
) -> ClaimStore:
    """Pilot always needs an atomic store. Missing Supabase → caller must BLOCK."""
    if injected is not None:
        return injected
    import os

    source = os.environ if env is None else env
    url = (source.get("SUPABASE_URL") or source.get("NEXT_PUBLIC_SUPABASE_URL") or "").strip()
    key = (source.get("SUPABASE_SERVICE_ROLE_KEY") or "").strip()
    if not url or not key:
        raise RuntimeError(
            "Supabase is required for alpaca_live_pilot claims. "
            "Local JSON is not a production concurrency authority."
        )
    return AtomicLiveClaimStore(execution_mode="live_pilot")
