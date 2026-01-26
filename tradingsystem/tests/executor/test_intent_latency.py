"""
Tests for O(1) intent latency via mailbox pattern.

Validates that the executor processes the LATEST intent immediately
regardless of event queue depth. This is critical for fast reaction
during market shocks.

Test scenarios:
1. Mailbox mode: intent latency is O(1) regardless of queue depth
2. Comparison: mailbox mode vs deprecated event queue mode
3. Stress test: many events don't block intent processing
"""

import queue
import threading
import time
from dataclasses import dataclass
from typing import Optional
from unittest.mock import MagicMock

import pytest

from tradingsystem.executor.actor import ExecutorActor
from tradingsystem.executor.policies import ExecutorPolicies
from tradingsystem.strategy import IntentMailbox
from tradingsystem.types import (
    QuoteMode,
    DesiredQuoteLeg,
    DesiredQuoteSet,
    BarrierEvent,
    FillEvent,
    GatewayResultEvent,
    ExecutorEventType,
    Token,
    Side,
    now_ms,
)


# -----------------------------------------------------------------------------
# Test Fixtures
# -----------------------------------------------------------------------------


class MockGateway:
    """Mock gateway that records submissions but doesn't do anything."""

    def __init__(self):
        self.places: list = []
        self.cancels: list = []
        self.cancel_alls: list = []
        self._action_counter = 0

    def submit_place(self, spec) -> str:
        self._action_counter += 1
        action_id = f"place_{self._action_counter}"
        self.places.append((action_id, spec))
        return action_id

    def submit_cancel(self, order_id: str) -> str:
        self._action_counter += 1
        action_id = f"cancel_{self._action_counter}"
        self.cancels.append((action_id, order_id))
        return action_id

    def submit_cancel_all(self, market_id: str) -> str:
        self._action_counter += 1
        action_id = f"cancel_all_{self._action_counter}"
        self.cancel_alls.append((action_id, market_id))
        return action_id


def make_intent(
    px_bid: int = 45,
    px_ask: int = 55,
    sz: int = 10,
    mode: QuoteMode = QuoteMode.NORMAL,
) -> DesiredQuoteSet:
    """Create a test intent with specified prices."""
    ts = now_ms()
    return DesiredQuoteSet(
        created_at_ts=ts,
        pm_seq=1,
        bn_seq=1,
        mode=mode,
        bid_yes=DesiredQuoteLeg(enabled=True, px_yes=px_bid, sz=sz),
        ask_yes=DesiredQuoteLeg(enabled=True, px_yes=px_ask, sz=sz),
        book_ts=ts,
    )


def make_dummy_fill_event() -> FillEvent:
    """Create a dummy fill event for queue padding."""
    return FillEvent(
        event_type=ExecutorEventType.FILL,
        ts_local_ms=now_ms(),
        server_order_id=f"dummy_order_{now_ms()}",
        token=Token.YES,
        side=Side.BUY,
        price=50,
        size=1,
        fee=0.0,
        ts_exchange=now_ms(),
        trade_id=f"dummy_trade_{now_ms()}",
        role="MAKER",
        is_pending=False,
    )


def make_dummy_gateway_result() -> GatewayResultEvent:
    """Create a dummy gateway result for queue padding."""
    return GatewayResultEvent(
        event_type=ExecutorEventType.GATEWAY_RESULT,
        ts_local_ms=now_ms(),
        action_id=f"dummy_action_{now_ms()}",
        success=True,
        server_order_id=f"dummy_order_{now_ms()}",
    )


# -----------------------------------------------------------------------------
# Test: O(1) Intent Latency with Mailbox
# -----------------------------------------------------------------------------


