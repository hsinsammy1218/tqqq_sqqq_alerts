"""Trade log backends: local JSONL file and/or Supabase append table."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol

from config import ConfigError, Settings
from supabase_client import create_supabase_client
from trade_log import append_trade_record, read_trade_records

TABLE = "bot_trade_log"

# Columns persisted to Supabase (mirrors TRADE_LOG_FIELDS + bot_id).
_ROW_KEYS = (
    "timestamp",
    "symbol",
    "side",
    "qty",
    "limit_price",
    "fill_price",
    "filled_qty",
    "order_id",
    "status",
    "purpose",
    "alert_type",
    "confidence",
    "regime",
    "signal_quality",
    "paper_trading",
    "dry_run",
    "error",
    "source",
    "detail",
)


class TradeLogStoreError(Exception):
    """Hard failure reading or writing the durable trade log."""


class TradeLogStore(Protocol):
    def describe(self) -> str: ...

    def append(self, record: dict[str, Any]) -> None: ...

    def read_all(self) -> list[dict[str, Any]]: ...


def record_to_supabase_row(record: dict[str, Any], *, bot_id: str) -> dict[str, Any]:
    """Map a JSONL trade record to a ``bot_trade_log`` insert payload."""
    row: dict[str, Any] = {"bot_id": bot_id}
    for key in _ROW_KEYS:
        if key not in record:
            continue
        value = record[key]
        if value is None:
            continue
        if key == "confidence" and value is not None:
            try:
                row[key] = int(value)
            except (TypeError, ValueError):
                row[key] = value
        else:
            row[key] = value
    if "timestamp" not in row or not row["timestamp"]:
        raise TradeLogStoreError("Trade log record missing timestamp.")
    return row


def supabase_row_to_record(row: dict[str, Any]) -> dict[str, Any]:
    """Strip DB-only columns so report code sees the same shape as JSONL."""
    out: dict[str, Any] = {}
    for key in _ROW_KEYS:
        if key in row and row[key] is not None:
            out[key] = row[key]
    return out


def parse_supabase_trade_log_payload(data: object) -> list[dict[str, Any]]:
    if data is None:
        raise TradeLogStoreError("Supabase trade log load returned no data payload.")
    if not isinstance(data, list):
        raise TradeLogStoreError(
            f"Supabase trade log load returned unexpected payload type: {type(data).__name__}."
        )
    records: list[dict[str, Any]] = []
    for item in data:
        if not isinstance(item, dict):
            raise TradeLogStoreError("Supabase trade log row was not an object.")
        records.append(supabase_row_to_record(item))
    return records


class FileTradeLogStore:
    def __init__(self, path: Path | str) -> None:
        self._path = Path(path)

    def describe(self) -> str:
        return str(self._path)

    def append(self, record: dict[str, Any]) -> None:
        try:
            append_trade_record(self._path, record)
        except OSError as exc:
            raise TradeLogStoreError(f"Failed to append trade log to {self._path}: {exc}") from exc

    def read_all(self) -> list[dict[str, Any]]:
        try:
            return read_trade_records(self._path)
        except (OSError, ValueError) as exc:
            raise TradeLogStoreError(f"Failed to read trade log from {self._path}: {exc}") from exc


class SupabaseTradeLogStore:
    def __init__(self, bot_id: str) -> None:
        self._bot_id = bot_id
        self._client = create_supabase_client()

    def describe(self) -> str:
        return f"supabase:{TABLE}:{self._bot_id}"

    def append(self, record: dict[str, Any]) -> None:
        row = record_to_supabase_row(record, bot_id=self._bot_id)
        try:
            self._client.table(TABLE).insert(row).execute()
        except Exception as exc:  # noqa: BLE001
            raise TradeLogStoreError(f"Failed to insert trade log into Supabase: {exc}") from exc

    def read_all(self) -> list[dict[str, Any]]:
        try:
            result = (
                self._client.table(TABLE)
                .select(",".join(_ROW_KEYS))
                .eq("bot_id", self._bot_id)
                .order("timestamp")
                .execute()
            )
        except Exception as exc:  # noqa: BLE001
            raise TradeLogStoreError(f"Failed to load trade log from Supabase: {exc}") from exc
        return parse_supabase_trade_log_payload(result.data)


class DualTradeLogStore:
    """Always write local JSONL; also insert to Supabase (Render durability)."""

    def __init__(self, file_store: FileTradeLogStore, remote: SupabaseTradeLogStore) -> None:
        self._file = file_store
        self._remote = remote

    def describe(self) -> str:
        return f"{self._file.describe()}+{self._remote.describe()}"

    def append(self, record: dict[str, Any]) -> None:
        errors: list[str] = []
        try:
            self._file.append(record)
        except TradeLogStoreError as exc:
            errors.append(str(exc))
        try:
            self._remote.append(record)
        except TradeLogStoreError as exc:
            errors.append(str(exc))
        if errors:
            raise TradeLogStoreError("; ".join(errors))

    def read_all(self) -> list[dict[str, Any]]:
        return self._remote.read_all()


def trade_log_store_from_settings(settings: Settings) -> TradeLogStore:
    backend = settings.trade_log_backend
    file_store = FileTradeLogStore(settings.trade_log_jsonl)
    if backend == "file":
        return file_store
    if backend == "supabase":
        return DualTradeLogStore(file_store, SupabaseTradeLogStore(settings.trade_log_bot_id))
    raise ConfigError(f"Unsupported TRADE_LOG_BACKEND: {backend!r} (use file or supabase).")
