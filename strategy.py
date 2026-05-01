from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from indicators import IndicatorSnapshot
from strategy_params import DEFAULT_SCORE_WEIGHTS, StrategyParams
from strategy_scoring import (
    WeightedSignalBreakdown,
    detect_market_regime,
    effective_thresholds,
    normalized_confidence_score,
    trading_days_between_inclusive,
    weighted_signal_breakdown,
)


def _fmt_px(value: float, decimals: int = 2) -> str:
    return f"{value:.{decimals}f}"


_VALID_SIDES = frozenset({"TQQQ", "SQQQ"})


@dataclass
class PositionState:
    active_symbol: str | None = None
    entry_price: float | None = None
    entry_timestamp: str | None = None
    last_signal: str | None = None
    updated_at: str | None = None


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


@dataclass(frozen=True)
class StrategyDebug:
    regime: str
    weighted_bull: float
    weighted_bear: float
    effective_bull_entry: float
    effective_bear_entry: float
    effective_weak: float
    normalized_confidence: int
    flip_suppressed: bool


@dataclass(frozen=True)
class DecideOptions:
    """Optional hooks for research/backtest; live runs pass None (same as defaults)."""

    debug_sanity_dominate: bool = False


def _coerce_entry_price(raw: object) -> tuple[float | None, bool]:
    if raw is None:
        return None, False
    if isinstance(raw, bool):
        return None, True
    if isinstance(raw, (int, float)):
        return float(raw), False
    return None, True


def _normalize_loaded_symbol(raw: object) -> tuple[str | None, bool]:
    if raw is None or raw == "":
        return None, False
    if not isinstance(raw, str):
        return None, True
    sym = raw.strip().upper()
    if sym not in _VALID_SIDES:
        return None, True
    return sym, False


def load_position(path: Path) -> tuple[PositionState, list[str]]:
    """Load position_state.json. Returns (flat state, warnings) on any problem."""
    warnings: list[str] = []
    if not path.exists():
        return PositionState(), warnings
    try:
        raw_text = path.read_text(encoding="utf-8")
        data = json.loads(raw_text)
    except (OSError, json.JSONDecodeError) as exc:
        warnings.append(f"Ignoring unreadable position_state.json ({exc}). Starting flat.")
        return PositionState(), warnings
    if not isinstance(data, dict):
        warnings.append("position_state.json must be a JSON object. Starting flat.")
        return PositionState(), warnings

    sym_raw = data.get("symbol", data.get("active_symbol"))
    sym, sym_bad = _normalize_loaded_symbol(sym_raw)
    if sym_bad:
        warnings.append(f"Ignoring invalid symbol {sym_raw!r}. Starting flat.")
        return PositionState(), warnings

    ep_raw = data.get("entry_price")
    entry_price, ep_bad = _coerce_entry_price(ep_raw)
    if ep_bad:
        warnings.append(f"Ignoring invalid entry_price {ep_raw!r}.")
        entry_price = None

    et_raw = data.get("entry_time", data.get("entry_timestamp"))
    entry_time: str | None = None
    if et_raw is None or et_raw == "":
        entry_time = None
    elif isinstance(et_raw, str):
        entry_time = et_raw.strip()
    else:
        warnings.append(f"Ignoring invalid entry_time {et_raw!r}.")
        entry_time = None

    last_signal = data.get("last_signal")
    if last_signal is not None and not isinstance(last_signal, str):
        warnings.append("Ignoring invalid last_signal.")
        last_signal = None
    elif isinstance(last_signal, str):
        last_signal = last_signal.strip() or None

    updated_at = data.get("updated_at")
    if updated_at is not None and not isinstance(updated_at, str):
        warnings.append("Ignoring invalid updated_at.")
        updated_at = None
    elif isinstance(updated_at, str):
        updated_at = updated_at.strip() or None

    if sym is None:
        return (
            PositionState(
                active_symbol=None,
                entry_price=None,
                entry_timestamp=None,
                last_signal=last_signal,
                updated_at=updated_at,
            ),
            warnings,
        )

    return (
        PositionState(
            active_symbol=sym,
            entry_price=entry_price,
            entry_timestamp=entry_time,
            last_signal=last_signal,
            updated_at=updated_at,
        ),
        warnings,
    )


