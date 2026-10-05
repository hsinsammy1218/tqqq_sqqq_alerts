"""Stale-bar gate for new paper exposure. Does not change decide() rules."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

FRESH = "FRESH"
STALE = "STALE"
UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class DataFreshness:
    state: str
    reason: str
    latest_bar: datetime | None = None
    age_seconds: float | None = None

    def blocks_new_exposure(self) -> bool:
        return self.state in {STALE, UNKNOWN}


def _as_utc(ts: datetime) -> datetime:
    if ts.tzinfo is None:
        return ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)


def assess_intraday_freshness(
    bar_start: datetime | None,
    now: datetime,
    *,
    max_age_minutes: float,
    bar_duration_minutes: float = 60,
) -> DataFreshness:
    """FRESH when ``now`` is within ``max_age`` after the bar's expected end.

    A missing timestamp is UNKNOWN. ``max_age_minutes`` <= 0 disables the gate
    (FRESH) so callers can opt out. The bar timestamp is the bar start.
    """
    if max_age_minutes <= 0:
        return DataFreshness(state=FRESH, reason="freshness gate off", latest_bar=bar_start)
    if bar_start is None:
        return DataFreshness(state=UNKNOWN, reason="no intraday bar timestamp")
    start = _as_utc(bar_start)
    current = _as_utc(now)
    bar_end = start + timedelta(minutes=float(bar_duration_minutes))
    age = (current - bar_end).total_seconds()
    if age > float(max_age_minutes) * 60.0:
        return DataFreshness(
            state=STALE,
            reason=(
                f"intraday bar ended {age / 60.0:.1f} min ago; "
                f"max age {max_age_minutes:g} min"
            ),
            latest_bar=start,
            age_seconds=age,
        )
    return DataFreshness(
        state=FRESH,
        reason="intraday bar is inside the freshness window",
        latest_bar=start,
        age_seconds=age,
    )


def filter_intents_for_stale_data(
    intents: list[Any],
    freshness: DataFreshness,
) -> tuple[list[Any], list[str]]:
    """Drop BUY / flip-entry intents when data is stale or unknown. Keep sells."""
    if not freshness.blocks_new_exposure():
        return list(intents), []
    kept: list[Any] = []
    skipped: list[str] = []
    for intent in intents:
        side = str(getattr(intent, "side", "") or "").lower()
        if side == "buy":
            skipped.append(
                f"STALE blocked buy {getattr(intent, 'symbol', '')} "
                f"({getattr(intent, 'purpose', '')}): {freshness.reason}"
            )
            continue
        kept.append(intent)
    return kept, skipped
