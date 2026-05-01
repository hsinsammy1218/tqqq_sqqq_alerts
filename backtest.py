from __future__ import annotations

import csv
import math
import statistics
from collections import Counter
from dataclasses import dataclass, field, fields
from datetime import datetime
from pathlib import Path

import pandas as pd

from data import CandleData
from indicators import IndicatorSnapshot, build_snapshot
from strategy_scoring import trading_days_between_inclusive
from strategy_params import StrategyParams
from strategy import DecideOptions, PositionState, decide


@dataclass(frozen=True)
class BacktestResult:
    bars_tested: int
    start_utc: str
    end_utc: str
    buys: int
    sells: int
    flips: int
    cash_no_trade_periods: int
    total_trades: int
    closed_trades: int
    winning_trades: int
    losing_trades: int
    win_rate_pct: float
    average_return_per_trade_pct: float
    median_return_per_trade_pct: float | None
    best_trade_pct: float | None
    worst_trade_pct: float | None
    max_drawdown_pct: float
    average_hold_days: float
    equity_start: float
    equity_end: float
    total_return_pct: float
    open_symbol: str | None
    open_entry_price: float | None
    open_unrealized_pct: float | None
    trade_rows: tuple["BacktestTradeRow", ...]


@dataclass
class _OpenTrade:
    symbol: str
    entry_price: float
    entry_timestamp: str


@dataclass(frozen=True)
class BacktestTradeRow:
    timestamp: str
    action: str
    symbol: str
    entry_price: float
    exit_price: float
    return_pct: float
    hold_days: int
    bull_strength: int
    bear_strength: int
    confidence: int
    regime: str
    reason: str


def _side_multiplier(symbol: str) -> float:
    return 1.0 if symbol == "TQQQ" else -1.0


def _trade_return_pct(symbol: str, entry_price: float, exit_price: float) -> float:
    move = (exit_price - entry_price) / entry_price
    return _side_multiplier(symbol) * move * 100.0


def _utc_calendar_date(ts: object) -> object:
    """Normalize to UTC calendar date for aligning daily vs intraday indices."""
    t = pd.Timestamp(ts)
    if t.tzinfo is None:
        return t.date()
    return t.tz_convert("UTC").date()


def synthetic_h4_fallback_from_daily(daily_slice: pd.DataFrame, *, min_rows: int = 5) -> pd.DataFrame:
    """Approximate a minimal intraday series from trailing daily OHLCV.

    Some data feeds return only a short trailing intraday window while daily history is long.
    Without this, expanding-window backtests skip every bar (no 4H context through older dates).
    Degraded vs real 4H; backtests only — live runs still use vendor intraday as fetched.
    """
    n = min(len(daily_slice), min_rows)
    tail = daily_slice.iloc[-n:]
    return pd.DataFrame(
        {
            "open": tail["open"],
            "high": tail["high"],
            "low": tail["low"],
            "close": tail["close"],
            "volume": tail["volume"],
        },
        index=tail.index,
    )


def h4_slice_through_daily_calendar_date(daily_ts: object, four_hour: pd.DataFrame) -> pd.DataFrame:
    """Return 4H bars through the daily bar's UTC calendar date.

    Daily candles often stamp midnight UTC while resampled 4H bars fall later the same day;
    filtering with ``four_hour.index <= daily_ts`` drops same-session intraday rows and can skip
    every backtest bar (len(h4) < 5).
    """
    if four_hour.empty:
        return four_hour
    cutoff_day = _utc_calendar_date(daily_ts)
    idx_days = four_hour.index.map(_utc_calendar_date)
    return four_hour[idx_days <= cutoff_day]


def _snapshot_nan_fields(snapshot: IndicatorSnapshot) -> tuple[str, ...]:
    bad: list[str] = []
    for f in fields(snapshot):
        val = getattr(snapshot, f.name)
        if isinstance(val, float) and math.isnan(val):
            bad.append(f.name)
    return tuple(bad)


@dataclass
class _DebugStrategyAccum:
    decisions_seen: int = 0
    thin_real_h4: int = 0
    max_bull_pct: int = -1
    min_bull_pct: int = 10**9
    max_bear_pct: int = -1
    min_bear_pct: int = 10**9
    max_wb: float = -1e18
    min_wb: float = 1e18
    max_wbear: float = -1e18
    min_wbear: float = 1e18
    regime_counts: Counter[str] = field(default_factory=Counter)
    close_bull_near: int = 0
    close_bear_near: int = 0
    nan_indicator_rows: int = 0
    flat_blocked_days: int = 0


