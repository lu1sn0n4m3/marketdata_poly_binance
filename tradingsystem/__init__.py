"""
Trading System - Binary CLOB Market-Making for Polymarket

A simplified, efficient market-making system for Polymarket Bitcoin hourly markets.
"""

__version__ = "0.1.0"

# Core types
from .mm_types import (
    # Enums
    Token,
    Side,
    OrderStatus,
    QuoteMode,
    ExecutorEventType,
    GatewayActionType,
    # Snapshot types
    MarketSnapshotMeta,
    PMBookTop,
    PMBookSnapshot,
    BNSnapshot,
    # State types
    InventoryState,
    RiskState,
    # Strategy types
    DesiredQuoteLeg,
    DesiredQuoteSet,
    # Order types
    RealOrderSpec,
    WorkingOrder,
    GatewayAction,
    GatewayResult,
    # Event types
    ExecutorEvent,
    StrategyIntentEvent,
    OrderAckEvent,
    CancelAckEvent,
    FillEvent,
    GatewayResultEvent,
    TimerTickEvent,
    # Config
    ExecutorConfig,
    MarketInfo,
    # Utilities
    now_ms,
    wall_ms,
    price_to_cents,
    cents_to_price,
)

# Gamma API
from .gamma_client import GammaClient, GammaAPIError

# Market discovery
from .market_finder import (
    BitcoinHourlyMarketFinder,
    MarketScheduler,
    build_market_slug,
    get_current_hour_et,
    get_next_hour_et,
    get_target_end_time,
    parse_market_end_time,
    extract_reference_price,
)

__all__ = [
    # Version
    "__version__",
    # Enums
    "Token",
    "Side",
    "OrderStatus",
    "QuoteMode",
    "ExecutorEventType",
    "GatewayActionType",
    # Snapshot types
    "MarketSnapshotMeta",
    "PMBookTop",
    "PMBookSnapshot",
    "BNSnapshot",
    # State types
    "InventoryState",
    "RiskState",
    # Strategy types
    "DesiredQuoteLeg",
    "DesiredQuoteSet",
    # Order types
    "RealOrderSpec",
    "WorkingOrder",
    "GatewayAction",
    "GatewayResult",
    # Event types
    "ExecutorEvent",
    "StrategyIntentEvent",
    "OrderAckEvent",
    "CancelAckEvent",
    "FillEvent",
    "GatewayResultEvent",
    "TimerTickEvent",
    # Config
    "ExecutorConfig",
    "MarketInfo",
    # Utilities
    "now_ms",
    "wall_ms",
    "price_to_cents",
    "cents_to_price",
    # Gamma API
    "GammaClient",
    "GammaAPIError",
    # Market discovery
    "BitcoinHourlyMarketFinder",
    "MarketScheduler",
    "build_market_slug",
    "get_current_hour_et",
    "get_next_hour_et",
    "get_target_end_time",
    "parse_market_end_time",
    "extract_reference_price",
]
