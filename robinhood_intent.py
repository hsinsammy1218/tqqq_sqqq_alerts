"""Immutable durable TradeIntent envelope for Robinhood host-mediated pilot.

Render creates these. The authenticated MCP host revalidates and may execute.
No OAuth tokens are stored here.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from brokers.types import TradeIntent

INTENT_STATUSES = frozenset(
    {
        "PENDING",
        "CLAIMED",
        "SUBMITTED",
        "FILLED",
        "PARTIALLY_FILLED",
        "REJECTED",
        "CANCELLED",
        "EXPIRED",
        "UNKNOWN",
        "RECONCILIATION_REQUIRED",
        "BLOCKED",
        "EXPIRED_INTENT",
        "SUPERSEDED",
    }
)

DEFAULT_INTENT_TTL_SECONDS = 15 * 60


def _as_utc(ts: datetime | None) -> datetime | None:
    if ts is None:
        return None
    if ts.tzinfo is None:
        return ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)


def canonical_intent_payload(intent: TradeIntent) -> dict[str, Any]:
    """Stable field set for integrity digests. Order is fixed via sort_keys."""
    return {
        "action": intent.action.strip().upper(),
        "client_order_id": intent.client_order_id.strip(),
        "confidence": int(intent.confidence),
        "estimated_notional": intent.estimated_notional,
        "estimated_price": intent.estimated_price,
        "execution_symbol": intent.execution_symbol.strip().upper(),
        "purpose": intent.purpose.strip(),
        "quantity": int(intent.quantity),
        "reason": intent.reason,
        "regime": intent.regime,
        "signal_id": intent.signal_id.strip(),
        "signal_symbol": intent.signal_symbol.strip().upper(),
        "strategy_version": intent.strategy_version.strip(),
        "timestamp": intent.timestamp.strip(),
    }


def integrity_digest(intent: TradeIntent) -> str:
    raw = json.dumps(canonical_intent_payload(intent), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def verify_integrity(intent: TradeIntent, digest: str) -> bool:
    expected = (digest or "").strip().lower()
    return bool(expected) and integrity_digest(intent) == expected


@dataclass(frozen=True)
class DurableTradeIntent:
    """TradeIntent plus expiry and integrity for the Supabase handoff table."""

    intent: TradeIntent
    expires_at: datetime
    integrity_digest: str
    status: str = "PENDING"

    def as_row(self, *, bot_id: str = "default", meta: dict[str, Any] | None = None) -> dict[str, Any]:
        intent = self.intent
        return {
            "client_order_id": intent.client_order_id,
            "bot_id": bot_id,
            "status": self.status,
            "strategy_version": intent.strategy_version,
            "signal_symbol": intent.signal_symbol,
            "execution_symbol": intent.execution_symbol,
            "action": intent.action,
            "quantity": int(intent.quantity),
            "estimated_price": intent.estimated_price,
            "estimated_notional": intent.estimated_notional,
            "confidence": int(intent.confidence),
            "regime": intent.regime,
            "reason": intent.reason,
            "signal_id": intent.signal_id,
            "purpose": intent.purpose,
            "intent_timestamp": intent.timestamp,
            "expires_at": self.expires_at.astimezone(timezone.utc).isoformat(),
            "integrity_digest": self.integrity_digest,
            "meta": meta or {},
        }

    def is_expired(self, now: datetime | None = None) -> bool:
        clock = _as_utc(now) or datetime.now(timezone.utc)
        return clock >= _as_utc(self.expires_at)  # type: ignore[operator]


def wrap_intent(
    intent: TradeIntent,
    *,
    now: datetime | None = None,
    ttl_seconds: int = DEFAULT_INTENT_TTL_SECONDS,
    status: str = "PENDING",
) -> DurableTradeIntent:
    if ttl_seconds <= 0:
        raise ValueError("ttl_seconds must be positive")
    if status not in INTENT_STATUSES:
        raise ValueError(f"unsupported status {status!r}")
    clock = _as_utc(now) or datetime.now(timezone.utc)
    return DurableTradeIntent(
        intent=intent,
        expires_at=clock + timedelta(seconds=int(ttl_seconds)),
        integrity_digest=integrity_digest(intent),
        status=status,
    )


def intent_from_row(row: dict[str, Any]) -> DurableTradeIntent:
    intent = TradeIntent(
        strategy_version=str(row["strategy_version"]),
        signal_symbol=str(row["signal_symbol"]),
        execution_symbol=str(row["execution_symbol"]),
        action=str(row["action"]),
        quantity=int(row["quantity"]),
        estimated_price=row.get("estimated_price"),
        estimated_notional=row.get("estimated_notional"),
        confidence=int(row.get("confidence") or 0),
        regime=str(row.get("regime") or ""),
        reason=str(row.get("reason") or ""),
        signal_id=str(row["signal_id"]),
        timestamp=str(row["intent_timestamp"]),
        purpose=str(row["purpose"]),
        client_order_id=str(row["client_order_id"]),
    )
    expires_raw = row["expires_at"]
    if isinstance(expires_raw, datetime):
        expires_at = _as_utc(expires_raw)  # type: ignore[assignment]
    else:
        expires_at = datetime.fromisoformat(str(expires_raw).replace("Z", "+00:00"))
        expires_at = _as_utc(expires_at)  # type: ignore[assignment]
    digest = str(row["integrity_digest"])
    if not verify_integrity(intent, digest):
        raise ValueError("integrity_digest mismatch; refusing to trust row")
    status = str(row.get("status") or "PENDING")
    if status not in INTENT_STATUSES:
        raise ValueError(f"unsupported status {status!r}")
    return DurableTradeIntent(
        intent=intent,
        expires_at=expires_at,  # type: ignore[arg-type]
        integrity_digest=digest,
        status=status,
    )
