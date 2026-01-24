"""
Dummy Strategy for Live Integration Testing.

Places safe orders that will never be filled:
- YES bid at 0.01 (1 cent) for 5 shares
- NO bid at 0.01 (1 cent) for 5 shares

These prices are so far from market that they test the full system
plumbing without risk of actual fills.
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


class DummyStrategy(Strategy):
    """
    Dummy strategy that places safe 1-cent bids for testing.

    Always outputs:
    - YES bid at 1 cent, 5 shares
    - YES ask at 99 cents, 5 shares (materializes as NO bid at 1 cent)

    This tests the full system:
    - PM Market WS (order book updates)
    - PM User WS (order acks, potential fills)
    - Binance poller (fair price streaming)
    - Executor (order materialization)
    - Gateway (order submission)
    - REST client (signing, API calls)
    """

    def __init__(self, size: int = 5):
        """
        Initialize dummy strategy.

        Args:
            size: Order size in shares (default 5)
        """
        self._size = size
        self._log_count = 0

    def compute_quotes(self, inp: StrategyInput) -> DesiredQuoteSet:
        """
        Always return the same safe quotes.

        Returns YES bid at 1 cent and YES ask at 99 cents.
        The executor will materialize these as:
        - BUY YES at 0.01 for 5 shares
        - BUY NO at 0.01 for 5 shares (complement of 99 cent ask)
        """
        ts = now_ms()

        # Log periodically (every 100 ticks ≈ 5 seconds at 20Hz)
        self._log_count += 1
        if self._log_count % 100 == 1:
            self._log_state(inp)

        # Always quote at extreme prices
        return DesiredQuoteSet(
            created_at_ts=ts,
            pm_seq=inp.pm_seq,
            bn_seq=inp.bn_seq,
            mode=QuoteMode.NORMAL,
            bid_yes=DesiredQuoteLeg(
                enabled=True,
                px_yes=1,  # 1 cent bid for YES
                sz=self._size,
            ),
            ask_yes=DesiredQuoteLeg(
                enabled=True,
                px_yes=99,  # 99 cent ask for YES → materializes as 1 cent bid for NO
                sz=self._size,
            ),
            reason_flags={"DUMMY_TEST"},
        )

    def _log_state(self, inp: StrategyInput) -> None:
        """Log current state for debugging."""
        pm_status = "OK" if inp.pm_book else "NO_DATA"
        bn_status = "OK" if inp.bn_snap else "NO_DATA"
        fair = inp.fair_px_cents if inp.fair_px_cents else "N/A"
        t_remain = inp.t_remaining_ms / 1000 if inp.t_remaining_ms else 0
        inv = inp.inventory

        logger.info(
            f"[DummyStrategy] PM={pm_status} BN={bn_status} "
            f"fair={fair}c t_remain={t_remain:.0f}s "
            f"inv(yes={inv.I_yes}, no={inv.I_no}, net={inv.net_E})"
        )