def save_position(path: Path, position: PositionState) -> None:
    payload = {
        "symbol": position.active_symbol,
        "entry_price": position.entry_price,
        "entry_time": position.entry_timestamp,
        "last_signal": position.last_signal,
        "updated_at": position.updated_at,
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


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


def score_signals(
    s: IndicatorSnapshot,
    weights: tuple[float, ...] | None = None,
) -> tuple[int, int, list[str], list[str]]:
    """Weighted checklist; returns 0–100 strength per side vs max possible weight sum."""
    w = weights if weights is not None else DEFAULT_SCORE_WEIGHTS
    bd = weighted_signal_breakdown(s, w)
    scale = max(bd.max_weight_total, 1e-9)
    bull_pct = int(round(100 * bd.weighted_bull / scale))
    bear_pct = int(round(100 * bd.weighted_bear / scale))
    return bull_pct, bear_pct, list(bd.bull_reasons), list(bd.bear_reasons)


def _weighted_breakdown(snapshot: IndicatorSnapshot, params: StrategyParams) -> WeightedSignalBreakdown:
    return weighted_signal_breakdown(snapshot, params.score_weights)


@dataclass(frozen=True)
class RunTechnicalMeta:
    """Console-only context for the technical breakdown block."""

    qqq_ticker: str
    run_utc_iso: str
    daily_bar_end: str | None
    h4_bar_end: str | None
    blocked_today: bool
    regime: str
    base_bull_entry_threshold: int
    base_bear_entry_threshold: int
    base_weak_threshold: int
    effective_bull_entry: float
    effective_bear_entry: float
    effective_weak: float
    score_weights: tuple[float, ...]
    weighted_bull: float
    weighted_bear: float
    stop_loss_pct: float
    take_profit_pct: float
    stretch_take_profit_pct: float
    max_hold_days: int
    entry_atr_multiplier: float
    anchor_date_label: str
    flip_in_range_regime: bool


def format_technical_breakdown(
    snapshot: IndicatorSnapshot,
    alert: AlertDecision,
    position_before: PositionState,
    meta: RunTechnicalMeta,
    today_iso: str,
) -> str:
    bull, bear, bull_reasons, bear_reasons = score_signals(snapshot, meta.score_weights)
    wsum = max(sum(meta.score_weights), 1e-9)
    lines: list[str] = [
        "--- Technical breakdown (inputs are QQQ; trades are TQQQ/SQQQ) ---",
        f"Ticker: {meta.qqq_ticker} | Run (UTC): {meta.run_utc_iso}",
        f"Bars: daily_last={meta.daily_bar_end or '?'} | h4_last={meta.h4_bar_end or '?'}",
        f"Event blackout today: {'yes' if meta.blocked_today else 'no'}",
        f"Regime: {meta.regime} (EMA sep/slope vs ATR)",
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
        "[Score engine] (weighted checklist; strength is % of max weighted stack)",
        f"  Bull strength {bull}/100 | Bear strength {bear}/100 | "
        f"Weighted raw {meta.weighted_bull:.2f} / {meta.weighted_bear:.2f} (max sum {wsum:.2f})",
        f"  Normalized confidence {alert.confidence_score}% (dominance of weighted stacks)",
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
            (
                f"  BUY TQQQ (weighted): bull_sum>={meta.effective_bull_entry:.2f} AND "
                f"bear_sum<{meta.effective_bear_entry:.2f} (flat, not blocked)"
            ),
            (
                f"  BUY SQQQ (weighted): bear_sum>={meta.effective_bear_entry:.2f} AND "
                f"bull_sum<{meta.effective_bull_entry:.2f} (flat, not blocked)"
            ),
            f"  Base thresholds (legacy 0–8 scale): bull≥{meta.base_bull_entry_threshold}, "
            f"bear≥{meta.base_bear_entry_threshold}, weak<{meta.base_weak_threshold}",
            (
                f"  SELL / exit while holding: weak trend OR opposite entry-level signal OR stop OR "
                f"take-profit OR max hold ({meta.max_hold_days} trading days)"
            ),
            (
                f"    weak if TQQQ: bull_sum<{meta.effective_weak:.2f}; "
                f"weak if SQQQ: bear_sum<{meta.effective_weak:.2f}"
            ),
            (
                f"    reverse if TQQQ held: bear_sum>={meta.effective_bear_entry:.2f}; "
                f"reverse if SQQQ held: bull_sum>={meta.effective_bull_entry:.2f} "
                f"(flip suppression: min hold / extra margin may apply)"
            ),
            (
                f"    reversal FLIPs in range regime: "
                f"{'allowed' if meta.flip_in_range_regime else 'disabled (use weaken/stop/TP/max hold only)'}"
            ),
            f"  Risk params: stop {meta.stop_loss_pct:.1%} | TP {meta.take_profit_pct:.1%} | stretch TP {meta.stretch_take_profit_pct:.1%}",
            f"  Entry zone (QQQ): close +/- {meta.entry_atr_multiplier}*ATR14 -> [{_fmt_px(alert.entry_zone_low)}, {_fmt_px(alert.entry_zone_high)}]",
            "",
            "[Bot position memory - before this decision]",
            f"  active_symbol={position_before.active_symbol!r}  entry_price={position_before.entry_price!r}  "
            f"entry_timestamp={position_before.entry_timestamp!r}",
            f"  last_signal={position_before.last_signal!r}  updated_at={position_before.updated_at!r}",
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
        wb = meta.weighted_bull
        wbear = meta.weighted_bear
        if sym == "TQQQ":
            weaken = wb < meta.effective_weak
            reverse = wbear >= meta.effective_bear_entry
            stop_hit = price <= alert.stop_loss
            tp_hit = price >= alert.take_profit
        else:
            weaken = wbear < meta.effective_weak
            reverse = wb >= meta.effective_bull_entry
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
    today_iso: str,
) -> str | None:
    sym = position_before.active_symbol
    if not sym:
        return None
    price = snapshot.daily_close
    reached_max = bool(alert.max_hold_date) and today_iso > alert.max_hold_date
    wb = meta.weighted_bull
    wbear = meta.weighted_bear
    if sym == "TQQQ":
        weaken = wb < meta.effective_weak
        reverse = wbear >= meta.effective_bear_entry
        stop_hit = price <= alert.stop_loss
        tp_hit = price >= alert.take_profit
    else:
        weaken = wbear < meta.effective_weak
        reverse = wb >= meta.effective_bull_entry
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
    params: StrategyParams,
    *,
    decide_options: DecideOptions | None = None,
) -> tuple[AlertDecision, PositionState, StrategyDebug]:
    bd = _weighted_breakdown(snapshot, params)
    wb = bd.weighted_bull
    wbear = bd.weighted_bear
    scale = max(bd.max_weight_total, 1e-9)
    regime = detect_market_regime(snapshot, params)
    bull_eff, bear_eff, weak_eff = effective_thresholds(regime, params)
    bull_pct = int(round(100 * wb / scale))
    bear_pct = int(round(100 * wbear / scale))
    confidence = normalized_confidence_score(wb, wbear, scale)
    ts = now_utc.replace(microsecond=0).isoformat().replace("+00:00", "Z")
    today = now_utc.date().isoformat()
    blocked = today in blocked_dates

    price = snapshot.daily_close
    atr = snapshot.daily_atr14
    entry_low = price - (params.entry_atr_multiplier * atr)
    entry_high = price + (params.entry_atr_multiplier * atr)

    bull_reasons = list(bd.bull_reasons)
    bear_reasons = list(bd.bear_reasons)
    dominant_reasons = bull_reasons[:3] if wb >= wbear else bear_reasons[:3]
    reason = "; ".join(dominant_reasons) if dominant_reasons else "mixed conditions"
    reason = f"{reason} | regime={regime}"
    symbol = "CASH"
    alert_type = "CASH"
    notes = "No high-confidence setup."
    flip_suppressed = False
    opts = decide_options or DecideOptions()

    if position.active_symbol is None and not blocked:
        if opts.debug_sanity_dominate:
            notes = "[DEBUG SANITY - dominance-only entries; not for production]"
            if wb > wbear:
                symbol = "TQQQ"
                alert_type = "BUY"
                notes += " BUY TQQQ (weighted bull > bear)."
            elif wbear > wb:
                symbol = "SQQQ"
                alert_type = "BUY"
                notes += " BUY SQQQ (weighted bear > bull)."
            else:
                alert_type = "CASH"
                notes += " tie stacks; flat."
        elif wb >= bull_eff and wbear < bear_eff:
            symbol = "TQQQ"
            alert_type = "BUY"
            notes = "Bullish QQQ setup."
        elif wbear >= bear_eff and wb < bull_eff:
            symbol = "SQQQ"
            alert_type = "BUY"
            notes = "Bearish QQQ setup."
        elif params.entry_dominance_gap_weight > 0:
            gap_w = params.entry_dominance_gap_weight
            if wb > wbear and (wb - wbear) >= gap_w:
                symbol = "TQQQ"
                alert_type = "BUY"
                notes = "Dominance entry: bull stack leads bear by weighted gap (fallback)."
            elif wbear > wb and (wbear - wb) >= gap_w:
                symbol = "SQQQ"
                alert_type = "BUY"
                notes = "Dominance entry: bear stack leads bull by weighted gap (fallback)."
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
        last_signal=alert_type,
        updated_at=ts,
    )

    if alert_type == "BUY":
        entry_price = price
        if symbol == "TQQQ":
            stop_loss = entry_price * (1 - params.stop_loss_pct)
            take_profit = entry_price * (1 + params.take_profit_pct)
            stretch_tp = entry_price * (1 + params.stretch_take_profit_pct)
        else:
            stop_loss = entry_price * (1 + params.stop_loss_pct)
            take_profit = entry_price * (1 - params.take_profit_pct)
            stretch_tp = entry_price * (1 - params.stretch_take_profit_pct)
        max_hold_date = _trading_days_after(ts, params.max_hold_days)
        new_position = PositionState(
            active_symbol=symbol,
            entry_price=entry_price,
            entry_timestamp=ts,
            last_signal="BUY",
            updated_at=ts,
        )
    elif position.active_symbol:
        symbol = position.active_symbol
        entry_price = float(position.entry_price or price)
        resolved_entry_ts = position.entry_timestamp or ts
        max_hold_date = _trading_days_after(resolved_entry_ts, params.max_hold_days)
        reached_max_hold = now_utc.date().isoformat() > max_hold_date

        if symbol == "TQQQ":
            stop_loss = entry_price * (1 - params.stop_loss_pct)
            take_profit = entry_price * (1 + params.take_profit_pct)
            stretch_tp = entry_price * (1 + params.stretch_take_profit_pct)
            weaken = wb < weak_eff
            raw_reverse = wbear >= bear_eff
            stop_hit = price <= stop_loss
            tp_hit = price >= take_profit
        else:
            stop_loss = entry_price * (1 + params.stop_loss_pct)
            take_profit = entry_price * (1 - params.take_profit_pct)
            stretch_tp = entry_price * (1 - params.stretch_take_profit_pct)
            weaken = wbear < weak_eff
            raw_reverse = wb >= bull_eff
            stop_hit = price >= stop_loss
            tp_hit = price <= take_profit

        if regime == "range" and not params.flip_in_range_regime:
            raw_reverse = False

        entry_day = datetime.fromisoformat(resolved_entry_ts.replace("Z", "+00:00")).date()
        td_hold = trading_days_between_inclusive(entry_day, now_utc.date())
        if symbol == "TQQQ":
            flip_ok = (not raw_reverse) or (
                td_hold >= params.flip_min_hold_trading_days
                or wbear >= bear_eff + params.flip_margin_weight
            )
        else:
            flip_ok = (not raw_reverse) or (
                td_hold >= params.flip_min_hold_trading_days
                or wb >= bull_eff + params.flip_margin_weight
            )
        flip_suppressed = raw_reverse and not flip_ok
        reverse = raw_reverse and flip_ok

        if reverse:
            flip_to = "SQQQ" if symbol == "TQQQ" else "TQQQ"
            alert_type = "FLIP"
            symbol = flip_to
            notes = f"Reverse signal: sell {position.active_symbol} and buy {flip_to}."
            max_hold_date = _trading_days_after(ts, params.max_hold_days)
            new_position = PositionState(
                active_symbol=flip_to,
                entry_price=price,
                entry_timestamp=ts,
                last_signal="FLIP",
                updated_at=ts,
            )
            if flip_to == "TQQQ":
                stop_loss = price * (1 - params.stop_loss_pct)
                take_profit = price * (1 + params.take_profit_pct)
                stretch_tp = price * (1 + params.stretch_take_profit_pct)
            else:
                stop_loss = price * (1 + params.stop_loss_pct)
                take_profit = price * (1 - params.take_profit_pct)
                stretch_tp = price * (1 - params.stretch_take_profit_pct)
        elif weaken or stop_hit or tp_hit or reached_max_hold:
            alert_type = "SELL"
            notes = "Exit rule triggered."
            new_position = PositionState(
                active_symbol=None,
                entry_price=None,
                entry_timestamp=None,
                last_signal="SELL",
                updated_at=ts,
            )
        else:
            alert_type = "CASH"
            notes = "Holding active position."
            if flip_suppressed:
                notes += " Flip suppressed (whipsaw guard)."
            new_position = PositionState(
                active_symbol=symbol,
                entry_price=position.entry_price,
                entry_timestamp=resolved_entry_ts,
                last_signal="CASH",
                updated_at=ts,
            )

    dbg = StrategyDebug(
        regime=regime,
        weighted_bull=wb,
        weighted_bear=wbear,
        effective_bull_entry=bull_eff,
        effective_bear_entry=bear_eff,
        effective_weak=weak_eff,
        normalized_confidence=confidence,
        flip_suppressed=flip_suppressed,
    )

    decision = AlertDecision(
        alert_type=alert_type,
        symbol=symbol,
        qqq_trend_reason=reason,
        bullish_score=bull_pct,
        bearish_score=bear_pct,
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
    return decision, new_position, dbg


def load_blocked_dates(path: Path) -> set[str]:
    return _load_blocked_dates(path)
