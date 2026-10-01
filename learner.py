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
            hypothesis=(
                "Enough strategy round-trips exist to form hypotheses, but adoption still "
                "requires research validation (strategy-eval / WFE)."
            ),
            proposed_knobs={},
            confidence="high",
            evidence=f"strategy_round_trips={strategy_trips} ≥ {min_trips}",
            validation=VALIDATION_REMINDER,
        )

    cash_skips = int(by_alert.get("CASH") or 0)
    skipped = int(by_status.get("skipped") or 0)
    if row_count > 0 and (cash_skips / row_count) >= 0.4:
        _add(
            proposal_id="high_cash_skip_rate",
            title="Investigate high CASH / skip rate",
            hypothesis=(
                "Many non-actionable CASH breadcrumbs suggest the checklist rarely clears "
                "entry bars in the sampled regimes — tighten or retarget research knobs "
                "via checklist-compare, not live weight edits."
            ),
            proposed_knobs={
                "research_flags": [
                    "soft_gate_range_confidence_add",
                    "entry_dominance_gap_weight",
                    "min_confidence_to_trade",
                ],
                "env_candidates": ["MIN_CONFIDENCE_TO_TRADE"],
                "note": "Propose values only after --checklist-compare / --strategy-eval.",
            },
            confidence="medium" if enough else "low",
            evidence=f"CASH alerts={cash_skips}, skipped status={skipped}, rows={row_count}",
            validation="python main.py --checklist-compare && python main.py --strategy-eval --small-grid",
        )

    range_rows = int(by_regime.get("range") or 0) + int(by_regime.get("chop") or 0)
    range_bucket = regime_pnl.get("range") or regime_pnl.get("chop")
    if range_bucket and int(range_bucket.get("count") or 0) >= 2 and float(range_bucket.get("sum_pnl_pct") or 0) < 0:
        _add(
            proposal_id="range_regime_drag",
            title="Range-regime paper P&L drag",
            hypothesis=(
                "Closed strategy trips tagged range/chop show negative summed P&L. "
                "Research soft range gates or entry filters — prior hard block_range "
                "already failed on the research window; do not auto-enable."
            ),
            proposed_knobs={
                "research_flags": [
                    "soft_gate_range_confidence_add",
                    "soft_gate_range_score_margin",
                    "block_range_entries",
                ],
                "defaults_must_stay_off": True,
            },
            confidence="medium" if enough else "low",
            evidence=(
                f"range/chop rows≈{range_rows}; strategy trips in range bucket="
                f"{range_bucket}"
            ),
            validation="python main.py --checklist-compare (reject unless net%, max DD, and WFE all improve)",
        )

    paper_dd = float(dd.get("paper_max_drawdown_pct") or 0.0)
    research_dd = float(dd.get("research_max_dd_pct") or DEFAULT_RESEARCH_MAX_DD_PCT)
    ratio = dd.get("paper_dd_vs_research_ratio")
    if ratio is not None and float(ratio) > 1.25 and strategy_trips >= 3:
        _add(
            proposal_id="paper_dd_above_research_band",
            title="Paper fill DD above research ~19% band",
            hypothesis=(
                "Inferred paper fill drawdown exceeds the research max-DD reference. "
                "Consider smaller paper notional / vol sizing — not looser stops — and "
                "re-check strategy-eval before any exit-knob change."
            ),
            proposed_knobs={
                "env_candidates": [
                    "ALPACA_PAPER_NOTIONAL",
                    "ALPACA_PAPER_EQUITY_PCT",
                    "ALPACA_PAPER_VOL_SIZING",
                    "ALPACA_PAPER_RISK_FRACTION",
                ],
                "avoid": ["widening STOP_LOSS_PCT to 'make it back'"],
            },
            confidence="medium" if enough else "low",
            evidence=(
                f"paper_max_drawdown_pct={paper_dd}, research_max_dd_pct={research_dd}, "
                f"ratio={ratio}"
            ),
            validation="python main.py --strategy-eval; compare paper_gate.required_paper_capital",
        )

    if conf_hint and conf_hint.get("low_conf_count", 0) >= 2:
        low_avg = conf_hint.get("low_conf_avg_pnl_pct")
        high_avg = conf_hint.get("high_conf_avg_pnl_pct")
        if low_avg is not None and (high_avg is None or float(low_avg) < float(high_avg or 0) - 1.0):
            _add(
                proposal_id="raise_min_confidence_hypothesis",
                title="Low-confidence entries underperform (hypothesis)",
                hypothesis=(
                    "Strategy round-trips with entry confidence <50 look weaker than "
                    "high-confidence (≥75) trips. Test a higher MIN_CONFIDENCE_TO_TRADE "
                    "in research only."
                ),
                proposed_knobs={
                    "env_candidates": ["MIN_CONFIDENCE_TO_TRADE"],
                    "research_flags": ["min_confidence_to_trade", "soft_gate_disagree_confidence_add"],
                },
                confidence="low",
                evidence=str(conf_hint),
                validation="python main.py --strategy-eval --small-grid (plateau + WFE must hold)",
            )

    errors = int(by_status.get("error") or 0) + int(by_status.get("rejected") or 0) + int(by_status.get("refused") or 0)
    if errors > 0:
        _add(
            proposal_id="ops_errors_in_journal",
            title="Broker/ops errors in trade journal",
            hypothesis=(
                "Error/rejected/refused rows are operational, not checklist edges. "
                "Fix keys/host/latches; do not change score weights."
            ),
            proposed_knobs={},
            confidence="high",
            evidence=f"error+rejected+refused={errors}; by_status={by_status}",
            validation="Inspect logs; confirm ALPACA_PAPER_* latches and paper host only.",
        )

    # Ensure every proposal carries the blocked reason when under gate.
    for proposal in proposals:
        if not enough:
            proposal["actionable"] = False
            proposal["blocked_reason"] = gate_note
        proposal["auto_apply"] = False

    return proposals


