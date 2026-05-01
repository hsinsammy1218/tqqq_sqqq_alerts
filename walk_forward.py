"""Walk-forward grid selection over strategy knobs (research only; QQQ proxy)."""

from __future__ import annotations

import csv
import itertools
import os
import statistics
from dataclasses import dataclass, replace
from pathlib import Path

from backtest import BacktestResult, run_backtest
from backtest_sweep import compute_balanced_score
from config import ConfigError, Settings
from data import CandleData
from strategy import DecideOptions
from strategy_params import StrategyParams


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


def _parse_bool_csv(env_name: str, raw: str | None, default: bool) -> tuple[bool, ...]:
    if raw is None or not str(raw).strip():
        return (default,)
    parts = [p.strip().lower() for p in str(raw).split(",") if p.strip()]
    if not parts:
        return (default,)
    out: list[bool] = []
    for p in parts:
        if p in {"1", "true", "yes", "y", "on"}:
            out.append(True)
        elif p in {"0", "false", "no", "n", "off"}:
            out.append(False)
        else:
            raise ConfigError(
                f"{env_name} values must be true/false (comma-separated); invalid token {p!r}."
            )
    return tuple(out)


def walk_forward_fold_count() -> int:
    raw = os.getenv("WALK_FORWARD_FOLDS", "3").strip()
    try:
        n = int(raw)
    except ValueError as exc:
        raise ConfigError("WALK_FORWARD_FOLDS must be a positive integer.") from exc
    if n < 1:
        raise ConfigError("WALK_FORWARD_FOLDS must be >= 1.")
    return n


@dataclass(frozen=True)
class WalkForwardGrid:
    min_confidence: tuple[int, ...]
    entry_dominance_gap: tuple[float, ...]
    regime_ranging_add: tuple[float, ...]
    flip_min_hold: tuple[int, ...]
    flip_margin: tuple[float, ...]
    flip_allow_in_range: tuple[bool, ...]

    @property
    def combination_count(self) -> int:
        return (
            len(self.min_confidence)
            * len(self.entry_dominance_gap)
            * len(self.regime_ranging_add)
            * len(self.flip_min_hold)
            * len(self.flip_margin)
            * len(self.flip_allow_in_range)
        )


def walk_forward_grid_from_settings(settings: Settings) -> WalkForwardGrid:
    """Grid lists from WALK_FORWARD_GRID_* env; omit or blank → single value from Settings."""
    return WalkForwardGrid(
        min_confidence=_parse_int_csv(
            "WALK_FORWARD_GRID_MIN_CONFIDENCE",
            os.getenv("WALK_FORWARD_GRID_MIN_CONFIDENCE"),
            settings.min_confidence_to_trade,
        ),
        entry_dominance_gap=_parse_float_csv(
            "WALK_FORWARD_GRID_DOM_GAP",
            os.getenv("WALK_FORWARD_GRID_DOM_GAP"),
            settings.entry_dominance_gap_weight,
        ),
        regime_ranging_add=_parse_float_csv(
            "WALK_FORWARD_GRID_RANGE_ADD",
            os.getenv("WALK_FORWARD_GRID_RANGE_ADD"),
            settings.regime_ranging_threshold_weight_add,
        ),
        flip_min_hold=_parse_int_csv(
            "WALK_FORWARD_GRID_FLIP_HOLD",
            os.getenv("WALK_FORWARD_GRID_FLIP_HOLD"),
            settings.flip_min_hold_trading_days,
        ),
        flip_margin=_parse_float_csv(
            "WALK_FORWARD_GRID_FLIP_MARGIN",
            os.getenv("WALK_FORWARD_GRID_FLIP_MARGIN"),
            settings.flip_margin_weight,
        ),
        flip_allow_in_range=_parse_bool_csv(
            "WALK_FORWARD_GRID_FLIP_ALLOW_IN_RANGE",
            os.getenv("WALK_FORWARD_GRID_FLIP_ALLOW_IN_RANGE"),
            settings.flip_in_range_regime,
        ),
    )


def _validation_folds(region_lo: int, region_hi: int, n_folds: int, *, min_fold_bars: int = 20) -> list[tuple[int, int]]:
    span = region_hi - region_lo
    if span < n_folds * min_fold_bars:
        raise ValueError(
            f"Walk-forward span ({span} bars) is too short for {n_folds} validation fold(s) "
            f"of at least {min_fold_bars} bars each; increase --backtest-bars or lower "
            "WALK_FORWARD_FOLDS."
        )
    folds: list[tuple[int, int]] = []
    for k in range(n_folds):
        lo = region_lo + (span * k) // n_folds
        hi = region_lo + (span * (k + 1)) // n_folds
        if hi - lo < min_fold_bars:
            raise ValueError(
                "Walk-forward fold split produced a segment shorter than "
                f"{min_fold_bars} bars; adjust --backtest-bars or WALK_FORWARD_FOLDS."
            )
        folds.append((lo, hi))
    return folds


