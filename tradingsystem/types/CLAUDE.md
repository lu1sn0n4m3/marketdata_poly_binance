# CLAUDE.md — Types (Shared Vocabulary and Contracts)

This directory defines the **canonical types and language** of the trading system.
Every module depends on these definitions behaving consistently.

This is not “just types” — this is where architectural boundaries are enforced.

---

## Purpose of this module

- Define shared enums, dataclasses, and event types.
- Establish **precise semantic meaning** for concepts like:
  - intent vs plan vs order
  - settled vs pending inventory
  - snapshot vs working state
- Provide stable contracts between Strategy, Executor, Gateway, Feeds, and Caches.

If meanings drift here, bugs propagate everywhere.

---

## What types must NEVER do

- Never contain business logic or side effects.
- Never depend on concrete implementations (feeds, executor internals).
- Never silently change semantics of existing fields.
- Never blur lifecycle distinctions (e.g. “pending” vs “settled”).

Types may have convenience methods, but not decision logic.

---

## Backward compatibility is intentional

`types/__init__.py` re-exports all public types for backward compatibility.
New code should prefer explicit submodule imports, but **old imports must keep working**.

Breaking imports here breaks the entire system.

---

## Core semantic boundaries (do not blur)

### 1. Intent vs Order vs Event

- **DesiredQuoteSet**: what Strategy wants.
- **ExecutionPlan / PlannedOrder**: what Executor *intends* to do.
- **RealOrderSpec / WorkingOrder**: what actually exists on the exchange.
- **ExecutorEvent / GatewayResult**: facts that happened.

Never reuse one type where another belongs “because it’s similar”.

---

### 2. Market data vs trading state

- `market_data.py` types are **snapshots**:
  - best-effort
  - potentially stale
  - immutable once published

- `orders.py`, `state.py` types are **authoritative state**:
  - mutated only by the Executor
  - reflect trading reality

Never mix snapshot types into trading state or vice versa.

---

### 3. Settled vs pending inventory (CRITICAL)

This distinction is fundamental and must never be collapsed.

- **Settled inventory** (`I_yes`, `I_no`):
  - tokens mined on-chain
  - can be SOLD
- **Pending inventory** (`pending_yes`, `pending_no`):
  - matched but not mined
  - CANNOT be sold yet

Correct usage:
- Strategy may reason using *effective* inventory (settled + pending).
- Executor SELL sizing must use **settled inventory only**.

Any change that allows selling pending inventory is a production-critical bug.

---

### 4. Order lifecycle is explicit

`OrderStatus` and `WorkingOrder` represent **exchange reality**, not intent.

- Orders can be partially filled.
- Orders can fill while cancel is in flight.
- Orders can reach terminal states asynchronously.

Do not “simplify” lifecycle states to make logic easier elsewhere.
The complexity exists because the exchange behaves this way.

---

## Event types are facts, not suggestions

All `ExecutorEvent` subclasses represent things that happened:
- an intent arrived
- an ack was received
- a fill occurred
- a timer tick fired

They are not commands.

Never overload an event to “signal” behavior.
Behavior belongs in the Executor, not in the type.

---

## Gateway types are transport contracts

`GatewayAction` and `GatewayResult` define the boundary between:
- internal intent
- external REST API behavior

Important:
- `success=False` does not imply “nothing happened”.
- `retryable=True` does not imply “retry immediately”.

The Gateway reports facts; the Executor decides what to do.

---

## Utility functions must remain pure

Functions in `utils.py` must:
- be deterministic
- have no side effects
- not depend on global state

If a function needs configuration, it does not belong here.

---

## Adding or changing types: rules of engagement

Before adding a new field or type, ask:
1. Which layer owns this concept?
2. Is this a snapshot, intent, state, or event?
3. Does this introduce lifecycle ambiguity?

When modifying existing types:
- Adding fields is usually safe.
- Changing meaning is not.
- Removing fields or renaming enums requires auditing *all* modules.

When in doubt, add a new type instead of mutating an old one.

---

## Where changes usually go wrong

Common regressions:
- collapsing pending + settled inventory
- adding “helper logic” into dataclasses
- reusing strategy types inside executor state
- adding executor-only fields into shared types
- changing enum values used in persisted logs or tests

If a change feels “convenient,” double-check it isn’t violating a boundary.

---

## Testing philosophy for types

Types should be tested for:
- serialization/deserialization
- equality and matching behavior
- lifecycle transitions (where applicable)

Types should NOT be tested for:
- trading behavior
- strategy correctness
- executor reconciliation logic

---

## Summary

This module defines the language of the system.

If words lose their meaning here, the rest of the architecture collapses.