"""Broker boundary for Alpaca paper and Robinhood shadow.

Strategy code stays free of broker-specific order calls. Alpaca paper
submission remains in ``alpaca_paper.execute_paper_orders``. Robinhood
submission is not implemented.
"""

from brokers.mode import UnsafeBrokerConfiguration, execution_broker_from_environ, parse_execution_broker
from brokers.robinhood_agentic import LIVE_SUBMISSION_IMPLEMENTED, LiveSubmissionDisabled, RobinhoodAgenticBroker
from brokers.types import TradeIntent

__all__ = [
    "LIVE_SUBMISSION_IMPLEMENTED",
    "LiveSubmissionDisabled",
    "RobinhoodAgenticBroker",
    "TradeIntent",
    "UnsafeBrokerConfiguration",
    "execution_broker_from_environ",
    "parse_execution_broker",
]
