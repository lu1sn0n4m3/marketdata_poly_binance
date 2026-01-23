"""Type definitions for Container B (Polymarket Trader)."""

from dataclasses import dataclass, field
from typing import Optional, Any
from enum import Enum, auto
from time import time_ns

# Import shared types
try:
    from shared.hourmm_common.enums import (
        RiskMode, SessionState, OrderPurpose, Side, OrderStatus, EventType
    )
    from shared.hourmm_common.schemas import BinanceSnapshot, MarketInfo, TokenIds, MarketView
except ImportError:
    import sys
    sys.path.insert(0, "/Users/luissafar/dev/marketdata_poly_binance")
    from shared.hourmm_common.enums import (
        RiskMode, SessionState, OrderPurpose, Side, OrderStatus, EventType
    )
    from shared.hourmm_common.schemas import BinanceSnapshot, MarketInfo, TokenIds, MarketView


# Re-export for convenience
__all__ = [
    "RiskMode", "SessionState", "OrderPurpose", "Side", "OrderStatus", "EventType",
    "BinanceSnapshot", "MarketInfo", "TokenIds", "MarketView",
    "Event", "BinanceSnapshotEvent", "MarketBboEvent", "TickSizeChangedEvent",
    "UserOrderEvent", "UserTradeEvent", "RestOrderAckEvent", "RestErrorEvent",
    "ControlCommandEvent", "WsEvent",
    "OrderState", "PositionState", "HealthState", "LimitState",
    "CanonicalState", "CanonicalStateView",
    "StrategyIntent", "DesiredOrder", "QuoteSet", "OrderAction",
    "DecisionContext", "RiskDecision",
]


# ============== Event Types ==============

