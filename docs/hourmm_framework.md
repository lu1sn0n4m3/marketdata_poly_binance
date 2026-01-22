# HourMM Python Framework (Implementation Guide for Coding Agent)

This document specifies an **intuitive, minimal Python framework** that implements the two-container HourMM architecture described in the whitepaper.  
It is designed to be added to an existing repository and deployed on a VPS.

**Goals**
- Two containers, strictly separated:
  - **Container A**: Binance ingestion → features → hour context → pricer → *latest snapshot* publisher.
  - **Container B**: Polymarket connectivity → canonical state reducer → risk/limits → strategy intents → order manager → executor → operator controls.
- **Watertight by construction**:
  - Single-writer canonical state reducer.
  - Conservative modes (NORMAL / REDUCE_ONLY / FLATTEN / HALT).
  - Cancel-all on reconnect / uncertainty.
  - Quotes always have expiration (GTD/TTL) where supported.
- **Abstractions only where change is expected**:
  - `Pricer` (pluggable), `FeatureEngine` (pluggable), `Strategy` (pluggable).
  - Polymarket WS clients are concrete (not abstracted beyond a small adapter).

---

## 0) Repository layout (additive)

Add two folders to the repo (names are suggestions):

```
/services
  /binance_pricer/                  # Container A
    Dockerfile
    requirements.txt
    src/binance_pricer/
      __init__.py
      app.py
      config.py
      types.py
      binance_ws.py
      feature_engine.py
      hour_context.py
      pricer.py
      snapshot_store.py
      snapshot_server.py
      health.py
      logging.py
    tests/
  /polymarket_trader/               # Container B
    Dockerfile
    requirements.txt
    src/polymarket_trader/
      __init__.py
      app.py
      config.py
      types.py
      gamma_client.py
      polymarket_ws_market.py
      polymarket_ws_user.py
      polymarket_rest.py
      journal_sqlite.py
      reducer.py
      reconciler.py
      risk.py
      strategy.py
      quote_composer.py
      order_manager.py
      executor.py
      control_plane.py
      metrics.py
      logging.py
    tests/

/shared/hourmm_common/              # Shared schemas + enums (optional but recommended)
  __init__.py
  time.py
  schemas.py
  enums.py
  errors.py
  util.py

/docker-compose.yml                 # add services here (do not create a new compose file)
```

**Notes**
- If your repo already has a Python package layout, adapt paths accordingly.
- Keep **shared types** in one place (either `/shared/hourmm_common` or duplicated minimal `types.py` in each container, but shared is better).

---

## 1) Cross-container contract: Latest Snapshot (Container A → Container B)

Container B will **poll** Container A at 20–50Hz to retrieve the latest snapshot.  
This is intentionally **pull-based** to keep execution deterministic and prevent event-driven re-entrancy.

### 1.1 Snapshot schema (must be stable)

Define a `BinanceSnapshot` data structure (dataclass or Pydantic model). Fields:

- `seq: int` — monotonic sequence
- `ts_local_ms: int` — local timestamp at publish time
- `ts_exchange_ms: int` — exchange timestamp of most recent Binance event included
- `age_ms: int` — local time since last Binance event received
- `stale: bool` — `age_ms > stale_threshold_ms`
- `hour_id: str` — e.g. `"2026-01-22T14:00:00Z"`
- `hour_start_ts_ms: int`
- `hour_end_ts_ms: int`
- `t_remaining_ms: int`
- `open_price: float | None` — becomes immutable once set
- `last_trade_price: float | None`
- `bbo_bid: float | None`
- `bbo_ask: float | None`
- `mid: float | None`
- `features: dict[str, float]` — e.g. realized vol, ewma vol, imbalance, etc.
- `p_yes_fair: float | None` — in [0,1]
- `p_yes_band_lo: float | None` (optional)
- `p_yes_band_hi: float | None` (optional)

**Contract invariants**
- A snapshot read returns a **self-consistent** structure (no partially updated fields).
- `seq` increases by 1 every publish.
- `stale` must be computed in Container A and passed through unchanged.

### 1.2 Snapshot transport

