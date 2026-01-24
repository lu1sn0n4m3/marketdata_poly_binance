# Executor Package

Complement-aware order execution for Polymarket binary CLOB markets.

## Problem Statement

Polymarket's binary markets have unique mechanics that require specialized order execution:

1. **Complement Symmetry**: YES and NO tokens are complements (YES + NO = $1.00). This means "SELL YES 5" when holding only 2 YES requires splitting: SELL YES 2 + BUY NO 3.

2. **Inventory Reservation**: Multiple orders compete for the same inventory. Without tracking reservations, you get "insufficient balance" errors when the second order tries to use already-committed inventory.

3. **Minimum Order Size**: Polymarket requires minimum 5 shares per order. This isn't a post-fill cleanup problem—it's a planning constraint that affects whether orders can be placed at all.

4. **SELL Overlap**: Placing a replacement SELL before the old SELL is canceled can cause double-spend of inventory on the venue side.

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                        ExecutorActor                             │
│  ┌─────────────┐    ┌─────────────┐    ┌─────────────────────┐  │
│  │   Planner   │───▶│  Reconciler │───▶│  Effect Execution   │  │
│  │             │    │             │    │                     │  │
│  │ intent +    │    │ plan vs     │    │ submit to gateway   │  │
│  │ state →     │    │ working →   │    │ update state        │  │
│  │ plan        │    │ effects     │    │                     │  │
│  └─────────────┘    └─────────────┘    └─────────────────────┘  │
│         │                                        │               │
│         ▼                                        ▼               │
│  ┌─────────────────────────────────────────────────────────┐    │
│  │                    ExecutorState                         │    │
│  │  ┌──────────┐  ┌──────────┐  ┌────────────────────────┐ │    │
│  │  │ bid_slot │  │ ask_slot │  │  ReservationLedger     │ │    │
│  │  │ (orders) │  │ (orders) │  │  (reserved YES/NO)     │ │    │
│  │  └──────────┘  └──────────┘  └────────────────────────┘ │    │
│  │  ┌──────────────────────────────────────────────────────┐│    │
│  │  │ inventory: I_yes, I_no | pnl: PnLTracker            ││    │
│  │  └──────────────────────────────────────────────────────┘│    │
│  └─────────────────────────────────────────────────────────┘    │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
                    ┌─────────────────┐
                    │     Gateway     │
                    │  (rate-limited) │
                    └─────────────────┘
```

## Key Concepts

### 1. Complement Token Symmetry

In binary markets, YES and NO are economic complements:
- Holding 1 YES + 1 NO = $1.00 (always)
- BUY YES at 45c ≡ SELL NO at 55c (same economic exposure)
- SELL YES at 55c ≡ BUY NO at 45c (same economic exposure)

The planner uses **reduce-first** logic:
- To go long YES (bid): prefer SELL NO (if held) over BUY YES
- To go short YES (ask): prefer SELL YES (if held) over BUY NO

This minimizes capital requirements and reduces position churn.

### 2. Split Orders

When you want to SELL YES 10 but only hold 3 YES:
```
SELL YES 3 (from inventory) + BUY NO 7 (complement)
```

Both orders together achieve the economic goal. The planner handles this automatically.

### 3. Inventory Reservation

```python
available_yes = settled_yes - reserved_yes - safety_buffer
```

Reservations are **derived from working SELL orders**, not tracked incrementally:
- On fill: reservation released automatically (order size decreased)
- On cancel: reservation released (order removed)
- On resync: reservations rebuilt from scratch

This prevents drift and ensures consistency.

### 4. Min-Size as Plan-Level Constraint

Polymarket requires minimum 5 shares per order. The planner treats this as a **coupled decision** for split orders:

| Reduce Size | Complement Size | Action (PASSIVE_FIRST) |
|-------------|-----------------|------------------------|
| ≥ 5         | ≥ 5             | Place both orders      |
| ≥ 5         | < 5             | Place reduce only, residual = complement |
| < 5         | ≥ 5             | Place complement only, residual = reduce |
| < 5         | < 5             | Place nothing, residual = total |

With `AGGREGATE` policy, sub-min totals get rounded up to min-size.

### 5. SELL Overlap Rule

**Never place a replacement SELL until the existing SELL is canceled.**

Why? Both SELLs reserve from the same inventory. The venue sees:
1. SELL YES 10 (reserves 10 YES)
2. New SELL YES 5 at different price (reserves 5 YES)
3. If cancel hasn't processed: 15 YES reserved from 10 YES inventory → rejected

The reconciler sets `sell_blocked_until_cancel_ack=True` when needed.

### 6. OrderKind for Deterministic Matching

Orders have a "kind" for matching plan to working orders:
- `REDUCE_SELL`: Selling held inventory
- `COMPLEMENT_BUY`: Buying the complement token

This enables deterministic reconciliation even when prices change.

## File Structure

```
executor/
├── __init__.py          # Public API exports
├── actor.py             # ExecutorActor - event loop, dispatch
├── state.py             # ExecutorState, OrderSlot, ReservationLedger
├── planner.py           # Pure planning: intent + state → plan
├── reconciler.py        # plan vs working → effects
├── effects.py           # ReconcileEffect, EffectBatch
├── policies.py          # MinSizePolicy, ExecutorPolicies
├── pnl.py               # PnLTracker, FillRecord
├── trade_log.py         # TradeLogger (JSONL output)
├── sync.py              # RESYNCING mode, tombstones
├── errors.py            # Normalized error codes
└── tests/
    └── test_planner.py  # Comprehensive planner tests
