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
    """
    Order fill event.

    Fills have two phases on Polymarket:
    1. MATCHED: Order matched off-chain (immediate). Tokens NOT in wallet yet.
    2. MINED: Transaction confirmed on-chain (~17-19 seconds later). Tokens in wallet.

    The `is_pending` flag distinguishes these:
    - is_pending=True: MATCHED fill. Strategy should see this inventory to avoid
      duplicate orders, but executor CANNOT place SELL orders on it.
    - is_pending=False: MINED fill. Tokens are settled and can be sold.

    When MINED arrives for a trade that was already MATCHED:
    - The executor should move inventory from pending to settled
    - NOT double-count as new inventory
    """
    server_order_id: str
    token: Token
    side: Side
    price: int  # Cents
    size: int   # Shares filled
    fee: float
    ts_exchange: int
    trade_id: str = ""  # Unique trade ID for deduplication (same trade has MATCHED->MINED->CONFIRMED)
    role: str = ""      # "TAKER" or "MAKER" - needed for proper fill_id construction
    is_pending: bool = False  # True for MATCHED (pending), False for MINED/CONFIRMED (settled)

    def __post_init__(self):
        self.event_type = ExecutorEventType.FILL

    @property
    def fill_id(self) -> str:
        """
        Unique fill ID for deduplication within same settlement status.

        Format: "{status}:{role}:{trade_id}:{order_id}"

        IMPORTANT: Different status (MATCHED vs MINED) get different fill_ids
        because we need to process BOTH events:
        - MATCHED -> update pending inventory
        - MINED -> settle pending inventory (move to settled)

        Within same status (e.g., duplicate MINED messages), they deduplicate.
        """
        status = "PENDING" if self.is_pending else "SETTLED"
        if self.trade_id and self.role:
            if self.role == "TAKER":
                return f"{status}:TAKER:{self.trade_id}"
            else:
                return f"{status}:MAKER:{self.trade_id}:{self.server_order_id}"
        # Fallback for backward compatibility
        return f"{status}:{self.server_order_id}:{self.size}:{self.price}"


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


@dataclass(slots=True)
class BarrierEvent(ExecutorEvent):
    """
    Test synchronization barrier.

    When the executor processes this event, it sets barrier_processed.
    Tests can use this for deterministic synchronization:
    1. Send barrier event with a threading.Event
    2. Wait for barrier_processed to be set
    3. Know that all events queued BEFORE the barrier have been processed
    """
    barrier_processed: "threading.Event" = None

    def __post_init__(self):
        self.event_type = ExecutorEventType.TEST_BARRIER


# Import here to avoid circular import (DesiredQuoteSet uses QuoteMode from core)
import threading  # noqa: E402
from .strategy import DesiredQuoteSet  # noqa: E402, F401
