"""Research metrics for the QQQ proxy backtest (not broker P&L).

Scores closed-trade returns already produced by ``backtest.py``. Stretch
take-profit is never treated as an exit. Walk-forward efficiency is reported
only when an in-sample and out-of-sample window were both run.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

from backtest import BacktestResult, BacktestTradeRow, run_backtest
from backtest_sweep import NeighborShare, SweepResultRow, profitable_neighbor_share
from data import CandleData
from strategy_params import DEFAULT_SCORE_WEIGHTS, StrategyParams
from strategy import DecideOptions

MIN_TRADES = 30
REWARD_RISK_BAR = 3.0
DRAWDOWN_RATIO_BAR = 3.0
WFE_ROBUST_LOW = 0.50
WFE_ROBUST_HIGH = 0.60
TRADING_DAYS_PER_YEAR = 252.0
# Wider fixed target used only in the exit comparison. Not STRETCH_TAKE_PROFIT_PCT.
DEFAULT_WIDER_TAKE_PROFIT_PCT = 0.30
EVAL_TARGET_BARS = 756  # about three years of sessions


def wider_take_profit_pct(current: float, stretch: float) -> float:
    """A wider fixed target than the live TP. Never silently adopts the stretch level."""
    if current < DEFAULT_WIDER_TAKE_PROFIT_PCT - 1e-9:
        candidate = DEFAULT_WIDER_TAKE_PROFIT_PCT
    else:
        candidate = round(float(current) + 0.10, 4)
    if abs(candidate - float(stretch)) < 1e-9:
        candidate = round(candidate + 0.05, 4)
    return candidate


def equity_curve_from_returns(returns: Sequence[float]) -> list[float]:
    """Compound closed-trade returns the same way ``run_backtest`` does."""
    equity = 1.0
    points = [equity]
    for ret_pct in returns:
        equity *= 1.0 + (float(ret_pct) / 100.0)
        points.append(equity)
    return points


def drawdown_series_pct(equity_points: Sequence[float]) -> list[float]:
    if not equity_points:
        return []
    peak = equity_points[0]
    series: list[float] = []
    for eq in equity_points:
        if eq > peak:
            peak = eq
        series.append((peak - eq) / peak * 100.0 if peak > 0 else 0.0)
    return series


def annualized_pnl_pct(total_return_pct: float, bars: int) -> float | None:
    """Compounded annualized return in percent. ``bars`` are trading sessions."""
    if bars <= 0:
        return None
    equity = 1.0 + float(total_return_pct) / 100.0
    years = bars / TRADING_DAYS_PER_YEAR
    if years <= 0:
        return None
    if equity <= 0:
        return -100.0
    return (equity ** (1.0 / years) - 1.0) * 100.0


def walk_forward_efficiency(annualized_oos_pct: float | None, annualized_is_pct: float | None) -> float | None:
    """OOS annualized P&L / IS annualized P&L. None when IS is missing or not positive."""
    if annualized_oos_pct is None or annualized_is_pct is None:
        return None
    if annualized_is_pct <= 0:
        return None
    return float(annualized_oos_pct) / float(annualized_is_pct)


def prom_return_pct(returns: Sequence[float]) -> dict[str, float | None]:
    """PROM-style pessimistic return.

    adjusted wins = n_wins - sqrt(n_wins)
    adjusted losses = n_losses + sqrt(n_losses)
    applied to average win and average loss (loss averages stay signed).
    Wins match the backtest: return_pct >= 0.
    """
    wins = [float(r) for r in returns if r >= 0]
    losses = [float(r) for r in returns if r < 0]
    n_wins = len(wins)
    n_losses = len(losses)
    avg_win = (sum(wins) / n_wins) if n_wins else None
    avg_loss = (sum(losses) / n_losses) if n_losses else None
    adjusted_wins = n_wins - math.sqrt(n_wins)
    adjusted_losses = n_losses + math.sqrt(n_losses)
    prom = (adjusted_wins * (avg_win or 0.0)) + (adjusted_losses * (avg_loss or 0.0))
    return {
        "n_wins": float(n_wins),
        "n_losses": float(n_losses),
        "adjusted_wins": adjusted_wins,
        "adjusted_losses": adjusted_losses,
        "average_win_pct": avg_win,
        "average_loss_pct": avg_loss,
        "prom_return_pct": prom,
    }


def metrics_from_returns(returns: Sequence[float]) -> dict[str, object]:
    """Score a sequence of closed-trade percent returns from the backtest proxy."""
    rets = [float(r) for r in returns]
    trade_count = len(rets)
    gross_profit = sum(r for r in rets if r > 0)
    gross_loss = sum(-r for r in rets if r < 0)
    profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else None
    curve = equity_curve_from_returns(rets)
    net_profit_pct = (curve[-1] - 1.0) * 100.0 if curve else 0.0
    net_profit_sum_pct = sum(rets)
    drawdowns = drawdown_series_pct(curve)
    max_dd = max(drawdowns) if drawdowns else 0.0
    positive_dds = [d for d in drawdowns if d > 0]
    avg_dd = (sum(positive_dds) / len(positive_dds)) if positive_dds else 0.0
    dd_ratio = (max_dd / avg_dd) if avg_dd > 0 else None
    reward_risk = (net_profit_pct / max_dd) if max_dd > 0 else None
    prom = prom_return_pct(rets)

    flags: list[str] = []
    if trade_count < MIN_TRADES:
        flags.append("trade_count_under_30")
    if dd_ratio is None:
        if max_dd > 0:
            flags.append("max_drawdown_vs_average_undefined")
    elif dd_ratio >= DRAWDOWN_RATIO_BAR:
        flags.append("max_drawdown_ge_3x_average")
    if reward_risk is None or reward_risk < REWARD_RISK_BAR:
        flags.append("reward_risk_below_3")

    return {
        "trade_count": trade_count,
        "profit_factor": profit_factor,
        "net_profit_pct": net_profit_pct,
        "net_profit_sum_pct": net_profit_sum_pct,
        "max_drawdown_pct": max_dd,
        "average_drawdown_pct": avg_dd,
        "max_dd_to_average_dd": dd_ratio,
        "reward_risk": reward_risk,
        "reward_risk_bar": REWARD_RISK_BAR,
        "prom": {
            "n_wins": int(prom["n_wins"] or 0),
            "n_losses": int(prom["n_losses"] or 0),
            "adjusted_wins": prom["adjusted_wins"],
            "adjusted_losses": prom["adjusted_losses"],
            "average_win_pct": prom["average_win_pct"],
            "average_loss_pct": prom["average_loss_pct"],
            "prom_return_pct": prom["prom_return_pct"],
        },
        "flags": flags,
        "pnl_source": "backtest_closed_trade_returns_qqq_proxy",
    }


def metrics_from_trades(trades: Sequence[BacktestTradeRow]) -> dict[str, object]:
    return metrics_from_returns([t.return_pct for t in trades])


def metrics_from_backtest(result: BacktestResult) -> dict[str, object]:
    payload = metrics_from_trades(result.trade_rows)
    payload["bars_tested"] = result.bars_tested
    payload["window_start"] = result.start_utc
    payload["window_end"] = result.end_utc
    payload["backtest_total_return_pct"] = result.total_return_pct
    return payload


def is_oos_split(
    n_daily: int,
    bars: int,
    *,
    n_segments: int = 3,
    min_bars: int = 20,
) -> tuple[tuple[int, int], tuple[int, int], list[tuple[int, int]]] | None:
    """Equal chronological segments: earlier segments = IS, last segment = OOS.

    The guide asks for equal windows so P&L is not all from one short stretch.
    Default 3 equal thirds: in-sample = first two, out-of-sample = last.
    """
    if n_segments < 2:
        raise ValueError("n_segments must be >= 2")
    region_lo = max(60, n_daily - bars)
    region_hi = n_daily
    span = region_hi - region_lo
    if span < n_segments * min_bars:
        return None
    segments: list[tuple[int, int]] = []
    for k in range(n_segments):
        lo = region_lo + (span * k) // n_segments
        hi = region_lo + (span * (k + 1)) // n_segments
        if hi - lo < min_bars:
            return None
        segments.append((lo, hi))
    is_lo = segments[0][0]
    is_hi = segments[-2][1]
    oos_lo, oos_hi = segments[-1]
    return (is_lo, is_hi), (oos_lo, oos_hi), segments


def required_paper_capital(
    max_drawdown_pct: float,
    *,
    safety_factor: float = 2.0,
    reference_equity: float = 100_000.0,
) -> dict[str, float]:
    """Guide capital check: max drawdown × safety factor (1.5–3), not the whole account."""
    dd = max(0.0, float(max_drawdown_pct)) / 100.0
    factor = max(1.0, float(safety_factor))
    fraction = dd * factor
    return {
        "max_drawdown_pct": float(max_drawdown_pct),
        "safety_factor": factor,
        "required_capital_fraction_of_equity": fraction,
        "required_capital_usd_at_reference": float(reference_equity) * fraction,
        "reference_equity_usd": float(reference_equity),
    }


def walk_forward_efficiency_from_results(
    in_sample: BacktestResult,
    out_of_sample: BacktestResult,
    *,
    method: str = "equal_segments_last_is_oos",
    segment_returns: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    ann_is = annualized_pnl_pct(in_sample.total_return_pct, in_sample.bars_tested)
    ann_oos = annualized_pnl_pct(out_of_sample.total_return_pct, out_of_sample.bars_tested)
    wfe = walk_forward_efficiency(ann_oos, ann_is)
    passes = wfe is not None and wfe >= WFE_ROBUST_LOW
    return {
        "available": True,
        "method": method,
        "in_sample_bars": in_sample.bars_tested,
        "out_of_sample_bars": out_of_sample.bars_tested,
        "in_sample_net_profit_pct": in_sample.total_return_pct,
        "out_of_sample_net_profit_pct": out_of_sample.total_return_pct,
        "annualized_is_pnl_pct": ann_is,
        "annualized_oos_pnl_pct": ann_oos,
        "walk_forward_efficiency": wfe,
        "robust_bar": [WFE_ROBUST_LOW, WFE_ROBUST_HIGH],
        "passes_robust_bar": passes,
        "equal_segments": segment_returns or [],
        "note": (
            "Efficiency is annualized OOS P&L divided by annualized IS P&L on the QQQ proxy "
            "(with research slippage when configured). Equal segments keep profit from being "
            "judged on a single stretch. About 50–60% is the robustness bar; below 50% fails. "
            "Undefined when in-sample annualized P&L is not positive."
        ),
    }


def neighbor_share_payload(share: NeighborShare) -> dict[str, object]:
    return {
        "neighbor_count": share.neighbor_count,
        "profitable_neighbors": share.profitable_neighbors,
        "profitable_neighbor_share": share.share,
        "best_total_return_pct": share.best_total_return_pct,
        "neighbors": list(share.neighbors),
        "note": "Profitable means QQQ-proxy total_return_pct > 0. Not broker P&L.",
    }


def _variant_record(result: BacktestResult, *, label: str, exit_mode: str, take_profit_pct: float) -> dict[str, object]:
    metrics = metrics_from_backtest(result)
    return {
        "label": label,
        "exit_mode": exit_mode,
        "take_profit_pct": take_profit_pct,
        "stretch_take_profit_is_exit": False,
        "metrics": metrics,
    }


def resolve_research_bars(requested: int, daily_len: int, *, expand_default: bool) -> int:
    """Use ~3 years when research leaves --backtest-bars at its 180 default."""
    available = max(0, daily_len - 60)
    target = requested
    if expand_default and requested == 180:
        target = EVAL_TARGET_BARS
    if available <= 0:
        return target
    return max(20, min(target, available))


def run_strategy_evaluation(
    candles: CandleData,
    *,
    anchor_date: str | None,
    blocked_dates: set[str],
    strategy_params: StrategyParams,
    bars: int,
    decide_options: DecideOptions | None = None,
    plateau_rows: list[SweepResultRow] | None = None,
    data_window: dict[str, object] | None = None,
    entry_slippage_bps: float = 10.0,
    exit_slippage_bps: float = 10.0,
    wfe_segments: int = 3,
    capital_safety_factor: float = 2.0,
) -> dict[str, object]:
    """Baseline metrics, exit comparison, and walk-forward efficiency on one history."""
    common = dict(
        anchor_date=anchor_date,
        blocked_dates=blocked_dates,
        bars=bars,
        decide_options=decide_options,
        entry_slippage_bps=entry_slippage_bps,
        exit_slippage_bps=exit_slippage_bps,
    )
    baseline_params = replace(strategy_params, exit_mode="fixed")
    baseline = run_backtest(candles, strategy_params=baseline_params, **common)
    wider_tp = wider_take_profit_pct(
        strategy_params.take_profit_pct,
        strategy_params.stretch_take_profit_pct,
    )
    wider_params = replace(strategy_params, exit_mode="fixed", take_profit_pct=wider_tp)
    wider = run_backtest(candles, strategy_params=wider_params, **common)
    atr_params = replace(strategy_params, exit_mode="atr_trail")
    atr = run_backtest(candles, strategy_params=atr_params, **common)

    wfe_block: dict[str, object]
    split = is_oos_split(len(candles.daily), bars, n_segments=wfe_segments)
    if split is None:
        wfe_block = {
            "available": False,
            "walk_forward_efficiency": None,
            "passes_robust_bar": False,
            "robust_bar": [WFE_ROBUST_LOW, WFE_ROBUST_HIGH],
            "note": (
                f"Window is too short to split into {wfe_segments} equal segments "
                "of at least 20 bars for in-sample / out-of-sample."
            ),
        }
    else:
        (is_lo, is_hi), (oos_lo, oos_hi), segments = split
        in_sample = run_backtest(
            candles,
            strategy_params=baseline_params,
            loop_start_idx=is_lo,
            loop_end_idx_exclusive=is_hi,
            **common,
        )
        out_of_sample = run_backtest(
            candles,
            strategy_params=baseline_params,
            loop_start_idx=oos_lo,
            loop_end_idx_exclusive=oos_hi,
            **common,
        )
        segment_returns: list[dict[str, object]] = []
        for idx, (lo, hi) in enumerate(segments):
            seg = run_backtest(
                candles,
                strategy_params=baseline_params,
                loop_start_idx=lo,
                loop_end_idx_exclusive=hi,
                **common,
            )
            segment_returns.append(
                {
                    "segment": idx + 1,
                    "bars": seg.bars_tested,
                    "start": seg.start_utc,
                    "end": seg.end_utc,
                    "total_return_pct": seg.total_return_pct,
                    "total_trades": seg.total_trades,
                    "role": "oos" if idx == len(segments) - 1 else "is",
                }
            )
        wfe_block = walk_forward_efficiency_from_results(
            in_sample,
            out_of_sample,
            method=f"equal_{wfe_segments}_segments_last_oos",
            segment_returns=segment_returns,
        )

    if plateau_rows:
        plateau: dict[str, object] | None = neighbor_share_payload(profitable_neighbor_share(plateau_rows))
    else:
        plateau = None

    baseline_metrics = metrics_from_backtest(baseline)
    flags = list(baseline_metrics["flags"])  # type: ignore[arg-type]
    wfe_value = wfe_block.get("walk_forward_efficiency")
    if wfe_block.get("available") and (wfe_value is None or float(wfe_value) < WFE_ROBUST_LOW):
        flags.append("walk_forward_efficiency_below_50")
    if plateau is not None:
        share = plateau.get("profitable_neighbor_share")
        if share is None or float(share) < 0.5:
            flags.append("plateau_share_below_50")
    elif plateau is None:
        flags.append("plateau_not_run")

    capital = required_paper_capital(
        float(baseline_metrics["max_drawdown_pct"]),
        safety_factor=capital_safety_factor,
    )
    gate_pass = (
        int(baseline_metrics["trade_count"]) >= MIN_TRADES
        and "reward_risk_below_3" not in flags
        and "max_drawdown_ge_3x_average" not in flags
        and "walk_forward_efficiency_below_50" not in flags
        and "plateau_share_below_50" not in flags
        and "plateau_not_run" not in flags
        and bool(wfe_block.get("passes_robust_bar"))
    )
    if not gate_pass:
        flags.append("paper_gate_failed")

    return {
        "generated_utc": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "pnl_source": "qqq_close_to_close_proxy_closed_trades",
        "slippage": {
            "entry_slippage_bps": entry_slippage_bps,
            "exit_slippage_bps": exit_slippage_bps,
            "note": "Guide: open/close fills are worse than the printed price. Applied to research P&L only.",
        },
        "stretch_take_profit_is_exit": False,
        "data_window": data_window or {},
        "paper_gate": {
            "passes": gate_pass,
            "action_if_fail": "Set ALPACA_PAPER_TRADING=false until walk-forward and plateau clear the bar.",
            "required_paper_capital": capital,
            "half_kelly_allowed": False,
            "half_kelly_note": "Half-Kelly only after the walk-forward sample is large enough and the gate passes. Full Kelly is never used.",
        },
        "baseline": _variant_record(
            baseline,
            label="fixed_current",
            exit_mode="fixed",
            take_profit_pct=baseline_params.take_profit_pct,
        ),
        "exit_comparison": {
            "fixed_current": _variant_record(
                baseline,
                label="fixed_current",
                exit_mode="fixed",
                take_profit_pct=baseline_params.take_profit_pct,
            ),
            "fixed_wider": _variant_record(
                wider,
                label="fixed_wider",
                exit_mode="fixed",
                take_profit_pct=wider_tp,
            ),
            "atr_trail": _variant_record(
                atr,
                label="atr_trail",
                exit_mode="atr_trail",
                take_profit_pct=atr_params.take_profit_pct,
            ),
            "note": (
                "fixed_current uses TAKE_PROFIT_PCT. fixed_wider is a research target "
                f"({wider_tp:.0%}), not the {strategy_params.stretch_take_profit_pct:.0%} stretch. "
                "atr_trail turns the fixed take-profit exit off and exits on a "
                f"{strategy_params.atr_trail_mult:g}x ATR trail plus the existing stop, weaken, and max-hold rules."
            ),
        },
        "walk_forward_efficiency": wfe_block,
        "plateau": plateau,
        "flags": flags,
    }


def grid_neighbors_payload(
    rows: list[SweepResultRow],
    *,
    ticker: str,
    bars: int,
    data_window: dict[str, object] | None = None,
) -> dict[str, object]:
    share = profitable_neighbor_share(rows)
    best = rows[0] if rows else None
    compact = [
        {
            "rank": r.rank,
            "balanced_score": r.balanced_score,
            "min_confidence_to_trade": r.min_confidence_to_trade,
            "entry_dominance_gap_weight": r.entry_dominance_gap_weight,
            "regime_ranging_threshold_weight_add": r.regime_ranging_threshold_weight_add,
            "flip_min_hold_trading_days": r.flip_min_hold_trading_days,
            "stop_loss_pct": r.stop_loss_pct,
            "take_profit_pct": r.take_profit_pct,
            "total_trades": r.total_trades,
            "total_return_pct": r.total_return_pct,
            "max_drawdown_pct": r.max_drawdown_pct,
            "profitable": r.total_return_pct > 0,
        }
        for r in rows
    ]
    best_payload = None
    if best is not None:
        best_payload = {
            "rank": best.rank,
            "min_confidence_to_trade": best.min_confidence_to_trade,
            "entry_dominance_gap_weight": best.entry_dominance_gap_weight,
            "regime_ranging_threshold_weight_add": best.regime_ranging_threshold_weight_add,
            "flip_min_hold_trading_days": best.flip_min_hold_trading_days,
            "stop_loss_pct": best.stop_loss_pct,
            "take_profit_pct": best.take_profit_pct,
            "total_trades": best.total_trades,
            "total_return_pct": best.total_return_pct,
            "max_drawdown_pct": best.max_drawdown_pct,
        }
    return {
        "generated_utc": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "ticker": ticker,
        "bars": bars,
        "combination_count": len(rows),
        "data_window": data_window or {},
        "best": best_payload,
        "profitable_neighbor_share": share.share,
        "neighbor_count": share.neighbor_count,
        "profitable_neighbors": share.profitable_neighbors,
        "neighbors": list(share.neighbors),
        "rows": compact,
        "pnl_source": "qqq_close_to_close_proxy",
    }


def write_json(path: str | Path, payload: dict[str, object]) -> None:
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _fmt_pct(value: object, digits: int = 2) -> str:
    if value is None:
        return "n/a"
    return f"{float(value):.{digits}f}"


def _fmt_num(value: object, digits: int = 2) -> str:
    if value is None:
        return "n/a"
    return f"{float(value):.{digits}f}"


def format_strategy_eval_report(payload: dict[str, object]) -> str:
    baseline = payload["baseline"]
    assert isinstance(baseline, dict)
    metrics = baseline["metrics"]
    assert isinstance(metrics, dict)
    prom = metrics["prom"]
    assert isinstance(prom, dict)
    wfe = payload["walk_forward_efficiency"]
    assert isinstance(wfe, dict)
    plateau = payload.get("plateau")
    flags = payload.get("flags") or []
    window = payload.get("data_window") or {}
    assert isinstance(window, dict)
    slip = payload.get("slippage") or {}
    assert isinstance(slip, dict)
    gate = payload.get("paper_gate") or {}
    assert isinstance(gate, dict)
    capital = gate.get("required_paper_capital") or {}
    assert isinstance(capital, dict)

    lines = [
        "--- Strategy evaluation (QQQ proxy; not broker P&L) ---",
        (
            f"Daily bars loaded: {window.get('daily_bars_loaded', 'n/a')} | "
            f"Requested bars: {window.get('bars_requested', 'n/a')} | "
            f"Tested: {metrics.get('bars_tested', 'n/a')}"
        ),
        f"Window: {window.get('daily_start', 'n/a')} -> {window.get('daily_end', 'n/a')}",
        (
            f"Slippage: entry {slip.get('entry_slippage_bps', 0)} bps / "
            f"exit {slip.get('exit_slippage_bps', 0)} bps"
        ),
        "",
        "Baseline (fixed take-profit, current TAKE_PROFIT_PCT):",
        f"  Trades: {metrics['trade_count']} (flag if under {MIN_TRADES})",
        f"  Profit factor: {_fmt_num(metrics['profit_factor'])}",
        f"  Net profit: {_fmt_pct(metrics['net_profit_pct'])}% compounded "
        f"(sum of trade returns {_fmt_pct(metrics['net_profit_sum_pct'])}%)",
        f"  Max drawdown: {_fmt_pct(metrics['max_drawdown_pct'])}%",
        f"  Average drawdown: {_fmt_pct(metrics['average_drawdown_pct'])}% "
        f"(ratio {_fmt_num(metrics['max_dd_to_average_dd'])}; flag if max >= ~{DRAWDOWN_RATIO_BAR:.0f}x average)",
        f"  Reward/risk: {_fmt_num(metrics['reward_risk'])} (bar ~{REWARD_RISK_BAR:.0f})",
        (
            "  PROM: "
            f"{_fmt_pct(prom['prom_return_pct'])}% "
            f"(adjusted wins {_fmt_num(prom['adjusted_wins'])} x avg win {_fmt_pct(prom['average_win_pct'])}%, "
            f"adjusted losses {_fmt_num(prom['adjusted_losses'])} x avg loss {_fmt_pct(prom['average_loss_pct'])}%)"
        ),
    ]
    if wfe.get("available"):
        lines.append(
            "  Walk-forward efficiency: "
            f"{_fmt_num(wfe.get('walk_forward_efficiency'), 3)} "
            f"(ann. OOS {_fmt_pct(wfe.get('annualized_oos_pnl_pct'))}% / "
            f"ann. IS {_fmt_pct(wfe.get('annualized_is_pnl_pct'))}%; "
            f"method={wfe.get('method')}; "
            f"robust bar ~{WFE_ROBUST_LOW:.0%}–{WFE_ROBUST_HIGH:.0%}; "
            f"pass={wfe.get('passes_robust_bar')})"
        )
        for seg in wfe.get("equal_segments") or []:
            if isinstance(seg, dict):
                lines.append(
                    f"    segment {seg.get('segment')}/{seg.get('role')}: "
                    f"net={_fmt_pct(seg.get('total_return_pct'))}% "
                    f"trades={seg.get('total_trades')} bars={seg.get('bars')}"
                )
    else:
        lines.append("  Walk-forward efficiency: n/a (no equal-segment in-sample / out-of-sample split).")
    if isinstance(plateau, dict) and plateau.get("profitable_neighbor_share") is not None:
        lines.append(
            "  Plateau (profitable neighbor share): "
            f"{float(plateau['profitable_neighbor_share']) * 100:.1f}% "
            f"({plateau.get('profitable_neighbors')}/{plateau.get('neighbor_count')})"
        )
    else:
        lines.append("  Plateau: n/a (run with --small-grid).")
    lines.append(f"  Flags: {', '.join(flags) if flags else 'none'}")
    lines.append("")
    lines.append(
        f"Paper gate: {'PASS' if gate.get('passes') else 'FAIL'} — "
        f"{'treat paper as validated only after PASS' if gate.get('passes') else 'keep ALPACA_PAPER_TRADING=false'}"
    )
    lines.append(
        "  Required capital (max DD × "
        f"{_fmt_num(capital.get('safety_factor'), 1)}): "
        f"{_fmt_pct(100.0 * float(capital.get('required_capital_fraction_of_equity') or 0), 1)}% of equity "
        f"(~${_fmt_num(capital.get('required_capital_usd_at_reference'), 0)} at "
        f"${_fmt_num(capital.get('reference_equity_usd'), 0)} reference). Not the whole paper account."
    )
    lines.append("  Half-Kelly: off until the gate passes and the walk-forward sample is large enough.")
    lines.append("")
    lines.append("Exit comparison (stretch % is not an exit):")
    comparison = payload["exit_comparison"]
    assert isinstance(comparison, dict)
    for key in ("fixed_current", "fixed_wider", "atr_trail"):
        variant = comparison[key]
        assert isinstance(variant, dict)
        vm = variant["metrics"]
        assert isinstance(vm, dict)
        lines.append(
            f"  {key}: exit={variant['exit_mode']} tp={float(variant['take_profit_pct']):.0%} "
            f"trades={vm['trade_count']} net={_fmt_pct(vm['net_profit_pct'])}% "
            f"maxDD={_fmt_pct(vm['max_drawdown_pct'])}% "
            f"pf={_fmt_num(vm['profit_factor'])} rrr={_fmt_num(vm['reward_risk'])}"
        )
    lines.append("")
    lines.append("Notes: closed round-trips only. TQQQ ~ long QQQ, SQQQ ~ short QQQ.")
    return "\n".join(lines)


# Alias kept for research reports; live defaults now use the same tilt.
TREND_EMA_WEIGHTS: tuple[float, ...] = DEFAULT_SCORE_WEIGHTS
EQUAL_SCORE_WEIGHTS: tuple[float, ...] = (1.0,) * 8


def checklist_variant_specs(
    base: StrategyParams,
) -> list[tuple[str, str, StrategyParams]]:
    """Named research variants. ``base`` should be current live params."""
    return [
        ("baseline", "Current live decide() rules", base),
        (
            "equal_weights_legacy",
            "Pre-adoption equal checklist weights (1.0 × 8)",
            replace(base, score_weights=EQUAL_SCORE_WEIGHTS),
        ),
        (
            "aligned_entry",
            "Flat BUY only TQQQ in trend_up / SQQQ in trend_down; exits unchanged",
            replace(base, entry_require_regime_align=True),
        ),
        (
            "block_range_entry",
            "Skip flat BUY while regime is range; counter-trend still possible",
            replace(base, block_range_entries=True),
        ),
        (
            "no_flip",
            "Disable FLIP; exit only via weaken / stop / take-profit / max hold",
            replace(base, allow_flips=False),
        ),
        (
            "aligned_entry_flip_exit",
            "Aligned entries + flatten instead of flipping into adverse regime",
            replace(base, entry_require_regime_align=True, flip_adverse_becomes_exit=True),
        ),
        (
            "aligned_entry_no_flip",
            "Aligned entries + no flips",
            replace(base, entry_require_regime_align=True, allow_flips=False),
        ),
        (
            "adverse_weak_exit",
            "Raise weak threshold by 1.0 while holding off-regime (sooner weaken)",
            replace(base, adverse_hold_weak_add=1.0),
        ),
        (
            "blunt_adverse_exit",
            "Prior failed blunt filter: aligned entry + force exit when regime turns adverse",
            replace(base, entry_require_regime_align=True, exit_on_adverse_regime=True),
        ),
    ]


def compare_checklist_variants(
    candles: CandleData,
    *,
    anchor_date: str | None,
    blocked_dates: set[str],
    strategy_params: StrategyParams,
    bars: int,
    decide_options: DecideOptions | None = None,
    entry_slippage_bps: float = 10.0,
    exit_slippage_bps: float = 10.0,
    wfe_segments: int = 3,
    data_window: dict[str, object] | None = None,
) -> dict[str, object]:
    """Run fixed-exit metrics for each checklist redesign candidate (no exit-mode grid)."""
    common = dict(
        anchor_date=anchor_date,
        blocked_dates=blocked_dates,
        bars=bars,
        decide_options=decide_options,
        entry_slippage_bps=entry_slippage_bps,
        exit_slippage_bps=exit_slippage_bps,
    )
    rows: list[dict[str, object]] = []
    baseline_net: float | None = None
    split = is_oos_split(len(candles.daily), bars, n_segments=wfe_segments)

    for name, description, params in checklist_variant_specs(strategy_params):
        print(f"[checklist-compare] evaluating {name}...", flush=True)
        fixed = replace(params, exit_mode="fixed")
        result = run_backtest(candles, strategy_params=fixed, **common)
        metrics = metrics_from_backtest(result)
        wfe_value: float | None = None
        if split is not None:
            (is_lo, is_hi), (oos_lo, oos_hi), _segments = split
            in_sample = run_backtest(
                candles,
                strategy_params=fixed,
                loop_start_idx=is_lo,
                loop_end_idx_exclusive=is_hi,
                **common,
            )
            out_of_sample = run_backtest(
                candles,
                strategy_params=fixed,
                loop_start_idx=oos_lo,
                loop_end_idx_exclusive=oos_hi,
                **common,
            )
            wfe_block = walk_forward_efficiency_from_results(
                in_sample,
                out_of_sample,
                method=f"equal_{wfe_segments}_segments_last_oos",
            )
            raw = wfe_block.get("walk_forward_efficiency")
            wfe_value = None if raw is None else float(raw)
        net = float(metrics["net_profit_pct"])
        if name == "baseline":
            baseline_net = net
        delta = None if baseline_net is None else net - baseline_net
        rows.append(
            {
                "name": name,
                "description": description,
                "trade_count": metrics["trade_count"],
                "net_profit_pct": net,
                "net_profit_delta_vs_baseline": delta,
                "max_drawdown_pct": metrics["max_drawdown_pct"],
                "profit_factor": metrics["profit_factor"],
                "reward_risk": metrics["reward_risk"],
                "walk_forward_efficiency": wfe_value,
                "beats_baseline": bool(delta is not None and delta > 0),
            }
        )

    adoptable = [r for r in rows if r["name"] != "baseline" and r["beats_baseline"]]
    adoptable.sort(key=lambda r: float(r["net_profit_pct"]), reverse=True)
    best = adoptable[0] if adoptable else None
    return {
        "generated_utc": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "bars": bars,
        "slippage_bps": {"entry": entry_slippage_bps, "exit": exit_slippage_bps},
        "data_window": data_window or {},
        "adoption_rule": (
            "Adopt into live decide() defaults only when a variant beats baseline "
            "net_profit_pct on this window. Paper gate still independent."
        ),
        "variants": rows,
        "best_beating_baseline": best,
        "adopt_into_decide": best is not None,
        "adopted_variant": best["name"] if best else None,
    }


def format_checklist_compare_report(payload: dict[str, object]) -> str:
    lines = [
        "Checklist redesign compare (QQQ proxy, fixed exit, same window as --strategy-eval)",
        f"Bars: {payload.get('bars')}",
        "",
        f"{'variant':28s} {'trades':>6s} {'net%':>8s} {'Δbase':>8s} {'maxDD%':>8s} {'WFE':>8s} {'beat?':>5s}",
    ]
    for row in payload.get("variants", []):  # type: ignore[union-attr]
        assert isinstance(row, dict)
        delta = row.get("net_profit_delta_vs_baseline")
        delta_s = "—" if delta is None else f"{float(delta):+.2f}"
        wfe = row.get("walk_forward_efficiency")
        wfe_s = "n/a" if wfe is None else f"{float(wfe):.2f}"
        beat = "yes" if row.get("beats_baseline") else ("—" if row["name"] == "baseline" else "no")
        lines.append(
            f"{str(row['name']):28s} {int(row['trade_count']):6d} "
            f"{float(row['net_profit_pct']):8.2f} {delta_s:>8s} "
            f"{float(row['max_drawdown_pct']):8.2f} {wfe_s:>8s} {beat:>5s}"
        )
    lines.append("")
    adopted = payload.get("adopted_variant")
    if adopted:
        lines.append(f"Best beating baseline: {adopted} (candidate for decide() adoption).")
    else:
        lines.append("No variant beat baseline net P&L — leave live decide() rules unchanged.")
    return "\n".join(lines)


@dataclass(frozen=True)
class ResearchWindow:
    """Documented fetch window for strategy-eval / small-grid.

    Requests ~4 years of daily and as much hourly as the feed allows so the
    4h series covers the swing window. Older gaps still fall back to daily-derived 4h.
    """

    daily_lookback_days: int = 1460
    max_daily_bars: int = 1100
    hourly_lookback_days: int = 1460
    max_hourly_bars: int = 20000
