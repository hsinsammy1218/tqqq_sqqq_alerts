"""Summarize paper trade journal for self-learning.

Reads from Supabase ``bot_trade_log`` when ``TRADE_LOG_BACKEND=supabase``,
else from ``logs/trades.jsonl``. Writes ``reports/trade_log_report.json``.
"""

from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from trade_log import DEFAULT_TRADE_LOG_PATH, read_trade_records

DEFAULT_REPORT_PATH = Path("reports/trade_log_report.json")
# Baseline research max drawdown from checklist-adopted strategy_eval (~19%).
DEFAULT_RESEARCH_MAX_DD_PCT = 19.06
MIN_STRATEGY_ROUND_TRIPS_FOR_RULES = 10


def confidence_bucket(confidence: Any) -> str:
    """Bucket confidence into coarse bands for counting."""
    if confidence is None:
        return "unknown"
    try:
        value = float(confidence)
    except (TypeError, ValueError):
        return "unknown"
    if value < 0:
        return "unknown"
    if value < 25:
        return "0-24"
    if value < 50:
        return "25-49"
    if value < 75:
        return "50-74"
    if value <= 100:
        return "75-100"
    return "unknown"


def _count_by(records: list[dict[str, Any]], key: str) -> dict[str, int]:
    counter: Counter[str] = Counter()
    for row in records:
        raw = row.get(key)
        label = "unknown" if raw is None or raw == "" else str(raw)
        counter[label] += 1
    return dict(sorted(counter.items(), key=lambda item: (-item[1], item[0])))


def _count_confidence_buckets(records: list[dict[str, Any]]) -> dict[str, int]:
    counter: Counter[str] = Counter(confidence_bucket(row.get("confidence")) for row in records)
    order = ("0-24", "25-49", "50-74", "75-100", "unknown")
    return {name: int(counter.get(name, 0)) for name in order if counter.get(name, 0)}


def _to_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _is_filled(row: dict[str, Any]) -> bool:
    status = str(row.get("status") or "").lower()
    if status != "filled":
        return False
    price = _to_float(row.get("fill_price"))
    qty = _to_float(row.get("filled_qty") if row.get("filled_qty") is not None else row.get("qty"))
    return price is not None and price > 0 and qty is not None and qty > 0


