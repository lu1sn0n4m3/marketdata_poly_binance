# Executor Package

Complement-aware order execution for Polymarket binary CLOB markets.

## Problem Statement

Polymarket's binary markets have unique mechanics that require specialized order execution:

1. **Complement Symmetry**: YES and NO tokens are complements (YES + NO = $1.00). This means "SELL YES 5" when holding only 2 YES requires splitting: SELL YES 2 + BUY NO 3.

2. **Inventory Reservation**: Multiple orders compete for the same inventory. Without tracking reservations, you get "insufficient balance" errors when the second order tries to use already-committed inventory.

3. **Minimum Order Size**: Polymarket requires minimum 5 shares per order. This isn't a post-fill cleanup problem—it's a planning constraint that affects whether orders can be placed at all.

4. **SELL Overlap**: Placing a replacement SELL before the old SELL is canceled can cause double-spend of inventory on the venue side.

5. **Queue Position**: Cancel/replace loses queue priority. Unnecessary replacements (like small size increases after partial fills) waste queue position.

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

---

## Core Concepts

### 1. YES-Space Strategy Intent

The **strategy operates in YES-space** - it only thinks about YES token prices and sizes:

```python
@dataclass
class DesiredQuoteSet:
    bid_yes: DesiredQuoteLeg  # "I want to BUY YES at this price"
    ask_yes: DesiredQuoteLeg  # "I want to SELL YES at this price"
```

The strategy is **ignorant of venue mechanics**. It doesn't know about:
- Complement tokens (NO)
- Inventory constraints
- Minimum order sizes
- Split orders

The **planner translates** strategy intent into executable orders.

### 2. Complement Token Symmetry

In binary markets, YES and NO are economic complements:
- Holding 1 YES + 1 NO = $1.00 (always)
- BUY YES at 45c ≡ SELL NO at 55c (same economic exposure)
- SELL YES at 55c ≡ BUY NO at 45c (same economic exposure)

**Key insight**: You can express any position using either token.

### 3. Reduce-First Priority

The planner uses **reduce-first logic** to minimize capital:

| Intent | Priority 1 (Reduce) | Priority 2 (Complement) |
|--------|---------------------|-------------------------|
| Want long YES (bid) | SELL NO (if held) | BUY YES |
| Want short YES (ask) | SELL YES (if held) | BUY NO |

**Why reduce first?**
- Selling releases capital locked in inventory
- Reduces gross exposure (total position size)
- No additional capital needed

### 4. Split Orders

When you want to express a position larger than your inventory:

```
Intent: SELL YES 10 (ask side)
Have:   3 YES, 0 NO

Result: SELL YES 3 (from inventory) + BUY NO 7 (complement)
```

Both orders together achieve the economic goal. The slot can hold **0, 1, or 2 orders**.

### 5. OrderKind: The Semantic Role

Every order has a **kind** that describes its role in the execution plan:

| Kind | Description | Real Order |
|------|-------------|------------|
| `REDUCE_SELL` | Selling held inventory to reduce exposure | SELL YES or SELL NO |
| `OPEN_BUY` | Buying primary token to open/increase exposure | BUY YES (for bid) |
| `COMPLEMENT_BUY` | Buying complement token to express opposite side | BUY NO (for ask) |

**How kind flows through the system:**

```
Planner decides kind → PendingPlace stores kind → WorkingOrder stores kind → Reconciler uses stored kind
```

**Why store kind?**
- Deterministic matching between planned and working orders
- Price changes don't cause flapping between order types
- Reconciler matches by kind first, then compares price/size

**Critical**: The reconciler uses **stored kind**, not inference. Inference from (side, token) is only a fallback for orders synced from the exchange where we don't know the original planner intent.

### 6. Inventory Reservation

```python
available_yes = settled_yes - reserved_yes - safety_buffer
```

Reservations are **derived from working SELL orders**:
- On fill: reservation released automatically (order size decreased)
- On cancel: reservation released (order removed)
- On resync: reservations rebuilt from scratch

This prevents drift and ensures consistency.

