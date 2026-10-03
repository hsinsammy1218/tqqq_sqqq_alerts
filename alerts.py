from __future__ import annotations

import json
import re
from typing import Any, Sequence

import requests

from indicators import IndicatorSnapshot
from strategy import AlertDecision, PositionState, RunTechnicalMeta, hold_exit_summary_line, score_signals
from strategy_types import HIGH_SIGNAL_QUALITY_THRESHOLD

# Embed accent colors (readable on mobile; avoid purple glow).
_COLOR_BUY = 0x22A06B
_COLOR_SELL = 0xC9372C
_COLOR_FLIP = 0xD97706
_COLOR_HOLD = 0x2F6FED
_COLOR_CASH = 0x6B7280
_COLOR_SKIP = 0xB45309
_COLOR_BLOCKED = 0x9CA3AF

# Internal decision/symbol id stays "CASH"; this is display-only for Discord/console.
_NO_POSITION_LABEL = "No position"


def _display_symbol(symbol: str | None) -> str:
    """Map internal CASH id to the user-facing No position label."""
    if symbol is None or symbol == "" or symbol == "CASH":
        return _NO_POSITION_LABEL
    return symbol


def _truncate(text: str, max_len: int) -> str:
    text = text.strip()
    if len(text) <= max_len:
        return text
    return text[: max_len - 3] + "..."


def _leading_etf(alert: AlertDecision) -> str | None:
    if alert.bullish_score > alert.bearish_score:
        return "TQQQ"
    if alert.bearish_score > alert.bullish_score:
        return "SQQQ"
    return None


def _show_trade_levels(alert: AlertDecision) -> bool:
    if alert.alert_type == "SELL":
        return False
    if alert.alert_type in ("BUY", "FLIP"):
        return True
    return alert.notes_kind == "holding"


def _action_line(
    alert: AlertDecision,
    *,
    position_before: PositionState | None = None,
    technical_meta: RunTechnicalMeta | None = None,
) -> str:
    kind = alert.notes_kind
    if alert.alert_type == "BUY":
        return f"BUY {alert.symbol} — consider opening a position"
    if alert.alert_type == "FLIP":
        held = position_before.active_symbol if position_before and position_before.active_symbol else "current ETF"
        return f"FLIP — sell {held}, buy {alert.symbol}"
    if alert.alert_type == "SELL":
        return f"SELL {alert.symbol} — close your position"
    if kind == "blocked":
        return "NO TRADE — calendar blackout (no new entries today)"
    if kind == "holding":
        return f"HOLD {alert.symbol} — keep your position, do not add the other side"
    if kind == "entry_skipped_confidence":
        lead = _leading_etf(alert) or "mixed"
        min_c = technical_meta.min_confidence_to_trade if technical_meta else 62
        return (
            f"NO TRADE — {lead} setup not strong enough "
            f"({alert.confidence_score}% dominance, need >={min_c}%)"
        )
    if kind == "entry_skipped_high_conf":
        return (
            f"NO TRADE — dominance {alert.confidence_score}% is below "
            f"high-confidence mode (need >={HIGH_SIGNAL_QUALITY_THRESHOLD}%)"
        )
    return "NO TRADE — no position (no clear entry)"


def _embed_title(alert: AlertDecision) -> str:
    kind = alert.notes_kind
    if alert.alert_type == "BUY":
        return f"BUY {alert.symbol}"
    if alert.alert_type == "FLIP":
        return f"FLIP → {alert.symbol}"
    if alert.alert_type == "SELL":
        return f"SELL {alert.symbol}"
    if kind == "blocked":
        return f"{_NO_POSITION_LABEL} · Calendar"
    if kind == "holding":
        return f"HOLD {alert.symbol}"
    if kind in ("entry_skipped_confidence", "entry_skipped_high_conf"):
        lead = _leading_etf(alert)
        if lead:
            return f"{_NO_POSITION_LABEL} · Weak {lead}"
        return f"{_NO_POSITION_LABEL} · Low confidence"
    return _NO_POSITION_LABEL


def _embed_color(alert: AlertDecision) -> int:
    if alert.alert_type == "BUY":
        return _COLOR_BUY
    if alert.alert_type == "FLIP":
        return _COLOR_FLIP
    if alert.alert_type == "SELL":
        return _COLOR_SELL
    kind = alert.notes_kind
    if kind == "blocked":
        return _COLOR_BLOCKED
    if kind == "holding":
        return _COLOR_HOLD
    if kind in ("entry_skipped_confidence", "entry_skipped_high_conf"):
        return _COLOR_SKIP
    return _COLOR_CASH


def _summary_line(
    alert: AlertDecision,
    *,
    position_before: PositionState | None = None,
    technical_meta: RunTechnicalMeta | None = None,
) -> str:
    kind = alert.notes_kind
    lead = _leading_etf(alert)
    bull, bear = alert.bullish_score, alert.bearish_score

    if alert.alert_type == "BUY":
        quality = f" ({alert.signal_quality})" if alert.signal_quality else ""
        return (
            f"QQQ looks {'bullish' if alert.symbol == 'TQQQ' else 'bearish'} "
            f"(checklist {bull}/{bear}, dominance {alert.confidence_score}%){quality}."
        )
    if alert.alert_type == "FLIP":
        held = position_before.active_symbol if position_before and position_before.active_symbol else "position"
        return f"Trend reversed — rotate from {held} to {alert.symbol}."
    if alert.alert_type == "SELL":
        detail = alert.notes.removeprefix("Exit: ").removesuffix(".") if kind == "exit" else alert.notes
        return f"Close {alert.symbol}: {detail}."
    if kind == "holding":
        extra = ""
        if alert.flip_suppressed:
            extra = " Opposite signal seen but flip blocked (whipsaw guard)."
        return f"Still in {alert.symbol}; no exit rule fired yet.{extra}"
    if kind == "blocked":
        return "Today is blocked for new entries (manual or event-risk calendar)."
    if kind == "entry_skipped_confidence":
        min_c = technical_meta.min_confidence_to_trade if technical_meta else 62
        if lead:
            return (
                f"Leans {lead} (checklist {bull}/{bear}) but dominance is only "
                f"{alert.confidence_score}% — below your {min_c}% minimum, so no buy."
            )
        return f"Mixed checklist ({bull}/{bear}); dominance {alert.confidence_score}% is below {min_c}%."
    if kind == "entry_skipped_high_conf":
        return (
            f"Would lean {lead or 'mixed'} but dominance {alert.confidence_score}% "
            f"is under the {HIGH_SIGNAL_QUALITY_THRESHOLD}% high-confidence bar."
        )
    if kind == "buy_bull" or kind == "buy_bear":
        return "Setup detected; see action line."
    return f"No actionable entry. Checklist {bull}/{bear}, dominance {alert.confidence_score}%."


def _checklist_summary(reasons: list[str], *, limit: int = 3, max_len: int = 400) -> str:
    if not reasons:
        return "(none)"
    lines = [f"• {_truncate(r, 120)}" for r in reasons[:limit]]
    if len(reasons) > limit:
        lines.append(f"• +{len(reasons) - limit} more")
    return _truncate("\n".join(lines), max_len)


def _trade_levels_value(alert: AlertDecision) -> str:
    return (
        f"Entry {alert.entry_zone_low:.2f}–{alert.entry_zone_high:.2f}\n"
        f"Stop {alert.stop_loss:.2f} · TP {alert.take_profit:.2f} · "
        f"Stretch {alert.stretch_take_profit:.2f}\n"
        f"Max hold {alert.max_hold_date or 'N/A'}"
    )


def _confidence_field(alert: Aler