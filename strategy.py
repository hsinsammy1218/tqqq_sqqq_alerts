from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from indicators import IndicatorSnapshot


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
        notes = "Entry blocked by event calendar."

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
        entry_ts = position.entry_timestamp or ts
        max_hold_date = _trading_days_after(entry_ts, max_hold_days)
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

        if weaken or reverse or stop_hit or tp_hit or reached_max_hold:
            alert_type = "SELL"
            notes = "Exit rule triggered."
            new_position = PositionState()
        else:
            alert_type = "CASH"
            notes = "Holding active position."

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
