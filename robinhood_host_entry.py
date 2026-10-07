"""Atomic daily entry reservation for Robinhood host-mediated pilot."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any, Protocol

from alpaca_live_entry_reservation import (
    PILOT_ENTRY_SLOT,
    ReservationResult,
    _is_unique_violation,
    trading_day_america_new_york,
)


class RhEntryReservationStore(Protocol):
    def reserve(
        self,
        *,
        trading_day: date,
        entry_slot: int = PILOT_ENTRY_SLOT,
        meta: dict[str, Any] | None = None,
    ) -> ReservationResult: ...


@dataclass
class InMemoryRhEntryReservationStore:
    bot_id: str = "default"
    _keys: set[tuple[str, date, int]] | None = None
    rows: list[dict[str, Any]] | None = None
    fail_next: str | None = None

    def __post_init__(self) -> None:
        if self._keys is None:
            self._keys = set()
        if self.rows is None:
            self.rows = []

    def reserve(
        self,
        *,
        trading_day: date,
        entry_slot: int = PILOT_ENTRY_SLOT,
        meta: dict[str, Any] | None = None,
    ) -> ReservationResult:
        assert self._keys is not None and self.rows is not None
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


class AtomicRhEntryReservationStore:
    TABLE = "bot_robinhood_entry_reservations"

    def __init__(self, client: Any | None = None, *, bot_id: str = "default") -> None:
        self._client = client
        self.bot_id = bot_id

    def _ensure_client(self) -> Any:
        if self._client is not None:
            return self._client
        from supabase_client import create_supabase_client

        self._client = create_supabase_client()
        return self._client

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


def reserve_rh_daily_entry(
    store: RhEntryReservationStore,
    *,
    now: datetime,
    meta: dict[str, Any] | None = None,
    entry_slot: int = PILOT_ENTRY_SLOT,
) -> ReservationResult:
    return store.reserve(
        trading_day=trading_day_america_new_york(now),
        entry_slot=entry_slot,
        meta=meta,
    )