class TestIntentLatencyMailbox:
    """
    Test that mailbox mode provides O(1) intent latency.

    The key property: intent latency should NOT grow with queue depth.
    """

    @pytest.fixture
    def setup_executor(self):
        """Set up executor with mailbox mode."""
        event_queue = queue.Queue(maxsize=1000)
        mailbox = IntentMailbox()
        gateway = MockGateway()

        executor = ExecutorActor(
            gateway=gateway,
            event_queue=event_queue,
            intent_mailbox=mailbox,
            yes_token_id="YES_TOKEN",
            no_token_id="NO_TOKEN",
            market_id="TEST_MARKET",
            policies=ExecutorPolicies(min_order_size=5),
        )
        executor.start()

        yield {
            "executor": executor,
            "event_queue": event_queue,
            "mailbox": mailbox,
            "gateway": gateway,
        }

        executor.stop(timeout=2.0)

    def test_intent_processed_with_empty_queue(self, setup_executor):
        """Baseline: intent processed quickly with empty queue."""
        executor = setup_executor["executor"]
        mailbox = setup_executor["mailbox"]
        event_queue = setup_executor["event_queue"]

        # Publish intent
        intent = make_intent(px_bid=40, px_ask=60, sz=10)
        t_start = time.monotonic()
        mailbox.put(intent)

        # Wait for processing via barrier
        barrier = threading.Event()
        event_queue.put(BarrierEvent(
            event_type=ExecutorEventType.TEST_BARRIER,
            ts_local_ms=now_ms(),
            barrier_processed=barrier,
        ))
        barrier.wait(timeout=2.0)
        t_end = time.monotonic()

        # Verify intent was processed
        assert executor.state.last_intent is not None
        assert executor.state.last_intent.bid_yes.px_yes == 40
        assert executor.state.last_intent.ask_yes.px_yes == 60

        # Latency should be < 100ms with empty queue
        latency_ms = (t_end - t_start) * 1000
        assert latency_ms < 100, f"Empty queue latency {latency_ms:.1f}ms exceeds 100ms"

    def test_intent_latency_bounded_under_load(self, setup_executor):
        """
        Core test: intent latency stays bounded even with many queued events.

        This tests the O(1) guarantee of the mailbox pattern.
        """
        executor = setup_executor["executor"]
        mailbox = setup_executor["mailbox"]
        event_queue = setup_executor["event_queue"]

        # Fill queue with many dummy events
        num_events = 100
        for _ in range(num_events):
            event_queue.put(make_dummy_gateway_result())

        # Record time and publish urgent intent
        intent = make_intent(px_bid=30, px_ask=70, sz=20)  # Wider spread = urgent
        t_publish = time.monotonic()
        mailbox.put(intent)

        # Add barrier AFTER the dummy events
        barrier = threading.Event()
        event_queue.put(BarrierEvent(
            event_type=ExecutorEventType.TEST_BARRIER,
            ts_local_ms=now_ms(),
            barrier_processed=barrier,
        ))
        barrier.wait(timeout=5.0)
        t_processed = time.monotonic()

        # Verify intent was processed
        assert executor.state.last_intent is not None
        assert executor.state.last_intent.bid_yes.px_yes == 30
        assert executor.state.last_intent.ask_yes.px_yes == 70

        # Key assertion: latency should be bounded (not proportional to queue depth)
        # With mailbox, we poll after each event, so worst case is N * event_processing_time
        # But the intent itself is polled with O(1), not waiting in queue
        latency_ms = (t_processed - t_publish) * 1000

        # Even with 100 events, total time should be < 500ms
        # (each event ~1-5ms processing, intent polled after each)
        assert latency_ms < 500, (
            f"Intent latency {latency_ms:.1f}ms with {num_events} queued events "
            f"exceeds 500ms bound"
        )

    def test_latest_intent_wins(self, setup_executor):
        """Verify that only the LATEST intent is processed (older ones discarded)."""
        executor = setup_executor["executor"]
        mailbox = setup_executor["mailbox"]
        event_queue = setup_executor["event_queue"]

        # Publish multiple intents rapidly
        mailbox.put(make_intent(px_bid=45, px_ask=55))  # Will be overwritten
        mailbox.put(make_intent(px_bid=40, px_ask=60))  # Will be overwritten
        mailbox.put(make_intent(px_bid=35, px_ask=65))  # This one wins

        # Wait for processing
        barrier = threading.Event()
        event_queue.put(BarrierEvent(
            event_type=ExecutorEventType.TEST_BARRIER,
            ts_local_ms=now_ms(),
            barrier_processed=barrier,
        ))
        barrier.wait(timeout=2.0)

        # Only the last intent should be processed
        assert executor.state.last_intent is not None
        assert executor.state.last_intent.bid_yes.px_yes == 35
        assert executor.state.last_intent.ask_yes.px_yes == 65

    def test_stop_intent_processed_immediately(self, setup_executor):
        """
        Critical test: STOP intent is processed quickly even under load.

        This simulates the scenario where BTC drops and we need to stop trading.
        """
        executor = setup_executor["executor"]
        mailbox = setup_executor["mailbox"]
        event_queue = setup_executor["event_queue"]
        gateway = setup_executor["gateway"]

        # First, set up some normal quoting
        mailbox.put(make_intent(px_bid=45, px_ask=55))
        time.sleep(0.05)  # Let it process

        # Fill queue with events (simulating volatility)
        for _ in range(50):
            event_queue.put(make_dummy_gateway_result())

        # Publish STOP intent
        stop_intent = DesiredQuoteSet.stop(ts=now_ms(), reason="BTC_CRASH")
        t_stop = time.monotonic()
        mailbox.put(stop_intent)

        # Wait for processing
        barrier = threading.Event()
        event_queue.put(BarrierEvent(
            event_type=ExecutorEventType.TEST_BARRIER,
            ts_local_ms=now_ms(),
            barrier_processed=barrier,
        ))
        barrier.wait(timeout=5.0)
        t_done = time.monotonic()

        # Verify STOP was processed
        assert executor.state.last_intent is not None
        assert executor.state.last_intent.mode == QuoteMode.STOP

        # Verify cancel-all was triggered
        assert len(gateway.cancel_alls) > 0

        # STOP latency should be bounded
        latency_ms = (t_done - t_stop) * 1000
        assert latency_ms < 500, f"STOP latency {latency_ms:.1f}ms exceeds 500ms"


