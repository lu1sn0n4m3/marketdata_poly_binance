"""
Polymarket Market Feed (threaded).

Connects to public market WebSocket for order book updates.
Updates PolymarketCache with latest BBO data.

WS handlers are kept minimal and fast per design doc guidelines.
"""

import logging
from typing import Optional

import orjson

from .websocket_base import ThreadedWsClient
from ..caches import PolymarketCache
from ..mm_types import price_to_cents

logger = logging.getLogger(__name__)

# Default WebSocket URL
PM_MARKET_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"


class PolymarketMarketFeed(ThreadedWsClient):
    """
    Polymarket market data feed.

    Responsibilities:
    - Maintain connection with exponential backoff reconnection
    - Subscribe to YES and NO token order books
    - Parse messages minimally and update PolymarketCache
    - NO heavy computation in message handlers
    """

    def __init__(
        self,
        pm_cache: PolymarketCache,
        ws_url: str = PM_MARKET_WS_URL,
        ping_interval: int = 20,
        ping_timeout: int = 60,
    ):
        super().__init__(
            ws_url=ws_url,
            name="PolymarketMarketFeed",
            ping_interval=ping_interval,
            ping_timeout=ping_timeout,
        )
        self._cache = pm_cache

        # Token configuration (set via set_tokens)
        self._yes_token_id: str = ""
        self._no_token_id: str = ""
        self._market_id: str = ""

        # BBO state per token (updated incrementally)
        # Format: (bid_px, bid_sz, ask_px, ask_sz)
        self._yes_bbo: tuple[int, int, int, int] = (0, 0, 100, 0)
        self._no_bbo: tuple[int, int, int, int] = (0, 0, 100, 0)

    def set_tokens(self, yes_token_id: str, no_token_id: str, market_id: str) -> None:
        """
        Configure tokens to subscribe to.

        Args:
            yes_token_id: YES token ID
            no_token_id: NO token ID
            market_id: Market/condition ID
        """
        self._yes_token_id = yes_token_id
        self._no_token_id = no_token_id
        self._market_id = market_id

        # Also update the cache
        self._cache.set_market(market_id, yes_token_id, no_token_id)

        # Reset BBO state
        self._yes_bbo = (0, 0, 100, 0)
        self._no_bbo = (0, 0, 100, 0)

    def _subscribe(self) -> None:
        """Send subscription message."""
        if not self._yes_token_id:
            logger.warning("PolymarketMarketFeed: No tokens configured, skipping subscribe")
            return

        msg = {
            "type": "MARKET",
            "assets_ids": [self._yes_token_id, self._no_token_id],
            "custom_feature_enabled": False,
        }
        self._send(orjson.dumps(msg))
        logger.info(f"PolymarketMarketFeed: Subscribed to {self._market_id[:20]}...")

    def _handle_message(self, data: bytes) -> None:
        """
        Parse and handle message. MUST BE FAST.

        Updates internal BBO state and publishes to cache.
        """
        try:
            msg = orjson.loads(data)

            # Handle array of events
            if isinstance(msg, list):
                for item in msg:
                    self._process_event(item)
            else:
                self._process_event(msg)

        except Exception as e:
            # Minimal logging - avoid string formatting in hot path
            logger.debug(f"PolymarketMarketFeed: Parse error: {e}")

    def _process_event(self, data: dict) -> None:
        """Process a single event."""
        event_type = data.get("event_type")

        if event_type == "book":
            self._handle_book(data)
        elif event_type == "price_change":
            self._handle_price_change(data)
        elif event_type == "best_bid_ask":
            self._handle_best_bid_ask(data)
        # Ignore other event types (last_trade_price, tick_size_change)

    def _handle_book(self, data: dict) -> None:
        """Handle full book snapshot."""
        asset_id = data.get("asset_id", "")
        bids = data.get("bids", [])
        asks = data.get("asks", [])

        # Extract TOB - bids/asks sorted ascending, best is last
        if bids:
            best_bid = self._parse_level(bids[-1])
        else:
            best_bid = (0, 0)

        if asks:
            best_ask = self._parse_level(asks[0])
        else:
            best_ask = (100, 0)

        bbo = (best_bid[0], best_bid[1], best_ask[0], best_ask[1])

        if asset_id == self._yes_token_id:
            self._yes_bbo = bbo
        elif asset_id == self._no_token_id:
            self._no_bbo = bbo

        self._publish_to_cache()

    def _handle_price_change(self, data: dict) -> None:
        """Handle incremental price change."""
        for change in data.get("price_changes", []):
            asset_id = change.get("asset_id", "")
            best_bid = self._price_str_to_cents(change.get("best_bid"))
            best_ask = self._price_str_to_cents(change.get("best_ask"))

            # Size info not always provided in price_change, keep existing
            if asset_id == self._yes_token_id:
                self._yes_bbo = (best_bid, self._yes_bbo[1], best_ask, self._yes_bbo[3])
            elif asset_id == self._no_token_id:
                self._no_bbo = (best_bid, self._no_bbo[1], best_ask, self._no_bbo[3])

        self._publish_to_cache()

    def _handle_best_bid_ask(self, data: dict) -> None:
        """Handle direct BBO update."""
        asset_id = data.get("asset_id", "")
        best_bid = self._price_str_to_cents(data.get("best_bid"))
        best_ask = self._price_str_to_cents(data.get("best_ask"))

        if asset_id == self._yes_token_id:
            self._yes_bbo = (best_bid, self._yes_bbo[1], best_ask, self._yes_bbo[3])
        elif asset_id == self._no_token_id:
            self._no_bbo = (best_bid, self._no_bbo[1], best_ask, self._no_bbo[3])

        self._publish_to_cache()

    def _publish_to_cache(self) -> None:
        """Publish current state to PolymarketCache."""
        self._cache.update_from_ws(
            yes_bbo=self._yes_bbo,
            no_bbo=self._no_bbo,
            market_id=self._market_id,
            yes_token_id=self._yes_token_id,
            no_token_id=self._no_token_id,
        )

    @staticmethod
    def _parse_level(level: dict) -> tuple[int, int]:
        """Parse price level to (price_cents, size)."""
        price_str = level.get("price", "0")
        size_str = level.get("size", "0")
        try:
            price = round(float(price_str) * 100)
            size = int(float(size_str))
            return (price, size)
        except (ValueError, TypeError):
            return (0, 0)

    @staticmethod
    def _price_str_to_cents(price_str: Optional[str]) -> int:
        """Convert price string to cents."""
        if not price_str:
            return 0
        try:
            return round(float(price_str) * 100)
        except (ValueError, TypeError):
            return 0

    def _on_connect(self) -> None:
        """Called after connection established."""
        logger.info(f"PolymarketMarketFeed: Ready, monitoring {self._market_id[:30]}...")

    def _on_disconnect(self) -> None:
        """Called after disconnection."""
        # Clear cache on disconnect to avoid stale data
        pass  # Keep cache for now, staleness detection handles this
