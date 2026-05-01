"""Grid search over strategy thresholds for backtests (alert-only; QQQ proxy)."""

from __future__ import annotations

import csv
import itertools
import os
from dataclasses import dataclass, replace
from pathlib import Path

from config import ConfigError, Settings
from data import CandleData
from strategy_params import StrategyParams

from backtest import BacktestResult, run_backtest

# Composite score weights (within-batch min–max normalization per metric).
_W_TOTAL_RETURN = 0.30
_W_WIN_RATE = 0.25
_W_AVG_TRADE_RETURN = 0.25
_W_MAX_DRAWDOWN = 0.15  # lower drawdown is better
_W_FLIPS = 0.05  # fewer flips is better


@dataclass(frozen=True)
class SweepGrid:
    bull_entry: tuple[int, ...]
    bear_entry: tuple[int, ...]
    weak: tuple[int, ...]
    regime_ranging_add: tuple[float, ...]
    flip_min_hold: tuple[int, ...]
    flip_margin: tuple[float, ...]

    @property
    def combination_count(self) -> int:
        return (
            len(self.bull_entry)
            * len(self.bear_entry)
            * len(self.weak)
            * len(self.regime_ranging_add)
            * len(self.flip_min_hold)
            * len(self.flip_margin)
        )


@dataclass(frozen=True)
class SweepResultRow:
    rank: int
    balanced_score: float
    bull_entry_threshold: int
    bear_entry_threshold: int
    weak_score_threshold: int
    regime_ranging_threshold_weight_add: float
    flip_min_hold_trading_days: int
    flip_margin_weight: float
    total_trades: int
    win_rate_pct: float
    average_return_per_trade_pct: float
    max_drawdown_pct: float
    average_hold_days: float
    flips: int
    equity_end: float
    total_return_pct: float


def _parse_int_csv(env_name: str, raw: str | None, default: int) -> tuple[int, ...]:
    if raw is None or not str(raw).strip():
        return (default,)
    parts = [p.strip() for p in str(raw).split(",") if p.strip()]
    if not parts:
        return (default,)
    try:
        return tuple(int(p) for p in parts)
    except ValueError as exc:
        raise ConfigError(f"{env_name} must be comma-separated integers.") from exc


def _parse_float_csv(env_name: str, raw: str | None, default: float) -> tuple[float, ...]:
    if raw is None or not str(raw).strip():
        return (default,)
    parts = [p.strip() for p in str(raw).split(",") if p.strip()]
    if not parts:
        return (default,)
    try:
        return tuple(float(p) for p in parts)
    except ValueError as exc:
        raise ConfigError(f"{env_name} must be comma-separated numbers.") from exc


def sweep_grid_from_settings(settings: Settings) -> SweepGrid:
    """Read BACKTEST_SWEEP_* lists from environment; missing → single default from Settings."""
    return SweepGrid(
        bull_entry=_parse_int_csv(
            "BACKTEST_SWEEP_BULL_ENTRY_THRESHOLD",
            os.getenv("BACKTEST_SWEEP_BULL_ENTRY_THRESHOLD"),
            settings.bull_entry_threshold,
        ),
        bear_entry=_parse_int_csv(
            "BACKTEST_SWEEP_BEAR_ENTRY_THRESHOLD",
            os.getenv("BACKTEST_SWEEP_BEAR_ENTRY_THRESHOLD"),
            settings.bear_entry_threshold,
        ),
        weak=_parse_int_csv(
            "BACKTEST_SWEEP_WEAK_SCORE_THRESHOLD",
            os.getenv("BACKTEST_SWEEP_WEAK_SCORE_THRESHOLD"),
            settings.weak_score_threshold,
        ),
        regime_ranging_add=_parse_float_csv(
            "BACKTEST_SWEEP_REGIME_RANGING_THRESHOLD_WEIGHT_ADD",
            os.getenv("BACKTEST_SWEEP_REGIME_RANGING_THRESHOLD_WEIGHT_ADD"),
            settings.regime_ranging_threshold_weight_add,
        ),
        flip_min_hold=_parse_int_csv(
            "BACKTEST_SWEEP_FLIP_MIN_HOLD_TRADING_DAYS",
            os.getenv("BACKTEST_SWEEP_FLIP_MIN_HOLD_TRADING_DAYS"),
            settings.flip_min_hold_trading_days,
        ),
        flip_margin=_parse_float_csv(
            "BACKTEST_SWEEP_FLIP_MARGIN_WEIGHT",
            os.getenv("BACKTEST_SWEEP_FLIP_MARGIN_WEIGHT"),
            settings.flip_margin_weight,
        ),
    )


