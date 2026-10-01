"""ETF replay of the live ``decide()`` rules.

Signals stay on QQQ. Fills are next-session day limits on TQQQ or SQQQ, not the
QQQ close and not a 1x sign flip. The most recent third of the research window
is a sealed holdout: ``evaluate_development`` does not run it.

This module does not change live score weights, the cron command, or Render env.
"""

from __future__ import annotations

import copy
import json
import statistics
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable

import pandas as pd

from backtest import (
    BacktestTradeRow,
    h4_slice_through_daily_calendar_date,
    synthetic_h4_fallback_from_daily,
)
from data import CandleData
from indicators import build_snapshot
from strategy import DecideOptions, PositionState, decide
from strategy_eval import (
    MIN_TRADES,
    WFE_ROBUST_LOW,
    WFE_ROBUST_HIGH,
    annualized_pnl_pct,
    metrics_from_returns,
    resolve_research_bars,
    walk_forward_efficiency,
)
from strategy_params import DEFAULT_SCORE_WEIGHTS, StrategyParams
from strategy_scoring import trading_days_between_inclusive

# Same 10 bp the paper bot uses as ALPACA_PAPER_LIMIT_OFFSET_BPS, in ETF price
# terms. The limit is the cost. Fills are not haircut a second time.
DEFAULT_ETF_COST_BPS = 10.0
SEALED_SEGMENT_COUNT = 3
SEAL_PATH = Path("reports/etf_sealed_oos.json")

DecideFn = Callable[..., tuple]


@dataclass(frozen=True)
class ResearchSplit:
    """Development is the earlier blocks. The last block is sealed."""

    segments: tuple[tuple[int, int], ...]
    development_lo: int
    development_hi: int
    holdout_lo: int
    holdout_hi: int


@dataclass
class FillStats:
    buy_signals: int = 0
    buy_fills: int = 0
    buy_misses: int = 0
    sell_signals: int = 0
    sell_fills: int = 0
    sell_misses: int = 0
    flip_signals: int = 0
    flip_sell_fills: int = 0
    flip_sell_misses: int = 0
    flip_second_leg_blocked: int = 0
    flip_buy_fills: int = 0
    flip_buy_misses: int = 0
    signals_without_next_bar: int = 0
    entry_cost_bps: list[float] = field(default_factory=list)


@dataclass(frozen=True)
class EtfBacktestResult:
    bars_tested: int
    start_utc: str
    end_utc: str
    cost_bps: float
    closed_trades: int
    winning_trades: int
    losing_trades: int
    win_rate_pct: float
    average_return_per_trade_pct: float
    median_return_per_trade_pct: float | None
    best_trade_pct: float | None
    worst_trade_pct: float | None
    max_drawdown_pct: float
    total_return_pct: float
    mtm_total_return_pct: float
    mtm_max_drawdown_pct: float
    exposure_pct: float
    average_entry_cost_bps: float | None
    open_symbol: str | None
    open_unrealized_pct: float | None
    fill_stats: FillStats
    trade_rows: tuple[BacktestTradeRow, ...]


@dataclass(frozen=True)
class AlignedEtfData:
    qqq: CandleData
    tqqq: pd.DataFrame
    sqqq: pd.DataFrame
    dates_dropped: int


@dataclass(frozen=True)
class PhaseEvaluation:
    role: str
    result: EtfBacktestResult
    metrics: dict[str, object]
    wfe: dict[str, object]
    gate: dict[str, object]
    buy_hold_qqq_pct: float
    buy_hold_tqqq_pct: float
    development_start_utc: str
    development_end_utc: str
    holdout_start_utc: str
    holdout_end_utc: str
    dates_dropped: int
    score_weights: tuple[float, ...]


@dataclass
class _Lot:
    symbol: str
    fill_price: float
    signal_timestamp: str


@dataclass
class _Pending:
    alert_type: str
    intended: PositionState
    before: PositionState
    tqqq_ref: float
    sqqq_ref: float
    signal_timestamp: str
    bullish_score: int
    bearish_score: int
    confidence: int
    regime: str
    reason: str


def limit_from_reference(ref_close: float, side: str, cost_bps: float) -> float:
    """Day-limit price  ``cost_bps`` through the signal close, in ETF points."""
    if ref_close <= 0:
        raise ValueError("ETF reference close must be positive.")
    bps = max(0.0, float(cost_bps))
    if side == "buy":
        return ref_close * (1.0 + bps / 10_000.0)
    if side == "sell":
        return ref_close * (1.0 - bps / 10_000.0)
    raise ValueError("side must be 'buy' or 'sell'.")


