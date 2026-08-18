"""Position memory backends: local JSON file or Supabase row."""

from __future__ import annotations

from typing import Any, Protocol

from config import ConfigError, Settings
from strategy_position import (
    load_position,
    position_state_from_dict,
    position_state_to_dict,
    save_position,
)
from strategy_types import PositionState
from supabase_client import create_supabase_client

TABLE = "bot_position_state"


class PositionStoreError(Exception):
    """Hard failure loading or saving position (do not start flat on cloud errors)."""


class PositionStore(Protocol):
    def describe(self) -> str: ...

    def load(self) -> tuple[PositionState, list[str]]: ...

    def save(self, position: PositionState) -> None: ...


class FilePositionStore:
    def __init__(self, path: Any) -> None:
        self._path = path

    def describe(self) -> str:
        return str(self._path)

    def load(self) -> tuple[PositionState, list[str]]:
        return load_position(self._path)

    def save(self, position: PositionState) -> None:
        try:
            save_position(self._path, position)
        except OSError as exc:
            raise PositionStoreError(f"Failed to save position to {self._path}: {exc}") from exc


def parse_supabase_position_payload(data: object) -> tuple[PositionState, list[str]]:
    """Parse a PostgREST select payload.

    An empty list means no row yet (start flat). Any other unexpected shape, or a
    row whose symbol is invalid, is a hard error — do not start flat.
    """
    if data is None:
        raise PositionStoreError("Supabase position load returned no data payload.")
    if not isinstance(data, list):
        raise PositionStoreError(
            f"Supabase position load returned unexpected payload type: {type(data).__name__}."
        )
    if not data:
        return PositionState(), []
    row = data[0]
    if not isinstance(row, dict):
        raise PositionStoreError("Supabase position row was not an object.")
    state, warnings = position_state_from_dict(row)
    fatal = [w for w in warnings if "Starting flat" in w]
    if fatal:
        raise PositionStoreError(
            "Refusing to start flat on invalid Supabase position row: " + "; ".join(fatal)
        )
    return state, warnings


class SupabasePositionStore:
    def __init__(self, bot_id: str) -> None:
        self._bot_id = bot_id
        self._client = create_supabase_client()

    def describe(self) -> str:
        return f"supabase:{TABLE}:{self._bot_id}"

    def load(self) -> tuple[PositionState, list[str]]:
        try:
            result = (
                self._client.table(TABLE)
                .select("symbol,entry_price,entry_time,last_signal,updated_at")
                .eq("bot_id", self._bot_id)
                .limit(1)
                .execute()
            )
        except Exception as exc:  # noqa: BLE001
            raise PositionStoreError(f"Failed to load position from Supabase: {exc}") from exc
        return parse_supabase_position_payload(result.data)

    def save(self, position: PositionState) -> None:
        payload = position_state_to_dict(position)
        row = {
            "bot_id": self._bot_id,
            "symbol": payload["symbol"],
            "entry_price": payload["entry_price"],
            "entry_time": payload["entry_time"],
            "last_signal": payload["last_signal"],
            "updated_at": payload["updated_at"],
        }
        try:
            self._client.table(TABLE).upsert(row, on_conflict="bot_id").execute()
        except Exception as exc:  # noqa: BLE001
            raise PositionStoreError(f"Failed to save position to Supabase: {exc}") from exc


def position_store_from_settings(settings: Settings) -> PositionStore:
    backend = settings.position_state_backend
    if backend == "file":
        return FilePositionStore(settings.position_state_json)
    if backend == "supabase":
        return SupabasePositionStore(settings.position_state_bot_id)
    raise ConfigError(f"Unsupported POSITION_STATE_BACKEND: {backend!r} (use file or supabase).")
