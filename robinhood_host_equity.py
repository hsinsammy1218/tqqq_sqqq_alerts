"""Durable equity baselines for Robinhood host risk (Phase 5R.1).

Never fabricate day_start / week_start / peak from the current equity mark.
When Robinhood's portfolio payload omits baselines, load durable Supabase
values. If still missing while loss/drawdown gates are configured → fail
closed at risk check time (see ``check_host_new_exposure``).
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime, timezone
from typing import Any, Protocol

from alpaca_live_entry_reservation import trading_day_america_new_york
from brokers.types import AccountView


def _iso_week_key(d: date) -> str:
    iso = d.isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


@dataclass(frozen=True)
class EquityBaselines:
    bot_id: str
    day_key: date | None
    day_start_equity: float | None
    week_key: str | None
    week_start_equity: float | None
    peak_equity: float | None
    source: str = "durable"
    detail: str = ""


class EquityBaselineStore(Protocol):
    def load(self) -> EquityBaselines: ...

    def save(self, baselines: EquityBaselines) -> None: ...


@dataclass
class InMemoryEquityBaselineStore:
    bot_id: str = "default"
    _state: EquityBaselines | None = None
    unavailable: bool = False

    def load(self) -> EquityBaselines:
        if self.unavailable:
            return EquityBaselines(
                bot_id=self.bot_id,
                day_key=None,
                day_start_equity=None,
                week_key=None,
                week_start_equity=None,
                peak_equity=None,
                source="unavailable",
                detail="equity baseline store unavailable",
            )
        if self._state is None:
            return EquityBaselines(
                bot_id=self.bot_id,
                day_key=None,
                day_start_equity=None,
                week_key=None,
                week_start_equity=None,
                peak_equity=None,
                source="empty",
                detail="no durable baselines yet",
            )
        return self._state

    def save(self, baselines: EquityBaselines) -> None:
        if self.unavailable:
            return
        self._state = baselines


class AtomicEquityBaselineStore:
    TABLE = "bot_robinhood_equity_baselines"

    def __init__(self, client: Any | None = None, *, bot_id: str = "default") -> None:
        self._client = client
        self.bot_id = bot_id

    def _ensure_client(self) -> Any:
        if self._client is not None:
            return self._client
        from supabase_client import create_supabase_client

        self._client = create_supabase_client()
        return self._client

    def load(self) -> EquityBaselines:
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
            return EquityBaselines(
                bot_id=self.bot_id,
                day_key=None,
                day_start_equity=None,
                week_key=None,
                week_start_equity=None,
                peak_equity=None,
                source="unavailable",
                detail=f"equity baseline store unavailable: {exc}",
            )
        rows = result.data if hasattr(result, "data") else result
        if not rows:
            return EquityBaselines(
                bot_id=self.bot_id,
                day_key=None,
                day_start_equity=None,
                week_key=None,
                week_start_equity=None,
                peak_equity=None,
                source="empty",
                detail="no durable baselines yet",
            )
        row = rows[0]
        day_key = row.get("day_key")
        if isinstance(day_key, str):
            day_key = date.fromisoformat(day_key)
        return EquityBaselines(
            bot_id=self.bot_id,
            day_key=day_key,
            day_start_equity=_finite(row.get("day_start_equity")),
            week_key=row.get("week_key"),
            week_start_equity=_finite(row.get("week_start_equity")),
            peak_equity=_finite(row.get("peak_equity")),
            source="durable",
            detail="",
        )

    def save(self, baselines: EquityBaselines) -> None:
        row = {
            "bot_id": baselines.bot_id,
            "day_key": baselines.day_key.isoformat() if baselines.day_key else None,
            "day_start_equity": baselines.day_start_equity,
            "week_key": baselines.week_key,
            "week_start_equity": baselines.week_start_equity,
            "peak_equity": baselines.peak_equity,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "detail": baselines.detail or "",
        }
        client = self._ensure_client()
        client.table(self.TABLE).upsert(row, on_conflict="bot_id").execute()


def _finite(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return number


def merge_account_baselines(
    account: AccountView,
    store: EquityBaselineStore,
    *,
    now: datetime,
) -> tuple[AccountView, EquityBaselines]:
    """Merge RH-provided baselines with durable store. Never invent from equity.

    Rules:
    - Prefer non-null values from the live account payload.
    - Else reuse durable value when the trading-day / ISO-week key still matches.
    - On a new trading day, only seed day_start from RH previous-close fields
      (already on ``account.day_start_equity``). Do **not** copy current equity.
    - Peak: max(durable peak, current equity, RH peak) when equity is known;
      never invent a peak when equity is unknown.
    """
    clock = now if now.tzinfo else now.replace(tzinfo=timezone.utc)
    today = trading_day_america_new_york(clock)
    week_key = _iso_week_key(today)
    durable = store.load()

    day_start = account.day_start_equity
    if day_start is None and durable.day_key == today:
        day_start = durable.day_start_equity
    # Explicit non-fabrication: if still None, leave None (risk fails closed).

    week_start = account.week_start_equity
    if week_start is None and durable.week_key == week_key:
        week_start = durable.week_start_equity

    peak_candidates = [
        v
        for v in (account.peak_equity, durable.peak_equity, account.equity)
        if v is not None
    ]
    peak = max(peak_candidates) if peak_candidates else None

    merged = replace(
        account,
        day_start_equity=day_start,
        week_start_equity=week_start,
        peak_equity=peak,
    )

    # If RH gave us a fresh previous-close today, roll the day key.
    if account.day_start_equity is not None:
        persist_day_key: date | None = today
        persist_day_start = account.day_start_equity
    elif durable.day_key == today and durable.day_start_equity is not None:
        persist_day_key = today
        persist_day_start = durable.day_start_equity
    else:
        persist_day_key = durable.day_key
        persist_day_start = durable.day_start_equity

    if account.week_start_equity is not None:
        persist_week_key: str | None = week_key
        persist_week_start = account.week_start_equity
    elif durable.week_key == week_key and durable.week_start_equity is not None:
        persist_week_key = week_key
        persist_week_start = durable.week_start_equity
    else:
        persist_week_key = durable.week_key
        persist_week_start = durable.week_start_equity

    updated = EquityBaselines(
        bot_id=durable.bot_id,
        day_key=persist_day_key,
        day_start_equity=persist_day_start,
        week_key=persist_week_key,
        week_start_equity=persist_week_start,
        peak_equity=peak,
        source="merged",
        detail="",
    )
    if any(
        v is not None
        for v in (
            updated.day_start_equity,
            updated.week_start_equity,
            updated.peak_equity,
        )
    ):
        store.save(updated)
    return merged, updated


__all__ = [
    "AtomicEquityBaselineStore",
    "EquityBaselineStore",
    "EquityBaselines",
    "InMemoryEquityBaselineStore",
    "merge_account_baselines",
]