### 7. Min-Size as Plan-Level Constraint

Polymarket requires minimum 5 shares per order. The planner treats split orders as a **coupled decision**:

| Reduce Size | Complement Size | Action (PASSIVE_FIRST) |
|-------------|-----------------|------------------------|
| ≥ 5         | ≥ 5             | Place both orders      |
| ≥ 5         | < 5             | Place reduce only, residual = complement |
| < 5         | ≥ 5             | Place complement only, residual = reduce |
| < 5         | < 5             | Place nothing, residual = total |

With `AGGREGATE` policy, sub-min totals get rounded up to min-size.

### 8. SELL Overlap Rule

**Never place a replacement SELL until the existing SELL is canceled.**

Why? Both SELLs reserve from the same inventory:
1. SELL YES 10 at 55c (reserves 10 YES)
2. Price moves, want SELL YES 10 at 53c
3. If we place before cancel ack: venue sees 20 YES reserved from 10 inventory → rejected

The reconciler sets `sell_blocked_until_cancel_ack=True` when needed.

### 9. Queue-Preserving Replacement Policy

The reconciler uses a **queue-preserving** policy to minimize churn:

| Scenario | Action | Reason |
|----------|--------|--------|
| Price change | **REPLACE** | Price must match |
| Size decrease | **REPLACE** | Risk reduction worth losing queue |
| Size increase < threshold | **DO NOTHING** | Preserve queue position |
| Size increase ≥ threshold | **REPLACE** | Material size change |
| Token mismatch | **REPLACE** | Shouldn't happen, but handle it |
| Side mismatch | **REPLACE** | Shouldn't happen, but handle it |

**Why this matters**: A partial fill doesn't warrant replacement. If order was 30 shares and 5 filled (25 remaining), strategy still wants 30 → size diff is +5 → below threshold → keep working order.

---

## The Reconciliation Process

### Step-by-Step Flow

```
1. Strategy publishes intent (DesiredQuoteSet)
        │
        ▼
2. Planner: (intent + inventory + reservations) → ExecutionPlan
   - Splits orders if needed
   - Applies min-size constraints
   - Assigns OrderKind to each PlannedOrder
        │
        ▼
3. Reconciler: (plan + working orders) → EffectBatch
   - For each slot (bid, ask):
     a. Index working orders by their stored kind
     b. Index planned orders by kind
     c. For each working order:
        - If kind not in plan → cancel
        - If kind in plan but price/size mismatch → cancel + place
        - If kind in plan and matches → keep (no effect)
     d. For each planned order with no matching working order → place
     e. If canceling a SELL and plan has replacement SELL → block place
        │
        ▼
4. Effect Execution
   - Submit cancels first
   - Submit places (unless SELL blocked)
   - Update slot state (IDLE → CANCELING or PLACING)
        │
        ▼
5. Ack Received
   - Update working orders
   - Release reservations on cancel
   - Transition slot back to IDLE
   - If SELL was blocked, re-reconcile to place it
```

### Matching Algorithm

```python
# Simplified reconciliation logic:

working_by_kind = {order.kind: order for order in slot.working_orders}
planned_by_kind = {order.kind: order for order in plan.orders}

for kind, working in working_by_kind.items():
    planned = planned_by_kind.get(kind)

    if planned is None:
        # Working order not in plan - cancel it
        effects.cancel(working)

    elif not matches(working, planned, price_tol, size_tol):
        # Working order differs from plan - cancel and replace
        effects.cancel(working)
        if not (is_sell(planned) and sell_being_canceled):
            effects.place(planned)
        else:
            # Block SELL until cancel ack
            effects.sell_blocked = True

for kind, planned in planned_by_kind.items():
    if kind not in working_by_kind:
        # New order needed - place it
        if not (is_sell(planned) and sell_being_canceled):
            effects.place(planned)
```

---

## Hard Examples

These examples demonstrate the most challenging scenarios the executor handles.

### Example 1: Split Order with Partial Inventory

