# Task 04: Polymarket User WebSocket Client

**Priority:** Critical (Order Events)
**Estimated Complexity:** High
**Dependencies:** Task 01 (Data Types)

---

## Objective

Implement an authenticated WebSocket client for Polymarket user events (order acks, fills, cancels). These events are **lossless** and feed directly into the Executor's high-priority queue.

---

## Context from Design Documents

From `polymarket_mm_efficiency_design.md` Section 3:

> **Category A — MUST BE LOSSLESS (never drop)**
> These are required to maintain correct order/inventory state:
> - Fills (partial or full)
> - Order acks (accepted/rejected)
> - Cancel acks
> - Cancel-all results
> - Gateway errors
>
> If you drop these, the Executor's view diverges from reality.

From Section 6:

> For user events: push a small structured event into Executor high-priority queue

---

## Polymarket User WebSocket API Reference

**Connection URL:** `wss://ws-subscriptions-clob.polymarket.com/ws/user`

**Authentication Required:** Yes (API key, secret, passphrase)

**Subscription Message:**
```json
{
  "type": "USER",
  "auth": {
    "apiKey": "<api_key>",
    "secret": "<api_secret>",
    "passphrase": "<passphrase>"
  },
  "markets": ["0x5f65..."]
}
```

### Message Types

**1. Order Placement (`order` with type=PLACEMENT):**
```json
{
  "event_type": "order",
  "type": "PLACEMENT",
  "id": "order_id_123",
  "price": "0.50",
  "original_size": "100",
  "size_matched": "0",
  "outcome": "Yes",
  "market": "0x5f65...",
  "side": "BUY",
  "timestamp": "1700000000000"
}
```

**2. Order Update (`order` with type=UPDATE):**
```json
{
  "event_type": "order",
  "type": "UPDATE",
  "id": "order_id_123",
  "price": "0.50",
  "original_size": "100",
  "size_matched": "25",
  "outcome": "Yes",
  "market": "0x5f65...",
  "side": "BUY",
  "timestamp": "1700000000001",
  "associate_trades": ["trade_123"]
}
```

**3. Order Cancellation (`order` with type=CANCELLATION):**
```json
{
  "event_type": "order",
  "type": "CANCELLATION",
  "id": "order_id_123",
  "price": "0.50",
  "original_size": "100",
  "size_matched": "10",
  "outcome": "Yes",
  "market": "0x5f65...",
  "side": "BUY",
  "timestamp": "1700000000002"
}
```

**4. Trade Fill (`trade`):**
```json
{
  "event_type": "trade",
  "type": "TRADE",
  "id": "trade_123",
  "status": "CONFIRMED",
  "price": "0.50",
  "size": "25",
  "side": "BUY",
  "outcome": "Yes",
  "market": "0x5f65...",
  "taker_order_id": "order_456",
  "maker_orders": [{
    "order_id": "order_123",
    "matched_amount": "25",
    "price": "0.50",
    "outcome": "Yes",
    "asset_id": "token_id_yes"
  }],
  "matchtime": "1700000000000",
  "timestamp": "1700000000001"
}
```

**Trade Statuses:**
- `MATCHED`: Order matched, pending settlement
- `MINED`: Transaction mined on chain
- `CONFIRMED`: Fully confirmed
- `RETRYING`: Settlement retry in progress
- `FAILED`: Settlement failed

---

## Implementation Checklist

### 1. Class Structure

