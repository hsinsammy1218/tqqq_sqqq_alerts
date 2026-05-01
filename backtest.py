from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from data import CandleData
from indicators import build_snapshot
from strategy_scoring import trading_days_between_inclusive
from strategy_params import StrategyParams
from strategy import PositionState, decide


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

    for i in range(start_idx, len(daily)):
        now_ts = daily.index[i]
        now_dt = now_ts.to_pydatetime()  # timezone matches daily index (typically UTC)
        daily_slice = daily.iloc[: i + 1]
        h4_slice = four_hour[four_hour.index <= now_ts]
        if len(h4_slice) < 5:
            continue

        snapshot = build_snapshot(daily_slice, h4_slice, anchor_date)
        alert, new_position, dbg = decide(
            snapshot=snapshot,
            position=position,
            blocked_dates=blocked_dates,
            now_utc=now_dt,
            params=strategy_params,
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
    best_trade = max(trade_returns) if trade_returns else None
    worst_trade = min(trade_returns) if trade_returns else None
    avg_hold_days = (sum(hold_days_values) / len(hold_days_values)) if hold_days_values else 0.0
    max_drawdown = _max_drawdown_pct(equity_points)
    total_return = (equity - 1.0) * 100.0
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
        ]
    )
