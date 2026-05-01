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