Keep it simple:
- A small HTTP server (FastAPI or aiohttp) with endpoint: `GET /snapshot/latest`
- Response: JSON encoding of `BinanceSnapshot`

Alternative (if you want lower overhead on one VPS):
- Unix domain socket + minimal HTTP.
- Either is fine; the contract remains the same.

---

## 2) Container A (Binance Pricer) — minimal class model

### 2.1 Component graph

```
BinanceWsClient ──► HourContextBuilder ──► FeatureEngine ──► Pricer
      │                    │                   │             │
      └──────────────► HealthTracker ◄─────────┴─────────────┘
                                     │
                             LatestSnapshotStore ──► SnapshotServer
```

### 2.2 Classes (properties + method list)

#### `AConfig`
Configuration container (env → typed config).

**Properties**
- `symbol: str` (e.g. `"BTCUSDT"`)
- `stale_threshold_ms: int`
- `snapshot_publish_hz: int` (e.g. 50)
- `feature_windows: dict`
- `pricer_params: dict`
- `http_host: str`, `http_port: int`
- `log_level: str`

**Methods**
- `from_env() -> AConfig`
- `validate() -> None`

---

#### `BinanceWsClient` (concrete, not abstract)
Connects to Binance websocket(s) and emits normalized events.

**Properties**
- `symbol: str`
- `connected: bool`
- `last_event_ts_exchange_ms: int | None`
- `last_event_ts_local_ms: int | None`
- `health: WsHealth` (see `HealthTracker`)
- `out_queue: asyncio.Queue[BinanceEvent]` (or callback)

**Methods**
- `connect() -> None`
- `run() -> None` (main loop: reads WS frames, parses, emits)
- `close() -> None`
- `subscribe() -> None`
- `parse_message(raw: str) -> list[BinanceEvent]`
- `emit(event: BinanceEvent) -> None`

---

#### `HourContextBuilder`
Maintains hour boundaries and open price.

**Properties**
- `hour_id: str`
- `hour_start_ts_ms: int`
- `hour_end_ts_ms: int`
- `open_price: float | None`
- `last_trade_price: float | None`
- `last_trade_ts_exchange_ms: int | None`

**Methods**
- `update_from_trade(price: float, ts_exchange_ms: int, ts_local_ms: int) -> None`
- `update_clock(now_ts_ms: int) -> None` (roll hour when needed)
- `get_context(now_ts_ms: int) -> HourContext`
- `reset_for_new_hour(hour_start_ts_ms: int) -> None`

---

#### `FeatureEngine` (PLUGGABLE interface)
Computes rolling features. Only abstract where the computation might change.

**Properties**
- `feature_names: list[str]`
- `ready: bool`

**Methods**
- `update(event: BinanceEvent, ctx: HourContext) -> None`
- `snapshot(ctx: HourContext) -> dict[str, float]`
- `reset_for_new_hour(ctx: HourContext) -> None`

Provide a default implementation:
- `DefaultFeatureEngine(FeatureEngine)` — realized vol windows, ewma vol, returns, simple imbalance.

---

#### `Pricer` (PLUGGABLE interface)
Turns features + context into fair YES probability/price and uncertainty.

**Properties**
- `name: str`
- `ready: bool`

**Methods**
- `update(ctx: HourContext, features: dict[str, float]) -> None` (optional)
- `price(ctx: HourContext, features: dict[str, float]) -> PricerOutput`
- `reset_for_new_hour(ctx: HourContext) -> None`

Provide a default:
- `BaselinePricer(Pricer)` — simple mapping to probability (placeholder).

---

#### `HealthTracker`
Tracks liveness/staleness.

**Properties**
- `last_binance_event_local_ms: int | None`
- `stale_threshold_ms: int`

**Methods**
- `update_on_event(ts_local_ms: int) -> None`
- `age_ms(now_ms: int) -> int`
- `is_stale(now_ms: int) -> bool`

---

#### `LatestSnapshotStore`
Holds the current snapshot as an atomic unit.

**Properties**
- `current: BinanceSnapshot`
- `lock` (if using threads) or relies on single-threaded asyncio discipline
- `seq: int`

