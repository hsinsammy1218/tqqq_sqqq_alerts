"""Broker boundary for Alpaca paper, Alpaca live shadow, and Robinhood shadow.

Strategy code stays free of broker-specific order calls. Alpaca paper
submission remains in ``alpaca_paper.execute_paper_orders``. Alpaca Live and
Robinhood submission are not implemented.
"""

from brokers.alpaca_live_broker import ALPACA_LIVE_SUBMISSION_IMPLEMENTED
from brokers.mode import UnsafeBrokerConfiguration, execution_broker_from_environ, parse_execution_broker
from brokers.robinhood_agentic import LIVE_SUBMISSION_IMPLEMENTED, LiveSubmissionDisabled, RobinhoodAgenticBroker
from brokers.types import TradeIntent

__all__ = [
    "ALPACA_LIVE_SUBMISSION_IMPLEMENTED",
    "LIVE_SUBMISSION_IMPLEMENTED",
    "LiveSubmissionDisabled",
    "RobinhoodAgenticBroker",
    "TradeIntent",
    "UnsafeBrokerConfiguration",
    "execution_broker_from_environ",
    "parse_execution_broker",
]
