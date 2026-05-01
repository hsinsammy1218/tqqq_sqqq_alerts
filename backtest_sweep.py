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

# Balanced ranking score (raw units, not min-max normalized). Tune coefficients here only.
# Formula:
#   balanced_score =
#       total_return_pct
#       + (win_rate_pct * WIN_RATE_WEIGHT)
#       - abs(max_drawdown_pct * DRAWDOWN_WEIGHT)
#       - (flip_count * FLIP_PENALTY)
_WIN_RATE_WEIGHT = 0.25
_DRAWDOWN_WEIGHT = 1.5
_FLIP_PENALTY = 0.25


def compute_balanced_score(
    total_return_pct: float,
    win_rate_pct: float,
    max_drawdown_pct: float,
    flip_count: int,
) -> float:
    """Higher is better. Uses simple penalties so ranking is not return-only."""
    return (
        total_return_pct
        + (win_rate_pct * _WIN_RATE_WEIGHT)
        - abs(max_drawdown_pct * _DRAWDOWN_WEIGHT)
        - (flip_count * _FLIP_PENALTY)
    )


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
    average_return_pct: float
    best_trade_pct: float | None
    worst_trade_pct: float | None
    max_drawdown_pct: float
    average_hold_days: float
    flip_count: int
    cash_periods: int
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


def _sweep_env_raw(*keys: str) -> str | None:
    """First non-empty BACKTEST_SWEEP_* value wins (supports legacy alias keys)."""
    for key in keys:
        raw = os.getenv(key)
        if raw is not None and str(raw).strip():
            return raw
    return None


def sweep_grid_from_settings(settings: Settings) -> SweepGrid:
    """Read sweep grids from env (short BACKTEST_SWEEP_* names); legacy long names still honored."""
    return SweepGrid(
        bull_entry=_parse_int_csv(
            "BACKTEST_SWEEP_BULL",
            _sweep_env_raw("BACKTEST_SWEEP_BULL", "BACKTEST_SWEEP_BULL_ENTRY_THRESHOLD"),
            settings.bull_entry_threshold,
        ),
        bear_entry=_parse_int_csv(
            "BACKTEST_SWEEP_BEAR",
            _sweep_env_raw("BACKTEST_SWEEP_BEAR", "BACKTEST_SWEEP_BEAR_ENTRY_THRESHOLD"),
            settings.bear_entry_threshold,
        ),
        weak=_parse_int_csv(
            "BACKTEST_SWEEP_WEAK",
            _sweep_env_raw("BACKTEST_SWEEP_WEAK", "BACKTEST_SWEEP_WEAK_SCORE_THRESHOLD"),
            settings.weak_score_threshold,
        ),
        regime_ranging_add=_parse_float_csv(
            "BACKTEST_SWEEP_RANGE_ADD",
            _sweep_env_raw(
                "BACKTEST_SWEEP_RANGE_ADD",
                "BACKTEST_SWEEP_REGIME_RANGING_THRESHOLD_WEIGHT_ADD",
            ),
            settings.regime_ranging_threshold_weight_add,
        ),
        flip_min_hold=_parse_int_csv(
            "BACKTEST_SWEEP_FLIP_HOLD",
            _sweep_env_raw(
                "BACKTEST_SWEEP_FLIP_HOLD",
                "BACKTEST_SWEEP_FLIP_MIN_HOLD_TRADING_DAYS",
            ),
            settings.flip_min_hold_trading_days,
        ),
        flip_margin=_parse_float_csv(
            "BACKTEST_SWEEP_FLIP_MARGIN",
            _sweep_env_raw("BACKTEST_SWEEP_FLIP_MARGIN", "BACKTEST_SWEEP_FLIP_MARGIN_WEIGHT"),
            settings.flip_margin_weight,
        ),
    )


