"""Shared enumerations for the HourMM trading system."""

from enum import Enum, auto


class RiskMode(Enum):
    """Risk modes that gate allowed actions.
    
    NORMAL: Quoting and taking allowed within limits.
    REDUCE_ONLY: Only actions that reduce worst-case exposure. No new risk.
    FLATTEN: Aggressive reduction toward zero inventory. Only unwind intents.
    HALT: No new orders. Cancel-all is permitted. Used for severe uncertainty.
    """
    NORMAL = auto()
    REDUCE_ONLY = auto()
    FLATTEN = auto()
    HALT = auto()


class SessionState(Enum):
    """Hourly session lifecycle states.
    
    BOOT_SYNC: Connect streams, cancel-all, establish clean start.
    ACTIVE: Normal opportunistic trading.
    WIND_DOWN: Gradually reduce inventory targets; restrict new exposure.
    FLATTEN: Enforce exit; prefer being flat before hour end.
    DONE: Stop trading this market; transition to next hour market.
    """
    BOOT_SYNC = auto()
    ACTIVE = auto()
    WIND_DOWN = auto()
    FLATTEN = auto()
    DONE = auto()


class OrderPurpose(Enum):
    """Purpose of an order for tracking and risk gating."""
    QUOTE = auto()  # Passive liquidity provision
    UNWIND = auto()  # Reducing existing position
    TAKE = auto()  # Aggressive crossing of spread


class Side(Enum):
    """Order side."""
    BUY = auto()
    SELL = auto()


class OrderStatus(Enum):
    """Order status tracking."""
    PENDING = auto()  # Submitted but not yet acknowledged
    OPEN = auto()  # Acknowledged and live on the book
    PARTIAL = auto()  # Partially filled
    FILLED = auto()  # Fully filled
    CANCELLED = auto()  # Cancelled (by user or exchange)
    REJECTED = auto()  # Rejected by exchange
    EXPIRED = auto()  # Expired (TTL/GTD)
    UNKNOWN = auto()  # Unknown state (e.g., timeout without ack)


class EventType(Enum):
    """Event types for the reducer."""
    # From Binance snapshot poller
    BINANCE_SNAPSHOT = auto()
    
    # From Polymarket market WS
    MARKET_BBO = auto()
    TICK_SIZE_CHANGED = auto()
    MARKET_STATUS = auto()
    
    # From Polymarket user WS
    USER_ORDER = auto()
    USER_TRADE = auto()
    
    # From executor
    REST_ORDER_ACK = auto()
    REST_ERROR = auto()
    
    # From control plane
    CONTROL_COMMAND = auto()
    
    # Internal
    REDUCER_TICK = auto()
    SESSION_ROLLOVER = auto()
    WS_DISCONNECT = auto()
    WS_RECONNECT = auto()
