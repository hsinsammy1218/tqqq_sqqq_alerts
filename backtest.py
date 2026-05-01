from __future__ import annotations

from dataclasses import dataclass

from data import CandleData
from indicators import build_snapshot
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
    closed_trades: int
    winning_trades: int
    losing_trades: int
    win_rate_pct: float
    equity_start: float
    equity_end: float
    total_return_pct: float
    open_symbol: str | None
    open_entry_price: float | None
    open_unrealized_pct: float | None


@dataclass
class _OpenTrade:
    symbol: str
    entry_price: float


def _side_multiplier(symbol: str) -> float:
    return 1.0 if symbol == "TQQQ" else -1.0


def _trade_return_pct(symbol: str, entry_price: float, exit_price: float) -> float:
    move = (exit_price - entry_price) / entry_price
    return _side_multiplier(symbol) * move * 100.0


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
    wins = 0
    losses = 0
    closed = 0

    for i in range(start_idx, len(daily)):
        now_ts = daily.index[i]
        now_dt = now_ts.to_pydatetime()  # timezone matches daily index (typically UTC)
        daily_slice = daily.iloc[: i + 1]
        h4_slice = four_hour[four_hour.index <= now_ts]
        if len(h4_slice) < 5:
            continue

        snapshot = build_snapshot(daily_slice, h4_slice, anchor_date)
        alert, new_position, _ = decide(
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
                )
        elif alert.alert_type == "SELL":
            sells += 1
            if open_trade is not None:
                ret_pct = _trade_return_pct(open_trade.symbol, open_trade.entry_price, close_px)
                equity *= 1.0 + (ret_pct / 100.0)
                closed += 1
                if ret_pct >= 0:
                    wins += 1
                else:
                    losses += 1
                open_trade = None
        elif alert.alert_type == "FLIP":
            flips += 1
            if open_trade is not None:
                ret_pct = _trade_return_pct(open_trade.symbol, open_trade.entry_price, close_px)
                equity *= 1.0 + (ret_pct / 100.0)
                closed += 1
                if ret_pct >= 0:
                    wins += 1
                else:
                    losses += 1
            if new_position.active_symbol:
                open_trade = _OpenTrade(
                    symbol=new_position.active_symbol,
                    entry_price=float(new_position.entry_price or close_px),
                )

        position = new_position

    open_unrealized: float | None = None
    if open_trade is not None:
        last_close = float(daily.iloc[-1]["close"])
        open_unrealized = _trade_return_pct(open_trade.symbol, open_trade.entry_price, last_close)

    win_rate = (wins / closed * 100.0) if closed else 0.0
    total_return = (equity - 1.0) * 100.0
    return BacktestResult(
        bars_tested=tested,
        start_utc=start_ts.isoformat(),
        end_utc=end_ts.isoformat(),
        buys=buys,
        sells=sells,
        flips=flips,
        closed_trades=closed,
        winning_trades=wins,
        losing_trades=losses,
        win_rate_pct=win_rate,
        equity_start=1.0,
        equity_end=equity,
        total_return_pct=total_return,
        open_symbol=open_trade.symbol if open_trade else None,
        open_entry_price=open_trade.entry_price if open_trade else None,
        open_unrealized_pct=open_unrealized,
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
            f"Signals: BUY={result.buys}, SELL={result.sells}, FLIP={result.flips}",
            (
                "Closed trades: "
                f"{result.closed_trades} (wins={result.winning_trades}, "
                f"losses={result.losing_trades}, win rate={result.win_rate_pct:.1f}%)"
            ),
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
