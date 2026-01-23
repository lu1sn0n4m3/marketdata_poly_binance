"""Strategy interface and implementations for Container B."""

import logging
from abc import ABC, abstractmethod
from typing import Optional

from .types import (
    DecisionContext, StrategyIntent, DesiredOrder, QuoteSet,
    Side, OrderPurpose, BinanceSnapshot,
)

try:
    from shared.hourmm_common.util import round_to_tick
except ImportError:
    import sys
    sys.path.insert(0, "/Users/luissafar/dev/marketdata_poly_binance")
    from shared.hourmm_common.util import round_to_tick

logger = logging.getLogger(__name__)


class Strategy(ABC):
    """
    Abstract base class for strategies.
    
    PLUGGABLE interface - different strategies can be swapped.
    """
    
    @property
    @abstractmethod
    def name(self) -> str:
        """Strategy name."""
        ...
    
    @property
    @abstractmethod
    def enabled(self) -> bool:
        """Whether strategy is enabled."""
        ...
    
    @abstractmethod
    def on_tick(self, ctx: DecisionContext) -> StrategyIntent:
        """
        Called on each decision tick.
        
        Args:
            ctx: DecisionContext with all relevant information
        
        Returns:
            StrategyIntent with desired actions
        """
        ...
    
    def on_session_start(self, ctx: DecisionContext) -> None:
        """Called at session start. Default is no-op."""
        pass
    
    def on_session_end(self, ctx: DecisionContext) -> None:
        """Called at session end. Default is no-op."""
        pass


class OpportunisticQuoteStrategy(Strategy):
    """
    Opportunistic quoting strategy.
    
    Posts quotes when edge exists, with inventory-aware skewing.
    
    Features:
    - Only quotes when fair price has meaningful edge vs market
    - Skews quotes based on inventory to mean-revert
    - Reduces size as hour end approaches
    """
    
    def __init__(
        self,
        min_edge_ticks: int = 2,
        base_spread_ticks: int = 3,
        base_size: float = 10.0,
        max_inventory_skew: float = 0.05,  # Max price adjustment for inventory
    ):
        """
        Initialize the strategy.
        
        Args:
            min_edge_ticks: Minimum edge in ticks to quote
            base_spread_ticks: Base spread in ticks from fair
            base_size: Base order size
            max_inventory_skew: Maximum inventory-based price skew
        """
        self._min_edge_ticks = min_edge_ticks
        self._base_spread_ticks = base_spread_ticks
        self._base_size = base_size
        self._max_inventory_skew = max_inventory_skew
        self._enabled = True
    
    @property
    def name(self) -> str:
        return "OpportunisticQuote"
    
    @property
    def enabled(self) -> bool:
        return self._enabled
    
    def on_tick(self, ctx: DecisionContext) -> StrategyIntent:
        """Generate strategy intent for this tick."""
        intent = StrategyIntent()
        
        state = ctx.state_view
        snapshot = ctx.snapshot
        
        # Need valid snapshot and market data
        if not snapshot or snapshot.p_yes_fair is None:
            return intent
        
        if state.market_view.best_bid is None or state.market_view.best_ask is None:
            return intent
        
        # Get fair price and market prices
        fair = snapshot.p_yes_fair
        market_bid = state.market_view.best_bid
        market_ask = state.market_view.best_ask
        tick_size = state.market_view.tick_size
        
        # Get token ID (YES token)
        token_id = ""
        if state.token_ids:
            token_id = state.token_ids.yes_token_id
        
        if not token_id:
            return intent
        
        # Compute edge
        edge_to_bid = fair - market_bid  # Positive means fair above bid (good to buy)
        edge_to_ask = market_ask - fair  # Positive means ask above fair (good to sell)
        
        min_edge = self._min_edge_ticks * tick_size
        
        # Compute inventory skew
        inventory_skew = self._compute_inventory_skew(
            state.positions.net_exposure,
            ctx.t_remaining_ms,
        )
        
        # Compute size (decay with time)
        size = self._compute_size(ctx.t_remaining_ms)
        
        # Compute expiration
        expires_at_ms = ctx.now_ms + 5000  # 5 second TTL
        
        # Build bid quote
        if edge_to_bid > min_edge:
            bid_price = round_to_tick(
                fair - self._base_spread_ticks * tick_size - inventory_skew,
                tick_size
            )
            bid_price = max(0.01, min(0.99, bid_price))
            
            intent.quotes.bid = DesiredOrder(
                side=Side.BUY,
                price=bid_price,
                size=size,
                purpose=OrderPurpose.QUOTE,
                expires_at_ms=expires_at_ms,
                token_id=token_id,
            )
        
        # Build ask quote
        if edge_to_ask > min_edge:
            ask_price = round_to_tick(
                fair + self._base_spread_ticks * tick_size - inventory_skew,
                tick_size
            )
            ask_price = max(0.01, min(0.99, ask_price))
            
            intent.quotes.ask = DesiredOrder(
                side=Side.SELL,
                price=ask_price,
                size=size,
                purpose=OrderPurpose.QUOTE,
                expires_at_ms=expires_at_ms,
                token_id=token_id,
            )
        
        return intent
    
    def _compute_inventory_skew(
        self,
        net_exposure: float,
        t_remaining_ms: int,
    ) -> float:
        """
        Compute price skew based on inventory.
        
        Positive inventory -> skew quotes down (encourage selling)
        Negative inventory -> skew quotes up (encourage buying)
        """
        hour_ms = 60 * 60 * 1000
        
        # Scale skew with urgency (more skew as time runs out)
        time_factor = 1.0 - (t_remaining_ms / hour_ms)
        time_factor = max(0.0, min(1.0, time_factor))
        
        # Normalize inventory
        inventory_norm = net_exposure / max(100.0, abs(net_exposure) + 100.0)
        
        # Skew: positive inventory -> negative skew (lower prices)
        skew = -inventory_norm * self._max_inventory_skew * (1.0 + time_factor)
        
        return skew
    
    def _compute_size(self, t_remaining_ms: int) -> float:
        """Compute order size based on time remaining."""
        hour_ms = 60 * 60 * 1000
        
        # Decay size as hour end approaches
        time_factor = t_remaining_ms / hour_ms
        time_factor = max(0.1, min(1.0, time_factor))
        
        return self._base_size * time_factor
    
    def on_session_start(self, ctx: DecisionContext) -> None:
        """Reset state at session start."""
        logger.info(f"Strategy {self.name}: Session started")
    
    def on_session_end(self, ctx: DecisionContext) -> None:
        """Clean up at session end."""
        logger.info(f"Strategy {self.name}: Session ended")
