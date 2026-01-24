"""
Executor Actor - the main event processing component.

Single-writer component that owns all trading state.
Processes events from a unified event queue deterministically.

KEY DESIGN PRINCIPLES:

1. Single event stream - no dual input polling
   - All inputs (intents, fills, acks) go through one queue
   - Deterministic ordering of events

2. Planner/Reconciler separation
   - Planner: (intent, state) -> ExecutionPlan
   - Reconciler: (plan, slot) -> Effects
   - Actor: execute effects via gateway

3. Reservation-aware inventory
   - Available = settled - reserved - buffer
   - Reservations derived from working orders
   - Rebuilt after resync

4. SELL overlap rule
   - Never place replacement SELL until cancel acks
   - Prevents "insufficient balance" errors

5. RESYNCING mode with orphan fill handling
   - Tombstoned orders match fills during resync
   - Fills always update inventory (never dropped)
"""

import logging
import queue
import threading
import time
from typing import Optional, Callable, TYPE_CHECKING

from .state import (
    ExecutorState,
    ExecutorMode,
    OrderSlot,
    SlotState,
    PendingPlace,
    PendingCancel,
    ReservationLedger,
)
from .planner import (
    plan_execution,
    create_execution_report,
    OrderKind,
    ExecutionPlan,
)
from .reconciler import reconcile_slot
from .effects import ReconcileEffect, EffectBatch
from .policies import ExecutorPolicies, MinSizePolicy
from .pnl import PnLTracker
from .trade_log import TradeLogger
from .errors import ErrorCode, normalize_error
from .sync import (
    enter_resync_mode,
    perform_full_resync,
    is_tombstoned_order,
    get_tombstoned_order,
    cleanup_expired_tombstones,
)

if TYPE_CHECKING:
    from ..types import (
        Token,
        Side,
        DesiredQuoteSet,
        RealOrderSpec,
        WorkingOrder,
        InventoryState,
    )
    from ..gateway import Gateway

logger = logging.getLogger(__name__)