def infer_round_trips(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """FIFO match buy→sell fills per symbol; return realized P&L when inferable."""
    open_lots: dict[str, list[dict[str, Any]]] = {}
    trips: list[dict[str, Any]] = []

    for row in records:
        if not _is_filled(row):
            continue
        symbol = str(row.get("symbol") or "").upper()
        side = str(row.get("side") or "").lower()
        price = _to_float(row.get("fill_price"))
        qty = _to_float(row.get("filled_qty") if row.get("filled_qty") is not None else row.get("qty"))
        if not symbol or price is None or qty is None:
            continue
        lot = {
            "timestamp": row.get("timestamp"),
            "price": price,
            "qty": qty,
            "source": row.get("source"),
            "alert_type": row.get("alert_type"),
            "regime": row.get("regime"),
            "confidence": row.get("confidence"),
            "order_id": row.get("order_id"),
        }
        if side == "buy":
            open_lots.setdefault(symbol, []).append(lot)
            continue
        if side != "sell":
            continue
        queue = open_lots.get(symbol) or []
        remaining = qty
        while remaining > 1e-12 and queue:
            entry = queue[0]
            take = min(remaining, float(entry["qty"]))
            entry_price = float(entry["price"])
            pnl_pct = ((price - entry_price) / entry_price) * 100.0 if entry_price else None
            pnl_abs = (price - entry_price) * take
            trips.append(
                {
                    "symbol": symbol,
                    "qty": take,
                    "entry_price": entry_price,
                    "exit_price": price,
                    "pnl_pct": None if pnl_pct is None else round(pnl_pct, 6),
                    "pnl_abs": round(pnl_abs, 6),
                    "entry_timestamp": entry.get("timestamp"),
                    "exit_timestamp": row.get("timestamp"),
                    "entry_source": entry.get("source"),
                    "exit_source": row.get("source"),
                    "regime": entry.get("regime") or row.get("regime"),
                    "confidence": entry.get("confidence"),
                }
            )
            entry["qty"] = float(entry["qty"]) - take
            remaining -= take
            if float(entry["qty"]) <= 1e-12:
                queue.pop(0)
        open_lots[symbol] = queue

    return trips


def _max_drawdown_pct_from_returns(return_pcts: list[float]) -> float:
    """Equity curve starting at 1.0; returns are percent per closed trip."""
    if not return_pcts:
        return 0.0
    equity = 1.0
    peak = 1.0
    max_dd = 0.0
    for ret in return_pcts:
        equity *= 1.0 + (ret / 100.0)
        if equity > peak:
            peak = equity
        if peak > 0:
            dd = (peak - equity) / peak * 100.0
            if dd > max_dd:
                max_dd = dd
    return round(max_dd, 6)


def build_trade_log_report(
    records: list[dict[str, Any]],
    *,
    research_max_dd_pct: float = DEFAULT_RESEARCH_MAX_DD_PCT,
    min_strategy_round_trips: int = MIN_STRATEGY_ROUND_TRIPS_FOR_RULES,
    source_path: str | None = None,
) -> dict[str, Any]:
    """Build a JSON-serializable summary of trade-log rows."""
    trips = infer_round_trips(records)
    strategy_trips = [
        t
        for t in trips
        if str(t.get("entry_source") or "") == "strategy" or str(t.get("exit_source") or "") == "strategy"
    ]
    manual_trips = [
        t
        for t in trips
        if str(t.get("entry_source") or "") == "manual_test"
        and str(t.get("exit_source") or "") == "manual_test"
    ]
    strategy_rows = [r for r in records if str(r.get("source") or "") == "strategy"]
    manual_rows = [r for r in records if str(r.get("source") or "") == "manual_test"]

    realized_pnl_pcts = [float(t["pnl_pct"]) for t in trips if t.get("pnl_pct") is not None]
    strategy_pnl_pcts = [float(t["pnl_pct"]) for t in strategy_trips if t.get("pnl_pct") is not None]
    paper_dd = _max_drawdown_pct_from_returns(strategy_pnl_pcts if strategy_pnl_pcts else realized_pnl_pcts)
    total_realized_pnl_abs = round(sum(float(t["pnl_abs"]) for t in trips if t.get("pnl_abs") is not None), 6)
    total_realized_pnl_pct_sum = round(sum(realized_pnl_pcts), 6) if realized_pnl_pcts else None

    only_manual = bool(records) and not strategy_rows and bool(manual_rows)
    enough_for_rules = len(strategy_trips) >= min_strategy_round_trips

    messages: list[str] = []
    if not records:
        messages.append("not enough data: trade log is empty")
    elif only_manual:
        messages.append(
            "not enough data: only manual test fills exist — need strategy paper sessions before rule changes"
        )
    elif not strategy_trips:
        messages.append(
            "not enough data: no strategy round-trip fills yet (skips/orders without matched buy→sell cannot yield P&L)"
        )
    elif not enough_for_rules:
        messages.append(
            f"not enough data: {len(strategy_trips)} strategy round-trip(s); "
            f"need ≥{min_strategy_round_trips} before considering rule changes"
        )

    dd_vs_research: dict[str, Any] = {
        "paper_max_drawdown_pct": paper_dd,
        "research_max_dd_pct": research_max_dd_pct,
        "paper_dd_vs_research_ratio": (
            round(paper_dd / research_max_dd_pct, 4) if research_max_dd_pct > 0 else None
        ),
        "note": (
            "Paper DD is inferred from closed fill round-trips only (not mark-to-market equity). "
            f"Research reference max DD ≈ {research_max_dd_pct}% from strategy_eval."
        ),
    }

    return {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source_path": source_path,
        "row_count": len(records),
        "counts": {
            "by_status": _count_by(records, "status"),
            "by_source": _count_by(records, "source"),
            "by_alert_type": _count_by(records, "alert_type"),
            "by_regime": _count_by(records, "regime"),
            "by_confidence_bucket": _count_confidence_buckets(records),
        },
        "realized_pnl": {
            "round_trip_count": len(trips),
            "strategy_round_trip_count": len(strategy_trips),
            "manual_test_round_trip_count": len(manual_trips),
            "total_pnl_abs": total_realized_pnl_abs,
            "sum_pnl_pct": total_realized_pnl_pct_sum,
            "round_trips": trips,
        },
        "drawdown_comparison": dd_vs_research,
        "enough_data_for_rule_changes": enough_for_rules,
        "min_strategy_round_trips_for_rules": min_strategy_round_trips,
        "messages": messages,
    }


def format_trade_log_report_summary(payload: dict[str, Any]) -> str:
    counts = payload.get("counts") or {}
    pnl = payload.get("realized_pnl") or {}
    dd = payload.get("drawdown_comparison") or {}
    lines = [
        "Trade log report",
        f"  Source: {payload.get('source_path')}",
        f"  Rows: {payload.get('row_count', 0)}",
        f"  By status: {counts.get('by_status') or {}}",
        f"  By source: {counts.get('by_source') or {}}",
        f"  By alert_type: {counts.get('by_alert_type') or {}}",
        f"  By regime: {counts.get('by_regime') or {}}",
        f"  By confidence: {counts.get('by_confidence_bucket') or {}}",
        (
            f"  Realized round-trips: {pnl.get('round_trip_count', 0)} "
            f"(strategy={pnl.get('strategy_round_trip_count', 0)}, "
            f"manual_test={pnl.get('manual_test_round_trip_count', 0)})"
        ),
        f"  Sum P&L % (closed trips): {pnl.get('sum_pnl_pct')}",
        (
            f"  Paper max DD (from fills): {dd.get('paper_max_drawdown_pct')}% "
            f"vs research ~{dd.get('research_max_dd_pct')}% "
            f"(ratio {dd.get('paper_dd_vs_research_ratio')})"
        ),
        f"  Enough data for rule changes: {payload.get('enough_data_for_rule_changes')}",
    ]
    for msg in payload.get("messages") or []:
        lines.append(f"  ! {msg}")
    return "\n".join(lines)


def write_trade_log_report(path: str | Path, payload: dict[str, Any]) -> Path:
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return out


def run_trade_log_report(
    *,
    trade_log_path: str | Path = DEFAULT_TRADE_LOG_PATH,
    report_path: str | Path = DEFAULT_REPORT_PATH,
    research_max_dd_pct: float = DEFAULT_RESEARCH_MAX_DD_PCT,
    trade_log_store: Any | None = None,
) -> dict[str, Any]:
    if trade_log_store is not None:
        records = trade_log_store.read_all()
        source_path = trade_log_store.describe()
    else:
        path = Path(trade_log_path)
        records = read_trade_records(path)
        source_path = str(path)
    payload = build_trade_log_report(
        records,
        research_max_dd_pct=research_max_dd_pct,
        source_path=source_path,
    )
    write_trade_log_report(report_path, payload)
    return payload


def load_research_max_dd_pct(
    strategy_eval_path: str | Path = Path("reports/strategy_eval.json"),
    default: float = DEFAULT_RESEARCH_MAX_DD_PCT,
) -> float:
    """Prefer baseline max_drawdown_pct from strategy_eval.json when present."""
    path = Path(strategy_eval_path)
    if not path.exists():
        return default
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        baseline = data.get("baseline") or {}
        metrics = baseline.get("metrics") or baseline
        value = metrics.get("max_drawdown_pct")
        if value is None:
            return default
        return round(float(value), 2)
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return default
