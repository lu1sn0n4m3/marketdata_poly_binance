# Task 03: Polymarket Market WebSocket Client

**Priority:** Critical (Data Feed)
**Estimated Complexity:** High
**Dependencies:** Task 01 (Data Types), Task 02 (Caches)

---

## Objective

Implement a lightweight, reconnection-resilient WebSocket client for Polymarket market data (order book updates). This feeds the PMCache.

---

## Context from Design Documents

From `polymarket_mm_efficiency_design.md` Section 6:

> WS handlers should do the minimum and return:
> - Parse message quickly
> - Update cache (build snapshot → atomic swap)
> - For user events: push a small structured event into Executor high-priority queue
>
> **WS handler anti-patterns** (avoid):
> - Computing features on every message
> - Heavy logging / string formatting
> - Calling blocking I/O
> - Building large nested objects repeatedly

---

## Polymarket WebSocket API Reference

**Connection URL:** `wss://ws-subscriptions-clob.polymarket.com/ws/market`

**Subscription Message:**
```json
{
  "type": "MARKET",
  "assets_ids": ["<yes_token_id>", "<no_token_id>"]
}
```

**Dynamic Subscription Update:**
```json
{
  "assets_ids": ["<token_id>"],
  "operation": "subscribe"
}
```

### Message Types

**1. Book Snapshot (`book`):**
```json
{
  "event_type": "book",
  "asset_id": "123456...",
  "market": "0x5f65...",
  "timestamp": 1700000000000,
  "bids": [{"price": "0.50", "size": "100"}],
  "asks": [{"price": "0.52", "size": "200"}]
}
```

**2. Price Change (`price_change`):**
```json
{
  "event_type": "price_change",
  "market": "0x5f65...",
  "price_changes": [{
    "asset_id": "123...",
    "price": "0.51",
    "size": "50",
    "side": "BUY",
    "best_bid": "0.50",
    "best_ask": "0.52"
  }],
  "timestamp": 1700000000001
}
```

**3. Best Bid/Ask (`best_bid_ask`):**
```json
{
  "event_type": "best_bid_ask",
  "asset_id": "123...",
  "market": "0x5f65...",
  "best_bid": "0.50",
  "best_ask": "0.52",
  "spread": "0.02"
}
```

**4. Last Trade Price (`last_trade_price`):**
```json
{
  "event_type": "last_trade_price",
  "asset_id": "123...",
  "price": "0.51",
  "size": "25",
  "side": "BUY"
}
```

---

## Implementation Checklist

### 1. Class Structure

