"""Slippage and paper-capital helpers. No network."""

from __future__ import annotations

import pytest

from backtest import _trade_return_pct, _trade_return_pct_with_slippage, apply_fill_slippage
from strategy_eval import is_oos_split, required_paper_capital


def test_apply_fill_slippage_worsens_long_and_short():
    long_e, long_x = apply_fill_slippage("TQQQ", 100.0, 110.0, entry_slippage_bps=10, exit_slippage_bps=10)
    assert long_e == pytest.approx(100.1)
    assert long_x == pytest.approx(109.89)
    short_e, short_x = apply_fill_slippage("SQQQ", 100.0, 90.0, entry_slippage_bps=10, exit_slippage_bps=10)
    assert short_e == pytest.approx(99.9)
    assert short_x == pytest.approx(90.09)


def test_slippage_reduces_proxy_return():
    clean = _trade_return_pct("TQQQ", 100.0, 110.0)
    slipped = _trade_return_pct_with_slippage(
        "TQQQ", 100.0, 110.0, entry_slippage_bps=10, exit_slippage_bps=10
    )
    assert slipped < clean
    assert slipped == pytest.approx((109.89 / 100.1 - 1.0) * 100.0)


def test_equal_segments_last_is_oos():
    split = is_oos_split(360, 300, n_segments=3, min_bars=20)
    assert split is not None
    (is_lo, is_hi), (oos_lo, oos_hi), segments = split
    assert len(segments) == 3
    assert is_lo == segments[0][0]
    assert is_hi == segments[1][1]
    assert (oos_lo, oos_hi) == segments[2]
    assert is_hi == oos_lo
    # Nearly equal lengths.
    lengths = [hi - lo for lo, hi in segments]
    assert max(lengths) - min(lengths) <= 1


def test_required_paper_capital_uses_drawdown_times_safety():
    cap = required_paper_capital(20.0, safety_factor=2.0, reference_equity=100_000)
    assert cap["required_capital_fraction_of_equity"] == pytest.approx(0.40)
    assert cap["required_capital_usd_at_reference"] == pytest.approx(40_000)
