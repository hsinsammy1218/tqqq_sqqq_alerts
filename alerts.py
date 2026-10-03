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


def _confidence_field(alert: AlertDecision, technical_meta: RunTechnicalMeta | None) -> str:
    min_c = technical_meta.min_confidence_to_trade if technical_meta else None
    base = f"{alert.confidence_score}%"
    if alert.signal_quality:
        base = f"{base} · {alert.signal_quality}"
    if min_c is not None and alert.alert_type == "CASH" and alert.notes_kind.startswith("entry_skipped"):
        return f"{base}\n(need ≥{min_c}% to buy)"
    return base


def _regime_label(alert: AlertDecision, technical_meta: RunTechnicalMeta | None) -> str:
    if technical_meta is not None and technical_meta.regime:
        return technical_meta.regime
    return _regime_from_trend(alert.qqq_trend_reason)


def _symbol_field(alert: AlertDecision) -> str:
    if alert.alert_type in ("BUY", "SELL", "FLIP"):
        return alert.symbol
    if alert.notes_kind == "holding" and alert.symbol not in ("CASH", ""):
        return alert.symbol
    lead = _leading_etf(alert)
    if lead:
        return f"{_NO_POSITION_LABEL} (lean {lead})"
    return _NO_POSITION_LABEL


def _reason_field(alert: AlertDecision) -> str:
    notes = (alert.notes or "").strip()
    if not notes:
        return "—"
    # Prefer a single short sentence; strip redundant "Exit:" prefix already familiar from title.
    cleaned = notes.removeprefix("Exit: ").strip()
    return _truncate(cleaned, 280)


def format_paper_results_summary(paper_results: Sequence[Any] | None) -> str | None:
    """Compact one/two-line paper-order outcome for Discord (None if nothing to show)."""
    if not paper_results:
        return None
    parts: list[str] = []
    for result in paper_results:
        intent = getattr(result, "intent", None)
        symbol = getattr(intent, "symbol", "?") if intent is not None else "?"
        side = getattr(intent, "side", "?") if intent is not None else "?"
        purpose = getattr(intent, "purpose", "") if intent is not None else ""
        ok = bool(getattr(result, "ok", False))
        status = str(getattr(result, "status", "") or ("ok" if ok else "failed"))
        order_id = getattr(result, "order_id", None)
        mark = "ok" if ok else "fail"
        bit = f"{mark}: {side.upper()} {symbol}"
        if purpose:
            bit += f" ({purpose})"
        bit += f" · {status}"
        if order_id:
            bit += f" · `{order_id}`"
        parts.append(bit)
    return _truncate("\n".join(parts), 500)


