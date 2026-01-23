# Task 07: Executor Actor (Single-Writer State Manager)

**Priority:** Critical (Core)
**Estimated Complexity:** Very High
**Dependencies:** Task 01-06

---

## Objective

Implement the Executor Actor - the single-writer component that owns all trading state (orders, inventory) and materializes strategy intents into legal orders.

---

## Context from Design Documents

From `polymarket_mm_efficiency_design.md` Section 1:

> **The single-writer invariant**
>
> To eliminate most race conditions:
> - **Only the Executor/OrderManager is allowed to mutate trading state.**
> - Everything else produces inputs and sends them to the Executor.
>
> This turns the Executor into an **actor**:
> - It processes one event at a time
> - It updates state deterministically
> - It issues order actions in a controlled way

From `polymarket_mm_framework.md` Section 4:

> **Executor / Order Manager (single-writer actor)**
> - Owns truth of: open orders, pending cancels/places, inventory, cooldown state
> - Receives Strategy intents and user execution events
> - Materializes synthetic YES quotes into legal orders on YES/NO
> - Reconciles desired vs actual while preventing races and excessive churn

---

## Implementation Checklist

### 1. Executor State (Owned Data)

```python
@dataclass
class ExecutorState:
    """
    All state owned by the Executor. ONLY Executor mutates this.
    """
    # Inventory (updated on fills)
    inventory: InventoryState = field(default_factory=InventoryState)

    # Working orders
    working_orders: dict[str, WorkingOrder] = field(default_factory=dict)

    # Pending actions (waiting for ack)
    pending_places: dict[str, RealOrderSpec] = field(default_factory=dict)
    pending_cancels: set[str] = field(default_factory=set)

    # Risk state
    risk: RiskState = field(default_factory=RiskState)

    # Latest intent from strategy
    latest_intent: Optional[DesiredQuoteSet] = None

    # Market config
    market_id: str = ""
    yes_token_id: str = ""
    no_token_id: str = ""

    # Client order ID counter
    order_id_counter: int = 0
```

### 2. Executor Actor Class

