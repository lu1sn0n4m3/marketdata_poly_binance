# Binary CLOB Market-Making System Framework (Polymarket YES/NO + Binance Reference)
*A minimal, production-oriented architecture for complementary binary tokens under a no-short constraint, optimized for low-latency quoting, regime switching, and adverse-selection control.*

> **Core idea:** Strategy thinks in one canonical instrument (YES probability/price). Executor is the single-writer that safely materializes those synthetic YES quotes into legal orders on YES and/or NO, using inventory-aware complement mapping to minimize gross collateral usage and avoid race conditions.

---

## 0) Scope and design goals

### Intended market type
- Binary event with two complementary tokens: **YES (UP)** and **NO (DOWN)**.
- Tick size: typically **1 cent**.
- The event resolves to **1 or 0** at expiry based on an external reference (e.g., BTC price relative to a reference level).
- You have real-time streams:
  - **Polymarket WS**: full book (or deltas), trades, plus user executions/acks (or equivalent).
  - **Binance WS**: BTC BBO + trades.

### Key constraints
- **No short selling**: You can only place a SELL (ask) on a token if you already hold inventory of that token.
- YES/NO are complements: quoting on one side can be expressed as quoting on the other side at `1 - p`.

### Primary risks to manage
- **Adverse selection**: getting filled right before a repricing/trend continues.
- **Latency & race conditions**: staleness between snapshots, fills, pending cancels, and strategy updates.
- **Collateral efficiency**: avoid growing **gross inventory** (YES + NO) unnecessarily.

### Hard requirements (non-negotiable)
1. **Strategy never sends live orders.** It only produces a “desired” quote state in canonical YES space.
2. **Executor/OrderManager is the single writer** of trading state (open orders, pending actions, inventory).
3. The Executor must **enforce no-short and complement mapping** deterministically.
4. Executor may **override** Strategy to STOP/cooldown for staleness, shocks, API errors, or inventory caps.

---

## 1) High-level architecture

### Components
1. **Market Data Feeds (event-driven)**
   - `PolymarketFeed`
     - Maintains connectivity; parses WS events.
     - Produces order book updates, trade prints.
     - Produces user-specific events: order acks, cancels, fills.
   - `BinanceFeed`
     - Parses BBO and trades.
     - Produces price/flow signals for the reference market.

2. **Snapshot Stores (in-memory caches)**
   - `PMCache`: latest Polymarket snapshot + rolling history for micro-features.
   - `BNCache`: latest Binance snapshot + rolling history for returns/flow.
   - Must allow fast, low-contention reads by Strategy and (optionally) Executor staleness checks.

3. **Strategy Engine (stateless intent generator)**
   - Reads snapshots and a small inventory summary.
   - Outputs **DesiredQuoteSet** in canonical YES space:
     - mode (NORMAL/CAUTION/ONE_SIDED/STOP)
     - desired YES bid/ask price + size
   - Does **not** track open orders or pending cancels.

4. **Executor / Order Manager (single-writer actor)**
   - Owns truth of: open orders, pending cancels/places, inventory, cooldown state.
   - Receives Strategy intents and user execution events.
   - Materializes synthetic YES quotes into legal orders on YES/NO.
   - Reconciles desired vs actual while preventing races and excessive churn.

5. **Gateway**
   - The only component allowed to interact with Polymarket APIs.
   - Accepts order/cancel/cancel-all requests.
   - Emits acks/errors back to Executor.

---

## 2) Runtime model (event-driven vs fixed frequency)

### Feeds: pure WS clock (event-driven)
- `PolymarketFeed` and `BinanceFeed` update caches immediately upon WS messages.
- Each snapshot must be stamped with:
  - local monotonic timestamp (for staleness checks),
  - feed sequence number (monotonic counter per feed).

### Strategy: hybrid (heartbeat + triggers)
- **Heartbeat** (recommended): e.g., 20–50 Hz, stable cadence.
- **Optional triggers** (fast response):
  - Binance shock threshold crossing,
  - Polymarket TOB changes by ≥ 1 tick,
  - time-to-expiry bucket change (e.g., 20m/10m/3m).

Strategy may skip producing an intent if inputs are stale.

### Executor: single-threaded actor + safety tick
- Processes events deterministically, one at a time (actor model).
- Receives:
  - Strategy intents,
  - user acks/fills/cancel-acks,
  - gateway errors,
  - periodic timer ticks (e.g., 10–20 Hz) for timeouts and staleness enforcement.

**Single-writer rule:** only the Executor mutates order and inventory state.

---

