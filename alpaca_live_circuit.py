"""Durable circuit breakers for Alpaca live pilot.

One new entry per day, kill switch for new entries (exits still allowed),
and durable daily/weekly/drawdown trip flags. Broker equity marks still
gate risk; this store survives process restarts.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime, timezone
from typing import Any, Protocol


@dataclass(frozen=True)
class CircuitState:
    bot_id: str
    day_key: date | None
    entries_today: int
    week_key: str | None
    kill_new_entries: bool
    daily_loss_tripped: bool
    weekly_loss_tripped: bool
    drawdown_tripped: bool
    detail: str = ""

    def blocks_new_entry(self, *, today: date, max_entries_per_day: int) -> str | None:
        if self.kill_new_entries:
            return "kill switch blocks new entries (exits still allowed)"
        if self.daily_loss_tripped:
            return "durable daily loss circuit is tripped"
        if self.weekly_loss_tripped:
            return "durable weekly loss circuit is tripped"
        if self.drawdown_tripped:
            return "durable drawdown circuit is tripped"
        count = self.entries_today if self.day_key == today else 0
        if count >= max_entries_per_day:
            return "one new entry per day already used (durable)"
        return None


class CircuitStore(Protocol):
    def load(self) -> CircuitState: ...

    def save(self, state: CircuitState) -> None: ...


class InMemoryCircuitStore:
    def __init__(self, state: CircuitState | None = None) -> None:
        self._state = state or CircuitState(
            bot_id="default",
            day_key=None,
            entries_today=0,
            week_key=None,
            kill_new_entries=False,
            daily_loss_tripped=False,
            weekly_loss_tripped=False,
            drawdown_tripped=False,
        )

    def load(self) -> CircuitState:
        return self._state

    def save(self, state: CircuitState) -> None:
        self._state = state


class SupabaseCircuitStore:
    TABLE = "bot_live_circuit_state"

    def __init__(self, client: Any | None = None, *, bot_id: str = "default") -> None:
        self._client = client
        self.bot_id = bot_id

    def _ensure_client(self) -> Any:
        if self._client is not None:
            return self._client
        from supabase_client import create_supabase_client

        self._client = create_supabase_client()
        return self._client

    def load(self) -> CircuitState:
        try:
            client = self._ensure_client()
            result = (
                client.table(self.TABLE)
                .select("*")
                .eq("bot_id", self.bot_id)
                .limit(1)
                .execute()
            )
        except Exception as exc:  # noqa: BLE001
            # Fail closed for new exposure: treat as kill.
            return CircuitState(
                bot_id=self.bot_id,
                day_key=None,
                entries_today=0,
                week_key=None,
                kill_new_entries=True,
                daily_loss_tripped=False,
                weekly_loss_tripped=False,
                drawdown_tripped=False,
                detail=f"circuit store unavailable: {exc}",
            )
        rows = result.data if hasattr(result, "data") else result
        if not rows:
            return CircuitState(
                bot_id=self.bot_id,
                day_key=None,
                entries_today=0,
                week_key=None,
                kill_new_entries=False,
                daily_loss_tripped=False,
                weekly_loss_tripped=False,
                drawdown_tripped=False,
            )
        row = rows[0]
        day_raw = row.get("day_key")
        day_key = None
        if isinstance(day_raw, date):
            day_key = day_raw
        elif isinstance(day_raw, str) and day_raw:
            day_key = date.fromisoformat(day_raw[:10])
        return CircuitState(
            bot_id=self.bot_id,
            day_key=day_key,
            entries_today=int(row.get("entries_today") or 0),
            week_key=str(row.get("week_key") or "") or None,
            kill_new_entries=bool(row.get("kill_new_entries")),
            daily_loss_tripped=bool(row.get("daily_loss_tripped")),
            weekly_loss_tripped=bool(row.get("weekly_loss_tripped")),
            drawdown_tripped=bool(row.get("drawdown_tripped")),
            detail=str(row.get("detail") or ""),
        )

    def save(self, state: CircuitState) -> None:
        row = {
            "bot_id": state.bot_id,
            "day_key": state.day_key.isoformat() if state.day_key else None,
            "entries_today": state.entries_today,
            "week_key": state.week_key,
            "kill_new_entries": state.kill_new_entries,
            "daily_loss_tripped": state.daily_loss_tripped,
            "weekly_loss_tripped": state.weekly_loss_tripped,
            "drawdown_tripped": state.drawdown_tripped,
            "detail": state.detail,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        try:
            client = self._ensure_client()
            client.table(self.TABLE).upsert(row).execute()
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"failed to persist circuit state: {exc}") from exc


def record_new_entry(store: CircuitStore, *, today: date, bot_id: str = "default") -> CircuitState:
    state = store.load()
    entries = state.entries_today if state.day_key == today else 0
    updated = replace(
        state,
        bot_id=bot_id,
        day_key=today,
        entries_today=entries + 1,
    )
    store.save(updated)
    return updated


def trip_from_risk_reason(store: CircuitStore, reason: str) -> CircuitState:
    state = store.load()
    lower = reason.lower()
    updated = state
    if "daily loss" in lower:
        updated = replace(updated, daily_loss_tripped=True, detail=reason)
    if "weekly loss" in lower:
        updated = replace(updated, weekly_loss_tripped=True, detail=reason)
    if "drawdown" in lower:
        updated = replace(updated, drawdown_tripped=True, detail=reason)
    if updated is not state:
        store.save(updated)
    return updated
