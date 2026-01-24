"""
Strategy Engine for market-making.

Stateless quote generator that outputs DesiredQuoteSet in YES-space.
Strategy runs at fixed Hz with debouncing via IntentMailbox.

Key design:
- Strategy is STATELESS: all state comes from StrategyInput
- Output is always in YES-space: Executor handles materialization
- IntentMailbox coalesces intents: only latest is kept
"""

import logging
import queue
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional, Callable

from .mm_types import (
    PMBookSnapshot,
    BNSnapshot,
    InventoryState,
    QuoteMode,
    DesiredQuoteSet,
    DesiredQuoteLeg,
    now_ms,
)

logger = logging.getLogger(__name__)


# =============================================================================
# STRATEGY INPUT
# =============================================================================


@dataclass(slots=True)
class StrategyInput:
    """
    All inputs needed by strategy to compute quotes.

    Strategy is stateless - all state comes through this.
    """
    # Market data
    pm_book: Optional[PMBookSnapshot]
    bn_snap: Optional[BNSnapshot]

    # Inventory
    inventory: InventoryState

    # Fair value (from pricer)
    fair_px_cents: Optional[int]  # Fair price in cents (0-100)

    # Time context
    t_remaining_ms: int  # Time until market close

    # Feed sequences for freshness
    pm_seq: int = 0
    bn_seq: int = 0

    @property
    def is_data_valid(self) -> bool:
        """Check if we have minimum data needed."""
        return self.pm_book is not None and self.fair_px_cents is not None

    @property
    def near_expiry(self) -> bool:
        """Check if we're near market expiry (last 5 minutes)."""
        return self.t_remaining_ms < 300_000

    @property
    def very_near_expiry(self) -> bool:
        """Check if we're very close to expiry (last 1 minute)."""
        return self.t_remaining_ms < 60_000


@dataclass(slots=True)
class StrategyConfig:
    """Configuration for strategy parameters."""
    # Spread parameters (cents)
    base_spread_cents: int = 3  # Base bid-ask spread
    min_spread_cents: int = 2   # Minimum spread
    max_spread_cents: int = 10  # Maximum spread

    # Position skew
    skew_per_share_cents: float = 0.01  # Cents of skew per share of net position
    max_skew_cents: int = 5  # Max position skew

    # Sizing
    base_size: int = 25  # Base order size in shares
    min_size: int = 10   # Minimum order size
    max_size: int = 100  # Maximum order size

    # Volatility adjustments
    vol_spread_multiplier: float = 1.5  # Spread multiplier in high vol
    high_vol_threshold: float = 0.02  # Z-score threshold for high vol

    # Position limits
    max_position: int = 500  # Maximum position per side

    # Expiry behavior
    expiry_spread_multiplier: float = 0.5  # Tighter spreads near expiry
    expiry_size_reduction: float = 0.5     # Reduce size near expiry


# =============================================================================
# STRATEGY BASE CLASS
# =============================================================================


class Strategy(ABC):
    """
    Abstract base class for market-making strategies.

    Strategies are STATELESS - all inputs come via StrategyInput.
    Output is always a DesiredQuoteSet in YES-space.
    """

    @abstractmethod
    def compute_quotes(self, inp: StrategyInput) -> DesiredQuoteSet:
        """
        Compute desired quotes from input.

        Args:
            inp: Current market state and parameters

        Returns:
            DesiredQuoteSet in YES-space
        """
        pass


# =============================================================================
# DEFAULT MM STRATEGY
# =============================================================================


