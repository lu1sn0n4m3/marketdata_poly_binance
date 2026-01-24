"""Tests for Executor module."""

import time
import queue
import pytest
from unittest.mock import Mock, MagicMock

from tradingsystem.mm_types import (
    Token,
    Side,
    OrderStatus,
    QuoteMode,
    ExecutorConfig,
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
    now_ms,
)
from tradingsystem.executor import (
    OrderMaterializer,
    ExecutorState,
    ExecutorActor,
    PendingPlace,
    PendingCancel,
)
from tradingsystem.strategy import IntentMailbox
from tradingsystem.gateway import Gateway


class TestOrderMaterializer:
    """Tests for OrderMaterializer."""

    @pytest.fixture
    def materializer(self):
        """Create materializer."""
        return OrderMaterializer(
            yes_token_id="yes_token_123",
            no_token_id="no_token_456",
        )

    def test_materialize_bid_no_inventory(self, materializer):
        """Bid without inventory becomes BUY YES."""
        inventory = InventoryState()
        spec = materializer.materialize_bid(px_yes=50, sz=100, inventory=inventory)

        assert spec.token == Token.YES
        assert spec.side == Side.BUY
        assert spec.px == 50
        assert spec.sz == 100

    def test_materialize_bid_with_no_inventory(self, materializer):
        """Bid with NO inventory becomes SELL NO at complement."""
        inventory = InventoryState(I_yes=0, I_no=50)
        spec = materializer.materialize_bid(px_yes=40, sz=100, inventory=inventory)

        assert spec.token == Token.NO
        assert spec.side == Side.SELL
        assert spec.px == 60  # Complement: 100 - 40
        assert spec.sz == 50  # Limited by inventory

    def test_materialize_ask_no_inventory(self, materializer):
        """Ask without inventory becomes BUY NO at complement."""
        inventory = InventoryState()
        spec = materializer.materialize_ask(px_yes=60, sz=100, inventory=inventory)

        assert spec.token == Token.NO
        assert spec.side == Side.BUY
        assert spec.px == 40  # Complement: 100 - 60
        assert spec.sz == 100

    def test_materialize_ask_with_yes_inventory(self, materializer):
        """Ask with YES inventory becomes SELL YES."""
        inventory = InventoryState(I_yes=50, I_no=0)
        spec = materializer.materialize_ask(px_yes=60, sz=100, inventory=inventory)

        assert spec.token == Token.YES
        assert spec.side == Side.SELL
        assert spec.px == 60
        assert spec.sz == 50  # Limited by inventory

    def test_materialize_bid_limit_to_no_inventory(self, materializer):
        """Bid size limited by available NO inventory."""
        inventory = InventoryState(I_yes=0, I_no=25)
        spec = materializer.materialize_bid(px_yes=50, sz=100, inventory=inventory)

        assert spec.sz == 25  # Limited to NO inventory

    def test_materialize_ask_limit_to_yes_inventory(self, materializer):
        """Ask size limited by available YES inventory."""
        inventory = InventoryState(I_yes=30, I_no=0)
        spec = materializer.materialize_ask(px_yes=50, sz=100, inventory=inventory)

        assert spec.sz == 30  # Limited to YES inventory


