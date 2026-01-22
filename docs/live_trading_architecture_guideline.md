# Live Trading Architecture Additions (Amsterdam VPS)

This document describes **two new repo folders** intended for the Amsterdam live-trading VPS, following the “Container A (Data/Pricing) → Container B (Execution/Risk)” split.

The goal is **watertight behavior** under real-world failure modes (disconnects, bursts, partial outages), while keeping the system **lean and efficient** in Python. This is **not** a strategy spec and **not** a pricer spec — it’s an architectural guideline so you can build those later without the bot ever “going out of control”.

---

## High-level goal

You want a trading system where:

- **Execution can always cancel / stop**, even if data ingestion becomes slow or unstable.
- Market data processing is **bounded** (no unbounded queues / backlogs).
- Risk limits are **hard**, not “best effort”.
- Restarts are safe: the bot comes back up and **reconciles** and **defaults to safe**.
- You can quickly answer: *“What is the bot doing right now and why?”*

---

## Proposed repo layout (add two folders)

You already have:

- `collector/` and `uploader/` for the Nürnberg research recorder

Add these for Amsterdam live trading:

```
repo/
  collector/
  uploader/
  live_data/              # NEW (Container A)
  execution/              # NEW (Container B)
  shared/                 # OPTIONAL (schemas + utilities only; no side effects)
  docker/
    docker-compose.live.yml
```

### Folder 1: `live_data/` (Container A — Data / Pricing Inputs)

**Responsibility:** connect to Binance WS (initially trades; later BBO), compute **minimal, crucial derived state**, and publish **latest-state** snapshots to Container B.

This folder must **never** place orders and must not need Polymarket credentials.

### Folder 2: `execution/` (Container B — Execution / Risk)

**Responsibility:** maintain trading state, enforce risk, generate quotes, and manage Polymarket orders.

This folder is the only place with **trading keys** and the only place allowed to submit or cancel orders.

---

## Core design principle: keep the handoff bounded

The biggest performance + safety win is to avoid pushing every WS message into execution.

**Use a “latest state” contract**:

- Container A produces a compact state object (e.g., fair price, last trade, short vol).
- Container B reads the most recent state when it needs to act.
- Under burst, you overwrite old state; you do not accumulate backlog.

This prevents queue explosions and reduces event loop jitter.

---

## Container A (`live_data/`) architecture

### Goals
- Stay connected to Binance.
- Be fast at parsing and cheap per message.
- Produce stable derived metrics (broadly: *fair, last, vol, freshness*).
- Publish **only what execution needs**.

### Suggested module structure

```
live_data/
  app.py                  # entrypoint: starts asyncio tasks
  binance_ws.py            # WS connection, reconnect, heartbeat
  transforms/
    trades.py              # trade parsing + lightweight aggregations
    bbo.py                 # (optional later) best-bid/offer parsing
  pricer_inputs.py         # produces derived state (NOT the final pricer)
  publish.py               # IPC publisher (Redis/NATS/TCP)
  config.py
  health.py                # liveness/readiness, last_msg_ts, reconnect_count
  logging.py
```

### Keep it efficient (Python reality)
- Don’t do heavy work in the WS callback. Parse → update in-memory aggregates → publish at a controlled cadence.
- Prefer **coalescing** over queueing.
- Do not log per tick; log on intervals and on anomalies.
- Use a fast JSON parser if needed (e.g., `orjson`) and keep allocations down.

### Publishing cadence
Even if Binance pushes 100–1000 msgs/sec, you usually do not need to publish that fast. Common patterns:

- Publish “latest_state” every **50–100ms** (10–20 Hz), or
- Publish on material change (e.g., price moved by X), but rate-limit.

### IPC options (choose one, keep it simple)
**Best “simple and good” choices:**
1. **Redis (local)**: `SET latest:BTCUSDT <blob>` (+ pubsub optional)
2. **NATS (local)**: publish on `prices.BTCUSDT`, consumer takes latest
3. **Local TCP/UDP**: custom tiny protocol; fastest but more bespoke

For a first live version, Redis locally on the same VPS is hard to beat for simplicity.

---

## Data contract between containers

Define a single schema for “latest pricing inputs”. Keep it small and stable.

Example fields (conceptual):

- `symbol` (e.g., BTCUSDT)
- `ts_exchange` (Binance event time if available)
- `ts_recv` (when Container A received it)
- `last_price`
- `mid_price` (if BBO)
- `fair_price` (placeholder; can initially equal last/mid)
- `vol_ewma` (placeholder)
- `staleness_ms` (computed at read time or included)

**Rule:** Container B must be able to handle missing fields (forward compatibility), and must treat stale data as “unsafe to trade”.

---

## Container B (`execution/`) architecture

### Goals
- Make all trading decisions **safe by construction**.
- Never place an order unless risk checks pass.
- Always be able to cancel quickly and stop.
- Recover safely after restart.

### Suggested module structure

```
execution/
  app.py                  # entrypoint: starts components, main loop
  state/
    models.py             # dataclasses: inventory, orders, positions
    store.py              # in-memory + optional persistent snapshot
    reconcile.py          # on startup: fetch open orders, positions
  risk/
    limits.py             # hard limits (cap, inventory, max orders)
    circuit_breakers.py   # stale data, volatility shock, venue issues
  strategy/
    quoter.py             # quote intent (broad; no alpha here)
  order/
    manager.py            # idempotent place/replace/cancel orchestration
    client.py             # Polymarket API client (REST/WS)
    rate_limits.py
  inputs/
    pricing_reader.py     # reads Container A latest_state
    polymarket_feed.py    # optional: market status, fills, end-of-market
  ops/
    kill_switch.py        # manual + automatic stop trading
    metrics.py            # timing, p95/p99 lat, loop lag, open orders
    logging.py            # structured audit logs
  config.py
  health.py
```

