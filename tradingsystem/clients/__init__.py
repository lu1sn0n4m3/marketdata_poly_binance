"""
Polymarket API clients for market discovery and order operations.

This module contains:
- GammaClient: Market discovery via Gamma API
- BitcoinHourlyMarketFinder: Bitcoin hourly market finding
- PolymarketRestClient: REST interface for order operations
"""

from .gamma_client import GammaClient, GammaAPIError
from .market_finder import (
    BitcoinHourlyMarketFinder,
    MarketScheduler,
    build_market_slug,
    get_current_hour_et,
    get_next_hour_et,
    extract_reference_price,
)
from .polymarket_rest_client import (
    PolymarketRestClient,
    OrderResult,
    CancelResult,
    OrderType,
)

__all__ = [
    "GammaClient",
    "GammaAPIError",
    "BitcoinHourlyMarketFinder",
    "MarketScheduler",
    "build_market_slug",
    "get_current_hour_et",
    "get_next_hour_et",
    "extract_reference_price",
    "PolymarketRestClient",
    "OrderResult",
    "CancelResult",
    "OrderType",
]
