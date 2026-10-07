"""Execution-broker selection. Real-money names are rejected."""

from __future__ import annotations

import os
from collections.abc import Mapping

from brokers.types import UnsafeBrokerConfiguration

ALPACA_PAPER = "alpaca_paper"
ALPACA_LIVE_SHADOW = "alpaca_live_shadow"
ALPACA_LIVE_PILOT = "alpaca_live_pilot"
ROBINHOOD_SHADOW = "robinhood_shadow"
ROBINHOOD_CONNECTED_SHADOW = "robinhood_connected_shadow"
ROBINHOOD_HOST_HANDOFF = "robinhood_host_handoff"
_ALLOWED = frozenset(
    {
        ALPACA_PAPER,
        ALPACA_LIVE_SHADOW,
        ALPACA_LIVE_PILOT,
        ROBINHOOD_SHADOW,
        ROBINHOOD_CONNECTED_SHADOW,
        ROBINHOOD_HOST_HANDOFF,
    }
)
_LIVE_NAMES = frozenset(
    {
        "alpaca_live",
        "robinhood_live",
        "robinhood",
        "live",
        "robinhood_agentic",
        "robinhood_real",
    }
)


def parse_execution_broker(raw: str | None) -> str:
    """Require an explicit allowlisted mode. Blank and bare live names fail closed."""
    if raw is None:
        raise UnsafeBrokerConfiguration(
            "EXECUTION_BROKER is missing. Refusing to guess a broker."
        )
    text = str(raw).strip().lower()
    if not text:
        raise UnsafeBrokerConfiguration(
            "EXECUTION_BROKER is blank. Refusing to guess a broker."
        )
    if text in _ALLOWED:
        return text
    if text in _LIVE_NAMES or "live" in text:
        raise UnsafeBrokerConfiguration(
            f"EXECUTION_BROKER={text!r} is not available. "
            "Use alpaca_live_shadow for reads or alpaca_live_pilot for the "
            "controlled real-money path (multi-key arming required)."
        )
    raise UnsafeBrokerConfiguration(
        f"EXECUTION_BROKER={text!r} is not supported. "
        f"Use {ALPACA_PAPER!r}, {ALPACA_LIVE_SHADOW!r}, {ALPACA_LIVE_PILOT!r}, "
        f"{ROBINHOOD_SHADOW!r}, {ROBINHOOD_CONNECTED_SHADOW!r}, "
        f"or {ROBINHOOD_HOST_HANDOFF!r}."
    )


def execution_broker_from_environ(env: Mapping[str, str] | None = None) -> str:
    """Resolve the process mode.

    An absent ``EXECUTION_BROKER`` keeps today's Alpaca paper cron. A present
    but blank, unknown, or live value fails closed before any order path.
    """
    source = os.environ if env is None else env
    if "EXECUTION_BROKER" not in source:
        return ALPACA_PAPER
    return parse_execution_broker(source.get("EXECUTION_BROKER"))