class TestExecutorState:
    """Tests for ExecutorState."""

    def test_initial_state(self):
        """Test initial state values."""
        state = ExecutorState()

        assert state.inventory.I_yes == 0
        assert state.inventory.I_no == 0
        assert len(state.working_orders) == 0
        assert len(state.pending_places) == 0
        assert len(state.pending_cancels) == 0
        assert state.bid_order_id is None
        assert state.ask_order_id is None

    def test_get_working_bid(self):
        """Test getting working bid order."""
        from tradingsystem.mm_types import WorkingOrder

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

        state.working_orders["server_1"] = order
        state.bid_order_id = "server_1"

        assert state.get_working_bid() is order
        assert state.get_working_ask() is None

    def test_get_working_ask(self):
        """Test getting working ask order."""
        from tradingsystem.mm_types import WorkingOrder

        state = ExecutorState()

        spec = RealOrderSpec(
            token=Token.YES,
            side=Side.SELL,
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

        state.working_orders["server_1"] = order
        state.ask_order_id = "server_1"

        assert state.get_working_ask() is order
        assert state.get_working_bid() is None


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
    def mailbox(self):
        """Create intent mailbox."""
        return IntentMailbox()

    @pytest.fixture
    def event_queue(self):
        """Create event queue."""
        return queue.Queue(maxsize=100)

    @pytest.fixture
    def executor(self, mock_gateway, mailbox, event_queue):
        """Create executor."""
        config = ExecutorConfig(
            place_timeout_ms=5000,
            cancel_timeout_ms=5000,
        )
        return ExecutorActor(
            gateway=mock_gateway,
            mailbox=mailbox,
            event_queue=event_queue,
            config=config,
            yes_token_id="yes_token_123",
            no_token_id="no_token_456",
            market_id="market_123",
        )

    def test_executor_starts_and_stops(self, executor):
        """Test executor can start and stop."""
        executor.start()
        assert executor.is_running

        executor.stop()
        assert not executor.is_running

    def test_handle_stop_intent(self, executor, mailbox, mock_gateway):
        """Test STOP intent triggers cancel-all."""
        intent = DesiredQuoteSet.stop(ts=now_ms())
        mailbox.put(intent)

        executor.start()
        time.sleep(0.1)
        executor.stop()

        mock_gateway.submit_cancel_all.assert_called_once_with("market_123")

    def test_handle_normal_intent_places_orders(self, executor, mailbox, mock_gateway):
        """Test normal intent places bid and ask orders."""
        intent = DesiredQuoteSet(
            created_at_ts=now_ms(),
            pm_seq=1,
            bn_seq=1,
            mode=QuoteMode.NORMAL,
            bid_yes=DesiredQuoteLeg(enabled=True, px_yes=48, sz=100),
            ask_yes=DesiredQuoteLeg(enabled=True, px_yes=52, sz=100),
        )
        mailbox.put(intent)

        executor.start()
        time.sleep(0.1)
        executor.stop()

        # Should have called submit_place twice (bid and ask)
        assert mock_gateway.submit_place.call_count == 2

    def test_handle_one_sided_buy_intent(self, executor, mailbox, mock_gateway):
        """Test one-sided buy intent only places bid."""
        intent = DesiredQuoteSet(
            created_at_ts=now_ms(),
            pm_seq=1,
            bn_seq=1,
            mode=QuoteMode.ONE_SIDED_BUY,
            bid_yes=DesiredQuoteLeg(enabled=True, px_yes=48, sz=100),
            ask_yes=DesiredQuoteLeg(enabled=False, px_yes=52, sz=0),
        )
        mailbox.put(intent)

        executor.start()
        time.sleep(0.1)
        executor.stop()

        # Should only place bid
        assert mock_gateway.submit_place.call_count == 1

    def test_handle_fill_updates_inventory(self, executor, event_queue):
        """Test fill event updates inventory for tracked orders."""
        # Pre-populate working order (simulates order being placed and acked)
        working_order = WorkingOrder(
            client_order_id="client_1",
            server_order_id="order_1",
            order_spec=RealOrderSpec(token=Token.YES, side=Side.BUY, px=50, sz=10),
            status=OrderStatus.WORKING,
            created_ts=now_ms(),
            last_state_change_ts=now_ms(),
            filled_sz=0,
        )
        executor._state.working_orders["order_1"] = working_order

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

    def test_handle_fill_sell_decreases_inventory(self, executor, event_queue):
        """Test sell fill decreases inventory for tracked orders."""
        # Pre-populate working order
        working_order = WorkingOrder(
            client_order_id="client_1",
            server_order_id="order_1",
            order_spec=RealOrderSpec(token=Token.YES, side=Side.SELL, px=50, sz=20),
            status=OrderStatus.WORKING,
            created_ts=now_ms(),
            last_state_change_ts=now_ms(),
            filled_sz=0,
        )
        executor._state.working_orders["order_1"] = working_order

        executor.set_inventory(yes=50, no=0)
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

        assert executor.inventory.I_yes == 30

    def test_set_inventory(self, executor):
        """Test setting initial inventory."""
        executor.set_inventory(yes=100, no=50)

        assert executor.inventory.I_yes == 100
        assert executor.inventory.I_no == 50

    def test_gateway_result_success_tracks_order(self, executor, mailbox, event_queue, mock_gateway):
        """Test successful gateway result creates working order."""
        # Place an order
        intent = DesiredQuoteSet(
            created_at_ts=now_ms(),
            pm_seq=1,
            bn_seq=1,
            mode=QuoteMode.NORMAL,
            bid_yes=DesiredQuoteLeg(enabled=True, px_yes=48, sz=100),
            ask_yes=DesiredQuoteLeg(enabled=False, px_yes=52, sz=0),
        )
        mailbox.put(intent)

        executor.start()
        time.sleep(0.05)

        # Simulate gateway success
        gw_result = GatewayResultEvent(
            event_type=ExecutorEventType.GATEWAY_RESULT,
            ts_local_ms=now_ms(),
            action_id="gw_1",
            success=True,
            server_order_id="server_order_1",
        )
        event_queue.put(gw_result)

        time.sleep(0.1)
        executor.stop()

        # Should have a working order
        assert len(executor.state.working_orders) == 1
        assert executor.state.bid_order_id == "server_order_1"

    def test_cooldown_after_cancel_all(self, executor, mailbox, mock_gateway):
        """Test cooldown is set after cancel-all."""
        intent = DesiredQuoteSet.stop(ts=now_ms())
        mailbox.put(intent)

        executor.start()
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

    def test_no_match_price_outside_tolerance(self):
        """Test no match when price outside tolerance."""
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
            px=52,  # 2 cents difference
            sz=100,
            token_id="yes",
        )
        assert not spec1.matches(spec2, price_tol=0)
        assert spec1.matches(spec2, price_tol=2)


