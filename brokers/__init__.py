"""Broker boundary for Alpaca paper, live shadow, live pilot, and Robinhood.

Strategy code stays free of broker-specific order calls. Alpaca paper
submission remains in ``alpaca_paper.execute_paper_orders``. Alpaca live shadow
never submits. Alpaca live pilot posts only through
``AlpacaLiveExecutor.submit_validated_order`` under multi-key arming.
Robinhood Agentic broker submit_order stays disabled. Host-mediated placement
uses an injected MCP host transport via robinhood_host_executor (Case C:
Render never places).
"""

from brokers.alpaca_live_broker import ALPACA_LIVE_SUBMISSION_IMPLEMENTED
from brokers.alpaca_live_executor import ALPACA_LIVE_PILOT_SUBMISSION_IMPLEMENTED
from brokers.mode import UnsafeBrokerConfiguration, execution_broker_from_environ, parse_execution_broker
from brokers.robinhood_agentic import LIVE_SUBMISSION_IMPLEMENTED, LiveSubmissionDisabled, RobinhoodAgenticBroker
from brokers.types import TradeIntent

__all__ = [
    "ALPACA_LIVE_PILOT_SUBMISSION_IMPLEMENTED",
    "ALPACA_LIVE_SUBMISSION_IMPLEMENTED",
    "LIVE_SUBMISSION_IMPLEMENTED",
    "LiveSubmissionDisabled",
    "RobinhoodAgenticBroker",
    "TradeIntent",
    "UnsafeBrokerConfiguration",
    "execution_broker_from_environ",
    "parse_execution_broker",
]