**Scenario**: Strategy wants to express a 15-share ask, but we only hold 8 YES.

```
State:
  inventory: YES=8, NO=0
  reservations: reserved_yes=0
  working orders: (none)

Intent:
  ask_yes: enabled=True, px=55, sz=15

Planner calculation:
  available_yes = 8 - 0 = 8
  reduce_size = min(15, 8) = 8    → SELL YES 8
  complement_size = 15 - 8 = 7   → BUY NO 7 (at 100-55=45c)

  Both ≥ 5 (min_size), so both are legal.

Plan output:
  ask_leg.orders = [
    PlannedOrder(kind=REDUCE_SELL, token=YES, side=SELL, px=55, sz=8),
    PlannedOrder(kind=COMPLEMENT_BUY, token=NO, side=BUY, px=45, sz=7),
  ]

Reconcile (no working orders):
  → Place SELL YES 8 @ 55c (kind=REDUCE_SELL)
  → Place BUY NO 7 @ 45c (kind=COMPLEMENT_BUY)

Result: Two orders working, together express 15-share ask.
```

### Example 2: Sub-Minimum Split Orders

**Scenario**: Strategy wants 8-share ask, but we only hold 3 YES.

```
State:
  inventory: YES=3, NO=0
  reservations: reserved_yes=0

Intent:
  ask_yes: enabled=True, px=55, sz=8

Planner calculation:
  reduce_size = min(8, 3) = 3     → SELL YES 3 (sub-min!)
  complement_size = 8 - 3 = 5    → BUY NO 5 (legal)

  SELL YES 3 < 5 (illegal)
  BUY NO 5 ≥ 5 (legal)

PASSIVE_FIRST policy:
  → Place only BUY NO 5 (the legal one)
  → residual = 3 (the dust we couldn't sell)

Plan output:
  ask_leg.orders = [
    PlannedOrder(kind=COMPLEMENT_BUY, token=NO, side=BUY, px=45, sz=5),
  ]
  ask_leg.residual_size = 3

Result: We express 5-share ask (best we can do). 3 shares of YES dust can't be sold.
```

### Example 3: Both Sub-Minimum with Aggregation

**Scenario**: Strategy wants 6-share ask, we hold 2 YES.

```
State:
  inventory: YES=2, NO=0

Intent:
  ask_yes: enabled=True, px=55, sz=6

Planner calculation:
  reduce_size = 2     → SELL YES 2 (sub-min!)
  complement_size = 4 → BUY NO 4 (sub-min!)

  Both are illegal (< 5).

PASSIVE_FIRST policy:
  → Place nothing
  → residual = 6

AGGREGATE policy:
  → Cannot aggregate SELL (only 2 YES available)
  → Aggregate the BUY: round up 4 → 5
  → Place BUY NO 5 @ 45c
  → residual = 2 (the dust)

Plan output (AGGREGATE):
  ask_leg.orders = [
    PlannedOrder(kind=COMPLEMENT_BUY, token=NO, side=BUY, px=45, sz=5),
  ]
  ask_leg.aggregated_from = 6
  ask_leg.residual_size = 2

Result: We express 5-share ask by buying slightly more complement than needed.
```

### Example 4: SELL Replacement with Overlap Blocking

**Scenario**: Price moves, need to replace our SELL order.

```
State:
  ask_slot.orders = {
    "order_A": WorkingOrder(SELL YES 10 @ 55c, kind="reduce_sell")
  }

Intent:
  ask_yes: enabled=True, px=53, sz=10   ← price changed!

Plan:
  ask_leg.orders = [
    PlannedOrder(kind=REDUCE_SELL, token=YES, side=SELL, px=53, sz=10)
  ]

Reconcile:
  working_by_kind = {REDUCE_SELL: order_A}
  planned_by_kind = {REDUCE_SELL: PlannedOrder(...)}

  1. Check order_A: kind=REDUCE_SELL matches plan
  2. Price mismatch: 55c vs 53c → must replace
  3. Cancel order_A → sell_being_canceled = True
  4. Place REDUCE_SELL? → BLOCKED (sell overlap rule!)

EffectBatch:
  cancels = [Cancel(order_A)]
  places = []  ← SELL blocked!
  sell_blocked_until_cancel_ack = True

After cancel_ack arrives:
  1. Slot transitions CANCELING → IDLE
  2. Re-reconcile with last_intent
  3. Now no SELL being canceled
  4. Place SELL YES 10 @ 53c

Result: SELL replacement is safe - we waited for cancel confirmation.
```

