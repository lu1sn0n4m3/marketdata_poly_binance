"""
Strategy runner and intent mailbox.

The StrategyRunner executes the strategy at a fixed rate and publishes
intents to the executor.

Two modes of operation:
1. Mailbox mode (legacy): Publish to an IntentMailbox, executor polls.
2. Event queue mode (new): Push StrategyIntentEvent directly to event queue.

Event queue mode is preferred as it provides deterministic ordering.
"""

import logging
import queue
import threading
import time
from typing import Optional, Callable, Union

from ..types import QuoteMode, DesiredQuoteSet, StrategyIntentEvent, ExecutorEventType, now_ms
from .base import Strategy, StrategyInput

logger = logging.getLogger(__name__)


class IntentMailbox:
    """
    Single-slot overwrite mailbox for strategy intents.

    Only keeps the latest intent - older intents are discarded.
    This provides natural coalescing when Executor is slow.

    Why a mailbox instead of a queue?
        - Strategy runs at 20Hz, Executor may process slower
        - Stale intents are useless (market has moved)
        - Only the latest intent matters
        - No backpressure needed - just overwrite

    Thread Safety:
        All methods are thread-safe. The mailbox is protected by a lock.

    Example:
        mailbox = IntentMailbox()

        # Producer (StrategyRunner)
        mailbox.put(intent)  # Overwrites any existing intent

        # Consumer (Executor)
        intent = mailbox.get()  # Returns latest and clears
        if intent:
            process(intent)
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._intent: Optional[DesiredQuoteSet] = None
        self._has_new = False

    def put(self, intent: DesiredQuoteSet) -> None:
        """
        Put new intent (overwrites any existing).

        If there was an unconsumed intent, it's discarded.
        This is intentional - we only want the latest.
        """
        with self._lock:
            self._intent = intent
            self._has_new = True

    def get(self) -> Optional[DesiredQuoteSet]:
        """
        Get latest intent if available (clears mailbox).

        Returns:
            The latest intent, or None if no new intent since last get()
        """
        with self._lock:
            if not self._has_new:
                return None
            intent = self._intent
            self._has_new = False
            return intent

    def peek(self) -> Optional[DesiredQuoteSet]:
        """
        Peek at latest intent without clearing.

        Useful for debugging or monitoring.
        """
        with self._lock:
            return self._intent


class StrategyRunner:
    """
    Runs strategy at fixed Hz with debouncing.

    The runner:
    1. Runs in a background thread
    2. Ticks at a fixed rate (default 20Hz)
    3. Calls strategy.compute_quotes() each tick
    4. Publishes intent if it changed (via mailbox or event queue)

    Two modes of operation:
    - Mailbox mode: Use mailbox.put() for coalescing (legacy)
    - Event queue mode: Push StrategyIntentEvent directly (preferred)

    Debouncing:
        The runner only publishes intents that differ materially from
        the last published intent. This reduces unnecessary work for
        the Executor.

    Thread Model:
        The runner runs in its own daemon thread. It sleeps between
        ticks using Event.wait() which allows for clean shutdown.

    Example (event queue mode - preferred):
        runner = StrategyRunner(
            strategy=DefaultMMStrategy(),
            event_queue=executor_event_queue,
            get_input=get_input,
            tick_hz=20,
        )

    Example (mailbox mode - legacy):
        runner = StrategyRunner(
            strategy=DefaultMMStrategy(),
            mailbox=intent_mailbox,
            get_input=get_input,
            tick_hz=20,
        )
    """

    def __init__(
        self,
        strategy: Strategy,
        get_input: Callable[[], StrategyInput],
        mailbox: Optional[IntentMailbox] = None,
        event_queue: Optional[queue.Queue] = None,
        tick_hz: int = 20,  # Strategy tick rate
    ):
        """
        Initialize strategy runner.

        Args:
            strategy: Strategy instance to run
            get_input: Callback to get current StrategyInput.
                       Called once per tick.
            mailbox: IntentMailbox to publish intents to (legacy mode)
            event_queue: Event queue to push StrategyIntentEvent to (preferred)
            tick_hz: How many times per second to run strategy.
                     Higher = more responsive, more CPU.
                     20Hz is a good balance for most markets.

        Note: Must provide either mailbox or event_queue (or both for hybrid).
              If both provided, intents are pushed to event_queue only.
        """
        if mailbox is None and event_queue is None:
            raise ValueError("Must provide either mailbox or event_queue")

        self._strategy = strategy
        self._mailbox = mailbox
        self._event_queue = event_queue
        self._get_input = get_input
        self._tick_interval = 1.0 / tick_hz

        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

        # Stats
        self._ticks = 0
        self._intents_produced = 0
        self._last_intent: Optional[DesiredQuoteSet] = None

    def start(self) -> None:
        """Start the strategy runner thread."""
        if self._thread and self._thread.is_alive():
            logger.warning("StrategyRunner already running")
            return

        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run_loop,
            name="Strategy-Runner",
            daemon=True,
        )
        self._thread.start()
        logger.info(f"StrategyRunner started at {1/self._tick_interval:.0f} Hz")

    def stop(self, timeout: float = 2.0) -> None:
        """
        Stop the strategy runner thread.

        Args:
            timeout: Maximum seconds to wait for thread to stop
        """
        logger.info("StrategyRunner stopping...")
        self._stop_event.set()

        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=timeout)
            if self._thread.is_alive():
                logger.warning("StrategyRunner did not stop in time")

        logger.info(f"StrategyRunner stopped. Ticks={self._ticks}, Intents={self._intents_produced}")

    def _run_loop(self) -> None:
        """Main strategy loop."""
        next_tick = time.monotonic()

        while not self._stop_event.is_set():
            now = time.monotonic()

            # Sleep until next tick
            if now < next_tick:
                sleep_time = next_tick - now
                if self._stop_event.wait(sleep_time):
                    break

            # Schedule next tick
            next_tick = now + self._tick_interval

            # Run strategy tick
            self._tick()

    def _tick(self) -> None:
        """Run a single strategy tick."""
        self._ticks += 1

        try:
            # Get current input
            inp = self._get_input()

            # Compute quotes
            intent = self._strategy.compute_quotes(inp)

            # Debounce: only publish if changed materially
            if not self._should_publish(intent):
                return

            # Publish intent
            self._publish_intent(intent)
            self._last_intent = intent
            self._intents_produced += 1

        except Exception as e:
            logger.error(f"Strategy tick error: {e}")

    def _publish_intent(self, intent: DesiredQuoteSet) -> None:
        """Publish intent via event queue (preferred) or mailbox (legacy)."""
        if self._event_queue is not None:
            # Wrap as event and push to executor queue directly
            event = StrategyIntentEvent(
                event_type=ExecutorEventType.STRATEGY_INTENT,
                ts_local_ms=now_ms(),
                intent=intent,
            )
            try:
                self._event_queue.put_nowait(event)
            except queue.Full:
                logger.warning("Event queue full, intent dropped")
        elif self._mailbox is not None:
            # Legacy mailbox mode
            self._mailbox.put(intent)

    def _should_publish(self, intent: DesiredQuoteSet) -> bool:
        """
        Check if intent changed enough to publish.

        We skip publishing if the intent is essentially the same as
        the last one. This reduces work for the Executor.

        Changes that trigger publish:
        - Mode changed (NORMAL -> STOP, etc.)
        - STOP mode (always publish to ensure cancellation)
        - Bid/ask enabled status changed
        - Price changed by >= 1 cent
        - Size changed by >= 5 shares
        """
        if self._last_intent is None:
            return True

        last = self._last_intent

        # Mode changed
        if intent.mode != last.mode:
            return True

        # STOP mode - always publish changes
        if intent.mode == QuoteMode.STOP:
            return True

        # Bid changed
        if intent.bid_yes.enabled != last.bid_yes.enabled:
            return True
        if intent.bid_yes.enabled:
            if abs(intent.bid_yes.px_yes - last.bid_yes.px_yes) >= 1:
                return True
            if abs(intent.bid_yes.sz - last.bid_yes.sz) >= 5:
                return True

        # Ask changed
        if intent.ask_yes.enabled != last.ask_yes.enabled:
            return True
        if intent.ask_yes.enabled:
            if abs(intent.ask_yes.px_yes - last.ask_yes.px_yes) >= 1:
                return True
            if abs(intent.ask_yes.sz - last.ask_yes.sz) >= 5:
                return True

        return False

    @property
    def stats(self) -> dict:
        """Get runner statistics."""
        return {
            "ticks": self._ticks,
            "intents_produced": self._intents_produced,
            "last_intent_mode": self._last_intent.mode.name if self._last_intent else None,
        }

    @property
    def is_running(self) -> bool:
        """Check if runner is running."""
        return self._thread is not None and self._thread.is_alive()