```python
class ExecutorActor:
    """
    Single-writer actor for order and inventory state.

    Event Processing:
    - StrategyIntentEvent: Update desired state, reconcile
    - OrderAckEvent: Order placed successfully
    - CancelAckEvent: Order canceled successfully
    - FillEvent: Update inventory
    - GatewayErrorEvent: Handle failures
    - TimerTickEvent: Check timeouts, enforce staleness

    CRITICAL: This is the ONLY component that mutates trading state.
    """

    def __init__(
        self,
        gateway: Gateway,
        pm_cache: PMCache,
        intent_mailbox: IntentMailbox,
        config: ExecutorConfig,
    ):
        self._gateway = gateway
        self._pm_cache = pm_cache
        self._intent_mailbox = intent_mailbox
        self._config = config

        # Owned state
        self._state = ExecutorState()

        # Event queues
        self._high_priority_queue: queue.Queue = queue.Queue(maxsize=1000)
        self._timer_interval_ms = 50  # 20 Hz timer

        # Materializer
        self._materializer = OrderMaterializer(config)

    @property
    def high_priority_queue(self) -> queue.Queue:
        """Queue for user events (fills, acks). LOSSLESS."""
        return self._high_priority_queue

    def set_market(self, market_id: str, yes_token_id: str, no_token_id: str) -> None:
        """Configure market identifiers."""
        self._state.market_id = market_id
        self._state.yes_token_id = yes_token_id
        self._state.no_token_id = no_token_id

    async def run(self, stop_event: asyncio.Event) -> None:
        """Main actor loop."""
        last_timer_ms = 0

        while not stop_event.is_set():
            now_ms = time.monotonic_ns() // 1_000_000

            # Process high-priority events first (fills, acks)
            self._drain_high_priority()

            # Check for new intent (non-blocking)
            intent = self._intent_mailbox.get_nowait()
            if intent:
                self._handle_intent(intent, now_ms)

            # Timer tick
            if now_ms - last_timer_ms >= self._timer_interval_ms:
                self._handle_timer_tick(now_ms)
                last_timer_ms = now_ms

            # Small sleep to prevent tight loop
            await asyncio.sleep(0.001)

    def _drain_high_priority(self) -> None:
        """Process all pending high-priority events."""
        while True:
            try:
                event = self._high_priority_queue.get_nowait()
                self._process_event(event)
            except queue.Empty:
                break

    def _process_event(self, event: ExecutorEvent) -> None:
        """Process a single event. Deterministic state update."""
        if event.event_type == ExecutorEventType.FILL:
            self._handle_fill(event)
        elif event.event_type == ExecutorEventType.ORDER_ACK:
            self._handle_order_ack(event)
        elif event.event_type == ExecutorEventType.CANCEL_ACK:
            self._handle_cancel_ack(event)
        elif event.event_type == ExecutorEventType.GATEWAY_RESULT:
            self._handle_gateway_result(event)

    # =========== Event Handlers ===========

    def _handle_fill(self, event: FillEvent) -> None:
        """Handle fill event. Update inventory."""
        # Update inventory
        if event.token == Token.YES:
            if event.side == Side.BUY:
                self._state.inventory.I_yes += event.size
            else:
                self._state.inventory.I_yes -= event.size
        else:  # NO
            if event.side == Side.BUY:
                self._state.inventory.I_no += event.size
            else:
                self._state.inventory.I_no -= event.size

        self._state.inventory.last_update_ts = event.ts_local_ms

        # Update working order
        if event.order_id in self._state.working_orders:
            order = self._state.working_orders[event.order_id]
            order.filled_sz += event.size
            if order.filled_sz >= order.order_spec.sz:
                order.status = OrderStatus.FILLED
                del self._state.working_orders[event.order_id]

        logger.info(
            f"FILL: {event.side.name} {event.size} {event.token.name} @ {event.price}c | "
            f"Inv: YES={self._state.inventory.I_yes} NO={self._state.inventory.I_no}"
        )

    def _handle_order_ack(self, event: OrderAckEvent) -> None:
        """Handle order acknowledgment."""
        # Move from pending to working
        client_id = self._find_pending_by_server_id(event.order_id)
        if client_id and client_id in self._state.pending_places:
            spec = self._state.pending_places.pop(client_id)
            self._state.working_orders[event.order_id] = WorkingOrder(
                client_order_id=client_id,
                order_spec=spec,
                status=OrderStatus.WORKING,
                last_state_change_ts=event.ts_local_ms,
                filled_sz=0,
            )

    def _handle_cancel_ack(self, event: CancelAckEvent) -> None:
        """Handle cancel acknowledgment."""
        if event.order_id in self._state.working_orders:
            self._state.working_orders[event.order_id].status = OrderStatus.CANCELED
            del self._state.working_orders[event.order_id]

        self._state.pending_cancels.discard(event.order_id)

    def _handle_gateway_result(self, event: GatewayResultEvent) -> None:
        """Handle gateway action result."""
        if not event.success:
            # Handle failure
            if event.error_kind == "ORDER_NOT_FOUND":
                # Order was already filled - WS will tell us
                pass
            elif event.retryable:
                # Will retry on next reconcile
                pass
            else:
                logger.warning(f"Gateway action failed: {event.error_kind}")

    def _handle_intent(self, intent: DesiredQuoteSet, now_ms: int) -> None:
        """Handle new strategy intent."""
        self._state.latest_intent = intent

        # Check for STOP mode
        if intent.mode == QuoteMode.STOP:
            self._cancel_all_orders()
            return

        # Check for safety overrides
        if self._should_override_to_stop(now_ms):
            self._cancel_all_orders()
            return

        # Reconcile orders
        self._reconcile(now_ms)

    def _handle_timer_tick(self, now_ms: int) -> None:
        """Handle periodic timer tick."""
        # Check staleness
        if self._pm_cache.is_stale(now_ms, self._config.pm_stale_threshold_ms):
            if not self._state.risk.stale_pm:
                self._state.risk.stale_pm = True
                logger.warning("PM data stale - canceling all")
                self._cancel_all_orders()
            return
        else:
            self._state.risk.stale_pm = False

        # Check pending timeouts
        self._check_pending_timeouts(now_ms)

    # =========== Reconciliation ===========

    def _reconcile(self, now_ms: int) -> None:
        """
        Reconcile desired state vs working orders.

        For each synthetic leg (YES bid, YES ask):
        1. Materialize into real desired order
        2. Compare with working orders
        3. Cancel orders that don't match
        4. Place orders to match desired state
        """
        intent = self._state.latest_intent
        if intent is None:
            return

        # Materialize YES-space intent into real orders
        desired_orders = self._materializer.materialize(
            intent=intent,
            inventory=self._state.inventory,
            yes_token_id=self._state.yes_token_id,
            no_token_id=self._state.no_token_id,
        )

        # Get current working orders by synthetic leg
        working_bid = self._find_working_order_for_leg("YES_BID")
        working_ask = self._find_working_order_for_leg("YES_ASK")

        # Reconcile BID leg
        self._reconcile_leg(
            leg_name="YES_BID",
            desired=desired_orders.get("YES_BID"),
            working=working_bid,
            now_ms=now_ms,
        )

        # Reconcile ASK leg
        self._reconcile_leg(
            leg_name="YES_ASK",
            desired=desired_orders.get("YES_ASK"),
            working=working_ask,
            now_ms=now_ms,
        )

    def _reconcile_leg(
        self,
        leg_name: str,
        desired: Optional[RealOrderSpec],
        working: Optional[WorkingOrder],
        now_ms: int,
    ) -> None:
        """Reconcile a single leg."""
        # Case 1: No desired, have working -> cancel
        if desired is None and working is not None:
            if working.order_spec.client_order_id not in self._state.pending_cancels:
                self._submit_cancel(working.client_order_id)
            return

        # Case 2: Have desired, no working -> place
        if desired is not None and working is None:
            # Check not already pending
            if not self._is_pending_place_matching(desired):
                self._submit_place(desired, leg_name)
            return

        # Case 3: Have both -> compare
        if desired is not None and working is not None:
            if self._orders_match(desired, working.order_spec):
                # Match - do nothing
                return
            else:
                # Mismatch - cancel then place
                if working.client_order_id not in self._state.pending_cancels:
                    self._submit_cancel(working.client_order_id)
                # Place will happen on next tick after cancel ack
            return

    def _orders_match(self, desired: RealOrderSpec, working: RealOrderSpec) -> bool:
        """Check if orders match within tolerance."""
        if desired.token != working.token:
            return False
        if desired.side != working.side:
            return False
        if abs(desired.px - working.px) > self._config.price_tolerance_cents:
            return False
        if abs(desired.sz - working.sz) > self._config.size_tolerance_shares:
            return False
        return True

    # =========== Order Submission ===========

    def _submit_place(self, spec: RealOrderSpec, leg_name: str) -> None:
        """Submit order placement via gateway."""
        # Generate client order ID
        self._state.order_id_counter += 1
        spec.client_order_id = f"{leg_name}_{self._state.order_id_counter}_{int(time.time()*1000)}"

        # Track as pending
        self._state.pending_places[spec.client_order_id] = spec

        # Submit to gateway
        self._gateway.submit_place(spec)

    def _submit_cancel(self, order_id: str) -> None:
        """Submit order cancellation via gateway."""
        self._state.pending_cancels.add(order_id)
        self._gateway.submit_cancel(order_id)

    def _cancel_all_orders(self) -> None:
        """Cancel all orders. FAST PATH."""
        now_ms = time.monotonic_ns() // 1_000_000

        # Check cooldown
        if now_ms < self._state.risk.cooldown_until_ts:
            return

        # Submit cancel-all
        self._gateway.submit_cancel_all(self._state.market_id)

        # Mark all as pending cancel
        for order_id in list(self._state.working_orders.keys()):
            self._state.pending_cancels.add(order_id)

        # Set cooldown
        self._state.risk.last_cancel_all_ts = now_ms
        self._state.risk.cooldown_until_ts = now_ms + self._config.cooldown_after_cancel_all_ms

        logger.info("CANCEL ALL submitted")

    # =========== Safety Checks ===========

    def _should_override_to_stop(self, now_ms: int) -> bool:
        """Check if we should override to STOP."""
        # In cooldown
        if now_ms < self._state.risk.cooldown_until_ts:
            return True

        # Gross cap exceeded
        if self._state.inventory.gross_G >= self._config.gross_cap:
            self._state.risk.gross_cap_hit = True
            return True

        return False

    def _check_pending_timeouts(self, now_ms: int) -> None:
        """Check for timed-out pending actions."""
        # Check pending cancels
        for order_id in list(self._state.pending_cancels):
            if order_id in self._state.working_orders:
                order = self._state.working_orders[order_id]
                if now_ms - order.last_state_change_ts > self._config.cancel_timeout_ms:
                    logger.warning(f"Cancel timeout for {order_id} - triggering cancel-all")
                    self._cancel_all_orders()
                    return

    # =========== Helpers ===========

    def _find_working_order_for_leg(self, leg_name: str) -> Optional[WorkingOrder]:
        """Find working order for a synthetic leg."""
        for order_id, order in self._state.working_orders.items():
            if order.client_order_id.startswith(leg_name):
                return order
        return None

    def _find_pending_by_server_id(self, server_order_id: str) -> Optional[str]:
        """Find pending place by server order ID."""
        # This requires correlation logic based on your order response format
        # Placeholder: return first pending
        if self._state.pending_places:
            return next(iter(self._state.pending_places.keys()))
        return None

    def _is_pending_place_matching(self, spec: RealOrderSpec) -> bool:
        """Check if there's already a pending place matching this spec."""
        for pending_spec in self._state.pending_places.values():
            if self._orders_match(spec, pending_spec):
                return True
        return False
```