def run_parameter_sweep(
    candles: CandleData,
    *,
    anchor_date: str | None,
    blocked_dates: set[str],
    base_params: StrategyParams,
    bars: int,
    grid: SweepGrid,
) -> list[SweepResultRow]:
    rows: list[SweepResultRow] = []
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
        bt = run_backtest(
            candles,
            anchor_date=anchor_date,
            blocked_dates=blocked_dates,
            strategy_params=params,
            bars=bars,
        )
        bal = compute_balanced_score(
            bt.total_return_pct,
            bt.win_rate_pct,
            bt.max_drawdown_pct,
            bt.flips,
        )
        rows.append(
            SweepResultRow(
                rank=0,
                balanced_score=bal,
                bull_entry_threshold=bull,
                bear_entry_threshold=bear,
                weak_score_threshold=weak,
                regime_ranging_threshold_weight_add=rng_add,
                flip_min_hold_trading_days=f_hold,
                flip_margin_weight=f_margin,
                total_trades=bt.total_trades,
                win_rate_pct=bt.win_rate_pct,
                average_return_pct=bt.average_return_per_trade_pct,
                best_trade_pct=bt.best_trade_pct,
                worst_trade_pct=bt.worst_trade_pct,
                max_drawdown_pct=bt.max_drawdown_pct,
                average_hold_days=bt.average_hold_days,
                flip_count=bt.flips,
                cash_periods=bt.cash_no_trade_periods,
                equity_end=bt.equity_end,
                total_return_pct=bt.total_return_pct,
            )
        )

    rows.sort(key=lambda r: r.balanced_score, reverse=True)
    ranked: list[SweepResultRow] = []
    for idx, row in enumerate(rows, start=1):
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
                average_return_pct=row.average_return_pct,
                best_trade_pct=row.best_trade_pct,
                worst_trade_pct=row.worst_trade_pct,
                max_drawdown_pct=row.max_drawdown_pct,
                average_hold_days=row.average_hold_days,
                flip_count=row.flip_count,
                cash_periods=row.cash_periods,
                equity_end=row.equity_end,
                total_return_pct=row.total_return_pct,
            )
        )
    return ranked


TOP_SWEEP_CONSOLE_ROWS = 10


def format_sweep_report(rows: list[SweepResultRow], *, ticker: str, bars: int, combo_count: int) -> str:
    top = rows[:TOP_SWEEP_CONSOLE_ROWS]
    lines = [
        "--- Backtest parameter sweep (research only; QQQ directional proxy) ---",
        f"Ticker: {ticker} | Bars: {bars} | Combinations evaluated: {combo_count}",
        "",
        "Balanced score (higher is better; see backtest_sweep.py):",
        "  total_return_pct + (win_rate_pct * 0.25) - abs(max_drawdown_pct * 1.5) - (flip_count * 0.25)",
        "",
        f"Top {len(top)} by balanced_score (full ranking in CSV if --backtest-sweep-csv is set):",
        "",
        (
            "rk | score   | bull bear weak | rng_add | fh | fmrg | trades | win% | avgRet% | "
            "best% | worst% | maxDD% | hold | flips | cash | equity | totRet%"
        ),
        "-" * 125,
    ]
    for r in top:
        best_s = f"{r.best_trade_pct:.2f}" if r.best_trade_pct is not None else "  n/a"
        worst_s = f"{r.worst_trade_pct:.2f}" if r.worst_trade_pct is not None else "  n/a"
        lines.append(
            f"{r.rank:2d} | {r.balanced_score:7.2f} | {r.bull_entry_threshold:4d} {r.bear_entry_threshold:4d} "
            f"{r.weak_score_threshold:4d} | {r.regime_ranging_threshold_weight_add:7.2f} | "
            f"{r.flip_min_hold_trading_days:2d} | {r.flip_margin_weight:4.2f} | "
            f"{r.total_trades:6d} | {r.win_rate_pct:4.0f} | {r.average_return_pct:7.2f} | "
            f"{best_s:>7} | {worst_s:>7} | {r.max_drawdown_pct:6.2f} | {r.average_hold_days:4.1f} | "
            f"{r.flip_count:5d} | {r.cash_periods:4d} | {r.equity_end:.4f} | {r.total_return_pct:7.2f}%"
        )
    lines.extend(
        [
            "",
            "Disclaimer: Sweep mode is for research only. Results use a QQQ close proxy, not real ETF fills.",
            "Top-ranked settings are not guaranteed future performance.",
            "",
            "Notes: TQQQ ~ long QQQ, SQQQ ~ short QQQ; not broker execution or ETF-accurate PnL.",
        ]
    )
    return "\n".join(lines)


def _csv_num_optional(value: float | None) -> str:
    if value is None:
        return ""
    return str(round(value, 6))


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
        "average_return_pct",
        "best_trade_pct",
        "worst_trade_pct",
        "max_drawdown_pct",
        "average_hold_days",
        "flip_count",
        "cash_periods",
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
                    "average_return_pct": round(r.average_return_pct, 6),
                    "best_trade_pct": _csv_num_optional(r.best_trade_pct),
                    "worst_trade_pct": _csv_num_optional(r.worst_trade_pct),
                    "max_drawdown_pct": round(r.max_drawdown_pct, 6),
                    "average_hold_days": round(r.average_hold_days, 6),
                    "flip_count": r.flip_count,
                    "cash_periods": r.cash_periods,
                    "equity_end": round(r.equity_end, 6),
                    "total_return_pct": round(r.total_return_pct, 6),
                }
            )
