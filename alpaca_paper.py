"""Optional Alpaca *paper* limit-order execution for actionable TQQQ/SQQQ alerts.

Orders are submitted only when ``ALPACA_PAPER_TRADING=true`` and ``DRY_RUN=false``.
The trading host is hard-gated to the paper API; live trading is not supported.
"""

from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import requests

from data import alpaca_auth_headers
from paper_risk import (
    KillStatus,
    PaperRiskLimits,
    apply_buy_notional_cap,
    assess_kill_from_broker,
    filter_intents_for_kill,
    filter_intents_for_tqqq_only,
)
from position_reconcile import BrokerSnapshot
from runtime_logging import log_event
from strategy_types import AlertDecision, PositionState
from trade_log import (
    DEFAULT_TRADE_LOG_PATH,
    append_trade_record,
    build_trade_record,
    extract_regime,
)

PAPER_TRADING_BASE_URL = "https://paper-api.alpaca.markets"
ACTIONABLE_ALERTS = frozenset({"BUY", "SELL", "FLIP"})
