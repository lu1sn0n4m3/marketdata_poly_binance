"""Tests for Executor module."""

import time
import queue
import threading
import pytest
from unittest.mock import Mock, MagicMock

from tradingsystem.types import (
    Token,
    Side,
    OrderStatus,
    QuoteMode,
    ExecutorEventType,
    InventoryState,
    RealOrderSpec,
    WorkingOrder,
    DesiredQuoteSet,
    DesiredQuoteLeg,
    GatewayResultEvent,
    OrderAckEvent,
    CancelAckEvent,
    FillEvent,
    StrategyIntentEvent,
    BarrierEvent,
    now_ms,
    wall_ms,
)
from tradingsystem.executor import (
    ExecutorState,
    ExecutorActor,
    ExecutorPolicies,
    MinSizePolicy,
    OrderKind,
    PnLTracker,
)
from tradingsystem.executor.state import (
    OrderSlot,
    SlotState,
    ReservationLedger,
    PendingPlace,
    PendingCancel,
)
from tradingsystem.executor.planner import plan_execution, PlannedOrder, LegPlan
from tradingsystem.gateway import Gateway


class TestExecutorState:
    """Tests for ExecutorState."""

    def test_initial_state(self):
        """Test initial state values."""
        state = ExecutorState()

        assert state.inventory.I_yes == 0
        assert state.inventory.I_no == 0
        assert len(state.bid_slot.orders) == 0
        assert len(state.ask_slot.orders) == 0
        assert state.last_intent is None

    def test_find_order_in_bid_slot(self):
        """Test finding order in bid slot."""
        state = ExecutorState()

        spec = RealOrderSpec(
            token=Token.YES,
            side=Side.BUY,
            px=50,
            sz=100,
            token_id="yes_token",
        )
        order = WorkingOrder(
            client_order_id="client_1",
            server_order_id="server_1",
            order_spec=spec,
            status=OrderStatus.WORKING,
            created_ts=now_ms(),
            last_state_change_ts=now_ms(),
        )

        state.bid_slot.orders["server_1"] = order

        slot, found = state.find_order("server_1")
        assert slot is state.bid_slot
        assert found is order

    def test_find_order_not_found(self):
        """Test finding non-existent order."""
        state = ExecutorState()

        slot, found = state.find_order("nonexistent")
        assert slot is None
        assert found is None

    def test_rebuild_reservations(self):
        """Test rebuilding reservations from working orders."""
        state = ExecutorState()

        # Add a SELL YES order
        spec = RealOrderSpec(
            token=Token.YES,
            side=Side.SELL,
            px=55,
            sz=10,
            token_id="yes_token",
        )
        order = WorkingOrder(
            client_order_id="client_1",
            server_order_id="server_1",
            order_spec=spec,
            status=OrderStatus.WORKING,
            created_ts=now_ms(),
            last_state_change_ts=now_ms(),
            filled_sz=3,  # Partial fill
        )
        state.ask_slot.orders["server_1"] = order

        # Rebuild reservations
        state.rebuild_reservations()

        # Should reserve remaining size (10 - 3 = 7)
        assert state.reservations.reserved_yes == 7
        assert state.reservations.reserved_no == 0


class TestOrderSlot:
    """Tests for OrderSlot state machine."""

    def test_initial_state(self):
        """Test initial slot state."""
        slot = OrderSlot(slot_type="bid")

        assert slot.state == SlotState.IDLE
        assert slot.can_submit()
        assert len(slot.orders) == 0

    def test_transition_to_canceling(self):
        """Test transition to CANCELING state."""
        slot = OrderSlot(slot_type="bid")

        slot.transition_to_canceling()
        assert slot.state == SlotState.CANCELING
        assert not slot.can_submit()

    def test_transition_to_placing(self):
        """Test transition to PLACING state."""
        slot = OrderSlot(slot_type="ask")

        slot.transition_to_placing()
        assert slot.state == SlotState.PLACING
        assert not slot.can_submit()

    def test_on_ack_received_clears_state(self):
        """Test that ack clears state when no pending."""
        slot = OrderSlot(slot_type="bid")
        slot.state = SlotState.CANCELING

        slot.on_ack_received()
        assert slot.state == SlotState.IDLE

    def test_on_ack_received_with_pending(self):
        """Test that ack doesn't clear state when pending remain."""
        slot = OrderSlot(slot_type="bid")
        slot.state = SlotState.PLACING
        slot.pending_places["action_1"] = PendingPlace(
            action_id="action_1",
            spec=Mock(),
            sent_at_ts=0,
            order_kind="complement_buy",
        )

        slot.on_ack_received()
        # Should still be PLACING because pending_places not empty
        assert slot.state == SlotState.PLACING


class TestReservationLedger:
    """Tests for ReservationLedger."""

    def test_available_inventory(self):
        """Test available inventory calculation."""
        ledger = ReservationLedger(
            reserved_yes=10,
            reserved_no=5,
            safety_buffer_yes=2,
            safety_buffer_no=1,
        )

        assert ledger.available_yes(100) == 88  # 100 - 10 - 2
        assert ledger.available_no(50) == 44  # 50 - 5 - 1

    def test_available_never_negative(self):
        """Test available inventory never goes negative."""
        ledger = ReservationLedger(reserved_yes=100, safety_buffer_yes=50)

        assert ledger.available_yes(10) == 0  # Would be -140, clamped to 0

    def test_reserve_and_release(self):
        """Test reserve and release operations."""
        ledger = ReservationLedger()

        ledger.reserve_yes(10)
        assert ledger.reserved_yes == 10

        ledger.release_yes(3)
        assert ledger.reserved_yes == 7

        ledger.release_yes(100)  # Over-release
        assert ledger.reserved_yes == 0  # Clamped to 0


