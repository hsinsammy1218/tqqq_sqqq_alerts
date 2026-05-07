from __future__ import annotations

from indicators import IndicatorSnapshot
from strategy_decision import score_signals
from strategy_types import (
    HIGH_SIGNAL_QUALITY_THRESHOLD,
    AlertDecision,
    PositionState,
    RunTechnicalMeta,
)


def _fmt_px(value: float, decimals: int = 2) -> str:
    return f"{value:.{decimals}f}"


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
            (
                f"  Flat BUY confidence gate: normalized confidence ≥{meta.min_confidence_to_trade}% "
                f"(MIN_CONFIDENCE_TO_TRADE; 0 disables)"
            ),
            (
                f"  BUY signal quality (after gates): HIGH ≥{HIGH_SIGNAL_QUALITY_THRESHOLD}% · "
                f"MEDIUM ≥{meta.min_confidence_to_trade}% and <{HIGH_SIGNAL_QUALITY_THRESHOLD}%"
            ),
            (
                "  CLI --high-confidence-only: flat BUY allowed only when normalized confidence "
                f"≥{HIGH_SIGNAL_QUALITY_THRESHOLD}% "
                f"({'ON' if meta.high_confidence_only else 'OFF'})"
            ),
            f"  Base thresholds (legacy 0-8 scale): bull>={meta.base_bull_entry_threshold}, "
            f"bear>={meta.base_bear_entry_threshold}, weak<{meta.base_weak_threshold}",
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
