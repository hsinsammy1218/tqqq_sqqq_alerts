from strategy_eval import EQUAL_SCORE_WEIGHTS, TREND_EMA_WEIGHTS, checklist_variant_specs
from strategy_params import StrategyParams


def _base() -> StrategyParams:
    return StrategyParams(
        bull_entry_threshold=5,
        bear_entry_threshold=5,
        weak_threshold=3,
        stop_loss_pct=0.08,
        take_profit_pct=0.15,
        stretch_take_profit_pct=0.25,
        max_hold_days=10,
        entry_atr_multiplier=0.5,
        score_weights=TREND_EMA_WEIGHTS,
        regime_sep_atr_mult=0.12,
        regime_slope_atr_mult=0.03,
        regime_ranging_threshold_weight_add=0.55,
        regime_trend_favorable_delta=0.5,
        flip_min_hold_trading_days=2,
        flip_margin_weight=0.5,
        entry_dominance_gap_weight=0.75,
        flip_in_range_regime=False,
        min_confidence_to_trade=62,
    )


def test_checklist_variant_specs_include_baseline_and_blunt() -> None:
    specs = checklist_variant_specs(_base())
    names = [n for n, _, p in specs]
    assert names[0] == "baseline"
    assert "blunt_adverse_exit" in names
    assert "equal_weights_legacy" in names
    assert "aligned_entry" in names
    blunt = next(p for n, _, p in specs if n == "blunt_adverse_exit")
    assert blunt.entry_require_regime_align is True
    assert blunt.exit_on_adverse_regime is True
    legacy = next(p for n, _, p in specs if n == "equal_weights_legacy")
    assert legacy.score_weights == EQUAL_SCORE_WEIGHTS
    assert specs[0][2].score_weights == TREND_EMA_WEIGHTS
    assert specs[0][2].entry_require_regime_align is False
    assert specs[0][2].allow_flips is True


def test_replace_preserves_new_knobs() -> None:
    from dataclasses import replace

    p = replace(_base(), entry_require_regime_align=True, allow_flips=False)
    assert p.entry_require_regime_align is True
    assert p.allow_flips is False
    assert p.block_range_entries is False