class TestExecutorActor:
    """Tests for ExecutorActor."""

    @pytest.fixture
    def mock_gateway(self):
        """Create mock gateway."""
        gateway = Mock(spec=Gateway)
        gateway.submit_place.return_value = "gw_1"
        gateway.submit_cancel.return_value = "gw_2"
        gateway.submit_cancel_all.return_value = "gw_3"
        return gateway

    @pytest.fixture
    def event_queue(self):
        """Create event queue."""
        return queue.Queue(maxsize=100)

    @pytest.fixture
    def executor(self, mock_gateway, event_queue):
        """Create executor."""
        policies = ExecutorPolicies(
            place_timeout_ms=5000,
            cancel_timeout_ms=5000,
        )
        return ExecutorActor(
            gateway=mock_gateway,
            event_queue=event_queue,
            yes_token_id="yes_token_123",
            no_token_id="no_token_456",
            market_id="market_123",
            policies=policies,
        )

    def test_executor_starts_and_stops(self, executor):
        """Test executor can start and stop."""
        executor.start()
        assert executor.is_running

        executor.stop()
        assert not executor.is_running

    def test_handle_stop_intent(self, executor, event_queue, mock_gateway):
        """Test STOP intent triggers cancel-all."""
        intent = DesiredQuoteSet.stop(ts=now_ms())

        executor.start()

        # Send intent via event queue
        event = StrategyIntentEvent(
            event_type=ExecutorEventType.STRATEGY_INTENT,
            ts_local_ms=now_ms(),
            intent=intent,
        )
        event_queue.put(event)

        time.sleep(0.1)
        executor.stop()

        mock_gateway.submit_cancel_all.assert_called_once_with("market_123")

    def test_handle_normal_intent_places_orders(self, executor, event_queue, mock_gateway):
        """Test normal intent places orders through planner."""
        intent = DesiredQuoteSet(
            created_at_ts=now_ms(),
            pm_seq=1,
            bn_seq=1,
            mode=QuoteMode.NORMAL,
            bid_yes=DesiredQuoteLeg(enabled=True, px_yes=48, sz=10),
            ask_yes=DesiredQuoteLeg(enabled=True, px_yes=52, sz=10),
        )

        executor.start()

        event = StrategyIntentEvent(
            event_type=ExecutorEventType.STRATEGY_INTENT,
            ts_local_ms=now_ms(),
            intent=intent,
        )
        event_queue.put(event)

        time.sleep(0.1)
        executor.stop()

        # Should have called submit_place twice (bid and ask)
        assert mock_gateway.submit_place.call_count == 2

    def test_handle_fill_updates_inventory(self, executor, event_queue):
        """Test fill event updates inventory for tracked orders."""
        # Pre-populate working order in bid slot
        working_order = WorkingOrder(
            client_order_id="client_1",
            server_order_id="order_1",
            order_spec=RealOrderSpec(token=Token.YES, side=Side.BUY, px=50, sz=10),
            status=OrderStatus.WORKING,
            created_ts=now_ms(),
            last_state_change_ts=now_ms(),
            filled_sz=0,
        )
        executor._state.bid_slot.orders["order_1"] = working_order

        executor.start()

        # Send fill event
        fill = FillEvent(
            event_type=ExecutorEventType.FILL,
            ts_local_ms=now_ms(),
            server_order_id="order_1",
            token=Token.YES,
            side=Side.BUY,
            price=50,
            size=10,
            fee=0.0,
            ts_exchange=wall_ms(),
            trade_id="trade_fill_inv_1",
            role="MAKER",
        )
        event_queue.put(fill)

        time.sleep(0.1)
        executor.stop()

        assert executor.inventory.I_yes == 10

    def test_handle_fill_releases_reservation(self, executor, event_queue):
        """Test fill releases reservation for SELL orders."""
        # Set up inventory and reservation
        executor.set_inventory(yes=50, no=0)
        executor._state.reservations.reserve_yes(20)

        # Pre-populate working order (SELL YES)
        working_order = WorkingOrder(
            client_order_id="client_1",
            server_order_id="order_1",
            order_spec=RealOrderSpec(token=Token.YES, side=Side.SELL, px=50, sz=20),
            status=OrderStatus.WORKING,
            created_ts=now_ms(),
            last_state_change_ts=now_ms(),
            filled_sz=0,
        )
        executor._state.ask_slot.orders["order_1"] = working_order

        executor.start()

        fill = FillEvent(
            event_type=ExecutorEventType.FILL,
            ts_local_ms=now_ms(),
            server_order_id="order_1",
            token=Token.YES,
            side=Side.SELL,
            price=50,
            size=20,
            fee=0.0,
            ts_exchange=wall_ms(),
            trade_id="trade_reservation_1",
            role="MAKER",
        )
        event_queue.put(fill)

        time.sleep(0.1)
        executor.stop()

        assert executor.inventory.I_yes == 30  # 50 - 20
        assert executor._state.reservations.reserved_yes == 0  # Released

    def test_set_inventory(self, executor):
        """Test setting initial inventory."""
        executor.set_inventory(yes=100, no=50)

        assert executor.inventory.I_yes == 100
        assert executor.inventory.I_no == 50

    def test_cooldown_after_cancel_all(self, executor, event_queue, mock_gateway):
        """Test cooldown is set after cancel-all."""
        intent = DesiredQuoteSet.stop(ts=now_ms())

        executor.start()

        event = StrategyIntentEvent(
            event_type=ExecutorEventType.STRATEGY_INTENT,
            ts_local_ms=now_ms(),
            intent=intent,
        )
        event_queue.put(event)

        time.sleep(0.1)

        # Should be in cooldown
        assert executor.state.risk.is_in_cooldown(now_ms())

        executor.stop()