def _aggregate_fold_trade_stats(results: list[BacktestResult]) -> tuple[int, float, float | None, float]:
    trade_count = sum(r.total_trades for r in results)
    wins = sum(r.winning_trades for r in results)
    closed = sum(r.closed_trades for r in results)
    win_rate = (wins / closed * 100.0) if closed else 0.0
    all_returns: list[float] = []
    for r in results:
        for tr in r.trade_rows:
            all_returns.append(tr.return_pct)
    median_ret = float(statistics.median(all_returns)) if all_returns else None
    max_dd = max((r.max_drawdown_pct for r in results), default=0.0)
    return trade_count, win_rate, median_ret, max_dd


@dataclass(frozen=True)
class WalkForwardResultRow:
    rank: int
    validation_score: float
    min_confidence_to_trade: int
    entry_dominance_gap_weight: float
    regime_ranging_threshold_weight_add: float
    flip_min_hold_trading_days: int
    flip_margin_weight: float
    flip_allow_in_range: bool
    trade_count: int
    win_rate_pct: float
    median_return_per_trade_pct: float | None
    max_drawdown_pct: float
    validation_windows_tested: int


def run_walk_forward(
    candles: CandleData,
    *,
    anchor_date: str | None,
    blocked_dates: set[str],
    base_params: StrategyParams,
    bars: int,
    grid: WalkForwardGrid,
    n_folds: int,
    debug_strategy: bool = False,
    decide_options: DecideOptions | None = None,
) -> list[WalkForwardResultRow]:
    daily = candles.daily
    n = len(daily)
    region_lo = max(60, n - bars)
    region_hi = n
    folds = _validation_folds(region_lo, region_hi, n_folds)

    rows: list[WalkForwardResultRow] = []
    for combo_idx, (mc, dom, rng_add, fh, fm, allow) in enumerate(
        itertools.product(
            grid.min_confidence,
            grid.entry_dominance_gap,
            grid.regime_ranging_add,
            grid.flip_min_hold,
            grid.flip_margin,
            grid.flip_allow_in_range,
        )
    ):
        params = replace(
            base_params,
            min_confidence_to_trade=mc,
            entry_dominance_gap_weight=dom,
            regime_ranging_threshold_weight_add=rng_add,
            flip_min_hold_trading_days=fh,
            flip_margin_weight=fm,
            flip_in_range_regime=allow,
        )
        fold_scores: list[float] = []
        fold_results: list[BacktestResult] = []
        for fold_idx, (lo, hi) in enumerate(folds):
            bt = run_backtest(
                candles,
                anchor_date=anchor_date,
                blocked_dates=blocked_dates,
                strategy_params=params,
                bars=bars,
                debug_strategy=bool(debug_strategy and combo_idx == 0 and fold_idx == 0),
                decide_options=decide_options,
                loop_start_idx=lo,
                loop_end_idx_exclusive=hi,
            )
            fold_results.append(bt)
            fold_scores.append(
                compute_balanced_score(
                    bt.total_return_pct,
                    bt.win_rate_pct,
                    bt.max_drawdown_pct,
                    bt.flips,
                    average_return_pct=bt.average_return_per_trade_pct,
                    median_return_pct=bt.median_return_per_trade_pct,
                    total_trades=bt.total_trades,
                )
            )
        validation_score = float(statistics.mean(fold_scores)) if fold_scores else 0.0
        tc, wr, med, mdd = _aggregate_fold_trade_stats(fold_results)
        rows.append(
            WalkForwardResultRow(
                rank=0,
                validation_score=validation_score,
                min_confidence_to_trade=mc,
                entry_dominance_gap_weight=dom,
                regime_ranging_threshold_weight_add=rng_add,
                flip_min_hold_trading_days=fh,
                flip_margin_weight=fm,
                flip_allow_in_range=allow,
                trade_count=tc,
                win_rate_pct=wr,
                median_return_per_trade_pct=med,
                max_drawdown_pct=mdd,
                validation_windows_tested=len(folds),
            )
        )

    rows.sort(key=lambda r: r.validation_score, reverse=True)
    ranked: list[WalkForwardResultRow] = []
    for idx, row in enumerate(rows, start=1):
        ranked.append(
            WalkForwardResultRow(
                rank=idx,
                validation_score=row.validation_score,
                min_confidence_to_trade=row.min_confidence_to_trade,
                entry_dominance_gap_weight=row.entry_dominance_gap_weight,
                regime_ranging_threshold_weight_add=row.regime_ranging_threshold_weight_add,
                flip_min_hold_trading_days=row.flip_min_hold_trading_days,
                flip_margin_weight=row.flip_margin_weight,
                flip_allow_in_range=row.flip_allow_in_range,
                trade_count=row.trade_count,
                win_rate_pct=row.win_rate_pct,
                median_return_per_trade_pct=row.median_return_per_trade_pct,
                max_drawdown_pct=row.max_drawdown_pct,
                validation_windows_tested=row.validation_windows_tested,
            )
        )
    return ranked


