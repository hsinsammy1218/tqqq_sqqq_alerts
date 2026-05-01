from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from indicators import IndicatorSnapshot


def _fmt_px(value: float, decimals: int = 2) -> str:
    return f"{value:.{decimals}f}"


@dataclass
class PositionState:
    active_symbol: str | None = None
    entry_price: float | None = None
    entry_timestamp: str | None = None


@dataclass
class AlertDecision:
    alert_type: str
    symbol: str
    qqq_trend_reason: str
    bullish_score: int
    bearish_score: int
    confidence_score: int
    entry_zone_low: float
    entry_zone_high: float
    stop_loss: float
    take_profit: float
    stretch_take_profit: float
    max_hold_date: str
    timestamp: str
    notes: str = ""


def load_position(path: Path) -> PositionState:
    if not path.exists():
        return PositionState()
    data = json.loads(path.read_text(encoding="utf-8"))
    return PositionState(
        active_symbol=data.get("active_symbol"),
        entry_price=data.get("entry_price"),
        entry_timestamp=data.get("entry_timestamp"),
    )


def save_position(path: Path, position: PositionState) -> None:
    path.write_text(json.dumps(asdict(position), indent=2), encoding="utf-8")


def _load_blocked_dates(path: Path) -> set[str]:
    if not path.exists():
        return set()
    try:
        data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return set()
    return set(data.get("blocked_dates", []))


def _trading_days_after(date_str: str, days: int) -> str:
    start = datetime.fromisoformat(date_str.replace("Z", "+00:00")).date()
    date = start
    count = 0
    while count < days:
        date += timedelta(days=1)
        if date.weekday() < 5:
            count += 1
    return date.isoformat()


def score_signals(s: IndicatorSnapshot) -> tuple[int, int, list[str], list[str]]:
    bull = 0
    bear = 0
    bull_reasons: list[str] = []
    bear_reasons: list[str] = []

    def add(cond: bool, side: str, text: str) -> None:
        nonlocal bull, bear
        if cond:
            if side == "bull":
                bull += 1
                bull_reasons.append(text)
            else:
                bear += 1
                bear_reasons.append(text)

    add(s.daily_close > s.daily_ema20, "bull", "daily close > EMA20")
    add(s.daily_ema20 > s.daily_ema50, "bull", "daily EMA20 > EMA50")
    add(s.daily_rsi14 > 50, "bull", "daily RSI > 50")
    add(s.daily_macd > s.daily_macd_signal, "bull", "MACD > signal")
    add(s.h4_close > s.h4_ema20, "bull", "4h close > EMA20")
    add(s.h4_ema20 > s.h4_ema50, "bull", "4h EMA20 > EMA50")
    add(s.daily_close > s.daily_weekly_vwap, "bull", "price > weekly VWAP")
    add(s.daily_volume > s.daily_vol_sma20, "bull", "volume > 20 avg")

    add(s.daily_close < s.daily_ema20, "bear", "daily close < EMA20")
    add(s.daily_ema20 < s.daily_ema50, "bear", "daily EMA20 < EMA50")
    add(s.daily_rsi14 < 50, "bear", "daily RSI < 50")
    add(s.daily_macd < s.daily_macd_signal, "bear", "MACD < signal")
    add(s.h4_close < s.h4_ema20, "bear", "4h close < EMA20")
    add(s.h4_ema20 < s.h4_ema50, "bear", "4h EMA20 < EMA50")
    add(s.daily_close < s.daily_weekly_vwap, "bear", "price < weekly VWAP")
    add(s.daily_volume > s.daily_vol_sma20, "bear", "volume > 20 avg")
    return bull, bear, bull_reasons, bear_reasons


@dataclass(frozen=True)
class RunTechnicalMeta:
    """Console-only context for the technical breakdown block."""

    qqq_ticker: str
    run_utc_iso: str
    daily_bar_end: str | None
    h4_bar_end: str | None
    blocked_today: bool
    bull_entry_threshold: int
    bear_entry_threshold: int
    weak_threshold: int
    stop_loss_pct: float
    take_profit_pct: float
    stretch_take_profit_pct: float
    max_hold_days: int
    entry_atr_multiplier: float
    anchor_date_label: str