### 3. Order Materializer

```python
class OrderMaterializer:
    """
    Materializes YES-space intents into legal real orders.

    From framework doc:
    > **Synthetic BUY YES @ B**
    > 1. Preferred (if you hold NO): SELL NO @ (100 - B), up to I_no
    > 2. Fallback: BUY YES @ B
    >
    > **Synthetic SELL YES @ A**
    > 1. Preferred (if you hold YES): SELL YES @ A, up to I_yes
    > 2. Fallback: BUY NO @ (100 - A)
    """

    def __init__(self, config: ExecutorConfig):
        self._config = config

    def materialize(
        self,
        intent: DesiredQuoteSet,
        inventory: InventoryState,
        yes_token_id: str,
        no_token_id: str,
    ) -> dict[str, Optional[RealOrderSpec]]:
        """
        Materialize intent into real orders.

        Returns dict with keys "YES_BID" and "YES_ASK" mapping to RealOrderSpec or None.
        """
        result = {"YES_BID": None, "YES_ASK": None}

        # Materialize BID (synthetic BUY YES)
        if intent.bid_yes.enabled and intent.bid_yes.sz > 0:
            result["YES_BID"] = self._materialize_bid(
                px_yes=intent.bid_yes.px_yes,
                sz=intent.bid_yes.sz,
                inventory=inventory,
                yes_token_id=yes_token_id,
                no_token_id=no_token_id,
            )

        # Materialize ASK (synthetic SELL YES)
        if intent.ask_yes.enabled and intent.ask_yes.sz > 0:
            result["YES_ASK"] = self._materialize_ask(
                px_yes=intent.ask_yes.px_yes,
                sz=intent.ask_yes.sz,
                inventory=inventory,
                yes_token_id=yes_token_id,
                no_token_id=no_token_id,
            )

        return result

    def _materialize_bid(
        self,
        px_yes: int,
        sz: int,
        inventory: InventoryState,
        yes_token_id: str,
        no_token_id: str,
    ) -> RealOrderSpec:
        """
        Materialize synthetic BUY YES.

        Preferred: SELL NO @ (100 - px_yes) if we hold NO
        Fallback: BUY YES @ px_yes
        """
        if inventory.I_no > 0 and no_token_id:
            # Sell NO at complement price
            no_px = 100 - px_yes
            sell_sz = min(sz, inventory.I_no)
            return RealOrderSpec(
                token=Token.NO,
                side=Side.SELL,
                px=no_px,
                sz=sell_sz,
            )
        else:
            # Buy YES directly
            return RealOrderSpec(
                token=Token.YES,
                side=Side.BUY,
                px=px_yes,
                sz=sz,
            )

    def _materialize_ask(
        self,
        px_yes: int,
        sz: int,
        inventory: InventoryState,
        yes_token_id: str,
        no_token_id: str,
    ) -> RealOrderSpec:
        """
        Materialize synthetic SELL YES.

        Preferred: SELL YES @ px_yes if we hold YES
        Fallback: BUY NO @ (100 - px_yes)
        """
        if inventory.I_yes > 0:
            # Sell YES directly
            sell_sz = min(sz, inventory.I_yes)
            return RealOrderSpec(
                token=Token.YES,
                side=Side.SELL,
                px=px_yes,
                sz=sell_sz,
            )
        else:
            # Buy NO at complement price
            no_px = 100 - px_yes
            return RealOrderSpec(
                token=Token.NO,
                side=Side.BUY,
                px=no_px,
                sz=sz,
            )
```