def _batch_norm(values: list[float], *, higher_is_better: bool) -> list[float]:
    if not values:
        return []
    lo = min(values)
    hi = max(values)
    if hi == lo:
        return [0.5 for _ in values]
    out: list[float] = []
    for v in values:
        t = (v - lo) / (hi - lo)
        if not higher_is_better:
            t = 1.0 - t
        out.append(t)
    return out


def run_parameter_sweep(
    candles: CandleData,
    *,
    anchor_date: str | None,
    blocked_dates: set[str],
    base_params: StrategyParams,
    bars: int,
    grid: SweepGrid,
) -> list[SweepResultRow]:
    combinations: list[SweepResultRow] = []
    for bull, bear, weak, rng_add, f_hold, f_margin in itertools.product(
        grid.bull_entry,
        grid.bear_entry,
        grid.weak,
        grid.regime_ranging_add,
        grid.flip_min_hold,
        grid.flip_margin,
    ):
        params = replace(
            base_params,
            bull_entry_threshold=bull,
            bear_entry_threshold=bear,
            weak_threshold=weak,
            regime_ranging_threshold_weight_add=rng_add,
            flip_min_hold_trading_days=f_hold,
            flip_margin_weight=f_margin,
        )
        result = run_backtest(
            candles,
            anchor_date=anchor_date,
            blocked_dates=blocked_dates,
            strategy_params=params,
            bars=bars,
        )
        combinations.append(
            _result_to_row(
                bull=bull,
                bear=bear,
                weak=weak,
                rng_add=rng_add,
                flip_hold=f_hold,
                flip_margin=f_margin,
                bt=result,
            )
        )

    total_returns = [float(r.total_return_pct) for r in combinations]
    win_rates = [float(r.win_rate_pct) for r in combinations]
    avg_rets = [float(r.average_return_per_trade_pct) for r in combinations]
    drawdowns = [float(r.max_drawdown_pct) for r in combinations]
    flip_counts = [float(r.flips) for r in combinations]

    n_tr = _batch_norm(total_returns, higher_is_better=True)
    n_wr = _batch_norm(win_rates, higher_is_better=True)
    n_ar = _batch_norm(avg_rets, higher_is_better=True)
    n_dd = _batch_norm(drawdowns, higher_is_better=False)
    n_fl = _batch_norm(flip_counts, higher_is_better=False)

    scored: list[tuple[float, SweepResultRow]] = []
    for i, row in enumerate(combinations):
        bal = (
            _W_TOTAL_RETURN * n_tr[i]
            + _W_WIN_RATE * n_wr[i]
            + _W_AVG_TRADE_RETURN * n_ar[i]
            + _W_MAX_DRAWDOWN * n_dd[i]
            + _W_FLIPS * n_fl[i]
        )
        scored.append(
            (
                bal,
                SweepResultRow(
                    rank=0,
                    balanced_score=bal,
                    bull_entry_threshold=row.bull_entry_threshold,
                    bear_entry_threshold=row.bear_entry_threshold,
                    weak_score_threshold=row.weak_score_threshold,
                    regime_ranging_threshold_weight_add=row.regime_ranging_threshold_weight_add,
                    flip_min_hold_trading_days=row.flip_min_hold_trading_days,
                    flip_margin_weight=row.flip_margin_weight,
                    total_trades=row.total_trades,
                    win_rate_pct=row.win_rate_pct,
                    average_return_per_trade_pct=row.average_return_per_trade_pct,
                    max_drawdown_pct=row.max_drawdown_pct,
                    average_hold_days=row.average_hold_days,
                    flips=row.flips,
                    equity_end=row.equity_end,
                    total_return_pct=row.total_return_pct,
                ),
            )
        )

    scored.sort(key=lambda x: x[0], reverse=True)
    ranked: list[SweepResultRow] = []
    for idx, (_, row) in enumerate(scored, start=1):
        ranked.append(
            SweepResultRow(
                rank=idx,
                balanced_score=row.balanced_score,
                bull_entry_threshold=row.bull_entry_threshold,
                bear_entry_threshold=row.bear_entry_threshold,
                weak_score_threshold=row.weak_score_threshold,
                regime_ranging_threshold_weight_add=row.regime_ranging_threshold_weight_add,
                flip_min_hold_trading_days=row.flip_min_hold_trading_days,
                flip_margin_weight=row.flip_margin_weight,
                total_trades=row.total_trades,
                win_rate_pct=row.win_rate_pct,
                average_return_per_trade_pct=row.average_return_per_trade_pct,
                max_drawdown_pct=row.max_drawdown_pct,
                average_hold_days=row.average_hold_days,
                flips=row.flips,
                equity_end=row.equity_end,
                total_return_pct=row.total_return_pct,
            )
        )
    return ranked