```

## Data Structures

### ExecutorState

```python
@dataclass
class ExecutorState:
    mode: ExecutorMode = ExecutorMode.NORMAL  # or RESYNCING

    # Order slots (bid and ask)
    bid_slot: OrderSlot
    ask_slot: OrderSlot

    # Inventory tracking
    inventory: InventoryState
    reservations: ReservationLedger

    # Risk controls
    risk: RiskState

    # PnL tracking
    pnl: PnLTracker

    # Last strategy intent (for re-reconciliation)
    last_intent: Optional[DesiredQuoteSet]

    # Tombstoned orders (for orphan fill matching during resync)
    tombstoned_orders: dict[str, TombstonedOrder]
```

### OrderSlot State Machine

```
     ┌──────────────────────────────────────────┐
     │                                          │
     ▼                                          │
  ┌──────┐  submit cancel  ┌───────────┐        │
  │ IDLE │────────────────▶│ CANCELING │────────┤ all acks
  └──────┘                 └───────────┘        │ received
     │                                          │
     │ submit place        ┌──────────┐         │
     └────────────────────▶│ PLACING  │─────────┘
                           └──────────┘
                                │
                                │ resync triggered
                                ▼
                           ┌───────────┐
                           │ RESYNCING │ (blocks all)
                           └───────────┘
```

**Invariant**: Only submit new operations when slot is IDLE.

### ReservationLedger

```python
@dataclass
class ReservationLedger:
    reserved_yes: int = 0
    reserved_no: int = 0
    safety_buffer_yes: int = 0
    safety_buffer_no: int = 0

    def available_yes(self, settled_yes: int) -> int:
        return max(0, settled_yes - self.reserved_yes - self.safety_buffer_yes)

    @classmethod
    def rebuild_from_slots(cls, slots: list[OrderSlot], safety_buffer: int = 0):
        """Rebuild from working orders - the source of truth."""
```

### ExecutionPlan

```python
@dataclass
class ExecutionPlan:
    bid: LegPlan  # Plan for bid side
    ask: LegPlan  # Plan for ask side
    timestamp_ms: int

@dataclass
class LegPlan:
    orders: list[PlannedOrder]  # 0, 1, or 2 orders
    residual_size: int          # Size we couldn't place (sub-min)
    aggregated_from: int        # Original size if aggregated
    policy_applied: MinSizePolicy

@dataclass
class PlannedOrder:
    kind: OrderKind      # REDUCE_SELL or COMPLEMENT_BUY
    token: Token         # YES or NO
    side: Side           # BUY or SELL
    px: int              # Price in cents
    sz: int              # Size in shares
```

## Event Flow

The executor uses a **unified event queue** - all inputs flow through one stream:

```
┌─────────────────┐     ┌─────────────────┐     ┌─────────────────┐
│ Strategy Runner │     │  User WebSocket │     │    Gateway      │
│ (StrategyIntent │     │ (Fills, Acks)   │     │ (Results)       │
│  Event)         │     │                 │     │                 │
└────────┬────────┘     └────────┬────────┘     └────────┬────────┘
         │                       │                       │
         └───────────────────────┼───────────────────────┘
                                 │
                                 ▼
                    ┌────────────────────────┐
                    │   Unified Event Queue  │
                    │ (deterministic order)  │
                    └───────────┬────────────┘
                                │
                                ▼
                    ┌────────────────────────┐
                    │    ExecutorActor       │
                    │    _dispatch_event()   │
                    └────────────────────────┘