**Methods**
- `publish(snapshot: BinanceSnapshot) -> None`
- `read_latest() -> BinanceSnapshot`
- `increment_seq() -> int`

---

#### `SnapshotPublisher`
Builds and publishes snapshots at fixed rate.

**Properties**
- `publish_hz: int`
- `store: LatestSnapshotStore`
- `ctx_builder: HourContextBuilder`
- `feature_engine: FeatureEngine`
- `pricer: Pricer`
- `health: HealthTracker`

**Methods**
- `run() -> None` (tick loop)
- `build_snapshot(now_ms: int) -> BinanceSnapshot`

---

#### `SnapshotServer` (concrete)
Serves latest snapshot via HTTP/UDS.

**Properties**
- `host: str`, `port: int`
- `store: LatestSnapshotStore`

**Methods**
- `start() -> None`
- `stop() -> None`
- `handle_get_latest() -> BinanceSnapshot`

---

#### `ContainerAApp`
Wires everything.

**Properties**
- `config: AConfig`
- `binance: BinanceWsClient`
- `hour_ctx: HourContextBuilder`
- `features: FeatureEngine`
- `pricer: Pricer`
- `publisher: SnapshotPublisher`
- `server: SnapshotServer`

**Methods**
- `start() -> None`
- `stop() -> None`
- `run() -> None` (supervise tasks, restarts if desired)

---

## 3) Container B (Polymarket Trader) — minimal class model

### 3.1 Component graph (single-writer reducer)

All event producers feed **one reducer queue**. Reducer owns canonical state.

```
GammaClient ─► MarketScheduler ─► (market selection)
PolymarketMarketWsClient ─┐
PolymarketUserWsClient ───┼──► reducer_queue ─► StateReducer ─► CanonicalState
Executor acks/errors ─────┘                          │
ControlPlane commands ───────────────────────────────┘
                                                     │
DecisionLoop (50Hz) ─► RiskEngine ─► Strategy ─► OrderManager ─► Executor ─► events
```

### 3.2 Core invariants to embed in class boundaries

- Only `StateReducer` mutates `CanonicalState`.
- `DecisionLoop` reads a **snapshot** of canonical state (immutable copy) for decisions.
- `OrderManager` produces **actions**, but actual REST calls happen only in `Executor`.
- On WS reconnect/uncertainty: `Reconciler` triggers cancel-all and sets safe mode.

---

### 3.3 Classes (properties + method list)

#### `BConfig`
**Properties**
- `poll_snapshot_hz: int` (20–50)
- `snapshot_url: str` (e.g. `http://binance_pricer:8080/snapshot/latest`)
- `market_query_params: dict` (how to find the hourly market)
- `pm_ws_url: str`
- `pm_user_ws_url: str`
- `pm_rest_url: str`
- `api_key/secret` or auth material (as required)
- `sqlite_path: str`
- `session_timers: dict` (`T_wind_ms`, `T_flatten_ms`, `hard_cutoff_ms`)
- risk limits: `max_reserved_capital`, `max_order_size`, `max_open_orders`, etc.
- `log_level: str`

**Methods**
- `from_env() -> BConfig`
- `validate() -> None`

---

#### `GammaClient` (concrete)
Used to discover the current hourly market and token identifiers.

**Properties**
- `base_url: str`
- `http_client` (e.g. httpx.AsyncClient)

**Methods**
- `get_current_hour_market(now_ms: int) -> MarketInfo`
- `resolve_token_ids(market_info: MarketInfo) -> TokenIds`
- `healthcheck() -> bool`

---

#### `MarketScheduler`
Responsible for session selection and transitions.

**Properties**
- `gamma: GammaClient`
- `active_market: MarketInfo | None`
- `active_hour_id: str | None`

**Methods**
- `select_market_for_now(now_ms: int) -> MarketInfo`
- `should_roll_market(now_ms: int, state: CanonicalStateView) -> bool`
- `roll_to_next_market(now_ms: int) -> MarketInfo`

---

#### `PolymarketMarketWsClient` (concrete)
Public market websocket for book/BBO/tick size.

**Properties**
- `market_id: str`
- `connected: bool`
- `last_msg_local_ms: int | None`
- `out_queue: asyncio.Queue[Event]` (to reducer)

