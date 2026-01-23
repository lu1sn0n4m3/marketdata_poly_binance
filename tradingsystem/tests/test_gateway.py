"""Tests for Gateway and GatewayWorker."""

import time
import queue
import pytest
from unittest.mock import Mock, MagicMock, patch

from tradingsystem.mm_types import (
    Token,
    Side,
    GatewayActionType,
    GatewayAction,
    GatewayResult,
    GatewayResultEvent,
    ExecutorEventType,
    RealOrderSpec,
)
from tradingsystem.pm_rest_client import (
    PolymarketRestClient,
    OrderResult,
    CancelResult,
    OrderType,
)
from tradingsystem.gateway import Gateway, GatewayWorker, GatewayStats


class TestOrderResult:
    """Tests for OrderResult dataclass."""

    def test_success_result(self):
        """Test successful order result."""
        result = OrderResult(success=True, order_id="order_123")
        assert result.success is True
        assert result.order_id == "order_123"
        assert result.error_msg is None
        assert result.retryable is False

    def test_failure_result(self):
        """Test failed order result."""
        result = OrderResult(
            success=False,
            error_msg="Insufficient funds",
            retryable=False,
        )
        assert result.success is False
        assert result.error_msg == "Insufficient funds"
        assert result.retryable is False

    def test_retryable_result(self):
        """Test retryable error."""
        result = OrderResult(
            success=False,
            error_msg="Connection timeout",
            retryable=True,
        )
        assert result.retryable is True


class TestCancelResult:
    """Tests for CancelResult dataclass."""

    def test_success_cancel(self):
        """Test successful cancel."""
        result = CancelResult(success=True)
        assert result.success is True
        assert result.not_found is False

    def test_not_found_cancel(self):
        """Test cancel for order not found (still success)."""
        result = CancelResult(success=True, not_found=True)
        assert result.success is True
        assert result.not_found is True


class TestGatewayStats:
    """Tests for GatewayStats."""

    def test_initial_stats(self):
        """Test initial stats are zero."""
        stats = GatewayStats()
        assert stats.actions_processed == 0
        assert stats.actions_succeeded == 0
        assert stats.actions_failed == 0
        assert stats.cancel_all_count == 0


class TestGatewayWorker:
    """Tests for GatewayWorker."""

    @pytest.fixture
    def mock_rest_client(self):
        """Create mock REST client."""
        client = Mock(spec=PolymarketRestClient)
        client.place_order.return_value = OrderResult(success=True, order_id="order_123")
        client.cancel_order.return_value = CancelResult(success=True)
        client.cancel_market_orders.return_value = CancelResult(success=True)
        return client

    @pytest.fixture
    def action_queue(self):
        """Create action queue."""
        return queue.Queue(maxsize=100)

    @pytest.fixture
    def results(self):
        """List to collect results."""
        return []

    @pytest.fixture
    def worker(self, mock_rest_client, action_queue, results):
        """Create worker with short rate limit for testing."""
        def result_callback(result):
            results.append(result)

        return GatewayWorker(
            rest_client=mock_rest_client,
            action_queue=action_queue,
            result_callback=result_callback,
            min_action_interval_ms=10,  # Short for testing
        )

    def test_worker_starts_and_stops(self, worker):
        """Test worker can start and stop."""
        worker.start()
        assert worker._thread.is_alive()

        worker.stop(timeout=2.0)
        assert not worker._thread.is_alive()

    def test_place_order_action(self, worker, mock_rest_client, action_queue, results):
        """Test processing a place order action."""
        worker.start()

        spec = RealOrderSpec(
            token=Token.YES,
            token_id="yes_token_123",
            side=Side.BUY,
            px=50,
            sz=100,
            client_order_id="client_1",
        )
        action = GatewayAction(
            action_type=GatewayActionType.PLACE,
            action_id="gw_1",
            order_spec=spec,
        )
        action_queue.put(action)

        # Wait for processing
        time.sleep(0.2)
        worker.stop()

        assert len(results) == 1
        assert results[0].success is True
        assert results[0].server_order_id == "order_123"
        mock_rest_client.place_order.assert_called_once()

    def test_cancel_order_action(self, worker, mock_rest_client, action_queue, results):
        """Test processing a cancel order action."""
        worker.start()

        action = GatewayAction(
            action_type=GatewayActionType.CANCEL,
            action_id="gw_2",
            server_order_id="order_to_cancel",
        )
        action_queue.put(action)

        time.sleep(0.2)
        worker.stop()

        assert len(results) == 1
        assert results[0].success is True
        mock_rest_client.cancel_order.assert_called_once_with("order_to_cancel")

    def test_cancel_all_action(self, worker, mock_rest_client, action_queue, results):
        """Test processing a cancel-all action."""
        worker.start()

        action = GatewayAction(
            action_type=GatewayActionType.CANCEL_ALL,
            action_id="gw_3",
            market_id="market_123",
        )
        action_queue.put(action)

        time.sleep(0.2)
        worker.stop()

        assert len(results) == 1
        assert results[0].success is True
        assert worker.stats.cancel_all_count == 1
        mock_rest_client.cancel_market_orders.assert_called_once_with("market_123")

    def test_missing_order_spec_fails(self, worker, action_queue, results):
        """Test that PLACE without order_spec fails."""
        worker.start()

        action = GatewayAction(
            action_type=GatewayActionType.PLACE,
            action_id="gw_4",
            order_spec=None,  # Missing
        )
        action_queue.put(action)

        time.sleep(0.2)
        worker.stop()

        assert len(results) == 1
        assert results[0].success is False
        assert results[0].error_kind == "MISSING_ORDER_SPEC"

    def test_missing_order_id_fails(self, worker, action_queue, results):
        """Test that CANCEL without order_id fails."""
        worker.start()

        action = GatewayAction(
            action_type=GatewayActionType.CANCEL,
            action_id="gw_5",
            server_order_id=None,  # Missing
        )
        action_queue.put(action)

        time.sleep(0.2)
        worker.stop()

        assert len(results) == 1
        assert results[0].success is False
        assert results[0].error_kind == "MISSING_ORDER_ID"

    def test_stats_tracking(self, worker, mock_rest_client, action_queue, results):
        """Test that stats are tracked correctly."""
        # Make one call fail
        mock_rest_client.place_order.side_effect = [
            OrderResult(success=True, order_id="order_1"),
            OrderResult(success=False, error_msg="Failed"),
        ]

        worker.start()

        spec = RealOrderSpec(
            token=Token.YES,
            token_id="yes_token",
            side=Side.BUY,
            px=50,
            sz=100,
            client_order_id="client_1",
        )

        # Submit two orders
        for i in range(2):
            action = GatewayAction(
                action_type=GatewayActionType.PLACE,
                action_id=f"gw_{i}",
                order_spec=spec,
            )
            action_queue.put(action)

        time.sleep(0.3)
        worker.stop()

        assert worker.stats.actions_processed == 2
        assert worker.stats.actions_succeeded == 1
        assert worker.stats.actions_failed == 1