def format_learner_summary(digest: dict[str, Any], proposals: list[dict[str, Any]]) -> str:
    counts = digest.get("counts") or {}
    svm = digest.get("strategy_vs_manual_test") or {}
    dd = digest.get("drawdown_comparison") or {}
    actionable = sum(1 for p in proposals if p.get("actionable"))
    blocked = len(proposals) - actionable
    lines = [
        "Learner digest (read-only)",
        f"  Source: {digest.get('source_path')}",
        f"  Rows: {digest.get('row_count', 0)}",
        f"  By status: {counts.get('by_status') or {}}",
        f"  By source: {counts.get('by_source') or {}}",
        f"  By alert_type: {counts.get('by_alert_type') or {}}",
        f"  By regime: {counts.get('by_regime') or {}}",
        f"  By confidence: {counts.get('by_confidence_bucket') or {}}",
        (
            f"  Strategy vs manual_test rows: "
            f"{svm.get('strategy_rows', 0)} / {svm.get('manual_test_rows', 0)}"
        ),
        (
            f"  Strategy round-trips: {svm.get('strategy_round_trips', 0)} "
            f"(manual_test={svm.get('manual_test_round_trips', 0)})"
        ),
        (
            f"  Paper max DD (fills): {dd.get('paper_max_drawdown_pct')}% "
            f"vs research ~{dd.get('research_max_dd_pct')}% "
            f"(ratio {dd.get('paper_dd_vs_research_ratio')})"
        ),
        f"  Enough data (gate): {digest.get('enough_data')}",
        f"  Proposals: {len(proposals)} (actionable={actionable}, blocked={blocked})",
        f"  Auto-apply: never ({VALIDATION_REMINDER})",
    ]
    for msg in digest.get("messages") or []:
        lines.append(f"  ! {msg}")
    for proposal in proposals[:5]:
        flag = "ACTIONABLE" if proposal.get("actionable") else "BLOCKED"
        lines.append(f"  - [{flag}] {proposal.get('id')}: {proposal.get('title')}")
    return "\n".join(lines)


