from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from strategy_types import PositionState

_VALID_SIDES = frozenset({"TQQQ", "SQQQ"})


def _coerce_entry_price(raw: object) -> tuple[float | None, bool]:
    if raw is None:
        return None, False
    if isinstance(raw, bool):
        return None, True
    if isinstance(raw, (int, float)):
        return float(raw), False
    return None, True


def _normalize_loaded_symbol(raw: object) -> tuple[str | None, bool]:
    if raw is None or raw == "":
        return None, False
    if not isinstance(raw, str):
        return None, True
    sym = raw.strip().upper()
    if sym not in _VALID_SIDES:
        return None, True
    return sym, False


def position_state_to_dict(position: PositionState) -> dict[str, object]:
    return {
        "symbol": position.active_symbol,
        "entry_price": position.entry_price,
        "entry_time": position.entry_timestamp,
        "last_signal": position.last_signal,
        "updated_at": position.updated_at,
    }


def position_state_from_dict(data: dict[str, Any]) -> tuple[PositionState, list[str]]:
    """Validate and normalize a position payload. Returns (state, warnings)."""
    warnings: list[str] = []

    sym_raw = data.get("symbol", data.get("active_symbol"))
    sym, sym_bad = _normalize_loaded_symbol(sym_raw)
    if sym_bad:
        warnings.append(f"Ignoring invalid symbol {sym_raw!r}. Starting flat.")
        return PositionState(), warnings

    ep_raw = data.get("entry_price")
    entry_price, ep_bad = _coerce_entry_price(ep_raw)
    if ep_bad:
        warnings.append(f"Ignoring invalid entry_price {ep_raw!r}.")
        entry_price = None

    et_raw = data.get("entry_time", data.get("entry_timestamp"))
    entry_time: str | None = None
    if et_raw is None or et_raw == "":
        entry_time = None
    elif isinstance(et_raw, str):
        entry_time = et_raw.strip()
    else:
        warnings.append(f"Ignoring invalid entry_time {et_raw!r}.")
        entry_time = None

    last_signal = data.get("last_signal")
    if last_signal is not None and not isinstance(last_signal, str):
        warnings.append("Ignoring invalid last_signal.")
        last_signal = None
    elif isinstance(last_signal, str):
        last_signal = last_signal.strip() or None

    updated_at = data.get("updated_at")
    if updated_at is not None and not isinstance(updated_at, str):
        warnings.append("Ignoring invalid updated_at.")
        updated_at = None
    elif isinstance(updated_at, str):
        updated_at = updated_at.strip() or None

    if sym is None:
        return (
            PositionState(
                active_symbol=None,
                entry_price=None,
                entry_timestamp=None,
                last_signal=last_signal,
                updated_at=updated_at,
            ),
            warnings,
        )

    return (
        PositionState(
            active_symbol=sym,
            entry_price=entry_price,
            entry_timestamp=entry_time,
            last_signal=last_signal,
            updated_at=updated_at,
        ),
        warnings,
    )


def load_position(path: Path) -> tuple[PositionState, list[str]]:
    """Load position_state.json. Returns (flat state, warnings) on any problem."""
    warnings: list[str] = []
    if not path.exists():
        return PositionState(), warnings
    try:
        raw_text = path.read_text(encoding="utf-8")
        data = json.loads(raw_text)
    except (OSError, json.JSONDecodeError) as exc:
        warnings.append(f"Ignoring unreadable position_state.json ({exc}). Starting flat.")
        return PositionState(), warnings
    if not isinstance(data, dict):
        warnings.append("position_state.json must be a JSON object. Starting flat.")
        return PositionState(), warnings
    return position_state_from_dict(data)


def save_position(path: Path, position: PositionState) -> None:
    path.write_text(json.dumps(position_state_to_dict(position), indent=2), encoding="utf-8")


def load_blocked_dates(path: Path) -> set[str]:
    if not path.exists():
        return set()
    try:
        data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return set()
    return set(data.get("blocked_dates", []))
