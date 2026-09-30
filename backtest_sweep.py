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

from strategy import DecideOptions

from backtest import BacktestResult, run_backtest

# Balanced ranking score (raw units, not min-max normalized). Tune coefficients here only.
# Formula (see compute_balanced_score):
#   total_return_pct
#   + win_rate * WIN_RATE_WEIGHT
#   + avg_return_per_trade * AVG_RETURN_WEIGHT
#   + median_return_per_trade * MEDIAN_RETURN_WEIGHT (skipped if no trades)
#   - abs(max_drawdown_pct) * DRAWDOWN_WEIGHT
#   - flip_count * FLIP_PENALTY
#   - under-trade penalty if total_trades < UNDER_TRADE_MIN
#   - over-trade penalty if total_trades > OVER_TRADE_MAX
_WIN_RATE_WEIGHT = 0.22
_DRAWDOWN_WEIGHT = 1.55
_FLIP_PENALTY = 0.42
_AVG_RETURN_WEIGHT = 0.18
_MEDIAN_RETURN_WEIGHT = 0.12
_UNDER_TRADE_MIN = 6
_UNDER_TRADE_PENALTY = 1.8
_OVER_TRADE_MAX = 42
_OVER_TRADE_PENALTY = 0.22


def compute_balanced_score(
    total_return_pct: float,
    win_rate_pct: float,
    max_drawdown_pct: float,
    flip_count: int,
    *,
    average_return_pct: float,
    median_return_pct: float | None,
    total_trades: int,
) -> float:
    """Higher is better; balances return quality, risk, flip churn, and trade count."""
    score = total_return_pct
    score += win_rate_pct * _WIN_RATE_WEIGHT
    score -= abs(max_drawdown_pct) * _DRAWDOWN_WEIGHT
    score -= flip_count * _FLIP_PENALTY
    score += average_return_pct * _AVG_RETURN_WEIGHT
    if median_return_pct is not None:
        score += median_return_pct * _MEDIAN_RETURN_WEIGHT
    if total_trades < _UNDER_TRADE_MIN:
        score -= (_UNDER_TRADE_MIN - total_trades) * _UNDER_TRADE_PENALTY
    elif total_trades > _OVER_TRADE_MAX:
        score -= (total_trades - _OVER_TRADE_MAX) * _OVER_TRADE_PENALTY
    return score


@dataclass(frozen=True)
class SweepGrid:
    bull_entry: tuple[int, ...]
    bear_entry: tuple[int, ...]
    weak: tuple[int, ...]
    regime_ranging_add: tuple[float, ...]
    flip_min_hold: tuple[int, ...]
    flip_margin: tuple[float, ...]
    entry_dominance_gap: tuple[float, ...]
    min_confidence: tuple[int, ...]
    stop_loss: tuple[float, ...] = (0.08,)
    take_profit: tuple[float, ...] = (0.15,)

    @property
    def combination_count(self) -> int:
        return (
            len(self.bull_entry)
            * len(self.bear_entry)
            * len(self.weak)
            * len(self.regime_ranging_add)
            * len(self.flip_min_hold)
            * len(self.flip_margin)
            * len(self.entry_dominance_gap)
            * len(self.min_confidence)
            * len(self.stop_loss)
            * len(self.take_profit)
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
    entry_dominance_gap_weight: float
    min_confidence_to_trade: int
    total_trades: int
    win_rate_pct: float
    average_return_pct: float
    median_return_pct: float | None
    best_trade_pct: float | None
    worst_trade_pct: float | None
    max_drawdown_pct: float
    average_hold_days: float
    flip_count: int
    cash_periods: int
    equity_end: float
    total_return_pct: float
    stop_loss_pct: float | None = None
    take_profit_pct: float | None = None


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
        entry_dominance_gap=_parse_float_csv(
            "BACKTEST_SWEEP_DOM_GAP",
            _sweep_env_raw(
                "BACKTEST_SWEEP_DOM_GAP",
                "BACKTEST_SWEEP_ENTRY_DOMINANCE_GAP_WEIGHT",
            ),
            settings.entry_dominance_gap_weight,
        ),
        min_confidence=_parse_int_csv(
            "BACKTEST_SWEEP_MIN_CONFIDENCE",
            _sweep_env_raw(
                "BACKTEST_SWEEP_MIN_CONFIDENCE",
                "BACKTEST_SWEEP_MIN_CONFIDENCE_TO_TRADE",
            ),
            settings.min_confidence_to_trade,
        ),
        stop_loss=_parse_float_csv(
            "BACKTEST_SWEEP_STOP",
            _sweep_env_raw("BACKTEST_SWEEP_STOP", "BACKTEST_SWEEP_STOP_LOSS_PCT"),
            settings.stop_loss_pct,
        ),
        take_profit=_parse_float_csv(
            "BACKTEST_SWEEP_TAKE_PROFIT",
            _sweep_env_raw("BACKTEST_SWEEP_TAKE_PROFIT", "BACKTEST_SWEEP_TAKE_PROFIT_PCT"),
            settings.take_profit_pct,
        ),
    )