def format_technical_breakdown(
    snapshot: IndicatorSnapshot,
    alert: AlertDecision,
    position_before: PositionState,
    meta: RunTechnicalMeta,
    today_iso: str,
) -> str:
    bull, bear, bull_reasons, bear_reasons = score_signals(snapshot)
    lines: list[str] = [
        "--- Technical breakdown (inputs are QQQ; trades are TQQQ/SQQQ) ---",
        f"Ticker: {meta.qqq_ticker} | Run (UTC): {meta.run_utc_iso}",
        f"Bars: daily_last={meta.daily_bar_end or '?'} | h4_last={meta.h4_bar_end or '?'}",
        f"Event blackout today: {'yes' if meta.blocked_today else 'no'}",
        f"Anchored VWAP anchor: {meta.anchor_date_label}",
        "",
        "[QQQ - daily]",
        f"  close={_fmt_px(snapshot.daily_close)}  EMA20={_fmt_px(snapshot.daily_ema20)}  EMA50={_fmt_px(snapshot.daily_ema50)}",
        f"  RSI14={_fmt_px(snapshot.daily_rsi14, 1)}  MACD={_fmt_px(snapshot.daily_macd)}  signal={_fmt_px(snapshot.daily_macd_signal)}",
        f"  ATR14={_fmt_px(snapshot.daily_atr14)}  weekly_VWAP={_fmt_px(snapshot.daily_weekly_vwap)}  anchored_VWAP={_fmt_px(snapshot.daily_anchored_vwap)}",
        f"  volume={_fmt_px(snapshot.daily_volume, 0)}  vs vol_SMA20={_fmt_px(snapshot.daily_vol_sma20, 0)}",
        "",
        "[QQQ - 4h last bar]",
        f"  close={_fmt_px(snapshot.h4_close)}  EMA20={_fmt_px(snapshot.h4_ema20)}  EMA50={_fmt_px(snapshot.h4_ema50)}",
        "",
        "[Score engine] (max 8 bull / 8 bear - dual-count volume regime)",
        f"  Bull {bull}/8 | Bear {bear}/8 | Confidence {alert.confidence_score}% (from |bull-bear|/8)",
        "  Bull checks:",
    ]
    if bull_reasons:
        lines.extend(f"    + {r}" for r in bull_reasons)
    else:
        lines.append("    (none)")
    lines.append("  Bear checks:")
    if bear_reasons:
        lines.extend(f"    + {r}" for r in bear_reasons)
    else:
        lines.append("    (none)")
    lines.extend(
        [
            "",
            "[Thresholds]",
            f"  BUY TQQQ: bull>={meta.bull_entry_threshold} AND bear<{meta.bear_entry_threshold} (flat, not blocked)",
            f"  BUY SQQQ: bear>={meta.bear_entry_threshold} AND bull<{meta.bull_entry_threshold} (flat, not blocked)",
            f"  SELL / exit while holding: weak trend OR opposite entry-level signal OR stop OR take-profit OR max hold ({meta.max_hold_days} trading days)",
            f"    weak if TQQQ: bull<{meta.weak_threshold}; weak if SQQQ: bear<{meta.weak_threshold}",
            f"    reverse if TQQQ held: bear>={meta.bear_entry_threshold}; reverse if SQQQ held: bull>={meta.bull_entry_threshold}",
            f"  Risk params: stop {meta.stop_loss_pct:.1%} | TP {meta.take_profit_pct:.1%} | stretch TP {meta.stretch_take_profit_pct:.1%}",
            f"  Entry zone (QQQ): close +/- {meta.entry_atr_multiplier}*ATR14 -> [{_fmt_px(alert.entry_zone_low)}, {_fmt_px(alert.entry_zone_high)}]",
            "",
            "[Bot position memory - before this decision]",
            f"  active_symbol={position_before.active_symbol!r}  entry_price={position_before.entry_price!r}  entry_timestamp={position_before.entry_timestamp!r}",
        ]
    )

    price = snapshot.daily_close
    sym = position_before.active_symbol
    if sym:
        entry_ref = float(position_before.entry_price or price)
        lines.extend(
            [
                "",
                "[Hold diagnostics - QQQ daily close vs levels from entry_ref]",
                f"  entry_ref (stored ETF avg or QQQ close proxy)={_fmt_px(entry_ref)}",
                f"  stop={_fmt_px(alert.stop_loss)}  TP={_fmt_px(alert.take_profit)}  stretch_TP={_fmt_px(alert.stretch_take_profit)}",
                f"  max_hold end date={alert.max_hold_date or '(n/a)'}  today={today_iso}",
            ]
        )
        reached_max = bool(alert.max_hold_date) and today_iso > alert.max_hold_date
        if sym == "TQQQ":
            weaken = bull < meta.weak_threshold
            reverse = bear >= meta.bear_entry_threshold
            stop_hit = price <= alert.stop_loss
            tp_hit = price >= alert.take_profit
        else:
            weaken = bear < meta.weak_threshold
            reverse = bull >= meta.bull_entry_threshold
            stop_hit = price >= alert.stop_loss
            tp_hit = price <= alert.take_profit
        lines.append("  Exit flags:")
        lines.append(f"    weaken={weaken}  opposite_signal={reverse}  stop_hit={stop_hit}  tp_hit={tp_hit}  past_max_hold={reached_max}")

    lines.extend(
        [
            "",
            "[This run]",
            f"  alert_type={alert.alert_type}  symbol={alert.symbol}",
            f"  headline reasons (dominant side, top 3): {alert.qqq_trend_reason}",
            f"  notes: {alert.notes}",
        ]
    )
    return "\n".join(lines)