class ExecutorActor:
    """
    Main executor actor - single-writer for all trading state.

    Processes events from unified event queue:
    - StrategyIntentEvent: new quote intent from strategy
    - GatewayResultEvent: result from order placement/cancel
    - OrderAckEvent: order acknowledged by exchange
    - CancelAckEvent: cancel acknowledged
    - FillEvent: order fill
    - TimerTickEvent: periodic maintenance

    All state modifications happen here.
    """

    def __init__(
        self,
        gateway: "Gateway",
        event_queue: queue.Queue,
        yes_token_id: str,
        no_token_id: str,
        market_id: str,
        rest_client: Optional["PolymarketRestClient"] = None,
        policies: Optional[ExecutorPolicies] = None,
        on_cancel_all: Optional[Callable[[], None]] = None,
    ):
        """
        Initialize executor.

        Args:
            gateway: Gateway for order submission
            event_queue: Unified event queue (all inputs flow through here)
            yes_token_id: YES token ID for this market
            no_token_id: NO token ID for this market
            market_id: Market/condition ID for cancel-all
            rest_client: REST client for resync operations
            policies: Executor policies (min-size, tolerances, etc.)
            on_cancel_all: Optional callback when cancel-all is triggered
        """
        self._gateway = gateway
        self._event_queue = event_queue
        self._yes_token_id = yes_token_id
        self._no_token_id = no_token_id
        self._market_id = market_id
        self._rest_client = rest_client
        self._policies = policies or ExecutorPolicies()
        self._on_cancel_all = on_cancel_all

        # State
        self._state = ExecutorState()

        # Deduplication: track processed fills to avoid double-counting
        # Key: (order_id, size, price) - unique per fill event
        self._processed_fills: set[tuple[str, int, int]] = set()

        # Threading
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

        # Client order ID counter
        self._client_order_counter = 0
        self._counter_lock = threading.Lock()

        # Trade logger
        self._trade_logger = TradeLogger()

        # Session start time - used to filter out old fills from WS
        self._session_start_ts: int = 0

    def start(self) -> None:
        """Start executor thread."""
        from ..types import now_ms

        if self._thread and self._thread.is_alive():
            logger.warning("ExecutorActor already running")
            return

        # Record session start time
        self._session_start_ts = now_ms()
        logger.info(f"Session started at {self._session_start_ts}")

        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run_loop,
            name="Executor-Actor",
            daemon=True,
        )
        self._thread.start()
        logger.info("ExecutorActor started")

    def stop(self, timeout: float = 5.0) -> None:
        """Stop executor thread."""
        logger.info("ExecutorActor stopping...")
        self._stop_event.set()

        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=timeout)
            if self._thread.is_alive():
                logger.warning("ExecutorActor did not stop in time")

        # Close trade logger
        if self._trade_logger:
            self._trade_logger.close()

        logger.info("ExecutorActor stopped")

    def _run_loop(self) -> None:
        """
        Main event processing loop - SINGLE INPUT STREAM.

        All events come through the unified event queue.
        No second input source, no polling race conditions.
        """
        while not self._stop_event.is_set():
            try:
                # ONLY source of input - deterministic ordering
                event = self._event_queue.get(timeout=0.1)
                self._dispatch_event(event)
            except queue.Empty:
                # Periodic maintenance on timeout
                self._on_idle_tick()
            except Exception as e:
                logger.error(f"Executor loop error: {e}", exc_info=True)

    def _dispatch_event(self, event) -> None:
        """Route event to appropriate handler."""
        from ..types import (
            GatewayResultEvent,
            OrderAckEvent,
            CancelAckEvent,
            FillEvent,
            TimerTickEvent,
            StrategyIntentEvent,
            now_ms,
        )

        ts = now_ms()

        if isinstance(event, StrategyIntentEvent):
            self._on_intent(event.intent, ts)
        elif isinstance(event, GatewayResultEvent):
            self._on_gateway_result(event, ts)
        elif isinstance(event, OrderAckEvent):
            self._on_order_ack(event, ts)
        elif isinstance(event, CancelAckEvent):
            self._on_cancel_ack(event, ts)
        elif isinstance(event, FillEvent):
            self._on_fill(event, ts)
        elif isinstance(event, TimerTickEvent):
            self._on_timer_tick(ts)
        else:
            logger.warning(f"Unknown event type: {type(event)}")

    # -------------------------------------------------------------------------
    # INTENT HANDLING
    # -------------------------------------------------------------------------

    def _on_intent(self, intent: "DesiredQuoteSet", ts: int) -> None:
        """Handle new strategy intent."""
        from ..types import QuoteMode

        state = self._state

        # Block if in RESYNCING mode
        if state.mode == ExecutorMode.RESYNCING:
            logger.debug("In RESYNCING mode, ignoring intent")
            return

        # Check cooldown
        if state.risk.is_in_cooldown(ts):
            logger.debug("In cooldown, ignoring intent")
            return

        # Store intent
        state.last_intent = intent
        state.last_intent_ts = ts

        # Handle STOP mode
        if intent.mode == QuoteMode.STOP:
            self._cancel_all_orders("STOP_MODE")
            return

        # Plan execution
        plan = plan_execution(
            intent=intent,
            inventory=state.inventory,
            reservations=state.reservations,
            yes_token_id=self._yes_token_id,
            no_token_id=self._no_token_id,
            policy=self._policies.min_size_policy,
            min_size=self._policies.min_order_size,
        )

        # Log feasibility report if there's divergence
        report = create_execution_report(intent, plan)
        if report.bid_residual > 0 or report.ask_residual > 0:
            logger.info(
                f"[FEASIBILITY] Intent {report.intent_bid_sz}/{report.intent_ask_sz} "
                f"-> Plan {report.bid_planned_size}/{report.ask_planned_size} "
                f"(residual: {report.bid_residual}/{report.ask_residual})"
            )

        # Reconcile each slot
        self._reconcile_slot(state.bid_slot, plan.bid, ts)
        self._reconcile_slot(state.ask_slot, plan.ask, ts)

    def _reconcile_slot(
        self,
        slot: OrderSlot,
        leg_plan: "LegPlan",
        ts: int,
    ) -> None:
        """Reconcile a single slot with its planned orders."""
        from .planner import LegPlan

        # Get effects from reconciler (queue-preserving policy)
        effects = reconcile_slot(
            plan=leg_plan,
            slot=slot,
            price_tol=self._policies.price_tolerance,
            top_up_threshold=self._policies.top_up_threshold,
        )

        if effects.is_empty:
            return

        # Execute cancels first
        if effects.cancels:
            self._execute_cancels(slot, effects.cancels, ts)

        # Execute places (if not blocked by SELL overlap rule)
        if effects.places:
            self._execute_places(slot, effects.places, ts)

        # Track if we need to defer SELL places
        if effects.sell_blocked_until_cancel_ack:
            slot.sell_blocked_until_cancel_ack = True

    def _execute_cancels(
        self,
        slot: OrderSlot,
        cancels: list[ReconcileEffect],
        ts: int,
    ) -> None:
        """Execute cancel effects."""
        from ..types import Side

        for effect in cancels:
            if not effect.order_id:
                continue

            # Check if order exists
            order = slot.orders.get(effect.order_id)
            if not order:
                continue

            # Track if this is a SELL cancel (for SELL overlap rule)
            was_sell = order.order_spec.side == Side.SELL

            # Submit cancel
            action_id = self._gateway.submit_cancel(effect.order_id)

            # Track pending cancel
            slot.pending_cancels[action_id] = PendingCancel(
                action_id=action_id,
                server_order_id=effect.order_id,
                sent_at_ts=ts,
                was_sell=was_sell,
            )

            logger.debug(
                f"Submitted cancel: {action_id} for {effect.order_id[:20]}... "
                f"(was_sell={was_sell})"
            )

        # Transition slot state if we have pending cancels
        if slot.pending_cancels and slot.state == SlotState.IDLE:
            slot.transition_to_canceling()

    def _execute_places(
        self,
        slot: OrderSlot,
        places: list[ReconcileEffect],
        ts: int,
    ) -> None:
        """Execute place effects."""
        from ..types import Side

        for effect in places:
            if not effect.spec:
                continue

            # Generate client order ID
            effect.spec.client_order_id = self._next_client_order_id()

            # Update reservation if this is a SELL
            if effect.spec.side == Side.SELL:
                from ..types import Token
                if effect.spec.token == Token.YES:
                    self._state.reservations.reserve_yes(effect.spec.sz)
                else:
                    self._state.reservations.reserve_no(effect.spec.sz)

            # Submit to gateway
            action_id = self._gateway.submit_place(effect.spec)

            # Track pending place
            slot.pending_places[action_id] = PendingPlace(
                action_id=action_id,
                spec=effect.spec,
                sent_at_ts=ts,
                order_kind=effect.kind.value if effect.kind else "unknown",
            )

            logger.debug(
                f"Submitted place: {action_id} {effect.spec.token.name} "
                f"{effect.spec.side.name} @ {effect.spec.px}c x {effect.spec.sz}"
            )

        # Transition slot state if we have pending places
        if slot.pending_places and slot.state == SlotState.IDLE:
            slot.transition_to_placing()

    # -------------------------------------------------------------------------
    # GATEWAY RESULT HANDLING
    # -------------------------------------------------------------------------

    def _on_gateway_result(self, event: "GatewayResultEvent", ts: int) -> None:
        """Handle gateway result."""
        from ..types import OrderStatus

        state = self._state
        action_id = event.action_id

        # Check bid slot pending places
        if action_id in state.bid_slot.pending_places:
            self._handle_place_result(state.bid_slot, action_id, event, ts)
            return

        # Check ask slot pending places
        if action_id in state.ask_slot.pending_places:
            self._handle_place_result(state.ask_slot, action_id, event, ts)
            return

        # Check bid slot pending cancels
        if action_id in state.bid_slot.pending_cancels:
            self._handle_cancel_result(state.bid_slot, action_id, event, ts)
            return

        # Check ask slot pending cancels
        if action_id in state.ask_slot.pending_cancels:
            self._handle_cancel_result(state.ask_slot, action_id, event, ts)
            return

        logger.debug(f"Gateway result for unknown action: {action_id}")

    def _handle_place_result(
        self,
        slot: OrderSlot,
        action_id: str,
        event: "GatewayResultEvent",
        ts: int,
    ) -> None:
        """Handle place result for a slot."""
        from ..types import OrderStatus, WorkingOrder, Side, Token

        pending = slot.pending_places.pop(action_id)

        if event.success and event.server_order_id:
            # Create working order with kind from pending place
            order = WorkingOrder(
                client_order_id=pending.spec.client_order_id,
                server_order_id=event.server_order_id,
                order_spec=pending.spec,
                status=OrderStatus.PENDING_NEW,
                created_ts=ts,
                last_state_change_ts=ts,
                kind=pending.order_kind,  # Propagate kind from planner
            )
            slot.orders[event.server_order_id] = order
            logger.debug(f"Order placed: {event.server_order_id[:20]}... (kind={pending.order_kind})")
        else:
            # Place failed - release reservation if SELL
            if pending.spec.side == Side.SELL:
                if pending.spec.token == Token.YES:
                    self._state.reservations.release_yes(pending.spec.sz)
                else:
                    self._state.reservations.release_no(pending.spec.sz)

            # Check for balance error -> trigger resync
            error_code = normalize_error(event.error_kind)
            if error_code == ErrorCode.INSUFFICIENT_BALANCE:
                logger.warning("Insufficient balance error - triggering resync")
                self._trigger_resync("insufficient_balance")
            else:
                logger.warning(f"Place failed: {event.error_kind}")

        # Check if slot can transition back to IDLE
        slot.on_ack_received()

        # If we were blocking SELLs and all cancels are done, re-reconcile
        self._maybe_retry_blocked_sells(slot, ts)

    def _handle_cancel_result(
        self,
        slot: OrderSlot,
        action_id: str,
        event: "GatewayResultEvent",
        ts: int,
    ) -> None:
        """Handle cancel result for a slot."""
        from ..types import Side, Token

        pending = slot.pending_cancels.pop(action_id)

        if event.success:
            # Remove from working orders
            order = slot.orders.pop(pending.server_order_id, None)

            # Release reservation if this was a SELL
            if order and order.order_spec.side == Side.SELL:
                remaining = order.remaining_sz
                if order.order_spec.token == Token.YES:
                    self._state.reservations.release_yes(remaining)
                else:
                    self._state.reservations.release_no(remaining)

            logger.debug(f"Cancel confirmed: {pending.server_order_id[:20]}...")
        else:
            logger.warning(f"Cancel failed: {event.error_kind}")

        # Check if slot can transition back to IDLE
        slot.on_ack_received()

        # If we were blocking SELLs and all cancels are done, re-reconcile
        self._maybe_retry_blocked_sells(slot, ts)

    def _maybe_retry_blocked_sells(self, slot: OrderSlot, ts: int) -> None:
        """Re-reconcile if we had blocked SELLs waiting for cancel acks."""
        if not slot.sell_blocked_until_cancel_ack:
            return

        if not slot.can_submit():
            return  # Still busy

        # Clear the flag and re-reconcile with last intent
        slot.sell_blocked_until_cancel_ack = False

        if self._state.last_intent:
            logger.debug(f"Retrying blocked SELLs for {slot.slot_type}")
            self._on_intent(self._state.last_intent, ts)

    # -------------------------------------------------------------------------
    # ORDER/CANCEL ACK HANDLING
    # -------------------------------------------------------------------------

    def _on_order_ack(self, event: "OrderAckEvent", ts: int) -> None:
        """Handle order acknowledgment from exchange."""
        state = self._state

        # Find order in slots
        slot, order = state.find_order(event.server_order_id)
        if order:
            order.status = event.status
            order.last_state_change_ts = ts
            logger.debug(f"Order ack: {event.server_order_id[:20]}... status={event.status.name}")

    def _on_cancel_ack(self, event: "CancelAckEvent", ts: int) -> None:
        """Handle cancel acknowledgment from exchange."""
        from ..types import OrderStatus, Side, Token

        state = self._state

        # Find order in slots
        slot, order = state.find_order(event.server_order_id)

        if event.success and order:
            order.status = OrderStatus.CANCELED
            order.last_state_change_ts = ts

            # Release reservation if SELL
            if order.order_spec.side == Side.SELL:
                remaining = order.remaining_sz
                if order.order_spec.token == Token.YES:
                    state.reservations.release_yes(remaining)
                else:
                    state.reservations.release_no(remaining)

            # Remove from slot
            if slot:
                slot.orders.pop(event.server_order_id, None)

            logger.info(f"Cancel ack: {event.server_order_id[:20]}...")

            # Re-reconcile with last intent if appropriate
            if state.last_intent and slot and slot.can_submit():
                self._on_intent(state.last_intent, ts)

    # -------------------------------------------------------------------------
    # FILL HANDLING
    # -------------------------------------------------------------------------

    def _on_fill(self, event: "FillEvent", ts: int) -> None:
        """
        Handle fill event.

        CRITICAL: Always update inventory from fills - never drop!
        Even unknown fills trigger resync but still update inventory.
        """
        from ..types import Side, Token

        state = self._state
        order_id = event.server_order_id

        # Filter out old fills from before this session started
        if event.ts_exchange < self._session_start_ts:
            logger.debug(
                f"Ignoring old fill from before session: "
                f"fill_ts={event.ts_exchange} session_start={self._session_start_ts}"
            )
            return

        # Deduplication
        fill_key = (order_id, event.size, event.price)
        if fill_key in self._processed_fills:
            logger.warning(
                f"Skipping duplicate fill: order={order_id[:20]}... "
                f"size={event.size} price={event.price}c"
            )
            return
        self._processed_fills.add(fill_key)

        # Find order in slots or tombstones
        slot, order = state.find_order(order_id)
        is_orphan = False

        if slot is None:
            # Check tombstoned orders
            order = get_tombstoned_order(state, order_id)
            if order:
                logger.info(f"Processing orphan fill for tombstoned order: {order_id[:20]}...")
                is_orphan = True
            else:
                # Unknown order - trigger resync but still update inventory
                logger.warning(
                    f"Fill for unknown order: {order_id[:20]}... "
                    "- updating inventory and triggering resync"
                )
                # Still fall through to update inventory

        # ALWAYS update inventory (never drop fills)
        state.inventory.update_from_fill(
            token=event.token,
            side=event.side,
            size=event.size,
            ts=ts,
        )

        # Release reservation for SELL fills
        if event.side == Side.SELL:
            if event.token == Token.YES:
                state.reservations.release_yes(event.size)
            else:
                state.reservations.release_no(event.size)

        # Update working order if found (not orphan)
        if slot and order and not is_orphan:
            self._update_order_from_fill(slot, order, event, ts)

        # Record PnL
        realized_pnl, avg_cost = state.pnl.record_fill(
            ts=ts,
            token=event.token,
            side=event.side,
            size=event.size,
            price=event.price,
            order_id=order_id,
        )

        # Log fill
        side_str = "BUY" if event.side == Side.BUY else "SELL"
        fill_value_usd = (event.size * event.price) / 100
        logger.info(
            f"FILL: {side_str} {event.size}x{event.token.name}@{event.price}c "
            f"(${fill_value_usd:.2f}) | PnL: realized={realized_pnl}c "
            f"total={state.pnl.realized_pnl_cents}c (${state.pnl.realized_pnl_cents/100:.2f})"
        )

        # Write to trade log
        self._trade_logger.log_fill(
            ts=ts,
            token=event.token.name,
            side=side_str,
            size=event.size,
            price=event.price,
            order_id=order_id,
            realized_pnl=realized_pnl,
            inventory_yes=state.inventory.I_yes,
            inventory_no=state.inventory.I_no,
            net_position=state.inventory.net_E,
        )

        # Trigger resync for unknown fills (after updating inventory)
        if slot is None and not is_orphan:
            self._trigger_resync("unknown_fill")

    def _update_order_from_fill(
        self,
        slot: OrderSlot,
        order: "WorkingOrder",
        event: "FillEvent",
        ts: int,
    ) -> None:
        """Update working order from fill event."""
        from ..types import OrderStatus

        order.filled_sz += event.size
        order.last_state_change_ts = ts

        # Check if fully filled
        if order.filled_sz >= order.order_spec.sz:
            order.status = OrderStatus.FILLED
            slot.orders.pop(order.server_order_id, None)
            # Don't re-reconcile here - wait for next intent with fresh inventory
        else:
            # Partial fill - check if remaining is sub-min
            remaining = order.remaining_sz
            if 0 < remaining < self._policies.min_order_size:
                logger.info(
                    f"Partial fill left {remaining} remaining "
                    f"(< {self._policies.min_order_size}), cancelling"
                )
                action_id = self._gateway.submit_cancel(order.server_order_id)
                slot.pending_cancels[action_id] = PendingCancel(
                    action_id=action_id,
                    server_order_id=order.server_order_id,
                    sent_at_ts=ts,
                    was_sell=order.order_spec.side.name == "SELL",
                )

    # -------------------------------------------------------------------------
    # TIMER & MAINTENANCE
    # -------------------------------------------------------------------------

    def _on_timer_tick(self, ts: int) -> None:
        """Handle periodic timer tick for maintenance."""
        self._check_timeouts(ts)
        self._cleanup_tombstones(ts)

    def _on_idle_tick(self) -> None:
        """Called on queue timeout - lightweight maintenance."""
        from ..types import now_ms
        ts = now_ms()
        self._cleanup_tombstones(ts)

    def _check_timeouts(self, ts: int) -> None:
        """Check for timed-out pending operations."""
        for slot in [self._state.bid_slot, self._state.ask_slot]:
            # Check place timeouts
            timed_out = [
                action_id
                for action_id, pending in slot.pending_places.items()
                if ts - pending.sent_at_ts > self._policies.place_timeout_ms
            ]
            for action_id in timed_out:
                logger.warning(f"Place timed out: {action_id}")
                slot.pending_places.pop(action_id, None)

            # Check cancel timeouts
            timed_out = [
                action_id
                for action_id, pending in slot.pending_cancels.items()
                if ts - pending.sent_at_ts > self._policies.cancel_timeout_ms
            ]
            for action_id in timed_out:
                logger.warning(f"Cancel timed out: {action_id}")
                slot.pending_cancels.pop(action_id, None)

            # Update slot state if all pendings cleared
            slot.on_ack_received()

    def _cleanup_tombstones(self, ts: int) -> None:
        """Clean up expired tombstones."""
        cleared = cleanup_expired_tombstones(self._state, ts)
        if cleared > 0:
            logger.debug(f"Cleared {cleared} expired tombstones")

    # -------------------------------------------------------------------------
    # RESYNC & CANCEL-ALL
    # -------------------------------------------------------------------------

    def _trigger_resync(self, reason: str) -> None:
        """Trigger a full resync."""
        state = self._state

        # Already in resync?
        if state.mode == ExecutorMode.RESYNCING:
            return

        # Enter resync mode
        enter_resync_mode(
            state,
            reason,
            self._policies.tombstone_retention_ms,
        )

        # Cancel all orders
        self._gateway.submit_cancel_all(self._market_id)

        # Set cooldown
        from ..types import now_ms
        ts = now_ms()
        state.risk.set_cooldown(ts, self._policies.cooldown_after_cancel_all_ms)

        # Perform REST sync if we have a client
        if self._rest_client:
            perform_full_resync(
                state=state,
                rest_client=self._rest_client,
                market_id=self._market_id,
                yes_token_id=self._yes_token_id,
                no_token_id=self._no_token_id,
                safety_buffer=self._policies.safety_buffer,
            )
        else:
            # Without REST client, just clear state and hope for the best
            state.bid_slot.clear()
            state.ask_slot.clear()
            state.rebuild_reservations(self._policies.safety_buffer)
            state.mode = ExecutorMode.NORMAL

    def _cancel_all_orders(self, reason: str) -> None:
        """Cancel all orders (fast path)."""
        from ..types import now_ms

        ts = now_ms()
        state = self._state

        logger.warning(f"Cancel-all triggered: {reason}")

        # Submit cancel-all
        self._gateway.submit_cancel_all(self._market_id)

        # Update risk state
        state.risk.last_cancel_all_ts = ts
        state.risk.set_cooldown(ts, self._policies.cooldown_after_cancel_all_ms)

        # Clear our tracked orders
        state.bid_slot.clear()
        state.ask_slot.clear()
        state.rebuild_reservations(self._policies.safety_buffer)

        # Callback
        if self._on_cancel_all:
            try:
                self._on_cancel_all()
            except Exception as e:
                logger.error(f"Cancel-all callback failed: {e}")

    def _next_client_order_id(self) -> str:
        """Generate unique client order ID."""
        from ..types import now_ms

        with self._counter_lock:
            self._client_order_counter += 1
            return f"exec_{self._client_order_counter}_{now_ms()}"

    # -------------------------------------------------------------------------
    # PUBLIC ACCESSORS
    # -------------------------------------------------------------------------

    @property
    def state(self) -> ExecutorState:
        """Get current state (read-only access)."""
        return self._state

    @property
    def inventory(self) -> "InventoryState":
        """Get current inventory."""
        return self._state.inventory

    @property
    def is_running(self) -> bool:
        """Check if executor is running."""
        return self._thread is not None and self._thread.is_alive()

    def get_pnl_stats(self, mark_price: int = 50) -> dict:
        """Get current PnL statistics."""
        return self._state.pnl.get_summary(mark_price)

    def get_fill_history(self) -> list:
        """Get list of all recorded fills."""
        return list(self._state.pnl.fills)

    def set_inventory(self, yes: int, no: int) -> None:
        """Set inventory state (for initialization)."""
        from ..types import now_ms

        self._state.inventory.I_yes = yes
        self._state.inventory.I_no = no
        self._state.inventory.last_update_ts = now_ms()

    def sync_open_orders(
        self,
        orders: list[dict],
    ) -> int:
        """
        Sync existing open orders from exchange into executor state.

        Call this at startup BEFORE starting the executor thread.
        """
        from .sync import sync_open_orders as _sync_orders

        # Create a mock client for the sync function
        class MockClient:
            def __init__(self, orders):
                self._orders = orders

            def get_open_orders(self, market_id):
                return self._orders

        mock = MockClient(orders)
        result = _sync_orders(
            self._state,
            mock,
            self._market_id,
            self._yes_token_id,
            self._no_token_id,
        )
        return result.orders_synced


# Import for type hints
from ..pm_rest_client import PolymarketRestClient  # noqa: E402
