"""Durable intent store + atomic claim for Robinhood host-mediated pilot.

Production authority is Supabase/Postgres. Insert PENDING wins on unique
client_order_id. Claim is UPDATE status PENDING→CLAIMED (compare-and-swap).
No OAuth tokens. Local/in-memory stores are for tests and offline rehearsal.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol

from robinhood_intent import DurableTradeIntent, INTENT_STATUSES, intent_from_row


def _is_unique_violation(exc: BaseException) -> bool:
    text = str(exc).lower()
    code = getattr(exc, "code", None)
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


@dataclass(frozen=True)
class StoreResult:
    ok: bool
    reason: str
    row: dict[str, Any] | None = None


class IntentStore(Protocol):
    def create_pending(self, durable: DurableTradeIntent, *, meta: dict[str, Any] | None = None) -> StoreResult: ...

    def claim_next_pending(
        self,
        *,
        claimed_by: str,
        now: datetime | None = None,
    ) -> StoreResult: ...

    def claim_by_client_order_id(
        self,
        client_order_id: str,
        *,
        claimed_by: str,
        now: datetime | None = None,
    ) -> StoreResult: ...

    def update_status(
        self,
        client_order_id: str,
        *,
        from_statuses: frozenset[str] | set[str],
        to_status: str,
        fields: dict[str, Any] | None = None,
    ) -> StoreResult: ...

    def get(self, client_order_id: str) -> dict[str, Any] | None: ...


@dataclass
class InMemoryIntentStore:
    """Process-local stand-in for tests."""

    bot_id: str = "default"
    rows: dict[str, dict[str, Any]] = field(default_factory=dict)
    fail_next: str | None = None

    def create_pending(self, durable: DurableTradeIntent, *, meta: dict[str, Any] | None = None) -> StoreResult:
        if self.fail_next:
            reason = self.fail_next
            self.fail_next = None
            return StoreResult(False, reason)
        row = durable.as_row(bot_id=self.bot_id, meta=meta)
        cid = row["client_order_id"]
        if cid in self.rows:
            return StoreResult(False, "client_order_id already exists (unique violation)")
        row["id"] = str(uuid.uuid4())
        row["created_at"] = datetime.now(timezone.utc).isoformat()
        row["updated_at"] = row["created_at"]
        self.rows[cid] = row
        return StoreResult(True, "created", dict(row))

    def _claim_row(self, row: dict[str, Any], *, claimed_by: str, now: datetime) -> StoreResult:
        if row["status"] != "PENDING":
            return StoreResult(False, f"status is {row['status']}, not PENDING")
        expires = datetime.fromisoformat(str(row["expires_at"]).replace("Z", "+00:00"))
        if now >= expires.astimezone(timezone.utc):
            row["status"] = "EXPIRED_INTENT"
            row["terminal_at"] = now.isoformat()
            row["updated_at"] = now.isoformat()
            row["detail"] = "expired before claim"
            return StoreResult(False, "intent expired before claim", dict(row))
        try:
            intent_from_row(row)
        except ValueError as exc:
            row["status"] = "BLOCKED"
            row["detail"] = str(exc)
            row["updated_at"] = now.isoformat()
            return StoreResult(False, str(exc), dict(row))
        row["status"] = "CLAIMED"
        row["claimed_at"] = now.isoformat()
        row["claimed_by"] = claimed_by
        row["updated_at"] = now.isoformat()
        return StoreResult(True, "claimed", dict(row))

    def claim_next_pending(
        self,
        *,
        claimed_by: str,
        now: datetime | None = None,
    ) -> StoreResult:
        clock = now or datetime.now(timezone.utc)
        if clock.tzinfo is None:
            clock = clock.replace(tzinfo=timezone.utc)
        pending = [
            row
            for row in self.rows.values()
            if row.get("status") == "PENDING" and row.get("bot_id") == self.bot_id
        ]
        pending.sort(key=lambda r: str(r.get("created_at") or ""))
        if not pending:
            return StoreResult(False, "no pending intents")
        return self._claim_row(pending[0], claimed_by=claimed_by, now=clock)

    def claim_by_client_order_id(
        self,
        client_order_id: str,
        *,
        claimed_by: str,
        now: datetime | None = None,
    ) -> StoreResult:
        clock = now or datetime.now(timezone.utc)
        if clock.tzinfo is None:
            clock = clock.replace(tzinfo=timezone.utc)
        row = self.rows.get((client_order_id or "").strip())
        if row is None:
            return StoreResult(False, "intent not found")
        return self._claim_row(row, claimed_by=claimed_by, now=clock)

    def update_status(
        self,
        client_order_id: str,
        *,
        from_statuses: frozenset[str] | set[str],
        to_status: str,
        fields: dict[str, Any] | None = None,
    ) -> StoreResult:
        if to_status not in INTENT_STATUSES:
            return StoreResult(False, f"unsupported status {to_status!r}")
        row = self.rows.get((client_order_id or "").strip())
        if row is None:
            return StoreResult(False, "intent not found")
        if row["status"] not in from_statuses:
            return StoreResult(False, f"status is {row['status']}, expected one of {sorted(from_statuses)}")
        row["status"] = to_status
        row["updated_at"] = datetime.now(timezone.utc).isoformat()
        if fields:
            row.update(fields)
        if to_status in {
            "FILLED",
            "PARTIALLY_FILLED",
            "REJECTED",
            "CANCELLED",
            "EXPIRED",
            "UNKNOWN",
            "BLOCKED",
            "EXPIRED_INTENT",
            "SUPERSEDED",
        }:
            row.setdefault("terminal_at", row["updated_at"])
        return StoreResult(True, "updated", dict(row))

    def get(self, client_order_id: str) -> dict[str, Any] | None:
        row = self.rows.get((client_order_id or "").strip())
        return dict(row) if row else None


class AtomicIntentStore:
    """Supabase-backed durable intents."""

    TABLE = "bot_robinhood_execution_intents"

    def __init__(self, client: Any | None = None, *, bot_id: str = "default") -> None:
        self._client = client
        self.bot_id = bot_id

    def _ensure_client(self) -> Any:
        if self._client is not None:
            return self._client
        try:
            from supabase_client import create_supabase_client
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"Supabase client unavailable: {exc}") from exc
        self._client = create_supabase_client()
        return self._client

    def create_pending(self, durable: DurableTradeIntent, *, meta: dict[str, Any] | None = None) -> StoreResult:
        row = durable.as_row(bot_id=self.bot_id, meta=meta)
        try:
            client = self._ensure_client()
            result = client.table(self.TABLE).insert(row).execute()
        except Exception as exc:  # noqa: BLE001
            if _is_unique_violation(exc):
                return StoreResult(False, "client_order_id already exists (unique violation)")
            return StoreResult(False, f"intent store unavailable ({exc})")
        data = getattr(result, "data", None) or []
        created = data[0] if data else row
        return StoreResult(True, "created", created if isinstance(created, dict) else row)

    def claim_by_client_order_id(
        self,
        client_order_id: str,
        *,
        claimed_by: str,
        now: datetime | None = None,
    ) -> StoreResult:
        clock = now or datetime.now(timezone.utc)
        if clock.tzinfo is None:
            clock = clock.replace(tzinfo=timezone.utc)
        text = (client_order_id or "").strip()
        if not text:
            return StoreResult(False, "empty client_order_id")
        patch = {
            "status": "CLAIMED",
            "claimed_at": clock.isoformat(),
            "claimed_by": claimed_by,
            "updated_at": clock.isoformat(),
        }
        try:
            client = self._ensure_client()
            result = (
                client.table(self.TABLE)
                .update(patch)
                .eq("client_order_id", text)
                .eq("bot_id", self.bot_id)
                .eq("status", "PENDING")
                .gt("expires_at", clock.isoformat())
                .execute()
            )
        except Exception as exc:  # noqa: BLE001
            return StoreResult(False, f"claim unavailable ({exc})")
        data = getattr(result, "data", None) or []
        if not data:
            return StoreResult(False, "claim lost race or intent not pending/unexpired")
        row = data[0]
        try:
            intent_from_row(row)
        except ValueError as exc:
            self.update_status(
                text,
                from_statuses={"CLAIMED"},
                to_status="BLOCKED",
                fields={"detail": str(exc)},
            )
            return StoreResult(False, str(exc), row if isinstance(row, dict) else None)
        return StoreResult(True, "claimed", row if isinstance(row, dict) else None)

    def claim_next_pending(
        self,
        *,
        claimed_by: str,
        now: datetime | None = None,
    ) -> StoreResult:
        clock = now or datetime.now(timezone.utc)
        if clock.tzinfo is None:
            clock = clock.replace(tzinfo=timezone.utc)
        try:
            client = self._ensure_client()
            listed = (
                client.table(self.TABLE)
                .select("*")
                .eq("bot_id", self.bot_id)
                .eq("status", "PENDING")
                .gt("expires_at", clock.isoformat())
                .order("created_at")
                .limit(1)
                .execute()
            )
        except Exception as exc:  # noqa: BLE001
            return StoreResult(False, f"list pending unavailable ({exc})")
        data = getattr(listed, "data", None) or []
        if not data:
            return StoreResult(False, "no pending intents")
        cid = str(data[0].get("client_order_id") or "")
        return self.claim_by_client_order_id(cid, claimed_by=claimed_by, now=clock)

    def update_status(
        self,
        client_order_id: str,
        *,
        from_statuses: frozenset[str] | set[str],
        to_status: str,
        fields: dict[str, Any] | None = None,
    ) -> StoreResult:
        if to_status not in INTENT_STATUSES:
            return StoreResult(False, f"unsupported status {to_status!r}")
        text = (client_order_id or "").strip()
        patch: dict[str, Any] = {
            "status": to_status,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        if fields:
            patch.update(fields)
        if to_status in {
            "FILLED",
            "PARTIALLY_FILLED",
            "REJECTED",
            "CANCELLED",
            "EXPIRED",
            "UNKNOWN",
            "BLOCKED",
            "EXPIRED_INTENT",
            "SUPERSEDED",
        }:
            patch.setdefault("terminal_at", patch["updated_at"])
        try:
            client = self._ensure_client()
            query = (
                client.table(self.TABLE)
                .update(patch)
                .eq("client_order_id", text)
                .eq("bot_id", self.bot_id)
                .in_("status", list(from_statuses))
            )
            result = query.execute()
        except Exception as exc:  # noqa: BLE001
            return StoreResult(False, f"update unavailable ({exc})")
        data = getattr(result, "data", None) or []
        if not data:
            return StoreResult(False, "status transition lost race")
        return StoreResult(True, "updated", data[0] if isinstance(data[0], dict) else None)

    def get(self, client_order_id: str) -> dict[str, Any] | None:
        try:
            client = self._ensure_client()
            result = (
                client.table(self.TABLE)
                .select("*")
                .eq("client_order_id", (client_order_id or "").strip())
                .eq("bot_id", self.bot_id)
                .limit(1)
                .execute()
            )
        except Exception:  # noqa: BLE001
            return None
        data = getattr(result, "data", None) or []
        return data[0] if data and isinstance(data[0], dict) else None


def intent_store_from_env(
    env: dict[str, str] | None = None,
    *,
    injected: IntentStore | None = None,
) -> IntentStore:
    if injected is not None:
        return injected
    import os

    source = os.environ if env is None else env
    url = (source.get("SUPABASE_URL") or source.get("NEXT_PUBLIC_SUPABASE_URL") or "").strip()
    key = (source.get("SUPABASE_SERVICE_ROLE_KEY") or "").strip()
    if not url or not key:
        raise RuntimeError(
            "Supabase is required for robinhood host-mediated intents. "
            "Local memory is not a production concurrency authority."
        )
    return AtomicIntentStore()
