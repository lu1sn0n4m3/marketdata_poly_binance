"""
Data feeds for the trading system.

Contains WebSocket and HTTP polling feeds for market data:
- BinanceFeed: WebSocket feed for real-time Binance BBO and trades
- BinancePricerPoller: Optional HTTP poller for pricer enrichment (p_yes, features)
- PricerEnrichment: Thread-safe container for pricer-computed fields
- PolymarketMarketFeed: WebSocket feed for Polymarket order book data
- PolymarketUserFeed: Authenticated WebSocket feed for user events (fills, acks)
"""

from .websocket_base import ExponentialBackoff, ThreadedWsClient
from .binance_feed import BinanceFeed, BinancePricerPoller, PricerEnrichment, BINANCE_WS_URL
from .polymarket_market_feed import PolymarketMarketFeed, PM_MARKET_WS_URL
from .polymarket_user_feed import PolymarketUserFeed, PM_USER_WS_URL

__all__ = [
    "ExponentialBackoff",
    "ThreadedWsClient",
    "BinanceFeed",
    "BinancePricerPoller",
    "PricerEnrichment",
    "BINANCE_WS_URL",
    "PolymarketMarketFeed",
    "PM_MARKET_WS_URL",
    "PolymarketUserFeed",
    "PM_USER_WS_URL",
]
