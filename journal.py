from __future__ import annotations

import csv
from pathlib import Path

from strategy import AlertDecision


FIELDS = [
    "timestamp_utc",
    "alert_type",
    "execution_symbol",
    "qqq_trend_reason",
    "bull_score",
    "bear_score",
    "confidence",
    "entry_zone_low",
    "entry_zone_high",
    "stop_price",
    "take_profit_price",
    "take_profit_stretch_price",
    "max_hold_date",
    "notes",
    "signal_quality",
]


def _infer_notes_kind(alert_type: str, notes: str, symbol: str) -> str:
    text = (notes or "").lower()
    atype = (alert_type or "").upper()
    if atype == "BUY":
        return "buy_bull" if symbol.upper() == "TQQQ" else "buy_bear"
    if atype == "SELL":
        return "exit"
    if atype == "FLIP":
        return "flip"
    if "blocked" in text or "calendar" in text or "blackout" in text:
        return "blocked"
    if "holding" in text:
        return "holding"
    if "high-confidence" in text or "high confidence" in text:
        return "entry_skipped_high_conf"
    if "dominance" in text or "minimum" in text or "skipped" in text:
        return "entry_skipped_confidence"
    return "other"


def alert_from_journal_row(row: dict[str, str]) -> AlertDecision:
    symbol = (row.get("execution_symbol") or "CASH").strip()
    alert_type = (row.get("alert_type") or "CASH").strip().upper()
    notes = row.get("notes") or ""
    quality = (row.get("signal_quality") or "").strip() or None
    return AlertDecision(
        alert_type=alert_type,
        symbol=symbol,
        qqq_trend_reason=row.get("qqq_trend_reason") or "",
        bullish_score=int(float(row.get("bull_score") or 0)),
        bearish_score=int(float(row.get("bear_score") or 0)),
        confidence_score=int(float(row.get("confidence") or 0)),
        entry_zone_low=float(row.get("entry_zone_low") or 0),
        entry_zone_high=float(row.get("entry_zone_high") or 0),
        stop_loss=float(row.get("stop_price") or 0),
        take_profit=float(row.get("take_profit_price") or 0),
        stretch_take_profit=float(row.get("take_profit_stretch_price") or 0),
        max_hold_date=row.get("max_hold_date") or "",
        timestamp=row.get("timestamp_utc") or "",
        notes=notes,
        notes_kind=_infer_notes_kind(alert_type, notes, symbol),
        flip_suppressed="flip suppressed" in notes.lower() or "whipsaw" in notes.lower(),
        signal_quality=quality,
    )


def load_last_journal_alert(path: Path) -> AlertDecision | None:
    if not path.exists():
        return None
    with path.open("r", newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        return None
    return alert_from_journal_row(rows[-1])


def append_journal(path: Path, alert: AlertDecision) -> None:
    exists = path.exists()
    with path.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        if not exists:
            writer.writeheader()
        writer.writerow(
            {
                "timestamp_utc": alert.timestamp,
                "alert_type": alert.alert_type,
                "execution_symbol": alert.symbol,
                "qqq_trend_reason": alert.qqq_trend_reason,
                "bull_score": alert.bullish_score,
                "bear_score": alert.bearish_score,
                "confidence": alert.confidence_score,
                "entry_zone_low": round(alert.entry_zone_low, 4),
                "entry_zone_high": round(alert.entry_zone_high, 4),
                "stop_price": round(alert.stop_loss, 4),
                "take_profit_price": round(alert.take_profit, 4),
                "take_profit_stretch_price": round(alert.stretch_take_profit, 4),
                "max_hold_date": alert.max_hold_date,
                "notes": alert.notes,
                "signal_quality": alert.signal_quality or "",
            }
        )