## 3) Communication patterns (events and queues)

### Executor inbox event types
- `StrategyIntentEvent(intent)`
- `OrderAckEvent(order_id, status, details)`
- `CancelAckEvent(order_id, status)`
- `FillEvent(order_id, token, side, price, size, fee, ts)`
- `GatewayErrorEvent(action_id, error_kind, retryable)`
- `TimerTickEvent(now_ts)`

### Snapshot access
- Strategy reads directly from caches.
- Executor generally does not rely on book messages to function; it acts on intents and user events.
- Executor may read cache metadata (age/seq) to enforce global safety (stale-feed kill switch).

---

## 4) Canonical instrument model (YES space)

### Complement relationship
- YES price `p` implies NO price approximately `1 - p`.
- A synthetic YES ask at `A` is economically EQUIVALENT to a NO bid at `1 - A`.
- A synthetic YES bid at `B` is economically EQUIVALENT to a NO ask at `1 - B`.

### Inventory model (minimal but sufficient)
Track inventory per token:
- `I_yes`: YES shares held
- `I_no`: NO shares held

Define derived exposures:
- **Net exposure**: `E = I_yes - I_no` (settlement-relevant)
- **Gross exposure**: `G = I_yes + I_no` (collateral-relevant)

**Goal:** implement Strategy quotes while minimizing unnecessary increases in `G`, because `G` ties up collateral.

---

## 5) Strategy outputs only synthetic YES quotes

### Strategy output contract
Strategy produces a `DesiredQuoteSet` with at most two legs:
- synthetic YES bid: want to BUY YES at price `B`
- synthetic YES ask: want to SELL YES at price `A`

Strategy does not output “trade NO”. Executor decides how to express the quote legally.

### Regime modes (recommended)
- `STOP`: no quoting, cancel all (Executor enforces).
- `NORMAL`: two-sided quoting.
- `CAUTION`: wider spread and smaller size.
- `ONE_SIDED_BUY`: only want synthetic YES bid (buy exposure).
- `ONE_SIDED_SELL`: only want synthetic YES ask (sell exposure).

---

## 6) Executor materialization: mapping synthetic legs to legal orders (no-short + complement)

The Executor converts each synthetic YES leg into a real, legal order on YES or NO.

### Definitions
- Prices are assumed in cents (0–100) for simplicity.
- Complement transform: `p_no = 100 - p_yes` in cents.

### Materialization rules (SELL inventory first)
**Synthetic BUY YES @ B**
1. Preferred (if you hold NO): **SELL NO @ (100 - B)**, up to `I_no`
2. Fallback: **BUY YES @ B**

**Synthetic SELL YES @ A**
1. Preferred (if you hold YES): **SELL YES @ A**, up to `I_yes`
2. Fallback: **BUY NO @ (100 - A)**

This achieves two critical properties:
- You express asks without shorting by buying the complement.
- When you already have inventory on the complement, you can implement a synthetic BUY YES by **selling NO**, which reduces gross collateral usage.

### Size selection during materialization
For each synthetic leg size `q`:
- If expressing as a SELL of inventory, real size is `min(q, available_inventory)`.
- Remaining unfilled “synthetic size” (if any) may be expressed via fallback BUY order if the regime allows increasing gross.

### Gross-control policy (to avoid collateral blow-up)
Executor should enforce a `G_max` (gross cap). When `G` is high or cap is hit:
- Disallow actions that increase gross (i.e., new BUYs that add inventory on either token).
- Allow only SELLs that reduce existing inventory.
- If Strategy requests two-sided quoting but gross is near cap, downgrade to ONE_SIDED inventory reduction or STOP.

**Important:** This is not a “compression routine.” The system simply avoids building gross in the first place.

---

## 7) Tick rounding rules for complement conversion (prevent accidental aggressiveness)

When converting `p_no = 100 - p_yes`, rounding can make you tighter than intended.
Use conservative rounding based on order type:

- If you are creating a **BID** on a book, round **down** (less aggressive).
- If you are creating an **ASK** on a book, round **up** (less aggressive).

Example:
- Synthetic YES ask = 57c → synthetic NO bid = 43c.
  - If decimals exist due to internal math, ensure NO bid is floored.
- Synthetic YES bid = 53c → synthetic NO ask = 47c.
  - Ensure NO ask is ceiled.

With 1-cent ticks and integer pricing, this is mostly about maintaining consistent safeguards when internal computations are not integers.

---

## 8) Reconciliation-based order management (desired state → actual state)

