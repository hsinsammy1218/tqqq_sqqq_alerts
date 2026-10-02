"""Phase 1 offline labels: ETF round-trip outcomes + features at flat BUY.

Development window only. Does not enable PROBABILITY_CONFIDENCE, retune weights,
or touch the sealed 2025-10-01→2026-10-01 holdout for fitting.
"""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from backtest import BacktestTradeRow
from etf_backtest import (
    DEFAULT_ETF_COST_BPS,
    PhaseEvaluation,
    evaluate_development,
)
from indicators import IndicatorSnapshot
from strategy import DecideOptions, PositionState, decide
from strategy_params import StrategyParams
from strategy_scoring import (
    detect_market_regime,
    effective_thresholds,
    normalized_confidence_score,
    weighted_signal_breakdown,
)
from strategy_types import AlertDecision, StrategyDebug

FEATURE_SCHEMA_VERSION = 1

CHECKLIST_NAMES: tuple[str, ...] = (
    "daily_close_vs_ema20",
    "daily_ema20_vs_ema50",
    "daily_rsi14_vs_50",
    "daily_macd_vs_signal",
    "h4_close_vs_ema20",
    "h4_ema20_vs_ema50",
    "price_vs_weekly_vwap",
    "volume_vs_sma20",
)

DOMINANCE_BUCKETS: tuple[tuple[str, int, int], ...] = (
    ("lt_50", 0, 49),
    ("50_61", 50, 61),
    ("62_74", 62, 74),
    ("75_plus", 75, 100),
)

DecideFn = Callable[..., tuple]


@dataclass(frozen=True)
class ChecklistHits:
    """Paired 0/1 checklist bits (bull side, bear side) in checklist order."""

    bull: tuple[int, ...]
    bear: tuple[int, ...]

    def __post_init__(self) -> None:
        if len(self.bull) != 8 or len(self.bear) != 8:
            raise ValueError("checklist hits must contain exactly 8 bull and 8 bear bits")
        if any(b not in (0, 1) for b in self.bull + self.bear):
            raise ValueError("checklist hits must be 0 or 1")


@dataclass(frozen=True)
class EntryFeatureVector:
    """Features recorded at the flat-BUY decision bar (signal time, pre-fill)."""

    schema_version: int
    entry_timestamp: str
    symbol: str
    regime: str
    regime_trend_up: int
    regime_trend_down: int
    regime_range: int
    bull_pct: int
    bear_pct: int
    weighted_bull: float
    weighted_bear: float
    weight_scale: float
    dominance: int
    effective_bull_entry: float
    effective_bear_entry: float
    effective_weak: float
    bull_hits: tuple[int, ...]
    bear_hits: tuple[int, ...]
    bull_weights: tuple[float, ...]
    bear_weights: tuple[float, ...]


@dataclass(frozen=True)
class ProbabilityLabelRow:
    """One closed ETF round-trip with entry features and binary win label."""

    features: EntryFeatureVector
    exit_timestamp: str
    action: str
    entry_price: float
    exit_price: float
    return_pct: float
    hold_days: int
    cost_bps: float
    label_profitable: bool
    window_role: str = "development"

    def to_flat_dict(self) -> dict[str, Any]:
        f = self.features
        out: dict[str, Any] = {
            "schema_version": f.schema_version,
            "window_role": self.window_role,
            "entry_timestamp": f.entry_timestamp,
            "exit_timestamp": self.exit_timestamp,
            "symbol": f.symbol,
            "action": self.action,
            "entry_price": self.entry_price,
            "exit_price": self.exit_price,
            "return_pct": self.return_pct,
            "hold_days": self.hold_days,
            "cost_bps": self.cost_bps,
            "label_profitable": self.label_profitable,
            "regime": f.regime,
            "regime_trend_up": f.regime_trend_up,
            "regime_trend_down": f.regime_trend_down,
            "regime_range": f.regime_range,
            "bull_pct": f.bull_pct,
            "bear_pct": f.bear_pct,
            "weighted_bull": f.weighted_bull,
            "weighted_bear": f.weighted_bear,
            "weight_scale": f.weight_scale,
            "dominance": f.dominance,
            "effective_bull_entry": f.effective_bull_entry,
            "effective_bear_entry": f.effective_bear_entry,
            "effective_weak": f.effective_weak,
        }
        for i, name in enumerate(CHECKLIST_NAMES):
            out[f"bull_hit_{name}"] = f.bull_hits[i]
            out[f"bear_hit_{name}"] = f.bear_hits[i]
            out[f"bull_w_{name}"] = f.bull_weights[i]
            out[f"bear_w_{name}"] = f.bear_weights[i]
        return out


