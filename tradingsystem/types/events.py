"""
Event types for the Executor's event-driven architecture.

All events inherit from ExecutorEvent base class.
"""

from dataclasses import dataclass
from typing import Optional

from .core import Token, Side, OrderStatus, ExecutorEventType


@dataclass(slots=True)
class ExecutorEvent:
    """Base class for Executor inbox events."""
    event_type: ExecutorEventType
    ts_local_ms: int


@dataclass(slots=True)
class StrategyIntentEvent(ExecutorEvent):
    """New intent from Strategy."""
    intent: "DesiredQuoteSet"

    def __post_init__(self):
        self.event_type = ExecutorEventType.STRATEGY_INTENT


@dataclass(slots=True)
class OrderAckEvent(ExecutorEvent):
    """Order placement acknowledged."""
    client_order_id: str
    server_order_id: str
    status: OrderStatus
    side: Side
    price: int
    size: int
    token: Token

    def __post_init__(self):
        self.event_type = ExecutorEventType.ORDER_ACK


@dataclass(slots=True)
class CancelAckEvent(ExecutorEvent):
    """Order cancellation acknowledged."""
    server_order_id: str
    success: bool
    reason: str = ""

    def __post_init__(self):
        self.event_type = ExecutorEventType.CANCEL_ACK


@dataclass(slots=True)
class FillEvent(ExecutorEvent):
    """Order fill event."""
    server_order_id: str
    token: Token
    side: Side
    price: int  # Cents
    size: int   # Shares filled
    fee: float
    ts_exchange: int

    def __post_init__(self):
        self.event_type = ExecutorEventType.FILL


@dataclass(slots=True)
class GatewayResultEvent(ExecutorEvent):
    """Result from Gateway action."""
    action_id: str
    success: bool
    server_order_id: Optional[str] = None
    error_kind: Optional[str] = None
    retryable: bool = False

    def __post_init__(self):
        self.event_type = ExecutorEventType.GATEWAY_RESULT


@dataclass(slots=True)
class TimerTickEvent(ExecutorEvent):
    """Periodic timer tick."""

    def __post_init__(self):
        self.event_type = ExecutorEventType.TIMER_TICK


# Import here to avoid circular import (DesiredQuoteSet uses QuoteMode from core)
from .strategy import DesiredQuoteSet  # noqa: E402, F401
