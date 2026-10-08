"""Strict Robinhood Agentic account isolation (Phase 5R.2).

Only the verified Agentic Individual Cash account may be used for reads,
intents, risk, and future order routing. Selection requires:

1. Exactly one ``agentic_allowed=true`` account from ``get_accounts``
2. Nickname fingerprint ``Agentic`` AND last-4 ``6650``
3. Full ``account_number`` binding (never nickname/last4 alone)

Protected suffixes (must never be accessed for positions/balances/orders):
Individual Margin ``9384``, Roth IRA ``5767``.

Fail closed on missing ID, identity change, multiple agentic matches,
fingerprint mismatch, protected suffix, or unbound calls.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

# Operator intent fingerprints (secondary confirmation only — never sole key).
OPERATOR_NICKNAME = "Agentic"
OPERATOR_LAST4 = "6650"
OPERATOR_BROKERAGE_ACCOUNT_TYPE = "individual"
OPERATOR_TYPE = "cash"

PROTECTED_LAST4 = frozenset({"9384", "5767"})

ENV_BOUND_ACCOUNT = "ROBINHOOD_AGENTIC_ACCOUNT_NUMBER"

# Account-scoped tools that MUST carry the bound account_number.
ACCOUNT_SCOPED_TOOLS = frozenset(
    {
        "get_portfolio",
        "get_equity_positions",
        "get_equity_orders",
        "get_equity_tradability",
        "get_equity_tax_lots",
        "get_trade_approval_setting",
        "get_trade_approvals",
        "get_realized_pnl",
        "get_pnl_trade_history",
        "get_option_positions",
        "get_option_orders",
        "get_crypto_positions",
        "get_crypto_orders",
        "get_advanced_orders",
        # Mutating tools also must never target non-agentic (defense in depth;
        # Phase 5R.2 still refuses these at the read-only gate).
        "review_equity_order",
        "place_equity_order",
        "cancel_equity_order",
        "review_option_order",
        "place_option_order",
        "cancel_option_order",
        "preview_crypto_order",
        "place_crypto_order",
        "cancel_crypto_order",
        "review_advanced_order",
        "place_advanced_order",
        "cancel_advanced_order",
        "decline_trade_approval",
    }
)


class AccountIsolationError(Exception):
    """Fail-closed account isolation violation."""


@dataclass(frozen=True)
class AccountFingerprint:
    account_number: str
    nickname: str
    last4: str
    agentic_allowed: bool
    brokerage_account_type: str
    account_type: str  # cash / margin
    state: str


@dataclass(frozen=True)
class BoundAgenticAccount:
    """Authoritative bound Agentic account (full number + fingerprints)."""

    account_number: str
    nickname: str
    last4: str
    brokerage_account_type: str
    account_type: str
    source: str

    def masked(self) -> str:
        return mask_account_number(self.account_number)


def mask_account_number(account_number: str) -> str:
    raw = (account_number or "").strip()
    if len(raw) < 4:
        return "••••????"
    return f"••••{raw[-4:]}"


def last4_of(account_number: str) -> str:
    raw = (account_number or "").strip()
    return raw[-4:] if len(raw) >= 4 else raw


def is_protected_account_number(account_number: str) -> bool:
    return last4_of(account_number) in PROTECTED_LAST4


def _truthy_agentic(value: Any) -> bool:
    if value is True:
        return True
    if value is False or value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _extract_accounts(payload: Any) -> list[dict[str, Any]]:
    if payload is None:
        return []
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if not isinstance(payload, dict):
        return []
    for key in ("accounts", "data", "result"):
        if key not in payload:
            continue
        nested = payload[key]
        if isinstance(nested, list):
            return [row for row in nested if isinstance(row, dict)]
        if isinstance(nested, dict) and isinstance(nested.get("accounts"), list):
            return [row for row in nested["accounts"] if isinstance(row, dict)]
    if "account_number" in payload:
        return [payload]
    return []


def parse_account_row(row: dict[str, Any]) -> AccountFingerprint | None:
    number = str(
        row.get("account_number")
        or row.get("accountNumber")
        or ""
    ).strip()
    if not number:
        return None
    nickname = str(row.get("nickname") or row.get("display_name") or "").strip()
    return AccountFingerprint(
        account_number=number,
        nickname=nickname,
        last4=last4_of(number),
        agentic_allowed=_truthy_agentic(row.get("agentic_allowed")),
        brokerage_account_type=str(
            row.get("brokerage_account_type") or row.get("brokerageAccountType") or ""
        )
        .strip()
        .lower(),
        account_type=str(row.get("type") or "").strip().lower(),
        state=str(row.get("state") or row.get("status") or "").strip().lower(),
    )


def resolve_agentic_account(get_accounts_payload: Any) -> BoundAgenticAccount:
    """Resolve and bind the single Agentic account from ``get_accounts``.

    Algorithm (fail closed):
    - Parse accounts
    - Require exactly one ``agentic_allowed=true``
    - Confirm nickname Agentic AND last4 6650
    - Confirm not a protected suffix
    - Return full ``account_number`` binding
    """
    rows = _extract_accounts(get_accounts_payload)
    if not rows:
        raise AccountIsolationError("get_accounts returned no accounts; refusing bind")

    parsed = [p for p in (parse_account_row(r) for r in rows) if p is not None]
    if not parsed:
        raise AccountIsolationError("get_accounts had no parseable account_number fields")

    agentic = [p for p in parsed if p.agentic_allowed]
    if len(agentic) == 0:
        raise AccountIsolationError(
            "no account with agentic_allowed=true; refusing bind"
        )
    if len(agentic) > 1:
        raise AccountIsolationError(
            f"expected exactly one agentic_allowed=true account, found {len(agentic)}; "
            "refusing bind"
        )

    chosen = agentic[0]
    if is_protected_account_number(chosen.account_number):
        raise AccountIsolationError(
            f"agentic account {mask_account_number(chosen.account_number)} is on the "
            "protected suffix list; refusing bind"
        )
    if chosen.nickname.strip().casefold() != OPERATOR_NICKNAME.casefold():
        raise AccountIsolationError(
            f"agentic account nickname {chosen.nickname!r} does not match operator "
            f"intent {OPERATOR_NICKNAME!r}; refusing bind"
        )
    if chosen.last4 != OPERATOR_LAST4:
        raise AccountIsolationError(
            f"agentic account last4 {chosen.last4!r} does not match operator intent "
            f"{OPERATOR_LAST4!r}; refusing bind"
        )
    if (
        chosen.brokerage_account_type
        and chosen.brokerage_account_type != OPERATOR_BROKERAGE_ACCOUNT_TYPE
    ):
        raise AccountIsolationError(
            f"agentic account brokerage_account_type {chosen.brokerage_account_type!r} "
            f"does not match {OPERATOR_BROKERAGE_ACCOUNT_TYPE!r}; refusing bind"
        )
    if chosen.account_type and chosen.account_type != OPERATOR_TYPE:
        raise AccountIsolationError(
            f"agentic account type {chosen.account_type!r} does not match "
            f"{OPERATOR_TYPE!r}; refusing bind"
        )

    return BoundAgenticAccount(
        account_number=chosen.account_number,
        nickname=chosen.nickname or OPERATOR_NICKNAME,
        last4=chosen.last4,
        brokerage_account_type=chosen.brokerage_account_type
        or OPERATOR_BROKERAGE_ACCOUNT_TYPE,
        account_type=chosen.account_type or OPERATOR_TYPE,
        source="get_accounts",
    )


def bound_account_from_env(env: Mapping[str, str] | None = None) -> BoundAgenticAccount | None:
    """Load a previously bound full account_number from env (no nickname/last4 alone)."""
    source = os.environ if env is None else env
    raw = str(source.get(ENV_BOUND_ACCOUNT) or "").strip()
    if not raw:
        return None
    if not re.fullmatch(r"[A-Za-z0-9]{6,32}", raw):
        raise AccountIsolationError(
            f"{ENV_BOUND_ACCOUNT} is malformed; refusing (need full account_number)"
        )
    if is_protected_account_number(raw):
        raise AccountIsolationError(
            f"{ENV_BOUND_ACCOUNT} points at protected suffix "
            f"{mask_account_number(raw)}; refusing"
        )
    if last4_of(raw) != OPERATOR_LAST4:
        raise AccountIsolationError(
            f"{ENV_BOUND_ACCOUNT} last4 {last4_of(raw)!r} does not match operator "
            f"intent {OPERATOR_LAST4!r}; refusing"
        )
    return BoundAgenticAccount(
        account_number=raw,
        nickname=OPERATOR_NICKNAME,
        last4=last4_of(raw),
        brokerage_account_type=OPERATOR_BROKERAGE_ACCOUNT_TYPE,
        account_type=OPERATOR_TYPE,
        source="env",
    )


def require_bound_account(
    *,
    get_accounts_payload: Any | None = None,
    env: Mapping[str, str] | None = None,
) -> BoundAgenticAccount:
    """Resolve from get_accounts when provided; else require env bind. Fail closed."""
    env_bound = bound_account_from_env(env)
    if get_accounts_payload is not None:
        resolved = resolve_agentic_account(get_accounts_payload)
        if env_bound is not None and env_bound.account_number != resolved.account_number:
            raise AccountIsolationError(
                "identity change: env-bound account "
                f"{env_bound.masked()} != resolved {resolved.masked()}; refusing"
            )
        return resolved
    if env_bound is None:
        raise AccountIsolationError(
            f"{ENV_BOUND_ACCOUNT} unset and no get_accounts payload; refusing"
        )
    return env_bound


def assert_account_allowed(
    account_number: str | None,
    bound: BoundAgenticAccount,
) -> str:
    """Require exact match to the bound Agentic account_number."""
    raw = (account_number or "").strip()
    if not raw:
        raise AccountIsolationError(
            "account_number missing; refusing (cannot target without full ID)"
        )
    if is_protected_account_number(raw):
        raise AccountIsolationError(
            f"refusing protected account {mask_account_number(raw)} "
            f"(suffix in {sorted(PROTECTED_LAST4)})"
        )
    if raw != bound.account_number:
        raise AccountIsolationError(
            f"refusing account {mask_account_number(raw)}; "
            f"only bound Agentic {bound.masked()} is allowlisted"
        )
    return raw


def enforce_tool_account_arg(
    tool: str,
    arguments: Mapping[str, Any] | None,
    bound: BoundAgenticAccount | None,
) -> dict[str, Any]:
    """Inject/validate account_number for account-scoped tools. Fail closed."""
    name = (tool or "").strip()
    args = dict(arguments or {})
    if name not in ACCOUNT_SCOPED_TOOLS:
        # Non-scoped tools must not smuggle a different account_number.
        if "account_number" in args and bound is not None:
            assert_account_allowed(str(args.get("account_number") or ""), bound)
        return args
    if bound is None:
        raise AccountIsolationError(
            f"refusing {name}: Agentic account not bound; call get_accounts resolve first"
        )
    provided = args.get("account_number")
    if provided is None or str(provided).strip() == "":
        args["account_number"] = bound.account_number
    else:
        assert_account_allowed(str(provided), bound)
        args["account_number"] = bound.account_number
    return args


def filter_accounts_payload_for_report(payload: Any) -> Any:
    """Sanitize get_accounts for docs: mask numbers, keep fingerprints."""
    rows = _extract_accounts(payload)
    out = []
    for row in rows:
        fp = parse_account_row(row)
        if fp is None:
            continue
        out.append(
            {
                "account_number_masked": mask_account_number(fp.account_number),
                "nickname": fp.nickname or None,
                "last4": fp.last4,
                "agentic_allowed": fp.agentic_allowed,
                "brokerage_account_type": fp.brokerage_account_type,
                "type": fp.account_type,
                "state": fp.state,
                "protected": is_protected_account_number(fp.account_number),
            }
        )
    return {"accounts": out}


__all__ = [
    "ACCOUNT_SCOPED_TOOLS",
    "AccountFingerprint",
    "AccountIsolationError",
    "BoundAgenticAccount",
    "ENV_BOUND_ACCOUNT",
    "OPERATOR_LAST4",
    "OPERATOR_NICKNAME",
    "PROTECTED_LAST4",
    "assert_account_allowed",
    "bound_account_from_env",
    "enforce_tool_account_arg",
    "filter_accounts_payload_for_report",
    "is_protected_account_number",
    "last4_of",
    "mask_account_number",
    "parse_account_row",
    "require_bound_account",
    "resolve_agentic_account",
]
