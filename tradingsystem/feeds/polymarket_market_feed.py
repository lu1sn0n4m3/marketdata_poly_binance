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
    - Maintain internal book for YES token to get BBO sizes
    - NO heavy computation in message handlers

    Book Architecture:
    - Internal book: dict[price_cents, size] for YES bids and asks
    - On 'book' snapshot: rebuild entire internal book
    - On 'price_change': update specific level, look up sizes at BBO prices
    - NO side is derived from YES (NO bid = 100 - YES ask, sizes match)
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

        # Internal book for YES token: {price_cents: size}
        # Used to look up sizes when BBO prices change
        self._yes_bids: dict[int, int] = {}
        self._yes_asks: dict[int, int] = {}

        # Cached BBO (bid_px, bid_sz, ask_px, ask_sz) - atomically swapped
        self._yes_bbo: tuple[int, int, int, int] = (0, 0, 100, 0)

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

        # Reset internal book and BBO
        self._yes_bids.clear()
        self._yes_asks.clear()
        self._yes_bbo = (0, 0, 100, 0)

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
        """
        Handle full book snapshot.

        Rebuilds internal book and computes BBO with sizes.
        Only maintains book for YES token; NO is derived.
        """
        asset_id = data.get("asset_id", "")

        # Only process YES token - NO is derived
        if asset_id != self._yes_token_id:
            return

        bids = data.get("bids", [])
        asks = data.get("asks", [])

        # Rebuild internal book from snapshot
        self._yes_bids.clear()
        self._yes_asks.clear()

        for level in bids:
            px, sz = self._parse_level(level)
            if sz > 0:
                self._yes_bids[px] = sz

        for level in asks:
            px, sz = self._parse_level(level)
            if sz > 0:
                self._yes_asks[px] = sz

        # Compute BBO from internal book
        self._update_yes_bbo_from_book()
        self._publish_to_cache()

    def _handle_price_change(self, data: dict) -> None:
        """
        Handle incremental price change.

        Updates internal book at specific price level, then looks up
        sizes at the new BBO prices (provided in the message).
        Only processes YES token; NO is derived.
        """
        for change in data.get("price_changes", []):
            asset_id = change.get("asset_id", "")

            # Only process YES token - NO is derived
            if asset_id != self._yes_token_id:
                continue

            # Update internal book at the specific price level
            side = change.get("side", "")
            price_str = change.get("price")
            size_str = change.get("size")

            if price_str is not None and size_str is not None:
                px = self._price_str_to_cents(price_str)
                try:
                    sz = int(float(size_str))
                except (ValueError, TypeError):
                    sz = 0

                if side == "BUY":
                    if sz > 0:
                        self._yes_bids[px] = sz
                    elif px in self._yes_bids:
                        del self._yes_bids[px]
                elif side == "SELL":
                    if sz > 0:
                        self._yes_asks[px] = sz
                    elif px in self._yes_asks:
                        del self._yes_asks[px]

            # Get new BBO prices from message, look up sizes in internal book
            raw_bid = change.get("best_bid")
            raw_ask = change.get("best_ask")

            if raw_bid is not None or raw_ask is not None:
                bid_px = self._price_str_to_cents(raw_bid) if raw_bid else self._yes_bbo[0]
                ask_px = self._price_str_to_cents(raw_ask) if raw_ask else self._yes_bbo[2]

                # Look up sizes at BBO prices
                bid_sz = self._yes_bids.get(bid_px, 0)
                ask_sz = self._yes_asks.get(ask_px, 0)

                self._yes_bbo = (bid_px, bid_sz, ask_px, ask_sz)

        self._publish_to_cache()

    def _handle_best_bid_ask(self, data: dict) -> None:
        """
        Handle direct BBO update.

        Only processes YES token; looks up sizes in internal book.
        """
        asset_id = data.get("asset_id", "")

        # Only process YES token - NO is derived
        if asset_id != self._yes_token_id:
            return

        raw_bid = data.get("best_bid")
        raw_ask = data.get("best_ask")

        bid_px = self._price_str_to_cents(raw_bid) if raw_bid else self._yes_bbo[0]
        ask_px = self._price_str_to_cents(raw_ask) if raw_ask else self._yes_bbo[2]

        # Look up sizes in internal book
        bid_sz = self._yes_bids.get(bid_px, 0)
        ask_sz = self._yes_asks.get(ask_px, 0)

        self._yes_bbo = (bid_px, bid_sz, ask_px, ask_sz)
        self._publish_to_cache()

    def _update_yes_bbo_from_book(self) -> None:
        """
        Compute YES BBO from internal book.

        Called after rebuilding book from snapshot.
        """
        # Best bid = highest price with size > 0
        if self._yes_bids:
            bid_px = max(self._yes_bids.keys())
            bid_sz = self._yes_bids[bid_px]
        else:
            bid_px, bid_sz = 0, 0

        # Best ask = lowest price with size > 0
        if self._yes_asks:
            ask_px = min(self._yes_asks.keys())
            ask_sz = self._yes_asks[ask_px]
        else:
            ask_px, ask_sz = 100, 0

        self._yes_bbo = (bid_px, bid_sz, ask_px, ask_sz)

    def _publish_to_cache(self) -> None:
        """
        Publish current state to PolymarketCache.

        Derives NO BBO from YES (complementary relationship):
        - NO bid = 100 - YES ask (willing to buy NO = willing to sell YES)
        - NO ask = 100 - YES bid (willing to sell NO = willing to buy YES)
        - Sizes match the corresponding YES side
        """
        yes_bid_px, yes_bid_sz, yes_ask_px, yes_ask_sz = self._yes_bbo

        # Derive NO from YES
        no_bid_px = 100 - yes_ask_px  # NO bid = 100 - YES ask
        no_bid_sz = yes_ask_sz        # Same liquidity
        no_ask_px = 100 - yes_bid_px  # NO ask = 100 - YES bid
        no_ask_sz = yes_bid_sz        # Same liquidity
        no_bbo = (no_bid_px, no_bid_sz, no_ask_px, no_ask_sz)

        # Debug: log extreme spreads
        yes_spread = yes_ask_px - yes_bid_px
        if yes_spread > 50:  # More than 50 cent spread
            logger.warning(
                f"[WIDE_SPREAD] YES bid={yes_bid_px} ask={yes_ask_px} "
                f"spread={yes_spread}c"
            )

        self._cache.update_from_ws(
            yes_bbo=self._yes_bbo,
            no_bbo=no_bbo,
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
