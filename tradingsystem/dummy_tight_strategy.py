"""
Dummy Tight Strategy for Live Testing.

Simple mean-reversion strategy:
- Flat position: bid 1c below best bid, ask 1c above best ask
- Long YES (got filled on bid): only ask at best ask to exit
- Short YES (got filled on ask): only bid at best bid to exit
- Dust position (< min size): stop quoting, accept dust

This tests:
- Real order placement near market
- Fill handling
- Inventory-based position management
- One-sided quoting
"""

import logging
from .strategy import Strategy, StrategyInput
from .mm_types import (
    DesiredQuoteSet,
    DesiredQuoteLeg,
    QuoteMode,
    now_ms,
)

logger = logging.getLogger(__name__)

# Polymarket minimum order size
MIN_ORDER_SIZE = 5


class DummyTightStrategy(Strategy):
    """
    Tight quoting strategy for live testing.

    Behavior:
    - Flat: Bid at (best_bid - 1), Ask at (best_ask + 1)
    - Long YES (>= min_size): Only ask at best_ask (exit position)
    - Short YES (>= min_size): Only bid at best_bid (exit position)
    - Dust (0 < |position| < min_size): Stop quoting, accept dust

    This creates real fills and tests inventory management.
    """

    def __init__(self, size: int = 5):
        """
        Initialize strategy.

        Args:
            size: Order size in shares (default 5, must be >= MIN_ORDER_SIZE)
        """
        self._size = max(size, MIN_ORDER_SIZE)
        self._log_count = 0

    def compute_quotes(self, inp: StrategyInput) -> DesiredQuoteSet:
        """
        Compute quotes based on inventory and BBO.
        """
        ts = now_ms()

        # Need PM book data
        if not inp.pm_book:
            return DesiredQuoteSet.stop(
                ts=ts,
                pm_seq=inp.pm_seq,
                bn_seq=inp.bn_seq,
                reason="NO_PM_DATA",
            )

        # Get BBO from YES book
        yes_top = inp.pm_book.yes_top
        best_bid = yes_top.best_bid_px
        best_ask = yes_top.best_ask_px

        # Need valid BBO
        if best_bid <= 0 or best_ask >= 100 or best_bid >= best_ask:
            return DesiredQuoteSet.stop(
                ts=ts,
                pm_seq=inp.pm_seq,
                bn_seq=inp.bn_seq,
                reason="INVALID_BBO",
            )

        # Get inventory
        net_pos = inp.inventory.net_E  # Positive = long YES, negative = short YES

        # Log periodically
        self._log_count += 1
        if self._log_count % 100 == 1:
            logger.info(
                f"[DummyTight] BBO={best_bid}/{best_ask} net_pos={net_pos} "
                f"inv(yes={inp.inventory.I_yes}, no={inp.inventory.I_no})"
            )

        # Determine quoting mode based on position
        # Dust positions (< MIN_ORDER_SIZE) are treated as flat since we can't exit them
        abs_pos = abs(net_pos)

        if abs_pos < MIN_ORDER_SIZE:
            # FLAT or DUST: Quote both sides (ignore dust, it will wash out)
            if abs_pos > 0 and self._log_count % 500 == 1:
                logger.debug(f"[DummyTight] Dust position {net_pos}, treating as flat")
            return self._quote_flat(ts, inp, best_bid, best_ask)
        elif net_pos >= MIN_ORDER_SIZE:
            # LONG YES (enough to exit): Only ask to exit
            return self._quote_long(ts, inp, best_bid, best_ask, net_pos)
        else:
            # SHORT YES (enough to exit): Only bid to exit
            return self._quote_short(ts, inp, best_bid, best_ask, net_pos)

    def _quote_flat(
        self, ts: int, inp: StrategyInput, best_bid: int, best_ask: int
    ) -> DesiredQuoteSet:
        """Quote both sides when flat - 1 cent worse than BBO."""
        # Bid 1 cent below best bid
        bid_px = max(1, best_bid)
        # Ask 1 cent above best ask
        ask_px = min(99, best_ask)

        logger.debug(f"[DummyTight] FLAT: bid={bid_px} ask={ask_px}")

        return DesiredQuoteSet(
            created_at_ts=ts,
            pm_seq=inp.pm_seq,
            bn_seq=inp.bn_seq,
            mode=QuoteMode.NORMAL,
            bid_yes=DesiredQuoteLeg(
                enabled=True,
                px_yes=bid_px,
                sz=self._size,
            ),
            ask_yes=DesiredQuoteLeg(
                enabled=True,
                px_yes=ask_px,
                sz=self._size,
            ),
            reason_flags={"FLAT", "TIGHT"},
        )

    def _quote_long(
        self, ts: int, inp: StrategyInput, best_bid: int, best_ask: int, net_pos: int
    ) -> DesiredQuoteSet:
        """Only ask when long YES - trying to exit."""
        # Ask at best ask to get filled
        ask_px = best_ask

        # Always use configured size to meet Polymarket minimum (5 shares)
        # If position < size, we may overshoot and flip to short, which is fine
        ask_sz = self._size

        # Log less frequently
        self._log_count += 1
        if self._log_count % 20 == 1:
            logger.info(f"[DummyTight] LONG {net_pos}: ask only at {ask_px} x {ask_sz}")

        return DesiredQuoteSet(
            created_at_ts=ts,
            pm_seq=inp.pm_seq,
            bn_seq=inp.bn_seq,
            mode=QuoteMode.ONE_SIDED_SELL,
            bid_yes=DesiredQuoteLeg(
                enabled=False,
                px_yes=0,
                sz=0,
            ),
            ask_yes=DesiredQuoteLeg(
                enabled=True,
                px_yes=ask_px,
                sz=ask_sz,
            ),
            reason_flags={"LONG", "EXIT_ONLY"},
        )

    def _quote_short(
        self, ts: int, inp: StrategyInput, best_bid: int, best_ask: int, net_pos: int
    ) -> DesiredQuoteSet:
        """Only bid when short YES (long NO) - trying to exit."""
        # Bid at best bid to get filled
        bid_px = best_bid

        # Always use configured size to meet Polymarket minimum (5 shares)
        # If position < size, we may overshoot and flip to long, which is fine
        bid_sz = self._size

        # Log less frequently
        self._log_count += 1
        if self._log_count % 20 == 1:
            logger.info(f"[DummyTight] SHORT {net_pos}: bid only at {bid_px} x {bid_sz}")

        return DesiredQuoteSet(
            created_at_ts=ts,
            pm_seq=inp.pm_seq,
            bn_seq=inp.bn_seq,
            mode=QuoteMode.ONE_SIDED_BUY,
            bid_yes=DesiredQuoteLeg(
                enabled=True,
                px_yes=bid_px,
                sz=bid_sz,
            ),
            ask_yes=DesiredQuoteLeg(
                enabled=False,
                px_yes=0,
                sz=0,
            ),
            reason_flags={"SHORT", "EXIT_ONLY"},
        )