def build_discord_embed(
    alert: AlertDecision,
    *,
    snapshot: IndicatorSnapshot | None = None,
    position_before: PositionState | None = None,
    technical_meta: RunTechnicalMeta | None = None,
    today_iso: str | None = None,
    paper_results: Sequence[Any] | None = None,
    preview: bool = False,
) -> dict[str, object]:
    action = _action_line(alert, position_before=position_before, technical_meta=technical_meta)
    summary = _summary_line(alert, position_before=position_before, technical_meta=technical_meta)
    regime = _regime_label(alert, technical_meta)

    description_lines = [
        f"**{action}**",
        "",
        summary,
        "",
        "_Manual trade · not broker advice_",
    ]
    if preview:
        description_lines.append("_Preview — no new paper orders_")

    fields: list[dict[str, object]] = [
        {"name": "Symbol", "value": _symbol_field(alert), "inline": True},
        {"name": "Confidence", "value": _confidence_field(alert, technical_meta), "inline": True},
        {"name": "Regime", "value": regime, "inline": True},
        {
            "name": "Strength",
            "value": f"Bull {alert.bullish_score} · Bear {alert.bearish_score}",
            "inline": True,
        },
    ]

    if _show_trade_levels(alert):
        fields.append(
            {
                "name": "Levels (QQQ)",
                "value": _truncate(_trade_levels_value(alert), 1024),
                "inline": False,
            }
        )

    fields.append({"name": "Reason", "value": _reason_field(alert), "inline": False})

    enriched = (
        snapshot is not None
        and technical_meta is not None
        and position_before is not None
        and today_iso is not None
    )
    if enriched:
        _bull, _bear, bull_reasons, bear_reasons = score_signals(snapshot, technical_meta.score_weights)
        lead = _leading_etf(alert)
        if lead == "TQQQ" or (lead is None and alert.bullish_score >= alert.bearish_score):
            checklist_name = "Checklist (bull)"
            checklist_value = _checklist_summary(bull_reasons)
        else:
            checklist_name = "Checklist (bear)"
            checklist_value = _checklist_summary(bear_reasons)
        fields.append({"name": checklist_name, "value": checklist_value, "inline": False})

        qqq_line = (
            f"D {snapshot.daily_close:.2f} · EMA20 {snapshot.daily_ema20:.2f} / "
            f"{snapshot.daily_ema50:.2f} · RSI {snapshot.daily_rsi14:.1f}"
        )
        fields.append({"name": "QQQ", "value": _truncate(qqq_line, 256), "inline": False})

        if position_before.active_symbol:
            mem = position_before.active_symbol
            if position_before.entry_price is not None:
                mem += f" @ {position_before.entry_price:g}"
            fields.append({"name": "Bot memory", "value": _truncate(mem, 256), "inline": True})

        exit_line = hold_exit_summary_line(snapshot, alert, position_before, technical_meta, today_iso)
        if exit_line:
            fields.append(
                {
                    "name": "Exit flags",
                    "value": _truncate(
                        exit_line.replace("weaken=", "weakened=").replace("_hit=", " "),
                        400,
                    ),
                    "inline": False,
                }
            )

    paper_line = format_paper_results_summary(paper_results)
    if paper_line:
        fields.append({"name": "Paper", "value": paper_line, "inline": False})

    footer_text = "QQQ swing alerts · TQQQ / SQQQ"
    if preview:
        footer_text += " · preview"
    else:
        footer_text += " · not financial advice"

    embed: dict[str, object] = {
        "title": _embed_title(alert),
        "description": "\n".join(description_lines),
        "color": _embed_color(alert),
        "fields": fields,
        "footer": {"text": footer_text},
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
        f"Symbol: {_display_symbol(alert.symbol)}\n"
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
    paper_results: Sequence[Any] | None = None,
    preview: bool = False,
) -> None:
    embed = build_discord_embed(
        alert,
        snapshot=snapshot,
        position_before=position_before,
        technical_meta=technical_meta,
        today_iso=today_iso,
        paper_results=paper_results,
        preview=preview,
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


def send_discord_kill_alert(
    webhook_url: str,
    *,
    reason: str,
    dry_run: bool,
    equity: float | None = None,
    daily_pnl: float | None = None,
    weekly_pnl: float | None = None,
) -> None:
    """Post a conspicuous KILL embed when paper/live loss caps trip. Never raises."""
    lines = [
        "**KILL** — paper risk kill switch tripped.",
        f"Reason: {reason}",
        "New buys blocked for this run; sells (flatten) still allowed.",
        "Pause cron / set `ALPACA_PAPER_TRADING=false` if you need a hard stop.",
    ]
    if equity is not None:
        lines.append(f"Equity: ${equity:,.2f}")
    if daily_pnl is not None:
        lines.append(f"Daily PnL: ${daily_pnl:,.2f}")
    if weekly_pnl is not None:
        lines.append(f"Weekly PnL: ${weekly_pnl:,.2f}")
    embed = {
        "title": "KILL — paper risk switch",
        "description": "\n".join(lines),
        "color": 0xE74C3C,
    }
    payload: dict[str, object] = {
        "username": "QQQ Swing Alerts",
        "content": "KILL",
        "embeds": [embed],
    }
    if dry_run:
        print("[DRY RUN] Discord KILL payload:")
        print(json.dumps(payload, indent=2))
        return
    if not webhook_url:
        print("[paper-risk] No DISCORD_WEBHOOK_URL — KILL logged to console only.")
        return
    try:
        response = requests.post(webhook_url, json=payload, timeout=15)
        if response.status_code >= 400:
            print(
                f"[paper-risk] Discord KILL webhook failed: "
                f"{response.status_code} {response.text}"
            )
    except requests.RequestException as exc:
        print(f"[paper-risk] Discord KILL webhook error: {exc}")


def _extract_json_object(text: str) -> dict[str, object] | None:
    idx = text.find("{")
    if idx < 0:
        return None
    try:
        obj, _end = json.JSONDecoder().raw_decode(text[idx:])
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None


def _parse_api_quota_detail(detail: str) -> dict[str, str | int | None]:
    parsed: dict[str, str | int | None] = {
        "message": "Monthly API usage limit reached.",
        "monthly_limit": None,
        "total_hits": None,
        "error_code": None,
    }
    payload = _extract_json_object(detail)
    if payload:
        code = payload.get("error_code")
        if isinstance(code, str) and code:
            parsed["error_code"] = code
        for key in ("stderr", "stdout", "message"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                parsed["message"] = value.strip()
                break
        data = payload.get("data")
        if isinstance(data, dict):
            for src, dst in (("monthly_limit", "monthly_limit"), ("total_hits", "total_hits")):
                raw = data.get(src)
                if isinstance(raw, int):
                    parsed[dst] = raw
                elif isinstance(raw, str) and raw.isdigit():
                    parsed[dst] = int(raw)
    else:
        for line in detail.splitlines():
            clean = line.strip()
            if clean and not clean.startswith("HTTPError:") and not clean.startswith("Endpoint:"):
                parsed["message"] = clean
                break
    return parsed


parse_api_quota_detail = _parse_api_quota_detail


def _usage_meter(used: int, limit: int) -> str:
    if limit <= 0:
        return f"**{used}** calls used"
    pct = min(100, round(100 * used / limit))
    filled = pct // 10
    bar = "\u2588" * filled + "\u2591" * (10 - filled)
    return f"`{bar}`  **{used:,}** / **{limit:,}**  ({pct}%)"


def build_api_quota_discord_embed(detail: str, timestamp: str) -> dict[str, object]:
    info = _parse_api_quota_detail(detail)
    limit = info["monthly_limit"]
    hits = info["total_hits"]
    message = str(info["message"] or "Monthly API usage limit reached.")

    description_lines = [
        "**Data feed paused** — Alpaca market-data rate/quota limit is blocking fetches.",
        "",
        "Scheduled runs can't pull fresh QQQ bars until the limit clears. "
        "Bot memory and your journal are unchanged; only new alerts are blocked.",
    ]

    fields: list[dict[str, object]] = []
    if isinstance(limit, int) and isinstance(hits, int):
        fields.append(
            {
                "name": "API usage this month",
                "value": _usage_meter(hits, limit),
                "inline": False,
            }
        )
    else:
        fields.append({"name": "Status", "value": _truncate(message, 256), "inline": False})

    fields.extend(
        [
            {
                "name": "Quota resets",
                "value": "When Alpaca rate limits clear (or next calendar month for soft budgets)",
                "inline": True,
            },
            {
                "name": "Alerts",
                "value": "Paused until data fetch succeeds",
                "inline": True,
            },
            {
                "name": "What you can do",
                "value": (
                    "\u2022 Wait for the rate limit to clear, or review your Alpaca data plan\n"
                    "\u2022 Trade manually from your broker if you still hold TQQQ/SQQQ\n"
                    "\u2022 Re-run after reset: `python main.py --health-check`"
                ),
                "inline": False,
            },
        ]
    )

    if info["error_code"]:
        fields.append(
            {
                "name": "Error code",
                "value": f"`{info['error_code']}`",
                "inline": True,
            }
        )

    fields.append(
        {
            "name": "Technical detail",
            "value": _truncate(detail.replace("`", "'"), 900),
            "inline": False,
        }
    )

    embed: dict[str, object] = {
        "title": "Alpaca market data limit reached",
        "description": "\n".join(description_lines),
        "color": 0xE67E22,
        "fields": fields,
        "footer": {"text": "QQQ swing alerts \u00b7 data provider notice"},
    }
    if timestamp:
        embed["timestamp"] = timestamp
    return embed


def send_discord_api_quota_alert(
    webhook_url: str,
    *,
    detail: str,
    timestamp: str,
    dry_run: bool,
) -> None:
    embed = build_api_quota_discord_embed(detail, timestamp)

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


def build_api_usage_warning_embed(
    *,
    total_calls: int,
    monthly_limit: int,
    warn_threshold: int,
    timestamp: str,
) -> dict[str, object]:
    remaining = max(0, monthly_limit - total_calls)
    pct = min(100, round(100 * total_calls / monthly_limit)) if monthly_limit else 0

    description_lines = [
        "**Heads up** — you're approaching the local Alpaca API soft budget.",
        "",
        "This count is tracked by the bot from runs on this machine "
        "(scheduled jobs, manual runs, health checks). "
        "Other Alpaca API use may not be included.",
    ]

    fields: list[dict[str, object]] = [
        {
            "name": "API usage this month",
            "value": _usage_meter(total_calls, monthly_limit),
            "inline": False,
        },
        {
            "name": "Warning level",
            "value": (
                f"**{warn_threshold:,}** calls "
                f"({round(100 * warn_threshold / monthly_limit)}% of limit)"
                if monthly_limit
                else f"**{warn_threshold:,}** calls"
            ),
            "inline": True,
        },
        {
            "name": "Remaining",
            "value": f"**~{remaining:,}** calls",
            "inline": True,
        },
        {
            "name": "What you can do",
            "value": (
                "\u2022 Avoid extra manual runs and health checks until reset\n"
                "\u2022 Skip `publish_technical_dashboard.py` if you use it\n"
                "\u2022 Review your Alpaca data plan if you need more headroom"
            ),
            "inline": False,
        },
    ]

    embed: dict[str, object] = {
        "title": "Alpaca usage warning",
        "description": "\n".join(description_lines),
        "color": 0xF1C40F,
        "fields": fields,
        "footer": {"text": "QQQ swing alerts \u00b7 data provider notice"},
    }
    if timestamp:
        embed["timestamp"] = timestamp
    return embed


def send_discord_api_usage_warning(
    webhook_url: str,
    *,
    total_calls: int,
    monthly_limit: int,
    warn_threshold: int,
    timestamp: str,
    dry_run: bool,
) -> None:
    embed = build_api_usage_warning_embed(
        total_calls=total_calls,
        monthly_limit=monthly_limit,
        warn_threshold=warn_threshold,
        timestamp=timestamp,
    )
    payload: dict[str, object] = {
        "username": "QQQ Swing Alerts",
        "embeds": [embed],
    }

    if dry_run:
        print("[DRY RUN] Discord API usage warning payload:")
        print(json.dumps(payload, indent=2))
        return
    if not webhook_url:
        print("[Live] No DISCORD_WEBHOOK_URL - skipping API usage warning.")
        return

    response = requests.post(webhook_url, json=payload, timeout=15)
    if response.status_code >= 400:
        raise RuntimeError(f"Discord webhook failed: {response.status_code} {response.text}")
