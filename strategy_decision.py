from __future__ import annotations

from datetime import datetime, timedelta

from indicators import IndicatorSnapshot
from strategy_params import DEFAULT_SCORE_WEIGHTS, StrategyParams
from strategy_scoring import (
    detect_market_regime,
    effective_thresholds,
    normalized_confidence_score,
    trading_days_between_inclusive,
    weighted_signal_breakdown,
)
from strategy_types import (
    HIGH_SIGNAL_QUALITY_THRESHOLD,
    AlertDecision,
    DecideOptions,
    PositionState,
    RunTechnicalMeta,
    StrategyDebug,
)


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
    """Weighted checklist; returns 0-100 strength per side vs max possible weight sum."""
    w = weights if weights is not None else DEFAULT_SCORE_WEIGHTS
    bd = weighted_signal_breakdown(s, w)
    scale = max(bd.max_weight_total, 1e-9)
    bull_pct = int(round(100 * bd.weighted_bull / scale))
    bear_pct = int(round(100 * bd.weighted_bear / scale))
    return bull_pct, bear_pct, list(bd.bull_reasons), list(bd.bear_reasons)


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
    bd = weighted_signal_breakdown(snapshot, params.score_weights)
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

    if (
        position.active_symbol is None
        and not blocked
        and alert_type == "BUY"
        and params.min_confidence_to_trade > 0
        and confidence < params.min_confidence_to_trade
    ):
        alert_type = "CASH"
        symbol = "CASH"
        notes = (
            f"Entry skipped: confidence {confidence}% is below MIN_CONFIDENCE_TO_TRADE "
            f"({params.min_confidence_to_trade}%)."
        )

    if (
        position.active_symbol is None
        and not blocked
        and alert_type == "BUY"
        and opts.high_confidence_only
        and confidence < HIGH_SIGNAL_QUALITY_THRESHOLD
    ):
        alert_type = "CASH"
        symbol = "CASH"
        notes = (
            f"Entry skipped: --high-confidence-only requires normalized confidence "
            f">={HIGH_SIGNAL_QUALITY_THRESHOLD}% (current {confidence}%)."
        )

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
        if confidence >= HIGH_SIGNAL_QUALITY_THRESHOLD and atr > 0:
            hx = snapshot.h4_close
            chase_extended = (symbol == "TQQQ" and hx > entry_high + atr) or (
                symbol == "SQQQ" and hx < entry_low - atr
            )
            if chase_extended:
                notes += " Extended move — consider waiting for pullback."
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

    signal_quality: str | None = None
    if alert_type == "BUY":
        signal_quality = "HIGH" if confidence >= HIGH_SIGNAL_QUALITY_THRESHOLD else "MEDIUM"

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
        signal_quality=signal_quality,
    )
    return decision, new_position, dbg
