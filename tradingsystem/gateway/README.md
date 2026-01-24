# Gateway Module

The Gateway is the **critical interface** between the trading system and the exchange. It handles all order operations (place, cancel, cancel-all) with proper rate limiting, priority handling, and thread safety.

## Why This Matters

In a market-making system, the Gateway is where things can go wrong fast:

- **Stale orders** can get placed after you've already decided to cancel everything
- **Rate limiting violations** can get your API key banned
- **Blocked operations** can cause the main trading loop to hang
- **Lost cancel-all requests** can leave you exposed to market risk

This module is designed to handle all of these edge cases correctly.

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                         Executor                                │
│                                                                 │
│  submit_place()  submit_cancel()  submit_cancel_all()           │
│       │               │                  │                      │
└───────┼───────────────┼──────────────────┼──────────────────────┘
        │               │                  │
        ▼               ▼                  ▼
┌─────────────────────────────────────────────────────────────────┐
│                         Gateway                                 │
│                                                                 │
│  ┌─────────────────────────────────────────────────────────┐    │
│  │                    ActionDeque                          │    │
│  │                                                         │    │
│  │   [CANCEL_ALL] → [CANCEL] → [PLACE] → [PLACE] → ...     │    │
│  │        ↑                                                │    │
│  │   (front/priority)              (back/normal)           │    │
│  │                                                         │    │
│  │   When CANCEL_ALL is enqueued:                          │    │
│  │   1. Jumps to front of queue                            │    │
│  │   2. Clears all pending PLACE actions                   │    │
│  │                                                         │    │
│  └─────────────────────────────────────────────────────────┘    │
│                           │                                     │
│                           ▼                                     │
│  ┌─────────────────────────────────────────────────────────┐    │
│  │                   GatewayWorker                         │    │
│  │                   (background thread)                   │    │
│  │                                                         │    │
│  │   - Rate limits normal actions (50ms default)           │    │
│  │   - NO rate limit for CANCEL_ALL                        │    │
│  │   - Sends results via callback                          │    │
│  └─────────────────────────────────────────────────────────┘    │
│                           │                                     │
└───────────────────────────┼─────────────────────────────────────┘
                            │
                            ▼
                    Polymarket REST API
```

## The Cancel-All Problem

Consider this scenario without priority handling:

```
Time    Queue State                Action
────    ───────────────────────    ──────────────────────
T+0     [PLACE_1, PLACE_2, ...]    Strategy wants to place orders
T+10    [PLACE_1, PLACE_2, ...]    Worker processing PLACE_1
T+20    [PLACE_2, ...]             PLACE_1 done, worker on PLACE_2
T+25    [CANCEL_ALL, PLACE_2, ...] EMERGENCY! Need to cancel all!
T+30    [CANCEL_ALL, ...]          Worker still on PLACE_2...
T+40    [CANCEL_ALL]               PLACE_2 placed (BAD!)
T+50    []                         Finally cancel-all executed
```

**Problem**: PLACE_2 was executed AFTER we decided to cancel everything!

### The Solution: Priority Queue + Clear

With our `ActionDeque`:

```
Time    Queue State         Action
────    ─────────────────   ──────────────────────
T+0     [PLACE_1, PLACE_2]  Strategy wants to place orders
T+10    [PLACE_2]           Worker processing PLACE_1
T+25    [CANCEL_ALL]        EMERGENCY! Cancel-all enqueued
                            → Jumps to front
                            → PLACE_2 CLEARED (dropped)
T+30    []                  Cancel-all executed immediately
```

**Result**: No stale orders placed after cancel-all.

## Module Structure

```
gateway/
├── __init__.py        # Public exports
├── README.md          # This file
├── action_deque.py    # Priority queue implementation
├── worker.py          # Background worker thread
└── gateway.py         # Main Gateway interface
```

### `action_deque.py` - Priority Queue

The `ActionDeque` is a thread-safe deque with two entry points:

| Method | Priority | Used For | Behavior |
|--------|----------|----------|----------|
| `put_back()` | Normal | PLACE, CANCEL | Adds to end of queue |
| `put_front()` | High | CANCEL_ALL | Adds to front, clears PLACEs |

Key features:
- **Thread-safe**: Uses `Lock` + `Condition` for synchronization
- **Blocking get**: Worker blocks efficiently when queue is empty
- **Wake on shutdown**: `wake_all()` unblocks waiting threads
- **Configurable**: `drop_places_on_cancel_all` flag controls clearing

### `worker.py` - Background Thread

The `GatewayWorker` runs in a daemon thread and:

1. Pulls actions from the deque (blocks when empty)
2. Applies rate limiting for normal actions
3. Executes via REST client
4. Sends results via callback

Rate limiting details:
- Default: 50ms between actions = 20 actions/sec
- CANCEL_ALL: **No rate limiting** (safety first)

### `gateway.py` - Public Interface

The `Gateway` class is what external code interacts with:

```python
from tradingsystem.gateway import Gateway

