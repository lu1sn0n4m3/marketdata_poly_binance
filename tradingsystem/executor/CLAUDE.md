# CLAUDE.md — Executor (Planner + Reconciler + Single-Writer Actor)

This package is the **single-writer** for live order and inventory state.
Strategy emits YES-space intents; the Executor turns them into **legal Polymarket orders**
while enforcing complement symmetry, no-short, min-size, reservations, and queue discipline.

If you change behavior here, you can create real-money bugs.

---

## The only acceptable mental model

**Intent (YES-space) → Planner (pure) → Plan → Reconciler (pure) → Effects → Actor (impure) → Gateway → Acks/Fills → State**

- Planner and Reconciler must remain pure functions.
- Only the actor mutates state.
- Gateway is I/O only.

---

## What the Planner guarantees (and why it exists)

Planner input: `(DesiredQuoteSet, InventoryState, ReservationLedger, policy, min_size)`  
Planner output: `ExecutionPlan(bid: LegPlan, ask: LegPlan)`.

Hard planner invariants:
1. **Never output illegal orders**: every `PlannedOrder.sz >= min_size` or the leg is omitted.
2. **Reduce-first priority**:
   - YES bid intent (“want long YES”): prefer `SELL NO` (reduce) then `BUY YES` (open)
   - YES ask intent (“want short YES”): prefer `SELL YES` (reduce) then `BUY NO` (complement)
3. **Split is allowed**: each leg can be 0, 1, or 2 orders.
4. **Min-size is plan-level and coupled**:
   - split legs are a coupled decision
   - if both legs are sub-min, treat as a paired feasibility decision
5. **SELL sizing uses settled inventory only**.
   Pending/unsettled inventory must not be sellable.

Important nuance (do not “simplify”):
- Strategy may reason using *effective* inventory to avoid duplicated intent.
- Planner must use **settled** inventory for any SELL sizing, otherwise you will place sells that cannot settle on-venue.

---

## Reservations: why “insufficient balance” is a state bug, not a retry bug

Inventory must be reserved so multiple SELL orders don’t compete for the same tokens.

Rules:
- Available inventory for SELL planning is:
  `available = settled - reserved - safety_buffer`.
- Reservations are only released on **cancel ACK**, never on cancel submit.
- Orders can fill while cancel is in flight; fills reduce remaining size and therefore reduce reservation safely.

If you “release on cancel submit”, you will eventually double-spend and get venue rejections + state drift.

---

## OrderKind is the reconciliation key

Each planned order has a semantic `OrderKind`:
- `REDUCE_SELL`
- `OPEN_BUY` (almost always BUY YES for bid side)
- `COMPLEMENT_BUY` (almost always BUY NO for ask side)

Matching policy:
- Reconciler matches **by kind first** (then compares price/size).
- **Stored `WorkingOrder.kind` is authoritative** when present.
- Inference from `(slot_type, token, side)` is only a fallback for orders loaded from the venue during sync/resync
  where kind is unknown.

Why this matters:
- Without stable kind, representation changes can cause flapping, churn, and illegal overlap transitions.
- Kind-based matching makes behavior deterministic and testable.

---

## Reconciler: safety + queue discipline

Reconciler compares `LegPlan` vs `OrderSlot.working_orders` and emits an `EffectBatch` of cancels/places.

Hard reconciler rules:

### 1) Slot gating (no overlapping waves)
If `slot.can_submit()` is false (state != IDLE), reconciler must return an empty batch.

This prevents racey multi-wave mutations and keeps actor behavior deterministic.

### 2) Cancel-before-place ordering
Effects are conceptually ordered:
- submit cancels first
- submit places second

### 3) SELL overlap rule (non-negotiable)
**Never place a replacement SELL until the existing SELL cancel ACK is received.**

Reconciler must detect “SELL being canceled” and block any replacement SELL in the same wave.

If you violate this, venue-side reservation will reject the new SELL (insufficient balance), and you will desync.

### 4) Queue-preserving replacement policy
Cancel/replace loses queue. Replacement rules are:

- price change → replace
- size decrease → replace (risk reduction)
- size increase < `top_up_threshold` → keep (preserve queue)
- size increase >= threshold → replace
- token/side mismatch → replace (shouldn’t happen, but must converge)

Do not change this into “always replace on mismatch”; it will destroy queue position and worsen fills.

---

## Actor behavior assumptions (explicit)

- Orders can fill while cancels are in flight.
- Acks can be delayed.
- WS may be incomplete; do not assume perfect event ordering.
- **Latest intent wins**: there is no backlog of effect batches.
  The actor stores only `last_intent` and reconciles toward that whenever the slot becomes available.

Design choice:
- Fills update inventory immediately, but do not necessarily trigger immediate reconcile;
  strategy is expected to publish frequently.

If strategy stops publishing, the executor should remain safe (no new intent ⇒ no new desired state).

---

## Two input sources (critical for latency)

The executor has two input sources:

### 1. Event Queue (FIFO, must not drop)
- Fills, acks, gateway results
- Processed sequentially for deterministic ordering
- Events MUST NOT be dropped (fills are critical)

### 2. Intent Mailbox (single-slot, O(1) access)
- Strategy intents (desired quotes)
- Single-slot overwrite - only latest intent kept
- Executor polls after every event and on idle

**Why two sources?**
Under load (many fills during volatility), the event queue can have many pending events.
If intents were in the queue, your urgent "widen/STOP" intent would wait behind fills.

With the mailbox pattern:
- Executor processes one event from queue
- Immediately polls mailbox for latest intent (O(1))
- Always sees the LATEST intent, not stale ones

This ensures O(1) intent latency regardless of queue depth, critical for fast
reaction during market shocks.

---

## RESYNCING mode exists for a reason

RESYNCING is a safety recovery path when the world becomes inconsistent:
- insufficient balance errors
- unknown fills
- missing/contradictory acks
- manual trigger

RESYNCING must:
- block all new submissions
- tombstone current orders for orphan fill matching
- cancel-all
- REST fetch balances + open orders
- rebuild state + rebuild reservations from working orders
- never drop fills for tombstoned orders

If you “simplify” resync, you will eventually lose fills or drift inventory.

---

## Where changes usually go wrong

Common failure patterns:
- releasing reservations early (on cancel submit)
- placing replacement SELL before cancel ACK
- using effective inventory for SELL sizing
- removing slot gating (submitting while busy)
- matching orders by (token, side) instead of kind
- “improving” queue policy to always replace

If you want to change behavior, add tests first (especially for overlap + partial fills + min-size coupling).