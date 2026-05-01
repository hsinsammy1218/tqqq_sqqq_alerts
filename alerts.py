from __future__ import annotations

import json

import requests

from indicators import IndicatorSnapshot
from strategy import AlertDecision, PositionState, RunTechnicalMeta, hold_exit_summary_line, score_signals


def _action_line(alert: AlertDecision) -> str:
    if alert.alert_type == "BUY":
        return f"ACTION: BUY {alert.symbol}"
    if alert.alert_type == "FLIP":
        return f"ACTION: FLIP -> SELL current side and BUY {alert.symbol}"
    if alert.alert_type == "SELL":
        return f"ACTION: SELL {alert.symbol} (exit / close the position)"
    notes_low = alert.notes.lower()
    if "blocked" in notes_low or "blackout" in notes_low:
        return "ACTION: WAIT - no new buys today (event calendar)"
    if "holding" in notes_low:
        return (
            f"ACTION: HOLD {alert.symbol} - keep position; "
            "do not add TQQQ/SQQQ on the other side"
        )
    return "ACTION: WAIT - stay in cash (no new BUY signal)"


def _embed_title(alert: AlertDecision) -> str:
    if alert.alert_type == "BUY":
        return f"📈 Buy {alert.symbol}"
    if alert.alert_type == "FLIP":
        return f"🔁 Flip to {alert.symbol}"
    if alert.alert_type == "SELL":
        return f"📉 Sell {alert.symbol}"
    notes_low = alert.notes.lower()
    if "blocked" in notes_low or "blackout" in notes_low:
        return "📆 Wait (calendar)"
    if "holding" in notes_low:
        return f"⏸️ Hold {alert.symbol}"
    return "⏳ Wait (cash)"


def _embed_color(alert: AlertDecision) -> int:
    if alert.alert_type == "BUY":
        return 0x2ECC71
    if alert.alert_type == "FLIP":
        return 0x9B59B6
    if alert.alert_type == "SELL":
        return 0xE74C3C
    notes_low = alert.notes.lower()
    if "blocked" in notes_low or "blackout" in notes_low:
        return 0x95A5A6
    if "holding" in notes_low:
        return 0x3498DB
    return 0xF39C12


def _truncate(text: str, max_len: int) -> str:
    text = text.strip()
    if len(text) <= max_len:
        return text
    return text[: max_len - 3] + "..."


def _checklist_embed_value(reasons: list[str], max_len: int = 950) -> str:
    body = "\n".join(f"• {r}" for r in reasons) if reasons else "• (none)"
    return _truncate(body, max_len)


