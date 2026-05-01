from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import requests

from strategy import load_blocked_dates


def _normalize_date_str(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if len(text) >= 10 and text[4] == "-" and text[7] == "-":
        head = text[:10]
        parts = head.split("-")
        if len(parts) == 3 and all(p.isdigit() for p in parts):
            return head
    return None


def _collect_risk_calendar_lists(payload: dict[str, Any]) -> set[str]:
    out: set[str] = set()
    rc = payload.get("risk_calendar")
    if not isinstance(rc, dict):
        return out
    for key in ("cpi_dates", "fomc_dates", "nasdaq_earnings_dates"):
        raw = rc.get(key)
        if not isinstance(raw, list):
            continue
        for item in raw:
            d = _normalize_date_str(item)
            if d:
                out.add(d)
    return out


def _merge_remote_calendar(url: str) -> tuple[set[str], str | None]:
    try:
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        if not isinstance(data, dict):
            return set(), "risk calendar URL returned JSON that is not an object"
        return _collect_risk_calendar_lists(data), None
    except Exception:  # noqa: BLE001
        return set(), "risk calendar URL unavailable (network or parse error)"


def load_merged_blackout_dates(
    events_path: Path,
    *,
    risk_avoidance: bool,
    risk_calendar_url: str | None,
) -> tuple[set[str], list[str]]:
    """
    Merge manual blocked_dates (events.json) with optional CPI/FOMC/earnings lists.

    Never raises: malformed files or failed HTTP falls back to whatever could be loaded.
    """
    manual = load_blocked_dates(events_path)
    notes: list[str] = []
    if not risk_avoidance:
        return manual, notes

    extra: set[str] = set()
    if events_path.exists():
        try:
            payload = json.loads(events_path.read_text(encoding="utf-8"))
            if isinstance(payload, dict):
                extra |= _collect_risk_calendar_lists(payload)
        except (json.JSONDecodeError, OSError) as exc:
            notes.append(f"could not read risk_calendar from events file ({exc})")

    url = (risk_calendar_url or "").strip()
    if url:
        remote, err = _merge_remote_calendar(url)
        extra |= remote
        if err:
            notes.append(err)

    return manual | extra, notes