def _pair_levels(current: float, other: float) -> tuple[float, ...]:
    levels = sorted({round(float(current), 6), round(float(other), 6)})
    if len(levels) == 1:
        levels.append(round(float(current) + abs(float(other) - float(current) or 0.01), 6))
    return tuple(levels)


def _pair_int_levels(current: int, other: int, *, low: int = 0, high: int = 100) -> tuple[int, ...]:
    levels = sorted({max(low, min(high, int(current))), max(low, min(high, int(other)))})
    if len(levels) == 1:
        bump = min(high, int(current) + 1)
        if bump == int(current):
            bump = max(low, int(current) - 1)
        levels = sorted({int(current), bump})
    return tuple(levels)


def small_research_grid(settings: Settings) -> SweepGrid:
    """Two levels on each guide knob. Other sweep axes stay at the live single value.

    2**6 = 64 combinations. This is the small grid, not the legacy full cartesian sweep.
    """
    return SweepGrid(
        bull_entry=(settings.bull_entry_threshold,),
        bear_entry=(settings.bear_entry_threshold,),
        weak=(settings.weak_score_threshold,),
        regime_ranging_add=_pair_levels(
            settings.regime_ranging_threshold_weight_add,
            max(0.0, settings.regime_ranging_threshold_weight_add - 0.15),
        ),
        flip_min_hold=_pair_int_levels(
            settings.flip_min_hold_trading_days,
            max(0, settings.flip_min_hold_trading_days - 1),
            low=0,
            high=30,
        ),
        flip_margin=(settings.flip_margin_weight,),
        entry_dominance_gap=_pair_levels(
            settings.entry_dominance_gap_weight,
            max(0.0, settings.entry_dominance_gap_weight - 0.25),
        ),
        min_confidence=_pair_int_levels(
            settings.min_confidence_to_trade,
            max(0, settings.min_confidence_to_trade - 8),
            low=0,
            high=100,
        ),
        stop_loss=_pair_levels(settings.stop_loss_pct, settings.stop_loss_pct + 0.02),
        take_profit=_pair_levels(settings.take_profit_pct, min(0.5, settings.take_profit_pct + 0.07)),
    )


def run_parameter_sweep(
    candles: CandleData,
    *,
    anchor_date: str | None,
    blocked_dates: set[str],
    base_params: StrategyParams,
    bars: int,
    grid: SweepGrid,
    debug_strategy: bool = False,
    decide_options: DecideOptions | None = None,
    entry_slippage_bps: float = 0.0,
    exit_slippage_bps: float = 0.0,
) -> list[SweepResultRow]:
    rows: list[SweepResultRow] = []
    for combo_idx, (bull, bear, weak, rng_add, f_hold, f_margin, dom_gap, min_cf, stop, take) in enumerate(
        itertools.product(
            grid.bull_entry,
            grid.bear_entry,
            grid.weak,
            grid.regime_ranging_add,
            grid.flip_min_hold,
            grid.flip_margin,
            grid.entry_dominance_gap,
            grid.min_confidence,
            grid.stop_loss,
            grid.take_profit,
        )
    ):
        params = replace(
            base_params,
            bull_entry_threshold=bull,
            bear_entry_threshold=bear,
            weak_threshold=weak,
            regime_ranging_threshold_weight_add=rng_add,
            flip_min_hold_trading_days=f_hold,
            flip_margin_weight=f_margin,
            entry_dominance_gap_weight=dom_gap,
            min_confidence_to_trade=min_cf,
            stop_loss_pct=stop,
            take_profit_pct=take,
        )
        if combo_idx == 0 or (combo_idx + 1) % 8 == 0:
            print(f"  sweep progress {combo_idx + 1}/{grid.combination_count}", flush=True)
        bt = run_backtest(
            candles,
            anchor_date=anchor_date,
            blocked_dates=blocked_dates,
            strategy_params=params,
            bars=bars,
            debug_strategy=bool(debug_strategy and combo_idx == 0),
            decide_options=decide_options,
            entry_slippage_bps=entry_slippage_bps,
            exit_slippage_bps=exit_slippage_bps,
        )
        bal = compute_balanced_score(
            bt.total_return_pct,
            bt.win_rate_pct,
            bt.max_drawdown_pct,
            bt.flips,
            average_return_pct=bt.average_return_per_trade_pct,
            median_return_pct=bt.median_return_per_trade_pct,
            total_trades=bt.total_trades,
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
                entry_dominance_gap_weight=dom_gap,
                min_confidence_to_trade=min_cf,
                total_trades=bt.total_trades,
                win_rate_pct=bt.win_rate_pct,
                average_return_pct=bt.average_return_per_trade_pct,
                median_return_pct=bt.median_return_per_trade_pct,
                best_trade_pct=bt.best_trade_pct,
                worst_trade_pct=bt.worst_trade_pct,
                max_drawdown_pct=bt.max_drawdown_pct,
                average_hold_days=bt.average_hold_days,
                flip_count=bt.flips,
                cash_periods=bt.cash_no_trade_periods,
                equity_end=bt.equity_end,
                total_return_pct=bt.total_return_pct,
                stop_loss_pct=stop,
                take_profit_pct=take,
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
                entry_dominance_gap_weight=row.entry_dominance_gap_weight,
                min_confidence_to_trade=row.min_confidence_to_trade,
                total_trades=row.total_trades,
                win_rate_pct=row.win_rate_pct,
                average_return_pct=row.average_return_pct,
                median_return_pct=row.median_return_pct,
                best_trade_pct=row.best_trade_pct,
                worst_trade_pct=row.worst_trade_pct,
                max_drawdown_pct=row.max_drawdown_pct,
                average_hold_days=row.average_hold_days,
                flip_count=row.flip_count,
                cash_periods=row.cash_periods,
                equity_end=row.equity_end,
                total_return_pct=row.total_return_pct,
                stop_loss_pct=row.stop_loss_pct,
                take_profit_pct=row.take_profit_pct,
            )
        )
    return ranked


