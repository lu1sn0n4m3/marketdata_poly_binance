# Task 05: Gateway (REST Order Operations)

**Priority:** Critical (Order Execution)
**Estimated Complexity:** High
**Dependencies:** Task 01 (Data Types)

---

## Objective

Implement a Gateway component that handles all Polymarket REST API interactions for order placement, cancellation, and cancel-all. The Gateway runs in a separate worker to avoid blocking the Executor.

---

## Context from Design Documents

From `polymarket_mm_efficiency_design.md` Section 8:

> **Keep Executor fast: do not block on network I/O**
>
> The Executor loop must remain responsive to fills/acks and timeouts.
> Therefore avoid blocking network calls inside the Executor event handler.
>
> **Recommended pattern:**
> - Executor emits `GatewayAction`s to a Gateway worker (separate thread/task)
> - Gateway worker performs network I/O and returns `GatewayResultEvent`s to Executor

From `polymarket_mm_framework.md` Section 5:

> **Gateway**
> - The only component allowed to interact with Polymarket APIs
> - Accepts order/cancel/cancel-all requests
> - Emits acks/errors back to Executor

---

## Polymarket REST API Reference

**Base URL:** `https://clob.polymarket.com`

### Order Placement

**Endpoint:** `POST /order`

**Request:**
```json
{
  "order": {
    "salt": "<random_int>",
    "maker": "<wallet_address>",
    "signer": "<signer_address>",
    "taker": "0x0000000000000000000000000000000000000000",
    "tokenId": "<token_id>",
    "makerAmount": "<size_in_shares>",
    "takerAmount": "<cost_in_usdc>",
    "side": "BUY",
    "expiration": "<unix_timestamp>",
    "nonce": "<nonce>",
    "feeRateBps": "0",
    "signatureType": 1,
    "signature": "<eip712_signature>"
  },
  "owner": "<wallet_address>",
  "orderType": "GTC"
}
```

### Batch Order Placement

**Endpoint:** `POST /orders`

**Request:**
```json
{
  "orders": [<order1>, <order2>, ...]
}
```

### Cancel Order

**Endpoint:** `DELETE /order/<order_id>`

### Batch Cancel

**Endpoint:** `DELETE /orders`

**Request:**
```json
{
  "orderIds": ["<order_id_1>", "<order_id_2>"]
}
```

### Cancel All

**Endpoint:** `DELETE /cancel-all`

**Request:**
```json
{
  "market": "<condition_id>"
}
```

---

## Implementation Checklist

### 1. Gateway Worker Class