### Separation of concerns (crucial)

Think in four layers, each with a tight contract:

1. **Inputs**: pricing state + venue state  
2. **State**: inventory, open orders, cash usage, last actions  
3. **Risk**: “allowed to trade?” and “how much?” (hard gates)  
4. **Order manager**: “make the venue match my intent” (idempotent)

If you keep these boundaries clean, you can change the strategy later without turning the bot into a hazard.

---

## Execution loop model (safe and efficient)

A good default approach is a **tick-based loop** (e.g., 10–20 Hz):

On each tick:
1. Read latest pricing inputs from IPC.
2. Update staleness and health signals.
3. Fetch/process venue events (fills, cancels, market end).
4. Run **risk gates**:
   - If not safe: cancel orders, do not place.
5. Compute quote *intent* (desired bid/ask + size).
6. Order manager diffs desired vs. current and applies minimal changes.

Why tick-based?
- Predictable CPU usage
- Easy to instrument
- Easy to enforce “max order churn”
- Less sensitivity to bursts

Avoid “react on every WS message” unless you need that; it’s how you get jitter.

---

## Order manager requirements (non-negotiable)

Even with a simple strategy, your order manager must:

- Be **idempotent**: repeated requests should not create duplicates.
- Be **state-aware**: track open orders and confirmations.
- Handle retries with jittered backoff.
- Support “cancel all” reliably.
- Avoid thrashing: enforce minimum replace interval, min price change threshold.

**Restart behavior:**
- On startup, immediately:
  1. Load local state (if any)
  2. Query venue for open orders + positions
  3. Reconcile: if unsure, **cancel** and rebuild from clean slate

Default restart stance should be **safe**, not “resume trading blindly”.

---

## Risk system: hard caps + circuit breakers

### Hard caps (always enforced)
Examples (conceptual):
- Max notional per market
- Max total notional across all markets
- Max inventory (by side)
- Max number of open orders
- Max order rate (per second/minute)

### Circuit breakers (automatic stop conditions)
- Pricing input stale > X ms
- Too many API errors in Y seconds
- Inconsistent state (unknown open orders)
- Venue maintenance / market resolved / trading halted
- Latency spike beyond threshold (optional)

**Action on trigger:** immediate “cancel all” + disable new placements until manual reset.

---

## “Stop trading” UX: simple, reliable, friendly

You need a stop mechanism that works even when you’re stressed.

Recommended approach: **one shared “trading_enabled” flag** that risk checks first.

Good implementations:
- Redis key: `SET trading:enabled 0`
- File flag: `/run/trading_enabled` (exists => trade)
- HTTP endpoint on localhost protected by token

### Minimum UX spec
- A single command:
  - `make stop` → sets flag off and requests cancel-all
  - `make start` → sets flag on (after health checks)
- Bot prints a clear banner:
  - `TRADING ENABLED` vs `TRADING DISABLED`
- On disable:
  - cancel all orders
  - keep monitoring (inputs, fills) but do not place new orders

Make sure the bot can’t “re-enable itself” after a crash without you explicitly allowing it.

---

## Observability: what you must log

Live trading without visibility is gambling.

### Audit log (append-only, structured)
Record every:
- intent (what you wanted to do)
- risk decision (why allowed/blocked)
- order action (place/replace/cancel + ids)
- fill event
- reconciliation event
- circuit breaker trigger

A JSONL log file is fine. Rotate daily. Ship asynchronously if you want.

### Real-time status
Expose a small status view (CLI or HTTP on localhost):
- trading enabled?
- last pricing ts / staleness
- open orders count
- inventory
- last action times
- error counters
- p95/p99 place/cancel latencies
- event loop lag

This is your “am I safe right now?” dashboard.

---

## Deployment suggestions (keep it minimal)

Use `docker-compose` on the Amsterdam VPS:

- `binance-live-data` (Container A)
- `polymarket-execution` (Container B)
- Optional: `redis` (IPC + kill switch + lightweight state)
- Optional: `vector` or `fluent-bit` for log shipping (later)

**Do not** store trading secrets in the data container.

---

## Testing modes you should build in early

You’ll move faster if you bake these in now:

- **Dry-run mode**: compute intents, run risk, but do not place orders.
- **Paper mode**: place/cancel to a sandbox if available or simulate locally.
- **Capped-live mode**: trade with tiny caps and strict churn limits.
- **Chaos hooks**: simulate Binance disconnect, Polymarket WS drop, delayed acks.

A bot that survives chaos testing is a bot you can sleep near.

---

## Practical “water tight” checklist

If you do nothing else, ensure these are true before increasing size:

- Execution never blocks on ingestion (bounded handoff).
- Kill switch works and cancels orders quickly.
- Risk gates run before every order action.
- Restart always reconciles and defaults to safe.
- Audit log is complete enough to reconstruct what happened.
- You can see staleness, open orders, inventory at a glance.

---

## Closing note

This architecture keeps Python viable and keeps the system sane under stress by:
- separating the **data plane** from the **execution plane**
- enforcing **hard risk boundaries**
- using **bounded latest-state** messaging instead of queues

Once strategy/pricer complexity grows, you can optimize individual pieces, but you won’t need to rewrite the whole system because the boundaries will already be correct.