### Why reconciliation
A market maker’s order state changes due to:
- strategy updates,
- partial fills,
- cancels in-flight,
- gateway delays and retries,
- WS bursts and missing messages.

Instead of issuing “do X now” imperatively, the Executor continuously reconciles:
- What Strategy wants (synthetic intent)
- What is legal and safe (risk constraints)
- What is already working (open orders)
- What actions are pending (cancel/ack)

### Minimal reconciliation loop
For each synthetic leg (YES bid and YES ask):
1. Materialize into a real desired order (or “no order”) based on inventory + constraints.
2. Compare desired real order vs currently working order on that side/token:
   - If identical (price and size within tolerance): do nothing.
   - Else:
     - If there is an existing order not already pending cancel: send cancel.
     - Once cancel is acknowledged (or safely timed out): place the new order.
3. Ensure you never place overlapping contradictory orders that violate your inventory/gross policies.

### Pending-action discipline
Maintain state per working order:
- `WORKING`
- `PENDING_CANCEL`
- `PENDING_NEW`
- `REJECTED/FAILED`

Timeout behavior:
- If cancel ack is not received within a configured timeout, escalate:
  - send cancel-all,
  - enter cooldown,
  - stop quoting until state is consistent again.

Rate limiting:
- Normal replace rate should be bounded (avoid self-induced lag).
- Cancel-all is always a fast path (no throttling).

---

## 9) Staleness, shocks, and cooldowns (Executor-level safety overrides)

Even if Strategy is perfect, the Executor must enforce global safety.

### Staleness guardrails
Define max snapshot ages:
- Polymarket data age threshold (e.g., 250–500 ms)
- Binance data age threshold (e.g., 250–1000 ms depending on feed)

If either is stale beyond a hard limit:
- Cancel all.
- Enter STOP/cooldown.
- Require fresh snapshots before re-entering.

### Shock triggers (reference market as primary)
Executor should support a “STOP fast path” when:
- Binance shock metric exceeds threshold (z-score jump),
- Polymarket book jumps multiple ticks rapidly,
- gateway error burst occurs,
- cancel/ack timeouts occur.

Cooldown logic:
- After cancel-all, wait a short cooldown (e.g., 1–3 seconds).
- Re-enter in CAUTION mode first (wider and smaller), then allow NORMAL if stable.

### Near-expiry risk multipliers
For 1-hour markets, near expiry is inherently dangerous, especially around 50c.
Executor should support stricter rules as `T` shrinks:
- tighter staleness thresholds,
- lower gross cap,
- smaller sizes,
- more frequent STOP transitions,
- preference for inventory reduction over spread capture.

---

## 10) Data model: recommended dataclasses (no code, implementable spec)

### 10.1 Snapshot meta

**`MarketSnapshotMeta`**
- Properties:
  - `monotonic_ts`: local monotonic timestamp
  - `wall_ts`: optional wall clock
  - `feed_seq`: monotonic sequence counter
  - `source`: enum (PM/BN)

Behaviors:
- compute `age_ms(now)`

### 10.2 Polymarket book snapshot

**`PMBookTop`**
- Properties (cents + shares):
  - `best_bid_px`, `best_bid_sz`
  - `best_ask_px`, `best_ask_sz`
  - `mid_px` (derived)
  - `micro_px` (optional derived)
  - `imbalance` (optional derived)

**`PMBookSnapshot`**
- Properties:
  - `meta: MarketSnapshotMeta`
  - `event_id` / `market_id` for YES and for NO (both identifiers)
  - `top: PMBookTop`
  - `levels`: optional top-N levels (for better imbalance)
  - `recent_trades`: optional ring buffer summary

### 10.3 Binance snapshot

**`BNSnapshot`**
- Properties:
  - `meta: MarketSnapshotMeta`
  - `symbol`
  - `best_bid_px`, `best_ask_px`, `mid_px`
  - `recent_trades` summary
  - optional derived:
    - `return_1s`
    - `ewma_vol_1s`
    - `shock_z`
    - `signed_volume_1s`

### 10.4 Inventory and exposure

**`InventoryState`**
- Properties:
  - `I_yes`, `I_no`
  - derived `net_E`, `gross_G`
  - `last_update_ts`

Behaviors:
- update from fill events

### 10.5 Strategy output (synthetic intent)

**`QuoteMode`**
- Values: STOP, NORMAL, CAUTION, ONE_SIDED_BUY, ONE_SIDED_SELL

**`DesiredQuoteLeg`**
- Properties:
  - `enabled`
  - `px_yes` (cents)
  - `sz` (shares)

