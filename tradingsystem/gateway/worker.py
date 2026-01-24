"""
Gateway worker thread for processing actions.

The worker runs in a separate thread to avoid blocking the main trading loop.
It consumes actions from the ActionDeque and sends results back via callback.
"""

import logging
import threading
import time
from dataclasses import dataclass
from typing import Optional, Callable

from ..types import (
    GatewayActionType,
    GatewayAction,
    GatewayResult,
    now_ms,
)
from ..pm_rest_client import PolymarketRestClient
from .action_deque import ActionDeque

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class GatewayStats:
    """
    Gateway statistics for monitoring and debugging.

    Attributes:
        actions_processed: Total actions processed
        actions_succeeded: Actions that completed successfully
        actions_failed: Actions that failed (errors, rejections, etc.)
        cancel_all_count: Number of cancel-all operations performed
        total_latency_ms: Cumulative latency for all actions
        actions_dropped_on_cancel_all: PLACE actions cleared by cancel-all
    """
    actions_processed: int = 0
    actions_succeeded: int = 0
    actions_failed: int = 0
    cancel_all_count: int = 0
    total_latency_ms: int = 0
    actions_dropped_on_cancel_all: int = 0


class GatewayWorker:
    """
    Gateway worker that processes actions in a separate thread.

    Receives GatewayActions from action_deque.
    Returns GatewayResults via result_callback.

    Rate Limiting:
        - Normal actions (PLACE, CANCEL): Limited to min_action_interval_ms
        - Cancel-all: NO rate limiting (safety fast path)

    Priority:
        - Cancel-all jumps to front of queue via ActionDeque
        - Pending PLACE actions are cleared when cancel-all is enqueued

    Thread Model:
        The worker runs a single thread that:
        1. Waits for actions on the deque (with timeout for shutdown checks)
        2. Applies rate limiting for normal actions
        3. Executes the action via REST client
        4. Sends result via callback

    Example:
        worker = GatewayWorker(
            rest_client=rest,
            action_deque=deque,
            result_callback=on_result,
        )
        worker.start()
        # ... gateway submits actions to deque ...
        worker.stop()
    """

    def __init__(
        self,
        rest_client: PolymarketRestClient,
        action_deque: ActionDeque,
        result_callback: Callable[[GatewayResult], None],
        min_action_interval_ms: int = 50,  # 20 actions/sec max
    ):
        """
        Initialize the gateway worker.

        Args:
            rest_client: Polymarket REST client for executing actions
            action_deque: Priority deque to receive actions from
            result_callback: Callback to send results (usually to Executor)
            min_action_interval_ms: Minimum ms between non-cancel-all actions.
                                   Default 50ms = 20 actions/sec to stay under
                                   Polymarket's rate limits.
        """
        self._rest = rest_client
        self._action_deque = action_deque
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
        """
        Stop the worker thread.

        Args:
            timeout: Maximum seconds to wait for thread to stop
        """
        logger.info("GatewayWorker stopping...")
        self._stop_event.set()
        # Wake the worker if it's waiting on empty deque
        self._action_deque.wake_all()

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
                action = self._action_deque.get(timeout=0.1)
                if action is None:
                    # Timeout or stopped
                    continue

                # Rate limit (except for cancel-all)
                if action.action_type != GatewayActionType.CANCEL_ALL:
                    self._apply_rate_limit()

                # Process action
                start_ts = now_ms()
                result = self._process_action(action)
                latency = now_ms() - start_ts

                # Update stats (including dropped count from deque)
                self.stats.actions_processed += 1
                self.stats.total_latency_ms += latency
                self.stats.actions_dropped_on_cancel_all = self._action_deque.dropped_count
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

        FAST PATH: No rate limiting applied. This is intentional -
        when you need to cancel all orders, you need it NOW.
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