def build_discord_embed(
    alert: AlertDecision,
    *,
    snapshot: IndicatorSnapshot | None = None,
    position_before: PositionState | None = None,
    technical_meta: RunTechnicalMeta | None = None,
    today_iso: str | None = None,
) -> dict[str, object]:
    trend = _truncate(alert.qqq_trend_reason, 3800)
    notes = _truncate(alert.notes, 1024) or "-"

    description_lines = [
        f"**{_action_line(alert)}**",
        "",
        f"**QQQ trend:** {trend}",
        "",
        "_Alerts only — you place orders manually._",
    ]

    fields: list[dict[str, object]] = [
        {"name": "Alert", "value": alert.alert_type, "inline": True},
        {"name": "Symbol", "value": alert.symbol, "inline": True},
        {"name": "Confidence", "value": f"{alert.confidence_score}% (norm.)", "inline": True},
        {"name": "Bull strength", "value": f"{alert.bullish_score}/100", "inline": True},
        {"name": "Bear strength", "value": f"{alert.bearish_score}/100", "inline": True},
        {"name": "Time (UTC)", "value": alert.timestamp, "inline": True},
        {
            "name": "Entry zone (QQQ)",
            "value": f"{alert.entry_zone_low:.2f} – {alert.entry_zone_high:.2f}",
            "inline": False,
        },
        {"name": "Stop loss", "value": f"{alert.stop_loss:.2f}", "inline": True},
        {"name": "Take profit", "value": f"{alert.take_profit:.2f}", "inline": True},
        {"name": "Stretch", "value": f"{alert.stretch_take_profit:.2f}", "inline": True},
        {"name": "Max hold", "value": alert.max_hold_date or "N/A", "inline": True},
    ]

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
            persist_bits.append(f"last_signal `{position_before.last_signal}`")
        if position_before.updated_at:
            persist_bits.append(f"updated_at `{position_before.updated_at}`")
        if persist_bits:
            mem += "\n" + " · ".join(persist_bits)

        data_ctx = (
            f"Daily bar end: `{technical_meta.daily_bar_end}`\n"
            f"4h bar end: `{technical_meta.h4_bar_end}`\n"
            f"Regime: **{technical_meta.regime}**\n"
            f"Event blackout: **{'yes' if technical_meta.blocked_today else 'no'}**\n"
            f"VWAP anchor: {technical_meta.anchor_date_label}"
        )
        daily_txt = (
            f"**Close** {snapshot.daily_close:.2f} · **EMA20** {snapshot.daily_ema20:.2f} · "
            f"**EMA50** {snapshot.daily_ema50:.2f}\n"
            f"**RSI14** {snapshot.daily_rsi14:.1f} · **MACD** {snapshot.daily_macd:.3f} · "
            f"**sig** {snapshot.daily_macd_signal:.3f}\n"
            f"**ATR14** {snapshot.daily_atr14:.2f} · **wk VWAP** {snapshot.daily_weekly_vwap:.2f} · "
            f"**anch VWAP** {snapshot.daily_anchored_vwap:.2f}\n"
            f"**Vol** {snapshot.daily_volume:,.0f} · **vol SMA20** {snapshot.daily_vol_sma20:,.0f}"
        )
        h4_txt = (
            f"**Close** {snapshot.h4_close:.2f} · **EMA20** {snapshot.h4_ema20:.2f} · **EMA50** {snapshot.h4_ema50:.2f}"
        )
        rules = (
            f"TQQQ BUY (weighted): bull_sum≥{technical_meta.effective_bull_entry:.2f}, "
            f"bear_sum<{technical_meta.effective_bear_entry:.2f}\n"
            f"SQQQ BUY (weighted): bear_sum≥{technical_meta.effective_bear_entry:.2f}, "
            f"bull_sum<{technical_meta.effective_bull_entry:.2f}\n"
            f"Flat BUY minimum confidence: **{technical_meta.min_confidence_to_trade}%** "
            f"(below → CASH; does not apply to SELL/FLIP)\n"
            f"Exit while holding: weak / opposite / stop / TP / **{technical_meta.max_hold_days}** trading-day max hold "
            f"(flip suppression may apply)\n"
            f"Range chop FLIPs: **{'allowed' if technical_meta.flip_in_range_regime else 'off'}** "
            f"(when off, reversals in range use exits only)\n"
            f"Weak: TQQQ bull_sum<{technical_meta.effective_weak:.2f} · "
            f"SQQQ bear_sum<{technical_meta.effective_weak:.2f}\n"
            f"Risk %: stop {technical_meta.stop_loss_pct:.0%}, TP {technical_meta.take_profit_pct:.0%}, "
            f"stretch {technical_meta.stretch_take_profit_pct:.0%} · entry zone ±{technical_meta.entry_atr_multiplier}×ATR14"
        )

        fields.extend(
            [
                {"name": "Bot memory (before this alert)", "value": _truncate(mem, 1024), "inline": False},
                {"name": "Data context", "value": _truncate(data_ctx, 1024), "inline": False},
                {"name": "QQQ — daily snapshot", "value": _truncate(daily_txt, 1024), "inline": False},
                {"name": "QQQ — 4h snapshot", "value": _truncate(h4_txt, 1024), "inline": False},
                {
                    "name": f"Bull checklist (strength {bull}/100)",
                    "value": _checklist_embed_value(bull_reasons),
                    "inline": False,
                },
                {
                    "name": f"Bear checklist (strength {bear}/100)",
                    "value": _checklist_embed_value(bear_reasons),
                    "inline": False,
                },
                {"name": "Rule thresholds", "value": _truncate(rules, 1024), "inline": False},
            ]
        )
        exit_line = hold_exit_summary_line(snapshot, alert, position_before, technical_meta, today_iso)
        if exit_line:
            fields.append(
                {
                    "name": "Hold exit flags (QQQ vs your levels)",
                    "value": _truncate(exit_line, 1024),
                    "inline": False,
                }
            )

    fields.append({"name": "Notes", "value": notes, "inline": False})

    embed: dict[str, object] = {
        "title": _embed_title(alert),
        "description": "\n".join(description_lines),
        "color": _embed_color(alert),
        "fields": fields,
        "footer": {"text": "QQQ swing alerts • TQQQ / SQQQ"},
    }

    if alert.timestamp:
        embed["timestamp"] = alert.timestamp

    return embed


def format_alert_message(alert: AlertDecision) -> str:
    return (
        f"{_action_line(alert)}\n"
        f"(You trade manually - alerts only, no broker execution.)\n"
        f"\n"
        f"Alert: {alert.alert_type}\n"
        f"Symbol: {alert.symbol}\n"
        f"QQQ trend: {alert.qqq_trend_reason}\n"
        f"Bullish strength: {alert.bullish_score}/100 (weighted checklist)\n"
        f"Bearish strength: {alert.bearish_score}/100 (weighted checklist)\n"
        f"Confidence (normalized): {alert.confidence_score}%\n"
        f"Entry zone: {alert.entry_zone_low:.2f} - {alert.entry_zone_high:.2f}\n"
        f"Stop loss: {alert.stop_loss:.2f}\n"
        f"Take profit: {alert.take_profit:.2f}\n"
        f"Stretch target: {alert.stretch_take_profit:.2f}\n"
        f"Max hold date: {alert.max_hold_date or 'N/A'}\n"
        f"Timestamp: {alert.timestamp}\n"
        f"Notes: {alert.notes}"
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
