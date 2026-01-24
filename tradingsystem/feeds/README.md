# Feeds Module

Threaded data feeds for market data and user events. All feeds run in background daemon threads.

## Architecture

```
websocket_base.py          Base class for WebSocket feeds
    |
    +-- polymarket_market_feed.py   Public market data (order book)
    +-- polymarket_user_feed.py     Authenticated user events (fills)

binance_feed.py            HTTP polling feed (standalone, no base class)
```

## websocket_base.py

`ThreadedWsClient` is the abstract base class for all WebSocket feeds.

**Features:**
- Runs in a daemon thread via `start()`/`stop()`
- Automatic reconnection with exponential backoff (1s-60s + jitter)
- Ping/pong keepalive handled by websocket-client library
- Thread-safe connection state via `connected` property

**Subclass Requirements:**
```python
class MyFeed(ThreadedWsClient):
    def _subscribe(self) -> None:
        """Send subscription message after connection."""
        self._send(orjson.dumps({"type": "subscribe", ...}))

    def _handle_message(self, data: bytes) -> None:
        """Handle incoming message. MUST BE FAST - no heavy computation."""
        msg = orjson.loads(data)
        # process msg

    # Optional hooks:
    def _on_connect(self) -> None: ...
    def _on_disconnect(self) -> None: ...
```

## BinanceFeed

HTTP polling feed for Binance prices. Updates `BinanceCache`.

**Usage:**
```python
from tradingsystem.feeds import BinanceFeed
from tradingsystem.caches import BinanceCache

cache = BinanceCache()
feed = BinanceFeed(
    cache=cache,
    url="http://localhost:8080/snapshot/latest",
    poll_hz=20,  # 20 requests/second
)

feed.start()
# ... cache now receives updates ...
feed.stop()
```

**Stats:** Access `feed.stats` for poll/success/error counts.

## PolymarketMarketFeed

Public WebSocket feed for Polymarket order book data. Updates `PolymarketCache` with BBO (best bid/offer).

**Usage:**
```python
from tradingsystem.feeds import PolymarketMarketFeed
from tradingsystem.caches import PolymarketCache

cache = PolymarketCache()
feed = PolymarketMarketFeed(pm_cache=cache)

# Configure tokens before starting
feed.set_tokens(
    yes_token_id="0x123...",
    no_token_id="0x456...",
    market_id="condition_id_here",
)

feed.start()
# ... cache now receives BBO updates ...
feed.stop()
```

**Event Types Handled:**
- `book` - Full order book snapshot
- `price_change` - Incremental BBO update
- `best_bid_ask` - Direct BBO update

## PolymarketUserFeed

Authenticated WebSocket feed for user events. Pushes events to an executor queue.

**Critical Safety:** Triggers `on_reconnect` callback on disconnect (should cancel all orders).

**Usage:**
```python
import queue
from tradingsystem.feeds import PolymarketUserFeed

event_queue = queue.Queue()

def on_reconnect():
    """Called on disconnect - cancel all orders for safety."""
    executor.cancel_all()

feed = PolymarketUserFeed(
    event_queue=event_queue,
    on_reconnect=on_reconnect,
)

# Set auth credentials
feed.set_auth(api_key="...", api_secret="...", passphrase="...")

# Configure markets to subscribe to
feed.set_markets(["condition_id_1", "condition_id_2"])

feed.start()
# ... events pushed to event_queue ...
feed.stop()
```

**Events Pushed to Queue:**
- `OrderAckEvent` - Order placed/updated (PLACEMENT, UPDATE)
- `CancelAckEvent` - Order canceled
- `FillEvent` - Trade fills (both taker and maker)

**Guarantee:** Events are NEVER dropped. If queue is full, blocks until space available.

## Typical Initialization Order

```python
from tradingsystem.caches import BinanceCache, PolymarketCache

# 1. Create caches
bn_cache = BinanceCache()
pm_cache = PolymarketCache()
event_queue = queue.Queue()

# 2. Create feeds
bn_feed = BinanceFeed(cache=bn_cache)
pm_market_feed = PolymarketMarketFeed(pm_cache=pm_cache)
pm_user_feed = PolymarketUserFeed(event_queue=event_queue, on_reconnect=cancel_all)

# 3. Configure feeds
pm_market_feed.set_tokens(yes_token, no_token, market_id)
pm_user_feed.set_auth(api_key, api_secret, passphrase)
pm_user_feed.set_markets([market_id])

# 4. Start all feeds
bn_feed.start()
pm_market_feed.start()
pm_user_feed.start()

# 5. Stop on shutdown
bn_feed.stop()
pm_market_feed.stop()
pm_user_feed.stop()
```