```python
class GatewayWorker:
    """
    Gateway worker that performs network I/O in a separate task.

    Receives GatewayActions from Executor via action_queue.
    Returns GatewayResults via result_callback.

    Runs as an async task, never blocks the Executor.
    """

    def __init__(
        self,
        rest_client: PolymarketRestClient,
        action_queue: asyncio.Queue[GatewayAction],
        result_callback: Callable[[GatewayResult], None],
        cancel_all_fast_path: bool = True,
    ):
        self._rest = rest_client
        self._action_queue = action_queue
        self._result_callback = result_callback
        self._cancel_all_fast_path = cancel_all_fast_path

        # Rate limiting
        self._last_action_ts: int = 0
        self._min_action_interval_ms: int = 50  # 20 actions/sec max

        # Stats
        self.actions_processed: int = 0
        self.errors: int = 0

    async def run(self, stop_event: asyncio.Event) -> None:
        """Process actions from queue."""
        while not stop_event.is_set():
            try:
                # Use timeout to allow checking stop_event
                try:
                    action = await asyncio.wait_for(
                        self._action_queue.get(),
                        timeout=0.1
                    )
                except asyncio.TimeoutError:
                    continue

                # Rate limiting (except for cancel-all)
                if action.action_type != GatewayActionType.CANCEL_ALL:
                    await self._rate_limit()

                # Process action
                result = await self._process_action(action)
                self._result_callback(result)
                self.actions_processed += 1

            except Exception as e:
                logger.error(f"Gateway worker error: {e}")
                self.errors += 1

    async def _rate_limit(self) -> None:
        """Apply rate limiting between actions."""
        now_ms = time.monotonic_ns() // 1_000_000
        elapsed = now_ms - self._last_action_ts

        if elapsed < self._min_action_interval_ms:
            await asyncio.sleep((self._min_action_interval_ms - elapsed) / 1000)

        self._last_action_ts = time.monotonic_ns() // 1_000_000

    async def _process_action(self, action: GatewayAction) -> GatewayResult:
        """Process a single gateway action."""
        try:
            if action.action_type == GatewayActionType.PLACE:
                return await self._place_order(action)
            elif action.action_type == GatewayActionType.CANCEL:
                return await self._cancel_order(action)
            elif action.action_type == GatewayActionType.CANCEL_ALL:
                return await self._cancel_all(action)
            else:
                return GatewayResult(
                    action_id=action.action_id,
                    success=False,
                    error_kind="UNKNOWN_ACTION",
                )
        except Exception as e:
            logger.error(f"Action {action.action_id} failed: {e}")
            return GatewayResult(
                action_id=action.action_id,
                success=False,
                error_kind=str(type(e).__name__),
                retryable=self._is_retryable(e),
            )

    async def _place_order(self, action: GatewayAction) -> GatewayResult:
        """Place a single order."""
        spec = action.order_spec
        if not spec:
            return GatewayResult(
                action_id=action.action_id,
                success=False,
                error_kind="MISSING_ORDER_SPEC",
            )

        result = await self._rest.place_order(
            token_id=spec.token_id,
            side=spec.side,
            price=spec.px / 100,  # Convert cents to dollars
            size=spec.sz,
            client_order_id=spec.client_order_id,
        )

        return GatewayResult(
            action_id=action.action_id,
            success=result.success,
            error_kind=result.error_msg if not result.success else None,
            retryable=result.retryable if hasattr(result, 'retryable') else False,
        )

    async def _cancel_order(self, action: GatewayAction) -> GatewayResult:
        """Cancel a single order."""
        if not action.client_order_id:
            return GatewayResult(
                action_id=action.action_id,
                success=False,
                error_kind="MISSING_ORDER_ID",
            )

        result = await self._rest.cancel_order(action.client_order_id)

        return GatewayResult(
            action_id=action.action_id,
            success=result.success,
            error_kind=result.error_msg if not result.success else None,
        )

    async def _cancel_all(self, action: GatewayAction) -> GatewayResult:
        """Cancel all orders for a market. FAST PATH - no rate limiting."""
        if not action.event_id:
            return GatewayResult(
                action_id=action.action_id,
                success=False,
                error_kind="MISSING_EVENT_ID",
            )

        result = await self._rest.cancel_all(action.event_id)

        return GatewayResult(
            action_id=action.action_id,
            success=result.success,
            error_kind=result.error_msg if not result.success else None,
        )

    @staticmethod
    def _is_retryable(error: Exception) -> bool:
        """Determine if an error is retryable."""
        error_str = str(error).lower()
        retryable_patterns = ["timeout", "connection", "503", "502", "504"]
        return any(p in error_str for p in retryable_patterns)
```

### 2. Gateway Interface (for Executor)