**Methods**
- `connect(market_id: str) -> None`
- `subscribe_market(market_id: str) -> None`
- `run() -> None`
- `close() -> None`
- `parse_message(raw: str) -> list[Event]`

---

#### `PolymarketUserWsClient` (concrete)
Authenticated user websocket for own orders/fills.

**Properties**
- `connected: bool`
- `last_msg_local_ms: int | None`
- `out_queue: asyncio.Queue[Event]`

**Methods**
- `connect() -> None`
- `subscribe_user() -> None`
- `run() -> None`
- `close() -> None`
- `parse_message(raw: str) -> list[Event]`

---

#### `PolymarketRestClient` (concrete)
REST executor interface for order placement/cancel-all.

**Properties**
- `base_url: str`
- `http_client`
- auth credentials / signer
- optional: `rate_limiter`

**Methods**
- `place_order(order: OrderRequest) -> OrderAck`
- `cancel_order(order_id: str) -> CancelAck`
- `cancel_all(market_id: str) -> CancelAllAck`
- `healthcheck() -> bool`

---

#### `EventJournalSQLite`
Durable journaling.

**Properties**
- `db_path: str`
- `conn` (sqlite)
- `enabled: bool`

**Methods**
- `init_schema() -> None`
- `append(event: EventRecord) -> None`
- `append_snapshot(state: CanonicalState) -> None`
- `load_latest_snapshot() -> CanonicalState | None`
- `load_events_since(snapshot_id: int) -> list[EventRecord]`

---

#### `CanonicalState` (reducer-owned)
Mutable only inside `StateReducer`. Expose read-only views elsewhere.

**Properties**
- `session_state: SessionState` (BOOT_SYNC/ACTIVE/WIND_DOWN/FLATTEN/DONE)
- `risk_mode: RiskMode` (NORMAL/REDUCE_ONLY/FLATTEN/HALT)
- `active_market: MarketInfo | None`
- `token_ids: TokenIds | None`
- `positions: PositionState` (YES, NO, cash/reserved)
- `open_orders: dict[str, OrderState]`
- `market_view: MarketView` (best bid/ask, tick_size, ws_age)
- `health: HealthState` (ws ages, rest health, snapshot staleness)
- `limits: LimitState`
- `timestamps: StateTimestamps` (last decision tick, last reconcile, etc.)

**Methods**
- `view() -> CanonicalStateView` (immutable/copy for decision loop)
- `clone_for_persist() -> CanonicalStateSnapshot` (serializable)

---

#### `StateReducer` (single writer)
Consumes all events and updates canonical state.

**Properties**
- `state: CanonicalState`
- `queue: asyncio.Queue[Event]`
- `journal: EventJournalSQLite`
- `scheduler: MarketScheduler` (for transitions)
- `reconciler: Reconciler` (invoked on certain triggers)

**Methods**
- `run() -> None` (main loop: get event, reduce, persist)
- `dispatch(event: Event) -> None`
- `reduce(event: Event) -> list[SideEffect]`
- `apply_side_effects(effects: list[SideEffect]) -> None`  
  (Side effects are restricted: e.g., signal executor or reconciler; reducer still owns the decision.)

---

#### `Reconciler`
Implements cancel-all and conservative rebuild triggers.

**Properties**
- `rest: PolymarketRestClient`
- `cooldown_ms: int`
- `in_progress: bool`

**Methods**
- `on_reconnect(market_id: str) -> None`
- `on_uncertainty(reason: str, market_id: str) -> None`
- `start_cancel_all(market_id: str) -> None`
- `mark_complete() -> None`

---

#### `SnapshotPoller`
Polls Container A for the latest snapshot at a fixed rate.

**Properties**
- `url: str`
- `poll_hz: int`
- `out_queue: asyncio.Queue[Event]`

**Methods**
- `run() -> None`
- `fetch_latest() -> BinanceSnapshot`
- `emit(snapshot: BinanceSnapshot) -> None`

---

#### `RiskEngine` (concrete)
Hard limits and mode gating. Not abstract.

**Properties**
- `limits: LimitState` (current)
- `session_timers: dict`
- `stale_threshold_ms: int` (for interpreting Binance snapshots)