class TestGateway:
    """Tests for Gateway interface."""

    @pytest.fixture
    def mock_rest_client(self):
        """Create mock REST client."""
        client = Mock(spec=PolymarketRestClient)
        client.place_order.return_value = OrderResult(success=True, order_id="order_123")
        client.cancel_order.return_value = CancelResult(success=True)
        client.cancel_market_orders.return_value = CancelResult(success=True)
        return client

    @pytest.fixture
    def result_queue(self):
        """Create result queue."""
        return queue.Queue(maxsize=100)

    @pytest.fixture
    def gateway(self, mock_rest_client, result_queue):
        """Create gateway with short rate limit for testing."""
        return Gateway(
            rest_client=mock_rest_client,
            result_queue=result_queue,
            min_action_interval_ms=10,
        )

    def test_gateway_starts_and_stops(self, gateway):
        """Test gateway can start and stop."""
        gateway.start()
        assert gateway.is_running

        gateway.stop()
        assert not gateway.is_running

    def test_submit_place_returns_action_id(self, gateway):
        """Test submit_place returns unique action ID."""
        gateway.start()

        spec = RealOrderSpec(
            token=Token.YES,
            token_id="yes_token",
            side=Side.BUY,
            px=50,
            sz=100,
            client_order_id="client_1",
        )

        action_id = gateway.submit_place(spec)
        assert action_id.startswith("gw_")
        assert gateway.pending_actions == 1

        gateway.stop()

    def test_submit_cancel_returns_action_id(self, gateway):
        """Test submit_cancel returns unique action ID."""
        gateway.start()

        action_id = gateway.submit_cancel("order_123")
        assert action_id.startswith("gw_")

        gateway.stop()

    def test_submit_cancel_all_returns_action_id(self, gateway):
        """Test submit_cancel_all returns unique action ID."""
        gateway.start()

        action_id = gateway.submit_cancel_all("market_123")
        assert action_id.startswith("gw_")

        gateway.stop()

    def test_action_ids_are_unique(self, gateway):
        """Test that action IDs are unique."""
        gateway.start()

        spec = RealOrderSpec(
            token=Token.YES,
            token_id="yes_token",
            side=Side.BUY,
            px=50,
            sz=100,
            client_order_id="client_1",
        )

        ids = [gateway.submit_place(spec) for _ in range(5)]
        assert len(ids) == len(set(ids))

        gateway.stop()

    def test_result_forwarded_to_queue(self, gateway, result_queue):
        """Test that results are forwarded to result queue."""
        gateway.start()

        spec = RealOrderSpec(
            token=Token.YES,
            token_id="yes_token",
            side=Side.BUY,
            px=50,
            sz=100,
            client_order_id="client_1",
        )

        action_id = gateway.submit_place(spec)

        # Wait for result
        time.sleep(0.3)

        assert not result_queue.empty()
        event = result_queue.get_nowait()

        assert isinstance(event, GatewayResultEvent)
        assert event.event_type == ExecutorEventType.GATEWAY_RESULT
        assert event.action_id == action_id
        assert event.success is True

        gateway.stop()

    def test_cancel_all_not_dropped_when_queue_full(self, mock_rest_client, result_queue):
        """Test cancel-all is never dropped even when queue is full."""
        # Create gateway with tiny queue
        gateway = Gateway(
            rest_client=mock_rest_client,
            result_queue=result_queue,
            max_queue_size=2,
            min_action_interval_ms=100,  # Slow processing
        )
        gateway.start()

        spec = RealOrderSpec(
            token=Token.YES,
            token_id="yes_token",
            side=Side.BUY,
            px=50,
            sz=100,
            client_order_id="client_1",
        )

        # Fill queue with place orders
        for _ in range(3):
            gateway.submit_place(spec)

        # Cancel-all should still work (blocks if needed)
        action_id = gateway.submit_cancel_all("market_123")
        assert action_id.startswith("gw_")

        gateway.stop()

    def test_stats_accessible(self, gateway, result_queue):
        """Test that gateway stats are accessible."""
        gateway.start()

        spec = RealOrderSpec(
            token=Token.YES,
            token_id="yes_token",
            side=Side.BUY,
            px=50,
            sz=100,
            client_order_id="client_1",
        )

        gateway.submit_place(spec)
        time.sleep(0.3)

        stats = gateway.stats
        assert stats is not None
        assert stats.actions_processed >= 1

        gateway.stop()