def day_limit_fill(
    bar_open: float,
    bar_high: float,
    bar_low: float,
    limit: float,
    side: str,
) -> float | None:
    """Fill a day limit on the next bar, or None when the bar never trades through it.

    A buy fills at the open when the open is at or below the limit, otherwise at
    the limit if the low touches it. A sell is the mirror. A gap through the
    limit with no trade back to it is a miss, which matches a day limit that
    cannot chase.
    """
    if side == "buy":
        if bar_low > limit:
            return None
        if bar_open <= limit:
            return float(bar_open)
        return float(limit)
    if side == "sell":
        if bar_high < limit:
            return None
        if bar_open >= limit:
            return float(bar_open)
        return float(limit)
    raise ValueError("side must be 'buy' or 'sell'.")


def phase_d_allowed(
    *,
    development_measured: bool,
    development_return_pct: float | None,
    development_trades: int,
    sealed_measured: bool,
    sealed_return_pct: float | None,
    sealed_trades: int,
) -> bool:
    """Phase D runs only when both windows were measured and both made money.

    Zero closed trades counts as flat. A non-positive compounded return counts
    as flat or negative. An unmeasured window is not a pass.
    """
    if not development_measured or not sealed_measured:
        return False
    if development_return_pct is None or sealed_return_pct is None:
        return False
    if development_trades <= 0 or sealed_trades <= 0:
        return False
    return development_return_pct > 0.0 and sealed_return_pct > 0.0


def _as_utc_days(frame: pd.DataFrame, *, label: str) -> pd.DataFrame:
    if frame is None or frame.empty:
        raise ValueError(f"{label} daily frame is empty.")
    idx = pd.to_datetime(frame.index, utc=True)
    out = frame.copy()
    out.index = pd.DatetimeIndex(idx.tz_convert("UTC").normalize())
    out = out[~out.index.duplicated(keep="last")].sort_index()
    required = ["open", "high", "low", "close"]
    missing = [col for col in required if col not in out.columns]
    if missing:
        raise ValueError(f"{label} daily frame missing columns: {missing}")
    return out


def align_etf_data(
    qqq: CandleData,
    tqqq_daily: pd.DataFrame,
    sqqq_daily: pd.DataFrame,
) -> AlignedEtfData:
    """Inner-join QQQ, TQQQ, and SQQQ on the UTC session date."""
    qqq_days = _as_utc_days(qqq.daily, label="QQQ")
    tqqq_days = _as_utc_days(tqqq_daily, label="TQQQ")
    sqqq_days = _as_utc_days(sqqq_daily, label="SQQQ")
    common = qqq_days.index.intersection(tqqq_days.index).intersection(sqqq_days.index)
    if len(common) < 80:
        raise ValueError(
            "Need at least 80 shared QQQ/TQQQ/SQQQ sessions for an ETF backtest. "
            f"Shared sessions: {len(common)}."
        )
    dropped = int(len(qqq_days) - len(common))
    return AlignedEtfData(
        qqq=CandleData(daily=qqq_days.loc[common], four_hour=qqq.four_hour),
        tqqq=tqqq_days.loc[common],
        sqqq=sqqq_days.loc[common],
        dates_dropped=dropped,
    )


def equal_segments(
    lo: int,
    hi: int,
    n_segments: int = SEALED_SEGMENT_COUNT,
    min_bars: int = 20,
) -> tuple[tuple[int, int], ...]:
    if n_segments < 2:
        raise ValueError("n_segments must be >= 2")
    span = hi - lo
    if span < n_segments * min_bars:
        raise ValueError(
            f"Window {lo}:{hi} is too short for {n_segments} segments of {min_bars} bars."
        )
    segments: list[tuple[int, int]] = []
    for k in range(n_segments):
        seg_lo = lo + (span * k) // n_segments
        seg_hi = lo + (span * (k + 1)) // n_segments
        if seg_hi - seg_lo < min_bars:
            raise ValueError("A research segment fell below the minimum bar count.")
        segments.append((seg_lo, seg_hi))
    return tuple(segments)


