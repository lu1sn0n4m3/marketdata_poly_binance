"""Quote composer for Container B."""

import logging
from typing import Optional

from .types import (
    DecisionContext, DesiredOrder, QuoteSet, Side, OrderPurpose,
)

try:
    from shared.hourmm_common.util import round_to_tick
except ImportError:
    import sys
    sys.path.insert(0, "/Users/luissafar/dev/marketdata_poly_binance")
    from shared.hourmm_common.util import round_to_tick

logger = logging.getLogger(__name__)


class QuoteComposer:
    """
    Turns fair price + spread/skew into candidate quotes.
    
    Concrete helper class used by strategies.
    """
    
    def __init__(
        self,
        min_spread_ticks: int = 2,
        replace_min_ticks: int = 2,
        max_quotes_per_side: int = 1,
    ):
        """
        Initialize the composer.
        
        Args:
            min_spread_ticks: Minimum spread from fair in ticks
            replace_min_ticks: Minimum tick movement to trigger replace
            max_quotes_per_side: Maximum quotes per side (typically 1)
        """
        self.min_spread_ticks = min_spread_ticks
        self.replace_min_ticks = replace_min_ticks
        self.max_quotes_per_side = max_quotes_per_side
    
    def compose(
        self,
        fair: float,
        ctx: DecisionContext,
        size: float = 10.0,
        token_id: str = "",
    ) -> QuoteSet:
        """
        Compose quotes around fair price.
        
        Args:
            fair: Fair price/probability
            ctx: DecisionContext for market info
            size: Order size
            token_id: Token ID to quote
        
        Returns:
            QuoteSet with bid and ask quotes
        """
        quotes = QuoteSet()
        
        tick_size = ctx.state_view.market_view.tick_size
        spread_in_price = self.min_spread_ticks * tick_size
        
        # Compute expiration
        expires_at_ms = ctx.now_ms + 5000  # 5 second default
        
        # Bid quote
        bid_price = self.round_to_tick(fair - spread_in_price, tick_size)
        bid_price = max(0.01, min(0.99, bid_price))
        
        quotes.bid = DesiredOrder(
            side=Side.BUY,
            price=bid_price,
            size=size,
            purpose=OrderPurpose.QUOTE,
            expires_at_ms=expires_at_ms,
            token_id=token_id,
        )
        
        # Ask quote
        ask_price = self.round_to_tick(fair + spread_in_price, tick_size)
        ask_price = max(0.01, min(0.99, ask_price))
        
        quotes.ask = DesiredOrder(
            side=Side.SELL,
            price=ask_price,
            size=size,
            purpose=OrderPurpose.QUOTE,
            expires_at_ms=expires_at_ms,
            token_id=token_id,
        )
        
        return quotes
    
    @staticmethod
    def round_to_tick(price: float, tick_size: float) -> float:
        """Round price to nearest tick."""
        return round_to_tick(price, tick_size)
    
    def with_inventory_skew(
        self,
        quotes: QuoteSet,
        inventory: float,
        t_remaining_ms: int,
        max_skew: float = 0.05,
    ) -> QuoteSet:
        """
        Apply inventory skew to quotes.
        
        Positive inventory -> skew down (encourage selling)
        Negative inventory -> skew up (encourage buying)
        
        Args:
            quotes: Original quotes
            inventory: Current net inventory
            t_remaining_ms: Time remaining in hour
            max_skew: Maximum skew amount
        
        Returns:
            Skewed QuoteSet
        """
        hour_ms = 60 * 60 * 1000
        
        # Urgency factor (more urgent as time runs out)
        urgency = 1.0 - (t_remaining_ms / hour_ms)
        urgency = max(0.0, min(1.0, urgency))
        
        # Normalize inventory
        inv_norm = inventory / max(100.0, abs(inventory) + 100.0)
        
        # Compute skew: positive inv -> negative skew (lower prices)
        skew = -inv_norm * max_skew * (1.0 + urgency)
        
        tick_size = 0.01  # Default, should come from context
        
        skewed = QuoteSet()
        
        if quotes.bid:
            new_bid_price = self.round_to_tick(quotes.bid.price + skew, tick_size)
            new_bid_price = max(0.01, min(0.99, new_bid_price))
            skewed.bid = DesiredOrder(
                side=quotes.bid.side,
                price=new_bid_price,
                size=quotes.bid.size,
                purpose=quotes.bid.purpose,
                expires_at_ms=quotes.bid.expires_at_ms,
                token_id=quotes.bid.token_id,
            )
        
        if quotes.ask:
            new_ask_price = self.round_to_tick(quotes.ask.price + skew, tick_size)
            new_ask_price = max(0.01, min(0.99, new_ask_price))
            skewed.ask = DesiredOrder(
                side=quotes.ask.side,
                price=new_ask_price,
                size=quotes.ask.size,
                purpose=quotes.ask.purpose,
                expires_at_ms=quotes.ask.expires_at_ms,
                token_id=quotes.ask.token_id,
            )
        
        return skewed