class DefaultMMStrategy(Strategy):
    """
    Default market-making strategy.

    Features:
    - Mid-market quoting around fair value
    - Position-based skew
    - Volatility-aware spread adjustment
    - Near-expiry behavior modification
    """

    def __init__(self, config: Optional[StrategyConfig] = None):
        self.config = config or StrategyConfig()

    def compute_quotes(self, inp: StrategyInput) -> DesiredQuoteSet:
        """Compute desired quotes."""
        ts = now_ms()

        # Check data validity
        if not inp.is_data_valid:
            return DesiredQuoteSet.stop(
                ts=ts,
                pm_seq=inp.pm_seq,
                bn_seq=inp.bn_seq,
                reason="NO_DATA",
            )

        # Very near expiry - stop quoting
        if inp.very_near_expiry:
            return DesiredQuoteSet.stop(
                ts=ts,
                pm_seq=inp.pm_seq,
                bn_seq=inp.bn_seq,
                reason="EXPIRY_IMMINENT",
            )

        # Compute spread
        spread = self._compute_spread(inp)
        half_spread = spread // 2

        # Compute skew based on position
        skew = self._compute_skew(inp)

        # Compute fair price with skew
        fair = inp.fair_px_cents
        mid_with_skew = fair - skew  # Positive skew pushes quotes down

        # Compute bid and ask prices
        bid_px = max(1, mid_with_skew - half_spread)
        ask_px = min(99, mid_with_skew + half_spread)

        # Ensure bid < ask
        if bid_px >= ask_px:
            ask_px = min(99, bid_px + 1)

        # Compute sizes
        bid_sz, ask_sz = self._compute_sizes(inp)

        # Determine mode
        mode = self._determine_mode(inp)

        # Build intent
        reason_flags = set()
        if inp.near_expiry:
            reason_flags.add("NEAR_EXPIRY")
        if abs(skew) > 0:
            reason_flags.add("POSITION_SKEW")

        bid_enabled = mode in (QuoteMode.NORMAL, QuoteMode.ONE_SIDED_BUY)
        ask_enabled = mode in (QuoteMode.NORMAL, QuoteMode.ONE_SIDED_SELL)

        return DesiredQuoteSet(
            created_at_ts=ts,
            pm_seq=inp.pm_seq,
            bn_seq=inp.bn_seq,
            mode=mode,
            bid_yes=DesiredQuoteLeg(
                enabled=bid_enabled and bid_sz > 0,
                px_yes=bid_px,
                sz=bid_sz,
            ),
            ask_yes=DesiredQuoteLeg(
                enabled=ask_enabled and ask_sz > 0,
                px_yes=ask_px,
                sz=ask_sz,
            ),
            reason_flags=reason_flags,
        )

    def _compute_spread(self, inp: StrategyInput) -> int:
        """Compute bid-ask spread based on conditions."""
        cfg = self.config
        spread = cfg.base_spread_cents

        # Volatility adjustment
        if inp.bn_snap and inp.bn_snap.shock_z is not None:
            if abs(inp.bn_snap.shock_z) > cfg.high_vol_threshold:
                spread = int(spread * cfg.vol_spread_multiplier)

        # Near expiry - tighter spreads
        if inp.near_expiry:
            spread = int(spread * cfg.expiry_spread_multiplier)

        # Clamp to bounds
        return max(cfg.min_spread_cents, min(cfg.max_spread_cents, spread))

    def _compute_skew(self, inp: StrategyInput) -> int:
        """Compute position-based skew (cents)."""
        cfg = self.config
        net_pos = inp.inventory.net_E

        # Skew per share of net position
        skew = int(net_pos * cfg.skew_per_share_cents)

        # Clamp to max skew
        return max(-cfg.max_skew_cents, min(cfg.max_skew_cents, skew))

    def _compute_sizes(self, inp: StrategyInput) -> tuple[int, int]:
        """Compute bid and ask sizes."""
        cfg = self.config
        base = cfg.base_size

        # Near expiry - reduce size
        if inp.near_expiry:
            base = int(base * cfg.expiry_size_reduction)

        # Position-based size adjustment
        net_pos = inp.inventory.net_E

        # Reduce bid size if already long
        if net_pos > 0:
            bid_reduction = min(1.0, net_pos / cfg.max_position)
            bid_sz = int(base * (1 - bid_reduction * 0.5))
        else:
            bid_sz = base

        # Reduce ask size if already short
        if net_pos < 0:
            ask_reduction = min(1.0, -net_pos / cfg.max_position)
            ask_sz = int(base * (1 - ask_reduction * 0.5))
        else:
            ask_sz = base

        # Clamp to bounds
        bid_sz = max(cfg.min_size, min(cfg.max_size, bid_sz))
        ask_sz = max(cfg.min_size, min(cfg.max_size, ask_sz))

        return bid_sz, ask_sz

    def _determine_mode(self, inp: StrategyInput) -> QuoteMode:
        """Determine quoting mode based on conditions."""
        cfg = self.config
        net_pos = inp.inventory.net_E

        # At max long position - only sell
        if net_pos >= cfg.max_position:
            return QuoteMode.ONE_SIDED_SELL

        # At max short position - only buy
        if net_pos <= -cfg.max_position:
            return QuoteMode.ONE_SIDED_BUY

        return QuoteMode.NORMAL


# =============================================================================
# INTENT MAILBOX
# =============================================================================


class IntentMailbox:
    """
    Single-slot overwrite mailbox for strategy intents.

    Only keeps the latest intent - older intents are discarded.
    This provides natural coalescing when Executor is slow.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._intent: Optional[DesiredQuoteSet] = None
        self._has_new = False

    def put(self, intent: DesiredQuoteSet) -> None:
        """Put new intent (overwrites any existing)."""
        with self._lock:
            self._intent = intent
            self._has_new = True

    def get(self) -> Optional[DesiredQuoteSet]:
        """Get latest intent if available (clears mailbox)."""
        with self._lock:
            if not self._has_new:
                return None
            intent = self._intent
            self._has_new = False
            return intent

    def peek(self) -> Optional[DesiredQuoteSet]:
        """Peek at latest intent without clearing."""
        with self._lock:
            return self._intent


# =============================================================================
# STRATEGY RUNNER
# =============================================================================


class StrategyRunner:
    """
    Runs strategy at fixed Hz with debouncing.

    Produces intents that get put into IntentMailbox.
    Coalescing happens naturally if Executor can't keep up.
    """

    def __init__(
        self,
        strategy: Strategy,
        mailbox: IntentMailbox,
        get_input: Callable[[], StrategyInput],
        tick_hz: int = 20,  # Strategy tick rate
    ):
        """
        Initialize strategy runner.

        Args:
            strategy: Strategy instance
            mailbox: IntentMailbox to put intents into
            get_input: Callback to get current StrategyInput
            tick_hz: How many times per second to run strategy
        """
        self._strategy = strategy
        self._mailbox = mailbox
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
        """Stop the strategy runner thread."""
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

            # Put into mailbox
            self._mailbox.put(intent)
            self._last_intent = intent
            self._intents_produced += 1

        except Exception as e:
            logger.error(f"Strategy tick error: {e}")

    def _should_publish(self, intent: DesiredQuoteSet) -> bool:
        """Check if intent changed enough to publish."""
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