def compute_research_split(
    n_daily: int,
    bars: int,
    *,
    n_segments: int = SEALED_SEGMENT_COUNT,
    min_bars: int = 20,
) -> ResearchSplit:
    """Last segment is the sealed holdout. Earlier segments are development."""
    if n_daily < 80:
        raise ValueError("Need at least 80 daily bars for an ETF backtest.")
    region_lo = max(60, n_daily - int(bars))
    region_hi = n_daily
    segments = equal_segments(region_lo, region_hi, n_segments, min_bars)
    return ResearchSplit(
        segments=segments,
        development_lo=segments[0][0],
        development_hi=segments[-2][1],
        holdout_lo=segments[-1][0],
        holdout_hi=segments[-1][1],
    )


def _index_label(index: pd.DatetimeIndex, pos: int) -> str:
    ts = index[pos]
    return ts.isoformat() if hasattr(ts, "isoformat") else str(ts)


def buy_and_hold_close_to_close(frame: pd.DataFrame, lo: int, hi: int) -> float:
    """Percent change from the window's first close to its last close."""
    entry = float(frame.iloc[lo]["close"])
    exit_px = float(frame.iloc[hi - 1]["close"])
    if entry <= 0:
        raise ValueError("Buy-and-hold entry close must be positive.")
    return (exit_px / entry - 1.0) * 100.0


def _max_drawdown_pct(equity_points: list[float]) -> float:
    if not equity_points:
        return 0.0
    peak = equity_points[0]
    worst = 0.0
    for eq in equity_points:
        if eq > peak:
            peak = eq
        drawdown = (peak - eq) / peak * 100.0 if peak > 0 else 0.0
        if drawdown > worst:
            worst = drawdown
    return worst


def _ref_close(pending: _Pending, symbol: str) -> float:
    if symbol == "TQQQ":
        return pending.tqqq_ref
    if symbol == "SQQQ":
        return pending.sqqq_ref
    raise ValueError(f"Unsupported ETF symbol: {symbol}")


def _bar_fill(
    frame: pd.DataFrame,
    bar_i: int,
    *,
    side: str,
    ref_close: float,
    cost_bps: float,
) -> tuple[float | None, float]:
    limit = limit_from_reference(ref_close, side, cost_bps)
    row = frame.iloc[bar_i]
    fill = day_limit_fill(float(row["open"]), float(row["high"]), float(row["low"]), limit, side)
    return fill, limit


def _close_trade(
    lot: _Lot,
    *,
    exit_fill: float,
    exit_day,
    pending: _Pending,
) -> BacktestTradeRow:
    ret_pct = (exit_fill / lot.fill_price - 1.0) * 100.0
    entry_day = datetime.fromisoformat(lot.signal_timestamp.replace("Z", "+00:00")).date()
    hold_days = trading_days_between_inclusive(entry_day, exit_day)
    return BacktestTradeRow(
        timestamp=pending.signal_timestamp,
        action="SELL" if pending.alert_type == "SELL" else "FLIP",
        symbol=lot.symbol,
        entry_price=lot.fill_price,
        exit_price=exit_fill,
        return_pct=ret_pct,
        hold_days=hold_days,
        bull_strength=pending.bullish_score,
        bear_strength=pending.bearish_score,
        confidence=pending.confidence,
        regime=pending.regime,
        reason=pending.reason,
    )


