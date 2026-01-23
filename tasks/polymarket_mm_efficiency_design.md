# Efficiency & Concurrency Design for a Binary CLOB MM System (Polymarket + Binance)
*Design notes focused on latency, race-condition avoidance, queue backpressure, and practical Python deployment on a small VPS (e.g., 2 vCPUs).*

This document complements the main framework description by explaining **how to make it fast and correct** in practice:
- how to structure threads/async tasks,
- how to prevent race conditions,
- how to avoid queue overload,
- what must **never** be dropped vs what can be coalesced,
- how to handle disconnects and resync safely.

---

## 1) The core idea: isolate state mutations into a single-writer Executor

### Why race conditions happen in trading code
Race conditions typically come from multiple threads/tasks concurrently mutating shared state such as:
- open orders
- pending cancels/places
- inventory
- cooldown flags
- “current mode” and risk overrides

### The single-writer invariant
To eliminate most race conditions:
- **Only the Executor/OrderManager is allowed to mutate trading state.**
- Everything else produces inputs (market data snapshots, strategy intents, user events) and sends them to the Executor.

This turns the Executor into an **actor**:
- it processes one event at a time
- it updates state deterministically
- it issues order actions in a controlled way

**Result:** no shared-state contention for orders/inventory → far fewer “impossible” bugs.

---

## 2) Decouple high-frequency market data from the Executor inbox

A major performance mistake is to push every market-data message into the same queue the Executor consumes.

### Correct separation
- **Market data (book/trades)** → goes into **snapshot caches** (latest-only + small ring buffer).
- **State-changing user events** (fills/acks/cancel-acks) → go into the **Executor inbox**.
- **Strategy intents** → go into the **Executor inbox** (but are allowed to be coalesced).

### Why this matters
Polymarket book deltas can burst. If you enqueue them all:
- Executor gets clogged
- your cancel/replace latency increases
- you get picked off

Instead:
- caches always hold the latest view
- Strategy reads caches at fixed Hz
- Executor consumes only low-rate, critical events

**Key takeaway:** the Executor should not be “dependent on the WS clock”.

---

## 3) Event priority: what must never be dropped vs what can be coalesced

### Category A — MUST BE LOSSLESS (never drop)
These are required to maintain correct order/inventory state:
- **Fills** (partial or full)
- **Order acks** (accepted/rejected)
- **Cancel acks**
- **Cancel-all results**
- **Gateway errors / request failures** (that affect state)

If you drop these, the Executor’s view diverges from reality.

**Implementation guidance:**
- Use a dedicated **high-priority queue** for these events.
- Make it unbounded or large.

### Category B — OK TO COALESCE (latest-wins)
These are “desired state updates”, not ground truth:
- **Strategy intents**
- Optional “nudge” events from market data (e.g., “TOB changed”) if you even send them

If the Executor is behind, processing older intents is pointless.
You want: **apply only the latest desired state.**

**Implementation guidance:**
- Use a **single-slot mailbox** (overwriteable) for latest intent, or a bounded queue of size 1–5.
- If full, replace old intent with newest (drop intermediate intents).

### Category C — SHOULD NOT BE ENQUEUED AT ALL
These are too high-rate and not necessary for Executor correctness:
- Polymarket full book updates / deltas
- Binance tick-by-tick BBO updates (in Executor inbox)

These belong in caches that Strategy reads.

### Category D — TIMER TICKS (droppable)
Timer ticks are helpful for timeouts and periodic checks, but if the Executor is busy:
- it is fine to skip ticks and process the next one

**Implementation guidance:**
- Use one timer tick per interval, but don’t queue 1000 ticks if stalled.
- Use a “tick due” boolean or schedule next tick explicitly.

---

## 4) Queue backpressure and overload handling

### What it means if a queue grows
A growing Executor inbox is a red flag:
- your processing is too slow, or
- you are enqueueing too much (usually book updates), or
- you’re blocking in the Executor loop (e.g., synchronous network I/O).

### The correct fixes (in order)
1. **Stop enqueuing market data into the Executor.** Put it in caches only.
2. **Coalesce Strategy intents** (latest-only).
3. **Move network I/O out of the Executor thread** (Gateway worker).
4. Reduce heavy work in event handlers (logging, JSON conversions, formatting).
5. Lower Strategy frequency if it’s excessive.

### Backpressure policies (recommended)
- High-priority queue (fills/acks): do not drop; ensure it never blocks producers.
- Intent mailbox: overwrite old intents when a new one arrives.
- Timer tick: if a tick is already pending, don’t enqueue another.

---

## 5) “How do I know my open orders if I don’t enqueue book updates?”

You track open orders using a **state machine** driven by:
- what the Executor instructed the Gateway to do (desired actions), and
- what the user stream confirms (acks/fills/cancel-acks).

### Order lifecycle (minimal)
- `PENDING_NEW` → `WORKING` (on ack)
- `WORKING` → `PENDING_CANCEL` (when you request cancel)
- `PENDING_CANCEL` → `CANCELED` (on cancel-ack)
- `WORKING` → `PARTIALLY_FILLED` → `FILLED` (on fills)

Book updates are not required for this.
Book updates only inform Strategy’s desired quotes.

### Resync on uncertainty
If you miss user events due to disconnect:
- simplest safe policy: **cancel-all on reconnect**, then restart quoting from scratch
- alternative (more complex): REST query open orders, reconcile, then continue

For a first production system: **cancel-all on reconnect** is recommended because it is deterministic and safe.

---

## 6) Keep WS handlers extremely fast