def format_walk_forward_report(
    rows: list[WalkForwardResultRow],
    *,
    ticker: str,
    bars: int,
    combo_count: int,
    n_folds: int,
) -> str:
    if not rows:
        return "--- Walk-forward parameter selection ---\n(no combinations evaluated)"
    top = rows[0]
    med_s = (
        f"{top.median_return_per_trade_pct:.2f}%"
        if top.median_return_per_trade_pct is not None
        else "N/A"
    )
    lines = [
        "--- Walk-forward parameter selection (research only; QQQ directional proxy) ---",
        f"Ticker: {ticker} | Bars: {bars} | Grid combinations: {combo_count} | "
        f"Validation folds: {n_folds}",
        "",
        "Objective: mean balanced score across chronological validation windows "
        "(same balanced_score as --backtest-sweep; see backtest_sweep.compute_balanced_score).",
        "",
        "Recommended settings (rank 1 by mean validation score):",
        f"  MIN_CONFIDENCE_TO_TRADE={top.min_confidence_to_trade}",
        f"  ENTRY_DOMINANCE_GAP_WEIGHT={top.entry_dominance_gap_weight:g}",
        f"  REGIME_RANGING_THRESHOLD_WEIGHT_ADD={top.regime_ranging_threshold_weight_add:g}",
        f"  FLIP_MIN_HOLD_TRADING_DAYS={top.flip_min_hold_trading_days}",
        f"  FLIP_MARGIN_WEIGHT={top.flip_margin_weight:g}",
        f"  FLIP_ALLOW_IN_RANGE={'true' if top.flip_allow_in_range else 'false'}",
        "",
        "Aggregated validation metrics (all folds pooled where noted):",
        f"  Validation score (mean): {top.validation_score:.4f}",
        f"  Trade count (closed round-trips, sum over folds): {top.trade_count}",
        f"  Win rate (pooled): {top.win_rate_pct:.1f}%",
        f"  Median return/trade (pooled): {med_s}",
        f"  Max drawdown (worst fold): {top.max_drawdown_pct:.2f}%",
        f"  Validation windows tested: {top.validation_windows_tested}",
        "",
        "These recommendations do not change live defaults until you copy them into `.env`.",
        "Disclaimer: walk-forward still uses the QQQ proxy, not ETF fills or broker execution.",
    ]
    return "\n".join(lines)


def export_walk_forward_csv(path: str, rows: list[WalkForwardResultRow]) -> None:
    fields = [
        "rank",
        "validation_score",
        "min_confidence_to_trade",
        "entry_dominance_gap_weight",
        "regime_ranging_threshold_weight_add",
        "flip_min_hold_trading_days",
        "flip_margin_weight",
        "flip_allow_in_range",
        "trade_count",
        "win_rate_pct",
        "median_return_per_trade_pct",
        "max_drawdown_pct",
        "validation_windows_tested",
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
                    "validation_score": round(r.validation_score, 6),
                    "min_confidence_to_trade": r.min_confidence_to_trade,
                    "entry_dominance_gap_weight": round(r.entry_dominance_gap_weight, 6),
                    "regime_ranging_threshold_weight_add": round(
                        r.regime_ranging_threshold_weight_add, 6
                    ),
                    "flip_min_hold_trading_days": r.flip_min_hold_trading_days,
                    "flip_margin_weight": round(r.flip_margin_weight, 6),
                    "flip_allow_in_range": int(r.flip_allow_in_range),
                    "trade_count": r.trade_count,
                    "win_rate_pct": round(r.win_rate_pct, 4),
                    "median_return_per_trade_pct": (
                        round(r.median_return_per_trade_pct, 6)
                        if r.median_return_per_trade_pct is not None
                        else ""
                    ),
                    "max_drawdown_pct": round(r.max_drawdown_pct, 6),
                    "validation_windows_tested": r.validation_windows_tested,
                }
            )
