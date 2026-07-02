from __future__ import annotations

from alerts import (
    _action_line,
    _embed_title,
    _regime_from_trend,
    _show_trade_levels,
    _summary_line,
    build_discord_embed,
    format_alert_message,
)
from strategy_types import (
    HIGH_SIGNAL_QUALITY_THRESHOLD,
    AlertDecision,
    PositionState,
    RunTechnicalMeta,
)


def _meta(*, min_confidence_to_trade: int = 62) -> RunTechnicalMeta:
    return RunTechnicalMeta(
        qqq_ticker="QQQ",
        run_utc_iso="2026-05-19T12:21:15Z",
        daily_bar_end="2026-05-19",
        h4_bar_end="2026-05-19",
        blocked_today=False,
        regime="trend_up",
        base_bull_entry_threshold=5,
        base_bear_entry_threshold=5,
        base_weak_threshold=3,
        effective_bull_entry=5.0,
        effective_bear_entry=5.0,
        effective_weak=3.0,
        score_weights=(1.0,) * 8,
        weighted_bull=6.0,
        weighted_bear=2.0,
        stop_loss_pct=0.08,
        take_profit_pct=0.15,
        stretch_take_profit_pct=0.25,
        max_hold_days=10,
        entry_atr_multiplier=0.5,
        anchor_date_label="Jan 1",
        flip_in_range_regime=False,
        min_confidence_to_trade=min_confidence_to_trade,
        high_confidence_only=False,
    )


def _cash_alert(**kwargs: object) -> AlertDecision:
    defaults = dict(
        alert_type="CASH",
        symbol="CASH",
        qqq_trend_reason="daily close > EMA20 | regime=trend_up",
        bullish_score=75,
        bearish_score=25,
        confidence_score=50,
        entry_zone_low=700.0,
        entry_zone_high=711.0,
        stop_loss=0.0,
        take_profit=0.0,
        stretch_take_profit=0.0,
        max_hold_date="",
        timestamp="2026-05-19T12:21:15Z",
        notes="Entry skipped: stack dominance 50% is below minimum 62%.",
        notes_kind="entry_skipped_confidence",
    )
    defaults.update(kwargs)
    return AlertDecision(**defaults)  # type: ignore[arg-type]


def test_action_line_confidence_skip():
    alert = _cash_alert()
    line = _action_line(alert, technical_meta=_meta())
    assert "NO TRADE" in line
    assert "TQQQ" in line
    assert "50%" in line
    assert "62" in line


def test_format_alert_message_uses_configured_min_confidence():
    alert = _cash_alert(
        notes="Entry skipped: stack dominance 50% is below minimum 70%.",
        notes_kind="entry_skipped_confidence",
    )
    message = format_alert_message(alert, technical_meta=_meta(min_confidence_to_trade=70))
    assert "need >=70%" in message
    assert "below minimum 70%" in message


def test_action_line_high_confidence_skip_uses_constant():
    alert = _cash_alert(
        confidence_score=70,
        notes_kind="entry_skipped_high_conf",
        notes=f"Entry skipped: --high-confidence-only requires stack dominance >=75% (current 70%).",
    )
    line = _action_line(alert)
    assert str(HIGH_SIGNAL_QUALITY_THRESHOLD) in line


def test_action_line_flip_uses_position_before():
    alert = AlertDecision(
        alert_type="FLIP",
        symbol="SQQQ",
        qqq_trend_reason="regime=trend_down",
        bullish_score=20,
        bearish_score=80,
        confidence_score=60,
        entry_zone_low=700.0,
        entry_zone_high=711.0,
        stop_loss=750.0,
        take_profit=650.0,
        stretch_take_profit=600.0,
        max_hold_date="2026-06-01",
        timestamp="2026-05-19T12:21:15Z",
        notes="Reverse signal: sell TQQQ and buy SQQQ.",
        notes_kind="flip",
    )
    position = PositionState(active_symbol="TQQQ", entry_price=700.0)
    line = _action_line(alert, position_before=position)
    summary = _summary_line(alert, position_before=position)
    assert "sell TQQQ" in line
    assert "rotate from TQQQ" in summary


def test_summary_exit_sell():
    alert = AlertDecision(
        alert_type="SELL",
        symbol="TQQQ",
        qqq_trend_reason="regime=trend_up",
        bullish_score=75,
        bearish_score=25,
        confidence_score=50,
        entry_zone_low=700.0,
        entry_zone_high=711.0,
        stop_loss=620.0,
        take_profit=775.0,
        stretch_take_profit=842.0,
        max_hold_date="2026-05-15",
        timestamp="2026-05-19T12:21:15Z",
        notes="Exit: max hold date passed (2026-05-15).",
        notes_kind="exit",
    )
    summary = _summary_line(alert)
    assert "Close TQQQ" in summary
    assert "max hold" in summary.lower()


def test_summary_holding_flip_suppressed():
    alert = AlertDecision(
        alert_type="CASH",
        symbol="TQQQ",
        qqq_trend_reason="regime=trend_up",
        bullish_score=75,
        bearish_score=25,
        confidence_score=50,
        entry_zone_low=700.0,
        entry_zone_high=711.0,
        stop_loss=620.0,
        take_profit=775.0,
        stretch_take_profit=842.0,
        max_hold_date="2026-06-01",
        timestamp="2026-05-19T12:21:15Z",
        notes="Holding active position. Flip suppressed (whipsaw guard).",
        notes_kind="holding",
        flip_suppressed=True,
    )
    summary = _summary_line(alert)
    assert "Still in TQQQ" in summary
    assert "whipsaw guard" in summary


def test_blocked_embed_title():
    alert = _cash_alert(
        symbol="CASH",
        notes="Entry blocked by event calendar (manual blackout + optional CPI/FOMC/earnings risk dates).",
        notes_kind="blocked",
    )
    assert _embed_title(alert) == "No trade (calendar)"


def test_flat_cash_hides_trade_levels():
    alert = _cash_alert()
    assert not _show_trade_levels(alert)


def test_sell_hides_trade_levels():
    alert = AlertDecision(
        alert_type="SELL",
        symbol="TQQQ",
        qqq_trend_reason="regime=trend_up",
        bullish_score=75,
        bearish_score=25,
        confidence_score=50,
        entry_zone_low=700.0,
        entry_zone_high=711.0,
        stop_loss=620.0,
        take_profit=775.0,
        stretch_take_profit=842.0,
        max_hold_date="2026-05-15",
        timestamp="2026-05-19T12:21:15Z",
        notes="Exit: stop loss hit.",
        notes_kind="exit",
    )
    assert not _show_trade_levels(alert)
    embed = build_discord_embed(alert)
    names = [f["name"] for f in embed["fields"]]  # type: ignore[index]
    assert "Levels (QQQ-based)" not in names


def test_discord_embed_no_zero_levels_for_skipped_entry():
    alert = _cash_alert()
    embed = build_discord_embed(alert)
    names = [f["name"] for f in embed["fields"]]  # type: ignore[index]
    assert "Levels (QQQ-based)" not in names
    assert "Stack dominance" in names
    assert "Why" in names
    assert _embed_title(alert) == "No trade (weak TQQQ signal)"


def test_regime_from_trend():
    assert _regime_from_trend("ema cross | regime=trend_up") == "trend_up"
    assert _regime_from_trend("no regime token") == "—"


def test_discord_embed_empty_timestamp_guard():
    alert = _cash_alert(timestamp="")
    embed = build_discord_embed(alert)
    time_field = next(f for f in embed["fields"] if f["name"] == "Time (UTC)")  # type: ignore[index]
    assert time_field["value"] == "—"

