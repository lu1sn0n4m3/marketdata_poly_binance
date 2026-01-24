"""
Trading System - Binary CLOB Market-Making for Polymarket

A simplified, efficient market-making system for Polymarket Bitcoin hourly markets.
"""

__version__ = "0.1.0"

# Core types
from .types import (
    # Enums
    Token,
    Side,
    OrderStatus,
    QuoteMode,
    ExecutorEventType,
    GatewayActionType,
    # Snapshot types
    MarketSnapshotMeta,
    PolymarketBookTop,
    PolymarketBookSnapshot,
    BinanceSnapshot,
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

# Snapshot caches
from .snapshot_store import LatestSnapshotStore
from .caches import PolymarketCache, BinanceCache

# Data feeds
from .feeds import (
    ExponentialBackoff,
    ThreadedWsClient,
    BinanceFeed,
    PolymarketMarketFeed,
    PM_MARKET_WS_URL,
    PolymarketUserFeed,
    PM_USER_WS_URL,
)

# REST client and Gateway
from .pm_rest_client import PolymarketRestClient, OrderResult, CancelResult, OrderType
from .gateway import Gateway, GatewayWorker, GatewayStats, ActionDeque

# Strategy
from .strategy import (
    Strategy,
    DefaultMMStrategy,
    StrategyRunner,
    StrategyInput,
    StrategyConfig,
    IntentMailbox,
    DummyStrategy,
    DummyTightStrategy,
)

# Executor (new package)
from .executor import (
    ExecutorActor,
    ExecutorState,
    ExecutorPolicies,
    MinSizePolicy,
    OrderKind,
    PnLTracker,
)

# Application
from .config import AppConfig
from .app import MMApplication

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
    "PolymarketBookTop",
    "PolymarketBookSnapshot",
    "BinanceSnapshot",
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
    # Snapshot caches
    "LatestSnapshotStore",
    "PolymarketCache",
    "BinanceCache",
    # Data feeds
    "ExponentialBackoff",
    "ThreadedWsClient",
    "PolymarketMarketFeed",
    "PM_MARKET_WS_URL",
    "PolymarketUserFeed",
    "PM_USER_WS_URL",
    "BinanceFeed",
    # REST client and Gateway
    "PolymarketRestClient",
    "OrderResult",
    "CancelResult",
    "OrderType",
    "Gateway",
    "GatewayWorker",
    "GatewayStats",
    "ActionDeque",
    # Strategy
    "Strategy",
    "DefaultMMStrategy",
    "DummyStrategy",
    "DummyTightStrategy",
    "StrategyRunner",
    "StrategyInput",
    "StrategyConfig",
    "IntentMailbox",
    # Executor
    "ExecutorActor",
    "ExecutorState",
    "ExecutorPolicies",
    "MinSizePolicy",
    "OrderKind",
    "PnLTracker",
    # Application
    "AppConfig",
    "MMApplication",
]
