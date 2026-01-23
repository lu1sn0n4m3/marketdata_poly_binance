# Implementation Task Overview

## Binary CLOB Market-Making System for Polymarket Bitcoin Hourly Markets

This document provides an overview of the implementation tasks for building a simplified, efficient market-making system based on the designs in `polymarket_mm_framework.md` and `polymarket_mm_efficiency_design.md`.

---

## Architecture Summary

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                           MM APPLICATION                                      │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                               │
│  ┌──────────────┐    ┌──────────────┐    ┌──────────────┐                   │
│  │ PM Market WS │────▶│   PMCache   │◀───│ Strategy     │                   │
│  │              │    │ (atomic)     │    │ Engine       │                   │
│  └──────────────┘    └──────────────┘    │ (fixed Hz)   │                   │
│                                           └──────┬───────┘                   │
│  ┌──────────────┐    ┌──────────────┐           │                           │
│  │ PM User WS   │────▶│ Executor    │◀──────────┘ Intent                    │
│  │ (fills/acks) │    │ Actor       │  Mailbox                               │
│  └──────────────┘    │ (single     │                                        │
│                      │  writer)    │                                        │
│  ┌──────────────┐    └──────┬───────┘                                        │
│  │ BN Poller    │────▶ BNCache        │                                      │
│  │              │    │ (atomic)       │                                      │
│  └──────────────┘    └────────────────┴────────────┐                        │
│                                                     │                        │
│                      ┌──────────────┐               │ Actions               │
│                      │   Gateway    │◀──────────────┘                        │
│                      │   Worker     │                                        │
│                      └──────┬───────┘                                        │
│                             │ REST                                           │
│                             ▼                                                │
│                      ┌──────────────┐                                        │
│                      │ Polymarket   │                                        │
│                      │ REST API     │                                        │
│                      └──────────────┘                                        │
│                                                                               │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## Task Execution Order

### Phase 1: Foundation (No Dependencies)

| Task | Name | Priority | Complexity |
|------|------|----------|------------|
| 01 | Data Types | Critical | Medium |
| 08 | Gamma Market Finder | High | Medium |

These can be implemented in parallel as they have no dependencies.

### Phase 2: Infrastructure

| Task | Name | Dependencies | Priority |
|------|------|--------------|----------|
| 02 | Snapshot Caches | Task 01 | Critical |

### Phase 3: Data Feeds

| Task | Name | Dependencies | Priority |
|------|------|--------------|----------|
| 03 | PM Market WS | Task 01, 02 | Critical |
| 04 | PM User WS | Task 01 | Critical |

These can be implemented in parallel.

### Phase 4: Order Execution

| Task | Name | Dependencies | Priority |
|------|------|--------------|----------|
| 05 | Gateway | Task 01 | Critical |

### Phase 5: Core Logic

| Task | Name | Dependencies | Priority |
|------|------|--------------|----------|
| 06 | Strategy Engine | Task 01, 02 | High |
| 07 | Executor Actor | Task 01-06 | Critical |

Task 06 and 07 can be partially developed in parallel.

### Phase 6: Integration

| Task | Name | Dependencies | Priority |
|------|------|--------------|----------|
| 09 | Application Wiring | Task 01-08 | Critical |

---

## Key Design Principles

### 1. Single-Writer Invariant
Only the Executor mutates trading state (orders, inventory). Everything else produces inputs.

### 2. Event Priority Categories
- **Lossless (A)**: Fills, acks, cancel-acks - NEVER DROP
- **Coalescing (B)**: Strategy intents - latest-only
- **Not Queued (C)**: Market data - goes to caches

### 3. Decouple Market Data
Market data floods the WS. It goes to atomic caches, NOT the Executor queue.

### 4. Fixed-Hz Strategy
Strategy runs at stable cadence (20-50 Hz), reads caches, emits intents.

### 5. Non-Blocking Gateway
Gateway runs in separate task, Executor never blocks on network I/O.

### 6. Cancel-All Fast Path
Cancel-all is never rate-limited, always preempts normal operations.

---

## File Structure

```
services/polymarket_trader/src/polymarket_trader/
├── mm_types.py           # Task 01: Core data types
├── pm_cache.py           # Task 02: Polymarket cache
├── bn_cache.py           # Task 02: Binance cache
├── pm_market_ws.py       # Task 03: Market WS client
├── pm_user_ws.py         # Task 04: User WS client
├── gateway.py            # Task 05: Gateway worker
├── strategy_engine.py    # Task 06: Strategy + runner
├── intent_mailbox.py     # Task 06: Coalescing queue
├── executor_actor.py     # Task 07: Executor
├── order_materializer.py # Task 07: Intent → Orders
├── gamma_client.py       # Task 08: Gamma API
├── market_finder.py      # Task 08: Market discovery
├── market_scheduler.py   # Task 08: Hour transitions
├── bn_poller.py          # Task 09: Binance polling
├── mm_app.py             # Task 09: Main application
└── config.py             # Configuration
```

---

## Testing Strategy

Each task includes specific unit tests. Integration testing should cover:

1. **Startup sequence**: Market discovery, WS connections, component initialization
2. **Normal operation**: Strategy → Executor → Gateway flow
3. **Fill handling**: Inventory updates, order state transitions
4. **Staleness**: Cancel-all triggered on stale data
5. **Reconnection**: Cancel-all on WS reconnect
6. **Hour transition**: Market change, pause before hour end

---

## Configuration Environment Variables

```bash
# API Credentials
PM_PRIVATE_KEY=0x...
PM_FUNDER=0x...
PM_API_KEY=...
PM_API_SECRET=...
PM_PASSPHRASE=...

# URLs
BINANCE_SNAPSHOT_URL=http://localhost:8080/snapshot/latest

# Strategy
STRATEGY_HZ=20
BASE_SPREAD_CENTS=2
BASE_SIZE=100

# Limits
GROSS_CAP=1000
MAX_POSITION=500

# Timeouts
PM_STALE_THRESHOLD_MS=500
BN_STALE_THRESHOLD_MS=1000
CANCEL_TIMEOUT_MS=5000

# Logging
LOG_LEVEL=INFO
```

---

## Risk Parameters

From the design documents, these are the critical safety parameters:

| Parameter | Default | Description |
|-----------|---------|-------------|
| PM stale threshold | 500ms | Cancel-all if PM data older |
| BN stale threshold | 1000ms | Cancel-all if BN data older |
| Cancel timeout | 5000ms | Escalate if cancel not acked |
| Cooldown after cancel-all | 3000ms | Wait before re-entering |
| Gross cap | 1000 shares | Max YES + NO combined |
| Max position | 500 shares | Max net exposure |

---

## References

- [polymarket_mm_framework.md](./polymarket_mm_framework.md) - Main architecture design
- [polymarket_mm_efficiency_design.md](./polymarket_mm_efficiency_design.md) - Performance and correctness guide
- [Polymarket CLOB Docs](https://docs.polymarket.com/developers/CLOB/)
- [Gamma API Docs](https://docs.polymarket.com/developers/gamma-markets-api/)
