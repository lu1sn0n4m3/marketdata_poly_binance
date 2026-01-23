"""
Gateway for Polymarket order operations.

Provides non-blocking order submission from Executor to REST API.
Gateway worker runs in a separate thread to avoid blocking.

Key design:
- Cancel-all is FAST PATH (no rate limiting)
- Normal actions are rate-limited (20/sec default)
- Results are returned via callback to Executor's queue
"""

import logging
import queue
import threading
import time
from dataclasses import dataclass, field
from typing import Optional, Callable

from .mm_types import (
    GatewayActionType,
    GatewayAction,
    GatewayResult,
    GatewayResultEvent,
    ExecutorEventType,
    RealOrderSpec,
    now_ms,
)
from .pm_rest_client import PolymarketRestClient

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class GatewayStats:
    """Gateway statistics."""
    actions_processed: int = 0
    actions_succeeded: int = 0
    actions_failed: int = 0
    cancel_all_count: int = 0
    total_latency_ms: int = 0


class GatewayWorker:
    """
    Gateway worker that processes actions in a separate thread.

    Receives GatewayActions from action_queue.
    Returns GatewayResults via result_callback.

    Rate limiting:
    - Normal actions: limited to min_action_interval_ms
    - Cancel-all: NO rate limiting (safety fast path)
    """

    def __init__(
        self,
        rest_client: PolymarketRestClient,
        action_queue: queue.Queue,
        result_callback: Callable[[GatewayResult], None],
        min_action_interval_ms: int = 50,  # 20 actions/sec max
    ):
        """
        Initialize the gateway worker.

        Args:
            rest_client: Polymarket REST client
            action_queue: Queue to receive actions from
            result_callback: Callback to send results
            min_action_interval_ms: Minimum ms between non-cancel-all actions
        """
        self._rest = rest_client
        self._action_queue = action_queue
        self._result_callback = result_callback
        self._min_interval_ms = min_action_interval_ms

        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._last_action_ts: int = 0

        self.stats = GatewayStats()

    def start(self) -> None:
        """Start the worker thread."""
        if self._thread and self._thread.is_alive():
            logger.warning("GatewayWorker already running")
            return

        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run_loop,
            name="Gateway-Worker",
            daemon=True,
        )
        self._thread.start()
        logger.info("GatewayWorker started")

    def stop(self, timeout: float = 5.0) -> None:
        """Stop the worker thread."""
        logger.info("GatewayWorker stopping...")
        self._stop_event.set()

        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=timeout)
            if self._thread.is_alive():
                logger.warning("GatewayWorker did not stop in time")

        logger.info("GatewayWorker stopped")

    def _run_loop(self) -> None:
        """Main processing loop."""
        while not self._stop_event.is_set():
            try:
                # Get action with timeout to allow checking stop_event
                try:
                    action = self._action_queue.get(timeout=0.1)
                except queue.Empty:
                    continue

                # Rate limit (except for cancel-all)
                if action.action_type != GatewayActionType.CANCEL_ALL:
                    self._apply_rate_limit()

                # Process action
                start_ts = now_ms()
                result = self._process_action(action)
                latency = now_ms() - start_ts

                # Update stats
                self.stats.actions_processed += 1
                self.stats.total_latency_ms += latency
                if result.success:
                    self.stats.actions_succeeded += 1
                else:
                    self.stats.actions_failed += 1
                if action.action_type == GatewayActionType.CANCEL_ALL:
                    self.stats.cancel_all_count += 1

                # Return result
                self._result_callback(result)

            except Exception as e:
                logger.error(f"GatewayWorker error: {e}")

        logger.info("GatewayWorker run loop exited")

    def _apply_rate_limit(self) -> None:
        """Apply rate limiting between actions."""
        now = now_ms()
        elapsed = now - self._last_action_ts

        if elapsed < self._min_interval_ms:
            sleep_ms = self._min_interval_ms - elapsed
            time.sleep(sleep_ms / 1000)

        self._last_action_ts = now_ms()

    def _process_action(self, action: GatewayAction) -> GatewayResult:
        """Process a single gateway action."""
        try:
            if action.action_type == GatewayActionType.PLACE:
                return self._place_order(action)
            elif action.action_type == GatewayActionType.CANCEL:
                return self._cancel_order(action)
            elif action.action_type == GatewayActionType.CANCEL_ALL:
                return self._cancel_all(action)
            else:
                return GatewayResult(
                    action_id=action.action_id,
                    success=False,
                    error_kind="UNKNOWN_ACTION_TYPE",
                )
        except Exception as e:
            logger.error(f"Action {action.action_id} failed: {e}")
            return GatewayResult(
                action_id=action.action_id,
                success=False,
                error_kind=type(e).__name__,
                retryable=self._is_retryable(e),
            )

    def _place_order(self, action: GatewayAction) -> GatewayResult:
        """Place a single order."""
        spec = action.order_spec
        if not spec:
            return GatewayResult(
                action_id=action.action_id,
                success=False,
                error_kind="MISSING_ORDER_SPEC",
            )

        # Convert cents to price [0, 1]
        price = spec.px / 100.0

        result = self._rest.place_order(
            token_id=spec.token_id,
            side=spec.side,
            price=price,
            size=spec.sz,
        )

        return GatewayResult(
            action_id=action.action_id,
            success=result.success,
            server_order_id=result.order_id,
            error_kind=result.error_msg if not result.success else None,
            retryable=result.retryable,
        )

    def _cancel_order(self, action: GatewayAction) -> GatewayResult:
        """Cancel a single order."""
        order_id = action.server_order_id
        if not order_id:
            return GatewayResult(
                action_id=action.action_id,
                success=False,
                error_kind="MISSING_ORDER_ID",
            )

        result = self._rest.cancel_order(order_id)

        return GatewayResult(
            action_id=action.action_id,
            success=result.success,
            error_kind=result.error_msg if not result.success else None,
        )

    def _cancel_all(self, action: GatewayAction) -> GatewayResult:
        """
        Cancel all orders for a market.

        FAST PATH: No rate limiting applied.
        """
        market_id = action.market_id
        if not market_id:
            return GatewayResult(
                action_id=action.action_id,
                success=False,
                error_kind="MISSING_MARKET_ID",
            )

        result = self._rest.cancel_market_orders(market_id)

        return GatewayResult(
            action_id=action.action_id,
            success=result.success,
            error_kind=result.error_msg if not result.success else None,
        )

    @staticmethod
    def _is_retryable(error: Exception) -> bool:
        """Determine if an error is retryable."""
        error_str = str(error).lower()
        retryable_patterns = ["timeout", "connection", "503", "502", "504", "429"]
        return any(p in error_str for p in retryable_patterns)