class TestInventoryState:
    """Tests for InventoryState."""

    def test_update_from_fill_buy_yes(self):
        """Test buy YES fill increases YES inventory."""
        inv = InventoryState()
        inv.update_from_fill(Token.YES, Side.BUY, 50, now_ms())

        assert inv.I_yes == 50
        assert inv.I_no == 0
        assert inv.net_E == 50

    def test_update_from_fill_sell_yes(self):
        """Test sell YES fill decreases YES inventory."""
        inv = InventoryState(I_yes=100)
        inv.update_from_fill(Token.YES, Side.SELL, 30, now_ms())

        assert inv.I_yes == 70
        assert inv.net_E == 70

    def test_update_from_fill_buy_no(self):
        """Test buy NO fill increases NO inventory."""
        inv = InventoryState()
        inv.update_from_fill(Token.NO, Side.BUY, 50, now_ms())

        assert inv.I_no == 50
        assert inv.I_yes == 0
        assert inv.net_E == -50  # Short YES exposure

    def test_update_from_fill_sell_no(self):
        """Test sell NO fill decreases NO inventory."""
        inv = InventoryState(I_no=100)
        inv.update_from_fill(Token.NO, Side.SELL, 30, now_ms())

        assert inv.I_no == 70
        assert inv.net_E == -70

    def test_gross_exposure(self):
        """Test gross exposure calculation."""
        inv = InventoryState(I_yes=50, I_no=30)

        assert inv.gross_G == 80
        assert inv.net_E == 20


class TestRealOrderSpec:
    """Tests for RealOrderSpec matching."""

    def test_matches_exact(self):
        """Test exact match."""
        spec1 = RealOrderSpec(
            token=Token.YES,
            side=Side.BUY,
            px=50,
            sz=100,
            token_id="yes",
        )
        spec2 = RealOrderSpec(
            token=Token.YES,
            side=Side.BUY,
            px=50,
            sz=100,
            token_id="yes",
        )
        assert spec1.matches(spec2)

    def test_matches_within_tolerance(self):
        """Test match within tolerance."""
        spec1 = RealOrderSpec(
            token=Token.YES,
            side=Side.BUY,
            px=50,
            sz=100,
            token_id="yes",
        )
        spec2 = RealOrderSpec(
            token=Token.YES,
            side=Side.BUY,
            px=50,
            sz=103,  # Within default 5 share tolerance
            token_id="yes",
        )
        assert spec1.matches(spec2)

    def test_no_match_different_token(self):
        """Test no match with different token."""
        spec1 = RealOrderSpec(
            token=Token.YES,
            side=Side.BUY,
            px=50,
            sz=100,
            token_id="yes",
        )
        spec2 = RealOrderSpec(
            token=Token.NO,
            side=Side.BUY,
            px=50,
            sz=100,
            token_id="no",
        )
        assert not spec1.matches(spec2)

    def test_no_match_different_side(self):
        """Test no match with different side."""
        spec1 = RealOrderSpec(
            token=Token.YES,
            side=Side.BUY,
            px=50,
            sz=100,
            token_id="yes",
        )
        spec2 = RealOrderSpec(
            token=Token.YES,
            side=Side.SELL,
            px=50,
            sz=100,
            token_id="yes",
        )
        assert not spec1.matches(spec2)


class TestSyncOpenOrders:
    """Tests for sync_open_orders functionality."""

    @pytest.fixture
    def executor(self):
        """Create executor with mocks."""
        mock_gateway = Mock()
        mock_gateway.submit_place = Mock(return_value="action_1")
        mock_gateway.submit_cancel = Mock(return_value="action_2")
        mock_gateway.submit_cancel_all = Mock()

        event_queue = queue.Queue()
        policies = ExecutorPolicies()

        return ExecutorActor(
            gateway=mock_gateway,
            event_queue=event_queue,
            yes_token_id="yes_token_123",
            no_token_id="no_token_456",
            market_id="market_1",
            policies=policies,
        )

    def test_sync_yes_buy_order(self, executor):
        """Test syncing a BUY YES order."""
        orders = [{
            "id": "order_abc123",
            "asset_id": "yes_token_123",
            "side": "BUY",
            "price": "0.01",
            "original_size": "5",
            "size_matched": "0",
            "status": "LIVE",
        }]

        synced = executor.sync_open_orders(orders)

        assert synced == 1
        # BUY YES goes in bid slot
        assert "order_abc123" in executor.state.bid_slot.orders

        order = executor.state.bid_slot.orders["order_abc123"]
        assert order.order_spec.token == Token.YES
        assert order.order_spec.side == Side.BUY
        assert order.order_spec.px == 1  # 1 cent
        assert order.order_spec.sz == 5

    def test_sync_no_buy_order(self, executor):
        """Test syncing a BUY NO order."""
        orders = [{
            "id": "order_def456",
            "asset_id": "no_token_456",
            "side": "BUY",
            "price": "0.01",
            "original_size": "5",
            "status": "LIVE",
        }]

        synced = executor.sync_open_orders(orders)

        assert synced == 1
        # BUY NO goes in ask slot
        assert "order_def456" in executor.state.ask_slot.orders

    def test_sync_handles_empty_list(self, executor):
        """Test syncing handles empty order list."""
        synced = executor.sync_open_orders([])

        assert synced == 0
        assert len(executor.state.bid_slot.orders) == 0
        assert len(executor.state.ask_slot.orders) == 0