### Example 5: Partial Fill with Queue Preservation

**Scenario**: Order partially filled, strategy still wants same size.

```
State:
  bid_slot.orders = {
    "order_B": WorkingOrder(BUY YES 30 @ 45c, filled=5, kind="open_buy")
  }

  remaining_sz = 30 - 5 = 25

Intent:
  bid_yes: enabled=True, px=45, sz=30   ← same as original

Plan:
  bid_leg.orders = [
    PlannedOrder(kind=OPEN_BUY, token=YES, side=BUY, px=45, sz=30)
  ]

Reconcile:
  1. Match by kind: working OPEN_BUY ↔ planned OPEN_BUY
  2. Price: 45c == 45c ✓
  3. Size comparison: planned=30, remaining=25
     size_diff = 30 - 25 = +5
  4. Is +5 ≥ top_up_threshold (10)? NO
  5. Queue-preserving: DO NOTHING

EffectBatch:
  cancels = []
  places = []

Result: Order stays working at 25 remaining. No churn, queue preserved!
```

### Example 6: Partial Fill with Large Size Increase

**Scenario**: After partial fill, strategy wants significantly more.

```
State:
  bid_slot.orders = {
    "order_B": WorkingOrder(BUY YES 20 @ 45c, filled=5, kind="open_buy")
  }

  remaining_sz = 20 - 5 = 15

Intent:
  bid_yes: enabled=True, px=45, sz=40   ← much larger!

Plan:
  bid_leg.orders = [
    PlannedOrder(kind=OPEN_BUY, token=YES, side=BUY, px=45, sz=40)
  ]

Reconcile:
  1. Match by kind: OPEN_BUY ↔ OPEN_BUY
  2. Price: 45c == 45c ✓
  3. Size comparison: planned=40, remaining=15
     size_diff = 40 - 15 = +25
  4. Is +25 ≥ top_up_threshold (10)? YES
  5. Material increase: REPLACE

EffectBatch:
  cancels = [Cancel(order_B)]
  places = [Place(BUY YES 40 @ 45c, kind=OPEN_BUY)]

Result: Cancel and replace with larger order. Worth losing queue for 25 more shares.
```

### Example 7: Transition from Split to Single Order

**Scenario**: Inventory increased, no longer need split.

```
State (initial):
  inventory: YES=3, NO=0
  ask_slot.orders = {
    "order_C": WorkingOrder(SELL YES 3 @ 55c, kind="reduce_sell"),
    "order_D": WorkingOrder(BUY NO 7 @ 45c, kind="complement_buy"),
  }

(Fill happens: received 10 more YES)

State (after fill):
  inventory: YES=13, NO=0
  reservations: reserved_yes=3 (from order_C)

Intent:
  ask_yes: enabled=True, px=55, sz=10

Planner calculation:
  available_yes = 13 - 3 = 10  ← considering reservation

Wait, we have a working SELL YES 3. The reservation is from that.
When we re-plan, we plan from scratch assuming we'll cancel if needed.

  reduce_size = min(10, 13) = 10  → SELL YES 10
  complement_size = 0            → nothing

Plan:
  ask_leg.orders = [
    PlannedOrder(kind=REDUCE_SELL, token=YES, side=SELL, px=55, sz=10)
  ]

Reconcile:
  working_by_kind = {
    REDUCE_SELL: order_C (SELL YES 3),
    COMPLEMENT_BUY: order_D (BUY NO 7)
  }
  planned_by_kind = {
    REDUCE_SELL: PlannedOrder(SELL YES 10)
  }

  1. order_C (REDUCE_SELL): in plan, but size mismatch (3 vs 10) → cancel + place
  2. order_D (COMPLEMENT_BUY): NOT in plan → cancel
  3. sell_being_canceled = True
  4. SELL replacement blocked

EffectBatch:
  cancels = [Cancel(order_C), Cancel(order_D)]
  places = []  ← blocked!
  sell_blocked_until_cancel_ack = True

After cancel acks:
  Re-reconcile → Place SELL YES 10 @ 55c

Result: Transitioned from split (SELL 3 + BUY 7) to single SELL 10.
```