class Gateway:
    """
    Gateway interface for Executor to submit actions.

    Provides non-blocking action submission.
    Results are returned via the Executor's event queue.

    Usage:
        gateway = Gateway(rest_client, executor_queue)
        gateway.start()

        # Submit actions (non-blocking)
        action_id = gateway.submit_place(order_spec)
        action_id = gateway.submit_cancel(server_order_id)
        action_id = gateway.submit_cancel_all(market_id)  # FAST PATH

        # Stop when done
        gateway.stop()
    """

    def __init__(
        self,
        rest_client: PolymarketRestClient,
        result_queue: queue.Queue,
        max_queue_size: int = 100,
        min_action_interval_ms: int = 50,
    ):
        """
        Initialize the gateway.

        Args:
            rest_client: Polymarket REST client
            result_queue: Queue to send results to (Executor's event queue)
            max_queue_size: Maximum pending actions
            min_action_interval_ms: Rate limit for normal actions
        """
        self._rest = rest_client
        self._result_queue = result_queue
        self._action_queue: queue.Queue = queue.Queue(maxsize=max_queue_size)

        self._action_id_counter: int = 0
        self._action_id_lock = threading.Lock()

        self._worker: Optional[GatewayWorker] = None
        self._min_interval_ms = min_action_interval_ms

    def start(self) -> None:
        """Start the gateway worker."""
        self._worker = GatewayWorker(
            rest_client=self._rest,
            action_queue=self._action_queue,
            result_callback=self._on_result,
            min_action_interval_ms=self._min_interval_ms,
        )
        self._worker.start()

    def stop(self, timeout: float = 5.0) -> None:
        """Stop the gateway worker."""
        if self._worker:
            self._worker.stop(timeout)

    def submit_place(self, order_spec: RealOrderSpec) -> str:
        """
        Submit order placement.

        Non-blocking. Returns action_id for correlation.

        Args:
            order_spec: Order specification

        Returns:
            Action ID for tracking
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

        Non-blocking. Returns action_id for correlation.

        Args:
            server_order_id: Server-assigned order ID

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

        FAST PATH: Not rate-limited, highest priority.
        Non-blocking. Returns action_id for correlation.

        Args:
            market_id: Market/condition ID

        Returns:
            Action ID for tracking
        """
        action_id = self._next_action_id()
        action = GatewayAction(
            action_type=GatewayActionType.CANCEL_ALL,
            action_id=action_id,
            market_id=market_id,
        )
        # For cancel-all, ensure it gets queued even if full
        self._enqueue_priority(action)
        return action_id

    def _enqueue(self, action: GatewayAction) -> bool:
        """
        Enqueue action (non-blocking).

        Returns True if queued, False if queue full.
        """
        try:
            self._action_queue.put_nowait(action)
            return True
        except queue.Full:
            logger.warning(f"Gateway queue full, dropping action {action.action_id}")
            return False

    def _enqueue_priority(self, action: GatewayAction) -> None:
        """
        Enqueue with priority (for cancel-all).

        Blocks if necessary - cancel-all should never be dropped.
        """
        try:
            self._action_queue.put_nowait(action)
        except queue.Full:
            # For cancel-all, block if necessary
            logger.warning("Gateway queue full, blocking to enqueue cancel-all")
            self._action_queue.put(action, block=True)

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
        return self._action_queue.qsize()

    @property
    def is_running(self) -> bool:
        """Check if gateway is running."""
        return self._worker is not None and self._worker._thread is not None and self._worker._thread.is_alive()