**Methods**
- `evaluate(ctx: DecisionContext) -> RiskDecision`
- `is_action_allowed(action: ProposedAction, risk_decision: RiskDecision) -> bool`
- `clamp_intent(intent: StrategyIntent, risk_decision: RiskDecision) -> StrategyIntent`
- `compute_target_inventory(t_remaining_ms: int) -> float`
- `compute_reserved_capital(state_view: CanonicalStateView) -> float`

---

#### `Strategy` (PLUGGABLE interface)
Only abstract where strategies differ.

**Properties**
- `name: str`
- `enabled: bool`

**Methods**
- `on_tick(ctx: DecisionContext) -> StrategyIntent`
- `on_session_start(ctx: DecisionContext) -> None`
- `on_session_end(ctx: DecisionContext) -> None`

Provide defaults:
- `OpportunisticQuoteStrategy(Strategy)`
- `DirectionalTakerStrategy(Strategy)` (optional)

---

#### `QuoteComposer` (concrete helper)
Turns fair price + spread/skew into candidate quotes.

**Properties**
- `min_spread_ticks: int`
- `replace_min_ticks: int`
- `max_quotes_per_side: int` (typically 1)

**Methods**
- `compose(fair: float, ctx: DecisionContext) -> QuoteSet`
- `round_to_tick(price: float, tick_size: float) -> float`
- `with_inventory_skew(quotes: QuoteSet, inventory: float, t_remaining_ms: int) -> QuoteSet`

---

#### `OrderManager` (concrete)
Desired-state reconciliation. Minimal churn.

**Properties**
- `max_working_orders: int`
- `replace_min_ticks: int`
- `replace_min_age_ms: int`

**Methods**
- `reconcile(desired: DesiredOrders, working: WorkingOrders, ctx: DecisionContext) -> list[OrderAction]`
- `should_replace(existing: OrderState, desired: DesiredOrder, ctx: DecisionContext) -> bool`
- `build_expiration(ctx: DecisionContext) -> int` (expires_at_ms)
- `enforce_tick_and_bounds(desired: DesiredOrders, tick_size: float) -> DesiredOrders`

---

#### `Executor` (concrete)
Performs REST calls and emits acks/errors as events back to reducer.

**Properties**
- `rest: PolymarketRestClient`
- `out_queue: asyncio.Queue[Event]`
- `inflight: dict[str, InflightCall]`

**Methods**
- `submit(actions: list[OrderAction]) -> None`
- `place(order_req: OrderRequest) -> None`
- `cancel(order_id: str) -> None`
- `cancel_all(market_id: str) -> None`
- `emit_ack(ack: Any) -> None`
- `emit_error(err: Exception, context: dict) -> None`

---

#### `DecisionLoop`
Fixed-frequency tick loop.

**Properties**
- `tick_hz: int`
- `state_provider: Callable[[], CanonicalStateView]` (reads reducer-owned state safely)
- `risk: RiskEngine`
- `strategy: Strategy`
- `order_manager: OrderManager`
- `executor: Executor`

**Methods**
- `run() -> None`
- `build_context(state_view: CanonicalStateView, snapshot: BinanceSnapshot) -> DecisionContext`
- `step(ctx: DecisionContext) -> None` (compute intent → clamp → reconcile → submit)
- `emit_decision_metrics(ctx: DecisionContext) -> None`

---

#### `ControlPlaneServer`
Local-only HTTP server for operator commands.

**Properties**
- `bind_host: str` (e.g. `127.0.0.1`)
- `port: int`
- `out_queue: asyncio.Queue[Event]`

**Methods**
- `start() -> None`
- `stop() -> None`
- handlers:
  - `POST /pause_quoting`
  - `POST /set_mode`
  - `POST /flatten_now`
  - `POST /set_limits`

Each handler emits a `ControlCommandEvent` into reducer queue (reducer applies).

---

#### `ContainerBApp`
Wires everything and supervises tasks.

