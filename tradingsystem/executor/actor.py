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
        # Key: fill_id (TAKER:trade_id or MAKER:trade_id:order_id)
        # This ensures proper dedup when same trade arrives as MATCHED->MINED->CONFIRMED
        self._processed_fills: set[str] = set()

        # Track consecutive insufficient balance errors to detect state divergence
        # Resync after N consecutive failures (indicates reservation/inventory mismatch)
        self._consecutive_balance_errors: int = 0
        self._max_balance_errors_before_resync: int = 3

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
        from ..types import wall_ms

        if self._thread and self._thread.is_alive():
            logger.warning("ExecutorActor already running")
            return

        # Record session start time (wall clock for comparison with exchange timestamps)
        self._session_start_ts = wall_ms()
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
            BarrierEvent,
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
        elif isinstance(event, BarrierEvent):
            self._on_test_barrier(event)
        else:
            logger.warning(f"Unknown event type: {type(event)}")

    # -------------------------------------------------------------------------
    # INTENT HANDLING
    # -------------------------------------------------------------------------

    def _on_intent(self, intent: "DesiredQuoteSet", ts: int) -> None:
        """Handle new strategy intent."""
        from ..types import QuoteMode

        state = self._state

        # Block if not in NORMAL mode
        if state.mode == ExecutorMode.RESYNCING:
            logger.debug("In RESYNCING mode, ignoring intent")
            return
        if state.mode == ExecutorMode.CLOSING:
            logger.debug("In CLOSING mode, ignoring intent")
            return
        if state.mode == ExecutorMode.STOPPED:
            logger.debug("In STOPPED mode, ignoring intent")
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

        # Debug: log planned orders and their prices (DEBUG to avoid hot path latency)
        for order in plan.bid.orders:
            logger.debug(
                f"[PLAN] bid: {order.token.name} {order.side.name} {order.sz}@{order.px}c kind={order.kind.name}"
            )
        for order in plan.ask.orders:
            logger.debug(
                f"[PLAN] ask: {order.token.name} {order.side.name} {order.sz}@{order.px}c kind={order.kind.name}"
            )

        # Log latency from book update to reconcile (DEBUG to avoid hot path latency)
        if intent.book_ts > 0:
            from ..types import now_ms
            latency_ms = now_ms() - intent.book_ts
            logger.debug(f"[LATENCY] book_to_plan: {latency_ms}ms (seq={intent.pm_seq})")

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

        # Rate limit check: prevent fast cancel/replace cycling
        if slot.is_rate_limited(ts, self._policies.min_reconcile_interval_ms):
            return

        # Get effects from reconciler (queue-preserving policy)
        effects = reconcile_slot(
            plan=leg_plan,
            slot=slot,
            price_tol=self._policies.price_tolerance,
            top_up_threshold=self._policies.top_up_threshold,
        )

        if effects.is_empty:
            return

        # Record this reconciliation to enforce rate limiting
        slot.record_submission(ts)

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

        # Atomic pop - may return None if timeout handler already removed it
        pending = slot.pending_places.pop(action_id, None)
        if pending is None:
            logger.debug(f"Place result for already-cleared action: {action_id}")
            return

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
            # Reset balance error counter on success
            self._consecutive_balance_errors = 0
        else:
            # Place failed - release reservation if SELL
            if pending.spec.side == Side.SELL:
                if pending.spec.token == Token.YES:
                    self._state.reservations.release_yes(pending.spec.sz)
                else:
                    self._state.reservations.release_no(pending.spec.sz)

            # Handle errors - track consecutive balance errors for resync trigger
            error_code = normalize_error(event.error_kind)
            if error_code == ErrorCode.INSUFFICIENT_BALANCE:
                self._consecutive_balance_errors += 1
                logger.warning(
                    f"Insufficient balance for {pending.spec.token.name} "
                    f"{pending.spec.side.name} {pending.spec.sz}@{pending.spec.px}c "
                    f"(consecutive: {self._consecutive_balance_errors}/{self._max_balance_errors_before_resync})"
                )
                # Trigger resync after N consecutive failures - indicates state divergence
                if self._consecutive_balance_errors >= self._max_balance_errors_before_resync:
                    logger.error(
                        f"Consecutive balance errors exceeded threshold "
                        f"({self._consecutive_balance_errors}), triggering resync"
                    )
                    self._consecutive_balance_errors = 0
                    self._trigger_resync("consecutive_balance_errors")
            else:
                logger.warning(f"Place failed: {event.error_kind}")
                # Reset counter on non-balance errors (different failure mode)
                self._consecutive_balance_errors = 0

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

        # Atomic pop - may return None if timeout handler already removed it
        pending = slot.pending_cancels.pop(action_id, None)
        if pending is None:
            logger.debug(f"Cancel result for already-cleared action: {action_id}")
            return

        if event.success:
            # Remove from working orders
            order = slot.orders.pop(pending.server_order_id, None)

            # CRITICAL: Tombstone the order so late-arriving fills can be processed!
            # Fills can arrive after cancel due to network delays.
            if order:
                self._tombstone_order(order, ts)

            # Release reservation if this was a SELL
            if order and order.order_spec.side == Side.SELL:
                remaining = order.remaining_sz
                if order.order_spec.token == Token.YES:
                    self._state.reservations.release_yes(remaining)
                else:
                    self._state.reservations.release_no(remaining)

            logger.debug(f"Cancel confirmed: {pending.server_order_id[:20]}...")
        else:
            # Check if this is an "order gone" error - order matched/filled or already canceled
            # In these cases the order is NOT on the venue anymore, so remove it from working state
            error_lower = (event.error_kind or "").lower()
            order_is_gone = (
                "matched" in error_lower or
                "not found" in error_lower or
                "already" in error_lower or
                "canceled" in error_lower
            )

            if order_is_gone:
                order = slot.orders.pop(pending.server_order_id, None)
                if order:
                    # Tombstone for late-arriving fills (order may have been matched)
                    self._tombstone_order(order, ts)

                    # Release reservation if SELL (order is gone from venue)
                    if order.order_spec.side == Side.SELL:
                        remaining = order.remaining_sz
                        if order.order_spec.token == Token.YES:
                            self._state.reservations.release_yes(remaining)
                        else:
                            self._state.reservations.release_no(remaining)

                    logger.info(
                        f"Cancel failed ({event.error_kind}) - order removed from state: "
                        f"{pending.server_order_id[:20]}..."
                    )
                else:
                    logger.warning(f"Cancel failed: {event.error_kind}")
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

            # Check if we're in CLOSING mode and should place closing orders
            if state.mode == ExecutorMode.CLOSING:
                self._maybe_place_closing_orders(ts)
                return

            # Re-reconcile with last intent if appropriate
            if state.last_intent and slot and slot.can_submit():
                self._on_intent(state.last_intent, ts)

    # -------------------------------------------------------------------------
    # FILL HANDLING
    # -------------------------------------------------------------------------

    def _on_fill(self, event: "FillEvent", ts: int) -> None:
        """
        Handle fill event.

        Fills have two phases:
        1. MATCHED (is_pending=True): Update pending inventory immediately.
           Strategy sees this to avoid duplicate orders. Can't sell yet.
        2. MINED (is_pending=False): Settle the pending inventory.
           Move from pending to settled. Now can be sold.

        CRITICAL: Only update inventory for fills we KNOW about.
        The WebSocket sends ALL makers in a trade, not just our orders.
        Unknown fills are for other people's orders - ignore them.
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

        # Deduplication using fill_id (includes settlement status for proper dedup)
        # MATCHED and MINED have different fill_ids so both get processed
        # Duplicate MINED messages deduplicate
        fill_id = event.fill_id
        if fill_id in self._processed_fills:
            logger.debug(
                f"Skipping duplicate fill: fill_id={fill_id[:40]}... "
                f"(already processed)"
            )
            return
        self._processed_fills.add(fill_id)

        # Find order in slots or tombstones
        slot, order = state.find_order(order_id)
        is_orphan = False
        is_unknown = False

        if slot is None:
            # Check tombstoned orders
            order = get_tombstoned_order(state, order_id)
            if order:
                logger.info(f"Processing orphan fill for tombstoned order: {order_id[:20]}...")
                is_orphan = True
            else:
                # Unknown order - but still process it!
                # The feed already filters by maker_address, so if we receive this fill,
                # it's definitely ours. The order might not be in slots because:
                # - Fill arrived before place ack (race condition)
                # - Order was canceled but not tombstoned
                # - Order was placed in a previous session
                # We MUST update inventory to prevent duplicate orders.
                logger.info(
                    f"Processing fill for unknown order (trusting feed filter): {order_id[:20]}... "
                    f"{event.token.name} {event.side.name} {event.size}@{event.price}c"
                )
                is_unknown = True

        # Update inventory based on fill type (pending vs settled)
        pending_str = "PENDING" if event.is_pending else "SETTLED"

        if event.is_pending:
            # MATCHED fill - update pending inventory
            # Strategy sees this, but can't SELL yet
            state.inventory.update_from_fill(
                token=event.token,
                side=event.side,
                size=event.size,
                ts=ts,
                is_pending=True,  # Update pending inventory
            )
            logger.info(
                f"[FILL {pending_str}] Updated pending inventory: "
                f"pending_yes={state.inventory.pending_yes} pending_no={state.inventory.pending_no}"
            )
        else:
            # MINED/CONFIRMED fill - settle the pending inventory
            # Move from pending to settled (or add directly if MATCHED was missed)
            state.inventory.settle_pending(
                token=event.token,
                side=event.side,
                size=event.size,
                ts=ts,
            )
            logger.info(
                f"[FILL {pending_str}] Settled inventory: "
                f"YES={state.inventory.I_yes} NO={state.inventory.I_no} "
                f"(pending_yes={state.inventory.pending_yes} pending_no={state.inventory.pending_no})"
            )

        # Release reservation for SELL fills (only on MINED, not MATCHED)
        # For BUY fills, no reservation to release
        # For SELL fills, release happens when MINED (tokens actually transferred)
        if not event.is_pending and event.side == Side.SELL:
            if event.token == Token.YES:
                state.reservations.release_yes(event.size)
            else:
                state.reservations.release_no(event.size)

        # Update working order if found (not orphan or unknown)
        # Only do this for MINED fills (order state changes are final then)
        if not event.is_pending and slot and order and not is_orphan and not is_unknown:
            self._update_order_from_fill(slot, order, event, ts)

        # Record PnL (only for settled/MINED fills)
        # We don't want to double-count PnL
        realized_pnl = 0.0
        if not event.is_pending:
            realized_pnl, avg_cost = state.pnl.record_fill(
                ts=ts,
                token=event.token,
                side=event.side,
                size=event.size,
                price=event.price,
                order_id=order_id,
            )

        # Log fill with inventory info
        side_str = "BUY" if event.side == Side.BUY else "SELL"
        fill_value_usd = (event.size * event.price) / 100
        if is_orphan:
            marker = " [ORPHAN]"
        elif is_unknown:
            marker = " [UNKNOWN_ORDER]"
        else:
            marker = ""
        logger.info(
            f"FILL ({pending_str}){marker}: {side_str} {event.size}x{event.token.name}@{event.price}c "
            f"(${fill_value_usd:.2f}) | settled: YES={state.inventory.I_yes} NO={state.inventory.I_no} "
            f"| pending: YES={state.inventory.pending_yes} NO={state.inventory.pending_no} "
            f"| effective_net={state.inventory.effective_net_E}"
        )

        # Write to trade log (only for settled fills)
        if not event.is_pending:
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

        # NOTE: We no longer trigger resync for unknown fills because:
        # 1. The feed now filters by maker_address, so unknown fills are still OURS
        # 2. Unknown fills happen when the fill arrives before place ack (race condition)
        # 3. We already updated inventory, so no need to resync

        # Check if we're in CLOSING mode and should transition to STOPPED
        # Only check on settled fills
        if not event.is_pending and state.mode == ExecutorMode.CLOSING:
            self._check_closing_complete(ts)

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

        # In CLOSING mode, try to place closing orders
        # (covers the case where there were no open orders to cancel)
        if self._state.mode == ExecutorMode.CLOSING:
            self._maybe_place_closing_orders(ts)

    def _on_test_barrier(self, event: "BarrierEvent") -> None:
        """
        Handle test barrier event.

        Sets the barrier_processed event to signal the test that
        all events queued before this barrier have been processed.
        """
        if event.barrier_processed:
            event.barrier_processed.set()

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

    def _tombstone_order(self, order: "WorkingOrder", ts: int) -> None:
        """
        Tombstone an order for orphan fill accounting.

        When an order is canceled or removed, we keep it in tombstones so that
        late-arriving fills (due to network delays) can still be processed.

        Args:
            order: The order to tombstone
            ts: Current timestamp
        """
        expiry_ts = ts + self._policies.tombstone_retention_ms
        self._state.tombstoned_orders[order.server_order_id] = order
        self._state.tombstone_expiry_ts[order.server_order_id] = expiry_ts
        logger.debug(f"Tombstoned order: {order.server_order_id[:20]}...")

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
    # EMERGENCY CLOSE
    # -------------------------------------------------------------------------

    def emergency_close(self) -> None:
        """
        Emergency close: cancel all orders and liquidate all positions.

        This is a one-way transition to STOPPED state.

        Sequence:
        1. Set mode to CLOSING (blocks all intents)
        2. Submit cancel-all
        3. After cancel acks, place marketable SELLs for any inventory
        4. After closing orders fill/ack, transition to STOPPED

        Can be called from any thread (e.g., signal handler).
        """
        from ..types import now_ms

        state = self._state

        # Only allow from NORMAL or RESYNCING
        if state.mode == ExecutorMode.STOPPED:
            logger.warning("Already in STOPPED mode")
            return
        if state.mode == ExecutorMode.CLOSING:
            logger.warning("Already in CLOSING mode")
            return

        logger.warning("=" * 60)
        logger.warning("EMERGENCY CLOSE INITIATED")
        logger.warning("=" * 60)

        ts = now_ms()

        # Transition to CLOSING
        state.mode = ExecutorMode.CLOSING

        # Log current inventory
        logger.warning(
            f"Current inventory: YES={state.inventory.I_yes} NO={state.inventory.I_no}"
        )

        # Submit cancel-all
        logger.warning("Cancelling all open orders...")
        self._gateway.submit_cancel_all(self._market_id)

        # Clear tracked orders (we'll place closing orders fresh)
        state.bid_slot.clear()
        state.ask_slot.clear()
        state.rebuild_reservations(0)  # No buffer for closing

        # Set cooldown
        state.risk.last_cancel_all_ts = ts
        state.risk.set_cooldown(ts, self._policies.cooldown_after_cancel_all_ms)

        # Wait briefly for cancel-all to propagate, then place closing orders
        # In practice, _maybe_place_closing_orders will be called from:
        # - _on_cancel_ack when cancels confirm
        # - Idle tick if no orders were open
        # Schedule an immediate check via the event queue
        from ..types import TimerTickEvent, ExecutorEventType
        try:
            event = TimerTickEvent(
                event_type=ExecutorEventType.TIMER_TICK,
                ts_local_ms=ts,
            )
            self._event_queue.put_nowait(event)
            logger.debug("TimerTickEvent queued for closing orders")
        except Exception as e:
            logger.warning(f"Failed to queue TimerTickEvent: {e}")

    def _maybe_place_closing_orders(self, ts: int) -> None:
        """
        Place closing orders if conditions are met.

        Called after cancel acks in CLOSING mode.
        Places marketable SELLs to close any inventory.
        """
        state = self._state

        if state.mode != ExecutorMode.CLOSING:
            return

        # Check if all slots are IDLE (no pending operations)
        if not state.bid_slot.can_submit() or not state.ask_slot.can_submit():
            logger.debug("Slots busy, waiting before placing closing orders")
            return

        # Check if we have any inventory to close
        yes_to_close = state.inventory.I_yes
        no_to_close = state.inventory.I_no

        if yes_to_close == 0 and no_to_close == 0:
            logger.warning("No inventory to close, transitioning to STOPPED")
            state.mode = ExecutorMode.STOPPED
            logger.warning("=" * 60)
            logger.warning("EMERGENCY CLOSE COMPLETE - MODE: STOPPED")
            logger.warning("=" * 60)
            return

        # Place closing orders
        # NOTE: Market orders (price=1 cent, marketable) have NO min_size constraint
        # on Polymarket, so we can close ANY position regardless of size.
        logger.warning(f"Placing closing orders: SELL YES {yes_to_close}, SELL NO {no_to_close}")

        # Close YES position (any amount > 0)
        if yes_to_close > 0:
            self._place_closing_order(
                token_str="YES",
                token_id=self._yes_token_id,
                size=yes_to_close,
                ts=ts,
            )

        # Close NO position (any amount > 0)
        if no_to_close > 0:
            self._place_closing_order(
                token_str="NO",
                token_id=self._no_token_id,
                size=no_to_close,
                ts=ts,
            )

    def _place_closing_order(
        self,
        token_str: str,
        token_id: str,
        size: int,
        ts: int,
    ) -> None:
        """
        Place a single closing order (marketable SELL).

        Uses price of 1 cent to ensure immediate fill at best bid.
        """
        from ..types import Token, Side, RealOrderSpec

        token = Token.YES if token_str == "YES" else Token.NO

        # Marketable price: 1 cent (will fill at best bid)
        # This is aggressive but ensures we close
        price = 1

        spec = RealOrderSpec(
            token=token,
            token_id=token_id,
            side=Side.SELL,
            px=price,
            sz=size,
        )
        spec.client_order_id = self._next_client_order_id()

        logger.warning(f"Placing closing order: SELL {size} {token_str} @ {price}c (marketable)")

        # Submit via gateway
        action_id = self._gateway.submit_place(spec)

        # Track as pending (use "closing" as kind)
        self._state.ask_slot.pending_places[action_id] = PendingPlace(
            action_id=action_id,
            spec=spec,
            sent_at_ts=ts,
            order_kind="closing",  # Special kind for closing orders
        )

    def _check_closing_complete(self, ts: int) -> None:
        """
        Check if emergency close is complete and transition to STOPPED.

        Called after fills in CLOSING mode.
        Transitions to STOPPED when both YES and NO inventory are 0.

        NOTE: Market orders (price=1 cent) have no min_size constraint,
        so we always wait until inventory is fully cleared.
        """
        state = self._state

        if state.mode != ExecutorMode.CLOSING:
            return

        yes_remaining = state.inventory.I_yes
        no_remaining = state.inventory.I_no

        # Check if both positions are fully closed
        if yes_remaining == 0 and no_remaining == 0:
            logger.warning("=" * 60)
            logger.warning("EMERGENCY CLOSE COMPLETE - ALL POSITIONS CLOSED")
            logger.warning("Final inventory: YES=0 NO=0")
            state.mode = ExecutorMode.STOPPED
            logger.warning("MODE: STOPPED")
            logger.warning("=" * 60)

    @property
    def mode(self) -> ExecutorMode:
        """Get current executor mode."""
        return self._state.mode

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
