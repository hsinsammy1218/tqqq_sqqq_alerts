"""Phase 5R.2 — Agentic account isolation (fail closed)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from brokers.robinhood_reader import RobinhoodReadClient, RobinhoodToolRejected
from robinhood_account_isolation import (
    ENV_BOUND_ACCOUNT,
    OPERATOR_LAST4,
    OPERATOR_NICKNAME,
    PROTECTED_LAST4,
    AccountIsolationError,
    assert_account_allowed,
    bound_account_from_env,
    enforce_tool_account_arg,
    filter_accounts_payload_for_report,
    is_protected_account_number,
    mask_account_number,
    require_bound_account,
    resolve_agentic_account,
)
from robinhood_host_schema import build_place_args, docs_confirmed_capabilities
from brokers.types import QuoteView, TradeIntent, UnsafeBrokerConfiguration
from strategy_params import STRATEGY_VERSION

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "rh_agentic_accounts.json"


@pytest.fixture(scope="module")
def accounts_fix() -> dict:
    return json.loads(FIXTURES.read_text(encoding="utf-8"))


@pytest.fixture
def agentic_row(accounts_fix: dict) -> dict:
    return dict(accounts_fix["agentic"])


@pytest.fixture
def margin_row(accounts_fix: dict) -> dict:
    return dict(accounts_fix["protected_margin"])


@pytest.fixture
def roth_row(accounts_fix: dict) -> dict:
    return dict(accounts_fix["protected_roth"])


@pytest.fixture
def get_accounts_payload(agentic_row: dict, margin_row: dict, roth_row: dict) -> dict:
    return {"accounts": [margin_row, roth_row, agentic_row]}


def test_01_strategy_still_frozen() -> None:
    assert STRATEGY_VERSION == "1.0.0"
    assert OPERATOR_NICKNAME == "Agentic"
    assert OPERATOR_LAST4 == "6650"
    assert PROTECTED_LAST4 == frozenset({"9384", "5767"})


def test_02_resolve_binds_full_agentic_id(get_accounts_payload: dict, agentic_row: dict) -> None:
    bound = resolve_agentic_account(get_accounts_payload)
    assert bound.account_number == agentic_row["account_number"]
    assert bound.account_number.endswith("6650")
    assert bound.nickname == "Agentic"
    assert bound.masked() == "••••6650"
    assert not is_protected_account_number(bound.account_number)


def test_03_reject_protected_margin_and_roth(
    agentic_row: dict, margin_row: dict, roth_row: dict
) -> None:
    bound = resolve_agentic_account({"accounts": [agentic_row]})
    for bad in (margin_row["account_number"], roth_row["account_number"]):
        assert is_protected_account_number(bad)
        with pytest.raises(AccountIsolationError, match="protected|allowlisted"):
            assert_account_allowed(bad, bound)
        with pytest.raises(AccountIsolationError):
            enforce_tool_account_arg(
                "get_equity_positions",
                {"account_number": bad},
                bound,
            )


def test_04_fail_closed_multiple_agentic(agentic_row: dict) -> None:
    twin = dict(agentic_row)
    twin["account_number"] = "OTHERAGT6650"
    with pytest.raises(AccountIsolationError, match="exactly one"):
        resolve_agentic_account({"accounts": [agentic_row, twin]})


def test_05_fail_closed_fingerprint_mismatch(agentic_row: dict) -> None:
    bad_nick = dict(agentic_row)
    bad_nick["nickname"] = "NotAgentic"
    with pytest.raises(AccountIsolationError, match="nickname"):
        resolve_agentic_account({"accounts": [bad_nick]})
    bad_last = dict(agentic_row)
    bad_last["account_number"] = "TESTAGT9999"
    with pytest.raises(AccountIsolationError, match="last4"):
        resolve_agentic_account({"accounts": [bad_last]})


def test_06_env_bind_rejects_protected_and_wrong_last4(margin_row: dict) -> None:
    with pytest.raises(AccountIsolationError, match="protected"):
        bound_account_from_env({ENV_BOUND_ACCOUNT: margin_row["account_number"]})
    with pytest.raises(AccountIsolationError, match="last4"):
        bound_account_from_env({ENV_BOUND_ACCOUNT: "ABCDEF9999"})


def test_06b_env_pin_alone_not_host_authority(agentic_row: dict) -> None:
    """H1: env last4 pin is not Agentic authority without live get_accounts."""
    with pytest.raises(AccountIsolationError, match="live get_accounts|not authority"):
        require_bound_account(env={ENV_BOUND_ACCOUNT: "EVILACC6650"})
    with pytest.raises(AccountIsolationError, match="live get_accounts|not authority"):
        require_bound_account(env={ENV_BOUND_ACCOUNT: agentic_row["account_number"]})
    # Explicit handoff-only escape hatch remains unverified.
    pin = require_bound_account(
        env={ENV_BOUND_ACCOUNT: agentic_row["account_number"]},
        allow_unverified_env_pin=True,
    )
    assert pin.source == "env_pin_unverified"


def test_07_identity_change_detected(get_accounts_payload: dict, agentic_row: dict) -> None:
    with pytest.raises(AccountIsolationError, match="identity change"):
        require_bound_account(
            get_accounts_payload=get_accounts_payload,
            env={ENV_BOUND_ACCOUNT: "MISMATCH6650"},
        )
    bound = require_bound_account(
        get_accounts_payload=get_accounts_payload,
        env={ENV_BOUND_ACCOUNT: agentic_row["account_number"]},
    )
    assert bound.account_number == agentic_row["account_number"]


def test_08_reader_injects_bound_account_and_blocks_others(
    get_accounts_payload: dict, agentic_row: dict, margin_row: dict
) -> None:
    seen: list[tuple[str, dict]] = []

    def transport(name: str, args: dict) -> object:
        seen.append((name, dict(args)))
        if name == "get_accounts":
            return get_accounts_payload
        return {"ok": True}

    client = RobinhoodReadClient(transport)
    snap = client.read_snapshot(("TQQQ", "SQQQ"))
    assert client.bound is not None
    assert client.bound.account_number == agentic_row["account_number"]
    assert snap["get_accounts"] == get_accounts_payload
    scoped = [a for n, a in seen if n == "get_portfolio"]
    assert scoped and scoped[0]["account_number"] == agentic_row["account_number"]
    with pytest.raises(RobinhoodToolRejected, match="protected|allowlisted"):
        client.call("get_equity_positions", {"account_number": margin_row["account_number"]})


def test_09_never_select_by_nickname_or_last4_alone(agentic_row: dict) -> None:
    # Missing full account_number → fail; nickname alone is not enough.
    with pytest.raises(AccountIsolationError):
        resolve_agentic_account(
            {
                "accounts": [
                    {
                        "nickname": "Agentic",
                        "agentic_allowed": True,
                        "type": "cash",
                        "brokerage_account_type": "individual",
                    }
                ]
            }
        )
    # last4 alone without agentic_allowed true on a matching full ID fails.
    with pytest.raises(AccountIsolationError, match="agentic_allowed"):
        resolve_agentic_account(
            {
                "accounts": [
                    {
                        "account_number": agentic_row["account_number"],
                        "nickname": "Agentic",
                        "agentic_allowed": False,
                        "type": "cash",
                        "brokerage_account_type": "individual",
                    }
                ]
            }
        )


def test_10_place_args_require_bound_and_include_account(
    agentic_row: dict,
) -> None:
    from robinhood_account_isolation import BoundAgenticAccount

    intent = TradeIntent(
        strategy_version="1.0.0",
        signal_symbol="QQQ",
        execution_symbol="TQQQ",
        action="BUY",
        quantity=1,
        estimated_price=50.0,
        estimated_notional=50.0,
        confidence=50,
        regime="trend_up",
        reason="test",
        signal_id="sig",
        timestamp="2026-10-07T15:00:00Z",
        purpose="entry",
        client_order_id="cid-1",
    )
    quote = QuoteView("TQQQ", 50.0, 50.10, None)
    with pytest.raises(UnsafeBrokerConfiguration, match="not bound"):
        build_place_args(
            intent,
            quote=quote,
            capabilities=docs_confirmed_capabilities(),
            slippage_bps=25,
            bound=None,
        )
    bound = BoundAgenticAccount(
        account_number=agentic_row["account_number"],
        nickname="Agentic",
        last4="6650",
        brokerage_account_type="individual",
        account_type="cash",
        source="test",
    )
    args = build_place_args(
        intent,
        quote=quote,
        capabilities=docs_confirmed_capabilities(),
        slippage_bps=25,
        bound=bound,
    )
    assert args["account_number"] == agentic_row["account_number"]
    assert "client_order_id" not in args


def test_11_report_sanitization_masks_ids(get_accounts_payload: dict) -> None:
    report = filter_accounts_payload_for_report(get_accounts_payload)
    for row in report["accounts"]:
        assert row["account_number_masked"].startswith("••••")
        assert "account_number" not in row or str(row.get("account_number", "")).startswith("••••")
    assert any(r["last4"] == "6650" and r["agentic_allowed"] for r in report["accounts"])
    assert any(r["last4"] == "9384" and r["protected"] for r in report["accounts"])
    assert any(r["last4"] == "5767" and r["protected"] for r in report["accounts"])
    assert mask_account_number("637506650") == "••••6650"