TOP_SWEEP_CONSOLE_ROWS = 10

_INT_SWEEP_FIELDS = frozenset(
    {
        "bull_entry_threshold",
        "bear_entry_threshold",
        "weak_score_threshold",
        "flip_min_hold_trading_days",
        "min_confidence_to_trade",
    }
)
_SWEEP_PARAM_FIELDS = (
    "bull_entry_threshold",
    "bear_entry_threshold",
    "weak_score_threshold",
    "regime_ranging_threshold_weight_add",
    "flip_min_hold_trading_days",
    "flip_margin_weight",
    "entry_dominance_gap_weight",
    "min_confidence_to_trade",
    "stop_loss_pct",
    "take_profit_pct",
)


def _norm_param(field: str, value: object) -> int | float | None:
    if value is None:
        return None
    if field in _INT_SWEEP_FIELDS:
        return int(value)  # type: ignore[arg-type]
    return round(float(value), 6)  # type: ignore[arg-type]


def _param_key(row: SweepResultRow) -> tuple[object, ...]:
    return tuple(_norm_param(field, getattr(row, field)) for field in _SWEEP_PARAM_FIELDS)


@dataclass(frozen=True)
class NeighborShare:
    neighbor_count: int
    profitable_neighbors: int
    share: float | None
    best_total_return_pct: float | None
    neighbors: tuple[dict[str, object], ...]


def profitable_neighbor_share(rows: list[SweepResultRow]) -> NeighborShare:
    """Share of one-step neighbors of the best row that stay profitable.

    A neighbor matches the best row on every swept field except one, where the
    value is the next lower or higher level present in the sweep. Profitable
    means total_return_pct > 0 (QQQ proxy, not broker P&L).
    """
    if not rows:
        return NeighborShare(0, 0, None, None, ())
    best = min(rows, key=lambda r: (r.rank if r.rank else 10**9, -r.balanced_score))
    index = {_param_key(row): row for row in rows}
    best_key = _param_key(best)
    neighbors: list[dict[str, object]] = []
    for axis, field in enumerate(_SWEEP_PARAM_FIELDS):
        values = sorted(
            {key[axis] for key in index if key[axis] is not None},
            key=lambda v: (isinstance(v, float), v),
        )
        # ints and floats are both comparable within one axis.
        try:
            values = sorted(set(values))
        except TypeError:
            continue
        current = best_key[axis]
        if current is None or current not in values:
            continue
        pos = values.index(current)
        adjacent_positions = [p for p in (pos - 1, pos + 1) if 0 <= p < len(values)]
        for adj_pos in adjacent_positions:
            key = list(best_key)
            key[axis] = values[adj_pos]
            neighbor = index.get(tuple(key))
            if neighbor is None or neighbor is best:
                continue
            profitable = neighbor.total_return_pct > 0
            neighbors.append(
                {
                    "field": field,
                    "value": values[adj_pos],
                    "total_return_pct": neighbor.total_return_pct,
                    "total_trades": neighbor.total_trades,
                    "profitable": profitable,
                    "balanced_score": neighbor.balanced_score,
                    "rank": neighbor.rank,
                }
            )
    count = len(neighbors)
    profitable_n = sum(1 for n in neighbors if n["profitable"])
    share = (profitable_n / count) if count else None
    return NeighborShare(
        neighbor_count=count,
        profitable_neighbors=profitable_n,
        share=share,
        best_total_return_pct=best.total_return_pct,
        neighbors=tuple(neighbors),
    )


