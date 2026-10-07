"""Atomic daily entry reservation for Alpaca live pilot.

Production authority is Postgres UNIQUE(bot_id, trading_day, entry_slot) via
Supabase insert. Insert wins; unique violation → BLOCK. No check-then-insert.
No local JSON fallback for real money. Supabase unavailable → BLOCK.

Trading day = America/New_York calendar date of the evaluation clock.
Pilot uses entry_slot=1. Crash after reserve: do NOT auto-free.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any, Protocol

from market_hours import US_EASTERN

PILOT_ENTRY_SLOT = 1


@dataclass(frozen=True)
class ReservationResult:
    reserved: bool
    reason: str
    trading_day: date | None = None
    entry_slot: int | None = None


class EntryReservationStore(Protocol):
    def reserve(
        self,
        *,
        trading_day: date,
        entry_slot: int = PILOT_ENTRY_SLOT,
        meta: dict[str, Any] | None = None,
    ) -> ReservationResult: ...

    def has_reservation(self, *, trading_day: date, entry_slot: int = PILOT_ENTRY_SLOT) -> bool: ...


def trading_day_america_new_york(now: datetime | None = None) -> date:
    """US market-day key: calendar date in America/New_York.

    Weekend/holiday dates still get distinct keys so a Friday reservation cannot
    be reused across a UTC midnight boundary. Cron already skips closed sessions.
    """
    clock = now or datetime.now(timezone.utc)
    if clock.tzinfo is None:
        clock = clock.replace(tzinfo=timezone.utc)
    return clock.astimezone(US_EASTERN).date()


def _is_unique_violation(exc: BaseException) -> bool:
    text = str(exc).lower()
    code = getattr(exc, "code", None)
    if code == "23505":
        return True
    # supabase/postgrest often wraps codes in the message
    markers = (
        "duplicate key",
        "unique constraint",
        "unique_violation",
        "23505",
        "already exists",
    )
    return any(marker in text for marker in markers)


class InMemoryEntryReservationStore:
    """Process-local stand-in that mimics UNIQUE(bot_id, trading_day, entry_slot)."""

    def __init__(self, *, bot_id: str = "default") -> None:
        self.bot_id = bot_id
        self._keys: set[tuple[str, date, int]] = set()
        self.rows: list[dict[str, Any]] = []
        self.fail_next: str | None = None

    def has_reservation(self, *, trading_day: date, entry_slot: int = PILOT_ENTRY_SLOT) -> bool:
        return (self.bot_id, trading_day, entry_slot) in self._keys

    def reserve(
        self,
        *,
        trading_day: date,
        entry_slot: int = PILOT_ENTRY_SLOT,
        meta: dict[str, Any] | None = None,
    ) -> ReservationResult:
        if entry_slot < 1:
            return ReservationResult(False, "entry_slot must be >= 1", trading_day, entry_slot)
        if self.fail_next:
            reason = self.fail_next
            self.fail_next = None
            return ReservationResult(False, reason, trading_day, entry_slot)
        key = (self.bot_id, trading_day, entry_slot)
        if key in self._keys:
            return ReservationResult(
                False,
                "daily entry slot already reserved (unique violation)",
                trading_day,
                entry_slot,
            )
        self._keys.add(key)
        row: dict[str, Any] = {
            "bot_id": self.bot_id,
            "trading_day": trading_day.isoformat(),
            "entry_slot": entry_slot,
            "reserved_at": datetime.now(timezone.utc).isoformat(),
        }
        if meta:
            row.update(meta)
        self.rows.append(row)
        return ReservationResult(True, "reserved", trading_day, entry_slot)


class AtomicEntryReservationStore:
    """Supabase/Postgres insert-reserve. Unique violation → BLOCK. Errors → BLOCK."""

    TABLE = "bot_live_entry_reservations"

    def __init__(
        self,
        client: Any | None = None,
        *,
        bot_id: str = "default",
    ) -> None:
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

    def has_reservation(self, *, trading_day: date, entry_slot: int = PILOT_ENTRY_SLOT) -> bool:
        try:
            client = self._ensure_client()
            result = (
                client.table(self.TABLE)
                .select("entry_slot")
                .eq("bot_id", self.bot_id)
                .eq("trading_day", trading_day.isoformat())
                .eq("entry_slot", entry_slot)
                .limit(1)
                .execute()
            )
        except Exception:  # noqa: BLE001
            return False
        rows = result.data if hasattr(result, "data") else result
        return bool(rows)

    def reserve(
        self,
        *,
        trading_day: date,
        entry_slot: int = PILOT_ENTRY_SLOT,
        meta: dict[str, Any] | None = None,
    ) -> ReservationResult:
        if entry_slot < 1:
            return ReservationResult(False, "entry_slot must be >= 1", trading_day, entry_slot)
        row: dict[str, Any] = {
            "bot_id": self.bot_id,
            "trading_day": trading_day.isoformat(),
            "entry_slot": entry_slot,
            "client_order_id": (meta or {}).get("client_order_id"),
            "purpose": (meta or {}).get("purpose"),
            "execution_symbol": (meta or {}).get("symbol")
            or (meta or {}).get("execution_symbol"),
            "signal_id": (meta or {}).get("signal_id"),
            "meta": meta or {},
        }
        try:
            client = self._ensure_client()
            client.table(self.TABLE).insert(row).execute()
        except Exception as exc:  # noqa: BLE001
            if _is_unique_violation(exc):
                return ReservationResult(
                    False,
                    "daily entry slot already reserved (unique violation)",
                    trading_day,
                    entry_slot,
                )
            return ReservationResult(
                False,
                f"entry reservation store unavailable; new exposure blocked ({exc})",
                trading_day,
                entry_slot,
            )
        return ReservationResult(True, "reserved", trading_day, entry_slot)


def reservation_store_for_pilot(
    env: dict[str, str] | None = None,
    *,
    injected: EntryReservationStore | None = None,
) -> EntryReservationStore:
    """Pilot always needs an atomic reservation store. Missing Supabase → raise."""
    if injected is not None:
        return injected
    import os

    source = os.environ if env is None else env
    url = (source.get("SUPABASE_URL") or source.get("NEXT_PUBLIC_SUPABASE_URL") or "").strip()
    key = (source.get("SUPABASE_SERVICE_ROLE_KEY") or "").strip()
    if not url or not key:
        raise RuntimeError(
            "Supabase is required for alpaca_live_pilot daily entry reservations. "
            "Local JSON is not a production concurrency authority."
        )
    return AtomicEntryReservationStore()


def reserve_daily_entry(
    store: EntryReservationStore,
    *,
    now: datetime,
    meta: dict[str, Any] | None = None,
    entry_slot: int = PILOT_ENTRY_SLOT,
) -> ReservationResult:
    day = trading_day_america_new_york(now)
    return store.reserve(trading_day=day, entry_slot=entry_slot, meta=meta)