def label_profitable_round_trip(return_pct: float) -> bool:
    """True when net round-trip return after the ETF fill/cost model is > 0."""
    return float(return_pct) > 0.0


def dominance_bucket(dominance: int) -> str:
    d = int(dominance)
    for name, lo, hi in DOMINANCE_BUCKETS:
        if lo <= d <= hi:
            return name
    if d < 0:
        return DOMINANCE_BUCKETS[0][0]
    return DOMINANCE_BUCKETS[-1][0]


def checklist_hit_bits(snapshot: IndicatorSnapshot) -> ChecklistHits:
    """Eight paired bull/bear checklist conditions (same order as scoring)."""
    bull = (
        int(snapshot.daily_close > snapshot.daily_ema20),
        int(snapshot.daily_ema20 > snapshot.daily_ema50),
        int(snapshot.daily_rsi14 > 50),
        int(snapshot.daily_macd > snapshot.daily_macd_signal),
        int(snapshot.h4_close > snapshot.h4_ema20),
        int(snapshot.h4_ema20 > snapshot.h4_ema50),
        int(snapshot.daily_close > snapshot.daily_weekly_vwap),
        int(snapshot.daily_volume > snapshot.daily_vol_sma20),
    )
    bear = (
        int(snapshot.daily_close < snapshot.daily_ema20),
        int(snapshot.daily_ema20 < snapshot.daily_ema50),
        int(snapshot.daily_rsi14 < 50),
        int(snapshot.daily_macd < snapshot.daily_macd_signal),
        int(snapshot.h4_close < snapshot.h4_ema20),
        int(snapshot.h4_ema20 < snapshot.h4_ema50),
        int(snapshot.daily_close < snapshot.daily_weekly_vwap),
        int(snapshot.daily_volume < snapshot.daily_vol_sma20),
    )
    return ChecklistHits(bull=bull, bear=bear)


def applied_checklist_weights(
    hits: ChecklistHits,
    weights: Sequence[float],
) -> tuple[tuple[float, ...], tuple[float, ...]]:
    if len(weights) != 8:
        raise ValueError("score_weights must contain exactly 8 values")
    bull_w = tuple(float(weights[i]) if hits.bull[i] else 0.0 for i in range(8))
    bear_w = tuple(float(weights[i]) if hits.bear[i] else 0.0 for i in range(8))
    return bull_w, bear_w


def build_entry_feature_vector(
    snapshot: IndicatorSnapshot,
    *,
    params: StrategyParams,
    entry_timestamp: str,
    symbol: str,
    dbg: StrategyDebug | None = None,
) -> EntryFeatureVector:
    """Build the v1 feature vector at a flat-BUY decision."""
    hits = checklist_hit_bits(snapshot)
    bull_w, bear_w = applied_checklist_weights(hits, params.score_weights)
    bd = weighted_signal_breakdown(snapshot, params.score_weights)
    scale = max(bd.max_weight_total, 1e-9)
    regime = dbg.regime if dbg is not None else detect_market_regime(snapshot, params)
    if dbg is not None:
        wb = float(dbg.weighted_bull)
        wbear = float(dbg.weighted_bear)
        bull_eff = float(dbg.effective_bull_entry)
        bear_eff = float(dbg.effective_bear_entry)
        weak_eff = float(dbg.effective_weak)
        dominance = int(dbg.normalized_confidence)
    else:
        wb = float(bd.weighted_bull)
        wbear = float(bd.weighted_bear)
        bull_eff, bear_eff, weak_eff = effective_thresholds(regime, params)  # type: ignore[arg-type]
        dominance = normalized_confidence_score(wb, wbear, scale)
    bull_pct = int(round(100 * wb / scale))
    bear_pct = int(round(100 * wbear / scale))
    return EntryFeatureVector(
        schema_version=FEATURE_SCHEMA_VERSION,
        entry_timestamp=entry_timestamp,
        symbol=symbol,
        regime=str(regime),
        regime_trend_up=int(regime == "trend_up"),
        regime_trend_down=int(regime == "trend_down"),
        regime_range=int(regime == "range"),
        bull_pct=bull_pct,
        bear_pct=bear_pct,
        weighted_bull=wb,
        weighted_bear=wbear,
        weight_scale=scale,
        dominance=dominance,
        effective_bull_entry=bull_eff,
        effective_bear_entry=bear_eff,
        effective_weak=weak_eff,
        bull_hits=hits.bull,
        bear_hits=hits.bear,
        bull_weights=bull_w,
        bear_weights=bear_w,
    )