```python
class PolymarketMarketWsClient:
    """
    Polymarket market data WebSocket client.

    Responsibilities:
    - Maintain connection with exponential backoff reconnection
    - Subscribe to YES and NO token order books
    - Parse messages minimally and update PMCache
    - NO heavy computation in message handlers
    """

    def __init__(
        self,
        ws_url: str,
        pm_cache: PMCache,
        ping_interval: int = 20,
        ping_timeout: int = 60,
    ):
        self._ws_url = ws_url
        self._cache = pm_cache
        self._ping_interval = ping_interval
        self._ping_timeout = ping_timeout

        # Token configuration (set via set_tokens)
        self._yes_token_id: str = ""
        self._no_token_id: str = ""
        self._market_id: str = ""

        # Connection state
        self._ws: Optional[websockets.WebSocketClientProtocol] = None
        self._connected = False
        self._reconnect_count = 0
        self._backoff = ExponentialBackoff()

        # BBO state per token (updated incrementally)
        self._yes_bbo: tuple[int, int, int, int] = (0, 0, 100, 0)
        self._no_bbo: tuple[int, int, int, int] = (0, 0, 100, 0)

        # Stop signal
        self._stop_event: Optional[asyncio.Event] = None

    def set_tokens(self, yes_token_id: str, no_token_id: str, market_id: str) -> None:
        """Configure tokens to subscribe to."""
        self._yes_token_id = yes_token_id
        self._no_token_id = no_token_id
        self._market_id = market_id

    async def run(self, stop_event: asyncio.Event) -> None:
        """Main run loop with reconnection."""
        self._stop_event = stop_event

        while not stop_event.is_set():
            try:
                await self._connect_and_listen()
            except websockets.ConnectionClosed:
                self._connected = False
                self._reconnect_count += 1
                delay = self._backoff.next_delay()
                logger.warning(f"PM Market WS disconnected, reconnecting in {delay:.1f}s")
                await asyncio.sleep(delay)
            except Exception as e:
                logger.error(f"PM Market WS error: {e}")
                await asyncio.sleep(1)

    async def _connect_and_listen(self) -> None:
        """Connect and process messages."""
        async with websockets.connect(
            self._ws_url,
            ping_interval=self._ping_interval,
            ping_timeout=self._ping_timeout,
        ) as ws:
            self._ws = ws
            self._connected = True
            self._backoff.reset()

            # Subscribe to tokens
            await self._subscribe()

            # Message loop
            async for message in ws:
                if self._stop_event and self._stop_event.is_set():
                    break
                self._handle_message(message)

    async def _subscribe(self) -> None:
        """Send subscription message."""
        if not self._yes_token_id:
            return

        msg = {
            "type": "MARKET",
            "assets_ids": [self._yes_token_id, self._no_token_id],
        }
        await self._ws.send(orjson.dumps(msg))

    def _handle_message(self, raw: str | bytes) -> None:
        """
        Parse and handle message. MUST BE FAST.

        Updates internal BBO state and publishes to cache.
        """
        try:
            data = orjson.loads(raw)
            event_type = data.get("event_type")

            if event_type == "book":
                self._handle_book(data)
            elif event_type == "price_change":
                self._handle_price_change(data)
            elif event_type == "best_bid_ask":
                self._handle_best_bid_ask(data)
            # Ignore other event types

        except Exception as e:
            logger.debug(f"Failed to parse PM market message: {e}")

    def _handle_book(self, data: dict) -> None:
        """Handle full book snapshot."""
        asset_id = data.get("asset_id", "")
        bids = data.get("bids", [])
        asks = data.get("asks", [])

        # Extract TOB
        best_bid = self._parse_level(bids[0]) if bids else (0, 0)
        best_ask = self._parse_level(asks[0]) if asks else (100, 0)

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
            best_bid = self._price_to_cents(change.get("best_bid"))
            best_ask = self._price_to_cents(change.get("best_ask"))

            # Size from price_change is delta, keep existing sizes
            if asset_id == self._yes_token_id:
                self._yes_bbo = (best_bid, self._yes_bbo[1], best_ask, self._yes_bbo[3])
            elif asset_id == self._no_token_id:
                self._no_bbo = (best_bid, self._no_bbo[1], best_ask, self._no_bbo[3])

        self._publish_to_cache()

    def _handle_best_bid_ask(self, data: dict) -> None:
        """Handle BBO update."""
        asset_id = data.get("asset_id", "")
        best_bid = self._price_to_cents(data.get("best_bid"))
        best_ask = self._price_to_cents(data.get("best_ask"))

        if asset_id == self._yes_token_id:
            self._yes_bbo = (best_bid, self._yes_bbo[1], best_ask, self._yes_bbo[3])
        elif asset_id == self._no_token_id:
            self._no_bbo = (best_bid, self._no_bbo[1], best_ask, self._no_bbo[3])

        self._publish_to_cache()

    def _publish_to_cache(self) -> None:
        """Publish current state to PMCache."""
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
        price = int(float(level.get("price", "0")) * 100)
        size = int(float(level.get("size", "0")))
        return (price, size)

    @staticmethod
    def _price_to_cents(price_str: Optional[str]) -> int:
        """Convert price string to cents."""
        if not price_str:
            return 0
        return int(float(price_str) * 100)
```

### 2. Exponential Backoff (reuse from binance_pricer)

```python
class ExponentialBackoff:
    """Exponential backoff with jitter for reconnection."""

    def __init__(self, min_seconds: float = 1.0, max_seconds: float = 60.0):
        self.min_seconds = min_seconds
        self.max_seconds = max_seconds
        self._attempts = 0

    def next_delay(self) -> float:
        delay = min(self.min_seconds * (2 ** self._attempts), self.max_seconds)
        self._attempts += 1
        # Add jitter
        return delay * (0.5 + random.random() * 0.5)

    def reset(self) -> None:
        self._attempts = 0
```

---

## File Location

Create: `services/polymarket_trader/src/polymarket_trader/pm_market_ws.py`

---

## Acceptance Criteria

- [ ] Connects to Polymarket market WebSocket
- [ ] Subscribes to YES and NO token order books
- [ ] Parses book, price_change, and best_bid_ask messages
- [ ] Updates PMCache atomically on each relevant message
- [ ] Reconnects with exponential backoff on disconnect
- [ ] Handler code is minimal and fast (no computation)
- [ ] Uses `orjson` for fast JSON parsing

---

## Design Notes

From `polymarket_mm_efficiency_design.md` Section 6:

> **Keep WS handlers extremely fast**
> - Parse message quickly
> - Update cache (build snapshot → atomic swap)
> - WS handler anti-patterns: computing features, heavy logging, blocking I/O

---

## Testing

Create `tests/test_pm_market_ws.py`:
- Test message parsing for each event type
- Test price_to_cents conversion
- Test cache update on message receipt
- Test reconnection behavior (mock WebSocket)