@dataclass(slots=True)
class Event:
    """Base event class."""
    event_type: EventType
    ts_local_ms: int = field(default_factory=lambda: time_ns() // 1_000_000)


@dataclass(slots=True)
class BinanceSnapshotEvent(Event):
    """Event from Binance snapshot poller."""
    snapshot: Optional[BinanceSnapshot] = None
    
    def __post_init__(self):
        self.event_type = EventType.BINANCE_SNAPSHOT


@dataclass(slots=True)
class MarketBboEvent(Event):
    """Event from Polymarket market WS."""
    best_bid: Optional[float] = None
    best_ask: Optional[float] = None
    tick_size: float = 0.01
    token_id: str = ""  # Which token this update is for

    def __post_init__(self):
        self.event_type = EventType.MARKET_BBO


@dataclass(slots=True)
class TickSizeChangedEvent(Event):
    """Event when tick size changes."""
    new_tick_size: float = 0.01
    
    def __post_init__(self):
        self.event_type = EventType.TICK_SIZE_CHANGED


@dataclass(slots=True)
class UserOrderEvent(Event):
    """Event from Polymarket user WS for order updates."""
    order_id: str = ""
    status: OrderStatus = OrderStatus.UNKNOWN
    side: Side = Side.BUY
    price: float = 0.0
    size: float = 0.0
    filled: float = 0.0
    remaining: float = 0.0
    token_id: str = ""
    
    def __post_init__(self):
        self.event_type = EventType.USER_ORDER


@dataclass(slots=True)
class UserTradeEvent(Event):
    """Event from Polymarket user WS for trade fills."""
    trade_id: str = ""
    order_id: str = ""
    side: Side = Side.BUY
    price: float = 0.0
    size: float = 0.0
    token_id: str = ""
    
    def __post_init__(self):
        self.event_type = EventType.USER_TRADE


@dataclass(slots=True)
class RestOrderAckEvent(Event):
    """Acknowledgment from REST order placement."""
    client_req_id: str = ""
    order_id: str = ""
    success: bool = True
    
    def __post_init__(self):
        self.event_type = EventType.REST_ORDER_ACK


@dataclass(slots=True)
class RestErrorEvent(Event):
    """Error from REST operation."""
    client_req_id: str = ""
    reason: str = ""
    recoverable: bool = True
    
    def __post_init__(self):
        self.event_type = EventType.REST_ERROR


@dataclass(slots=True)
class ControlCommandEvent(Event):
    """Operator control command."""
    command_type: str = ""  # pause_quoting, set_mode, flatten_now, set_limits
    payload: dict = field(default_factory=dict)
    
    def __post_init__(self):
        self.event_type = EventType.CONTROL_COMMAND


@dataclass(slots=True)
class WsEvent(Event):
    """WebSocket connection event."""
    ws_name: str = ""  # "market" or "user"
    connected: bool = False
    
    def __post_init__(self):
        self.event_type = EventType.WS_RECONNECT if self.connected else EventType.WS_DISCONNECT


# ============== State Types ==============

@dataclass(slots=True)
class OrderState:
    """State of a single order."""
    order_id: str
    client_req_id: str
    side: Side
    price: float
    size: float
    filled: float
    remaining: float
    status: OrderStatus
    token_id: str
    purpose: OrderPurpose
    created_at_ms: int
    expires_at_ms: int
    last_update_ms: int


@dataclass(slots=True)
class PositionState:
    """Current position state."""
    yes_tokens: float = 0.0
    no_tokens: float = 0.0
    cash_reserved: float = 0.0
    
    @property
    def net_exposure(self) -> float:
        """Net exposure in terms of YES tokens."""
        return self.yes_tokens - self.no_tokens


@dataclass(slots=True)
class HealthState:
    """Health state of various connections."""
    market_ws_connected: bool = False
    market_ws_last_msg_ms: int = 0
    user_ws_connected: bool = False
    user_ws_last_msg_ms: int = 0
    rest_healthy: bool = True
    rest_last_error_ms: int = 0
    binance_snapshot_stale: bool = True
    binance_snapshot_age_ms: int = 999999


@dataclass(slots=True)
class LimitState:
    """Current limits (may be adjusted by operator)."""
    max_reserved_capital: float = 1000.0
    max_position_size: float = 500.0
    max_order_size: float = 100.0
    max_open_orders: int = 4
    quoting_enabled: bool = True
    taking_enabled: bool = True


@dataclass
class CanonicalState:
    """
    Canonical trading state - ONLY mutated by StateReducer.
    
    This is the single source of truth for all trading state.
    """
    # Session state
    session_state: SessionState = SessionState.BOOT_SYNC
    risk_mode: RiskMode = RiskMode.HALT
    
    # Active market
    active_market: Optional[MarketInfo] = None
    token_ids: Optional[TokenIds] = None
    
    # Positions and orders
    positions: PositionState = field(default_factory=PositionState)
    open_orders: dict[str, OrderState] = field(default_factory=dict)
    
    # Market view
    market_view: MarketView = field(default_factory=lambda: MarketView(market_id=""))
    
    # Health
    health: HealthState = field(default_factory=HealthState)
    
    # Limits
    limits: LimitState = field(default_factory=LimitState)
    
    # Timestamps
    last_decision_tick_ms: int = 0
    last_reconcile_ms: int = 0
    session_start_ms: int = 0
    
    # Latest Binance snapshot (for reference)
    latest_snapshot: Optional[BinanceSnapshot] = None
    
    def view(self) -> "CanonicalStateView":
        """Create an immutable view for decision loop."""
        return CanonicalStateView(
            session_state=self.session_state,
            risk_mode=self.risk_mode,
            active_market=self.active_market,
            token_ids=self.token_ids,
            positions=PositionState(
                yes_tokens=self.positions.yes_tokens,
                no_tokens=self.positions.no_tokens,
                cash_reserved=self.positions.cash_reserved,
            ),
            open_orders=dict(self.open_orders),  # Shallow copy
            market_view=MarketView(
                market_id=self.market_view.market_id,
                tick_size=self.market_view.tick_size,
                best_bid=self.market_view.best_bid,
                best_ask=self.market_view.best_ask,
                yes_best_bid=self.market_view.yes_best_bid,
                yes_best_ask=self.market_view.yes_best_ask,
                no_best_bid=self.market_view.no_best_bid,
                no_best_ask=self.market_view.no_best_ask,
                book_ts_local_ms=self.market_view.book_ts_local_ms,
                ws_age_ms=self.market_view.ws_age_ms,
            ),
            health=HealthState(
                market_ws_connected=self.health.market_ws_connected,
                market_ws_last_msg_ms=self.health.market_ws_last_msg_ms,
                user_ws_connected=self.health.user_ws_connected,
                user_ws_last_msg_ms=self.health.user_ws_last_msg_ms,
                rest_healthy=self.health.rest_healthy,
                rest_last_error_ms=self.health.rest_last_error_ms,
                binance_snapshot_stale=self.health.binance_snapshot_stale,
                binance_snapshot_age_ms=self.health.binance_snapshot_age_ms,
            ),
            limits=LimitState(
                max_reserved_capital=self.limits.max_reserved_capital,
                max_position_size=self.limits.max_position_size,
                max_order_size=self.limits.max_order_size,
                max_open_orders=self.limits.max_open_orders,
                quoting_enabled=self.limits.quoting_enabled,
                taking_enabled=self.limits.taking_enabled,
            ),
            latest_snapshot=self.latest_snapshot,
        )


@dataclass(frozen=True)
class CanonicalStateView:
    """
    Immutable view of CanonicalState for decision loop.
    
    The decision loop only sees this - cannot mutate the actual state.
    """
    session_state: SessionState
    risk_mode: RiskMode
    active_market: Optional[MarketInfo]
    token_ids: Optional[TokenIds]
    positions: PositionState
    open_orders: dict[str, OrderState]
    market_view: MarketView
    health: HealthState
    limits: LimitState
    latest_snapshot: Optional[BinanceSnapshot]


# ============== Strategy Types ==============

@dataclass(slots=True)
class DesiredOrder:
    """A desired order from strategy."""
    side: Side
    price: float
    size: float
    purpose: OrderPurpose
    expires_at_ms: int
    token_id: str


@dataclass(slots=True)
class QuoteSet:
    """Set of desired quotes from strategy."""
    bid: Optional[DesiredOrder] = None
    ask: Optional[DesiredOrder] = None


@dataclass(slots=True)
class StrategyIntent:
    """Output from strategy."""
    quotes: QuoteSet = field(default_factory=QuoteSet)
    take_actions: list[DesiredOrder] = field(default_factory=list)
    target_inventory: Optional[float] = None  # Target net position
    cancel_all: bool = False


class OrderActionType(Enum):
    """Type of order action."""
    PLACE = auto()
    CANCEL = auto()
    REPLACE = auto()


@dataclass(slots=True)
class OrderAction:
    """An action to take on an order."""
    action_type: OrderActionType
    order: Optional[DesiredOrder] = None  # For PLACE/REPLACE
    order_id: Optional[str] = None  # For CANCEL/REPLACE
    client_req_id: str = ""


@dataclass(slots=True)
class DecisionContext:
    """Context provided to strategy for decision making."""
    state_view: CanonicalStateView
    snapshot: Optional[BinanceSnapshot]
    now_ms: int
    t_remaining_ms: int


@dataclass(slots=True)
class RiskDecision:
    """Output from risk engine."""
    allowed_mode: RiskMode
    max_new_exposure: float
    can_quote: bool
    can_take: bool
    target_inventory: Optional[float]  # Risk-adjusted target
    reason: str = ""