def hold_exit_summary_line(
    snapshot: IndicatorSnapshot,
    alert: AlertDecision,
    position_before: PositionState,
    meta: RunTechnicalMeta,
    bull: int,
    bear: int,
    today_iso: str,
) -> str | None:
    sym = position_before.active_symbol
    if not sym:
        return None
    price = snapshot.daily_close
    reached_max = bool(alert.max_hold_date) and today_iso > alert.max_hold_date
    if sym == "TQQQ":
        weaken = bull < meta.weak_threshold
        reverse = bear >= meta.bear_entry_threshold
        stop_hit = price <= alert.stop_loss
        tp_hit = price >= alert.take_profit
    else:
        weaken = bear < meta.weak_threshold
        reverse = bull >= meta.bull_entry_threshold
        stop_hit = price >= alert.stop_loss
        tp_hit = price <= alert.take_profit
    return (
        f"weaken={weaken}, opposite={reverse}, stop_hit={stop_hit}, tp_hit={tp_hit}, past_max_hold={reached_max}"
    )


def decide(
    snapshot: IndicatorSnapshot,
    position: PositionState,
    blocked_dates: set[str],
    now_utc: datetime,
    bull_entry_threshold: int,
    bear_entry_threshold: int,
    weak_threshold: int,
    stop_loss_pct: float,
    take_profit_pct: float,
    stretch_take_profit_pct: float,
    max_hold_days: int,
    entry_atr_multiplier: float,
) -> tuple[AlertDecision, PositionState]:
    bull, bear, bull_reasons, bear_reasons = score_signals(snapshot)
    confidence = int(round((abs(bull - bear) / 8) * 100))
    ts = now_utc.replace(microsecond=0).isoformat().replace("+00:00", "Z")
    today = now_utc.date().isoformat()
    blocked = today in blocked_dates

    price = snapshot.daily_close
    atr = snapshot.daily_atr14
    entry_low = price - (entry_atr_multiplier * atr)
    entry_high = price + (entry_atr_multiplier * atr)

    dominant_reasons = bull_reasons[:3] if bull >= bear else bear_reasons[:3]
    reason = "; ".join(dominant_reasons) if dominant_reasons else "mixed conditions"
    symbol = "CASH"
    alert_type = "CASH"
    notes = "No high-confidence setup."

    if position.active_symbol is None and not blocked:
        if bull >= bull_entry_threshold and bear < bear_entry_threshold:
            symbol = "TQQQ"
            alert_type = "BUY"
            notes = "Bullish QQQ setup."
        elif bear >= bear_entry_threshold and bull < bull_entry_threshold:
            symbol = "SQQQ"
            alert_type = "BUY"
            notes = "Bearish QQQ setup."
    elif position.active_symbol is None and blocked:
        notes = "Entry blocked by event calendar (manual blackout + optional CPI/FOMC/earnings risk dates)."

    stop_loss = 0.0
    take_profit = 0.0
    stretch_tp = 0.0
    max_hold_date = ""
    new_position = PositionState(
        active_symbol=position.active_symbol,
        entry_price=position.entry_price,
        entry_timestamp=position.entry_timestamp,
    )

    if alert_type == "BUY":
        entry_price = price
        if symbol == "TQQQ":
            stop_loss = entry_price * (1 - stop_loss_pct)
            take_profit = entry_price * (1 + take_profit_pct)
            stretch_tp = entry_price * (1 + stretch_take_profit_pct)
        else:
            stop_loss = entry_price * (1 + stop_loss_pct)
            take_profit = entry_price * (1 - take_profit_pct)
            stretch_tp = entry_price * (1 - stretch_take_profit_pct)
        max_hold_date = _trading_days_after(ts, max_hold_days)
        new_position = PositionState(active_symbol=symbol, entry_price=entry_price, entry_timestamp=ts)
    elif position.active_symbol:
        symbol = position.active_symbol
        entry_price = float(position.entry_price or price)
        resolved_entry_ts = position.entry_timestamp or ts
        max_hold_date = _trading_days_after(resolved_entry_ts, max_hold_days)
        reached_max_hold = now_utc.date().isoformat() > max_hold_date

        if symbol == "TQQQ":
            stop_loss = entry_price * (1 - stop_loss_pct)
            take_profit = entry_price * (1 + take_profit_pct)
            stretch_tp = entry_price * (1 + stretch_take_profit_pct)
            weaken = bull < weak_threshold
            reverse = bear >= bear_entry_threshold
            stop_hit = price <= stop_loss
            tp_hit = price >= take_profit
        else:
            stop_loss = entry_price * (1 + stop_loss_pct)
            take_profit = entry_price * (1 - take_profit_pct)
            stretch_tp = entry_price * (1 - stretch_take_profit_pct)
            weaken = bear < weak_threshold
            reverse = bull >= bull_entry_threshold
            stop_hit = price >= stop_loss
            tp_hit = price <= take_profit

        if reverse:
            flip_to = "SQQQ" if symbol == "TQQQ" else "TQQQ"
            alert_type = "FLIP"
            symbol = flip_to
            notes = f"Reverse signal: sell {position.active_symbol} and buy {flip_to}."
            max_hold_date = _trading_days_after(ts, max_hold_days)
            new_position = PositionState(active_symbol=flip_to, entry_price=price, entry_timestamp=ts)
            if flip_to == "TQQQ":
                stop_loss = price * (1 - stop_loss_pct)
                take_profit = price * (1 + take_profit_pct)
                stretch_tp = price * (1 + stretch_take_profit_pct)
            else:
                stop_loss = price * (1 + stop_loss_pct)
                take_profit = price * (1 - take_profit_pct)
                stretch_tp = price * (1 - stretch_take_profit_pct)
        elif weaken or stop_hit or tp_hit or reached_max_hold:
            alert_type = "SELL"
            notes = "Exit rule triggered."
            new_position = PositionState()
        else:
            alert_type = "CASH"
            notes = "Holding active position."
            new_position = PositionState(
                active_symbol=symbol,
                entry_price=position.entry_price,
                entry_timestamp=resolved_entry_ts,
            )

    decision = AlertDecision(
        alert_type=alert_type,
        symbol=symbol,
        qqq_trend_reason=reason,
        bullish_score=bull,
        bearish_score=bear,
        confidence_score=confidence,
        entry_zone_low=entry_low,
        entry_zone_high=entry_high,
        stop_loss=stop_loss,
        take_profit=take_profit,
        stretch_take_profit=stretch_tp,
        max_hold_date=max_hold_date,
        timestamp=ts,
        notes=notes,
    )
    return decision, new_position


def load_blocked_dates(path: Path) -> set[str]:
    return _load_blocked_dates(path)