def _resolve_pending(
    pending: _Pending,
    *,
    bar_i: int,
    tqqq: pd.DataFrame,
    sqqq: pd.DataFrame,
    position: PositionState,
    lot: _Lot | None,
    cost_bps: float,
    stats: FillStats,
    exit_day,
) -> tuple[PositionState, _Lot | None, BacktestTradeRow | None, float | None]:
    """Apply yesterday's day order. Returns position, lot, closed trade, and cash multiple.

    The cash multiple is None when the account's share count should stay as it is.
    Callers apply share math separately so a missed fill does not change inventory.
    """
    frames = {"TQQQ": tqqq, "SQQQ": sqqq}

    def fill(symbol: str, side: str) -> float | None:
        px, _limit = _bar_fill(
            frames[symbol],
            bar_i,
            side=side,
            ref_close=_ref_close(pending, symbol),
            cost_bps=cost_bps,
        )
        return px

    if pending.alert_type == "BUY":
        stats.buy_signals += 1
        symbol = pending.intended.active_symbol or ""
        px = fill(symbol, "buy") if symbol in frames else None
        if px is None:
            stats.buy_misses += 1
            return position, lot, None, None
        stats.buy_fills += 1
        ref = _ref_close(pending, symbol)
        stats.entry_cost_bps.append((px / ref - 1.0) * 10_000.0)
        return pending.intended, _Lot(symbol, px, pending.signal_timestamp), None, px

    if pending.alert_type == "SELL":
        stats.sell_signals += 1
        symbol = (pending.before.active_symbol or "")
        px = fill(symbol, "sell") if symbol in frames and lot is not None else None
        if px is None or lot is None:
            stats.sell_misses += 1
            return position, lot, None, None
        stats.sell_fills += 1
        trade = _close_trade(lot, exit_fill=px, exit_day=exit_day, pending=pending)
        return PositionState(), None, trade, px

    if pending.alert_type == "FLIP":
        stats.flip_signals += 1
        held = pending.before.active_symbol or ""
        sell_px = fill(held, "sell") if held in frames and lot is not None else None
        if sell_px is None or lot is None:
            stats.flip_sell_misses += 1
            stats.flip_second_leg_blocked += 1
            return position, lot, None, None
        stats.flip_sell_fills += 1
        trade = _close_trade(lot, exit_fill=sell_px, exit_day=exit_day, pending=pending)
        new_symbol = pending.intended.active_symbol or ""
        buy_px = fill(new_symbol, "buy") if new_symbol in frames else None
        if buy_px is None:
            stats.flip_buy_misses += 1
            return PositionState(), None, trade, sell_px
        stats.flip_buy_fills += 1
        ref = _ref_close(pending, new_symbol)
        stats.entry_cost_bps.append((buy_px / ref - 1.0) * 10_000.0)
        return (
            pending.intended,
            _Lot(new_symbol, buy_px, pending.signal_timestamp),
            trade,
            buy_px,
        )

    return position, lot, None, None