### Example 8: Synced Order Matching (Kind Inference)

**Scenario**: Orders loaded from exchange at startup (kind unknown).

```
State after sync:
  ask_slot.orders = {
    "synced_123": WorkingOrder(SELL YES 10 @ 55c, kind=None)  ← unknown!
  }

Intent:
  ask_yes: enabled=True, px=55, sz=10

Plan:
  ask_leg.orders = [
    PlannedOrder(kind=REDUCE_SELL, token=YES, side=SELL, px=55, sz=10)
  ]

Reconcile:
  1. Get kind for synced order: kind=None → infer from spec
  2. Infer: side=SELL → kind=REDUCE_SELL
  3. Match: inferred REDUCE_SELL ↔ planned REDUCE_SELL
  4. Price/size match → no action

EffectBatch:
  cancels = []
  places = []

Result: Synced order correctly matched via inference fallback.
```

### Example 9: Bid Side with NO Inventory (Reduce First)

**Scenario**: We hold NO tokens and want to go long YES.

```
State:
  inventory: YES=0, NO=12

Intent:
  bid_yes: enabled=True, px=45, sz=20   ← want to go long YES

Planner calculation:
  To go long YES, we can:
  1. SELL NO (releases capital, reduces short YES exposure)
  2. BUY YES (costs capital, opens long YES exposure)

  Reduce-first: SELL NO first

  available_no = 12
  reduce_size = min(20, 12) = 12  → SELL NO 12 @ 55c (100-45)
  complement_size = 20 - 12 = 8  → BUY YES 8 @ 45c

Plan:
  bid_leg.orders = [
    PlannedOrder(kind=REDUCE_SELL, token=NO, side=SELL, px=55, sz=12),
    PlannedOrder(kind=OPEN_BUY, token=YES, side=BUY, px=45, sz=8),
  ]

Result: Express 20-share YES bid by selling NO (12) and buying YES (8).
```

### Example 10: Size Decrease Always Replaces (Risk Reduction)

**Scenario**: Strategy reduces desired size mid-flight.

```
State:
  bid_slot.orders = {
    "order_E": WorkingOrder(BUY YES 50 @ 45c, filled=10, kind="open_buy")
  }

  remaining_sz = 50 - 10 = 40

Intent:
  bid_yes: enabled=True, px=45, sz=25   ← reduced from 50!

Plan:
  bid_leg.orders = [
    PlannedOrder(kind=OPEN_BUY, token=YES, side=BUY, px=45, sz=25)
  ]

Reconcile:
  1. Match by kind: OPEN_BUY ↔ OPEN_BUY
  2. Price: 45c == 45c ✓
  3. Size comparison: planned=25, remaining=40
     size_diff = 25 - 40 = -15 (DECREASE!)
  4. Size decrease → ALWAYS REPLACE (risk reduction)

EffectBatch:
  cancels = [Cancel(order_E)]
  places = [Place(BUY YES 25 @ 45c, kind=OPEN_BUY)]

Result: Replaced despite losing queue. Risk reduction is paramount.
```

---

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
    ├── test_planner.py      # Planner unit tests
    └── test_reconciler.py   # Reconciler unit tests (34 tests)
```

---

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
    tombstoned_orders: dict[str, WorkingOrder]
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

### WorkingOrder

```python
@dataclass
class WorkingOrder:
    client_order_id: str
    server_order_id: str
    order_spec: RealOrderSpec
    status: OrderStatus
    created_ts: int
    last_state_change_ts: int
    filled_sz: int = 0
    kind: str | None = None  # "reduce_sell", "open_buy", "complement_buy", or None

    @property
    def remaining_sz(self) -> int:
        return max(0, self.order_spec.sz - self.filled_sz)