gateway = Gateway(
    rest_client=rest_client,
    result_queue=executor_queue,
    max_queue_size=100,           # Drop if full
    min_action_interval_ms=50,    # Rate limit
    drop_places_on_cancel_all=True,  # Clear stale orders
)

gateway.start()

# All methods are non-blocking and thread-safe
action_id = gateway.submit_place(order_spec)
action_id = gateway.submit_cancel(server_order_id)
action_id = gateway.submit_cancel_all(market_id)  # PRIORITY!

# Results arrive in result_queue as GatewayResultEvents
```

## Configuration

| Parameter | Default | Description |
|-----------|---------|-------------|
| `max_queue_size` | 100 | Maximum pending actions. Drops if exceeded. |
| `min_action_interval_ms` | 50 | Rate limit for normal actions (50ms = 20/sec) |
| `drop_places_on_cancel_all` | True | Clear pending PLACEs on cancel-all |

## Statistics

Access via `gateway.stats`:

```python
stats = gateway.stats
print(f"Processed: {stats.actions_processed}")
print(f"Succeeded: {stats.actions_succeeded}")
print(f"Failed: {stats.actions_failed}")
print(f"Cancel-all: {stats.cancel_all_count}")
print(f"Dropped on cancel-all: {stats.actions_dropped_on_cancel_all}")
print(f"Avg latency: {stats.total_latency_ms / stats.actions_processed}ms")
```

## Error Handling

The Gateway categorizes errors as **retryable** or **not**:

| Retryable | Not Retryable |
|-----------|---------------|
| Timeout | Insufficient funds |
| Connection error | Invalid order |
| 502, 503, 504 | Order not found |
| 429 (rate limit) | Authentication error |

Results include `retryable: bool` flag for the Executor to decide whether to retry.

## Thread Safety

All public methods are thread-safe:

- `submit_*()` methods can be called from any thread
- The worker runs in its own daemon thread
- Statistics are updated atomically

## Testing

The module has comprehensive tests:

```bash
pytest tradingsystem/tests/test_gateway.py -v
```

Key test scenarios:
- Normal action processing
- Cancel-all priority (jumps queue)
- Cancel-all clears pending PLACEs
- Rate limiting behavior
- Queue full handling
- Worker start/stop
- Thread safety

## Common Pitfalls

### 1. Forgetting to start the gateway
```python
gateway = Gateway(...)
gateway.submit_place(spec)  # This queues but nothing happens!
gateway.start()  # Oops, should be before submit
```

### 2. Not handling queue full
```python
# submit_place returns action_id even if queue is full
# Check gateway.pending_actions if queue full is a concern
if gateway.pending_actions < 90:
    gateway.submit_place(spec)
```

### 3. Ignoring dropped PLACE actions
```python
# When cancel-all drops pending PLACEs, they're gone
# No result event is sent for dropped actions
# Check gateway.stats.actions_dropped_on_cancel_all
```

## Design Decisions

### Why a deque instead of two queues?

We considered having a separate priority queue for cancel-all, but:
- Single deque is simpler to reason about
- `appendleft()` gives us O(1) priority insertion
- Clearing PLACEs is natural (just filter the deque)

### Why clear PLACEs but not CANCELs?

- PLACE after cancel-all = placing stale orders = BAD
- CANCEL after cancel-all = cancelling already-cancelled = harmless

### Why no rate limit for cancel-all?

When you need to exit, you need to exit NOW. Rate limiting a safety operation defeats its purpose.

### Why blocking get with timeout?

Allows the worker to:
1. Wait efficiently (no busy-wait)
2. Check for shutdown periodically
3. Wake immediately when work arrives

## Integration with Executor

The Executor submits actions and receives results:

```
Executor                    Gateway                     Exchange
   │                           │                           │
   │  submit_place(spec)       │                           │
   │──────────────────────────>│                           │
   │  <- action_id             │                           │
   │                           │  POST /orders             │
   │                           │──────────────────────────>│
   │                           │  <- order_id              │
   │  <- GatewayResultEvent    │                           │
   │     (via result_queue)    │                           │
```

The Executor tracks:
- `action_id` → `client_order_id` mapping
- Pending actions awaiting results
- Retry logic for retryable failures
