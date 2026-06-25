from __future__ import annotations

import json
import re

import requests

from indicators import IndicatorSnapshot
from strategy import AlertDecision, PositionState, RunTechnicalMeta, hold_exit_summary_line, score_signals
from strategy_types import HIGH_SIGNAL_QUALITY_THRESHOLD


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
    return "NO TRADE — stay in cash (no clear entry)"


def _embed_title(alert: AlertDecision) -> str:
    kind = alert.notes_kind
    if alert.alert_type == "BUY":
        return f"Buy {alert.symbol}"
    if alert.alert_type == "FLIP":
        return f"Flip to {alert.symbol}"
    if alert.alert_type == "SELL":
        return f"Sell {alert.symbol}"
    if kind == "blocked":
        return "No trade (calendar)"
    if kind == "holding":
        return f"Hold {alert.symbol}"
    if kind in ("entry_skipped_confidence", "entry_skipped_high_conf"):
        lead = _leading_etf(alert)
        if lead:
            return f"No trade (weak {lead} signal)"
        return "No trade (low dominance)"
    return "No trade (cash)"


def _embed_color(alert: AlertDecision) -> int:
    if alert.alert_type == "BUY":
        return 0x2ECC71
    if alert.alert_type == "FLIP":
        return 0x9B59B6
    if alert.alert_type == "SELL":
        return 0xE74C3C
    kind = alert.notes_kind
    if kind == "blocked":
        return 0x95A5A6
    if kind == "holding":
        return 0x3498DB
    if kind in ("entry_skipped_confidence", "entry_skipped_high_conf"):
        return 0xE67E22
    return 0xF39C12


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


def _checklist_embed_value(reasons: list[str], max_len: int = 950) -> str:
    body = "\n".join(f"• {r}" for r in reasons) if reasons else "• (none)"
    return _truncate(body, max_len)


def _trade_levels_value(alert: AlertDecision) -> str:
    return (
        f"**QQQ entry zone:** {alert.entry_zone_low:.2f} – {alert.entry_zone_high:.2f}\n"
        f"**Stop:** {alert.stop_loss:.2f} · **TP:** {alert.take_profit:.2f} · "
        f"**Stretch:** {alert.stretch_take_profit:.2f}\n"
        f"**Max hold date:** {alert.max_hold_date or 'N/A'}"
    )


def _dominance_field(alert: AlertDecision, technical_meta: RunTechnicalMeta | None) -> str:
    min_c = technical_meta.min_confidence_to_trade if technical_meta else None
    if min_c is not None and alert.alert_type == "CASH":
        status = "OK for entry" if alert.confidence_score >= min_c else f"need >={min_c}% to buy"
        return f"**{alert.confidence_score}%** ({status})"
    return f"**{alert.confidence_score}%**"