def format_proposals_markdown(digest: dict[str, Any], proposals: list[dict[str, Any]]) -> str:
    svm = digest.get("strategy_vs_manual_test") or {}
    enough = bool(digest.get("enough_data"))
    min_trips = digest.get("min_strategy_round_trips_for_rules")
    lines = [
        "# Learner proposals (read-only)",
        "",
        f"Generated: `{digest.get('generated_at')}`",
        f"Source: `{digest.get('source_path')}`",
        f"Strategy round-trips: **{svm.get('strategy_round_trips', 0)}** "
        f"(gate ≥{min_trips}; enough_data={enough})",
        "",
        f"**{VALIDATION_REMINDER}**",
        "",
    ]
    if not proposals:
        lines.append("_No proposals generated._")
        return "\n".join(lines) + "\n"

    for proposal in proposals:
        status = "actionable" if proposal.get("actionable") else "blocked"
        lines.extend(
            [
                f"## {proposal.get('id')} — {proposal.get('title')}",
                "",
                f"- Status: **{status}**",
                f"- Confidence: `{proposal.get('confidence')}`",
                f"- Auto-apply: `{proposal.get('auto_apply')}`",
            ]
        )
        if proposal.get("blocked_reason"):
            lines.append(f"- Gate: {proposal['blocked_reason']}")
        lines.extend(
            [
                f"- Hypothesis: {proposal.get('hypothesis')}",
                f"- Evidence: {proposal.get('evidence')}",
                f"- Proposed knobs: `{json.dumps(proposal.get('proposed_knobs') or {}, sort_keys=True)}`",
                f"- Validation required: {proposal.get('validation_required')}",
                "",
            ]
        )
    return "\n".join(lines)


def write_json(path: str | Path, payload: dict[str, Any]) -> Path:
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return out


def write_text(path: str | Path, text: str) -> Path:
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    return out


def send_learner_discord_summary(
    webhook_url: str,
    summary: str,
    *,
    dry_run: bool = False,
    post: Callable[..., Any] | None = None,
) -> None:
    """Optional Discord post. Caller must pass ``--learn-discord``; default path skips this."""
    embed = {
        "title": "Learner digest (read-only)",
        "description": summary[:3900],
        "color": 0x2F6FED,
    }
    payload: dict[str, object] = {
        "username": "QQQ Swing Learner",
        "embeds": [embed],
    }
    if dry_run:
        print("[DRY RUN] Learner Discord payload:")
        print(json.dumps(payload, indent=2))
        return
    if not webhook_url:
        print("[Learner] No DISCORD_WEBHOOK_URL — skipping Discord summary.")
        return
    poster = post or requests.post
    response = poster(webhook_url, json=payload, timeout=15)
    if getattr(response, "status_code", 200) >= 400:
        raise RuntimeError(
            f"Discord webhook failed: {response.status_code} {getattr(response, 'text', '')}"
        )


def run_learn_from_trades(
    *,
    trade_log_path: str | Path = DEFAULT_TRADE_LOG_PATH,
    digest_path: str | Path = DEFAULT_DIGEST_PATH,
    proposals_path: str | Path = DEFAULT_PROPOSALS_PATH,
    research_max_dd_pct: float = DEFAULT_RESEARCH_MAX_DD_PCT,
    trade_log_store: Any | None = None,
    send_discord: bool = False,
    discord_webhook_url: str = "",
    discord_dry_run: bool = False,
    discord_post: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """Load trade log, write digest + proposals, optionally post Discord summary."""
    if trade_log_store is not None:
        records = trade_log_store.read_all()
        source_path = trade_log_store.describe()
    else:
        path = Path(trade_log_path)
        records = read_trade_records(path)
        source_path = str(path)

    digest = build_learner_digest(
        records,
        research_max_dd_pct=research_max_dd_pct,
        source_path=source_path,
    )
    proposals = build_proposals(digest)
    proposals_payload = {
        "generated_at": digest.get("generated_at"),
        "source_path": source_path,
        "enough_data": digest.get("enough_data"),
        "enough_data_for_rule_changes": digest.get("enough_data_for_rule_changes"),
        "min_strategy_round_trips_for_rules": digest.get("min_strategy_round_trips_for_rules"),
        "strategy_round_trips": (digest.get("strategy_vs_manual_test") or {}).get("strategy_round_trips"),
        "auto_apply": False,
        "validation_reminder": VALIDATION_REMINDER,
        "proposals": proposals,
    }

    write_json(digest_path, digest)
    # Sidecar JSON for tests/tools; markdown is the human-facing proposals file.
    write_json(Path(proposals_path).with_suffix(".json"), proposals_payload)
    write_text(proposals_path, format_proposals_markdown(digest, proposals))

    summary = format_learner_summary(digest, proposals)
    if send_discord:
        send_learner_discord_summary(
            discord_webhook_url,
            summary,
            dry_run=discord_dry_run,
            post=discord_post,
        )

    return {
        "digest": digest,
        "proposals": proposals,
        "proposals_payload": proposals_payload,
        "summary": summary,
        "digest_path": str(digest_path),
        "proposals_path": str(proposals_path),
    }