# -----------------------------------------------------------------------------
# Test: Scaling Behavior
# -----------------------------------------------------------------------------


class TestIntentLatencyScaling:
    """
    Test that intent latency scales O(1), not O(n) with queue depth.

    Compare latencies with different queue depths to verify scaling.
    """

    def test_latency_scaling_with_queue_depth(self):
        """
        Measure intent latency at different queue depths.

        Expected: latency should be roughly constant (O(1)), not linear (O(n)).
        """
        results = []

        for num_events in [0, 10, 50, 100, 200]:
            # Set up fresh executor for each test
            event_queue = queue.Queue(maxsize=1000)
            mailbox = IntentMailbox()
            gateway = MockGateway()

            executor = ExecutorActor(
                gateway=gateway,
                event_queue=event_queue,
                intent_mailbox=mailbox,
                yes_token_id="YES_TOKEN",
                no_token_id="NO_TOKEN",
                market_id="TEST_MARKET",
                policies=ExecutorPolicies(min_order_size=5),
            )
            executor.start()

            try:
                # Fill queue with dummy events
                for _ in range(num_events):
                    event_queue.put(make_dummy_gateway_result())

                # Publish intent and measure
                intent = make_intent(px_bid=40, px_ask=60)
                t_start = time.monotonic()
                mailbox.put(intent)

                # Wait for processing
                barrier = threading.Event()
                event_queue.put(BarrierEvent(
                    event_type=ExecutorEventType.TEST_BARRIER,
                    ts_local_ms=now_ms(),
                    barrier_processed=barrier,
                ))
                barrier.wait(timeout=5.0)
                t_end = time.monotonic()

                latency_ms = (t_end - t_start) * 1000
                results.append((num_events, latency_ms))

            finally:
                executor.stop(timeout=2.0)

        # Analyze results
        latency_0 = results[0][1]
        latency_200 = results[-1][1]

        # Key assertion: absolute latency is bounded, even with 200 queued events
        # With mailbox, the intent is polled after EACH event, so it's processed
        # incrementally, not waiting behind all events. Total time includes
        # processing all events, but intent is seen immediately.
        #
        # The important property: latency stays low in absolute terms.
        # Even with 200 events, we expect < 50ms (each event ~0.1-0.5ms to process)
        assert latency_200 < 50, (
            f"Latency with 200 events ({latency_200:.1f}ms) exceeds 50ms bound"
        )

        # Also verify the scaling is sub-linear (not O(n) queue-wait behavior)
        # With old event queue mode, 200 events would mean intent waits behind all
        # With mailbox mode, intent is polled after each event (no queue wait)
        # Allow 20x factor for processing overhead variance
        assert latency_200 < max(latency_0 * 20, 10), (
            f"Latency scaling appears worse than expected: "
            f"0 events={latency_0:.1f}ms, 200 events={latency_200:.1f}ms"
        )

        # Log results for debugging
        print("\nIntent latency scaling test results:")
        for num_events, latency in results:
            print(f"  {num_events:3d} events: {latency:6.1f}ms")
        print(f"  Scaling factor (200/0): {latency_200/max(latency_0, 0.1):.1f}x")