def run_etf_backtest(
    data: AlignedEtfData,
    *,
    strategy_params: StrategyParams,
    blocked_dates: set[str],
    anchor_date: str | None,
    loop_start_idx: int,
    loop_end_idx_exclusive: int,
    cost_bps: float = DEFAULT_ETF_COST_BPS,
    decide_options: DecideOptions | None = None,
    decide_fn: DecideFn | None = None,
) -> EtfBacktestResult:
    """Replay ``decide()`` and fill on the next ETF bar.

    ``loop_end_idx_exclusive`` is not read past. A signal on the last bar has no
    next session inside the window and does not fill.
    """
    decider = decide if decide_fn is None else decide_fn
    daily = data.qqq.daily
    four_hour = data.qqq.four_hour
    tqqq = data.tqqq
    sqqq = data.sqqq
    n = len(daily)
    lo = int(loop_start_idx)
    hi = int(loop_end_idx_exclusive)
    if lo < 60:
        raise ValueError("loop_start_idx must be >= 60 (indicator warmup).")
    if hi > n or hi <= lo:
        raise ValueError("Invalid ETF backtest loop range.")
    if hi - lo < 20:
        raise ValueError("ETF backtest segment must span at least 20 daily bars.")

    position = PositionState()
    lot: _Lot | None = None
    pending: _Pending | None = None
    stats = FillStats()
    equity = 1.0
    cash = 1.0
    shares = 0.0
    held_symbol: str | None = None
    equity_points = [equity]
    mtm_points = [1.0]
    trade_rows: list[BacktestTradeRow] = []
    exposed = 0
    wins = 0
    losses = 0

    for i in range(lo, hi):
        now_ts = daily.index[i]
        now_dt = now_ts.to_pydatetime()
        exit_day = now_dt.date()
        if pending is not None:
            before_shares = shares
            before_cash = cash
            before_held = held_symbol
            new_position, new_lot, trade, _touch = _resolve_pending(
                pending,
                bar_i=i,
                tqqq=tqqq,
                sqqq=sqqq,
                position=position,
                lot=lot,
                cost_bps=cost_bps,
                stats=stats,
                exit_day=exit_day,
            )
            if trade is not None and lot is not None and before_shares > 0:
                cash = before_shares * trade.exit_price
                shares = 0.0
                equity = cash
                equity_points.append(equity)
                trade_rows.append(trade)
                if trade.return_pct >= 0:
                    wins += 1
                else:
                    losses += 1
            if new_lot is not None and (lot is None or new_lot.symbol != before_held or trade is not None):
                if new_position.active_symbol and cash > 0 and (
                    trade is not None or before_shares <= 0
                ):
                    shares = cash / new_lot.fill_price
                    cash = 0.0
                    held_symbol = new_lot.symbol
            if new_position.active_symbol is None:
                held_symbol = None
                shares = 0.0
            position = new_position
            lot = new_lot
            pending = None

        if position.active_symbol:
            exposed += 1
            mark_frame = tqqq if position.active_symbol == "TQQQ" else sqqq
            mark = shares * float(mark_frame.iloc[i]["close"]) if shares > 0 else cash
        else:
            mark = cash
        mtm_points.append(mark)

        daily_slice = daily.iloc[: i + 1]
        h4_real = h4_slice_through_daily_calendar_date(now_ts, four_hour)
        h4_slice = h4_real if len(h4_real) >= 5 else synthetic_h4_fallback_from_daily(daily_slice)
        snapshot = build_snapshot(daily_slice, h4_slice, anchor_date)
        position_before = copy.copy(position)
        alert, intended, dbg = decider(
            snapshot,
            position,
            blocked_dates,
            now_dt,
            strategy_params,
            decide_options=decide_options,
        )
        alert_type = (alert.alert_type or "").upper()
        if alert_type in {"BUY", "SELL", "FLIP"}:
            if i + 1 >= hi:
                stats.signals_without_next_bar += 1
            else:
                pending = _Pending(
                    alert_type=alert_type,
                    intended=intended,
                    before=position_before,
                    tqqq_ref=float(tqqq.iloc[i]["close"]),
                    sqqq_ref=float(sqqq.iloc[i]["close"]),
                    signal_timestamp=alert.timestamp,
                    bullish_score=int(alert.bullish_score),
                    bearish_score=int(alert.bearish_score),
                    confidence=int(alert.confidence_score),
                    regime=str(dbg.regime),
                    reason=str(alert.qqq_trend_reason),
                )

    open_unrealized = None
    if lot is not None and position.active_symbol:
        last_close = float(
            (tqqq if lot.symbol == "TQQQ" else sqqq).iloc[hi - 1]["close"]
        )
        marked = last_close * (1.0 - max(0.0, cost_bps) / 10_000.0)
        open_unrealized = (marked / lot.fill_price - 1.0) * 100.0

    closed = len(trade_rows)
    returns = [row.return_pct for row in trade_rows]
    avg_cost = (
        sum(stats.entry_cost_bps) / len(stats.entry_cost_bps) if stats.entry_cost_bps else None
    )
    tested = hi - lo
    return EtfBacktestResult(
        bars_tested=tested,
        start_utc=_index_label(daily.index, lo),
        end_utc=_index_label(daily.index, hi - 1),
        cost_bps=float(cost_bps),
        closed_trades=closed,
        winning_trades=wins,
        losing_trades=losses,
        win_rate_pct=(wins / closed * 100.0) if closed else 0.0,
        average_return_per_trade_pct=(sum(returns) / closed) if closed else 0.0,
        median_return_per_trade_pct=float(statistics.median(returns)) if returns else None,
        best_trade_pct=max(returns) if returns else None,
        worst_trade_pct=min(returns) if returns else None,
        max_drawdown_pct=_max_drawdown_pct(equity_points),
        total_return_pct=(equity - 1.0) * 100.0,
        mtm_total_return_pct=(mtm_points[-1] - 1.0) * 100.0,
        mtm_max_drawdown_pct=_max_drawdown_pct(mtm_points),
        exposure_pct=(exposed / tested * 100.0) if tested else 0.0,
        average_entry_cost_bps=avg_cost,
        open_symbol=position.active_symbol,
        open_unrealized_pct=open_unrealized,
        fill_stats=stats,
        trade_rows=tuple(trade_rows),
    )