```

### Event Types

| Event | Source | Handler |
|-------|--------|---------|
| `StrategyIntentEvent` | Strategy Runner | `_on_intent()` → plan → reconcile → execute |
| `GatewayResultEvent` | Gateway | `_handle_place_result()` or `_handle_cancel_result()` |
| `OrderAckEvent` | User WS | `_on_order_ack()` → update working order status |
| `CancelAckEvent` | User WS | `_on_cancel_ack()` → remove order, release reservation |
| `FillEvent` | User WS | `_on_fill()` → update inventory, PnL, reservations |
| `TimerTickEvent` | Timer | `_on_timer_tick()` → check timeouts, cleanup |

## Usage

### Basic Setup

```python
from tradingsystem.executor import ExecutorActor, ExecutorPolicies, MinSizePolicy

# Create policies
policies = ExecutorPolicies(
    min_order_size=5,
    min_size_policy=MinSizePolicy.PASSIVE_FIRST,
    safety_buffer=0,  # 0 for paper, >0 for production
    price_tolerance=0,
    top_up_threshold=10,  # Only replace on size increase >= 10
)

# Create executor
executor = ExecutorActor(
    gateway=gateway,
    event_queue=event_queue,
    yes_token_id="0x1234...",
    no_token_id="0x5678...",
    market_id="0xabcd...",
    rest_client=rest_client,  # For resync
    policies=policies,
)

# Start
executor.start()

# Push intents via event queue
event_queue.put(StrategyIntentEvent(intent=intent, ts_local_ms=now_ms()))

# Stop
executor.stop()
```

### Sync Existing Orders at Startup

```python
# Fetch open orders from REST API
orders = rest_client.get_open_orders(market_id=market_id)

# Sync into executor state BEFORE starting
synced = executor.sync_open_orders(orders=orders)
print(f"Synced {synced} existing orders")

# Now start
executor.start()
```

## Policies

### MinSizePolicy

| Policy | Behavior | Use Case |
|--------|----------|----------|
| `PASSIVE_FIRST` | Don't place sub-min orders, keep as residual | Default, safest |
| `AGGREGATE` | Round up to min-size if feasible | Maintain quoting with dust |
| `DUST_CLEANUP` | Use marketable orders to eliminate dust | Emergency cleanup only |

### ExecutorPolicies

```python
@dataclass
class ExecutorPolicies:
    min_order_size: int = 5           # Polymarket minimum
    min_size_policy: MinSizePolicy = MinSizePolicy.PASSIVE_FIRST
    safety_buffer: int = 0            # Subtracted from available
    price_tolerance: int = 0          # For order matching (cents)
    top_up_threshold: int = 10        # Queue-preserving: only replace on size increase >= this
    cooldown_after_cancel_all_ms: int = 3000
    place_timeout_ms: int = 5000
    cancel_timeout_ms: int = 5000
    tombstone_retention_ms: int = 30000
```

### Queue-Preserving Replacement Policy

The reconciler uses a **queue-preserving** replacement policy to minimize unnecessary order churn:

| Scenario | Action | Reason |
|----------|--------|--------|
| Price change | REPLACE | Price must match |
| Size decrease | REPLACE | Risk reduction worth losing queue |
| Size increase < threshold | DO NOTHING | Preserve queue position |
| Size increase >= threshold | REPLACE | Material size change |

**Example**: If you have a working order for 20 shares and strategy wants 25:
- With `top_up_threshold=10`: NO replacement (+5 < threshold)
- With `top_up_threshold=5`: REPLACE (+5 >= threshold)

**Why this matters**: Cancel/replace loses queue priority on Polymarket. A partial fill (30→28) shouldn't trigger replacement just because strategy still wants 30. The strategy will naturally adjust sizes based on inventory.

## RESYNCING Mode

Triggered by:
- Insufficient balance error from gateway
- Unknown fill (order not in state)
- Manual trigger

Process:
1. Enter RESYNCING mode (blocks all new submissions)
2. Tombstone current orders (for orphan fill matching)
3. Submit cancel-all to gateway
4. Set cooldown
5. Fetch balances and open orders via REST
6. Rebuild state from REST response
7. Rebuild reservations from working orders
8. Exit RESYNCING mode

**Critical**: Fills for tombstoned orders still update inventory and PnL. Never drop fills.

## Validation Traces

### Trace 1: "Have 2 YES, target SELL YES 5, min 5"

```
Input:  avail_yes=2, intent ask_sz=5
Split:  SELL YES 2, BUY NO 3
Check:  SELL YES 2 < 5 (sub-min), BUY NO 3 < 5 (sub-min)
Result: PASSIVE_FIRST → orders=[], residual=5
        AGGREGATE → orders=[BUY NO 5], aggregated_from=5, residual=2
