"""Metric tests use synthetic closed-trade returns. No network and no broker P&L."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from backtest_sweep import SweepResultRow, profitable_neighbor_share, small_research_grid
from strategy_eval import (
    annualized_pnl_pct,
    metrics_from_returns,
    prom_return_pct,
    walk_forward_efficiency,
    wider_take_profit_pct,
)


def test_prom_shrinks_wins_and_inflates_losses():
    returns = [10.0, 10.0, 10.0, -5.0, -5.0]
    prom = prom_return_pct(returns)
    assert prom["n_wins"] == 3
    assert prom["n_losses"] == 2
    assert prom["adjusted_wins"] == pytest.approx(3 - 3**0.5)
    assert prom["adjusted_losses"] == pytest.approx(2 + 2**0.5)
    assert prom["average_win_pct"] == pytest.approx(10.0)
    assert prom["average_loss_pct"] == pytest.approx(-5.0)
    expected = (3 - 3**0.5) * 10.0 + (2 + 2**0.5) * -5.0
    assert prom["prom_return_pct"] == pytest.approx(expected)
    assert prom["prom_return_pct"] < 0


def test_metrics_from_synthetic_trades_flag_sample_size_and_reward_risk():
    returns = [10.0, 10.0, 10.0, -5.0, -5.0]
    metrics = metrics_from_returns(returns)
    assert metrics["trade_count"] == 5
    assert metrics["profit_factor"] == pytest.approx(3.0)
    assert metrics["net_profit_sum_pct"] == pytest.approx(20.0)
    assert metrics["net_profit_pct"] == pytest.approx((1.1**3 * 0.95**2 - 1.0) * 100.0)
    assert metrics["max_drawdown_pct"] > 0
    assert metrics["average_drawdown_pct"] > 0
    assert "trade_count_under_30" in metrics["flags"]
    assert "reward_risk_below_3" in metrics["flags"]
    assert metrics["pnl_source"] == "backtest_closed_trade_returns_qqq_proxy"


def test_drawdown_ratio_flag_when_max_is_at_least_three_times_average():
    spiked = metrics_from_returns([10.0, -0.5, 0.6, -0.5, 0.6, -0.5, 0.6, -15.0])
    assert float(spiked["max_dd_to_average_dd"]) >= 3
    assert "max_drawdown_ge_3x_average" in spiked["flags"]
    mild = metrics_from_returns([5.0, -1.0, 1.0, -20.0])
    assert float(mild["max_dd_to_average_dd"]) < 3
    assert "max_drawdown_ge_3x_average" not in mild["flags"]


def test_walk_forward_efficiency_ratio_and_non_positive_in_sample():
    ann_is = annualized_pnl_pct(100.0, 252)
    ann_oos = annualized_pnl_pct(50.0, 252)
    assert ann_is == pytest.approx(100.0)
    assert ann_oos == pytest.approx(50.0)
    assert walk_forward_efficiency(ann_oos, ann_is) == pytest.approx(0.5)
    assert walk_forward_efficiency(-10.0, 20.0) == pytest.approx(-0.5)
    assert walk_forward_efficiency(10.0, 0.0) is None
    assert walk_forward_efficiency(10.0, -5.0) is None
    assert walk_forward_efficiency(None, 10.0) is None


def test_wider_take_profit_is_not_the_stretch_level():
    assert wider_take_profit_pct(0.15, 0.25) == pytest.approx(0.30)
    # 30% would have copied the stretch, so the comparison target steps past it.
    assert wider_take_profit_pct(0.15, 0.30) == pytest.approx(0.35)
    assert wider_take_profit_pct(0.30, 0.40) == pytest.approx(0.45)


def _sweep_row(**kwargs: object) -> SweepResultRow:
    base: dict[str, object] = dict(
        rank=1,
        balanced_score=1.0,
        bull_entry_threshold=5,
        bear_entry_threshold=5,
        weak_score_threshold=3,
        regime_ranging_threshold_weight_add=0.55,
        flip_min_hold_trading_days=3,
        flip_margin_weight=1.25,
        entry_dominance_gap_weight=1.25,
        min_confidence_to_trade=62,
        total_trades=10,
        win_rate_pct=50.0,
        average_return_pct=1.0,
        median_return_pct=1.0,
        best_trade_pct=2.0,
        worst_trade_pct=-1.0,
        max_drawdown_pct=5.0,
        average_hold_days=3.0,
        flip_count=0,
        cash_periods=0,
        equity_end=1.1,
        total_return_pct=10.0,
        stop_loss_pct=0.08,
        take_profit_pct=0.15,
    )
    base.update(kwargs)
    return SweepResultRow(**base)  # type: ignore[arg-type]


def test_profitable_neighbor_share_around_best_row():
    rows = [
        _sweep_row(rank=1, balanced_score=5.0, min_confidence_to_trade=62, total_return_pct=10.0),
        _sweep_row(rank=2, balanced_score=1.0, min_confidence_to_trade=54, total_return_pct=-2.0),
        _sweep_row(rank=3, balanced_score=2.0, stop_loss_pct=0.10, total_return_pct=4.0),
    ]
    share = profitable_neighbor_share(rows)
    assert share.neighbor_count == 2
    assert share.profitable_neighbors == 1
    assert share.share == pytest.approx(0.5)


def test_small_research_grid_is_two_levels_on_six_knobs():
    settings = SimpleNamespace(
        bull_entry_threshold=5,
        bear_entry_threshold=5,
        weak_score_threshold=3,
        regime_ranging_threshold_weight_add=0.55,
        flip_min_hold_trading_days=3,
        flip_margin_weight=1.25,
        entry_dominance_gap_weight=1.25,
        min_confidence_to_trade=62,
        stop_loss_pct=0.08,
        take_profit_pct=0.15,
    )
    grid = small_research_grid(settings)  # type: ignore[arg-type]
    assert grid.combination_count == 64
    assert len(grid.min_confidence) == 2
    assert len(grid.entry_dominance_gap) == 2
    assert len(grid.regime_ranging_add) == 2
    assert len(grid.flip_min_hold) == 2
    assert len(grid.stop_loss) == 2
    assert len(grid.take_profit) == 2
    assert grid.bull_entry == (5,)
