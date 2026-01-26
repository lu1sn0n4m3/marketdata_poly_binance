# CLAUDE.md — Feeds (Market Data + User Event Ingestion)

This directory contains **threaded data feeds** that ingest external events
and push them into caches or queues.

Feeds are **I/O only**. They must be fast, boring, and failure-aware.

---

## What feeds are responsible for

- Maintaining external connections (WebSocket or HTTP polling).
- Receiving raw market data or user events.
- Parsing messages into structured updates.
- Publishing updates to:
  - caches (market data), or
  - queues (user events).

Feeds do not make trading decisions.

---

## What feeds must NEVER do

- Never place or cancel orders.
- Never interpret strategy intent.
- Never mutate executor state directly.
- Never infer safety decisions (“looks fine, keep trading”).
- Never block on heavy computation or downstream logic.

If a feed starts “thinking,” the architecture is broken.

---

## Threading and performance rules (non-negotiable)

- All feeds run in **daemon threads**.
- `_handle_message()` must be:
  - fast,
  - non-blocking,
  - allocation-light.
- Heavy work (features, risk checks, reconciliation) belongs elsewhere.

Blocking rules:
- Market data feeds must never block on consumers.
- User event feeds may block **only** on the event queue to preserve event integrity.

If a feed blocks unpredictably, you will eventually miss heartbeats or reconnect loops.

---

## Disconnections are safety events

Especially for **authenticated user feeds**, a disconnect is not “just a reconnect.”

Rules:
- Any disconnect on the user feed must trigger a safety callback
  (e.g., cancel-all, STOP mode).
- Reconnect happens after safety action, not before.

Rationale:
- A disconnected user feed means you are blind to fills.
- Trading while blind is unacceptable.

---

## websocket_base.py: base class constraints

`ThreadedWsClient` provides:
- daemon-thread lifecycle (`start()` / `stop()`)
- reconnection with exponential backoff + jitter
- connection state tracking

Subclass rules:
- `_subscribe()` must only send subscription messages.
- `_handle_message()` must parse and publish, not decide.
- `_on_connect()` / `_on_disconnect()` must be lightweight.

Do not override reconnection logic to “optimize” it.
Backoff and jitter are safety features.

---

## BinanceFeed (HTTP polling)

Purpose:
- ingest reference-market context (BTC price, pricer output).

Constraints:
- polling frequency must be bounded (e.g., 20 Hz).
- failures should increment stats, not crash the thread.
- partial or failed responses are allowed.

BinanceFeed must not:
- attempt to synchronize with Polymarket state,
- gate trading directly,
- retry in tight loops.

Staleness decisions are handled by Strategy or Executor.

---

## PolymarketMarketFeed (public market data)

Purpose:
- ingest public order book updates (BBO).

Constraints:
- updates go directly into `PolymarketCache`.
- feed may see:
  - full snapshots,
  - partial updates,
  - out-of-order messages.

Consumers must handle inconsistency safely.
Feed must not attempt to “repair” the book.

---

## PolymarketUserFeed (authenticated user events)

Purpose:
- ingest **authoritative** user events:
  - order acks,
  - cancel acks,
  - fills.

Critical guarantees:
- **Events must never be dropped.**
- If the event queue is full, the feed must block.

Why blocking is correct here:
- Dropping user events causes permanent state divergence.
- Backpressure is safer than silent loss.

Disconnect behavior:
- On disconnect, invoke `on_reconnect` callback immediately.
- Typical action: cancel-all + executor STOP/cooldown.

Reconnect does not imply safety.
Safety must be re-established explicitly.

---

## Data flow boundaries (do not cross)

Allowed:

Feed → Cache
Feed → Queue

Forbidden:

Feed → Executor methods
Feed → Strategy methods
Feed → Gateway

All coordination happens above feeds.

---

## Initialization and shutdown ordering

Correct order:
1. Create caches and queues.
2. Create feeds.
3. Configure tokens, auth, markets.
4. Start feeds.
5. On shutdown: stop feeds first.

Feeds must tolerate:
- starting before data is available,
- stopping while mid-reconnect,
- being restarted after failure.

---

## Where changes usually go wrong

Common regressions:
- adding “just a little logic” in `_handle_message`
- blocking on executor calls
- treating reconnect as harmless
- swallowing parse errors silently
- assuming WS message order or completeness

If a feed becomes complicated, it’s doing too much.

---

## Testing philosophy

Feeds should be tested for:
- reconnect behavior
- correct publish-on-message behavior
- disconnect safety callbacks
- thread start/stop correctness

Feeds should NOT be tested for:
- trading behavior
- strategy correctness
- executor state transitions

---

## Summary

Feeds are plumbing.

They:
- connect,
- receive,
- publish,
- reconnect,
- get out of the way.

All intelligence lives elsewhere.