def _print_debug_strategy_footer(
    *,
    strategy_params: StrategyParams,
    dbg: _DebugStrategyAccum,
    sanity_dominate: bool,
) -> None:
    """Console-only diagnostics; gated by --debug-strategy."""
    print()
    print("[debug-strategy] --- aggregate (bars that reached decide()) ---")
    print(f"  Bars evaluated (decide calls): {dbg.decisions_seen}")
    print(
        "  Bars with sparse real 4H through date (daily-derived 4H fallback used): "
        f"{dbg.thin_real_h4}"
    )
    min_b = dbg.min_bull_pct if dbg.decisions_seen else None
    min_be = dbg.min_bear_pct if dbg.decisions_seen else None
    print(
        f"  Max bull strength %: {dbg.max_bull_pct if dbg.decisions_seen else 'n/a'} "
        f"| Min bull strength %: {min_b if min_b is not None else 'n/a'}"
    )
    print(
        f"  Max bear strength %: {dbg.max_bear_pct if dbg.decisions_seen else 'n/a'} "
        f"| Min bear strength %: {min_be if min_be is not None else 'n/a'}"
    )
    if dbg.decisions_seen:
        print(f"  Weighted bull max/min: {dbg.max_wb:.3f} / {dbg.min_wb:.3f}")
        print(f"  Weighted bear max/min: {dbg.max_wbear:.3f} / {dbg.min_wbear:.3f}")
    else:
        print("  Weighted bull max/min: n/a")
        print("  Weighted bear max/min: n/a")
    print(f"  Regime counts: {dict(dbg.regime_counts)}")
    print(
        "  Near-entry (flat, not blocked, within 20% of weighted bull threshold "
        f"but below gate): {dbg.close_bull_near}"
    )
    print(
        "  Near-entry (flat, not blocked, within 20% of weighted bear threshold "
        f"but below gate): {dbg.close_bear_near}"
    )
    print(f"  Rows with NaN indicator fields: {dbg.nan_indicator_rows}")
    print(f"  Flat + blocked (entry suppressed): {dbg.flat_blocked_days}")
    if sanity_dominate:
        print("  SANITY mode: threshold gates bypassed for entries (dominance-only).")
    print()
    print("[debug-strategy] --- heuristic diagnosis ---")
    bull_tgt = (strategy_params.bull_entry_threshold / 8.0) * strategy_params.weight_scale
    bear_tgt = (strategy_params.bear_entry_threshold / 8.0) * strategy_params.weight_scale
    scale = strategy_params.weight_scale
    print(
        f"  Base weighted targets (pre-regime): bull~{bull_tgt:.3f}, bear~{bear_tgt:.3f} "
        f"(scale={scale:.3f}). Range regime adds +{strategy_params.regime_ranging_threshold_weight_add:g} "
        "to BOTH entry gates - strict chop can block symmetric entries."
    )
    print(
        "  Dominance fallback: ENTRY_DOMINANCE_GAP_WEIGHT="
        f"{strategy_params.entry_dominance_gap_weight:g} weighted units "
        "(0 disables)."
    )
    if (
        dbg.decisions_seen
        and dbg.max_wb < bull_tgt * 0.5
        and dbg.max_wbear < bear_tgt * 0.5
    ):
        print("  Observation: peak stacks stayed well below typical entry targets - check data / NaNs / 4H alignment.")
    elif dbg.decisions_seen and (dbg.close_bull_near + dbg.close_bear_near) > dbg.decisions_seen // 3:
        print("  Observation: many near-miss bars - thresholds/regime penalty likely tight vs realized stacks.")
    if dbg.nan_indicator_rows:
        print("  Observation: NaNs present - checklist votes may be silently False; inspect upstream series.")
    if dbg.decisions_seen and dbg.thin_real_h4 > dbg.decisions_seen // 2:
        print(
            "  Observation: vendor 4H/intraday window is short vs daily history - "
            "fallback stitches trailing daily bars as pseudo-4H (see synthetic_h4_fallback_from_daily)."
        )
    print(
        "  4H alignment: decide() uses the latest 4H bar with timestamp <= each daily bar; "
        "large gaps between daily date and h4_last in the sample table can mute intraday confirmation."
    )
    print(
        "[debug-strategy] Note: report 'total_trades' counts CLOSED round-trips only; "
        "open positions at the window end have buys/flips without a recorded exit yet."
    )


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