def format_sweep_report(rows: list[SweepResultRow], *, ticker: str, bars: int, combo_count: int) -> str:
    top = rows[:TOP_SWEEP_CONSOLE_ROWS]
    lines = [
        "--- Backtest parameter sweep (research only; QQQ directional proxy) ---",
        f"Ticker: {ticker} | Bars: {bars} | Combinations evaluated: {combo_count}",
        "",
        "Balanced score (higher is better; see backtest_sweep.compute_balanced_score):",
        "  total_return_pct",
        "  + win_rate_pct * 0.22 + avg_return_pct * 0.18 + median_return_pct * 0.12 (if trades)",
        "  - abs(max_drawdown_pct) * 1.55 - flip_count * 0.42",
        "  - under-trade penalty if trades < 6 | over-trade penalty if trades > 42",
        "",
        f"Top {len(top)} by balanced_score (full ranking in CSV if --backtest-sweep-csv is set):",
        "",
        (
            "rk | score   | bull bear weak | rng_add | fh | fmrg | dom | mc | medRt | trades | win% | "
            "avgRet | maxDD | flips | totRet%"
        ),
        "-" * 118,
    ]
    for r in top:
        med_s = f"{r.median_return_pct:5.2f}" if r.median_return_pct is not None else "  n/a"
        lines.append(
            f"{r.rank:2d} | {r.balanced_score:7.2f} | {r.bull_entry_threshold:4d} {r.bear_entry_threshold:4d} "
            f"{r.weak_score_threshold:4d} | {r.regime_ranging_threshold_weight_add:7.2f} | "
            f"{r.flip_min_hold_trading_days:2d} | {r.flip_margin_weight:4.2f} | {r.entry_dominance_gap_weight:4.2f} | "
            f"{r.min_confidence_to_trade:2d} | {med_s} | "
            f"{r.total_trades:6d} | {r.win_rate_pct:4.0f} | {r.average_return_pct:6.2f} | "
            f"{r.max_drawdown_pct:6.2f} | {r.flip_count:5d} | {r.total_return_pct:7.2f}%"
        )
    share = profitable_neighbor_share(rows)
    if share.share is None:
        neighbor_line = "Profitable neighbors around best row: n/a (no adjacent parameter sets)."
    else:
        neighbor_line = (
            "Profitable neighbors around best row: "
            f"{share.share * 100:.1f}% ({share.profitable_neighbors}/{share.neighbor_count} "
            "with total_return_pct > 0)."
        )
    lines.extend(
        [
            "",
            neighbor_line,
            "Plateau check: a single profitable spike with few profitable neighbors is not a robust hill.",
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
        "entry_dominance_gap_weight",
        "min_confidence_to_trade",
        "total_trades",
        "win_rate_pct",
        "average_return_pct",
        "median_return_pct",
        "best_trade_pct",
        "worst_trade_pct",
        "max_drawdown_pct",
        "average_hold_days",
        "flip_count",
        "cash_periods",
        "equity_end",
        "total_return_pct",
        "stop_loss_pct",
        "take_profit_pct",
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
                    "entry_dominance_gap_weight": round(r.entry_dominance_gap_weight, 6),
                    "min_confidence_to_trade": r.min_confidence_to_trade,
                    "total_trades": r.total_trades,
                    "win_rate_pct": round(r.win_rate_pct, 4),
                    "average_return_pct": round(r.average_return_pct, 6),
                    "median_return_pct": _csv_num_optional(r.median_return_pct),
                    "best_trade_pct": _csv_num_optional(r.best_trade_pct),
                    "worst_trade_pct": _csv_num_optional(r.worst_trade_pct),
                    "max_drawdown_pct": round(r.max_drawdown_pct, 6),
                    "average_hold_days": round(r.average_hold_days, 6),
                    "flip_count": r.flip_count,
                    "cash_periods": r.cash_periods,
                    "equity_end": round(r.equity_end, 6),
                    "total_return_pct": round(r.total_return_pct, 6),
                    "stop_loss_pct": _csv_num_optional(r.stop_loss_pct),
                    "take_profit_pct": _csv_num_optional(r.take_profit_pct),
                }
            )