# -----------------------------------------------------------------------------
# Test: Mailbox Polling Frequency
# -----------------------------------------------------------------------------


class TestMailboxPolling:
    """Test that mailbox is polled at the expected frequency."""

    def test_mailbox_polled_after_each_event(self):
        """
        Verify mailbox is polled after processing each event.

        This ensures intent updates are picked up promptly.
        """
        event_queue = queue.Queue(maxsize=100)
        mailbox = IntentMailbox()
        gateway = MockGateway()

        executor = ExecutorActor(
            gateway=gateway,
            event_queue=event_queue,
            intent_mailbox=mailbox,
            yes_token_id="YES_TOKEN",
            no_token_id="NO_TOKEN",
            market_id="TEST_MARKET",
            policies=ExecutorPolicies(min_order_size=5),
        )
        executor.start()

        try:
            intents_seen = []

            # Interleave events and intents
            for i in range(5):
                # Put dummy event
                event_queue.put(make_dummy_gateway_result())
                # Put intent with unique price
                mailbox.put(make_intent(px_bid=40 + i, px_ask=60 - i))

                # Small delay to let executor process
                time.sleep(0.02)

                # Record what intent executor has
                if executor.state.last_intent:
                    intents_seen.append(executor.state.last_intent.bid_yes.px_yes)

            # Wait for all processing
            barrier = threading.Event()
            event_queue.put(BarrierEvent(
                event_type=ExecutorEventType.TEST_BARRIER,
                ts_local_ms=now_ms(),
                barrier_processed=barrier,
            ))
            barrier.wait(timeout=2.0)

            # Final intent should be the last one published
            assert executor.state.last_intent is not None
            assert executor.state.last_intent.bid_yes.px_yes == 44  # 40 + 4

        finally:
            executor.stop(timeout=2.0)

    def test_mailbox_polled_on_idle(self):
        """Verify mailbox is polled during idle ticks (no events)."""
        event_queue = queue.Queue(maxsize=100)
        mailbox = IntentMailbox()
        gateway = MockGateway()

        executor = ExecutorActor(
            gateway=gateway,
            event_queue=event_queue,
            intent_mailbox=mailbox,
            yes_token_id="YES_TOKEN",
            no_token_id="NO_TOKEN",
            market_id="TEST_MARKET",
            policies=ExecutorPolicies(min_order_size=5),
        )
        executor.start()

        try:
            # Don't put any events, just intent
            mailbox.put(make_intent(px_bid=42, px_ask=58))

            # Wait for idle tick to pick it up (timeout is 100ms in executor loop)
            time.sleep(0.2)

            # Intent should be processed via idle tick
            assert executor.state.last_intent is not None
            assert executor.state.last_intent.bid_yes.px_yes == 42

        finally:
            executor.stop(timeout=2.0)