WS handlers should do the minimum and return:
- parse message quickly
- update cache (build snapshot → atomic swap)
- for user events: push a small structured event into Executor high-priority queue

### WS handler anti-patterns
Avoid in WS callbacks:
- computing features (vol/shock) on every message
- heavy logging / string formatting
- calling blocking I/O (REST, file writes)
- building large nested objects repeatedly

### Snapshot cache best practice
- Build an **immutable snapshot object**
- Swap a single reference to “latest”
- Keep a small ring buffer of last N mids/trades for Strategy

---

## 7) Make Strategy fast and stable (debounce + fixed Hz)

Strategy should be designed so it *cannot* overwhelm the system:
- run on a fixed heartbeat (e.g., 20–50 Hz)
- read the latest snapshots
- compute features incrementally (EWMA updates, not full window recomputes)
- emit intent only when it materially changes (price/size/mode)

### Debouncing rule
Don’t emit a new intent if:
- desired prices are unchanged, and
- mode is unchanged, and
- sizes are unchanged (or within tolerance)

This reduces churn and gateway load.

---

## 8) Keep Executor fast: do not block on network I/O

The Executor loop must remain responsive to fills/acks and timeouts.
Therefore avoid blocking network calls inside the Executor event handler.

### Recommended pattern
- Executor emits `GatewayAction`s to a **Gateway worker** (separate thread/task).
- Gateway worker performs network I/O and returns `GatewayResultEvent`s to Executor.

This prevents:
- stuck cancels
- delayed inventory updates
- runaway queues

---

## 9) Threading vs asyncio on a small VPS (2 vCPUs)

Both can work. The best choice depends on your current codebase and comfort.

### Recommendation for this specific bot
**Threaded actor + WS threads** is usually simplest and very reliable on 2 vCPUs.

#### Why threading works well here
- WS clients often run fine in their own threads.
- The Executor actor is a single thread with deterministic event processing.
- Strategy runs in a fixed-frequency thread reading caches.
- Gateway is another thread to isolate blocking I/O.

This maps cleanly to your components and avoids complex async cancellation semantics.

### A good default threading layout (single event)
- Thread 1: Polymarket book WS → PMCache
- Thread 2: Polymarket user WS → Executor high-priority queue
- Thread 3: Binance WS → BNCache
- Thread 4: Strategy heartbeat → intent mailbox/queue
- Thread 5: Executor actor → emits actions
- Thread 6: Gateway worker (optional but recommended)

On 2 vCPUs, 5–6 threads is fine because most are I/O bound.
The CPU-heavy part (Strategy + Executor) stays small.

### Asyncio alternative
Asyncio can be excellent if:
- your WS libs are asyncio-native,
- your Gateway calls are async,
- you are disciplined about not blocking the event loop.

If you use asyncio:
- still keep the Executor as a single consumer task
- use `asyncio.Queue` for high-priority events
- use an overwriteable structure for latest intents

**Avoid mixing** heavy CPU work in the asyncio loop; offload to a thread pool if needed.

### Practical decision rule
- If you already have working asyncio WS code: stay with asyncio.
- If you’re building from scratch: start with threading for speed-to-correctness.

---

## 10) Micro-optimizations that actually matter in Python

### 10.1 Keep hot-path data primitive
- prices as `int` (cents)
- sizes as `int`
- timestamps as `int` (monotonic ns or ms)
- avoid float math in the Executor hot path

### 10.2 Reduce object churn
- preallocate ring buffers
- avoid building giant dicts per update
- keep snapshots minimal (TOB + a few fields)

### 10.3 Logging discipline
Logging is often the biggest hidden latency.
- log less on hot path
- use structured logs with deferred formatting
- sample high-rate logs (e.g., 1 per second)

### 10.4 Use monotonic time everywhere for logic
- `monotonic_ns()` for staleness and timeouts
- wall time only for reporting

### 10.5 Keep JSON parsing and validation light
- parse only required fields
- avoid repeated conversions
- if available, use a faster JSON library (optional), but keep architecture clean first

---

## 11) Operational safety rules (minimum viable “correctness”)

### Never compromise these
- Do not drop fills/acks.
- Cancel-all fast path must preempt normal throttles.
- On reconnect or state uncertainty: cancel-all + cooldown.
- Executor must be the only writer of order/inventory state.

### The intended “failure mode”
In uncertain conditions, the system should:
1) stop quoting
2) cancel all
3) wait briefly
4) re-enter cautiously

A safe bot that sometimes stops is better than a fast bot that occasionally goes blind.

---

## 12) Checklist for a coding agent (implementation guidance)
- [ ] Implement caches with immutable snapshots + atomic swap.
- [ ] Implement high-priority Executor queue for user events (lossless).
- [ ] Implement latest-intent mailbox (coalescing).
- [ ] Implement Executor actor loop (single thread/task).
- [ ] Move Gateway I/O to a worker to avoid blocking Executor.
- [ ] Implement staleness checks and cancel-all on hard stale.
- [ ] Implement timeouts for pending cancels/places.
- [ ] Add minimal structured logs and key metrics.

---

## 13) Summary
This design stays efficient on a small VPS by:
- keeping WS handlers lightweight,
- preventing book-data bursts from clogging the Executor,
- coalescing Strategy intents,
- isolating state mutation into a single-writer Executor,
- avoiding blocking I/O inside the Executor loop,
- treating user acks/fills as lossless, high-priority inputs.

If you adhere to the drop/coalesce rules and keep the single-writer invariant, you get a trading system that remains correct and responsive even under bursty WS traffic and imperfect network conditions.
