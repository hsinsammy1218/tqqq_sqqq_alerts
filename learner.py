"""Read-only learner agent: digest paper trades and propose (not apply) knobs.

Separate from the paper trader path. Reads the same backends as
``--trade-log-report`` (``logs/trades.jsonl`` and/or Supabase ``bot_trade_log``).
Writes a digest + optional proposals. Never places orders, never edits
``DEFAULT_SCORE_WEIGHTS`` / live ``decide()`` defaults / paper latches.

Any proposed checklist/env change must still be validated via
``--strategy-eval`` / WFE (and typically ``--checklist-compare``) before humans
adopt it by hand.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import requests

from trade_log import DEFAULT_TRADE_LOG_PATH, read_trade_records
from trade_log_report import (
    DEFAULT_RESEARCH_MAX_DD_PCT,
    MIN_STRATEGY_ROUND_TRIPS_FOR_RULES,
    build_trade_log_report,
)

DEFAULT_DIGEST_PATH = Path("reports/learner_digest.json")
DEFAULT_PROPOSALS_PATH = Path("reports/learner_proposals.md")

# Human-facing reminder attached to every proposal document.
VALIDATION_REMINDER = (
    "Do not auto-apply. Validate any proposed checklist/env change with "
    "`python main.py --strategy-eval` (and WFE / `--checklist-compare` as needed) "
    "before humans copy knobs into `.env` or code. This learner never edits "
    "DEFAULT_SCORE_WEIGHTS or live decide() defaults."
)


def build_learner_digest(
    records: list[dict[str, Any]],
    *,
    research_max_dd_pct: float = DEFAULT_RESEARCH_MAX_DD_PCT,
    min_strategy_round_trips: int = MIN_STRATEGY_ROUND_TRIPS_FOR_RULES,
    source_path: str | None = None,
) -> dict[str, Any]:
    """Build digest from trade rows (reuses trade-log report core fields)."""
    report = build_trade_log_report(
        records,
        research_max_dd_pct=research_max_dd_pct,
        min_strategy_round_trips=min_strategy_round_trips,
        source_path=source_path,
    )
    pnl = report.get("realized_pnl") or {}
    strategy_trips = int(pnl.get("strategy_round_trip_count") or 0)
    remaining = max(0, min_strategy_round_trips - strategy_trips)
    enough = bool(report.get("enough_data_for_rule_changes"))

    return {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "agent": "learner",
        "read_only": True,
        "auto_apply": False,
        "source_path": report.get("source_path"),
        "row_count": report.get("row_count", 0),
        "counts": report.get("counts") or {},
        "strategy_vs_manual_test": {
            "strategy_rows": (report.get("counts") or {}).get("by_source", {}).get("strategy", 0),
            "manual_test_rows": (report.get("counts") or {}).get("by_source", {}).get("manual_test", 0),
            "strategy_round_trips": strategy_trips,
            "manual_test_round_trips": int(pnl.get("manual_test_round_trip_count") or 0),
            "round_trip_count": int(pnl.get("round_trip_count") or 0),
        },
        "realized_pnl": {
            "total_pnl_abs": pnl.get("total_pnl_abs"),
            "sum_pnl_pct": pnl.get("sum_pnl_pct"),
            "strategy_round_trip_count": strategy_trips,
            "manual_test_round_trip_count": pnl.get("manual_test_round_trip_count"),
            "round_trip_count": pnl.get("round_trip_count"),
            # Full trip list kept for offline analysis; proposals use aggregates.
            "round_trips": pnl.get("round_trips") or [],
        },
        "drawdown_comparison": report.get("drawdown_comparison") or {},
        "enough_data": enough,
        "enough_data_for_rule_changes": enough,
        "min_strategy_round_trips_for_rules": min_strategy_round_trips,
        "strategy_round_trips_remaining": remaining,
        "messages": list(report.get("messages") or []),
        "validation_reminder": VALIDATION_REMINDER,
    }


def _regime_pnl_buckets(trips: list[dict[str, Any]]) -> dict[str, dict[str, float | int]]:
    buckets: dict[str, dict[str, float | int]] = {}
    for trip in trips:
        if str(trip.get("entry_source") or "") != "strategy" and str(trip.get("exit_source") or "") != "strategy":
            continue
        regime = str(trip.get("regime") or "unknown")
        pnl = trip.get("pnl_pct")
        if pnl is None:
            continue
        slot = buckets.setdefault(regime, {"count": 0, "sum_pnl_pct": 0.0})
        slot["count"] = int(slot["count"]) + 1
        slot["sum_pnl_pct"] = float(slot["sum_pnl_pct"]) + float(pnl)
    for slot in buckets.values():
        count = int(slot["count"])
        slot["avg_pnl_pct"] = round(float(slot["sum_pnl_pct"]) / count, 4) if count else 0.0
        slot["sum_pnl_pct"] = round(float(slot["sum_pnl_pct"]), 4)
    return buckets


def _confidence_outcome_hint(trips: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Crude split: strategy trips with entry confidence <50 vs ≥75."""
    low: list[float] = []
    high: list[float] = []
    for trip in trips:
        if str(trip.get("entry_source") or "") != "strategy" and str(trip.get("exit_source") or "") != "strategy":
            continue
        conf = trip.get("confidence")
        pnl = trip.get("pnl_pct")
        if conf is None or pnl is None:
            continue
        try:
            c = float(conf)
            p = float(pnl)
        except (TypeError, ValueError):
            continue
        if c < 50:
            low.append(p)
        elif c >= 75:
            high.append(p)
    if not low and not high:
        return None
    return {
        "low_conf_count": len(low),
        "low_conf_avg_pnl_pct": round(sum(low) / len(low), 4) if low else None,
        "high_conf_count": len(high),
        "high_conf_avg_pnl_pct": round(sum(high) / len(high), 4) if high else None,
    }


