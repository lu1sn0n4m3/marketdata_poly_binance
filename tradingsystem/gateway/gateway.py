"""
Main Gateway interface for order operations.

The Gateway provides a non-blocking interface for the Executor to submit
order operations. Actions are queued and processed by a background worker.

This is the public API - external code should only interact with the Gateway
class, not the internal ActionDeque or GatewayWorker.
"""

import logging
import queue
import threading
import time
from typing import Optional

from ..types import (
    GatewayActionType,
    GatewayAction,
    GatewayResult,
    GatewayResultEvent,
    ExecutorEventType,
    RealOrderSpec,
    now_ms,
)
from .action_deque import ActionDeque
from .worker import GatewayWorker, GatewayStats

logger = logging.getLogger(__name__)


class Gateway:
    """
    Gateway interface for Executor to submit actions.

    Provides non-blocking action submission. Results are returned via
    the Executor's event queue as GatewayResultEvents.

    Priority Model:
        - Normal actions (submit_place, submit_cancel): Processed in FIFO order
        - Cancel-all (submit_cancel_all): Jumps to front of queue, clears
          pending PLACE actions to prevent stale orders

    Rate Limiting:
        - Normal actions: Limited to ~20/sec (configurable)
        - Cancel-all: No rate limiting (safety fast path)

    Usage:
        gateway = Gateway(rest_client, executor_queue)
        gateway.start()

        # Submit actions (non-blocking, returns immediately)
        action_id = gateway.submit_place(order_spec)
        action_id = gateway.submit_cancel(server_order_id)
        action_id = gateway.submit_cancel_all(market_id)  # FAST PATH

        # Results arrive in executor_queue as GatewayResultEvents

        # Stop when done
        gateway.stop()

    Thread Safety:
        All submit methods are thread-safe and can be called from any thread.
    """

    def __init__(
        self,
        rest_client: "PolymarketRestClient",
        result_queue: queue.Queue,
        max_queue_size: int = 100,
        min_action_interval_ms: int = 25,
        drop_places_on_cancel_all: bool = True,
    ):
        """
        Initialize the gateway.

        Args:
            rest_client: Polymarket REST client for executing actions
            result_queue: Queue to send results to (Executor's event queue)
            max_queue_size: Maximum pending actions before queue is full.
                           If full, submit_place/submit_cancel will drop actions.
            min_action_interval_ms: Rate limit for normal actions.
                                   Default 50ms = 20 actions/sec.
            drop_places_on_cancel_all: If True (default), clear pending PLACE
                                       actions when cancel-all is submitted.
                                       This prevents stale orders from being
                                       placed after a cancel-all.
        """
        from ..pm_rest_client import PolymarketRestClient

        self._rest = rest_client
        self._result_queue = result_queue
        self._action_deque = ActionDeque(
            maxlen=max_queue_size,
            drop_places_on_cancel_all=drop_places_on_cancel_all,
        )

        self._action_id_counter: int = 0
        self._action_id_lock = threading.Lock()

        self._worker: Optional[GatewayWorker] = None
        self._min_interval_ms = min_action_interval_ms

    def start(self) -> None:
        """Start the gateway worker thread."""
        # Reset deque stopped flag in case of restart
        self._action_deque.reset()
        self._worker = GatewayWorker(
            rest_client=self._rest,
            action_deque=self._action_deque,
            result_callback=self._on_result,
            min_action_interval_ms=self._min_interval_ms,
        )
        self._worker.start()

    def stop(self, timeout: float = 5.0) -> None:
        """
        Stop the gateway worker thread.

        Args:
            timeout: Maximum seconds to wait for worker to stop
        """
        if self._worker:
            self._worker.stop(timeout)

    def submit_place(self, order_spec: RealOrderSpec) -> str:
        """
        Submit order placement.

        Non-blocking. Returns action_id for correlation with result.

        Args:
            order_spec: Order specification (token, side, price, size)

        Returns:
            Action ID for tracking. Use this to correlate with the
            GatewayResultEvent that arrives in result_queue.
        """
        action_id = self._next_action_id()
        action = GatewayAction(
            action_type=GatewayActionType.PLACE,
            action_id=action_id,
            order_spec=order_spec,
        )
        self._enqueue(action)
        return action_id

    def submit_cancel(self, server_order_id: str) -> str:
        """
        Submit order cancellation.

        Non-blocking. Returns action_id for correlation with result.

        Args:
            server_order_id: Server-assigned order ID to cancel

        Returns:
            Action ID for tracking
        """
        action_id = self._next_action_id()
        action = GatewayAction(
            action_type=GatewayActionType.CANCEL,
            action_id=action_id,
            server_order_id=server_order_id,
        )
        self._enqueue(action)
        return action_id

    def submit_cancel_all(self, market_id: str) -> str:
        """
        Submit cancel-all for a market.

        FAST PATH: Not rate-limited, jumps to front of queue, clears
        pending PLACE actions.

        Use this for emergency situations where you need to immediately
        exit all positions. This method ensures:
        1. Cancel-all executes before any pending normal actions
        2. Pending PLACE actions are dropped (won't execute after cancel)
        3. No rate limiting delay

        Non-blocking. Returns action_id for correlation with result.

        Args:
            market_id: Market/condition ID to cancel all orders for

        Returns:
            Action ID for tracking
        """
        action_id = self._next_action_id()
        action = GatewayAction(
            action_type=GatewayActionType.CANCEL_ALL,
            action_id=action_id,
            market_id=market_id,
        )
        # For cancel-all, use priority enqueue
        self._enqueue_priority(action)
        return action_id

    def _enqueue(self, action: GatewayAction) -> bool:
        """
        Enqueue action to back of queue (normal priority).

        Returns True if queued, False if queue full.
        Non-blocking.
        """
        if not self._action_deque.put_back(action):
            logger.warning(f"Gateway queue full, dropping action {action.action_id}")
            return False
        return True

    def _enqueue_priority(self, action: GatewayAction) -> int:
        """
        Enqueue with priority (for cancel-all).

        Jumps to front of queue and clears pending PLACE actions.
        This ensures cancel-all executes immediately and stale orders
        don't get placed after the cancel.

        Returns:
            Number of PLACE actions that were dropped
        """
        dropped = self._action_deque.put_front(action, clear_places=True)
        if dropped > 0:
            logger.info(f"Cancel-all enqueued, dropped {dropped} pending PLACE actions")
        return dropped

    def _on_result(self, result: GatewayResult) -> None:
        """
        Callback when action completes.

        Wraps result in event and forwards to Executor queue.
        """
        event = GatewayResultEvent(
            event_type=ExecutorEventType.GATEWAY_RESULT,
            ts_local_ms=now_ms(),
            action_id=result.action_id,
            success=result.success,
            server_order_id=result.server_order_id,
            error_kind=result.error_kind,
            retryable=result.retryable,
        )

        try:
            self._result_queue.put_nowait(event)
        except queue.Full:
            # Result queue should never be full, but don't drop results
            logger.error("Result queue full! Blocking to enqueue")
            self._result_queue.put(event, block=True)

    def _next_action_id(self) -> str:
        """Generate unique action ID."""
        with self._action_id_lock:
            self._action_id_counter += 1
            return f"gw_{self._action_id_counter}_{int(time.time() * 1000)}"

    @property
    def stats(self) -> Optional[GatewayStats]:
        """Get gateway statistics."""
        if self._worker:
            return self._worker.stats
        return None

    @property
    def pending_actions(self) -> int:
        """Number of pending actions in queue."""
        return self._action_deque.qsize()

    @property
    def is_running(self) -> bool:
        """Check if gateway worker is running."""
        return (
            self._worker is not None
            and self._worker._thread is not None
            and self._worker._thread.is_alive()
        )
