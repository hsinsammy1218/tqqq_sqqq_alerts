from __future__ import annotations

from pathlib import Path

from journal import alert_from_journal_row, append_journal, load_last_journal_alert
from strategy_types import AlertDecision


def test_load_last_journal_alert(tmp_path: Path):
    path = tmp_path / "alerts_journal.csv"
    assert load_last_journal_alert(path) is None

    first = AlertDecision(
        alert_type="CASH",
        symbol="CASH",
        qqq_trend_reason="regime=range",
        bullish_score=50,
        bearish_score=50,
        confidence_score=0,
        entry_zone_low=1.0,
        entry_zone_high=2.0,
        stop_loss=0.0,
        take_profit=0.0,
        stretch_take_profit=0.0,
        max_hold_date="",
        timestamp="2026-05-01T10:00:00Z",
        notes="No high-confidence setup.",
        notes_kind="other",
    )
    second = AlertDecision(
        alert_type="BUY",
        symbol="TQQQ",
        qqq_trend_reason="regime=trend_up",
        bullish_score=75,
        bearish_score=25,
        confidence_score=80,
        entry_zone_low=100.0,
        entry_zone_high=110.0,
        stop_loss=90.0,
        take_profit=120.0,
        stretch_take_profit=130.0,
        max_hold_date="2026-05-20",
        timestamp="2026-05-02T10:00:00Z",
        notes="Bullish QQQ setup.",
        notes_kind="buy_bull",
        signal_quality="HIGH",
    )
    append_journal(path, first)
    append_journal(path, second)
    loaded = load_last_journal_alert(path)
    assert loaded is not None
    assert loaded.alert_type == "BUY"
    assert loaded.symbol == "TQQQ"
    assert loaded.signal_quality == "HIGH"
    assert loaded.notes_kind == "buy_bull"


def test_alert_from_journal_row_infers_skip_kind():
    alert = alert_from_journal_row(
        {
            "timestamp_utc": "2026-05-19T12:21:15Z",
            "alert_type": "CASH",
            "execution_symbol": "CASH",
            "qqq_trend_reason": "regime=trend_down",
            "bull_score": "39",
            "bear_score": "61",
            "confidence": "22",
            "entry_zone_low": "654.3",
            "entry_zone_high": "668.8",
            "stop_price": "0",
            "take_profit_price": "0",
            "take_profit_stretch_price": "0",
            "max_hold_date": "",
            "notes": "Entry skipped: stack dominance 22% is below minimum 62%.",
            "signal_quality": "",
        }
    )
    assert alert.notes_kind == "entry_skipped_confidence"
    assert alert.bearish_score == 61