**Properties**
- `config: BConfig`
- `scheduler: MarketScheduler`
- `market_ws: PolymarketMarketWsClient`
- `user_ws: PolymarketUserWsClient`
- `rest: PolymarketRestClient`
- `journal: EventJournalSQLite`
- `reducer: StateReducer`
- `poller: SnapshotPoller`
- `decision_loop: DecisionLoop`
- `control_plane: ControlPlaneServer`
- `executor: Executor`

**Methods**
- `start() -> None`
- `stop() -> None`
- `run() -> None`

---

## 4) Event model (simple and consistent)

Use a small set of event types. Keep them plain dataclasses/Pydantic models.

### 4.1 Event categories

**From Binance snapshot poller**
- `BinanceSnapshotEvent(snapshot: BinanceSnapshot)`

**From Polymarket market WS**
- `MarketBboEvent(best_bid, best_ask, tick_size, ts_local_ms)`
- `TickSizeChangedEvent(new_tick_size, ts_local_ms)`
- `MarketStatusEvent(status, ts_local_ms)`

**From Polymarket user WS**
- `UserOrderEvent(order_id, status, side, price, size, filled, remaining, ts_local_ms)`
- `UserTradeEvent(trade_id, order_id, side, price, size, ts_local_ms)`

**From executor**
- `RestOrderAckEvent(client_req_id, order_id, ts_local_ms)`
- `RestErrorEvent(client_req_id, reason, ts_local_ms)`

**From control plane**
- `ControlCommandEvent(type, payload, ts_local_ms)`

**From internal timers**
- `ReducerTickEvent(now_ms)` (optional, if reducer needs periodic checks)
- `SessionRolloverEvent(hour_id)` (optional, emitted by scheduler)

### 4.2 Reducer rules (must be written down as acceptance criteria)

Reducer applies:
- Market events update `state.market_view`.
- User events update `state.open_orders` and `state.positions`.
- Snapshot events update `state.health.binance_stale`, `state.health.snapshot_age_ms`, and store latest fair value (for observability; decision loop also reads snapshot directly).
- Executor events resolve inflight unknowns and can trigger `Reconciler` on repeated errors.
- Control commands update `state.limits`, `state.risk_mode`, `state.session_state` as needed.

---

## 5) Docker Compose integration (main repo)

Add two services to the repo root `docker-compose.yml` (do not create a new compose file):

- `binance_pricer`
  - exposes port (e.g. 8080) on internal network only
  - restart policy: unless-stopped
  - healthcheck: `GET /health` (optional)

- `polymarket_trader`
  - depends on `binance_pricer`
  - restart policy: unless-stopped
  - binds `control_plane` to localhost on VPS (e.g. `127.0.0.1:9000:9000`)
  - persistent volume for SQLite journal (important)

Use a dedicated docker network for internal service DNS.

---

## 6) Test plan (descriptive, no-code)

### 6.1 Test categories

1) **Unit/contract tests (offline)**  
2) **Component tests (mocked WS/REST)**  
3) **Integration tests (optional live endpoints, gated)**  
4) **Smoke tests (small code allowed; see §7)**

---

## 6.2 Container A tests

### A-1: Binance WS connection + parsing
**Purpose**: verify Binance WS client connects, subscribes, and parses messages into normalized events.

**Setup**
- Use a mocked websocket server that replays recorded Binance messages (trades + bookTicker/BBO).
- Or use recorded fixtures only and call `parse_message`.

**Assertions**
- `connect()` transitions `connected=True`.
- Subscriptions are sent once per connection.
- `parse_message` yields correct event types with required fields.
- Timestamps are populated (exchange + local).

---

### A-2: Hour context correctness (open price and rollover)
**Purpose**: ensure hour boundaries and open price rules.

**Setup**
- Feed trade events spanning an hour boundary.
- Simulate local clock progression.

**Assertions**
- `open_price` is set by first qualifying trade in the hour and never changes within that hour.
- `hour_id` rolls at correct boundary.
- `t_remaining_ms` decreases and resets on new hour.
- `reset_for_new_hour` clears open price and reinitializes correctly.

---

### A-3: Snapshot atomicity and monotonic seq
**Purpose**: ensure latest snapshot is always consistent and seq monotonic.