class TestSyncOpenOrders:
    """Tests for sync_open_orders functionality."""

    @pytest.fixture
    def executor(self):
        """Create executor with mocks."""
        mock_gateway = Mock()
        mock_gateway.submit_place = Mock(return_value="action_1")
        mock_gateway.submit_cancel = Mock(return_value="action_2")
        mock_gateway.submit_cancel_all = Mock()

        from tradingsystem.strategy import IntentMailbox
        mailbox = IntentMailbox()
        event_queue = queue.Queue()
        config = ExecutorConfig()

        return ExecutorActor(
            gateway=mock_gateway,
            mailbox=mailbox,
            event_queue=event_queue,
            config=config,
            yes_token_id="yes_token_123",
            no_token_id="no_token_456",
            market_id="market_1",
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

        synced = executor.sync_open_orders(orders, "yes_token_123", "no_token_456")

        assert synced == 1
        assert "order_abc123" in executor.state.working_orders
        assert executor.state.bid_order_id == "order_abc123"  # BUY YES is a bid

        order = executor.state.working_orders["order_abc123"]
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

        synced = executor.sync_open_orders(orders, "yes_token_123", "no_token_456")

        assert synced == 1
        assert "order_def456" in executor.state.working_orders
        assert executor.state.ask_order_id == "order_def456"  # BUY NO is an ask

    def test_sync_multiple_orders(self, executor):
        """Test syncing both bid and ask orders."""
        orders = [
            {
                "id": "order_bid",
                "asset_id": "yes_token_123",
                "side": "BUY",
                "price": "0.01",
                "original_size": "5",
                "status": "LIVE",
            },
            {
                "id": "order_ask",
                "asset_id": "no_token_456",
                "side": "BUY",
                "price": "0.01",
                "original_size": "5",
                "status": "LIVE",
            },
        ]

        synced = executor.sync_open_orders(orders, "yes_token_123", "no_token_456")

        assert synced == 2
        assert executor.state.bid_order_id == "order_bid"
        assert executor.state.ask_order_id == "order_ask"

    def test_sync_skips_unknown_asset(self, executor):
        """Test syncing skips orders with unknown asset."""
        orders = [{
            "id": "order_unknown",
            "asset_id": "some_other_token",
            "side": "BUY",
            "price": "0.50",
            "original_size": "10",
            "status": "LIVE",
        }]

        synced = executor.sync_open_orders(orders, "yes_token_123", "no_token_456")

        assert synced == 0
        assert len(executor.state.working_orders) == 0

    def test_sync_handles_empty_list(self, executor):
        """Test syncing handles empty order list."""
        synced = executor.sync_open_orders([], "yes_token_123", "no_token_456")

        assert synced == 0
        assert len(executor.state.working_orders) == 0