def build_discord_embed(
    alert: AlertDecision,
    *,
    snapshot: IndicatorSnapshot | None = None,
    position_before: PositionState | None = None,
    technical_meta: RunTechnicalMeta | None = None,
    today_iso: str | None = None,
) -> dict[str, object]:
    trend = _truncate(alert.qqq_trend_reason, 3800)
    notes = _truncate(alert.notes, 1024) or "—"
    action = _action_line(alert, position_before=position_before, technical_meta=technical_meta)
    summary = _summary_line(alert, position_before=position_before, technical_meta=technical_meta)

    position_label = "Flat"
    if alert.alert_type in ("BUY", "FLIP"):
        position_label = f"Opening {alert.symbol}"
    elif alert.alert_type == "SELL":
        position_label = f"Closing {alert.symbol}"
    elif alert.notes_kind == "holding":
        position_label = f"Holding {alert.symbol}"
    elif alert.symbol not in ("CASH", ""):
        position_label = alert.symbol

    description_lines = [
        f"**{action}**",
        "",
        summary,
        "",
        f"**Regime:** {_regime_from_trend(trend)} · **Checklist:** bull {alert.bullish_score} / bear {alert.bearish_score}",
        "",
        "_Alerts only — you place orders manually in your broker._",
    ]

    fields: list[dict[str, object]] = [
        {"name": "Signal", "value": alert.alert_type, "inline": True},
        {"name": "Position", "value": position_label, "inline": True},
        {
            "name": "Stack dominance",
            "value": _dominance_field(alert, technical_meta),
            "inline": True,
        },
    ]

    if alert.signal_quality:
        fields.append({"name": "Quality", "value": alert.signal_quality, "inline": True})

    fields.append({"name": "Why", "value": notes, "inline": False})

    if _show_trade_levels(alert):
        fields.append(
            {
                "name": "Levels (QQQ-based)",
                "value": _truncate(_trade_levels_value(alert), 1024),
                "inline": False,
            }
        )

    fields.append(
        {
            "name": "QQQ trend (detail)",
            "value": trend,
            "inline": False,
        }
    )
    fields.append({"name": "Time (UTC)", "value": alert.timestamp or "—", "inline": True})

    enriched = (
        snapshot is not None
        and technical_meta is not None
        and position_before is not None
        and today_iso is not None
    )
    if enriched:
        bull, bear, bull_reasons, bear_reasons = score_signals(snapshot, technical_meta.score_weights)

        if position_before.active_symbol:
            mem = position_before.active_symbol
            if position_before.entry_price is not None:
                mem += f" @ {position_before.entry_price:g}"
            if position_before.entry_timestamp:
                mem += f" · since `{position_before.entry_timestamp}`"
        else:
            mem = "Flat — no TQQQ/SQQQ in bot memory"

        persist_bits = []
        if position_before.last_signal:
            persist_bits.append(f"last `{position_before.last_signal}`")
        if position_before.updated_at:
            persist_bits.append(f"updated `{position_before.updated_at}`")
        if persist_bits:
            mem += "\n" + " · ".join(persist_bits)

        daily_txt = (
            f"Close {snapshot.daily_close:.2f} · EMA20 {snapshot.daily_ema20:.2f} · "
            f"EMA50 {snapshot.daily_ema50:.2f} · RSI {snapshot.daily_rsi14:.1f}"
        )
        h4_txt = (
            f"Close {snapshot.h4_close:.2f} · EMA20 {snapshot.h4_ema20:.2f} · "
            f"EMA50 {snapshot.h4_ema50:.2f}"
        )
        rules = (
            f"Buy TQQQ: bull stack >={technical_meta.effective_bull_entry:.1f}, bear below bear entry · "
            f"Buy SQQQ: bear stack >={technical_meta.effective_bear_entry:.1f}, bull below bull entry · "
            f"Flat buy needs dominance >={technical_meta.min_confidence_to_trade}% · "
            f"Max hold {technical_meta.max_hold_days} trading days"
        )

        fields.extend(
            [
                {"name": "Bot memory (before alert)", "value": _truncate(mem, 1024), "inline": False},
                {"name": "QQQ daily", "value": _truncate(daily_txt, 1024), "inline": True},
                {"name": "QQQ 4h", "value": _truncate(h4_txt, 1024), "inline": True},
                {
                    "name": f"Bull checklist ({bull}/100)",
                    "value": _checklist_embed_value(bull_reasons),
                    "inline": False,
                },
                {
                    "name": f"Bear checklist ({bear}/100)",
                    "value": _checklist_embed_value(bear_reasons),
                    "inline": False,
                },
                {"name": "Rules (reference)", "value": _truncate(rules, 1024), "inline": False},
            ]
        )
        exit_line = hold_exit_summary_line(snapshot, alert, position_before, technical_meta, today_iso)
        if exit_line:
            fields.append(
                {
                    "name": "Exit flags (if holding)",
                    "value": _truncate(exit_line.replace("weaken=", "weakened=").replace("_hit=", " "), 1024),
                    "inline": False,
                }
            )

    embed: dict[str, object] = {
        "title": _embed_title(alert),
        "description": "\n".join(description_lines),
        "color": _embed_color(alert),
        "fields": fields,
        "footer": {"text": "QQQ swing alerts · TQQQ / SQQQ · not financial advice"},
    }

    if alert.timestamp:
        embed["timestamp"] = alert.timestamp

    return embed