def _hold_calendar_days(trip: dict[str, Any]) -> int | None:
    """Whole calendar days between entry and exit timestamps when parseable."""
    entry_raw = trip.get("entry_timestamp")
    exit_raw = trip.get("exit_timestamp")
    if not entry_raw or not exit_raw:
        return None
    try:
        entry = datetime.fromisoformat(str(entry_raw).replace("Z", "+00:00"))
        exit_ = datetime.fromisoformat(str(exit_raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    return max(0, (exit_.date() - entry.date()).days)


def _hold_pnl_buckets(trips: list[dict[str, Any]]) -> dict[str, dict[str, float | int]]:
    """Split strategy trips into short (≤10d) vs longer holds for proposal hints."""
    buckets: dict[str, dict[str, float | int]] = {
        "short_le_10d": {"count": 0, "sum_pnl_pct": 0.0},
        "long_11d_plus": {"count": 0, "sum_pnl_pct": 0.0},
    }
    for trip in trips:
        if str(trip.get("entry_source") or "") != "strategy" and str(trip.get("exit_source") or "") != "strategy":
            continue
        pnl = trip.get("pnl_pct")
        hold = _hold_calendar_days(trip)
        if pnl is None or hold is None:
            continue
        key = "short_le_10d" if hold <= 10 else "long_11d_plus"
        slot = buckets[key]
        slot["count"] = int(slot["count"]) + 1
        slot["sum_pnl_pct"] = float(slot["sum_pnl_pct"]) + float(pnl)
    for slot in buckets.values():
        count = int(slot["count"])
        slot["avg_pnl_pct"] = round(float(slot["sum_pnl_pct"]) / count, 4) if count else 0.0
        slot["sum_pnl_pct"] = round(float(slot["sum_pnl_pct"]), 4)
    return buckets


def build_proposals(
    digest: dict[str, Any],
    *,
    min_strategy_round_trips: int | None = None,
) -> list[dict[str, Any]]:
    """Propose research hypotheses / knobs. Never marks actionable under the gate."""
    min_trips = (
        min_strategy_round_trips
        if min_strategy_round_trips is not None
        else int(digest.get("min_strategy_round_trips_for_rules") or MIN_STRATEGY_ROUND_TRIPS_FOR_RULES)
    )
    enough = bool(digest.get("enough_data_for_rule_changes") or digest.get("enough_data"))
    strategy_trips = int(
        (digest.get("strategy_vs_manual_test") or {}).get("strategy_round_trips")
        or (digest.get("realized_pnl") or {}).get("strategy_round_trip_count")
        or 0
    )
    remaining = max(0, min_trips - strategy_trips)
    gate_note = (
        None
        if enough
        else f"blocked until {remaining} more strategy round-trip(s) "
        f"(have {strategy_trips}, need ≥{min_trips})"
    )

    counts = digest.get("counts") or {}
    by_status = counts.get("by_status") or {}
    by_alert = counts.get("by_alert_type") or {}
    by_regime = counts.get("by_regime") or {}
    row_count = int(digest.get("row_count") or 0)
    dd = digest.get("drawdown_comparison") or {}
    trips = list((digest.get("realized_pnl") or {}).get("round_trips") or [])
    regime_pnl = _regime_pnl_buckets(trips)
    conf_hint = _confidence_outcome_hint(trips)
    hold_pnl = _hold_pnl_buckets(trips)

    proposals: list[dict[str, Any]] = []

    def _add(
        *,
        proposal_id: str,
        title: str,
        hypothesis: str,
        proposed_knobs: dict[str, Any],
        confidence: str,
        evidence: str,
        validation: str,
    ) -> None:
        proposals.append(
            {
                "id": proposal_id,
                "title": title,
                "hypothesis": hypothesis,
                "proposed_knobs": proposed_knobs,
                "confidence": confidence,
                "evidence": evidence,
                "validation_required": validation,
                "actionable": enough,
                "blocked_reason": gate_note,
                "auto_apply": False,
            }
        )

    # Always surface IEX deeper-cut priors (learning only; never actionable).
    _add(
        proposal_id="research_prior_iex_deeper_cuts",
        title="IEX pre-seal priors: range / short holds / low confidence",
        hypothesis=(
            "Offline IEX deeper cuts (2020→pre-seal, frozen weights) showed range "
            "regimes, holds ≤10d, and low exit-row confidence as the weak slices. "
            "Watch paper fills for the same patterns; do not retune "
            "DEFAULT_SCORE_WEIGHTS or reopen sealed OOS."
        ),
        proposed_knobs={
            "research_flags": [
                "soft_gate_range_confidence_add",
                "min_confidence_to_trade",
                "flip_min_hold_trading_days",
            ],
            "watch_slices": ["range", "hold_le_10d", "confidence_lt_62_exit_row"],
            "note": "Priors only. Run scripts/learning_deeper_cuts.py for a fresh offline cut.",
        },
        confidence="medium",
        evidence=(
            "IEX deeper cuts: range ~11% wins / large in-group drag; holds ≤10d mostly "
            "losers; exit-row conf <62 weak vs 80+. TQQQ-only counterfactual is diagnostic only."
        ),
        validation=(
            "Keep Path B paper soak. After the trip gate, test only via "
            "`--checklist-compare` / `--strategy-eval`. Never auto-apply; never reopen sealed OOS."
        ),
    )

    # Always include a meta proposal about the gate / process.
    if not enough:
        _add(
            proposal_id="accumulate_strategy_round_trips",
            title="Keep weekday paper sessions running",
            hypothesis=(
                "Strategy round-trips are below the learning gate; rule/checklist changes "
                "would be noise-driven until the sample grows."
            ),
            proposed_knobs={},
            confidence="high",
            evidence=(
                f"strategy_round_trips={strategy_trips}, "
                f"min_required={min_trips}, row_count={row_count}"
            ),
            validation=(
                "No knob change. Continue paper cron / `scripts/run_weekday_paper.py`; "
                "re-run `--learn-from-trades` after more strategy fills."
            ),
        )
    else:
        _add(
            proposal_id="gate_cleared_research_only",
            title="Gate cleared — research before any adopt",
            h