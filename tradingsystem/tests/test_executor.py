"""Tests for Executor module."""

import time
import queue
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
    now_ms,
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
            ts_exchange=now_ms(),
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
            ts_exchange=now_ms(),
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
