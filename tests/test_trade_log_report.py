"""Unit tests for trade_log_report bucketing / P&L / DD (no network)."""

from __future__ import annotations

import json
from pathlib import Path

from trade_log import append_trade_record, build_trade_record
from trade_log_report import (
    build_trade_log_report,
    confidence_bucket,
    format_trade_log_report_summary,
    infer_round_trips,
    run_trade_log_report,
)


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


def test_confidence_bucket():
    assert confidence_bucket(None) == "unknown"
    assert confidence_bucket("x") == "unknown"
    assert confidence_bucket(10) == "0-24"
    assert confidence_bucket(25) == "25-49"
    assert confidence_bucket(49.9) == "25-49"
    assert confidence_bucket(50) == "50-74"
    assert confidence_bucket(80) == "75-100"
    assert confidence_bucket(100) == "75-100"


def test_build_report_buckets_from_synthetic_jsonl(tmp_path: Path):
    path = tmp_path / "trades.jsonl"
    rows = [
        _row(
            side="buy",
            status="filled",
            source="strategy",
            alert_type="BUY",
            regime="bull",
            confidence=80,
            fill_price="100",
            timestamp="2026-09-01T10:00:00Z",
        ),
        _row(
            side="sell",
            status="filled",
            source="strategy",
            alert_type="SELL",
            regime="bull",
            confidence=70,
            fill_price="110",
            purpose="exit",
            timestamp="2026-09-02T10:00:00Z",
        ),
        _row(
            side="buy",
            status="skipped",
            source="strategy",
            alert_type="BUY",
            regime="range",
            confidence=40,
            fill_price=None,
            filled_qty=None,
            qty="0",
            purpose="entry",
            error="no_quote",
            timestamp="2026-09-03T10:00:00Z",
        ),
        _row(
            side="none",
            status="skipped",
            source="strategy",
            alert_type="CASH",
            regime="trend_down",
            confidence=22,
            fill_price=None,
            filled_qty=None,
            qty=None,
            purpose="non_actionable",
            error="non_actionable",
            timestamp="2026-09-04T10:00:00Z",
        ),
        _row(
            side="buy",
            status="filled",
            source="manual_test",
            alert_type="BUY",
            regime=None,
            confidence=None,
            fill_price="50",
            timestamp="2026-09-05T10:00:00Z",
        ),
        _row(
            side="sell",
            status="filled",
            source="manual_test",
            alert_type="SELL",
            fill_price="45",
            purpose="exit",
            timestamp="2026-09-05T11:00:00Z",
        ),
    ]
    for row in rows:
        append_trade_record(path, row)

    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    report = build_trade_log_report(records, research_max_dd_pct=19.06, source_path=str(path))

    assert report["row_count"] == 6
    assert report["counts"]["by_status"] == {"filled": 4, "skipped": 2}
    assert report["counts"]["by_source"]["strategy"] == 4
    assert report["counts"]["by_source"]["manual_test"] == 2
    assert report["counts"]["by_alert_type"]["BUY"] == 3  # 1 filled strategy + 1 skipped + 1 manual
    assert report["counts"]["by_alert_type"]["SELL"] == 2
    assert report["counts"]["by_alert_type"]["CASH"] == 1
    assert report["counts"]["by_regime"]["bull"] == 2
    assert report["counts"]["by_regime"]["trend_down"] == 1
    assert report["counts"]["by_confidence_bucket"]["75-100"] == 1
    assert report["counts"]["by_confidence_bucket"]["50-74"] == 1
    assert report["counts"]["by_confidence_bucket"]["25-49"] == 1
    assert report["counts"]["by_confidence_bucket"]["0-24"] == 1
    assert report["counts"]["by_confidence_bucket"]["unknown"] == 2

    trips = report["realized_pnl"]["round_trips"]
    assert report["realized_pnl"]["round_trip_count"] == 2
    assert report["realized_pnl"]["strategy_round_trip_count"] == 1
    assert abs(trips[0]["pnl_pct"] - 10.0) < 1e-6
    assert abs(trips[1]["pnl_pct"] - (-10.0)) < 1e-6
    # Only one strategy round-trip → not enough for rule changes
    assert report["enough_data_for_rule_changes"] is False
    assert any("not enough data" in m for m in report["messages"])

    summary = format_trade_log_report_summary(report)
    assert "By status:" in summary
    assert "not enough data" in summary


def test_manual_only_message(tmp_path: Path):
    path = tmp_path / "manual.jsonl"
    append_trade_record(
        path,
        _row(source="manual_test", side="buy", fill_price="10", timestamp="2026-01-01T00:00:00Z"),
    )
    append_trade_record(
        path,
        _row(
            source="manual_test",
            side="sell",
            fill_price="11",
            purpose="exit",
            timestamp="2026-01-02T00:00:00Z",
        ),
    )
    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    report = build_trade_log_report(records)
    assert report["enough_data_for_rule_changes"] is False
    assert any("only manual test fills" in m for m in report["messages"])


def test_infer_round_trips_empty_and_unmatched():
    assert infer_round_trips([]) == []
    assert (
        infer_round_trips(
            [
                _row(side="buy", status="accepted", fill_price=None),
                _row(side="sell", status="skipped", fill_price=None),
            ]
        )
        == []
    )


def test_run_trade_log_report_writes_json(tmp_path: Path):
    trade_path = tmp_path / "trades.jsonl"
    out_path = tmp_path / "report.json"
    append_trade_record(
        trade_path,
        _row(
            source="manual_test",
            side="buy",
            fill_price="78.64",
            alert_type="BUY",
            timestamp="2026-09-30T01:11:00Z",
        ),
    )
    append_trade_record(
        trade_path,
        _row(
            source="manual_test",
            side="sell",
            fill_price="77.08",
            alert_type="SELL",
            purpose="exit",
            timestamp="2026-09-30T01:11:30Z",
        ),
    )
    payload = run_trade_log_report(
        trade_log_path=trade_path,
        report_path=out_path,
        research_max_dd_pct=19.06,
    )
    assert out_path.exists()
    loaded = json.loads(out_path.read_text(encoding="utf-8"))
    assert loaded["row_count"] == 2
    assert payload["messages"]
    assert "only manual test fills" in payload["messages"][0]
