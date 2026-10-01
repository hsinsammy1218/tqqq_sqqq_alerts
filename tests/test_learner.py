"""Unit tests for the read-only learner agent (no network, no orders)."""

from __future__ import annotations

import json
from pathlib import Path

from learner import (
    VALIDATION_REMINDER,
    build_learner_digest,
    build_proposals,
    format_learner_summary,
    format_proposals_markdown,
    run_learn_from_trades,
    send_learner_discord_summary,
)
from trade_log import append_trade_record, build_trade_record
from trade_log_report import MIN_STRATEGY_ROUND_TRIPS_FOR_RULES


def _row(**kwargs):
    base = dict(
        symbol="TQQQ",
        side="buy",
        status="filled",
        source="strategy",
        qty="1",
        fill_price="100",
        filled_qty="1",
        paper_trading=True,
        dry_run=False,
    )
    base.update(kwargs)
    return build_trade_record(**base)


def _strategy_round_trip(i: int, *, pnl_up: bool = True, regime: str = "bull", confidence: int = 80):
    entry = 100.0 + i
    exit_px = entry * (1.05 if pnl_up else 0.95)
    day = 1 + i
    return [
        _row(
            side="buy",
            status="filled",
            source="strategy",
            alert_type="BUY",
            regime=regime,
            confidence=confidence,
            fill_price=str(entry),
            timestamp=f"2026-09-{day:02d}T10:00:00Z",
            order_id=f"buy-{i}",
        ),
        _row(
            side="sell",
            status="filled",
            source="strategy",
            alert_type="SELL",
            regime=regime,
            confidence=confidence,
            fill_price=str(round(exit_px, 4)),
            purpose="exit",
            timestamp=f"2026-09-{day:02d}T15:00:00Z",
            order_id=f"sell-{i}",
        ),
    ]


def test_digest_buckets_and_enough_data_false(tmp_path: Path):
    path = tmp_path / "trades.jsonl"
    rows = [
        *_strategy_round_trip(1, pnl_up=True, regime="bull", confidence=80),
        _row(
            side="none",
            status="skipped",
            source="strategy",
            alert_type="CASH",
            regime="range",
            confidence=22,
            fill_price=None,
            filled_qty=None,
            qty=None,
            purpose="non_actionable",
            timestamp="2026-09-10T10:00:00Z",
        ),
        *_strategy_round_trip(2, pnl_up=False, regime="range", confidence=40),
        _row(
            side="buy",
            status="filled",
            source="manual_test",
            alert_type="BUY",
            fill_price="50",
            timestamp="2026-09-20T10:00:00Z",
        ),
        _row(
            side="sell",
            status="filled",
            source="manual_test",
            alert_type="SELL",
            fill_price="55",
            purpose="exit",
            timestamp="2026-09-20T11:00:00Z",
        ),
    ]
    for row in rows:
        append_trade_record(path, row)
    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]

    digest = build_learner_digest(records, research_max_dd_pct=19.06, source_path=str(path))

    assert digest["agent"] == "learner"
    assert digest["read_only"] is True
    assert digest["auto_apply"] is False
    assert digest["row_count"] == len(rows)
    assert digest["counts"]["by_source"]["strategy"] == 5
    assert digest["counts"]["by_source"]["manual_test"] == 2
    assert digest["counts"]["by_alert_type"]["CASH"] == 1
    assert digest["counts"]["by_regime"]["bull"] == 2
    assert digest["counts"]["by_confidence_bucket"]["75-100"] == 2
    assert digest["strategy_vs_manual_test"]["strategy_round_trips"] == 2
    assert digest["strategy_vs_manual_test"]["manual_test_round_trips"] == 1
    assert digest["enough_data"] is False
    assert digest["enough_data_for_rule_changes"] is False
    assert digest["strategy_round_trips_remaining"] == MIN_STRATEGY_ROUND_TRIPS_FOR_RULES - 2
    assert "paper_max_drawdown_pct" in digest["drawdown_comparison"]
    assert digest["drawdown_comparison"]["research_max_dd_pct"] == 19.06
    assert VALIDATION_REMINDER in digest["validation_reminder"]


def test_proposals_blocked_under_gate():
    trips = []
    for i in range(3):
        trips.extend(_strategy_round_trip(i + 1, pnl_up=(i % 2 == 0), regime="range", confidence=40))
    # Pad with CASH so high_cash_skip_rate can fire once we have enough rows.
    for i in range(8):
        trips.append(
            _row(
                side="none",
                status="skipped",
                source="strategy",
                alert_type="CASH",
                regime="range",
                confidence=20,
                fill_price=None,
                filled_qty=None,
                qty=None,
                purpose="non_actionable",
                timestamp=f"2026-08-{i + 1:02d}T12:00:00Z",
            )
        )
    digest = build_learner_digest(trips, research_max_dd_pct=19.06, source_path="mem")
    proposals = build_proposals(digest)

    assert proposals
    assert all(p["actionable"] is False for p in proposals)
    assert all(p["auto_apply"] is False for p in proposals)
    assert any(p["id"] == "accumulate_strategy_round_trips" for p in proposals)
    blocked = [p for p in proposals if p.get("blocked_reason")]
    assert blocked
    assert "blocked until" in blocked[0]["blocked_reason"]
    assert "strategy round-trip" in blocked[0]["blocked_reason"]