def wait_until(predicate, timeout_s: float = 1.0, poll_interval_s: float = 0.01):
    """
    Poll predicate until True or timeout.

    Much better than time.sleep() - deterministic and fast when condition is met.
    """
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(poll_interval_s)
    return False


def wait_for_processing(event_queue: queue.Queue, timeout_s: float = 1.0) -> bool:
    """
    Wait until all events queued before this call have been processed.

    Uses a barrier event (ping/pong pattern) for deterministic synchronization:
    1. Creates a barrier event with a threading.Event
    2. Queues the barrier
    3. Waits for the executor to process the barrier and set the event

    This is MUCH more reliable than checking queue.empty() because:
    - queue.empty() only tells us the queue has no items, not that processing is done
    - The barrier proves the executor has actually processed all prior events

    Args:
        event_queue: The executor's event queue
        timeout_s: Maximum time to wait

    Returns:
        True if barrier was processed, False on timeout
    """
    barrier_processed = threading.Event()
    barrier_event = BarrierEvent(
        event_type=ExecutorEventType.TEST_BARRIER,
        ts_local_ms=now_ms(),
        barrier_processed=barrier_processed,
    )
    event_queue.put(barrier_event)
    return barrier_processed.wait(timeout=timeout_s)


class TestEmergencyClose:
    """
    Tests for emergency close functionality.

    These tests are event-driven where possible, using wait_until() instead of
    raw time.sleep() to avoid race conditions and flaky tests.
    """

    @pytest.fixture
    def mock_gateway(self):
        """Create mock gateway."""
        gateway = Mock(spec=Gateway)
        gateway.submit_place.return_value = "gw_place_1"
        gateway.submit_cancel.return_value = "gw_cancel_1"
        gateway.submit_cancel_all.return_value = "gw_cancel_all_1"
        return gateway

    @pytest.fixture
    def event_queue(self):
        """Create event queue."""
        return queue.Queue(maxsize=100)

    @pytest.fixture
    def executor(self, mock_gateway, event_queue):
        """Create executor."""
        policies = ExecutorPolicies(
            place_timeout_ms=5000,
            cancel_timeout_ms=5000,
            min_order_size=5,
        )
        return ExecutorActor(
            gateway=mock_gateway,
            event_queue=event_queue,
            yes_token_id="yes_token_123",
            no_token_id="no_token_456",
            market_id="market_123",
            policies=policies,
        )

    def test_emergency_close_transitions_to_closing_mode(self, executor, mock_gateway):
        """
        Test emergency_close() synchronously sets CLOSING mode.

        This tests the SYNCHRONOUS behavior of emergency_close() - the mode
        should be CLOSING immediately after the call returns, before any
        async processing happens.

        We set inventory >= min_size to ensure it stays in CLOSING (doesn't
        race to STOPPED).
        """
        from tradingsystem.executor.state import ExecutorMode

        # Set inventory to prevent immediate STOPPED transition
        executor.set_inventory(yes=100, no=0)

        executor.start()

        # emergency_close() should synchronously set mode to CLOSING
        executor.emergency_close()

        # Assert synchronous effects (no waiting needed)
        assert executor._state.mode == ExecutorMode.CLOSING
        mock_gateway.submit_cancel_all.assert_called_once_with("market_123")

        executor.stop()

    def test_emergency_close_blocks_new_intents(self, executor, event_queue, mock_gateway):
        """
        Test that intents are ignored in CLOSING mode.

        This validates the safety invariant: once emergency close is triggered,
        the strategy cannot place new orders.
        """
        from tradingsystem.executor.state import ExecutorMode

        # Set inventory to stay in CLOSING
        executor.set_inventory(yes=100, no=0)

        executor.start()
        executor.emergency_close()

        # Wait for closing orders to be placed (so we know the loop processed)
        assert wait_until(lambda: mock_gateway.submit_place.call_count > 0, timeout_s=1.0)

        # Reset call counts
        mock_gateway.submit_place.reset_mock()

        # Send a normal intent
        intent = DesiredQuoteSet(
            created_at_ts=now_ms(),
            pm_seq=1,
            bn_seq=1,
            mode=QuoteMode.NORMAL,
            bid_yes=DesiredQuoteLeg(enabled=True, px_yes=48, sz=10),
            ask_yes=DesiredQuoteLeg(enabled=True, px_yes=52, sz=10),
        )
        event = StrategyIntentEvent(
            event_type=ExecutorEventType.STRATEGY_INTENT,
            ts_local_ms=now_ms(),
            intent=intent,
        )
        event_queue.put(event)

        # Wait for event to be processed (deterministic barrier)
        assert wait_for_processing(event_queue), "Barrier timed out"

        # Should NOT have placed any new orders (intent was ignored)
        assert mock_gateway.submit_place.call_count == 0

        executor.stop()

    def test_emergency_close_with_no_inventory_goes_to_stopped(
        self, executor, event_queue, mock_gateway
    ):
        """
        Test that emergency close with no inventory transitions to STOPPED.

        State machine: NORMAL → CLOSING → STOPPED (no inventory to close)
        """
        from tradingsystem.executor.state import ExecutorMode

        # No inventory set - should go CLOSING → STOPPED quickly
        executor.start()

        executor.emergency_close()

        # Wait for STOPPED state
        assert wait_until(
            lambda: executor._state.mode == ExecutorMode.STOPPED,
            timeout_s=1.0
        ), f"Expected STOPPED, got {executor._state.mode}"

        # No closing orders placed (no inventory)
        assert mock_gateway.submit_place.call_count == 0

        executor.stop()

    def test_emergency_close_places_closing_orders_after_timer_tick(
        self, executor, event_queue, mock_gateway
    ):
        """
        Test that closing orders are placed via timer tick (not prematurely).

        This validates the design: cancel-all is submitted first, then closing
        orders are placed after the timer tick processes the CLOSING state.
        """
        from tradingsystem.executor.state import ExecutorMode

        # Set up inventory
        executor.set_inventory(yes=100, no=50)

        executor.start()

        # Before emergency_close: no orders placed
        assert mock_gateway.submit_place.call_count == 0

        executor.emergency_close()

        # Immediately after emergency_close: cancel_all submitted, mode is CLOSING
        assert executor._state.mode == ExecutorMode.CLOSING
        mock_gateway.submit_cancel_all.assert_called_once()

        # Wait for closing orders to be placed (timer tick processes CLOSING)
        assert wait_until(
            lambda: mock_gateway.submit_place.call_count == 2,
            timeout_s=1.0
        ), f"Expected 2 closing orders, got {mock_gateway.submit_place.call_count}"

        # Verify exact orders placed
        placed_orders = [call[0][0] for call in mock_gateway.submit_place.call_args_list]

        # Find YES and NO orders
        yes_order = next((o for o in placed_orders if o.token == Token.YES), None)
        no_order = next((o for o in placed_orders if o.token == Token.NO), None)

        assert yes_order is not None, "Missing SELL YES closing order"
        assert no_order is not None, "Missing SELL NO closing order"

        # Verify YES order
        assert yes_order.side == Side.SELL
        assert yes_order.sz == 100
        assert yes_order.px == 1  # Marketable

        # Verify NO order
        assert no_order.side == Side.SELL
        assert no_order.sz == 50
        assert no_order.px == 1  # Marketable

        # Still in CLOSING (waiting for fills)
        assert executor._state.mode == ExecutorMode.CLOSING

        executor.stop()

    def test_emergency_close_small_positions(self, executor, event_queue, mock_gateway):
        """
        Test that even small positions (below normal min_size) CAN be closed.

        Market orders (price=1 cent) have no min_size constraint on Polymarket,
        so ALL positions can be closed regardless of size.

        YES=3, NO=2 → both closing orders placed.
        """
        from tradingsystem.executor.state import ExecutorMode

        # Set inventory below normal min_size (5)
        executor.set_inventory(yes=3, no=2)

        executor.start()

        executor.emergency_close()

        # Wait for closing orders - both should be placed
        assert wait_until(
            lambda: mock_gateway.submit_place.call_count == 2,
            timeout_s=1.0
        ), f"Expected 2 closing orders, got {mock_gateway.submit_place.call_count}"

        # Verify both orders placed
        placed_orders = [call[0][0] for call in mock_gateway.submit_place.call_args_list]
        yes_order = next((o for o in placed_orders if o.token == Token.YES), None)
        no_order = next((o for o in placed_orders if o.token == Token.NO), None)

        assert yes_order is not None and yes_order.sz == 3
        assert no_order is not None and no_order.sz == 2

        # Still CLOSING (waiting for fills)
        assert executor._state.mode == ExecutorMode.CLOSING

        executor.stop()

    def test_emergency_close_mixed_inventory_sizes(self, executor, event_queue, mock_gateway):
        """
        Test mixed inventory: one large position, one small position.

        Market orders have no min_size, so BOTH positions are closed.
        YES=100, NO=2 → both closing orders placed.
        """
        from tradingsystem.executor.state import ExecutorMode

        executor.set_inventory(yes=100, no=2)

        executor.start()
        executor.emergency_close()

        # Wait for both closing orders
        assert wait_until(
            lambda: mock_gateway.submit_place.call_count == 2,
            timeout_s=1.0
        ), f"Expected 2 closing orders, got {mock_gateway.submit_place.call_count}"

        # Verify both orders placed
        placed_orders = [call[0][0] for call in mock_gateway.submit_place.call_args_list]
        yes_order = next((o for o in placed_orders if o.token == Token.YES), None)
        no_order = next((o for o in placed_orders if o.token == Token.NO), None)

        assert yes_order is not None and yes_order.sz == 100
        assert no_order is not None and no_order.sz == 2

        # Still CLOSING (waiting for fills)
        assert executor._state.mode == ExecutorMode.CLOSING

        executor.stop()

    def test_fill_in_closing_mode_transitions_to_stopped(
        self, executor, event_queue, mock_gateway
    ):
        """
        Test that fills reduce inventory and transition to STOPPED when done.

        This test directly sets up the state to isolate fill handling logic.
        """
        from tradingsystem.executor.state import ExecutorMode

        # Set up inventory
        executor.set_inventory(yes=10, no=0)

        # Pre-populate a working order (simulates order after place ack)
        working_order = WorkingOrder(
            client_order_id="closing_client_1",
            server_order_id="closing_server_1",
            order_spec=RealOrderSpec(
                token=Token.YES,
                side=Side.SELL,
                px=1,
                sz=10,
                token_id="yes_token_123",
            ),
            status=OrderStatus.WORKING,
            created_ts=now_ms(),
            last_state_change_ts=now_ms(),
            filled_sz=0,
            kind="closing",
        )
        executor._state.ask_slot.orders["closing_server_1"] = working_order
        executor._state.mode = ExecutorMode.CLOSING

        executor.start()

        # Send fill
        fill = FillEvent(
            event_type=ExecutorEventType.FILL,
            ts_local_ms=now_ms(),
            server_order_id="closing_server_1",
            token=Token.YES,
            side=Side.SELL,
            price=50,
            size=10,
            fee=0.0,
            ts_exchange=wall_ms(),
            trade_id="trade_closing_1",
            role="MAKER",
        )
        event_queue.put(fill)

        # Wait for STOPPED
        assert wait_until(
            lambda: executor._state.mode == ExecutorMode.STOPPED,
            timeout_s=1.0
        ), f"Expected STOPPED, got {executor._state.mode}"

        # Inventory reduced
        assert executor._state.inventory.I_yes == 0

        executor.stop()

    def test_emergency_close_idempotent(self, executor, mock_gateway):
        """
        Test that calling emergency_close multiple times is safe (idempotent).

        Only the first call should have any effect.
        """
        from tradingsystem.executor.state import ExecutorMode

        # Set inventory to stay in CLOSING
        executor.set_inventory(yes=100, no=0)

        executor.start()

        executor.emergency_close()
        executor.emergency_close()  # Should be no-op
        executor.emergency_close()  # Should be no-op

        # Should only call cancel_all once
        assert mock_gateway.submit_cancel_all.call_count == 1
        assert executor._state.mode == ExecutorMode.CLOSING

        executor.stop()

    def test_stopped_mode_blocks_all_operations(self, executor, event_queue, mock_gateway):
        """
        Test that STOPPED mode is terminal - no new operations accepted.
        """
        from tradingsystem.executor.state import ExecutorMode

        # No inventory - will go to STOPPED
        executor.start()
        executor.emergency_close()

        # Wait for STOPPED
        assert wait_until(
            lambda: executor._state.mode == ExecutorMode.STOPPED,
            timeout_s=1.0
        )

        # Reset mocks
        mock_gateway.submit_place.reset_mock()
        mock_gateway.submit_cancel_all.reset_mock()

        # Try to send an intent
        intent = DesiredQuoteSet(
            created_at_ts=now_ms(),
            pm_seq=1,
            bn_seq=1,
            mode=QuoteMode.NORMAL,
            bid_yes=DesiredQuoteLeg(enabled=True, px_yes=48, sz=10),
            ask_yes=DesiredQuoteLeg(enabled=True, px_yes=52, sz=10),
        )
        event = StrategyIntentEvent(
            event_type=ExecutorEventType.STRATEGY_INTENT,
            ts_local_ms=now_ms(),
            intent=intent,
        )
        event_queue.put(event)

        # Wait for event to be processed (deterministic barrier)
        assert wait_for_processing(event_queue), "Barrier timed out"

        # Nothing should happen
        assert mock_gateway.submit_place.call_count == 0

        # Try emergency_close again - should be no-op
        executor.emergency_close()
        assert mock_gateway.submit_cancel_all.call_count == 0  # Still 0

        executor.stop()

    def test_closing_orders_blocked_by_pending_cancel_operations(
        self, executor, event_queue, mock_gateway
    ):
        """
        Test that closing orders wait for pending cancel operations to complete.

        This tests the slot state machine: if a slot has pending_cancels,
        can_submit() returns False and closing orders must wait.
        """
        from tradingsystem.executor.state import ExecutorMode, SlotState, PendingCancel

        executor.set_inventory(yes=100, no=0)

        executor.start()

        # Manually set up state: slot has a pending cancel (simulating
        # a cancel that was submitted but not yet acked)
        executor._state.mode = ExecutorMode.CLOSING
        executor._state.ask_slot.pending_cancels["action_123"] = PendingCancel(
            action_id="action_123",
            server_order_id="order_xyz",
            sent_at_ts=now_ms(),
            was_sell=True,
        )
        executor._state.ask_slot.state = SlotState.CANCELING

        # Force a timer tick to try placing closing orders
        from tradingsystem.types import TimerTickEvent
        tick = TimerTickEvent(
            event_type=ExecutorEventType.TIMER_TICK,
            ts_local_ms=now_ms(),
        )
        event_queue.put(tick)
        assert wait_for_processing(event_queue), "Barrier timed out"

        # No closing orders placed because slot has pending cancel
        # (can_submit() returns False for CANCELING state)
        assert mock_gateway.submit_place.call_count == 0

        # Now clear the pending cancel and set slot back to IDLE
        executor._state.ask_slot.pending_cancels.clear()
        executor._state.ask_slot.state = SlotState.IDLE

        # Send another timer tick
        tick2 = TimerTickEvent(
            event_type=ExecutorEventType.TIMER_TICK,
            ts_local_ms=now_ms(),
        )
        event_queue.put(tick2)
        assert wait_for_processing(event_queue), "Barrier 2 timed out"

        # NOW closing order should be placed
        assert mock_gateway.submit_place.call_count == 1
        spec = mock_gateway.submit_place.call_args[0][0]
        assert spec.token == Token.YES
        assert spec.side == Side.SELL
        assert spec.sz == 100

        executor.stop()

    def test_emergency_close_places_full_inventory(self, executor, event_queue, mock_gateway):
        """
        Test that emergency close places order for full inventory.

        With min_size=5 and inventory=7:
        - 7 >= 5, so we CAN place a closing order
        - Order is placed for full 7, not truncated to a multiple of min_size

        This validates: inventory=7, min=5 → places order for 7 (not 5).
        """
        from tradingsystem.executor.state import ExecutorMode

        executor.set_inventory(yes=7, no=0)

        executor.start()
        executor.emergency_close()

        # Wait for closing order
        assert wait_until(
            lambda: mock_gateway.submit_place.call_count == 1,
            timeout_s=1.0
        ), f"Expected 1 closing order, got {mock_gateway.submit_place.call_count}"

        spec = mock_gateway.submit_place.call_args[0][0]
        assert spec.token == Token.YES
        assert spec.side == Side.SELL
        assert spec.sz == 7  # Full inventory

        executor.stop()

    def test_emergency_close_full_close_sequence(
        self, executor, event_queue, mock_gateway
    ):
        """
        Test full close sequence with fills transitioning to STOPPED.

        YES=7, NO=3 → both closing orders placed
        → Simulate fills for both
        → Transitions to STOPPED when inventory is 0
        """
        from tradingsystem.executor.state import ExecutorMode

        executor.set_inventory(yes=7, no=3)

        executor.start()
        executor.emergency_close()

        # Wait for both closing orders
        assert wait_until(
            lambda: mock_gateway.submit_place.call_count == 2,
            timeout_s=1.0
        ), f"Expected 2 closing orders, got {mock_gateway.submit_place.call_count}"

        # Verify both orders placed
        placed_orders = [call[0][0] for call in mock_gateway.submit_place.call_args_list]
        yes_spec = next((o for o in placed_orders if o.token == Token.YES), None)
        no_spec = next((o for o in placed_orders if o.token == Token.NO), None)

        assert yes_spec is not None and yes_spec.sz == 7
        assert no_spec is not None and no_spec.sz == 3

        # Still CLOSING (waiting for fills)
        assert executor._state.mode == ExecutorMode.CLOSING

        # Set up working orders for fills to match
        executor._state.ask_slot.orders["closing_yes"] = WorkingOrder(
            client_order_id="closing_yes_client",
            server_order_id="closing_yes",
            order_spec=yes_spec,
            status=OrderStatus.WORKING,
            created_ts=now_ms(),
            last_state_change_ts=now_ms(),
            filled_sz=0,
            kind="closing",
        )
        executor._state.ask_slot.orders["closing_no"] = WorkingOrder(
            client_order_id="closing_no_client",
            server_order_id="closing_no",
            order_spec=no_spec,
            status=OrderStatus.WORKING,
            created_ts=now_ms(),
            last_state_change_ts=now_ms(),
            filled_sz=0,
            kind="closing",
        )

        # Send YES fill
        fill_yes = FillEvent(
            event_type=ExecutorEventType.FILL,
            ts_local_ms=now_ms(),
            server_order_id="closing_yes",
            token=Token.YES,
            side=Side.SELL,
            price=50,
            size=7,
            fee=0.0,
            ts_exchange=wall_ms(),
            trade_id="trade_full_seq_yes",
            role="MAKER",
        )
        event_queue.put(fill_yes)
        assert wait_for_processing(event_queue), "YES fill barrier timed out"

        # Still CLOSING (NO not yet filled)
        assert executor._state.mode == ExecutorMode.CLOSING
        assert executor._state.inventory.I_yes == 0
        assert executor._state.inventory.I_no == 3  # Still have NO

        # Send NO fill
        fill_no = FillEvent(
            event_type=ExecutorEventType.FILL,
            ts_local_ms=now_ms(),
            server_order_id="closing_no",
            token=Token.NO,
            side=Side.SELL,
            price=50,
            size=3,
            fee=0.0,
            ts_exchange=wall_ms(),
            trade_id="trade_full_seq_no",
            role="MAKER",
        )
        event_queue.put(fill_no)

        # Wait for STOPPED (both positions closed)
        assert wait_until(
            lambda: executor._state.mode == ExecutorMode.STOPPED,
            timeout_s=1.0
        ), f"Expected STOPPED, got {executor._state.mode}"

        # Inventory should be 0/0
        assert executor._state.inventory.I_yes == 0
        assert executor._state.inventory.I_no == 0

        executor.stop()

    def test_partial_fill_in_closing_mode_continues_close(
        self, executor, event_queue, mock_gateway
    ):
        """
        Test partial fill during emergency close continues until fully closed.

        Since market orders have no min_size constraint:
        - inventory YES=7, place closing SELL for 7
        - partial fill of 4 arrives → inventory=3
        - executor stays in CLOSING (can still close the remaining 3)
        - final fill of 3 → inventory=0 → STOPPED

        This tests: partial fills don't cause premature STOPPED transition.
        """
        from tradingsystem.executor.state import ExecutorMode

        executor.set_inventory(yes=7, no=0)

        # Pre-populate a working closing order
        closing_order = WorkingOrder(
            client_order_id="closing_1",
            server_order_id="closing_server_1",
            order_spec=RealOrderSpec(
                token=Token.YES,
                side=Side.SELL,
                px=1,
                sz=7,
                token_id="yes_token_123",
            ),
            status=OrderStatus.WORKING,
            created_ts=now_ms(),
            last_state_change_ts=now_ms(),
            filled_sz=0,
            kind="closing",
        )
        executor._state.ask_slot.orders["closing_server_1"] = closing_order
        executor._state.mode = ExecutorMode.CLOSING

        executor.start()

        # Send partial fill (4 of 7)
        fill1 = FillEvent(
            event_type=ExecutorEventType.FILL,
            ts_local_ms=now_ms(),
            server_order_id="closing_server_1",
            token=Token.YES,
            side=Side.SELL,
            price=50,
            size=4,
            fee=0.0,
            ts_exchange=wall_ms(),
            trade_id="trade_partial_1",  # Unique trade_id
            role="MAKER",
        )
        event_queue.put(fill1)
        assert wait_for_processing(event_queue), "Fill 1 barrier timed out"

        # After partial fill: inventory=3, still CLOSING (not STOPPED)
        # Because market orders can close ANY size, including the remaining 3
        assert executor._state.inventory.I_yes == 3
        assert executor._state.mode == ExecutorMode.CLOSING

        # Send final fill (remaining 3)
        fill2 = FillEvent(
            event_type=ExecutorEventType.FILL,
            ts_local_ms=now_ms(),
            server_order_id="closing_server_1",
            token=Token.YES,
            side=Side.SELL,
            price=50,
            size=3,
            fee=0.0,
            ts_exchange=wall_ms() + 1,
            trade_id="trade_partial_2",  # Different trade_id (second trade)
            role="MAKER",
        )
        event_queue.put(fill2)

        # Wait for STOPPED (inventory now 0)
        assert wait_until(
            lambda: executor._state.mode == ExecutorMode.STOPPED,
            timeout_s=1.0
        ), f"Expected STOPPED, got {executor._state.mode}"

        # Final inventory should be 0
        assert executor._state.inventory.I_yes == 0

        executor.stop()

    def test_emergency_close_idempotent_mid_flight(
        self, executor, event_queue, mock_gateway
    ):
        """
        Test emergency_close is idempotent even when called rapidly mid-flight.

        Real scenario: operator spams Ctrl+C multiple times before the loop
        processes the first emergency_close.

        Invariants:
        - cancel_all is called exactly once
        - closing orders are placed exactly once per token
        - no crash, no duplicate state corruption
        """
        from tradingsystem.executor.state import ExecutorMode

        executor.set_inventory(yes=100, no=50)

        executor.start()

        # Rapid-fire emergency_close before any barrier
        executor.emergency_close()
        executor.emergency_close()
        executor.emergency_close()

        # Now wait for processing
        assert wait_for_processing(event_queue), "Barrier timed out"

        # cancel_all should be called exactly once
        assert mock_gateway.submit_cancel_all.call_count == 1

        # Wait for closing orders
        assert wait_until(
            lambda: mock_gateway.submit_place.call_count == 2,
            timeout_s=1.0
        ), f"Expected 2 closing orders, got {mock_gateway.submit_place.call_count}"

        # Verify exactly 2 closing orders (one YES, one NO)
        placed_orders = [call[0][0] for call in mock_gateway.submit_place.call_args_list]
        tokens = [o.token for o in placed_orders]
        assert Token.YES in tokens
        assert Token.NO in tokens
        assert len(placed_orders) == 2  # No duplicates

        executor.stop()

    def test_fill_during_cancel_flight_updates_inventory_correctly(
        self, executor, event_queue, mock_gateway
    ):
        """
        Test fill arrives during cancel flight doesn't corrupt inventory/reservations.

        This is the "double-spend prevention" case:
        - Working SELL YES 20 is on the book
        - emergency_close() triggers cancel-all
        - Before cancel acks, a fill arrives for that SELL
        - inventory and reservations must update correctly
        - closing orders (when placed) must use correct available inventory

        Without this protection, we could:
        - Place closing SELLs sized for stale inventory
        - Get "insufficient balance" errors
        """
        from tradingsystem.executor.state import ExecutorMode, SlotState, PendingCancel

        # Start with inventory and a working SELL
        executor.set_inventory(yes=100, no=0)

        # Pre-populate working SELL order
        sell_order = WorkingOrder(
            client_order_id="sell_1",
            server_order_id="server_sell_1",
            order_spec=RealOrderSpec(
                token=Token.YES,
                side=Side.SELL,
                px=55,
                sz=20,
                token_id="yes_token_123",
            ),
            status=OrderStatus.WORKING,
            created_ts=now_ms(),
            last_state_change_ts=now_ms(),
            filled_sz=0,
        )
        executor._state.ask_slot.orders["server_sell_1"] = sell_order
        executor._state.reservations.reserve_yes(20)

        executor.start()

        # Set up CLOSING mode with pending cancel (simulating cancel-all in flight)
        executor._state.mode = ExecutorMode.CLOSING
        executor._state.ask_slot.pending_cancels["cancel_action_1"] = PendingCancel(
            action_id="cancel_action_1",
            server_order_id="server_sell_1",
            sent_at_ts=now_ms(),
            was_sell=True,
        )
        executor._state.ask_slot.state = SlotState.CANCELING

        # Fill arrives BEFORE cancel ack (sell filled while cancel in flight)
        fill = FillEvent(
            event_type=ExecutorEventType.FILL,
            ts_local_ms=now_ms(),
            server_order_id="server_sell_1",
            token=Token.YES,
            side=Side.SELL,
            price=55,
            size=20,  # Full fill
            fee=0.0,
            ts_exchange=wall_ms(),
            trade_id="trade_cancel_flight_1",
            role="MAKER",
        )
        event_queue.put(fill)
        assert wait_for_processing(event_queue), "Fill barrier timed out"

        # Inventory must be updated: 100 - 20 = 80
        assert executor._state.inventory.I_yes == 80

        # Reservation must be released: 20 - 20 = 0
        assert executor._state.reservations.reserved_yes == 0

        # Clear the pending cancel (simulating cancel ack or order complete)
        executor._state.ask_slot.orders.pop("server_sell_1", None)
        executor._state.ask_slot.pending_cancels.clear()
        executor._state.ask_slot.state = SlotState.IDLE

        # Force timer tick to place closing orders with UPDATED inventory
        from tradingsystem.types import TimerTickEvent
        tick = TimerTickEvent(
            event_type=ExecutorEventType.TIMER_TICK,
            ts_local_ms=now_ms(),
        )
        event_queue.put(tick)
        assert wait_for_processing(event_queue), "Tick barrier timed out"

        # Closing order should be for 80 (current inventory), not 100 (stale)
        assert mock_gateway.submit_place.call_count == 1
        spec = mock_gateway.submit_place.call_args[0][0]
        assert spec.token == Token.YES
        assert spec.side == Side.SELL
        assert spec.sz == 80  # Correct: uses updated inventory

        executor.stop()
