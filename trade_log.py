"""Append-only paper trade journal for self-learning.

Primary local path: ``logs/trades.jsonl`` (one JSON object per line).
On Render, also persist to Supabase ``public.bot_trade_log`` via
``trade_log_store`` when ``TRADE_LOG_BACKEND=supabase``.
Runtime data under ``logs/`` is gitignored.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from runtime_logging import format_utc_z

DEFAULT_TRADE_LOG_PATH = Path("logs/trades.jsonl")

# Documented schema keys (extra keys are allowed; missing optional keys omitted).
TRADE_LOG_FIELDS = (
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


def extract_regime(qqq_trend_reason: str | None) -> str | None:
    """Pull ``regime=<name>`` from alert reasoning text when present."""
    if not qqq_trend_reason:
        return None
    for token in qqq_trend_reason.replace(",", " ").split():
        if token.lower().startswith("regime="):
            value = token.split("=", 1)[1].strip()
            return value or None
    return None


def build_trade_record(
    *,
    symbol: str,
    side: str,
    status: str,
    source: str = "strategy",
    qty: str | float | None = None,
    limit_price: str | float | None = None,
    fill_price: str | float | None = None,
    filled_qty: str | float | None = None,
    order_id: str | None = None,
    purpose: str | None = None,
    alert_type: str | None = None,
    confidence: int | None = None,
    regime: str | None = None,
    signal_quality: str | None = None,
    paper_trading: bool | None = None,
    dry_run: bool | None = None,
    error: str | None = None,
    detail: str | None = None,
    timestamp: str | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "timestamp": timestamp or format_utc_z(datetime.now(timezone.utc)),
        "symbol": (symbol or "").upper(),
        "side": (side or "").lower(),
        "status": status,
        "source": source,
    }
    optional: dict[str, Any] = {
        "qty": None if qty is None else str(qty),
        "limit_price": None if limit_price is None else str(limit_price),
        "fill_price": None if fill_price is None else str(fill_price),
        "filled_qty": None if filled_qty is None else str(filled_qty),
        "order_id": order_id,
        "purpose": purpose,
        "alert_type": alert_type,
        "confidence": confidence,
        "regime": regime,
        "signal_quality": signal_quality,
        "paper_trading": paper_trading,
        "dry_run": dry_run,
        "error": error,
        "detail": detail,
    }
    for key, value in optional.items():
        if value is not None:
            record[key] = value
    if extra:
        for key, value in extra.items():
            if value is not None and key not in record:
                record[key] = value
    return record


def append_trade_record(path: Path | str, record: dict[str, Any]) -> Path:
    """Append one JSONL record. Creates parent directories as needed."""
    log_path = Path(path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    payload = dict(record)
    if "timestamp" not in payload or not payload["timestamp"]:
        payload["timestamp"] = format_utc_z(datetime.now(timezone.utc))
    line = json.dumps(payload, ensure_ascii=True, separators=(",", ":"))
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")
    return log_path


def read_trade_records(path: Path | str) -> list[dict[str, Any]]:
    log_path = Path(path)
    if not log_path.exists():
        return []
    rows: list[dict[str, Any]] = []
    with log_path.open("r", encoding="utf-8") as handle:
        for line in handle:
            text = line.strip()
            if not text:
                continue
            rows.append(json.loads(text))
    return rows