def export_backtest_trades_csv(path: str, trade_rows: tuple[BacktestTradeRow, ...]) -> None:
    fields = [
        "timestamp",
        "action",
        "symbol",
        "entry_price",
        "exit_price",
        "return_pct",
        "hold_days",
        "bull_strength",
        "bear_strength",
        "confidence",
        "regime",
        "reason",
    ]
    out_path = Path(path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for row in trade_rows:
            writer.writerow(
                {
                    "timestamp": row.timestamp,
                    "action": row.action,
                    "symbol": row.symbol,
                    "entry_price": round(row.entry_price, 4),
                    "exit_price": round(row.exit_price, 4),
                    "return_pct": round(row.return_pct, 4),
                    "hold_days": row.hold_days,
                    "bull_strength": row.bull_strength,
                    "bear_strength": row.bear_strength,
                    "confidence": row.confidence,
                    "regime": row.regime,
                    "reason": row.reason,
                }
            )


def run_backtest(
    candles: CandleData,
    *,
    anchor_date: str | None,
    blocked_dates: set[str],
    strategy_params: StrategyParams,
    bars: int,
    debug_strategy: bool = False,
    decide_options: DecideOptions | None = None,
) -> BacktestResult:
    daily = candles.daily
    four_hour = candles.four_hour
    if len(daily) < 80:
        raise ValueError("Need at least 80 daily bars for backtest.")
    if bars < 20:
        raise ValueError("--backtest-bars must be >= 20.")

    start_idx = max(60, len(daily) - bars)
    tested = len(daily) - start_idx
    start_ts = daily.index[start_idx]
    end_ts = daily.index[-1]

    position = PositionState()
    open_trade: _OpenTrade | None = None
    equity = 1.0

    buys = 0
    sells = 0
    flips = 0
    cash_no_trade_periods = 0
    wins = 0
    losses = 0
    closed = 0
    trade_returns: list[float] = []
    hold_days_values: list[int] = []
    trade_rows: list[BacktestTradeRow] = []
    equity_points: list[float] = [equity]

    dbg_accum = _DebugStrategyAccum()
    sample_lines: list[str] = []
    sanity_dominate = bool((decide_options or DecideOptions()).debug_sanity_dominate)

    for i in range(start_idx, len(daily)):
        now_ts = daily.index[i]
        now_dt = now_ts.to_pydatetime()  # timezone matches daily index (typically UTC)
        daily_slice = daily.iloc[: i + 1]
        h4_real = h4_slice_through_daily_calendar_date(now_ts, four_hour)
        if len(h4_real) < 5:
            if debug_strategy:
                dbg_accum.thin_real_h4 += 1
            h4_slice = synthetic_h4_fallback_from_daily(daily_slice, min_rows=5)
        else:
            h4_slice = h4_real

        snapshot = build_snapshot(daily_slice, h4_slice, anchor_date)
        alert, new_position, dbg = decide(
            snapshot=snapshot,
            position=position,
            blocked_dates=blocked_dates,
            now_utc=now_dt,
            params=strategy_params,
            decide_options=decide_options,
        )

        if debug_strategy:
            dbg_accum.decisions_seen += 1
            bp, bep = alert.bullish_score, alert.bearish_score
            dbg_accum.max_bull_pct = max(dbg_accum.max_bull_pct, bp)
            dbg_accum.min_bull_pct = min(dbg_accum.min_bull_pct, bp)
            dbg_accum.max_bear_pct = max(dbg_accum.max_bear_pct, bep)
            dbg_accum.min_bear_pct = min(dbg_accum.min_bear_pct, bep)
            wb_i = dbg.weighted_bull
            wbear_i = dbg.weighted_bear
            dbg_accum.max_wb = max(dbg_accum.max_wb, wb_i)
            dbg_accum.min_wb = min(dbg_accum.min_wb, wb_i)
            dbg_accum.max_wbear = max(dbg_accum.max_wbear, wbear_i)
            dbg_accum.min_wbear = min(dbg_accum.min_wbear, wbear_i)
            dbg_accum.regime_counts[dbg.regime] += 1
            today_iso = now_dt.date().isoformat()
            blocked_today = today_iso in blocked_dates
            if position.active_symbol is None and blocked_today:
                dbg_accum.flat_blocked_days += 1
            be_bull = dbg.effective_bull_entry
            be_bear = dbg.effective_bear_entry
            if (
                position.active_symbol is None
                and not blocked_today
                and be_bull > 0
                and wb_i < be_bull
                and wb_i >= 0.8 * be_bull
                and wbear_i < be_bear
            ):
                dbg_accum.close_bull_near += 1
            if (
                position.active_symbol is None
                and not blocked_today
                and be_bear > 0
                and wbear_i < be_bear
                and wbear_i >= 0.8 * be_bear
                and wb_i < be_bull
            ):
                dbg_accum.close_bear_near += 1
            if _snapshot_nan_fields(snapshot):
                dbg_accum.nan_indicator_rows += 1
            if len(sample_lines) < 20:
                h4_ts = h4_slice.index[-1]
                if hasattr(h4_ts, "isoformat"):
                    h4_lab = h4_ts.isoformat()[:19].replace("T", " ")
                else:
                    h4_lab = str(h4_ts)[:19]
                sample_lines.append(
                    f"{len(sample_lines):2d} | {str(now_ts)[:10]} | {bp:3d} {bep:3d} | {dbg.regime:10s} | "
                    f"{be_bull:5.2f} {be_bear:5.2f} | {wb_i:4.2f} {wbear_i:5.2f} | "
                    f"{alert.alert_type:4s} {alert.symbol:4s} | "
                    f"{'blk' if blocked_today else 'ok '} | {h4_lab}"
                )

        close_px = snapshot.daily_close
        if alert.alert_type == "BUY":
            buys += 1
            if new_position.active_symbol:
                open_trade = _OpenTrade(
                    symbol=new_position.active_symbol,
                    entry_price=float(new_position.entry_price or close_px),
                    entry_timestamp=alert.timestamp,
                )
        elif alert.alert_type == "SELL":
            sells += 1
            if open_trade is not None:
                ret_pct = _trade_return_pct(open_trade.symbol, open_trade.entry_price, close_px)
                equity *= 1.0 + (ret_pct / 100.0)
                closed += 1
                trade_returns.append(ret_pct)
                entry_day = datetime.fromisoformat(
                    open_trade.entry_timestamp.replace("Z", "+00:00")
                ).date()
                hold_days = trading_days_between_inclusive(entry_day, now_dt.date())
                hold_days_values.append(hold_days)
                trade_rows.append(
                    BacktestTradeRow(
                        timestamp=alert.timestamp,
                        action="SELL",
                        symbol=open_trade.symbol,
                        entry_price=open_trade.entry_price,
                        exit_price=close_px,
                        return_pct=ret_pct,
                        hold_days=hold_days,
                        bull_strength=alert.bullish_score,
                        bear_strength=alert.bearish_score,
                        confidence=alert.confidence_score,
                        regime=dbg.regime,
                        reason=alert.qqq_trend_reason,
                    )
                )
                if ret_pct >= 0:
                    wins += 1
                else:
                    losses += 1
                open_trade = None
                equity_points.append(equity)
        elif alert.alert_type == "FLIP":
            flips += 1
            if open_trade is not None:
                ret_pct = _trade_return_pct(open_trade.symbol, open_trade.entry_price, close_px)
                equity *= 1.0 + (ret_pct / 100.0)
                closed += 1
                trade_returns.append(ret_pct)
                entry_day = datetime.fromisoformat(
                    open_trade.entry_timestamp.replace("Z", "+00:00")
                ).date()
                hold_days = trading_days_between_inclusive(entry_day, now_dt.date())
                hold_days_values.append(hold_days)
                trade_rows.append(
                    BacktestTradeRow(
                        timestamp=alert.timestamp,
                        action="FLIP",
                        symbol=open_trade.symbol,
                        entry_price=open_trade.entry_price,
                        exit_price=close_px,
                        return_pct=ret_pct,
                        hold_days=hold_days,
                        bull_strength=alert.bullish_score,
                        bear_strength=alert.bearish_score,
                        confidence=alert.confidence_score,
                        regime=dbg.regime,
                        reason=alert.qqq_trend_reason,
                    )
                )
                if ret_pct >= 0:
                    wins += 1
                else:
                    losses += 1
                equity_points.append(equity)
            if new_position.active_symbol:
                open_trade = _OpenTrade(
                    symbol=new_position.active_symbol,
                    entry_price=float(new_position.entry_price or close_px),
                    entry_timestamp=alert.timestamp,
                )
        elif alert.alert_type == "CASH" and position.active_symbol is None:
            cash_no_trade_periods += 1

        position = new_position

    open_unrealized: float | None = None
    if open_trade is not None:
        last_close = float(daily.iloc[-1]["close"])
        open_unrealized = _trade_return_pct(open_trade.symbol, open_trade.entry_price, last_close)

    total_trades = closed
    win_rate = (wins / closed * 100.0) if closed else 0.0
    avg_ret = (sum(trade_returns) / len(trade_returns)) if trade_returns else 0.0
    median_ret = float(statistics.median(trade_returns)) if trade_returns else None
    best_trade = max(trade_returns) if trade_returns else None
    worst_trade = min(trade_returns) if trade_returns else None
    avg_hold_days = (sum(hold_days_values) / len(hold_days_values)) if hold_days_values else 0.0
    max_drawdown = _max_drawdown_pct(equity_points)
    total_return = (equity - 1.0) * 100.0

    if debug_strategy:
        print("[debug-strategy] --- sample rows (first <=20 bars that reached decide()) ---")
        header = (
            "ix | date       | bull bear | regime     | bThr bearT | wb    wbear | dec  sym | blk | h4_last"
        )
        print(header)
        print("-" * len(header))
        for line in sample_lines:
            print(line)
        _print_debug_strategy_footer(
            strategy_params=strategy_params,
            dbg=dbg_accum,
            sanity_dominate=sanity_dominate,
        )

    return BacktestResult(
        bars_tested=tested,
        start_utc=start_ts.isoformat(),
        end_utc=end_ts.isoformat(),
        buys=buys,
        sells=sells,
        flips=flips,
        cash_no_trade_periods=cash_no_trade_periods,
        total_trades=total_trades,
        closed_trades=closed,
        winning_trades=wins,
        losing_trades=losses,
        win_rate_pct=win_rate,
        average_return_per_trade_pct=avg_ret,
        median_return_per_trade_pct=median_ret,
        best_trade_pct=best_trade,
        worst_trade_pct=worst_trade,
        max_drawdown_pct=max_drawdown,
        average_hold_days=avg_hold_days,
        equity_start=1.0,
        equity_end=equity,
        total_return_pct=total_return,
        open_symbol=open_trade.symbol if open_trade else None,
        open_entry_price=open_trade.entry_price if open_trade else None,
        open_unrealized_pct=open_unrealized,
        trade_rows=tuple(trade_rows),
    )


def format_backtest_report(result: BacktestResult, ticker: str) -> str:
    open_line = "None"
    if result.open_symbol and result.open_entry_price is not None:
        open_line = f"{result.open_symbol} @ {result.open_entry_price:.2f}"
        if result.open_unrealized_pct is not None:
            open_line += f" (unrealized {result.open_unrealized_pct:.2f}%)"
    return "\n".join(
        [
            "--- Backtest (lightweight, QQQ-rule proxy) ---",
            f"Ticker: {ticker}",
            f"Bars tested: {result.bars_tested}",
            f"Window: {result.start_utc} -> {result.end_utc}",
            "",
            (
                f"Signals: BUY={result.buys}, SELL={result.sells}, FLIP={result.flips}, "
                f"CASH(no-trade)={result.cash_no_trade_periods}"
            ),
            (
                "Closed trades: "
                f"{result.closed_trades} (wins={result.winning_trades}, "
                f"losses={result.losing_trades}, win rate={result.win_rate_pct:.1f}%)"
            ),
            f"Total trades: {result.total_trades}",
            f"Average return/trade: {result.average_return_per_trade_pct:.2f}%",
            (
                f"Median return/trade: {result.median_return_per_trade_pct:.2f}%"
                if result.median_return_per_trade_pct is not None
                else "Median return/trade: N/A"
            ),
            (
                f"Best trade: {result.best_trade_pct:.2f}% | "
                f"Worst trade: {result.worst_trade_pct:.2f}%"
                if result.best_trade_pct is not None and result.worst_trade_pct is not None
                else "Best trade: N/A | Worst trade: N/A"
            ),
            f"Max drawdown: {result.max_drawdown_pct:.2f}%",
            f"Average hold days: {result.average_hold_days:.2f}",
            f"FLIP events: {result.flips}",
            (
                f"Equity: {result.equity_start:.2f} -> {result.equity_end:.4f} "
                f"({result.total_return_pct:.2f}%)"
            ),
            f"Open position at end: {open_line}",
            "",
            (
                "Notes: QQQ close-to-close directional proxy only "
                "(TQQQ ~ long QQQ, SQQQ ~ short QQQ); not broker execution or ETF-accurate PnL."
            ),
            "Note: total_trades counts closed round-trips only (SELL/FLIP completes); "
            "a BUY without exit before window end stays open.",
        ]
    )