class TestRateLimiting:
    """Tests for rate limiting behavior."""

    @pytest.fixture
    def mock_rest_client(self):
        """Create mock REST client."""
        client = Mock(spec=PolymarketRestClient)
        client.place_order.return_value = OrderResult(success=True, order_id="order_123")
        client.cancel_market_orders.return_value = CancelResult(success=True)
        return client

    def test_normal_actions_are_rate_limited(self, mock_rest_client):
        """Test that normal actions respect rate limit."""
        action_queue = queue.Queue()
        results = []

        worker = GatewayWorker(
            rest_client=mock_rest_client,
            action_queue=action_queue,
            result_callback=lambda r: results.append(r),
            min_action_interval_ms=100,  # 100ms between actions
        )
        worker.start()

        spec = RealOrderSpec(
            token=Token.YES,
            token_id="yes_token",
            side=Side.BUY,
            px=50,
            sz=100,
            client_order_id="client_1",
        )

        # Submit 3 orders
        start = time.time()
        for i in range(3):
            action = GatewayAction(
                action_type=GatewayActionType.PLACE,
                action_id=f"gw_{i}",
                order_spec=spec,
            )
            action_queue.put(action)

        # Wait for all to process
        time.sleep(0.5)
        worker.stop()

        elapsed = time.time() - start
        # Should take at least 200ms (2 intervals between 3 actions)
        assert elapsed >= 0.2
        assert len(results) == 3

    def test_cancel_all_bypasses_rate_limit(self, mock_rest_client):
        """Test that cancel-all is NOT rate limited."""
        action_queue = queue.Queue()
        results = []
        timestamps = []

        def record_result(r):
            results.append(r)
            timestamps.append(time.time())

        worker = GatewayWorker(
            rest_client=mock_rest_client,
            action_queue=action_queue,
            result_callback=record_result,
            min_action_interval_ms=500,  # Long rate limit
        )
        worker.start()

        # Submit 3 cancel-all actions
        start = time.time()
        for i in range(3):
            action = GatewayAction(
                action_type=GatewayActionType.CANCEL_ALL,
                action_id=f"gw_{i}",
                market_id="market_123",
            )
            action_queue.put(action)

        # Wait for all to process
        time.sleep(0.3)
        worker.stop()

        elapsed = time.time() - start
        # Should NOT wait for rate limit (cancel-all is fast path)
        assert elapsed < 0.5  # Much less than 1s (2 * 500ms)
        assert len(results) == 3
        assert worker.stats.cancel_all_count == 3


class TestPolymarketRestClientRetryable:
    """Tests for retryable error detection."""

    def test_timeout_is_retryable(self):
        """Test that timeout errors are retryable."""
        assert PolymarketRestClient._is_retryable_error("Connection timeout") is True

    def test_connection_error_is_retryable(self):
        """Test that connection errors are retryable."""
        assert PolymarketRestClient._is_retryable_error("Connection refused") is True

    def test_503_is_retryable(self):
        """Test that 503 errors are retryable."""
        assert PolymarketRestClient._is_retryable_error("503 Service Unavailable") is True

    def test_429_is_retryable(self):
        """Test that rate limit (429) errors are retryable."""
        assert PolymarketRestClient._is_retryable_error("429 Too Many Requests") is True

    def test_insufficient_funds_not_retryable(self):
        """Test that insufficient funds is not retryable."""
        assert PolymarketRestClient._is_retryable_error("Insufficient funds") is False

    def test_none_not_retryable(self):
        """Test that None/empty is not retryable."""
        assert PolymarketRestClient._is_retryable_error("") is False
        assert PolymarketRestClient._is_retryable_error(None) is False