**`DesiredQuoteSet`**
- Properties:
  - `created_at_ts`
  - `pm_seq`, `bn_seq` (for freshness checks)
  - `mode`
  - `bid_yes: DesiredQuoteLeg`
  - `ask_yes: DesiredQuoteLeg`
  - `reason_flags` (set of strings for logging)

### 10.6 Real orders and actions

**`Token`**
- Values: YES, NO

**`Side`**
- Values: BUY, SELL

**`RealOrderSpec`**
- Properties:
  - `token` (YES/NO)
  - `side` (BUY/SELL)
  - `px` (cents)
  - `sz` (shares)
  - `time_in_force` (if applicable)
  - `client_order_id` (assigned by Executor)

**`OrderStatus`**
- Values: WORKING, PENDING_NEW, PENDING_CANCEL, FILLED, CANCELED, REJECTED, UNKNOWN

**`WorkingOrder`**
- Properties:
  - `client_order_id`
  - `order_spec: RealOrderSpec`
  - `status`
  - `last_state_change_ts`
  - `filled_sz` (cumulative)

**`GatewayAction`**
- Types:
  - PLACE(order_spec)
  - CANCEL(client_order_id)
  - CANCEL_ALL(event_id)

**`GatewayResult`**
- Properties:
  - `action_id`
  - success/failure
  - error_kind
  - retryable flag

### 10.7 Executor risk state

**`RiskState`**
- Properties:
  - `cooldown_until_ts`
  - `mode_override` (optional forced STOP)
  - `stale_pm`, `stale_bn`
  - `gross_cap_hit`
  - `last_cancel_all_ts`
  - `error_burst_counter`

---

## 11) Class responsibilities (properties and methods)

### `PMCache`
- Holds latest `PMBookSnapshot` and rolling features.
- Responsibilities:
  - update from feed messages
  - provide immutable “latest snapshot” reference
  - provide snapshot age and feed_seq quickly

### `BNCache`
- Holds latest `BNSnapshot` and rolling features.
- Responsibilities:
  - update from feed messages
  - update EWMA vol/shock metrics
  - provide immutable “latest snapshot” reference

### `StrategyEngine`
- Inputs:
  - `PMBookSnapshot`, `BNSnapshot`
  - minimal inventory summary (I_yes, I_no, net, gross)
  - time-to-expiry for event
- Output:
  - `DesiredQuoteSet` (YES-space intent)
- Responsibilities:
  - compute features (vol/jumpiness, drift, distance-to-line, etc.)
  - decide mode (NORMAL/CAUTION/STOP/ONE_SIDED)
  - compute desired YES bid/ask and sizes
  - throttle intent emission (don’t spam identical intents)

### `ExecutorActor`
- Owns:
  - full `InventoryState`
  - `RiskState`
  - `WorkingOrders` (typically ≤ 2 per event)
  - pending action timers and retry counters
- Responsibilities:
  - accept intents and user events
  - enforce staleness/shock/cooldown overrides
  - materialize YES-space intent into legal `RealOrderSpec`s (sell-first, gross-aware)
  - reconcile desired vs actual safely (pending cancels/acks)
  - provide a cancel-all fast path

### `Gateway`
- Responsibilities:
  - place/cancel/cancel-all requests
  - translate responses into structured ack/error events
  - provide idempotency support via client_order_id/action_id

---

## 12) Minimal configuration knobs
- Strategy heartbeat frequency
- Executor timer frequency
- Staleness thresholds for PM and BN snapshots
- Cancel ack timeout and place ack timeout
- Cooldown durations by severity
- Gross cap per event (`G_max`) and near-expiry scaling
- Replace-rate limit (normal) and cancel-all always-on fast path

---

## 13) Observability essentials
- Intent → working latency
- Cancel ack latency distribution
- Inventory (I_yes/I_no) over time, net and gross
- Mode distribution (NORMAL/CAUTION/STOP)
- Cancel-all reasons and frequency
- Post-fill mid move proxy for adverse selection

---

## 14) Explicit exclusions
- No inventory “compression routine.” The framework prevents gross buildup via sell-first + gross caps rather than post-hoc compression.
- No requirement for a sophisticated probability model; regime gating is the main defense.

---

## 15) Summary (why this solves your subtlety)
- Complement mapping is a first-class concern handled by the Executor, not an afterthought.
- Sell-first materialization uses the complementary nature of YES/NO to reduce collateral usage.
- Single-writer Executor actor avoids race conditions between strategy/market data/fills.
- Reconciliation loop + timeouts makes order state robust to WS jitter and gateway latency.
