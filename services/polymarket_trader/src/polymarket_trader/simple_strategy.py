"""
Simple Strategy Interface for Polymarket Trading

This is the SIMPLIFIED interface for implementing trading strategies.
The system handles all the complexity - you just output target quotes.

HOW IT WORKS:
=============
1. Your strategy receives current state (position, prices, etc.)
2. You return target quotes: "I want bid at X, ask at Y"
3. The executor handles placing/cancelling to match your targets
4. If orders get filled, you see updated position next tick

THAT'S IT. No order tracking, no state management, just:
  Input:  Current state (position, market prices, fair price)
  Output: Target quotes (bid price/size, ask price/size)

EXAMPLE STRATEGY:
=================
```python
class MyStrategy(SimpleStrategy):
    def compute_quotes(self, ctx: StrategyContext) -> TargetQuotes:
        fair = ctx.fair_price
        pos = ctx.position
        
        # Simple market making: quote around fair price
        bid = fair - 0.02
        ask = fair + 0.02
        
        # Skew based on position (if long, lower bid to reduce)
        if pos > 10:
            bid -= 0.01
        elif pos < -10:
            ask += 0.01
        
        return TargetQuotes(
            bid_price=bid,
            bid_size=10.0,
            ask_price=ask,
            ask_size=10.0,
        )
```
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional
import logging

logger = logging.getLogger(__name__)


# =============================================================================
# SIMPLE DATA TYPES
# =============================================================================

@dataclass
class StrategyContext:
    """
    Everything your strategy needs to make a decision.

    All values are READ-ONLY and reflect the CURRENT state.
    Updated by WebSocket in real-time.
    """
    # Time
    now_ms: int                    # Current timestamp (ms)
    t_remaining_ms: int            # Time until hour end (ms)

    # Your position (updated when fills happen via WS)
    position: float                # Net position in YES tokens (+ = long, - = short)

    # Market prices (from Polymarket WS) - legacy combined fields
    market_bid: Optional[float]    # Best bid on Polymarket (legacy, may mix YES/NO)
    market_ask: Optional[float]    # Best ask on Polymarket (legacy, may mix YES/NO)
    tick_size: float               # Minimum price increment (usually 0.01)

    # Separate YES/NO token market prices (recommended)
    yes_market_bid: Optional[float] = None  # Best bid for YES token
    yes_market_ask: Optional[float] = None  # Best ask for YES token
    no_market_bid: Optional[float] = None   # Best bid for NO token
    no_market_ask: Optional[float] = None   # Best ask for NO token

    # Fair price (from Binance pricer)
    fair_price: Optional[float] = None    # Model's fair value for YES token
    btc_price: Optional[float] = None     # Current BTC price

    # Your current open orders (from WS)
    open_bid_price: Optional[float] = None   # Your current bid price (if any)
    open_bid_size: float = 0.0               # Your current bid size
    open_ask_price: Optional[float] = None   # Your current ask price (if any)
    open_ask_size: float = 0.0               # Your current ask size

    # Token info
    yes_token_id: str = ""              # Token ID for YES side
    no_token_id: str = ""               # Token ID for NO side

    # Separate YES/NO positions (for position-aware execution)
    yes_position: float = 0.0           # YES token position (positive = holding YES)
    no_position: float = 0.0            # NO token position (positive = holding NO)


@dataclass
class TargetQuotes:
    """
    Your strategy's desired quotes.

    Supports multiple orders per side via dicts: {price: size}
    The executor will:
      - Cancel any orders NOT in the target dicts
      - Place new orders to match the targets
      - Keep existing orders that match

    Example:
        TargetQuotes(
            bids={0.45: 10, 0.44: 5},   # Two bids at different prices
            asks={0.55: 10, 0.56: 5},   # Two asks at different prices
        )
    """
    bids: dict[float, float] = field(default_factory=dict)  # {price: size}
    asks: dict[float, float] = field(default_factory=dict)  # {price: size}

    def __post_init__(self):
        """Validate prices are in valid range."""
        self.bids = {
            max(0.01, min(0.99, p)): s
            for p, s in self.bids.items() if s > 0
        }
        self.asks = {
            max(0.01, min(0.99, p)): s
            for p, s in self.asks.items() if s > 0
        }

    # Convenience properties for single-order strategies
    @property
    def bid_price(self) -> Optional[float]:
        """First bid price (for single-order compatibility)."""
        return next(iter(self.bids.keys()), None) if self.bids else None

    @property
    def bid_size(self) -> float:
        """First bid size (for single-order compatibility)."""
        return next(iter(self.bids.values()), 0.0) if self.bids else 0.0

    @property
    def ask_price(self) -> Optional[float]:
        """First ask price (for single-order compatibility)."""
        return next(iter(self.asks.keys()), None) if self.asks else None

    @property
    def ask_size(self) -> float:
        """First ask size (for single-order compatibility)."""
        return next(iter(self.asks.values()), 0.0) if self.asks else 0.0

    @classmethod
    def single(
        cls,
        bid_price: Optional[float] = None,
        bid_size: float = 0.0,
        ask_price: Optional[float] = None,
        ask_size: float = 0.0,
    ) -> "TargetQuotes":
        """Create TargetQuotes with single bid/ask (convenience constructor)."""
        bids = {bid_price: bid_size} if bid_price and bid_size > 0 else {}
        asks = {ask_price: ask_size} if ask_price and ask_size > 0 else {}
        return cls(bids=bids, asks=asks)


# =============================================================================
# SIMPLE STRATEGY BASE CLASS
# =============================================================================

class SimpleStrategy(ABC):
    """
    Base class for simple trading strategies.
    
    IMPLEMENT THIS:
        compute_quotes(ctx) -> TargetQuotes
    
    THAT'S ALL YOU NEED TO DO.
    
    The framework handles:
        - Order placement/cancellation
        - Position tracking (via WS fills)
        - Error handling
        - Race conditions (trusts WS for truth)
    """
    
    @property
    def name(self) -> str:
        """Strategy name for logging."""
        return self.__class__.__name__
    
    @abstractmethod
    def compute_quotes(self, ctx: StrategyContext) -> TargetQuotes:
        """
        Compute your target quotes given current state.
        
        This is called every tick (e.g., 2 Hz = every 500ms).
        
        Args:
            ctx: Current market state, position, prices
        
        Returns:
            TargetQuotes with your desired bid/ask
        
        Tips:
            - Return None for bid_price/ask_price to not quote that side
            - Your position updates automatically when fills happen
            - You see fills reflected in ctx.position next tick
            - Don't worry about order IDs or state - just output targets
        """
        ...
    
    def on_fill(self, side: str, price: float, size: float) -> None:
        """
        Called when one of your orders gets filled.
        
        Override this for custom fill handling (logging, etc.)
        Default: just logs the fill.
        
        Args:
            side: "BUY" or "SELL"
            price: Fill price
            size: Fill size
        """
        logger.info(f"{self.name}: FILL {side} {size:.1f} @ ${price:.2f}")
    
    def on_session_start(self) -> None:
        """Called when trading session starts. Override for setup."""
        logger.info(f"{self.name}: Session started")
    
    def on_session_end(self) -> None:
        """Called when trading session ends. Override for cleanup."""
        logger.info(f"{self.name}: Session ended")


# =============================================================================
# EXAMPLE STRATEGIES
# =============================================================================

class SimpleMarketMaker(SimpleStrategy):
    """
    Example: Simple market making strategy.
    
    - Quotes bid/ask around fair price
    - Widens spread when position is large
    - Reduces size near hour end
    """
    
    def __init__(
        self,
        spread: float = 0.02,      # Base spread from fair (each side)
        size: float = 10.0,        # Base order size
        max_position: float = 50.0, # Max position before widening
    ):
        self.spread = spread
        self.size = size
        self.max_position = max_position
    
    def compute_quotes(self, ctx: StrategyContext) -> TargetQuotes:
        # Need fair price to quote
        if ctx.fair_price is None:
            return TargetQuotes()  # No quotes
        
        fair = ctx.fair_price
        pos = ctx.position
        
        # Base quotes around fair
        bid = fair - self.spread
        ask = fair + self.spread
        
        # Widen spread if position is large (to mean-revert)
        if abs(pos) > self.max_position * 0.5:
            extra_spread = 0.01 * (abs(pos) / self.max_position)
            if pos > 0:  # Long: lower bid to reduce
                bid -= extra_spread
            else:        # Short: raise ask to reduce
                ask += extra_spread
        
        # Reduce size near hour end
        size = self.size
        if ctx.t_remaining_ms < 5 * 60 * 1000:  # Last 5 minutes
            size = self.size * 0.5
        
        # Don't quote if position too large
        bid_size = size if pos < self.max_position else 0
        ask_size = size if pos > -self.max_position else 0

        bids = {bid: bid_size} if bid_size > 0 else {}
        asks = {ask: ask_size} if ask_size > 0 else {}

        return TargetQuotes(bids=bids, asks=asks)


class FixedQuoteStrategy(SimpleStrategy):
    """
    Example: Fixed price strategy for testing.
    
    Always quotes at fixed prices - useful for testing the system.
    """
    
    def __init__(
        self,
        bid_price: float = 0.01,
        ask_price: float = 0.99,
        size: float = 5.0,
    ):
        self.bid_price = bid_price
        self.ask_price = ask_price
        self.size = size
    
    def compute_quotes(self, ctx: StrategyContext) -> TargetQuotes:
        return TargetQuotes(
            bids={self.bid_price: self.size},
            asks={self.ask_price: self.size},
        )


class EdgeBasedStrategy(SimpleStrategy):
    """
    Example: Only quote when there's edge vs market.
    
    - Only bids if fair > market_bid + min_edge
    - Only asks if fair < market_ask - min_edge
    """
    
    def __init__(
        self,
        min_edge: float = 0.02,    # Minimum edge to quote
        spread: float = 0.01,      # Spread from fair
        size: float = 10.0,
    ):
        self.min_edge = min_edge
        self.spread = spread
        self.size = size
    
    def compute_quotes(self, ctx: StrategyContext) -> TargetQuotes:
        if ctx.fair_price is None or ctx.market_bid is None or ctx.market_ask is None:
            return TargetQuotes()
        
        fair = ctx.fair_price
        
        # Only bid if we have edge
        bids = {}
        bid_edge = fair - ctx.market_bid
        if bid_edge > self.min_edge:
            bids[fair - self.spread] = self.size

        # Only ask if we have edge
        asks = {}
        ask_edge = ctx.market_ask - fair
        if ask_edge > self.min_edge:
            asks[fair + self.spread] = self.size

        return TargetQuotes(bids=bids, asks=asks)
