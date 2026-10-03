from __future__ import annotations

from types import SimpleNamespace

from alerts import (
    _COLOR_BUY,
    _COLOR_CASH,
    _COLOR_FLIP,
    _COLOR_HOLD,
    _COLOR_SELL,
    _COLOR_SKIP,
    _action_line,
    _embed_color,
    _embed_title,
    _regime_from_trend,
    _show_trade_levels,
    _summary_line,
    build_discord_embed,
    format_alert_message,
    format_paper_results_summary,
)
from strategy_types import (
    HIGH_SIGNAL_QUALITY_THRESHOLD,
    AlertDecision,
    PositionState,
    RunTechnicalMeta,
)


def _meta(*, min_confidence_to_trade: int = 62, regime: str = "trend_up") -> RunTechnicalMeta:
    return RunTechnicalMeta(
        qqq_ticker="QQQ",
        run_utc_iso="2026-05-19T12:21:15Z",
        daily_bar_end="2026-05-19",
        h4_bar_end="2026-05-19",
        blocked_today=False,
        regime=regime,
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


def _buy_alert(**kwargs: object) -> AlertDecision:
    defaults = dict(
        alert_type="BUY",
        symbol="TQQQ",
        qqq_trend_reason="daily close > EMA20 | regime=trend_up",
        bullish_score=75,
        bearish_score=25,
        confidence_score=78,
        entry_zone_low=432.10,
        entry_zone_high=439.70,
        stop_loss=400.65,
        take_profit=502.13,
        stretch_take_profit=545.79,
        max_hold_date="2026-05-15",
        timestamp="2026-05-01T14:20:00Z",
        notes="Bullish QQQ setup.",
        notes_kind="buy_bull",
        signal_quality="HIGH",
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
    assert "Symbol: No position" in message
    assert "Alert: CASH" in message


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
    assert _embed_title(alert) == "No position · Calendar"


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
    assert "Levels (QQQ)" not in names


def test_discord_embed_no_zero_levels_for_skipped_entry():
    alert = _cash_alert()
    embed = build_discord_embed(alert, technical_meta=_meta())
    names = [f["name"] for f in embed["fields"]]  # type: ignore[index]
    assert "Levels (QQQ)" not in names
    assert "Confidence" in names
    assert "Reason" in names
    assert "Symbol" in names
    assert "Regime" in names
    assert "Strength" in names
    assert _embed_title(alert) == "No position · Weak TQQQ"
    symbol = next(f for f in embed["fields"] if f["name"] == "Symbol")  # type: ignore[index]
    assert symbol["value"] == "No position (lean TQQQ)"
    assert alert.alert_type == "CASH"
    assert alert.symbol == "CASH"
    assert "Manual trade" in embed["description"]  # type: ignore[operator]
    assert "QQQ trend (detail)" not in names
    assert "Rules (reference)" not in names
    assert "Bull checklist" not in "".join(names)
    assert embed["timestamp"] == "2026-05-19T12:21:15Z"
    assert _embed_color(alert) == _COLOR_SKIP


def test_discord_embed_buy_shape_and_levels():
    alert = _buy_alert()
    embed = build_discord_embed(alert, technical_meta=_meta())
    assert embed["title"] == "BUY TQQQ"
    assert embed["color"] == _COLOR_BUY
    names = [f["name"] for f in embed["fields"]]  # type: ignore[index]
    assert names[:4] == ["Symbol", "Confidence", "Regime", "Strength"]
    assert "Levels (QQQ)" in names
    assert "Reason" in names
    levels = next(f for f in embed["fields"] if f["name"] == "Levels (QQQ)")  # type: ignore[index]
    assert "432.10" in levels["value"]
    assert "400.65" in levels["value"]
    conf = next(f for f in embed["fields"] if f["name"] == "Confidence")  # type: ignore[index]
    assert "78%" in conf["value"]
    assert "HIGH" in conf["value"]
    assert "BUY TQQQ" in embed["description"]  # type: ignore[operator]


def test_discord_embed_action_colors():
    assert _embed_color(_buy_alert()) == _COLOR_BUY
    assert _embed_color(_buy_alert(alert_type="SELL", notes_kind="exit", signal_quality=None)) == _COLOR_SELL
    assert _embed_color(_buy_alert(alert_type="FLIP", symbol="SQQQ", notes_kind="flip", signal_quality=None)) == _COLOR_FLIP
    hold = _cash_alert(symbol="TQQQ", notes_kind="holding", notes="Holding active position.")
    assert _embed_color(hold) == _COLOR_HOLD
    assert _embed_title(hold) == "HOLD TQQQ"
    plain = _cash_alert(notes_kind="other", notes="No high-confidence setup.", confidence_score=0)
    assert _embed_color(plain) == _COLOR_CASH
    assert _embed_title(plain) == "No position"


def test_discord_embed_paper_and_preview():
    alert = _buy_alert()
    intent = SimpleNamespace(symbol="TQQQ", side="buy", purpose="entry")
    paper = [SimpleNamespace(intent=intent, ok=True, status="accepted", order_id="ord-1", detail="ok")]
    embed = build_discord_embed(alert, paper_results=paper, preview=True)
    names = [f["name"] for f in embed["fields"]]  # type: ignore[index]
    assert "Paper" in names
    paper_field = next(f for f in embed["fields"] if f["name"] == "Paper")  # type: ignore[index]
    assert "BUY TQQQ" in paper_field["value"]
    assert "ord-1" in paper_field["value"]
    assert "Preview" in embed["description"]  # type: ignore[operator]
    assert "preview" in embed["footer"]["text"]  # type: ignore[index]


def test_format_paper_results_summary_empty():
    assert format_paper_results_summary(None) is None
    assert format_paper_results_summary([]) is None


def test_regime_from_trend():
    assert _regime_from_trend("ema cross | regime=trend_up") == "trend_up"
    assert _regime_from_trend("no regime token") == "—"


def test_discord_embed_empty_timestamp_guard():
    alert = _cash_alert(timestamp="")
    embed = build_discord_embed(alert)
    assert "timestamp" not in embed
    names = [f["name"] for f in embed["fields"]]  # type: ignore[index]
    assert "Time (UTC)" not in names
