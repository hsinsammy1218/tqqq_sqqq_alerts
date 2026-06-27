from __future__ import annotations

import data


def test_cli_call_counter_tracks_attempts(monkeypatch):
    class Proc:
        returncode = 0
        stdout = "[]"
        stderr = ""

    monkeypatch.setattr(data.subprocess, "run", lambda *args, **kwargs: Proc())
    data.reset_cli_call_count()
    data._run_ka_json("ka", ["prices", "-s", "QQQ"], "key")
    data._run_ka_json("ka", ["intraday", "-s", "QQQ"], "key")
    assert data.cli_calls_attempted() == 2
    assert data.format_cli_usage_line() == "[klickanalytics] 2 CLI calls attempted this run"


def test_format_cli_usage_line_with_reason_and_month():
    data.reset_cli_call_count()
    line = data.format_cli_usage_line(reason="market closed", month_total=120, month_limit=500)
    assert "market closed" in line
    assert "month 120/500" in line