def _wfe_block(
    data: AlignedEtfData,
    *,
    lo: int,
    hi: int,
    strategy_params: StrategyParams,
    blocked_dates: set[str],
    anchor_date: str | None,
    cost_bps: float,
    decide_options: DecideOptions | None,
    decide_fn: DecideFn | None,
) -> dict[str, object]:
    """Walk-forward efficiency inside one window. Does not read outside ``lo:hi``."""
    try:
        segments = equal_segments(lo, hi, SEALED_SEGMENT_COUNT, 20)
    except ValueError as exc:
        return {
            "available": False,
            "walk_forward_efficiency": None,
            "passes_robust_bar": False,
            "robust_bar": [WFE_ROBUST_LOW, WFE_ROBUST_HIGH],
            "note": str(exc),
        }
    is_lo = segments[0][0]
    is_hi = segments[-2][1]
    oos_lo, oos_hi = segments[-1]
    common = dict(
        strategy_params=strategy_params,
        blocked_dates=blocked_dates,
        anchor_date=anchor_date,
        cost_bps=cost_bps,
        decide_options=decide_options,
        decide_fn=decide_fn,
    )
    in_sample = run_etf_backtest(data, loop_start_idx=is_lo, loop_end_idx_exclusive=is_hi, **common)
    out_of_sample = run_etf_backtest(
        data, loop_start_idx=oos_lo, loop_end_idx_exclusive=oos_hi, **common
    )
    ann_is = annualized_pnl_pct(in_sample.total_return_pct, in_sample.bars_tested)
    ann_oos = annualized_pnl_pct(out_of_sample.total_return_pct, out_of_sample.bars_tested)
    wfe = walk_forward_efficiency(ann_oos, ann_is)
    return {
        "available": True,
        "method": "development_equal_segments_last_is_oos",
        "in_sample_bars": in_sample.bars_tested,
        "out_of_sample_bars": out_of_sample.bars_tested,
        "in_sample_net_profit_pct": in_sample.total_return_pct,
        "out_of_sample_net_profit_pct": out_of_sample.total_return_pct,
        "in_sample_start": in_sample.start_utc,
        "in_sample_end": in_sample.end_utc,
        "out_of_sample_start": out_of_sample.start_utc,
        "out_of_sample_end": out_of_sample.end_utc,
        "annualized_is_pnl_pct": ann_is,
        "annualized_oos_pnl_pct": ann_oos,
        "walk_forward_efficiency": wfe,
        "robust_bar": [WFE_ROBUST_LOW, WFE_ROBUST_HIGH],
        "passes_robust_bar": wfe is not None and wfe >= WFE_ROBUST_LOW,
        "note": (
            "Development-window walk-forward efficiency on ETF next-bar fills. "
            "The sealed final block is not part of this ratio."
        ),
    }


def _gate(result: EtfBacktestResult, wfe: dict[str, object], *, include_plateau_flag: bool) -> dict[str, object]:
    metrics = metrics_from_returns([row.return_pct for row in result.trade_rows])
    metrics["pnl_source"] = "etf_next_bar_day_limit"
    metrics["bars_tested"] = result.bars_tested
    metrics["window_start"] = result.start_utc
    metrics["window_end"] = result.end_utc
    metrics["backtest_total_return_pct"] = result.total_return_pct
    flags = list(metrics["flags"])
    wfe_value = wfe.get("walk_forward_efficiency")
    if wfe.get("available") and (wfe_value is None or float(wfe_value) < WFE_ROBUST_LOW):
        flags.append("walk_forward_efficiency_below_50")
    if not wfe.get("available"):
        flags.append("walk_forward_efficiency_unavailable")
    if include_plateau_flag:
        flags.append("plateau_not_run")
    passes = (
        int(metrics["trade_count"]) >= MIN_TRADES
        and "reward_risk_below_3" not in flags
        and "max_drawdown_ge_3x_average" not in flags
        and "walk_forward_efficiency_below_50" not in flags
        and "walk_forward_efficiency_unavailable" not in flags
        and "plateau_not_run" not in flags
        and bool(wfe.get("passes_robust_bar"))
    )
    if not passes:
        flags.append("paper_gate_failed")
    return {"passes": passes, "flags": flags, "metrics": metrics}


def _window_labels(index: pd.DatetimeIndex, split: ResearchSplit) -> dict[str, str]:
    return {
        "development_start_utc": _index_label(index, split.development_lo),
        "development_end_utc": _index_label(index, split.development_hi - 1),
        "holdout_start_utc": _index_label(index, split.holdout_lo),
        "holdout_end_utc": _index_label(index, split.holdout_hi - 1),
    }


def _prepare(
    qqq: CandleData,
    tqqq_daily: pd.DataFrame,
    sqqq_daily: pd.DataFrame,
    *,
    requested_bars: int,
    expand_default: bool,
) -> tuple[AlignedEtfData, int, ResearchSplit]:
    aligned = align_etf_data(qqq, tqqq_daily, sqqq_daily)
    bars = resolve_research_bars(
        requested_bars,
        len(aligned.qqq.daily),
        expand_default=expand_default,
    )
    split = compute_research_split(len(aligned.qqq.daily), bars)
    return aligned, bars, split


