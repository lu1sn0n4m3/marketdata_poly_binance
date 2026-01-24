"""
Priority action queue for the Gateway.

Implements a thread-safe deque with priority support for cancel-all operations.
This is the core data structure that enables the "safety fast path" for
emergency order cancellation.

Key features:
- Normal actions (PLACE, CANCEL) go to the back of the queue
- Cancel-all jumps to the front, ensuring immediate execution
- Pending PLACE actions are cleared when cancel-all is enqueued
- Uses Condition variable for efficient blocking when empty
"""

import logging
import threading
from collections import deque
from typing import Optional

from ..types import GatewayAction, GatewayActionType

logger = logging.getLogger(__name__)


class ActionDeque:
    """
    Thread-safe deque with priority support for cancel-all.

    Normal actions go to the back. Cancel-all jumps to front and
    optionally clears pending PLACE actions to prevent stale orders
    from executing after the cancel-all.

    Uses Condition for efficient blocking when empty.

    Thread Safety:
        All methods are thread-safe. The deque is protected by a lock,
        and waiting threads are notified via a Condition variable.

    Example:
        deque = ActionDeque(maxlen=100)

        # Normal priority (back of queue)
        deque.put_back(place_action)

        # High priority (front of queue, clears pending PLACEs)
        deque.put_front(cancel_all_action)

        # Worker consumes from front
        action = deque.get(timeout=0.1)
    """

    def __init__(self, maxlen: int = 100, drop_places_on_cancel_all: bool = True):
        """
        Initialize the action deque.

        Args:
            maxlen: Maximum queue size (0 = unlimited)
            drop_places_on_cancel_all: If True, clear pending PLACE actions
                                       when cancel-all is enqueued. This prevents
                                       stale orders from being placed after a
                                       cancel-all operation.
        """
        self._deque: deque[GatewayAction] = deque()
        self._maxlen = maxlen
        self._drop_places_on_cancel_all = drop_places_on_cancel_all
        self._lock = threading.Lock()
        self._not_empty = threading.Condition(self._lock)
        self._stopped = False

        # Stats
        self.dropped_count = 0

    def put_back(self, action: GatewayAction) -> bool:
        """
        Add action to back of queue (normal priority).

        Returns True if queued, False if queue full.
        Non-blocking.

        Args:
            action: The action to enqueue

        Returns:
            True if successfully queued, False if queue is full
        """
        with self._lock:
            if self._maxlen > 0 and len(self._deque) >= self._maxlen:
                return False
            self._deque.append(action)
            self._not_empty.notify()
            return True

    def put_front(self, action: GatewayAction, clear_places: bool = True) -> int:
        """
        Add action to front of queue (high priority).

        Used for cancel-all. Optionally clears pending PLACE actions to
        prevent stale orders from executing after the cancel.

        This is the key method for the "safety fast path" - when you need
        to cancel all orders immediately, you don't want to wait behind
        a backlog of pending PLACE operations.

        Args:
            action: The action to enqueue (typically CANCEL_ALL)
            clear_places: If True and drop_places_on_cancel_all is True,
                         remove all pending PLACE actions from the queue

        Returns:
            Number of PLACE actions that were dropped (cleared)
        """
        with self._lock:
            dropped = 0
            if clear_places and self._drop_places_on_cancel_all:
                dropped = self._clear_places_locked()

            self._deque.appendleft(action)
            self._not_empty.notify()
            return dropped

    def get(self, timeout: float = 0.1) -> Optional[GatewayAction]:
        """
        Get next action from front of queue.

        Blocks up to timeout seconds if empty. This allows the worker
        to periodically check for shutdown signals.

        Args:
            timeout: Maximum seconds to wait for an action

        Returns:
            The next action, or None if timeout expired or stop() was called
        """
        with self._not_empty:
            while len(self._deque) == 0:
                if self._stopped:
                    return None
                # Wait with timeout
                notified = self._not_empty.wait(timeout)
                if not notified:
                    # Timeout expired
                    return None
                if self._stopped:
                    return None

            return self._deque.popleft()

    def wake_all(self) -> None:
        """
        Wake all waiting threads (used during shutdown).

        After calling this, all threads blocked in get() will return None.
        """
        with self._lock:
            self._stopped = True
            self._not_empty.notify_all()

    def reset(self) -> None:
        """
        Reset stopped flag (for restart).

        Call this before restarting the worker after a stop().
        """
        with self._lock:
            self._stopped = False

    def _clear_places_locked(self) -> int:
        """
        Remove all PLACE actions from queue. Must hold lock.

        This is called when a CANCEL_ALL is enqueued to prevent the
        following race condition:

        1. Backlog has old PLACE actions
        2. CANCEL_ALL cancels all orders on exchange
        3. Worker continues processing and places those stale orders

        By clearing pending PLACEs, we ensure no stale orders are placed
        after the cancel-all.

        Note: CANCEL actions are NOT cleared - they're harmless (cancelling
        an already-cancelled order is a no-op) and may still be useful.

        Returns:
            Number of actions removed
        """
        original_len = len(self._deque)
        # Keep only non-PLACE actions
        self._deque = deque(
            a for a in self._deque
            if a.action_type != GatewayActionType.PLACE
        )
        dropped = original_len - len(self._deque)
        self.dropped_count += dropped
        if dropped > 0:
            logger.info(f"Cleared {dropped} pending PLACE actions on cancel-all")
        return dropped

    def qsize(self) -> int:
        """Return current queue size."""
        with self._lock:
            return len(self._deque)

    def clear(self) -> int:
        """
        Clear all actions from the queue.

        Returns:
            Number of actions cleared
        """
        with self._lock:
            count = len(self._deque)
            self._deque.clear()
            return count
