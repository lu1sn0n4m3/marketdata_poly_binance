"""
Trading system types.

This module re-exports all types for backward compatibility.
You can import directly from here or from the specific submodules.

Example:
    # Backward compatible (works as before)
    from tradingsystem.types import Token, Side, now_ms

    # More explicit (preferred for new code)
    from tradingsystem.types.core import Token, Side
    from tradingsystem.types.utils import now_ms
"""

# Core enums
from .core import (
    Token,
    Side,
    OrderStatus,
    QuoteMode,
    ExecutorEventType,
    GatewayActionType,
)

# Utility functions
from .utils import (
    now_ms,
    wall_ms,
    price_to_cents,
    cents_to_price,
)

# Market data snapshots
from .market_data import (
    MarketSnapshotMeta,
    PolymarketBookTop,
    PolymarketBookSnapshot,
    BinanceSnapshot,
)

# Strategy output types
from .strategy import (
    DesiredQuoteLeg,
    DesiredQuoteSet,
)

# Order types
from .orders import (
    RealOrderSpec,
    WorkingOrder,
)

# Event types
from .events import (
    ExecutorEvent,
    StrategyIntentEvent,
    OrderAckEvent,
    CancelAckEvent,
    FillEvent,
    GatewayResultEvent,
    TimerTickEvent,
)

# Gateway types
from .gateway import (
    GatewayAction,
    GatewayResult,
)

# State types
from .state import (
    InventoryState,
    RiskState,
)

# Configuration types
from .config import (
    ExecutorConfig,
    MarketInfo,
)

__all__ = [
    # Core enums
    "Token",
    "Side",
    "OrderStatus",
    "QuoteMode",
    "ExecutorEventType",
    "GatewayActionType",
    # Utility functions
    "now_ms",
    "wall_ms",
    "price_to_cents",
    "cents_to_price",
    # Market data
    "MarketSnapshotMeta",
    "PolymarketBookTop",
    "PolymarketBookSnapshot",
    "BinanceSnapshot",
    # Strategy
    "DesiredQuoteLeg",
    "DesiredQuoteSet",
    # Orders
    "RealOrderSpec",
    "WorkingOrder",
    # Events
    "ExecutorEvent",
    "StrategyIntentEvent",
    "OrderAckEvent",
    "CancelAckEvent",
    "FillEvent",
    "GatewayResultEvent",
    "TimerTickEvent",
    # Gateway
    "GatewayAction",
    "GatewayResult",
    # State
    "InventoryState",
    "RiskState",
    # Config
    "ExecutorConfig",
    "MarketInfo",
]