```python
class PolymarketUserWsClient:
    """
    Polymarket user events WebSocket client (authenticated).

    Responsibilities:
    - Maintain authenticated connection
    - Parse order/trade events
    - Push events to Executor high-priority queue (LOSSLESS)
    - Handle reconnection with cancel-all trigger
    """

    def __init__(
        self,
        ws_url: str,
        api_key: str,
        api_secret: str,
        passphrase: str,
        executor_queue: queue.Queue,
        on_reconnect: Optional[Callable[[], Awaitable[None]]] = None,
        ping_interval: int = 20,
        ping_timeout: int = 60,
    ):
        self._ws_url = ws_url
        self._api_key = api_key
        self._api_secret = api_secret
        self._passphrase = passphrase
        self._executor_queue = executor_queue
        self._on_reconnect = on_reconnect
        self._ping_interval = ping_interval
        self._ping_timeout = ping_timeout

        # Market configuration
        self._market_ids: list[str] = []

        # Connection state
        self._ws: Optional[websockets.WebSocketClientProtocol] = None
        self._connected = False
        self._reconnect_count = 0
        self._backoff = ExponentialBackoff()

        # Stop signal
        self._stop_event: Optional[asyncio.Event] = None

    def set_markets(self, market_ids: list[str]) -> None:
        """Configure markets to subscribe to."""
        self._market_ids = market_ids

    def set_auth(self, api_key: str, api_secret: str, passphrase: str) -> None:
        """Update authentication credentials."""
        self._api_key = api_key
        self._api_secret = api_secret
        self._passphrase = passphrase

    async def run(self, stop_event: asyncio.Event) -> None:
        """Main run loop with reconnection."""
        self._stop_event = stop_event

        while not stop_event.is_set():
            try:
                await self._connect_and_listen()
            except websockets.ConnectionClosed:
                self._connected = False
                self._reconnect_count += 1

                # CRITICAL: Trigger cancel-all on reconnect
                if self._on_reconnect:
                    logger.warning("User WS disconnected - triggering reconnect handler")
                    try:
                        await self._on_reconnect()
                    except Exception as e:
                        logger.error(f"Reconnect handler failed: {e}")

                delay = self._backoff.next_delay()
                logger.warning(f"PM User WS reconnecting in {delay:.1f}s")
                await asyncio.sleep(delay)
            except Exception as e:
                logger.error(f"PM User WS error: {e}")
                await asyncio.sleep(1)

    async def _connect_and_listen(self) -> None:
        """Connect, authenticate, and process messages."""
        async with websockets.connect(
            self._ws_url,
            ping_interval=self._ping_interval,
            ping_timeout=self._ping_timeout,
        ) as ws:
            self._ws = ws
            self._connected = True
            self._backoff.reset()

            # Subscribe with authentication
            await self._subscribe()

            # Message loop
            async for message in ws:
                if self._stop_event and self._stop_event.is_set():
                    break
                self._handle_message(message)

    async def _subscribe(self) -> None:
        """Send authenticated subscription message."""
        msg = {
            "type": "USER",
            "auth": {
                "apiKey": self._api_key,
                "secret": self._api_secret,
                "passphrase": self._passphrase,
            },
            "markets": self._market_ids,
        }
        await self._ws.send(orjson.dumps(msg))

    def _handle_message(self, raw: str | bytes) -> None:
        """
        Parse and handle message. Push events to Executor queue.

        These events MUST NOT be dropped.
        """
        try:
            data = orjson.loads(raw)
            event_type = data.get("event_type")

            if event_type == "order":
                self._handle_order_event(data)
            elif event_type == "trade":
                self._handle_trade_event(data)

        except Exception as e:
            logger.error(f"Failed to parse PM user message: {e}")

    def _handle_order_event(self, data: dict) -> None:
        """Handle order placement/update/cancellation."""
        order_type = data.get("type", "")
        now_ms = time.monotonic_ns() // 1_000_000

        if order_type == "PLACEMENT":
            event = OrderAckEvent(
                event_type=ExecutorEventType.ORDER_ACK,
                ts_local_ms=now_ms,
                order_id=data.get("id", ""),
                status=OrderStatus.WORKING,
                side=Side.BUY if data.get("side") == "BUY" else Side.SELL,
                price=self._price_to_cents(data.get("price")),
                size=int(float(data.get("original_size", "0"))),
                token=self._outcome_to_token(data.get("outcome")),
            )
            self._enqueue(event)

        elif order_type == "UPDATE":
            # Partial fill indicator
            size_matched = int(float(data.get("size_matched", "0")))
            event = OrderUpdateEvent(
                event_type=ExecutorEventType.ORDER_ACK,
                ts_local_ms=now_ms,
                order_id=data.get("id", ""),
                filled_sz=size_matched,
            )
            self._enqueue(event)

        elif order_type == "CANCELLATION":
            event = CancelAckEvent(
                event_type=ExecutorEventType.CANCEL_ACK,
                ts_local_ms=now_ms,
                order_id=data.get("id", ""),
                success=True,
            )
            self._enqueue(event)

    def _handle_trade_event(self, data: dict) -> None:
        """Handle trade fill event."""
        status = data.get("status", "")
        now_ms = time.monotonic_ns() // 1_000_000

        # Only process confirmed fills
        if status not in ("CONFIRMED", "MINED"):
            return

        event = FillEvent(
            event_type=ExecutorEventType.FILL,
            ts_local_ms=now_ms,
            order_id=data.get("taker_order_id", ""),
            token=self._outcome_to_token(data.get("outcome")),
            side=Side.BUY if data.get("side") == "BUY" else Side.SELL,
            price=self._price_to_cents(data.get("price")),
            size=int(float(data.get("size", "0"))),
            fee=0.0,  # Fee not provided in this message
            ts_exchange=int(data.get("timestamp", "0")),
        )
        self._enqueue(event)

        # Also handle maker fills if we're the maker
        for maker in data.get("maker_orders", []):
            maker_event = FillEvent(
                event_type=ExecutorEventType.FILL,
                ts_local_ms=now_ms,
                order_id=maker.get("order_id", ""),
                token=self._outcome_to_token(maker.get("outcome")),
                side=Side.SELL,  # Maker is always passive side
                price=self._price_to_cents(maker.get("price")),
                size=int(float(maker.get("matched_amount", "0"))),
                fee=0.0,
                ts_exchange=int(data.get("timestamp", "0")),
            )
            self._enqueue(maker_event)

    def _enqueue(self, event: ExecutorEvent) -> None:
        """
        Enqueue event to Executor. MUST NOT DROP.

        Uses put() which blocks if queue is full (should never happen
        with properly sized high-priority queue).
        """
        try:
            self._executor_queue.put_nowait(event)
        except queue.Full:
            # This should never happen - log error and put with blocking
            logger.error("Executor queue full! Blocking to enqueue user event")
            self._executor_queue.put(event)

    @staticmethod
    def _price_to_cents(price_str: Optional[str]) -> int:
        if not price_str:
            return 0
        return int(float(price_str) * 100)

    @staticmethod
    def _outcome_to_token(outcome: Optional[str]) -> Token:
        if outcome and outcome.lower() in ("yes", "up"):
            return Token.YES
        return Token.NO
```