def label_row_from_trade(
    trade: BacktestTradeRow,
    features: EntryFeatureVector,
    *,
    cost_bps: float = DEFAULT_ETF_COST_BPS,
    window_role: str = "development",
) -> ProbabilityLabelRow:
    """Join a closed ETF trade row to its entry feature vector."""
    if features.entry_timestamp and trade.entry_timestamp:
        if features.entry_timestamp != trade.entry_timestamp:
            raise ValueError(
                "entry_timestamp mismatch between features "
                f"({features.entry_timestamp}) and trade ({trade.entry_timestamp})"
            )
    if features.symbol and trade.symbol and features.symbol != trade.symbol:
        raise ValueError(
            f"symbol mismatch between features ({features.symbol}) and trade ({trade.symbol})"
        )
    return ProbabilityLabelRow(
        features=features,
        exit_timestamp=trade.timestamp,
        action=trade.action,
        entry_price=trade.entry_price,
        exit_price=trade.exit_price,
        return_pct=trade.return_pct,
        hold_days=trade.hold_days,
        cost_bps=float(cost_bps),
        label_profitable=label_profitable_round_trip(trade.return_pct),
        window_role=window_role,
    )


def label_rows_from_trades(
    trades: Sequence[BacktestTradeRow],
    features_by_entry_ts: Mapping[str, EntryFeatureVector],
    *,
    cost_bps: float = DEFAULT_ETF_COST_BPS,
    window_role: str = "development",
) -> list[ProbabilityLabelRow]:
    """Attach features to closed trades keyed by entry_timestamp."""
    rows: list[ProbabilityLabelRow] = []
    missing: list[str] = []
    for trade in trades:
        key = trade.entry_timestamp or ""
        feats = features_by_entry_ts.get(key)
        if feats is None:
            missing.append(key or trade.timestamp)
            continue
        rows.append(
            label_row_from_trade(
                trade,
                feats,
                cost_bps=cost_bps,
                window_role=window_role,
            )
        )
    if missing:
        raise KeyError(
            "missing entry features for "
            f"{len(missing)} trade(s); first={missing[0]!r}"
        )
    return rows


def make_feature_capturing_decide(
    store: dict[str, EntryFeatureVector],
    *,
    decide_fn: DecideFn | None = None,
) -> DecideFn:
    """Wrap ``decide`` so flat BUY bars record an entry feature vector."""

    base = decide if decide_fn is None else decide_fn

    def _wrapped(
        snapshot: IndicatorSnapshot,
        position: PositionState,
        blocked_dates: set[str],
        now_utc,
        params: StrategyParams,
        decide_options: DecideOptions | None = None,
    ) -> tuple[AlertDecision, PositionState, StrategyDebug]:
        was_flat = position.active_symbol is None
        alert, intended, dbg = base(
            snapshot,
            position,
            blocked_dates,
            now_utc,
            params,
            decide_options=decide_options,
        )
        if was_flat and (alert.alert_type or "").upper() == "BUY":
            store[alert.timestamp] = build_entry_feature_vector(
                snapshot,
                params=params,
                entry_timestamp=alert.timestamp,
                symbol=str(alert.symbol),
                dbg=dbg,
            )
        return alert, intended, dbg

    return _wrapped


@dataclass
class LabelBuildResult:
    rows: list[ProbabilityLabelRow]
    evaluation: PhaseEvaluation
    cost_bps: float
    feature_schema_version: int = FEATURE_SCHEMA_VERSION
    notes: list[str] = field(default_factory=list)

    @property
    def label_count(self) -> int:
        return len(self.rows)

    @property
    def win_rate(self) -> float:
        if not self.rows:
            return 0.0
        wins = sum(1 for r in self.rows if r.label_profitable)
        return wins / len(self.rows)


