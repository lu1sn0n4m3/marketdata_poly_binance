# CLAUDE.md — Caches (Market Data Snapshot Stores)

This directory contains **thread-safe, best-effort caches** for market data snapshots.
Caches are memory, not truth.

They exist to decouple data ingestion (feeds) from decision-making (strategy, executor)
without introducing locks, blocking, or hidden logic.

---

## What caches are responsible for

- Holding the **latest known snapshot** of market data.
- Providing **lock-free reads** for multiple consumers.
- Tracking **snapshot age and sequence numbers**.
- Offering lightweight, read-only convenience accessors.

Caches do not make decisions.

---

## What caches must NEVER do

- Never place orders, cancel orders, or trigger actions.
- Never infer trading intent or risk state.
- Never “fix” stale data by guessing or extrapolating.
- Never mutate shared objects after publication.
- Never block readers or writers.
- Never contain strategy or executor logic.

If you are tempted to add “smart” behavior here, you are breaking the architecture.

---

## Mental model (important)

Caches are **best-effort memory**:

- Data may be stale.
- Data may jump.
- Data may briefly be inconsistent across sources.
- Reads may race with writes.

This is expected.

**Strategy and Executor must treat cache data as advisory, not authoritative.**
Safety decisions (staleness kill-switches, STOP mode, cancel-all) belong upstream.

---

## Single-writer, multi-reader rule

Each cache follows a strict pattern:

- **Exactly one writer** (feed or poller thread).
- **Many readers** (strategy, executor, monitoring).
- No reader ever blocks the writer.
- No writer waits for readers.

`LatestSnapshotStore` exists specifically to enforce this pattern.

Do not add locks or shared mutation.

---

## Snapshot semantics

Every snapshot must be:
- immutable after publication,
- timestamped,
- sequence-numbered.

Consumers rely on:
- `seq` for monotonicity,
- `age_ms` / `is_stale()` for safety checks,
- the fact that a snapshot is self-consistent.

Never mutate a snapshot in place after it has been published.

---

## BinanceCache: intent and limits

`BinanceCache` stores **reference-market context**, not trading signals.

It may include:
- BBO prices
- recent returns / volatility features
- shock indicators
- fair value output from the pricer
- time-remaining metadata

It must not:
- gate trading directly,
- trigger STOPs or cancels,
- attempt to synchronize with Polymarket state.

If Binance data is stale or missing, **that decision is handled by Strategy or Executor**, not here.

---

## PolymarketCache: intent and limits

`PolymarketCache` stores **top-of-book state** and minimal rolling history.

It may include:
- YES/NO BBO snapshots
- market and token identifiers
- small rolling buffers for micro-features

It must not:
- track fills, orders, or user state,
- assume book correctness or completeness,
- reconcile YES/NO symmetry (that’s executor logic),
- enforce min-tick or pricing rules.

Book data may be partial or jumpy; consumers must handle this safely.

---

## Staleness is a signal, not an action

Caches expose staleness checks:
- `get_age_ms()`
- `is_stale(threshold_ms)`

They do **not** act on staleness.

Examples of correct usage:
- Strategy: “data invalid → STOP intent”
- Executor: “data stale → cancel-all and cooldown”

Incorrect usage:
- cache refusing to update
- cache clamping values
- cache emitting synthetic data

---

## Common interface contract

All caches expose:
- `get_latest() -> (snapshot, seq)`
- `seq` (monotonic)
- `has_data`
- `get_age_ms()`
- `is_stale(threshold_ms)`
- `clear()`

Consumers must tolerate:
- `snapshot is None`
- non-consecutive `seq`
- sudden jumps in values

---

## Where changes usually go wrong

Common architectural regressions:
- adding derived trading logic (“this mid looks wrong, fix it”)
- mutating snapshot objects after publication
- adding locks “for safety”
- letting executor assume cache data is fresh or complete
- mixing REST and WS semantics inside the cache

If correctness depends on cache perfection, the design is already broken.

---

## Testing philosophy

Caches should be tested for:
- thread-safety under concurrent reads/writes
- immutability of published snapshots
- correct staleness and sequence behavior

They should NOT be tested for:
- trading behavior
- strategy correctness
- executor safety logic

---

## Summary

Caches are deliberately boring.

They:
- remember the latest thing they saw,
- tell you how old it is,
- get out of the way.

All real decisions happen elsewhere.