def _regime_from_trend(trend: str) -> str:
    m = re.search(r"regime=(\w+)", trend)
    return m.group(1) if m else "—"


def format_alert_message(
    alert: AlertDecision,
    *,
    position_before: PositionState | None = None,
    technical_meta: RunTechnicalMeta | None = None,
) -> str:
    action = _action_line(alert, position_before=position_before, technical_meta=technical_meta)
    summary = _summary_line(alert, position_before=position_before, technical_meta=technical_meta)
    sq_line = f"Signal quality: {alert.signal_quality}\n" if alert.signal_quality else ""
    levels = ""
    if _show_trade_levels(alert):
        levels = (
            f"Entry zone (QQQ): {alert.entry_zone_low:.2f} - {alert.entry_zone_high:.2f}\n"
            f"Stop loss: {alert.stop_loss:.2f}\n"
            f"Take profit: {alert.take_profit:.2f}\n"
            f"Stretch target: {alert.stretch_take_profit:.2f}\n"
            f"Max hold date: {alert.max_hold_date or 'N/A'}\n"
        )
    return (
        f"{action}\n"
        f"{summary}\n"
        f"(You trade manually - alerts only, no broker execution.)\n"
        f"\n"
        f"Alert: {alert.alert_type}\n"
        f"Symbol: {alert.symbol}\n"
        f"QQQ trend: {alert.qqq_trend_reason}\n"
        f"Bullish strength: {alert.bullish_score}/100 (weighted checklist)\n"
        f"Bearish strength: {alert.bearish_score}/100 (weighted checklist)\n"
        f"Stack dominance: {alert.confidence_score}%\n"
        f"{sq_line}"
        f"{levels}"
        f"Timestamp: {alert.timestamp}\n"
        f"Why: {alert.notes}"
    )


def send_discord(
    webhook_url: str,
    alert: AlertDecision,
    dry_run: bool,
    *,
    snapshot: IndicatorSnapshot | None = None,
    position_before: PositionState | None = None,
    technical_meta: RunTechnicalMeta | None = None,
    today_iso: str | None = None,
) -> None:
    embed = build_discord_embed(
        alert,
        snapshot=snapshot,
        position_before=position_before,
        technical_meta=technical_meta,
        today_iso=today_iso,
    )
    payload: dict[str, object] = {
        "username": "QQQ Swing Alerts",
        "embeds": [embed],
    }

    if dry_run:
        print("[DRY RUN] Discord payload:")
        print(json.dumps(payload, indent=2))
        return
    if not webhook_url:
        print("[Live] No DISCORD_WEBHOOK_URL - skipping Discord (journal + console only).")
        return

    response = requests.post(webhook_url, json=payload, timeout=15)
    if response.status_code >= 400:
        raise RuntimeError(f"Discord webhook failed: {response.status_code} {response.text}")


def send_discord_api_quota_alert(
    webhook_url: str,
    *,
    detail: str,
    timestamp: str,
    dry_run: bool,
) -> None:
    embed: dict[str, object] = {
        "title": "KlickAnalytics monthly limit reached",
        "description": (
            "Market data could not be fetched because the KlickAnalytics API "
            "monthly usage limit appears to be exhausted.\n\n"
            f"**Detail:** {_truncate(detail, 900)}"
        ),
        "color": 0xE74C3C,
        "footer": {"text": "QQQ swing alerts · bot system notice"},
    }
    if timestamp:
        embed["timestamp"] = timestamp

    payload: dict[str, object] = {
        "username": "QQQ Swing Alerts",
        "embeds": [embed],
    }

    if dry_run:
        print("[DRY RUN] Discord API quota payload:")
        print(json.dumps(payload, indent=2))
        return
    if not webhook_url:
        print("[Live] No DISCORD_WEBHOOK_URL - skipping API quota Discord notice.")
        return

    response = requests.post(webhook_url, json=payload, timeout=15)
    if response.status_code >= 400:
        raise RuntimeError(f"Discord webhook failed: {response.status_code} {response.text}")
