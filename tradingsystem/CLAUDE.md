# CLAUDE.md — Trading System (Binary CLOB MM on Polymarket)

This repository is a production-oriented framework for market-making binary YES/NO event markets
(Polymarket CLOB style), using a reference market (e.g., Binance BTC) for fair value + regime control.

This file is the **top-level operating manual for an LLM**. It defines system invariants and boundaries.
Module-level details live in each folder’s CLAUDE.md.

---

## The one-sentence architecture

**Strategy emits YES-space intent → Executor (single writer) materializes to legal orders via Planner/Reconciler → Gateway serializes API calls with CANCEL_ALL priority → Feeds update caches and deliver user events → Types enforce shared semantics.**

If you change anything, keep that sentence true.

---

## System goals (prioritized)

1. **Safety / consistency**: never trade while blind, never double-spend inventory, never place stale orders after STOP.
2. **Deterministic state**: single-writer order/inventory/reservation state under asynchronous acks/fills.
3. **Low churn**: preserve queue position where possible; avoid unnecessary cancel/replace.
4. **Capital efficiency**: reduce-first complement routing to avoid gross buildup (YES+NO).

---

## Core vocab (do not blur these)

- **Snapshot**: best-effort market data (from caches). May be stale or inconsistent.
- **Intent**: strategy’s desired quotes in YES-space (`DesiredQuoteSet`).
- **Plan**: executor’s executable representation of intent (`ExecutionPlan`, `PlannedOrder`), including splits/residuals.
- **Working order**: what exists on the exchange (`WorkingOrder`).
- **Reservation**: inventory committed to working SELL orders.
- **Settled vs pending inventory**: matched fills are pending; mined fills are settled.

If you reuse one concept where another belongs, you will create subtle bugs.

---

## Non-negotiable invariants (global laws)

### A. Single-writer rule
Only the **ExecutorActor** mutates:
- working orders
- pending action state
- inventory (settled + pending)
- reservation ledger
- risk/cooldown/resync state

No other module may mutate trading state.

### B. Strategy never trades
Strategy produces **YES-space intent only**.
No complement logic, no min-size feasibility, no order lifecycle management.

### C. Gateway is I/O only, with safety priority
Gateway:
- is the only exchange interface
- runs actions in one worker thread
- rate-limits normal actions
- treats `CANCEL_ALL` as a safety interrupt:
  - jumps to front
  - clears pending PLACEs (default)
  - is not rate-limited

### D. Feeds and caches are best-effort, not truth
Feeds:
- ingest external data/events
- publish to caches/queues
- do not decide or trade

Caches:
- store latest snapshots for lock-free reads
- do not repair, infer, or gate trading

### E. No-short + pending/settled is real
Polymarket fills have phases:
- MATCHED (pending): tokens not in wallet, not sellable
- MINED (settled): tokens in wallet, sellable

Executor must size SELLs using **settled inventory only**.

### F. Min order size is a planning constraint
Polymarket minimum order size (e.g., 5 shares) is handled in the **planner**.
Plans must never output illegal orders. Sub-min intent becomes explicit residuals.

### G. Reservation release is ACK-based
Reservations may only be released on **cancel ACK**, never on cancel submit.
Orders can fill while cancel is in flight; remaining size must adjust reservations safely.

### H. SELL overlap is forbidden
Replacement SELLs must not be placed until the existing SELL is canceled (ACK received).
This prevents venue-side double-reservation and “insufficient balance” errors.

### I. Latest intent wins
There is no backlog of effect batches.
The system converges toward the most recent intent (`last_intent`), applying it when slots are idle.

---

## Module boundaries (what goes where)

- `types/`: shared vocabulary and contracts (pending vs settled, events, order specs). No business logic.
- `feeds/`: external ingestion (WS/HTTP). No trading logic. User-feed disconnect triggers safety action.
- `caches/`: lock-free latest snapshots. No decisions. Staleness is reported, not acted upon.
- `strategy/`: stateless intent generation (YES-space). Debounced. Single-slot mailbox overwrite.
- `executor/`: the core. Planner + Reconciler + actor state machine. Enforces invariants.
- `gateway/`: exchange I/O. Priority queue + worker thread. CANCEL_ALL preempts PLACEs.

If logic appears in the “wrong” module, it is almost always a regression.

---

## Failure model (what the system assumes can happen)

Assume all of these are possible:
- WS messages delayed, duplicated, missing
- fills arrive while cancels are in flight
- REST calls time out but succeed server-side
- rate limiting / 429s
- transient 5xx responses
- partial data availability (one cache stale, the other fresh)

Design is built to remain safe under these conditions. Do not “simplify” the system
by assuming they don’t happen.

---

## If you are about to change something

Ask these questions first:

1. Does this preserve the single-writer model?
2. Does this preserve CANCEL_ALL interrupt semantics (no stale place after stop)?
3. Could this cause a SELL to be sized from pending inventory or double-reserved inventory?
4. Does this increase cancel/replace churn (queue loss)?
5. Does this introduce new hidden state transitions?

If any answer is “maybe,” you need tests before changes.

---

## Running code

Its very important you actually use `source .venv/bin/activate` before running code in order to activate the virtual environment with the installed packages.

---

## Where to read next

Each major module has its own CLAUDE.md with local invariants:
- `executor/CLAUDE.md`
- `strategy/CLAUDE.md`
- `gateway/CLAUDE.md`
- `feeds/CLAUDE.md`
- `caches/CLAUDE.md`
- `types/CLAUDE.md`