```

**Key**: The `kind` field is set at placement time and used by the reconciler for matching.

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
    kind: OrderKind      # REDUCE_SELL, OPEN_BUY, or COMPLEMENT_BUY
    token: Token         # YES or NO
    side: Side           # BUY or SELL
    px: int              # Price in cents
    sz: int              # Size in shares
    token_id: str        # Actual token ID for exchange
```

---

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

---

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

---

## Live Trading Safety Invariants

These invariants separate "works in simulation" from "works in production."

### 1. Reservation Release Timing

**CRITICAL: Only release reservations on CANCEL ACK, never on cancel submit.**

```
Timeline:
  t=0: Submit cancel for SELL YES 10
  t=1: Order can STILL FILL on venue (cancel in flight)
  t=2: Fill arrives for 3 shares → inventory: YES += 3
  t=3: Cancel ack arrives → order fully canceled (7 remaining released)

WRONG approach:
  t=0: Submit cancel → release 10 reservation ❌
  t=1: New SELL YES 10 placed using "available" inventory
  t=2: Venue: TWO sells reserve same inventory → rejection

CORRECT approach (implemented):
  - Reservation released only when cancel ACK confirms order is gone
  - Fill during cancel-in-flight reduces remaining_sz, reservation auto-adjusts
  - No double-counting possible
```

