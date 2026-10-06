from __future__ import annotations

import sys

from rh_agent import build_rh_playbook
from strategy_types import AlertDecision, PositionState


def _alert(**kwargs: object) -> AlertDecision:
    defaults = dict(
        alert_type="BUY",
        symbol="TQQQ",
        qqq_trend_reason="regime=trend_up",
        bullish_score=80,
        bearish_score=20,
        confidence_score=78,
        entry_zone_low=400.0,
        entry_zone_high=410.0,
        stop_loss=370.0,
        take_profit=460.0,
        stretch_take_profit=500.0,
        max_hold_date="2026-06-01",
        timestamp="2026-10-04T15:00:00Z",
        notes="Bullish setup.",
        notes_kind="buy_bull",
    )
    defaults.update(kwargs)
    return AlertDecision(**defaults)  # type: ignore[arg-type]


def test_cash_is_no_order_and_never_places():
    playbook = build_rh_playbook(_alert(alert_type="CASH", symbol="CASH", notes_kind="other"))
    assert playbook["status"] == "no_order"
    assert playbook["allow_place"] is False
    assert playbook["withheld_place_steps"] == []
    tools = [step["tool"] for step in playbook["steps"]]
    assert "place_equity_order" not in tools
    assert "review_equity_order" not in tools
    assert "get_equity_positions" in tools


def test_buy_tqqq_is_review_only_by_default():
    playbook = build_rh_playbook(_alert(), max_notional_usd=200)
    assert playbook["status"] == "ready_to_review"
    reviews = [s for s in playbook["steps"] if s["tool"] == "review_equity_order"]
    assert len(reviews) == 1
    assert reviews[0]["arguments"]["symbol"] == "TQQQ"
    assert reviews[0]["arguments"]["side"] == "buy"
    assert reviews[0]["arguments"]["notional_usd"] == 200
    assert playbook["withheld_place_steps"][0]["tool"] == "place_equity_order"
    assert all(s["tool"] != "place_equity_order" for s in playbook["steps"])


def test_sqqq_buy_blocked_when_tqqq_only():
    playbook = build_rh_playbook(_alert(symbol="SQQQ", notes_kind="buy_bear"))
    assert playbook["status"] == "blocked"
    assert "SQQQ" in playbook["blocked"][0]
    assert all(s["tool"] != "review_equity_order" for s in playbook["steps"])


def test_flip_reviews_exit_then_entry():
    playbook = build_rh_playbook(
        _alert(alert_type="FLIP", symbol="SQQQ", notes_kind="flip"),
        PositionState(active_symbol="TQQQ"),
        tqqq_only=False,
    )
    reviews = [s for s in playbook["steps"] if s["tool"] == "review_equity_order"]
    assert [r["arguments"]["side"] for r in reviews] == ["sell", "buy"]
    assert reviews[0]["arguments"]["symbol"] == "TQQQ"
    assert reviews[0]["arguments"]["sell_all"] is True
    assert reviews[1]["arguments"]["symbol"] == "SQQQ"
    assert len(playbook["withheld_place_steps"]) == 2


def test_cli_preview_runs_without_alpaca_keys(monkeypatch, capsys, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ALPACA_API_KEY", "")
    monkeypatch.setenv("ALPACA_API_SECRET", "")
    monkeypatch.setattr("sys.argv", ["main.py", "--rh-preview"])
    from main import run

    assert run() == 0
    out = capsys.readouterr().out
    assert "ready_to_review" in out
    assert "Config error" not in out
    assert "place_equity_order" not in out.split("withheld_place_steps")[0]


def test_allow_place_cannot_emit_a_live_order_tool():
    playbook = build_rh_playbook(_alert(), allow_place=True)
    tools = [s["tool"] for s in playbook["steps"]]
    assert "place_equity_order" not in tools
    assert playbook["withheld_place_steps"][0]["tool"] == "place_equity_order"
    assert playbook["allow_place"] is False
    assert any("allow_place ignored" in item for item in playbook["blocked"])
