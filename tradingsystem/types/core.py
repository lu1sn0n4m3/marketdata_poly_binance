"""
Core enums - the fundamental vocabulary of the trading system.

These are the basic building blocks used throughout the codebase.
"""

from enum import Enum, auto


class Token(Enum):
    """Binary outcome token."""
    YES = auto()
    NO = auto()


class Side(Enum):
    """Order side."""
    BUY = auto()
    SELL = auto()


class OrderStatus(Enum):
    """Order lifecycle status."""
    PENDING_NEW = auto()
    WORKING = auto()
    PENDING_CANCEL = auto()
    PARTIALLY_FILLED = auto()
    FILLED = auto()
    CANCELED = auto()
    REJECTED = auto()
    UNKNOWN = auto()


class QuoteMode(Enum):
    """Strategy quoting mode."""
    STOP = auto()
    NORMAL = auto()
    CAUTION = auto()
    ONE_SIDED_BUY = auto()
    ONE_SIDED_SELL = auto()


class ExecutorEventType(Enum):
    """Event types processed by the Executor."""
    STRATEGY_INTENT = auto()
    ORDER_ACK = auto()
    CANCEL_ACK = auto()
    FILL = auto()
    GATEWAY_RESULT = auto()
    TIMER_TICK = auto()
    TEST_BARRIER = auto()  # For deterministic test synchronization


class GatewayActionType(Enum):
    """Gateway action types."""
    PLACE = auto()
    CANCEL = auto()
    CANCEL_ALL = auto()
