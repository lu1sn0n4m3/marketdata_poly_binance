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

# Snapshot caches
from .snapshot_store import LatestSnapshotStore
from .pm_cache import PMCache
from .bn_cache import BNCache

# WebSocket clients
from .ws_base import ExponentialBackoff, ThreadedWsClient
from .pm_market_ws import PolymarketMarketWsClient, PM_MARKET_WS_URL
from .pm_user_ws import PolymarketUserWsClient, PM_USER_WS_URL

# REST client and Gateway
from .pm_rest_client import PolymarketRestClient, OrderResult, CancelResult, OrderType
from .gateway import Gateway, GatewayWorker, GatewayStats

# Strategy
from .strategy import Strategy, DefaultMMStrategy, StrategyRunner, StrategyInput, StrategyConfig, IntentMailbox
from .dummy_strategy import DummyStrategy

# Executor
from .executor import ExecutorActor, OrderMaterializer, ExecutorState

# Application
from .config import AppConfig
from .bn_poller import BinanceSnapshotPoller
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
    # Snapshot caches
    "LatestSnapshotStore",
    "PMCache",
    "BNCache",
    # WebSocket clients
    "ExponentialBackoff",
    "ThreadedWsClient",
    "PolymarketMarketWsClient",
    "PM_MARKET_WS_URL",
    "PolymarketUserWsClient",
    "PM_USER_WS_URL",
    # REST client and Gateway
    "PolymarketRestClient",
    "OrderResult",
    "CancelResult",
    "OrderType",
    "Gateway",
    "GatewayWorker",
    "GatewayStats",
    # Strategy
    "Strategy",
    "DefaultMMStrategy",
    "DummyStrategy",
    "StrategyRunner",
    "StrategyInput",
    "StrategyConfig",
    "IntentMailbox",
    # Executor
    "ExecutorActor",
    "OrderMaterializer",
    "ExecutorState",
    # Application
    "AppConfig",
    "BinanceSnapshotPoller",
    "MMApplication",
]