def _result_to_row(
    *,
    bull: int,
    bear: int,
    weak: int,
    rng_add: float,
    flip_hold: int,
    flip_margin: float,
    bt: BacktestResult,
) -> SweepResultRow:
    return SweepResultRow(
        rank=0,
        balanced_score=0.0,
        bull_entry_threshold=bull,
        bear_entry_threshold=bear,
        weak_score_threshold=weak,
        regime_ranging_threshold_weight_add=rng_add,
        flip_min_hold_trading_days=flip_hold,
        flip_margin_weight=flip_margin,
        total_trades=bt.total_trades,
        win_rate_pct=bt.win_rate_pct,
        average_return_per_trade_pct=bt.average_return_per_trade_pct,
        max_drawdown_pct=bt.max_drawdown_pct,
        average_hold_days=bt.average_hold_days,
        flips=bt.flips,
        equity_end=bt.equity_end,
        total_return_pct=bt.total_return_pct,
    )


def format_sweep_report(rows: list[SweepResultRow], *, ticker: str, bars: int, combo_count: int) -> str:
    lines = [
        "--- Backtest parameter sweep (QQQ-rule proxy; ranked by balanced score) ---",
        f"Ticker: {ticker} | Bars setting: {bars} | Combinations: {combo_count}",
        "",
        "Balanced score: min-max normalized within this sweep - rewards total return, win rate, avg/trade return;",
        "penalizes max drawdown and flip count (see backtest_sweep.py weights).",
        "",
        "rank | score | bull | bear | weak | rng_add | flip_hold | flip_margin | trades | win% | avgRet% | maxDD% | "
        "holdDays | flips | equity | totRet%",
        "-" * 120,
    ]
    for r in rows:
        lines.append(
            f"{r.rank:4d} | {r.balanced_score:.4f} | {r.bull_entry_threshold:4d} | {r.bear_entry_threshold:4d} | "
            f"{r.weak_score_threshold:4d} | {r.regime_ranging_threshold_weight_add:7.2f} | "
            f"{r.flip_min_hold_trading_days:9d} | {r.flip_margin_weight:11.2f} | "
            f"{r.total_trades:6d} | {r.win_rate_pct:5.1f} | {r.average_return_per_trade_pct:7.2f} | "
            f"{r.max_drawdown_pct:6.2f} | {r.average_hold_days:8.2f} | {r.flips:5d} | "
            f"{r.equity_end:.4f} | {r.total_return_pct:7.2f}%"
        )
    lines.extend(
        [
            "",
            "Notes: QQQ close-to-close directional proxy only "
            "(TQQQ ~ long QQQ, SQQQ ~ short QQQ); not broker execution or ETF-accurate PnL.",
        ]
    )
    return "\n".join(lines)


def export_sweep_csv(path: str, rows: list[SweepResultRow]) -> None:
    fields = [
        "rank",
        "balanced_score",
        "bull_entry_threshold",
        "bear_entry_threshold",
        "weak_score_threshold",
        "regime_ranging_threshold_weight_add",
        "flip_min_hold_trading_days",
        "flip_margin_weight",
        "total_trades",
        "win_rate_pct",
        "average_return_per_trade_pct",
        "max_drawdown_pct",
        "average_hold_days",
        "flips",
        "equity_end",
        "total_return_pct",
    ]
    out_path = Path(path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for r in rows:
            writer.writerow(
                {
                    "rank": r.rank,
                    "balanced_score": round(r.balanced_score, 6),
                    "bull_entry_threshold": r.bull_entry_threshold,
                    "bear_entry_threshold": r.bear_entry_threshold,
                    "weak_score_threshold": r.weak_score_threshold,
                    "regime_ranging_threshold_weight_add": round(r.regime_ranging_threshold_weight_add, 6),
                    "flip_min_hold_trading_days": r.flip_min_hold_trading_days,
                    "flip_margin_weight": round(r.flip_margin_weight, 6),
                    "total_trades": r.total_trades,
                    "win_rate_pct": round(r.win_rate_pct, 4),
                    "average_return_per_trade_pct": round(r.average_return_per_trade_pct, 6),
                    "max_drawdown_pct": round(r.max_drawdown_pct, 6),
                    "average_hold_days": round(r.average_hold_days, 6),
                    "flips": r.flips,
                    "equity_end": round(r.equity_end, 6),
                    "total_return_pct": round(r.total_return_pct, 6),
                }
            )
