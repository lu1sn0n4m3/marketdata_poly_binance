"""
Strategy output types.

Types representing the Strategy's desired quote state.
"""

from dataclasses import dataclass, field

from .core import QuoteMode


@dataclass(slots=True)
class DesiredQuoteLeg:
    """
    A single leg of the desired quote.

    Represents either the bid or ask side in YES space.
    """
    enabled: bool
    px_yes: int  # Price in cents (0-100)
    sz: int      # Size in shares
    skip_min_size: bool = False  # If True, skip min_size checks (for marketable dust cleanup)


@dataclass(slots=True)
class DesiredQuoteSet:
    """
    Complete desired quote state from Strategy.

    Always expressed in canonical YES space.
    Executor handles materialization to real orders.
    """
    created_at_ts: int  # Monotonic ms when created
    pm_seq: int         # PM feed sequence for freshness
    bn_seq: int         # BN feed sequence for freshness
    mode: QuoteMode
    bid_yes: DesiredQuoteLeg  # Synthetic BUY YES
    ask_yes: DesiredQuoteLeg  # Synthetic SELL YES
    reason_flags: set = field(default_factory=set)  # Logging reasons
    book_ts: int = 0    # Monotonic ms when PM book was received (for latency tracking)

    @classmethod
    def stop(cls, ts: int, pm_seq: int = 0, bn_seq: int = 0, reason: str = "STOP", book_ts: int = 0) -> "DesiredQuoteSet":
        """Create a STOP intent."""
        return cls(
            created_at_ts=ts,
            pm_seq=pm_seq,
            bn_seq=bn_seq,
            mode=QuoteMode.STOP,
            bid_yes=DesiredQuoteLeg(enabled=False, px_yes=0, sz=0),
            ask_yes=DesiredQuoteLeg(enabled=False, px_yes=0, sz=0),
            reason_flags={reason},
            book_ts=book_ts,
        )
