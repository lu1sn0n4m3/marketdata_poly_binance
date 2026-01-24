"""
Strategy base classes and input types.

This module defines:
- StrategyInput: All inputs needed by a strategy (market data, inventory, etc.)
- StrategyConfig: Tunable parameters for strategy behavior
- Strategy: Abstract base class that all strategies must implement
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional

from ..types import (
    PolymarketBookSnapshot,
    BinanceSnapshot,
    InventoryState,
    DesiredQuoteSet,
)


@dataclass(slots=True)
class StrategyInput:
    """
    All inputs needed by strategy to compute quotes.

    Strategy is STATELESS - all state comes through this dataclass.
    The StrategyRunner is responsible for assembling this from various
    data sources (caches, pricer, etc.).

    Attributes:
        pm_book: Polymarket order book snapshot (YES and NO tokens)
        bn_snap: Binance market data snapshot (for fair value)
        inventory: Current position state (YES and NO holdings)
        fair_px_cents: Fair price in cents (0-100), from pricer
        t_remaining_ms: Time until market closes
        pm_seq: Polymarket feed sequence number (for staleness detection)
        bn_seq: Binance feed sequence number (for staleness detection)
    """
    # Market data
    pm_book: Optional[PolymarketBookSnapshot]
    bn_snap: Optional[BinanceSnapshot]

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
        """Check if we have minimum data needed to quote."""
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
    """
    Configuration for strategy parameters.

    All values can be tuned to adjust strategy behavior.
    The DefaultMMStrategy uses these; custom strategies may
    define their own config classes.

    Spread Parameters:
        base_spread_cents: Starting bid-ask spread
        min_spread_cents: Floor for spread (won't go tighter)
        max_spread_cents: Ceiling for spread (won't go wider)

    Position Skew:
        skew_per_share_cents: How much to skew quotes per share of position
        max_skew_cents: Maximum allowed skew

    Sizing:
        base_size: Default order size
        min_size: Minimum order size (Polymarket minimum is 5)
        max_size: Maximum order size

    Volatility:
        vol_spread_multiplier: Widen spread by this factor in high vol
        high_vol_threshold: Z-score threshold for "high volatility"

    Position Limits:
        max_position: Maximum allowed position per side

    Expiry Behavior:
        expiry_spread_multiplier: Tighten spreads near expiry (< 1.0)
        expiry_size_reduction: Reduce size near expiry (< 1.0)
    """
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


class Strategy(ABC):
    """
    Abstract base class for market-making strategies.

    Strategies are STATELESS - all inputs come via StrategyInput.
    Output is always a DesiredQuoteSet in YES-space.

    The key insight is that strategies don't need to track orders or
    positions themselves. They simply look at the current state and
    decide what quotes they WANT. The Executor handles reconciling
    these desires with reality (existing orders, fills, etc.).

    Implementing a Strategy:
        1. Subclass Strategy
        2. Implement compute_quotes(inp: StrategyInput) -> DesiredQuoteSet
        3. Return quotes in YES-space (bid_yes, ask_yes)

    Example:
        class MyStrategy(Strategy):
            def compute_quotes(self, inp: StrategyInput) -> DesiredQuoteSet:
                if not inp.is_data_valid:
                    return DesiredQuoteSet.stop(ts=now_ms(), reason="NO_DATA")

                # Your logic here...
                return DesiredQuoteSet(
                    created_at_ts=now_ms(),
                    mode=QuoteMode.NORMAL,
                    bid_yes=DesiredQuoteLeg(enabled=True, px_yes=45, sz=10),
                    ask_yes=DesiredQuoteLeg(enabled=True, px_yes=55, sz=10),
                )
    """

    @abstractmethod
    def compute_quotes(self, inp: StrategyInput) -> DesiredQuoteSet:
        """
        Compute desired quotes from input.

        This is called at a fixed rate (default 20Hz) by the StrategyRunner.
        The output is placed in an IntentMailbox for the Executor to consume.

        Args:
            inp: Current market state and parameters

        Returns:
            DesiredQuoteSet in YES-space. The Executor handles:
            - Materializing YES-space quotes to actual orders
            - Deciding whether to use YES or NO token
            - Managing order lifecycle
        """
        pass