def collect_development_labels(
    qqq,
    tqqq_daily,
    sqqq_daily,
    *,
    strategy_params: StrategyParams,
    blocked_dates: set[str],
    anchor_date: str | None,
    requested_bars: int,
    expand_default: bool = True,
    cost_bps: float = DEFAULT_ETF_COST_BPS,
    decide_options: DecideOptions | None = None,
) -> LabelBuildResult:
    """Replay the development window and join closed trades to BUY features."""
    feature_store: dict[str, EntryFeatureVector] = {}
    capturing = make_feature_capturing_decide(feature_store)
    evaluation = evaluate_development(
        qqq,
        tqqq_daily,
        sqqq_daily,
        strategy_params=strategy_params,
        blocked_dates=blocked_dates,
        anchor_date=anchor_date,
        requested_bars=requested_bars,
        expand_default=expand_default,
        cost_bps=cost_bps,
        decide_options=decide_options,
        decide_fn=capturing,
    )
    # Guard: never use sealed dates for labels.
    sealed_start = evaluation.holdout_start_utc
    rows = label_rows_from_trades(
        evaluation.result.trade_rows,
        feature_store,
        cost_bps=cost_bps,
        window_role="development",
    )
    for row in rows:
        if sealed_start and row.features.entry_timestamp[:10] >= sealed_start[:10]:
            raise RuntimeError(
                "refusing label whose entry falls in/after sealed holdout "
                f"({row.features.entry_timestamp} >= {sealed_start})"
            )
    notes = [
        f"development {evaluation.development_start_utc} → {evaluation.development_end_utc}",
        f"sealed holdout reserved (not labeled): {evaluation.holdout_start_utc} → {evaluation.holdout_end_utc}",
        f"cost_bps={cost_bps}",
        f"closed_trades={evaluation.result.closed_trades}",
        f"feature_schema_version={FEATURE_SCHEMA_VERSION}",
        "PROBABILITY_CONFIDENCE not enabled; live MIN_CONFIDENCE_TO_TRADE unchanged",
    ]
    return LabelBuildResult(
        rows=rows,
        evaluation=evaluation,
        cost_bps=float(cost_bps),
        notes=notes,
    )


def summarize_labels(rows: Sequence[ProbabilityLabelRow]) -> dict[str, Any]:
    n = len(rows)
    wins = sum(1 for r in rows if r.label_profitable)
    by_bucket: dict[str, dict[str, int]] = {
        name: {"n": 0, "wins": 0} for name, _, _ in DOMINANCE_BUCKETS
    }
    by_symbol: dict[str, dict[str, int]] = {}
    by_regime: dict[str, dict[str, int]] = {}
    for row in rows:
        b = dominance_bucket(row.features.dominance)
        by_bucket[b]["n"] += 1
        by_bucket[b]["wins"] += int(row.label_profitable)
        sym = row.features.symbol
        by_symbol.setdefault(sym, {"n": 0, "wins": 0})
        by_symbol[sym]["n"] += 1
        by_symbol[sym]["wins"] += int(row.label_profitable)
        reg = row.features.regime
        by_regime.setdefault(reg, {"n": 0, "wins": 0})
        by_regime[reg]["n"] += 1
        by_regime[reg]["wins"] += int(row.label_profitable)

    def _rate(block: dict[str, int]) -> dict[str, Any]:
        nn = block["n"]
        ww = block["wins"]
        return {
            "n": nn,
            "wins": ww,
            "win_rate": (ww / nn) if nn else None,
        }

    return {
        "label_count": n,
        "wins": wins,
        "losses": n - wins,
        "win_rate": (wins / n) if n else None,
        "by_dominance_bucket": {k: _rate(v) for k, v in by_bucket.items()},
        "by_symbol": {k: _rate(v) for k, v in by_symbol.items()},
        "by_regime": {k: _rate(v) for k, v in by_regime.items()},
        "mean_return_pct": (sum(r.return_pct for r in rows) / n) if n else None,
        "feature_schema_version": FEATURE_SCHEMA_VERSION,
    }


def write_label_artifacts(
    result: LabelBuildResult,
    *,
    reports_dir: Path | str = Path("reports"),
    csv_name: str = "probability_labels.csv",
    summary_name: str = "probability_labels_summary.json",
) -> dict[str, Path]:
    """Write gitignored CSV + JSON summary under reports/."""
    out_dir = Path(reports_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    csv_path = out_dir / csv_name
    summary_path = out_dir / summary_name
    flat_rows = [row.to_flat_dict() for row in result.rows]
    fieldnames: list[str] = []
    if flat_rows:
        fieldnames = list(flat_rows[0].keys())
    with csv_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames or ["label_profitable"])
        writer.writeheader()
        for row in flat_rows:
            payload = dict(row)
            payload["label_profitable"] = bool(payload["label_profitable"])
            writer.writerow(payload)
    summary = {
        "role": "development",
        "development_start_utc": result.evaluation.development_start_utc,
        "development_end_utc": result.evaluation.development_end_utc,
        "holdout_start_utc": result.evaluation.holdout_start_utc,
        "holdout_end_utc": result.evaluation.holdout_end_utc,
        "cost_bps": result.cost_bps,
        "score_weights": list(result.evaluation.score_weights),
        "notes": result.notes,
        "summary": summarize_labels(result.rows),
        "paths": {
            "csv": str(csv_path),
            "summary_json": str(summary_path),
        },
        "sealed_peek": False,
    }
    summary_path.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    return {"csv": csv_path, "summary_json": summary_path}
