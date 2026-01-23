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
    token_id: str = ""  # Token ID to distinguish YES vs NO orders


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

    def set_token_ids(self, yes_token_id: str, no_token_id: str) -> None:
        """Update token IDs (useful when market changes)."""
        self.yes_token_id = yes_token_id
        self.no_token_id = no_token_id
    
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


class PositionAwareExecutor(SimpleExecutor):
    """
    Position-aware executor that avoids splitting shares.

    This executor handles the following logic automatically:

    For BID (buy YES):
        - Always places BUY order on YES token

    For ASK (sell YES):
        - If you HAVE YES shares (position > 0): places SELL order on YES token
        - If you DON'T have YES shares: places BUY order on NO token at (1 - price)

    This allows market making without pre-splitting USDC into shares.
    The strategy remains indifferent to this - it just outputs target quotes.

    Uses BATCH operations for efficiency - collects all cancels and places,
    then executes them in single API calls.
    """

    def __init__(
        self,
        rest: PolymarketRestClient,
        yes_token_id: str,
        no_token_id: str = "",
        default_ttl_ms: int = 60_000,
        pending_timeout_ms: int = 3000,
    ):
        super().__init__(rest, yes_token_id, no_token_id, default_ttl_ms)
        # Track position for smart routing
        self._yes_position: float = 0.0
        self._no_position: float = 0.0
        # Pending orders: placed but not yet WS-confirmed
        # Maps client_req_id -> (slot, price, size, timestamp_ms)
        self._pending_by_id: dict[str, tuple[str, float, float, int]] = {}
        self._pending_timeout_ms = pending_timeout_ms

    def _add_pending(self, slot: str, price: float, size: float, now_ms: int, client_req_id: str) -> None:
        """Record an order as pending (placed, awaiting WS confirmation)."""
        self._pending_by_id[client_req_id] = (slot, price, size, now_ms)

    def _remove_pending(self, client_req_id: str) -> None:
        """Remove a pending order by client_req_id (on failure or confirmation)."""
        self._pending_by_id.pop(client_req_id, None)

    def _get_pending_prices(self, slot: str, now_ms: int) -> set[float]:
        """Get prices of pending (non-expired) orders for a slot."""
        prices = set()
        expired = []

        for cid, (s, price, size, ts) in self._pending_by_id.items():
            if s == slot:
                if now_ms - ts < self._pending_timeout_ms:
                    prices.add(price)
                else:
                    expired.append(cid)

        # Clean up expired
        for cid in expired:
            del self._pending_by_id[cid]

        return prices

    def _clear_pending_at_price(self, slot: str, price: float) -> None:
        """Clear pending order when WS confirms it (or we see it in confirmed orders)."""
        to_remove = []
        for cid, (s, p, sz, ts) in self._pending_by_id.items():
            if s == slot and abs(p - price) < 0.001:
                to_remove.append(cid)
        for cid in to_remove:
            del self._pending_by_id[cid]

    def _is_pending_or_confirmed(
        self, slot: str, price: float, confirmed_prices: set[float], now_ms: int
    ) -> bool:
        """Check if order at this price is already confirmed OR pending."""
        # Check confirmed
        if any(abs(price - cp) < 0.001 for cp in confirmed_prices):
            # It's confirmed - clear from pending if it was there
            self._clear_pending_at_price(slot, price)
            return True

        # Check pending
        pending_prices = self._get_pending_prices(slot, now_ms)
        if any(abs(price - pp) < 0.001 for pp in pending_prices):
            return True

        return False

    def get_pending_count(self) -> int:
        """Get count of pending orders (for display/debugging)."""
        return len(self._pending_by_id)

    def update_position(self, yes_position: float, no_position: float = 0.0) -> None:
        """
        Update the executor's view of current position.

        This MUST be called before sync_to_targets each tick.

        Args:
            yes_position: Current YES token position (positive = long)
            no_position: Current NO token position (positive = long)
        """
        self._yes_position = yes_position
        self._no_position = no_position

    async def sync_to_targets(
        self,
        targets: TargetQuotes,
        current_orders: dict[str, OpenOrder],
        now_ms: int,
    ) -> dict:
        """
        Sync current orders to match target quotes with position-aware routing.

        Uses BATCH operations: collects all cancels and places first,
        then executes them in single API calls for efficiency.

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
            "pending": [],  # Orders that were skipped because they're pending confirmation
        }

        # Collect orders to cancel and place (batch them)
        orders_to_cancel: list[str] = []
        orders_to_place: list[OrderRequest] = []

        # Separate current orders by side AND token
        current_yes_bids = {}  # BUY YES
        current_yes_asks = {}  # SELL YES
        current_no_bids = {}   # BUY NO (synthetic YES ask)
        current_no_asks = {}   # SELL NO (when we have NO position)

        for oid, order in current_orders.items():
            is_no_order = self._is_no_order(oid, order)

            if order.side == Side.BUY:
                if is_no_order:
                    current_no_bids[oid] = order
                else:
                    current_yes_bids[oid] = order
            else:  # SELL
                if is_no_order:
                    current_no_asks[oid] = order
                else:
                    current_yes_asks[oid] = order

        # --- Handle BID side (always BUY YES) - supports multiple bids ---
        # Copy target bids so we can remove matched ones
        remaining_bids = dict(targets.bids)

        # Collect confirmed YES bid prices
        confirmed_yes_bid_prices = {o.price for o in current_yes_bids.values()}

        for oid, order in current_yes_bids.items():
            # Check if this order matches any target bid
            matched_price = None
            for target_price, target_size in remaining_bids.items():
                if abs(order.price - target_price) < 0.001 and order.size >= target_size * 0.9:
                    matched_price = target_price
                    break

            if matched_price is not None:
                actions["kept"].append(f"BID {order.size:.0f} @ ${order.price:.2f}")
                self._clear_pending_at_price("YES_BID", order.price)
                del remaining_bids[matched_price]
            else:
                orders_to_cancel.append(oid)
                actions["cancels"].append(f"BID {order.size:.0f} @ ${order.price:.2f}")

        # Place orders for remaining target bids (not yet matched or pending)
        for target_price, target_size in remaining_bids.items():
            if not self._is_pending_or_confirmed("YES_BID", target_price, confirmed_yes_bid_prices, now_ms):
                client_req_id = f"{now_ms}_YES_BUY_{int(target_price*100)}"
                orders_to_place.append(OrderRequest(
                    client_req_id=client_req_id,
                    token_id=self.yes_token_id,
                    side=Side.BUY,
                    price=target_price,
                    size=target_size,
                    expires_at_ms=now_ms + self.default_ttl_ms,
                ))
                self._add_pending("YES_BID", target_price, target_size, now_ms, client_req_id)
                actions["places"].append(f"YES BUY {target_size:.0f} @ ${target_price:.2f}")
            else:
                actions["pending"].append(f"BID {target_size:.0f} @ ${target_price:.2f}")

        # --- Handle ASK side (position-aware routing) - supports multiple asks ---
        if targets.asks:
            if self._yes_position > 0:
                # We have YES shares - actually sell them
                self._plan_asks_with_yes_position(
                    targets.asks, now_ms,
                    current_yes_asks, current_no_bids, current_no_asks,
                    orders_to_cancel, orders_to_place, actions
                )
            elif self._no_position > 0:
                # We have NO shares - actually sell them
                self._plan_asks_with_no_position(
                    targets.asks, now_ms,
                    current_yes_asks, current_no_bids, current_no_asks,
                    orders_to_cancel, orders_to_place, actions
                )
            else:
                # No position - buy NO at complement price
                self._plan_asks_without_position(
                    targets.asks, now_ms,
                    current_yes_asks, current_no_bids, current_no_asks,
                    orders_to_cancel, orders_to_place, actions
                )
        else:
            # No target asks - cancel all ask-side orders
            for oid, order in current_yes_asks.items():
                orders_to_cancel.append(oid)
                actions["cancels"].append(f"YES SELL {order.size:.0f} @ ${order.price:.2f}")
            for oid, order in current_no_bids.items():
                orders_to_cancel.append(oid)
                actions["cancels"].append(f"NO BUY {order.size:.0f} @ ${order.price:.2f}")
            for oid, order in current_no_asks.items():
                orders_to_cancel.append(oid)
                actions["cancels"].append(f"NO SELL {order.size:.0f} @ ${order.price:.2f}")

        # === EXECUTE BATCH OPERATIONS ===

        # First, cancel all orders that need cancelling (single API call)
        if orders_to_cancel:
            self.cancels_sent += len(orders_to_cancel)
            try:
                result = await self.rest.cancel_orders(orders_to_cancel)
                if not result.success:
                    self.cancel_failures += len(result.not_cancelled)
                    for oid, reason in result.not_cancelled.items():
                        logger.info(f"Cancel returned error (expected if filled): {reason} [order={oid[:10]}...]")
            except Exception as e:
                self.cancel_failures += len(orders_to_cancel)
                logger.warning(f"Batch cancel request failed: {e}")

        # Then, place all new orders (single API call)
        if orders_to_place:
            self.places_sent += len(orders_to_place)
            try:
                result = await self.rest.place_orders(orders_to_place)
                if not result.success:
                    self.place_failures += len(result.failed)
                    for client_id, error_msg in result.failed:
                        logger.warning(f"Place failed: {error_msg} [{client_id}]")
                        # Remove failed order from pending immediately
                        self._remove_pending(client_id)
            except Exception as e:
                self.place_failures += len(orders_to_place)
                logger.warning(f"Batch place request failed: {e}")
                # Remove all orders from pending on total failure
                for order in orders_to_place:
                    self._remove_pending(order.client_req_id)

        return actions

    def _plan_asks_with_yes_position(
        self,
        target_asks: dict[float, float],  # {price: size}
        now_ms: int,
        current_yes_asks: dict[str, OpenOrder],
        current_no_bids: dict[str, OpenOrder],
        current_no_asks: dict[str, OpenOrder],
        orders_to_cancel: list[str],
        orders_to_place: list[OrderRequest],
        actions: dict,
    ) -> None:
        """
        Plan multiple ASKs when we HAVE YES shares - will place SELL YES.

        Collects cancels/places into lists for batch execution.
        """
        # Cancel any NO bids (we're now using YES sells)
        for oid, order in current_no_bids.items():
            orders_to_cancel.append(oid)
            actions["cancels"].append(f"NO BUY {order.size:.0f} @ ${order.price:.2f}")

        # Cancel any NO sells (we're selling YES, not NO)
        for oid, order in current_no_asks.items():
            orders_to_cancel.append(oid)
            actions["cancels"].append(f"NO SELL {order.size:.0f} @ ${order.price:.2f}")

        # Copy target asks so we can remove matched ones
        remaining_asks = dict(target_asks)

        # Collect confirmed YES sell prices
        confirmed_yes_sell_prices = {o.price for o in current_yes_asks.values()}

        # Check if existing YES sells match any targets
        for oid, order in current_yes_asks.items():
            matched_price = None
            for target_price, target_size in remaining_asks.items():
                if abs(order.price - target_price) < 0.001 and order.size >= target_size * 0.9:
                    matched_price = target_price
                    break

            if matched_price is not None:
                actions["kept"].append(f"YES SELL {order.size:.0f} @ ${order.price:.2f}")
                self._clear_pending_at_price("YES_SELL", order.price)
                del remaining_asks[matched_price]
            else:
                orders_to_cancel.append(oid)
                actions["cancels"].append(f"YES SELL {order.size:.0f} @ ${order.price:.2f}")

        # Place orders for remaining target asks
        for target_price, target_size in remaining_asks.items():
            if not self._is_pending_or_confirmed("YES_SELL", target_price, confirmed_yes_sell_prices, now_ms):
                client_req_id = f"{now_ms}_YES_SELL_{int(target_price*100)}"
                orders_to_place.append(OrderRequest(
                    client_req_id=client_req_id,
                    token_id=self.yes_token_id,
                    side=Side.SELL,
                    price=target_price,
                    size=target_size,
                    expires_at_ms=now_ms + self.default_ttl_ms,
                ))
                self._add_pending("YES_SELL", target_price, target_size, now_ms, client_req_id)
                actions["places"].append(f"YES SELL {target_size:.0f} @ ${target_price:.2f}")
            else:
                actions["pending"].append(f"YES SELL {target_size:.0f} @ ${target_price:.2f}")

    def _plan_asks_with_no_position(
        self,
        target_asks: dict[float, float],  # {yes_ask_price: size}
        now_ms: int,
        current_yes_asks: dict[str, OpenOrder],
        current_no_bids: dict[str, OpenOrder],
        current_no_asks: dict[str, OpenOrder],
        orders_to_cancel: list[str],
        orders_to_place: list[OrderRequest],
        actions: dict,
    ) -> None:
        """
        Plan multiple ASKs when we HAVE NO shares - will place SELL NO.

        The strategy outputs YES-normalized prices. When selling NO:
        - YES ask price 0.60 -> NO sell price 0.40 (complement)

        Collects cancels/places into lists for batch execution.
        """
        # Cancel any YES sells (we don't have YES shares)
        for oid, order in current_yes_asks.items():
            orders_to_cancel.append(oid)
            actions["cancels"].append(f"YES SELL {order.size:.0f} @ ${order.price:.2f}")

        # Cancel any NO bids (we want to SELL NO, not buy more)
        for oid, order in current_no_bids.items():
            orders_to_cancel.append(oid)
            actions["cancels"].append(f"NO BUY {order.size:.0f} @ ${order.price:.2f}")

        # Convert target asks (YES prices) to NO sell prices
        # YES ask @ 0.60 = NO sell @ 0.40 (since YES + NO = 1.00)
        # {no_sell_price: (size, original_yes_ask_price)}
        target_no_sells = {}
        for yes_ask_price, size in target_asks.items():
            no_sell_price = round(1.0 - yes_ask_price, 2)
            no_sell_price = max(0.01, min(0.99, no_sell_price))
            target_no_sells[no_sell_price] = (size, yes_ask_price)

        # Copy for tracking remaining
        remaining_no_sells = dict(target_no_sells)

        # Collect confirmed NO sell prices from existing orders
        confirmed_no_sell_prices = {o.price for o in current_no_asks.values()}

        # Check if existing NO sells match any targets
        for oid, order in current_no_asks.items():
            matched_price = None
            for no_price, (size, yes_price) in remaining_no_sells.items():
                if abs(order.price - no_price) < 0.001 and order.size >= size * 0.9:
                    matched_price = no_price
                    break

            if matched_price is not None:
                size, yes_price = remaining_no_sells[matched_price]
                actions["kept"].append(f"NO SELL {order.size:.0f} @ ${order.price:.2f} (YES equiv ${yes_price:.2f})")
                self._clear_pending_at_price("NO_SELL", order.price)
                del remaining_no_sells[matched_price]
            else:
                orders_to_cancel.append(oid)
                actions["cancels"].append(f"NO SELL {order.size:.0f} @ ${order.price:.2f}")

        # Place orders for remaining targets
        if not self.no_token_id:
            if remaining_no_sells:
                logger.warning("Cannot place NO SELL: no_token_id not set")
            return

        for no_price, (size, yes_price) in remaining_no_sells.items():
            if not self._is_pending_or_confirmed("NO_SELL", no_price, confirmed_no_sell_prices, now_ms):
                client_req_id = f"{now_ms}_NO_SELL_{int(no_price*100)}"
                orders_to_place.append(OrderRequest(
                    client_req_id=client_req_id,
                    token_id=self.no_token_id,
                    side=Side.SELL,
                    price=no_price,
                    size=size,
                    expires_at_ms=now_ms + self.default_ttl_ms,
                ))
                self._add_pending("NO_SELL", no_price, size, now_ms, client_req_id)
                actions["places"].append(f"NO SELL {size:.0f} @ ${no_price:.2f} (YES equiv ${yes_price:.2f})")
            else:
                actions["pending"].append(f"NO SELL {size:.0f} @ ${no_price:.2f}")

    def _plan_asks_without_position(
        self,
        target_asks: dict[float, float],  # {yes_ask_price: size}
        now_ms: int,
        current_yes_asks: dict[str, OpenOrder],
        current_no_bids: dict[str, OpenOrder],
        current_no_asks: dict[str, OpenOrder],
        orders_to_cancel: list[str],
        orders_to_place: list[OrderRequest],
        actions: dict,
    ) -> None:
        """
        Plan multiple ASKs when we DON'T have YES shares - will place BUY NO at complement.

        The complement price: if we want to sell YES at 0.60, we buy NO at 0.40
        Collects cancels/places into lists for batch execution.
        """
        # Cancel any YES sells (we don't have shares to sell)
        for oid, order in current_yes_asks.items():
            orders_to_cancel.append(oid)
            actions["cancels"].append(f"YES SELL {order.size:.0f} @ ${order.price:.2f}")

        # Cancel any NO sells (we don't have NO shares either)
        for oid, order in current_no_asks.items():
            orders_to_cancel.append(oid)
            actions["cancels"].append(f"NO SELL {order.size:.0f} @ ${order.price:.2f}")

        # Convert target asks (YES prices) to NO bid prices
        # {no_bid_price: (size, original_yes_ask_price)}
        target_no_bids = {}
        for yes_ask_price, size in target_asks.items():
            no_bid_price = round(1.0 - yes_ask_price, 2)
            no_bid_price = max(0.01, min(0.99, no_bid_price))
            target_no_bids[no_bid_price] = (size, yes_ask_price)

        # Copy for tracking remaining
        remaining_no_bids = dict(target_no_bids)

        # Collect confirmed NO bid prices
        confirmed_no_bid_prices = {o.price for o in current_no_bids.values()}

        # Check if existing NO bids match any targets
        for oid, order in current_no_bids.items():
            matched_price = None
            for no_price, (size, yes_price) in remaining_no_bids.items():
                if abs(order.price - no_price) < 0.001 and order.size >= size * 0.9:
                    matched_price = no_price
                    break

            if matched_price is not None:
                size, yes_price = remaining_no_bids[matched_price]
                actions["kept"].append(f"NO BUY {order.size:.0f} @ ${order.price:.2f} (synth ASK ${yes_price:.2f})")
                self._clear_pending_at_price("NO_BID", order.price)
                del remaining_no_bids[matched_price]
            else:
                orders_to_cancel.append(oid)
                actions["cancels"].append(f"NO BUY {order.size:.0f} @ ${order.price:.2f}")

        # Place orders for remaining targets
        if not self.no_token_id:
            if remaining_no_bids:
                logger.warning("Cannot place NO BUY: no_token_id not set")
            return

        for no_price, (size, yes_price) in remaining_no_bids.items():
            if not self._is_pending_or_confirmed("NO_BID", no_price, confirmed_no_bid_prices, now_ms):
                client_req_id = f"{now_ms}_NO_BUY_{int(no_price*100)}"
                orders_to_place.append(OrderRequest(
                    client_req_id=client_req_id,
                    token_id=self.no_token_id,
                    side=Side.BUY,
                    price=no_price,
                    size=size,
                    expires_at_ms=now_ms + self.default_ttl_ms,
                ))
                self._add_pending("NO_BID", no_price, size, now_ms, client_req_id)
                actions["places"].append(f"NO BUY {size:.0f} @ ${no_price:.2f} (synth ASK ${yes_price:.2f})")
            else:
                actions["pending"].append(f"NO BUY {size:.0f} @ ${no_price:.2f}")

    def _is_yes_order(self, oid: str, order: OpenOrder) -> bool:
        """Check if order is on YES token."""
        if order.token_id:
            return order.token_id == self.yes_token_id
        return True  # Default to YES

    def _is_no_order(self, oid: str, order: OpenOrder) -> bool:
        """Check if order is on NO token."""
        if order.token_id:
            return order.token_id == self.no_token_id
        return False