def evaluate_development(
    qqq: CandleData,
    tqqq_daily: pd.DataFrame,
    sqqq_daily: pd.DataFrame,
    *,
    strategy_params: StrategyParams,
    blocked_dates: set[str],
    anchor_date: str | None,
    requested_bars: int,
    expand_default: bool = True,
    cost_bps: float = DEFAULT_ETF_COST_BPS,
    decide_options: DecideOptions | None = None,
    decide_fn: DecideFn | None = None,
) -> PhaseEvaluation:
    """Score the earlier block only. Does not call the sealed-window runner."""
    aligned, _bars, split = _prepare(
        qqq,
        tqqq_daily,
        sqqq_daily,
        requested_bars=requested_bars,
        expand_default=expand_default,
    )
    common = dict(
        strategy_params=strategy_params,
        blocked_dates=blocked_dates,
        anchor_date=anchor_date,
        cost_bps=cost_bps,
        decide_options=decide_options,
        decide_fn=decide_fn,
    )
    result = run_etf_backtest(
        aligned,
        loop_start_idx=split.development_lo,
        loop_end_idx_exclusive=split.development_hi,
        **common,
    )
    wfe = _wfe_block(
        aligned,
        lo=split.development_lo,
        hi=split.development_hi,
        **common,
    )
    gate = _gate(result, wfe, include_plateau_flag=True)
    labels = _window_labels(aligned.qqq.daily.index, split)
    return PhaseEvaluation(
        role="development",
        result=result,
        metrics=gate["metrics"],  # type: ignore[arg-type]
        wfe=wfe,
        gate=gate,
        buy_hold_qqq_pct=buy_and_hold_close_to_close(
            aligned.qqq.daily, split.development_lo, split.development_hi
        ),
        buy_hold_tqqq_pct=buy_and_hold_close_to_close(
            aligned.tqqq, split.development_lo, split.development_hi
        ),
        dates_dropped=aligned.dates_dropped,
        score_weights=tuple(strategy_params.score_weights),
        **labels,
    )


def evaluate_sealed(
    qqq: CandleData,
    tqqq_daily: pd.DataFrame,
    sqqq_daily: pd.DataFrame,
    *,
    strategy_params: StrategyParams,
    blocked_dates: set[str],
    anchor_date: str | None,
    requested_bars: int,
    expand_default: bool = True,
    cost_bps: float = DEFAULT_ETF_COST_BPS,
    decide_options: DecideOptions | None = None,
    decide_fn: DecideFn | None = None,
) -> PhaseEvaluation:
    """One look at the final block. Does not rerun the development P&L."""
    aligned, _bars, split = _prepare(
        qqq,
        tqqq_daily,
        sqqq_daily,
        requested_bars=requested_bars,
        expand_default=expand_default,
    )
    result = run_etf_backtest(
        aligned,
        strategy_params=strategy_params,
        blocked_dates=blocked_dates,
        anchor_date=anchor_date,
        loop_start_idx=split.holdout_lo,
        loop_end_idx_exclusive=split.holdout_hi,
        cost_bps=cost_bps,
        decide_options=decide_options,
        decide_fn=decide_fn,
    )
    wfe = {
        "available": False,
        "walk_forward_efficiency": None,
        "passes_robust_bar": False,
        "robust_bar": [WFE_ROBUST_LOW, WFE_ROBUST_HIGH],
        "note": (
            "The sealed window is a single final block. It is not split again, "
            "and this function does not recompute the development window."
        ),
    }
    gate = _gate(result, wfe, include_plateau_flag=True)
    labels = _window_labels(aligned.qqq.daily.index, split)
    return PhaseEvaluation(
        role="sealed",
        result=result,
        metrics=gate["metrics"],  # type: ignore[arg-type]
        wfe=wfe,
        gate=gate,
        buy_hold_qqq_pct=buy_and_hold_close_to_close(
            aligned.qqq.daily, split.holdout_lo, split.holdout_hi
        ),
        buy_hold_tqqq_pct=buy_and_hold_close_to_close(
            aligned.tqqq, split.holdout_lo, split.holdout_hi
        ),
        dates_dropped=aligned.dates_dropped,
        score_weights=tuple(strategy_params.score_weights),
        **labels,
    )