**Setup**
- Run publisher loop with mocked dependencies (hour ctx, features, pricer).
- Read snapshot concurrently (if using threads) or rapidly in asyncio.

**Assertions**
- `seq` increases by 1 each publish.
- Reads never see partially updated fields (e.g. `hour_id` and `hour_start_ts_ms` must correspond).
- `stale` flips true when no events arrive for longer than threshold.

---

### A-4: FeatureEngine and Pricer pluggability
**Purpose**: verify interface stability.

**Setup**
- Plug in dummy FeatureEngine/Pricer that returns deterministic outputs.

**Assertions**
- Container A app wires them without changes.
- Publisher includes their outputs and does not require internal knowledge of model type.

---

## 6.3 Container B tests

### B-1: Gamma market discovery selects correct hourly market
**Purpose**: ensure scheduler chooses the market that corresponds to current hour.

**Setup**
- Mock GammaClient responses for multiple markets/hours.
- Provide a fixed `now_ms`.

**Assertions**
- `select_market_for_now` returns correct `MarketInfo`.
- `resolve_token_ids` yields correct YES/NO token IDs.
- `should_roll_market` triggers only when hour boundary passes (and/or market status indicates roll).

---

### B-2: Polymarket market WS parsing (tick size + BBO)
**Purpose**: ensure market websocket messages update market view correctly.

**Setup**
- Replay recorded WS messages with best bid/ask and tick size change events.

**Assertions**
- Reducer receives `MarketBboEvent` and updates `state.market_view.best_bid/ask`.
- Tick size changes update `state.market_view.tick_size` and trigger order manager re-rounding path (via an internal flag or downstream behavior).

---

### B-3: Polymarket user WS parsing (orders + trades)
**Purpose**: confirm that positions/open orders derive from user events.

**Setup**
- Replay a sequence: order placed → partial fill → further fill → cancel → final state.

**Assertions**
- Open order appears in `state.open_orders` with correct remaining quantity.
- Position increases/decreases appropriately on fills.
- Partial fills update filled/remaining consistently.
- Cancel removes or marks order terminal.
- Journal records each event.

---

### B-4: Reducer single-writer enforcement
**Purpose**: ensure no other component mutates state.

**Setup**
- Expose only `CanonicalStateView` outside reducer.
- Attempt to mutate in decision loop should be structurally impossible (type-level).

**Assertions**
- Decision loop cannot access mutable state object.
- Any state changes only happen via reducer processing events.

---

### B-5: Cancel-all on reconnect / uncertainty
**Purpose**: validate emergency path.

**Setup**
- Simulate WS disconnect event and reconnect event.
- Use a fake REST client capturing calls.

**Assertions**
- System transitions to `HALT`.
- `Reconciler.start_cancel_all(market_id)` is invoked.
- No new orders are submitted while reconcile in progress.
- After cancel-all completion and stabilization window, system can return to NORMAL (if enabled).

---

### B-6: Risk mode gating with stale Binance snapshot
**Purpose**: enforce “do less under uncertainty”.

**Setup**
- Provide DecisionContext with `snapshot.stale=True`.

**Assertions**
- RiskEngine returns `REDUCE_ONLY`.
- Strategy intent is clamped: no new exposure, only unwind-intents allowed.
- OrderManager generates only cancel/reduce actions.

---

### B-7: OrderManager desired→working reconciliation
**Purpose**: ensure minimal churn and replace policy.

**Setup**
- Provide a working order and a desired order slightly different.

**Assertions**
- If price delta < `replace_min_ticks`, no replace.
- If price delta >= threshold, generates replace action.
- Expirations are built and shorten as `t_remaining_ms` decreases.

---

### B-8: Inventory management and end-of-hour flattening
**Purpose**: validate session phase behavior.

**Setup**
- Simulate time approaching end of hour.
- Provide non-zero positions.

**Assertions**
- Session transitions: ACTIVE → WIND_DOWN → FLATTEN at configured times.
- In WIND_DOWN, target inventory decreases; quote sizes shrink and skew unwind.
- In FLATTEN, only unwind orders permitted; exposure-increasing quotes are canceled.

---

## 6.4 End-to-end “happy path” integration test (mocked)
**Scenario**: One full hour lifecycle with normal connectivity.