See [actor.py:496-506](executor/actor.py#L496-L506) for the ack-only release logic.

### 2. Order Can Fill While Cancel Is In Flight

A cancel request is not instantaneous. Between submit and ack:
- The order remains LIVE on venue
- Fills can and do arrive
- Partial fills reduce the order's remaining size

The executor handles this by:
- Keeping the order in working state until cancel ack
- Processing any fills that arrive (updating inventory and PnL)
- Only removing the order from state after ack confirms

### 3. Two Input Sources (Event Queue + Intent Mailbox)

**CRITICAL: The executor has two input sources with different semantics.**

```
┌─────────────────────────────────────────────────────────────────┐
│                        ExecutorActor                             │
│                                                                   │
│  ┌────────────────────┐     ┌─────────────────────────────────┐  │
│  │   Event Queue      │     │      Intent Mailbox              │  │
│  │   (FIFO, must not  │     │      (single-slot overwrite,    │  │
│  │    drop events)    │     │       O(1) access)              │  │
│  │                    │     │                                  │  │
│  │  - Fills           │     │  - Strategy intents only        │  │
│  │  - Acks            │     │  - Latest intent wins           │  │
│  │  - Gateway results │     │  - Polled after every event     │  │
│  └─────────┬──────────┘     └──────────────┬──────────────────┘  │
│            │                               │                      │
│            │     ┌─────────────────────┐   │                      │
│            └────▶│    Event Loop       │◀──┘                      │
│                  │                     │                          │
│                  │  1. Get event       │                          │
│                  │  2. Dispatch event  │                          │
│                  │  3. Poll mailbox    │                          │
│                  └─────────────────────┘                          │
└─────────────────────────────────────────────────────────────────┘
```

**Why two sources?**

Under load (many fills during volatile markets), the event queue can have many pending events.
If intents were in the event queue, your urgent "widen/STOP" intent would wait behind fills.

With the mailbox pattern:
- Executor processes one event from queue
- Immediately polls mailbox for latest intent (O(1))
- Always sees the LATEST intent, not stale ones

This ensures O(1) intent latency regardless of queue depth - critical for fast reaction during market shocks.

**Event Queue Rules:**
- FIFO processing (deterministic order)
- MUST NOT drop events (fills are critical for inventory)
- Processed sequentially

**Mailbox Rules:**
- Single-slot overwrite (only latest intent kept)
- Polled after every event AND on idle ticks
- O(1) access regardless of event queue depth

### 4. Latest Intent Wins (No Effect Backlog)

**CRITICAL: There is no queue of effect batches. Only the latest strategy intent matters.**

```
Problem scenario (hypothetical wrong design):
  t=0: Intent A → effects queued
  t=1: Intent B → effects queued
  t=2: Execute effects from A
  t=3: Execute effects from B
  Result: Thrashing between A and B!

Correct design (implemented):
  t=0: Intent A → store as last_intent
  t=1: Intent B → overwrite last_intent (A discarded)
  t=2: Slot busy? → reconciler returns empty batch, slot marked dirty
  t=3: Any state change (ack, fill, timeout) → re-reconcile with last_intent (B)
  Result: Only B is executed, A is forgotten
```

The key mechanisms:
- `last_intent` stores only the most recent strategy intent
- Busy slots do not emit effects
- Re-reconcile triggers:
  - **Cancel acks**: Re-reconcile with `last_intent` (may unblock SELLs, slot now IDLE)
  - **Blocked SELL resolution**: After all cancels complete, re-reconcile to place deferred SELLs
  - **New intent arrival**: Always re-reconcile with fresh state
- **Fills do NOT trigger re-reconcile** - inventory updates immediately, but reconciliation waits for next intent (design choice: strategy runs continuously and publishes frequently)

**Important**: Fills update `remaining_sz` and inventory in real-time. The next intent arrival will reconcile against this updated state. This assumes the strategy publishes intents frequently enough that stale-quote windows are short.

### 5. Kind Must Be Set (Assertion/Warning)

**CRITICAL: Every working order should have a `kind` set.**

The kind field flows through:
```
Planner → PendingPlace.order_kind → WorkingOrder.kind → Reconciler uses stored kind
```

For synced orders (loaded from exchange at startup/resync):
```
sync.py infers kind → WorkingOrder.kind set at sync time
```

**If kind is None in normal operation, something is wrong.** The reconciler logs a warning:
```
WARNING: Order XYZ has kind=None - inferring from spec (this should not happen in normal operation)
```

This warning should be monitored in production. If it fires, investigate why the kind wasn't set.

### 6. SELL Overlap Rule Enforcement

**CRITICAL: Never submit a replacement SELL until the original SELL is canceled.**

Both SELLs would reserve from the same inventory pool. The venue would reject the second SELL.

The reconciler enforces this by:
- Setting `sell_blocked_until_cancel_ack=True` when canceling a SELL that has a replacement
- NOT including the replacement SELL in the `places` list
- After cancel ack, re-reconciling places the replacement SELL

### 7. Slot State Machine Invariants

| State | Can Submit? | On Ack |
|-------|-------------|--------|
| IDLE | YES | N/A |
| CANCELING | NO | Transition to IDLE when all cancel acks received |
| PLACING | NO | Transition to IDLE when all place acks received |
| RESYNCING | NO | Transition to IDLE after REST sync completes |

**Invariant**: If slot is not IDLE, reconciler returns empty batch. No overlapping "mutating waves."

### 8. Submission Ordering and Convergence Guarantee

**CRITICAL: The system must converge. Every plan must eventually be reflected in working orders.**

Submission rules:
1. **Cancels before places** - Always execute cancels first, especially for SELL overlap
2. **Only submit when IDLE** - Slots in CANCELING/PLACING/RESYNCING reject new ops
3. **Dirty flag convergence** - Busy slots are marked dirty; reconcile re-runs on next state change

Convergence guarantee:
```
For any last_intent I:
  1. If slot is IDLE → reconcile produces effects → submit → slot becomes busy
  2. If slot is busy → reconcile returns empty, last_intent preserved
  3. On cancel ack → slot transitions to IDLE, re-reconcile with last_intent
  4. On blocked SELL resolution → re-reconcile to place deferred SELLs
  5. On next intent arrival → reconcile with fresh state
  6. Working orders converge to plan derived from last_intent
```

**Design assumption**: Strategy publishes intents frequently (every tick). This ensures fills that change `remaining_sz` are accounted for at the next intent, not immediately.

**Why this works**:
- `last_intent` is preserved across busy periods → no intent is "lost"
- Cancel acks explicitly re-reconcile → blocked SELLs get placed
- Strategy runs continuously → fills are reflected in next intent's reconciliation
- Slot state machine → no overlapping waves, deterministic transition to IDLE

Failure modes prevented:
- "Intent dropped while busy" → last_intent preserved, re-applied on cancel ack
- "SELL blocked forever" → explicit re-reconcile after cancel ack clears block
- "Stale quotes after fills" → next intent arrival uses fresh inventory/remaining

Potential gap (by design):
- If strategy stops publishing intents, fills won't trigger reconciliation
- This is acceptable: no new intent = no desired state change = no action needed

### 9. Checklist: Sim vs Live

| Property | Sim | Live | Status |
|----------|-----|------|--------|
| Cancel ack before reservation release | Optional | REQUIRED | ✅ Implemented |
| Order fills during cancel flight | Rare | Common | ✅ Handled |
| Latest intent wins (no backlog) | Nice-to-have | REQUIRED | ✅ Implemented |
| Intent via mailbox (O(1) latency) | Optional | REQUIRED | ✅ Implemented |
| Kind always set on WorkingOrder | Optional | REQUIRED | ✅ Enforced (warning if None) |
| SELL overlap blocked | Optional | REQUIRED | ✅ Enforced |
| Tombstones for orphan fills | Optional | REQUIRED | ✅ Implemented |

---

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

---

## Testing

```bash
# Run all executor tests
pytest tradingsystem/executor/tests/ -v

# Run planner tests only
pytest tradingsystem/executor/tests/test_planner.py -v

# Run reconciler tests only
pytest tradingsystem/executor/tests/test_reconciler.py -v
```

### Test Coverage

The reconciler tests (34 tests) cover:
- Price/size tolerance matching
- Queue-preserving replacement policy
- Multi-order plans (split orders)
- SELL overlap rule edge cases
- Partial fill handling
- Stored kind behavior

---

## Debugging

### Enable Debug Logging

```python
import logging
logging.getLogger("tradingsystem.executor").setLevel(logging.DEBUG)
```

### Key Log Messages

- `[FEASIBILITY] Intent X/Y -> Plan A/B (residual: R/S)` - When plan differs from intent
- `[QUEUE_PRESERVE] Ignoring top-up X->Y (+Z < threshold T)` - Queue preserved
- `Submitted place: ...` - Order submission
- `Submitted cancel: ...` - Cancel submission
- `FILL: BUY/SELL XxTOKEN@Pc` - Fill received
- `Triggering resync: reason` - Resync triggered
- `Resync complete` - State rebuilt

---

## Quick Reference

### Order Kinds

| Kind | Side | Token | When Used |
|------|------|-------|-----------|
| `REDUCE_SELL` | SELL | YES or NO | Selling held inventory |
| `OPEN_BUY` | BUY | YES | Opening/increasing YES exposure (bid) |
| `COMPLEMENT_BUY` | BUY | NO | Using complement to express ask |

### Replacement Decision Tree

```
Price changed?
  └─ YES → REPLACE

Token changed?
  └─ YES → REPLACE (shouldn't happen)

Side changed?
  └─ YES → REPLACE (shouldn't happen)

Size changed?
  ├─ Decrease → REPLACE (risk reduction)
  ├─ Increase < threshold → DO NOTHING (preserve queue)
  └─ Increase ≥ threshold → REPLACE (material change)
```

### Split Order Decision Tree

```
Total target size S, available inventory A:

reduce_size = min(S, A)
complement_size = S - reduce_size

Is reduce_size ≥ min_size?
  ├─ YES: Include REDUCE_SELL in plan
  └─ NO: Dust (can't place)

Is complement_size ≥ min_size?
  ├─ YES: Include OPEN_BUY or COMPLEMENT_BUY in plan
  └─ NO: Dust (can't place) unless AGGREGATE policy rounds up
```