def test_proposals_actionable_when_gate_cleared():
    rows: list[dict] = []
    for i in range(MIN_STRATEGY_ROUND_TRIPS_FOR_RULES):
        # Alternate winners/losers; some range losers to trigger range_regime_drag.
        rows.extend(
            _strategy_round_trip(
                i + 1,
                pnl_up=(i % 3 != 0),
                regime="range" if i % 2 == 0 else "bull",
                confidence=40 if i % 2 == 0 else 80,
            )
        )
    digest = build_learner_digest(rows, research_max_dd_pct=5.0, source_path="mem")
    assert digest["enough_data"] is True
    proposals = build_proposals(digest)
    assert proposals
    assert any(p["id"] == "gate_cleared_research_only" for p in proposals)
    assert all(p["actionable"] is True for p in proposals if p["id"] != "ops_errors_in_journal")
    # DD ratio vs research 5% should be elevated with losers present.
    assert any("strategy-eval" in (p.get("validation_required") or "") for p in proposals)


def test_run_learn_from_trades_writes_outputs(tmp_path: Path):
    trade_path = tmp_path / "trades.jsonl"
    digest_path = tmp_path / "learner_digest.json"
    proposals_md = tmp_path / "learner_proposals.md"
    for row in _strategy_round_trip(1):
        append_trade_record(trade_path, row)

    posted: list[dict] = []

    class _Resp:
        status_code = 204
        text = ""

    def fake_post(url, json=None, timeout=15):  # noqa: A002
        posted.append({"url": url, "json": json, "timeout": timeout})
        return _Resp()

    result = run_learn_from_trades(
        trade_log_path=trade_path,
        digest_path=digest_path,
        proposals_path=proposals_md,
        research_max_dd_pct=19.06,
        send_discord=False,
    )
    assert digest_path.exists()
    assert proposals_md.exists()
    assert proposals_md.with_suffix(".json").exists()
    loaded = json.loads(digest_path.read_text(encoding="utf-8"))
    assert loaded["enough_data"] is False
    assert "Learner digest" in result["summary"]
    md = proposals_md.read_text(encoding="utf-8")
    assert "blocked" in md.lower()
    assert "strategy-eval" in md or "Do not auto-apply" in md
    assert posted == []

    # Discord only when explicitly requested.
    run_learn_from_trades(
        trade_log_path=trade_path,
        digest_path=digest_path,
        proposals_path=proposals_md,
        send_discord=True,
        discord_webhook_url="https://example.test/webhook",
        discord_post=fake_post,
    )
    assert len(posted) == 1
    assert posted[0]["url"] == "https://example.test/webhook"
    assert posted[0]["json"]["username"] == "QQQ Swing Learner"


def test_discord_helper_dry_run_no_post(capsys):
    calls: list[object] = []

    def boom(*_a, **_k):
        calls.append(1)
        raise AssertionError("should not post")

    send_learner_discord_summary("https://example.test/hook", "hello", dry_run=True, post=boom)
    assert calls == []
    out = capsys.readouterr().out
    assert "[DRY RUN] Learner Discord payload" in out


def test_format_helpers_include_gate_and_validation():
    digest = build_learner_digest([], source_path="empty")
    proposals = build_proposals(digest)
    summary = format_learner_summary(digest, proposals)
    md = format_proposals_markdown(digest, proposals)
    assert "Enough data (gate): False" in summary
    assert "Auto-apply: never" in summary
    assert "Learner proposals" in md
    assert VALIDATION_REMINDER.split(".", 1)[0] in md


def test_prefers_trade_log_store(tmp_path: Path):
    class FakeStore:
        def read_all(self):
            return [
                _row(
                    side="buy",
                    fill_price="10",
                    source="strategy",
                    alert_type="BUY",
                    timestamp="2026-01-01T00:00:00Z",
                ),
                _row(
                    side="sell",
                    fill_price="11",
                    source="strategy",
                    alert_type="SELL",
                    purpose="exit",
                    timestamp="2026-01-02T00:00:00Z",
                ),
            ]

        def describe(self):
            return "supabase:bot_trade_log:default"

    out_d = tmp_path / "d.json"
    out_p = tmp_path / "p.md"
    result = run_learn_from_trades(
        trade_log_path=tmp_path / "unused.jsonl",
        digest_path=out_d,
        proposals_path=out_p,
        trade_log_store=FakeStore(),
    )
    assert result["digest"]["source_path"] == "supabase:bot_trade_log:default"
    assert result["digest"]["strategy_vs_manual_test"]["strategy_round_trips"] == 1