```

### Trace 2: "Have 7 YES, target SELL YES 5"

```
Input:  avail_yes=7, intent ask_sz=5
Split:  SELL YES 5, BUY NO 0
Check:  SELL YES 5 >= 5 (legal)
Result: orders=[SELL YES 5], residual=0
```

### Trace 3: "Have 3 NO, target BUY YES 8"

```
Input:  avail_no=3, intent bid_sz=8
Split:  SELL NO 3, BUY YES 5
Check:  SELL NO 3 < 5 (sub-min), BUY YES 5 >= 5 (legal)
Result: orders=[BUY YES 5], residual=3
```

### Trace 4: Cancel SELL then Replace

```
Working: SELL YES 10 @ 55c
Plan:    SELL YES 5 @ 53c (price changed)
Reconcile:
  - Cancel SELL YES 10 → sell_being_canceled=True
  - Place SELL YES 5? → BLOCKED (sell_blocked_until_cancel_ack=True)
After cancel ack:
  - Re-reconcile with last_intent
  - Place SELL YES 5 @ 53c
```

## Error Handling

### Normalized Error Codes

```python
class ErrorCode(Enum):
    SUCCESS = "success"
    INSUFFICIENT_BALANCE = "insufficient_balance"  # Triggers resync
    RATE_LIMIT = "rate_limit"
    TIMEOUT = "timeout"
    ORDER_NOT_FOUND = "order_not_found"
    INVALID_PRICE = "invalid_price"
    INVALID_SIZE = "invalid_size"
    MARKET_CLOSED = "market_closed"
    UNKNOWN = "unknown"
```

The `normalize_error()` function maps raw error strings to stable codes.

## Testing

Run planner tests:
```bash
pytest tradingsystem/executor/tests/test_planner.py -v
```

Run all executor tests:
```bash
pytest tradingsystem/tests/test_executor.py -v
```

### Key Test Scenarios

1. **Disabled legs** → empty plan
2. **Simple BUY YES bid** → single order
3. **Simple SELL YES ask** → single order
4. **Reduce-first with inventory** → SELL complement first
5. **Split orders** → two orders when partial inventory
6. **Coupled min-size decisions** → Trace 1, 2, 3 scenarios
7. **Reservation-aware planning** → respects reserved inventory
8. **Safety buffer** → reduces available inventory

## Debugging

### Enable Debug Logging

```python
import logging
logging.getLogger("tradingsystem.executor").setLevel(logging.DEBUG)
```

### Key Log Messages

- `[FEASIBILITY] Intent X/Y -> Plan A/B (residual: R/S)` - When plan differs from intent
- `Submitted place: ...` - Order submission
- `Submitted cancel: ...` - Cancel submission
- `FILL: BUY/SELL XxTOKEN@Pc` - Fill received
- `Triggering resync: reason` - Resync triggered
- `Resync complete` - State rebuilt

### Inspecting State

```python
# Get current state
state = executor.state

# Check reservations
print(f"Reserved YES: {state.reservations.reserved_yes}")
print(f"Reserved NO: {state.reservations.reserved_no}")

# Check slot states
print(f"Bid slot: {state.bid_slot.state.name}")
print(f"Ask slot: {state.ask_slot.state.name}")

# Check working orders
for order_id, order in state.bid_slot.orders.items():
    print(f"  {order_id[:20]}: {order.order_spec}")

# Get PnL
pnl = executor.get_pnl_stats(mark_price=50)
print(f"Realized PnL: ${pnl['realized_pnl_usd']:.2f}")
```

## Performance Considerations

1. **Event queue size**: Default 1000, increase if strategy is very fast
2. **Tick rate**: Strategy at 20Hz is typical, executor processes as fast as events arrive
3. **Reservation rebuild**: O(n) in working orders, called only on resync
4. **Tombstone cleanup**: Runs on idle ticks, configurable retention time

## Migration from Old Executor

The old monolithic `executor.py` (1224 lines) has been replaced by this package.

Key differences:
- No more `IntentMailbox` polling (intents via event queue)
- No more `OrderMaterializer` (replaced by planner + reconciler)
- State split into `OrderSlot` per side (was single bid/ask order IDs)
- Reservations derived from working orders (was incremental tracking)
- Min-size handled at plan time (was post-fill cleanup)

All existing functionality is preserved with improved correctness guarantees.