### 4. Executor Config

```python
@dataclass
class ExecutorConfig:
    """Configuration for Executor."""
    # Staleness thresholds
    pm_stale_threshold_ms: int = 500
    bn_stale_threshold_ms: int = 1000

    # Timeouts
    cancel_timeout_ms: int = 5000
    place_timeout_ms: int = 3000

    # Cooldowns
    cooldown_after_cancel_all_ms: int = 3000

    # Caps
    gross_cap: int = 1000
    max_position: int = 500

    # Tolerances for order matching
    price_tolerance_cents: int = 0
    size_tolerance_shares: int = 5
```

---

## File Locations

- Create: `services/polymarket_trader/src/polymarket_trader/executor_actor.py`
- Create: `services/polymarket_trader/src/polymarket_trader/order_materializer.py`

---

## Acceptance Criteria

- [ ] Executor is single-writer of all trading state
- [ ] Processes events deterministically, one at a time
- [ ] Fills update inventory correctly (YES and NO)
- [ ] Order acks move orders from pending to working
- [ ] Cancel acks remove orders from working
- [ ] Intent triggers reconciliation
- [ ] Reconciliation: cancel mismatches, place to match desired
- [ ] STOP mode triggers cancel-all
- [ ] Staleness triggers cancel-all
- [ ] Cancel-all is fast path with cooldown
- [ ] Pending timeouts trigger cancel-all (escalation)
- [ ] Materializer implements sell-first inventory logic

---

## Critical Design Notes

From `polymarket_mm_efficiency_design.md` Section 11:

> **Never compromise these:**
> - Do not drop fills/acks
> - Cancel-all fast path must preempt normal throttles
> - On reconnect or state uncertainty: cancel-all + cooldown
> - Executor must be the only writer of order/inventory state

---

## Testing

Create `tests/test_executor_actor.py`:
- Test fill processing updates inventory correctly
- Test order ack transitions state
- Test reconciliation logic (cancel/place decisions)
- Test STOP mode cancels all
- Test staleness triggers cancel-all
- Test materializer sell-first logic
- Test timeout escalation
