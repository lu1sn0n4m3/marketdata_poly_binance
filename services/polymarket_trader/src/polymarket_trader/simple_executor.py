"""
Simple Executor - Fire and Forget Order Management

This executor:
1. Takes target quotes from strategy
2. Compares with current open orders (from WS state)
3. Fires cancels/places as needed
4. DOES NOT WAIT for results - trusts WS for state updates

Race Condition Handling:
- If cancel fails with "not found" -> Order was filled, WS will tell us
- If place fails -> Log and retry next tick
- Position updates come from WS trade events

The philosophy: DON'T GUESS, TRUST THE WEBSOCKET.
"""

import asyncio
import logging
from time import time_ns
from typing import Optional
from dataclasses import dataclass

from .simple_strategy import TargetQuotes, StrategyContext
from .polymarket_rest import PolymarketRestClient, OrderRequest
from .types import Side

logger = logging.getLogger(__name__)


@dataclass
class OpenOrder:
    """Represents an open order from WS state."""
    order_id: str
    side: Side
    price: float
    size: float


class SimpleExecutor:
    """
    Simple fire-and-forget executor.
    
    Each tick:
    1. Get target quotes from strategy
    2. Get current orders from state
    3. Cancel orders not matching targets
    4. Place orders to match targets
    
    All REST calls are fire-and-forget. We trust WS for truth.
    """
    
    def __init__(
        self,
        rest: PolymarketRestClient,
        yes_token_id: str,
        no_token_id: str = "",
        default_ttl_ms: int = 60_000,
    ):
        """
        Initialize the executor.
        
        Args:
            rest: REST client for order placement
            yes_token_id: Token ID for YES side
            no_token_id: Token ID for NO side (optional)
            default_ttl_ms: Default order TTL in milliseconds
        """
        self.rest = rest
        self.yes_token_id = yes_token_id
        self.no_token_id = no_token_id
        self.default_ttl_ms = default_ttl_ms
        
        # Stats
        self.places_sent = 0
        self.cancels_sent = 0
        self.cancel_failures = 0
        self.place_failures = 0
    
    async def sync_to_targets(
        self,
        targets: TargetQuotes,
        current_orders: dict[str, OpenOrder],
        now_ms: int,
    ) -> dict:
        """
        Sync current orders to match target quotes.
        
        Args:
            targets: Desired quotes from strategy
            current_orders: Current open orders from WS state
            now_ms: Current timestamp
        
        Returns:
            Dict with actions taken for logging
        """
        actions = {
            "cancels": [],
            "places": [],
            "kept": [],
        }
        
        # Separate orders by side
        current_bids = {oid: o for oid, o in current_orders.items() if o.side == Side.BUY}
        current_asks = {oid: o for oid, o in current_orders.items() if o.side == Side.SELL}
        
        # --- Handle BID side ---
        target_bid = targets.bid_price
        target_bid_size = targets.bid_size if target_bid else 0
        
        for oid, order in current_bids.items():
            if target_bid and abs(order.price - target_bid) < 0.001 and order.size >= target_bid_size * 0.9:
                # Order matches target, keep it
                actions["kept"].append(f"BID {order.size:.0f} @ ${order.price:.2f}")
                target_bid = None  # Don't place new one
            else:
                # Order doesn't match, cancel it
                await self._cancel(oid, order.price)
                actions["cancels"].append(f"BID {order.size:.0f} @ ${order.price:.2f}")
        
        # Place new bid if needed
        if target_bid and target_bid_size > 0:
            await self._place(Side.BUY, target_bid, target_bid_size, now_ms)
            actions["places"].append(f"BID {target_bid_size:.0f} @ ${target_bid:.2f}")
        
        # --- Handle ASK side ---
        target_ask = targets.ask_price
        target_ask_size = targets.ask_size if target_ask else 0
        
        for oid, order in current_asks.items():
            if target_ask and abs(order.price - target_ask) < 0.001 and order.size >= target_ask_size * 0.9:
                # Order matches target, keep it
                actions["kept"].append(f"ASK {order.size:.0f} @ ${order.price:.2f}")
                target_ask = None  # Don't place new one
            else:
                # Order doesn't match, cancel it
                await self._cancel(oid, order.price)
                actions["cancels"].append(f"ASK {order.size:.0f} @ ${order.price:.2f}")
        
        # Place new ask if needed
        if target_ask and target_ask_size > 0:
            await self._place(Side.SELL, target_ask, target_ask_size, now_ms)
            actions["places"].append(f"ASK {target_ask_size:.0f} @ ${target_ask:.2f}")
        
        return actions
    
    async def cancel_all(self, current_orders: dict[str, OpenOrder]) -> None:
        """Cancel all open orders."""
        for oid, order in current_orders.items():
            await self._cancel(oid, order.price)
    
    async def _cancel(self, order_id: str, price: float) -> None:
        """
        Fire a cancel request.
        
        If it fails, we log but don't panic - WS will tell us what happened.
        """
        self.cancels_sent += 1
        
        try:
            result = await self.rest.cancel_order(order_id)
            
            if not result.success:
                self.cancel_failures += 1
                # Log the failure but don't panic - WS is source of truth
                logger.info(
                    f"Cancel returned error (expected if filled): {result.error_msg} "
                    f"[order=${price:.2f}]"
                )
        except Exception as e:
            self.cancel_failures += 1
            logger.warning(f"Cancel request failed: {e}")
    
    async def _place(self, side: Side, price: float, size: float, now_ms: int) -> None:
        """
        Fire a place request.
        
        If it fails, we log and will retry next tick.
        """
        self.places_sent += 1
        
        try:
            expires_at_ms = now_ms + self.default_ttl_ms
            
            request = OrderRequest(
                client_req_id=f"{now_ms}_{side.name}",
                token_id=self.yes_token_id,
                side=side,
                price=price,
                size=size,
                expires_at_ms=expires_at_ms,
            )
            
            result = await self.rest.place_order(request)
            
            if not result.success:
                self.place_failures += 1
                logger.warning(f"Place failed: {result.error_msg} [{side.name} {size:.0f} @ ${price:.2f}]")
        
        except Exception as e:
            self.place_failures += 1
            logger.warning(f"Place request failed: {e}")
    
    def get_stats(self) -> dict:
        """Get executor statistics."""
        return {
            "places_sent": self.places_sent,
            "cancels_sent": self.cancels_sent,
            "place_failures": self.place_failures,
            "cancel_failures": self.cancel_failures,
        }