**Steps**
1. Scheduler selects current hour market, token IDs resolved.
2. Market WS provides tick size and BBO.
3. Binance snapshot provides fair price and is fresh.
4. Decision loop posts opportunistic quotes.
5. User WS reports partial fill.
6. Risk engine skews quotes to manage inventory.
7. WIND_DOWN begins; inventory target decreases.
8. FLATTEN begins; system exits to flat.
9. Hour ends; DONE; scheduler rolls to next market (or returns to BOOT_SYNC).

**Assertions**
- No state mutation outside reducer.
- No orders remain live beyond their expiration.
- Journal contains full trace of events/actions.
- System ends hour with inventory close to zero (as per config), and no working orders.

---

## 6.5 Optional live tests (gated, non-default)
These should run only when explicitly enabled via environment flag (e.g. `RUN_LIVE_TESTS=1`).

- Binance: connect to public WS and confirm messages arrive within N seconds.
- Polymarket: connect to WS endpoints and confirm subscription ack + updates.
- Polymarket REST: place a tiny order on a sandbox/test environment if available (or skip if not).

**Do not run live tests by default** in CI.

---

## 7) Small code smoke tests (allowed; minimal)

These tests are not about trading correctness—only that objects wire up and basic calls work.

### 7.1 Smoke test: Container A wiring
- Instantiate `AConfig` with test values.
- Create `BinanceWsClient`, `HourContextBuilder`, `DefaultFeatureEngine`, `BaselinePricer`, `LatestSnapshotStore`.
- Call `SnapshotPublisher.build_snapshot(now_ms)` with a mocked context.
- Assert:
  - snapshot is serializable
  - `seq` increments on publish
  - required fields exist

### 7.2 Smoke test: Container B wiring
- Instantiate `BConfig` with test values.
- Create `CanonicalState`, `StateReducer` (with in-memory journal stub), `RiskEngine`, `OpportunisticQuoteStrategy`, `OrderManager`, `Executor` (with fake REST).
- Build a `DecisionContext` with a dummy Binance snapshot and dummy market view.
- Call `DecisionLoop.step(ctx)` once.
- Assert:
  - no exceptions
  - executor receives either zero actions (no edge) or a bounded set of actions
  - reducer queue receives executor ack/error events when simulated

### 7.3 Smoke test: cancel-all path
- Simulate WS disconnect event into reducer.
- Ensure `Reconciler.start_cancel_all` called once, and decision loop submits no new risk actions while reconcile is active.

(These can be implemented using `pytest` with lightweight fakes/mocks.)

---

## 8) Implementation notes (to keep it simple)

- Use **asyncio** end-to-end in both containers.
- Use **dataclasses** or **Pydantic** for schemas; keep them stable.
- Use **SQLite WAL** for the journal in Container B. Keep schema minimal: `events` table + `snapshots` table.
- Keep order counts small (1 quote per side by default) to reduce churn.
- Always log:
  - mode changes
  - session transitions
  - cancel-all triggers
  - executor errors/timeouts

---

## 9) Deliverables checklist for the coding agent

1. Two service folders under `/services/` with isolated dependencies and Dockerfiles.
2. Shared schema package under `/shared/` (or duplicated minimal types).
3. Docker compose entries added to repo root `docker-compose.yml`:
   - internal network
   - volume for SQLite journal
   - control-plane port bound to localhost
4. Container A:
   - WS ingestion
   - hour context
   - feature engine
   - pricer
   - snapshot store + HTTP endpoint
5. Container B:
   - Gamma discovery + scheduler
   - WS market + WS user clients
   - reducer + SQLite journal
   - snapshot poller
   - risk engine + session state machine
   - strategy plugin
   - order manager + executor
   - control plane
6. Tests:
   - offline unit/contract tests for parsing, hour context, reducer, risk gating
   - mocked end-to-end hour lifecycle test
   - optional gated live tests
   - minimal smoke tests for wiring

---

If you want, I can also provide a **one-page “acceptance criteria” checklist** that you can use to review the implementation PR (focused on invariants like single-writer, cancel-all-on-reconnect, and TTL orders).
