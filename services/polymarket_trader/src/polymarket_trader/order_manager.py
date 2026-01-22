"""Order manager for Container B."""

import logging
from time import time_ns
from typing import Optional
import uuid

from .types import (
    DecisionContext, DesiredOrder, OrderState, OrderAction, OrderActionType,
    Side, OrderStatus,
)

try:
    from shared.hourmm_common.util import round_to_tick
except ImportError:
    import sys
    sys.path.insert(0, "/Users/luissafar/dev/marketdata_poly_binance")
    from shared.hourmm_common.util import round_to_tick

logger = logging.getLogger(__name__)


class DesiredOrders:
    """Container for desired order state."""
    
    def __init__(self):
        self.bids: list[DesiredOrder] = []
        self.asks: list[DesiredOrder] = []


class WorkingOrders:
    """Container for working orders."""
    
    def __init__(self):
        self.bids: list[OrderState] = []
        self.asks: list[OrderState] = []


class OrderManager:
    """
    Desired-state reconciliation with minimal churn.
    
    Converts desired quotes into actions (place/cancel/replace)
    while minimizing order churn.
    """
    
    def __init__(
        self,
        max_working_orders: int = 4,
        replace_min_ticks: int = 2,
        replace_min_age_ms: int = 500,
    ):
        """
        Initialize the order manager.
        
        Args:
            max_working_orders: Maximum working orders allowed
            replace_min_ticks: Minimum tick movement to replace
            replace_min_age_ms: Minimum age before replacing
        """
        self.max_working_orders = max_working_orders
        self.replace_min_ticks = replace_min_ticks
        self.replace_min_age_ms = replace_min_age_ms
    
    def reconcile(
        self,
        desired: DesiredOrders,
        working: WorkingOrders,
        ctx: DecisionContext,
    ) -> list[OrderAction]:
        """
        Reconcile desired orders with working orders.
        
        Args:
            desired: Desired order state
            working: Current working orders
            ctx: Decision context
        
        Returns:
            List of actions to take
        """
        actions: list[OrderAction] = []
        tick_size = ctx.state_view.market_view.tick_size
        now_ms = ctx.now_ms
        
        # Reconcile bids
        bid_actions = self._reconcile_side(
            desired.bids, working.bids, tick_size, now_ms, Side.BUY
        )
        actions.extend(bid_actions)
        
        # Reconcile asks
        ask_actions = self._reconcile_side(
            desired.asks, working.asks, tick_size, now_ms, Side.SELL
        )
        actions.extend(ask_actions)
        
        # Enforce max orders limit
        total_orders = len(working.bids) + len(working.asks)
        new_places = sum(1 for a in actions if a.action_type == OrderActionType.PLACE)
        cancels = sum(1 for a in actions if a.action_type == OrderActionType.CANCEL)
        
        if total_orders + new_places - cancels > self.max_working_orders:
            # Remove some place actions
            place_actions = [a for a in actions if a.action_type == OrderActionType.PLACE]
            non_place = [a for a in actions if a.action_type != OrderActionType.PLACE]
            
            excess = (total_orders + new_places - cancels) - self.max_working_orders
            place_actions = place_actions[:-excess] if excess > 0 else place_actions
            
            actions = non_place + place_actions
        
        return actions
    
    def _reconcile_side(
        self,
        desired: list[DesiredOrder],
        working: list[OrderState],
        tick_size: float,
        now_ms: int,
        side: Side,
    ) -> list[OrderAction]:
        """Reconcile one side (bid or ask)."""
        actions: list[OrderAction] = []
        
        # Match desired to working
        used_working = set()
        
        for des in desired:
            # Find matching working order
            matched = None
            for work in working:
                if work.order_id in used_working:
                    continue
                if self._is_match(des, work, tick_size, now_ms):
                    matched = work
                    used_working.add(work.order_id)
                    break
            
            if matched:
                # Check if replace needed
                if self.should_replace(matched, des, tick_size, now_ms):
                    actions.append(OrderAction(
                        action_type=OrderActionType.REPLACE,
                        order=des,
                        order_id=matched.order_id,
                        client_req_id=self._generate_client_id(),
                    ))
                # Else keep existing order
            else:
                # Place new order
                actions.append(OrderAction(
                    action_type=OrderActionType.PLACE,
                    order=des,
                    client_req_id=self._generate_client_id(),
                ))
        
        # Cancel unmatched working orders
        for work in working:
            if work.order_id not in used_working:
                actions.append(OrderAction(
                    action_type=OrderActionType.CANCEL,
                    order_id=work.order_id,
                    client_req_id=self._generate_client_id(),
                ))
        
        return actions
    
    def _is_match(
        self,
        desired: DesiredOrder,
        working: OrderState,
        tick_size: float,
        now_ms: int,
    ) -> bool:
        """Check if desired and working are close enough to be matched."""
        if desired.side != working.side:
            return False
        
        # Price difference in ticks
        price_diff_ticks = abs(desired.price - working.price) / tick_size
        
        # Consider it a match if within replace threshold
        return price_diff_ticks < self.replace_min_ticks * 2
    
    def should_replace(
        self,
        existing: OrderState,
        desired: DesiredOrder,
        tick_size: float,
        now_ms: int,
    ) -> bool:
        """
        Check if an existing order should be replaced.
        
        Args:
            existing: Existing order state
            desired: Desired order
            tick_size: Current tick size
            now_ms: Current timestamp
        
        Returns:
            True if replace is warranted
        """
        # Check minimum age
        order_age_ms = now_ms - existing.created_at_ms
        if order_age_ms < self.replace_min_age_ms:
            return False
        
        # Check price movement
        price_diff_ticks = abs(desired.price - existing.price) / tick_size
        if price_diff_ticks < self.replace_min_ticks:
            return False
        
        # Check size change (only replace if significant)
        size_diff = abs(desired.size - existing.remaining)
        if size_diff / max(desired.size, existing.remaining) < 0.2:
            # Less than 20% size change, don't replace
            if price_diff_ticks < self.replace_min_ticks:
                return False
        
        return True
    
    def build_expiration(self, ctx: DecisionContext, ttl_ms: int = 5000) -> int:
        """
        Build expiration timestamp.
        
        Shortens as hour end approaches.
        
        Args:
            ctx: Decision context
            ttl_ms: Base TTL in milliseconds
        
        Returns:
            Expiration timestamp in milliseconds
        """
        now_ms = ctx.now_ms
        t_remaining = ctx.t_remaining_ms
        
        # Shorten TTL as hour end approaches
        if t_remaining < 60 * 1000:  # Last minute
            ttl_ms = min(ttl_ms, 1000)
        elif t_remaining < 5 * 60 * 1000:  # Last 5 minutes
            ttl_ms = min(ttl_ms, 2000)
        
        return now_ms + ttl_ms
    
    def enforce_tick_and_bounds(
        self,
        desired: DesiredOrders,
        tick_size: float,
    ) -> DesiredOrders:
        """
        Ensure all prices are rounded and within bounds.
        
        Args:
            desired: Desired orders
            tick_size: Current tick size
        
        Returns:
            Adjusted DesiredOrders
        """
        result = DesiredOrders()
        
        for order in desired.bids:
            adjusted = DesiredOrder(
                side=order.side,
                price=max(0.01, min(0.99, round_to_tick(order.price, tick_size))),
                size=order.size,
                purpose=order.purpose,
                expires_at_ms=order.expires_at_ms,
                token_id=order.token_id,
            )
            result.bids.append(adjusted)
        
        for order in desired.asks:
            adjusted = DesiredOrder(
                side=order.side,
                price=max(0.01, min(0.99, round_to_tick(order.price, tick_size))),
                size=order.size,
                purpose=order.purpose,
                expires_at_ms=order.expires_at_ms,
                token_id=order.token_id,
            )
            result.asks.append(adjusted)
        
        return result
    
    def _generate_client_id(self) -> str:
        """Generate unique client request ID."""
        return str(uuid.uuid4())[:16]