---

## File Location

Create: `services/polymarket_trader/src/polymarket_trader/pm_user_ws.py`

---

## Acceptance Criteria

- [ ] Connects with API key authentication
- [ ] Parses order placement, update, and cancellation events
- [ ] Parses trade fill events (taker and maker)
- [ ] Events are **never dropped** - use blocking put if necessary
- [ ] Triggers reconnect handler (cancel-all) on disconnect
- [ ] Reconnects with exponential backoff
- [ ] Uses `orjson` for fast parsing

---

## Critical Safety Note

From `polymarket_mm_efficiency_design.md` Section 5:

> **Resync on uncertainty:**
> If you miss user events due to disconnect:
> - Simplest safe policy: **cancel-all on reconnect**, then restart quoting from scratch
>
> For a first production system: **cancel-all on reconnect** is recommended because it is deterministic and safe.

The `on_reconnect` callback MUST trigger a cancel-all in the Executor.

---

## Event Types to Emit

1. **OrderAckEvent**: When order is placed (PLACEMENT)
2. **OrderUpdateEvent**: When order is partially filled (UPDATE)
3. **CancelAckEvent**: When order is canceled (CANCELLATION)
4. **FillEvent**: When trade is confirmed (CONFIRMED/MINED status)

---

## Testing

Create `tests/test_pm_user_ws.py`:
- Test order event parsing (all types)
- Test trade event parsing
- Test enqueue never drops (mock full queue)
- Test reconnect handler is called on disconnect
- Test authentication message format
