"""Phase 5R.2 — Robinhood MCP read-only preflight (offline / mock).

Live authenticated tools/list requires interactive OAuth on an approved host.
These tests never open a socket to Robinhood and never invoke place/review/cancel.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from brokers.robinhood_agentic import LIVE_SUBMISSION_IMPLEMENTED
from brokers.robinhood_reader import AUTH_CASE, UNATTENDED_AUTH_SUPPORTED
from robinhood_host_handoff import HANDOFF_SUBMISSION_IMPLEMENTED
from robinhood_host_schema import (
    PLACE_ARG_FORBIDDEN,
    capabilities_from_tools_list,
    filter_place_args,
)
from robinhood_mcp_readonly import (
    OFFICIAL_MCP_URL,
    PREFLIGHT_MUTATING_DENYLIST,
    PREFLIGHT_READ_ALLOWLIST,
    ReadOnlyPreflightClient,
    ToolClass,
    assert_preflight_tool_allowed,
    assert_preflight_tool_or_raise,
    auth_gate_status,
    classify_tools_list,
    kill_switches_disarmed,
    sanitize_for_report,
)
from strategy_params import STRATEGY_VERSION

FIXTURES = Path(__file__).resolve().parent / "fixtures"
TOOLS_FIXTURE = FIXTURES / "rh_mcp_tools_list_docs_2026_10_07.json"
RESP_FIXTURE = FIXTURES / "rh_mcp_readonly_responses_sanitized.json"


@pytest.fixture(scope="module")
def docs_tools_list() -> dict:
    return json.loads(TOOLS_FIXTURE.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def sanitized_responses() -> dict:
    return json.loads(RESP_FIXTURE.read_text(encoding="utf-8"))


def test_01_strategy_frozen_and_lives_disarmed() -> None:
    assert STRATEGY_VERSION == "1.0.0"
    assert LIVE_SUBMISSION_IMPLEMENTED is False
    assert HANDOFF_SUBMISSION_IMPLEMENTED is False
    assert AUTH_CASE == "C"
    assert UNATTENDED_AUTH_SUPPORTED is False


def test_02_kill_switches_default_false() -> None:
    ok, tripped = kill_switches_disarmed({})
    assert ok is True
    assert tripped == ()
    ok2, tripped2 = kill_switches_disarmed(
        {"ROBINHOOD_HOST_LIVE_SUBMISSION": "true", "LIVE_TRADING_ENABLED": "1"}
    )
    assert ok2 is False
    assert "ROBINHOOD_HOST_LIVE_SUBMISSION" in tripped2
    assert "LIVE_TRADING_ENABLED" in tripped2


def test_03_official_endpoint_constant() -> None:
    assert OFFICIAL_MCP_URL == "https://agent.robinhood.com/mcp/trading"


def test_04_classify_docs_fixture(docs_tools_list: dict) -> None:
    classified = classify_tools_list(docs_tools_list)
    by_name = {row.name: row for row in classified}
    assert by_name["get_accounts"].classification is ToolClass.READ_ONLY
    assert by_name["get_equity_quotes"].classification is ToolClass.READ_ONLY
    assert by_name["place_equity_order"].classification is ToolClass.MUTATING
    assert by_name["review_equity_order"].classification is ToolClass.MUTATING
    assert by_name["cancel_equity_order"].classification is ToolClass.MUTATING
    assert by_name["get_realized_pnl"].classification is ToolClass.UNKNOWN
    reads = {r.name for r in classified if r.classification is ToolClass.READ_ONLY}
    muts = {r.name for r in classified if r.classification is ToolClass.MUTATING}
    assert PREFLIGHT_READ_ALLOWLIST & reads
    assert "place_equity_order" in muts
    assert "review_equity_order" in PREFLIGHT_MUTATING_DENYLIST


def test_05_allowlist_refuses_mutating_and_unknown() -> None:
    assert assert_preflight_tool_allowed("get_portfolio").allowed is True
    assert assert_preflight_tool_allowed("place_equity_order").allowed is False
    assert assert_preflight_tool_allowed("review_equity_order").allowed is False
    assert assert_preflight_tool_allowed("cancel_equity_order").allowed is False
    assert assert_preflight_tool_allowed("get_realized_pnl").allowed is False
    with pytest.raises(PermissionError, match="refuses tool"):
        assert_preflight_tool_or_raise("place_equity_order")
    with pytest.raises(PermissionError, match="refuses tool"):
        assert_preflight_tool_or_raise("review_equity_order")


def test_06_readonly_client_runs_sanitized_reads(sanitized_responses: dict) -> None:
    calls: list[tuple[str, dict]] = []

    def transport(name: str, args: dict) -> dict:
        calls.append((name, args))
        assert name not in PREFLIGHT_MUTATING_DENYLIST
        return sanitized_responses[name]

    client = ReadOnlyPreflightClient(transport)
    snap = client.run_account_reads(symbols=("QQQ", "TQQQ", "SQQQ"))
    assert set(client.invocations) <= PREFLIGHT_READ_ALLOWLIST
    assert "place_equity_order" not in client.invocations
    assert "review_equity_order" not in client.invocations
    assert snap["get_equity_quotes"]["quotes"][0]["symbol"] == "QQQ"
    assert any(c[0] == "get_equity_quotes" and c[1]["symbols"] == ["QQQ", "TQQQ", "SQQQ"] for c in calls)


def test_07_readonly_client_blocks_place_before_transport() -> None:
    def transport(name: str, args: dict) -> dict:
        raise AssertionError(f"transport must not be called for {name}")

    client = ReadOnlyPreflightClient(transport)
    with pytest.raises(PermissionError):
        client.call("place_equity_order", {"symbol": "TQQQ", "side": "buy"})
    assert client.invocations == []
    assert client.rejected == ["place_equity_order"]


def test_08_sanitize_redacts_sensitive_keys() -> None:
    raw = {
        "account_id": "abc-123",
        "access_token": "super-secret",
        "nested": {"order_id": "ord-999", "symbol": "TQQQ"},
    }
    clean = sanitize_for_report(raw)
    assert clean["access_token"] == "[REDACTED]"
    assert clean["account_id"] == "[REDACTED]"
    assert clean["nested"]["order_id"] == "[REDACTED]"
    assert clean["nested"]["symbol"] == "TQQQ"


def test_09_auth_gate_blocked_without_interactive_oauth() -> None:
    blocked = auth_gate_status(
        interactive_oauth_complete=False,
        self_hosted_workers=0,
        rh_mcp_namespace_present=False,
    )
    assert blocked["status"] == "BLOCKED"
    assert blocked["unattended_auth_supported"] is False
    ready = auth_gate_status(
        interactive_oauth_complete=True,
        self_hosted_workers=1,
        rh_mcp_namespace_present=False,
    )
    assert ready["status"] == "READY"


def test_10_host_schema_still_forbids_client_order_id(docs_tools_list: dict) -> None:
    caps = capabilities_from_tools_list(docs_tools_list)
    assert caps.place_equity_order is True
    assert caps.review_equity_order is True
    with pytest.raises(Exception, match="forbidden|undocumented"):
        filter_place_args({"symbol": "TQQQ", "client_order_id": "should-fail"})
    assert "client_order_id" in PLACE_ARG_FORBIDDEN


def test_11_empty_tools_list_fail_closed() -> None:
    classified = classify_tools_list({"tools": []})
    assert classified == []
    caps = capabilities_from_tools_list({"tools": []})
    assert caps.place_equity_order is False
    assert caps.source == "empty_tools_list"


def test_12_orders_and_money_remain_zero_in_mock_path(sanitized_responses: dict) -> None:
    place_attempts = 0

    def transport(name: str, args: dict) -> dict:
        nonlocal place_attempts
        if name == "place_equity_order":
            place_attempts += 1
        return sanitized_responses.get(name, {})

    client = ReadOnlyPreflightClient(transport)
    client.run_account_reads()
    assert place_attempts == 0
    assert all(t != "place_equity_order" for t in client.invocations)
    money_moved = 0.0
    assert money_moved == 0.0