def _fmt_pct(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{value:.2f}%"


def format_phase_report(evaluation: PhaseEvaluation) -> str:
    """Text report. A development report names the holdout dates and not its P&L."""
    result = evaluation.result
    metrics = evaluation.metrics
    stats = result.fill_stats
    wfe = evaluation.wfe.get("walk_forward_efficiency")
    lines = [
        "--- ETF backtest (live decide(), next-bar day limits) ---",
        f"Role: {evaluation.role}",
        f"Frozen live score weights: {evaluation.score_weights}",
        f"Code default weights (not retuned): {DEFAULT_SCORE_WEIGHTS}",
        f"ETF day-limit cost: {result.cost_bps:.1f} bp through the signal close (no second haircut)",
        f"Sessions dropped for missing TQQQ or SQQQ dates: {evaluation.dates_dropped}",
        f"Window: {result.start_utc} -> {result.end_utc} ({result.bars_tested} sessions)",
        (
            "Sealed holdout dates (not evaluated in this report): "
            f"{evaluation.holdout_start_utc} -> {evaluation.holdout_end_utc}"
            if evaluation.role == "development"
            else f"Sealed window: {evaluation.holdout_start_utc} -> {evaluation.holdout_end_utc}"
        ),
        "",
        f"Closed trades: {result.closed_trades} (win rate {result.win_rate_pct:.1f}%)",
        f"Compounded closed-trade return: {result.total_return_pct:.2f}%",
        f"Closed-trade max drawdown: {result.max_drawdown_pct:.2f}%",
        f"Marked-to-market return: {result.mtm_total_return_pct:.2f}%",
        f"Marked-to-market max drawdown: {result.mtm_max_drawdown_pct:.2f}%",
        f"Exposure (decision bars in a position): {result.exposure_pct:.1f}%",
        f"Buy-and-hold QQQ, same dates, close to close: {evaluation.buy_hold_qqq_pct:.2f}%",
        f"Buy-and-hold TQQQ, same dates, close to close: {evaluation.buy_hold_tqqq_pct:.2f}%",
        f"Average realized entry cost vs signal close: {_fmt_pct(result.average_entry_cost_bps).replace('%', ' bp')}",
        "",
        f"Profit factor: {metrics.get('profit_factor')}",
        f"Reward/risk: {metrics.get('reward_risk')}",
        f"Max DD / average DD: {metrics.get('max_dd_to_average_dd')}",
        f"PROM return: {(metrics.get('prom') or {}).get('prom_return_pct')}",
        f"Development-only WFE: {wfe}",
        f"Paper gate passes: {evaluation.gate.get('passes')}",
        f"Gate flags: {', '.join(evaluation.gate.get('flags') or [])}",
        "",
        (
            "Fills: "
            f"BUY {stats.buy_fills}/{stats.buy_signals} "
            f"(miss {stats.buy_misses}), "
            f"SELL {stats.sell_fills}/{stats.sell_signals} "
            f"(miss {stats.sell_misses}), "
            f"FLIP sell {stats.flip_sell_fills} miss {stats.flip_sell_misses}, "
            f"second leg blocked {stats.flip_second_leg_blocked}, "
            f"FLIP buy {stats.flip_buy_fills} miss {stats.flip_buy_misses}, "
            f"no next bar {stats.signals_without_next_bar}"
        ),
        "",
        "Research only. This is not a live-trading approval. Live weights were not changed.",
    ]
    return "\n".join(lines)


def write_seal(path: Path, evaluation: PhaseEvaluation) -> None:
    """Record that the sealed window was looked at once."""
    payload = {
        "role": evaluation.role,
        "window_start": evaluation.result.start_utc,
        "window_end": evaluation.result.end_utc,
        "total_return_pct": evaluation.result.total_return_pct,
        "closed_trades": evaluation.result.closed_trades,
        "max_drawdown_pct": evaluation.result.max_drawdown_pct,
        "exposure_pct": evaluation.result.exposure_pct,
        "buy_hold_qqq_pct": evaluation.buy_hold_qqq_pct,
        "buy_hold_tqqq_pct": evaluation.buy_hold_tqqq_pct,
        "score_weights": list(evaluation.score_weights),
        "note": "One sealed ETF look. Do not delete this file to rerun the window.",
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