```python
class Gateway:
    """
    Gateway interface for Executor to submit actions.

    Provides non-blocking action submission and fast-path cancel-all.
    """

    def __init__(
        self,
        rest_client: PolymarketRestClient,
        result_queue: queue.Queue,  # Executor's high-priority queue
    ):
        self._action_queue: asyncio.Queue[GatewayAction] = asyncio.Queue(maxsize=100)
        self._result_queue = result_queue
        self._action_id_counter: int = 0
        self._worker: Optional[GatewayWorker] = None
        self._rest = rest_client

    def start(self, stop_event: asyncio.Event) -> asyncio.Task:
        """Start the gateway worker as an async task."""
        self._worker = GatewayWorker(
            rest_client=self._rest,
            action_queue=self._action_queue,
            result_callback=self._on_result,
        )
        return asyncio.create_task(self._worker.run(stop_event))

    def submit_place(self, order_spec: RealOrderSpec) -> str:
        """Submit order placement. Returns action_id."""
        action_id = self._next_action_id()
        action = GatewayAction(
            action_type=GatewayActionType.PLACE,
            action_id=action_id,
            order_spec=order_spec,
        )
        self._enqueue(action)
        return action_id

    def submit_cancel(self, client_order_id: str) -> str:
        """Submit order cancellation. Returns action_id."""
        action_id = self._next_action_id()
        action = GatewayAction(
            action_type=GatewayActionType.CANCEL,
            action_id=action_id,
            client_order_id=client_order_id,
        )
        self._enqueue(action)
        return action_id

    def submit_cancel_all(self, event_id: str) -> str:
        """
        Submit cancel-all. Returns action_id.

        FAST PATH: Not rate-limited, highest priority.
        """
        action_id = self._next_action_id()
        action = GatewayAction(
            action_type=GatewayActionType.CANCEL_ALL,
            action_id=action_id,
            event_id=event_id,
        )
        # Put at front of queue (priority)
        self._enqueue_priority(action)
        return action_id

    def _enqueue(self, action: GatewayAction) -> None:
        """Enqueue action (non-blocking)."""
        try:
            self._action_queue.put_nowait(action)
        except asyncio.QueueFull:
            logger.warning(f"Gateway queue full, dropping action {action.action_id}")

    def _enqueue_priority(self, action: GatewayAction) -> None:
        """Enqueue with priority (for cancel-all)."""
        # For asyncio.Queue, we can't easily prioritize
        # So we just ensure cancel-all is never dropped
        self._action_queue.put_nowait(action)

    def _on_result(self, result: GatewayResult) -> None:
        """Callback when action completes. Forwards to Executor queue."""
        event = GatewayResultEvent(
            event_type=ExecutorEventType.GATEWAY_RESULT,
            ts_local_ms=time.monotonic_ns() // 1_000_000,
            action_id=result.action_id,
            success=result.success,
            error_kind=result.error_kind,
            retryable=result.retryable,
        )
        try:
            self._result_queue.put_nowait(event)
        except queue.Full:
            logger.error("Result queue full!")
            self._result_queue.put(event)

    def _next_action_id(self) -> str:
        """Generate unique action ID."""
        self._action_id_counter += 1
        return f"gw_{self._action_id_counter}_{int(time.time()*1000)}"
```

### 3. Enhanced REST Client Methods

Ensure the existing `PolymarketRestClient` has these methods:

```python
class PolymarketRestClient:
    # ... existing code ...

    async def cancel_all(self, market_id: str) -> OrderResult:
        """Cancel all orders for a market."""
        try:
            async with self._session.delete(
                f"{self._base_url}/cancel-all",
                json={"market": market_id},
                headers=self._get_headers(),
            ) as resp:
                if resp.status == 200:
                    return OrderResult(success=True)
                else:
                    text = await resp.text()
                    return OrderResult(success=False, error_msg=text)
        except Exception as e:
            return OrderResult(success=False, error_msg=str(e))
```

---

## File Locations

- Create: `services/polymarket_trader/src/polymarket_trader/gateway.py`
- Update: `services/polymarket_trader/src/polymarket_trader/polymarket_rest.py` (add cancel_all)

---

## Acceptance Criteria

- [ ] Gateway runs as separate async task
- [ ] Non-blocking action submission from Executor
- [ ] Rate limiting for normal actions (but NOT cancel-all)
- [ ] Cancel-all is fast-path, never rate-limited
- [ ] Results forwarded to Executor's high-priority queue
- [ ] Proper error handling with retryable flag
- [ ] Action ID tracking for correlation

---

## Design Notes

From `polymarket_mm_framework.md` Section 8:

> **Rate limiting:**
> - Normal replace rate should be bounded (avoid self-induced lag)
> - Cancel-all is always a fast path (no throttling)

From `polymarket_mm_efficiency_design.md` Section 8:

> This prevents:
> - stuck cancels
> - delayed inventory updates
> - runaway queues

---

## Testing

Create `tests/test_gateway.py`:
- Test action submission is non-blocking
- Test rate limiting between actions
- Test cancel-all bypasses rate limiting
- Test error handling and retryable classification
- Test result callback fires correctly
