"""Executor for Container B - performs REST calls and emits events."""

import asyncio
import logging
from time import time_ns
from typing import Optional
from dataclasses import dataclass

from .types import (
    Event, OrderAction, OrderActionType, RestOrderAckEvent, RestErrorEvent,
)
from .polymarket_rest import PolymarketRestClient, OrderRequest

logger = logging.getLogger(__name__)


@dataclass
class InflightCall:
    """Tracks an inflight REST call."""
    client_req_id: str
    action_type: OrderActionType
    submitted_at_ms: int
    order_id: Optional[str] = None


class Executor:
    """
    Performs REST calls and emits acks/errors as events.
    
    Acts as the bridge between order manager actions and REST execution.
    """
    
    def __init__(
        self,
        rest: PolymarketRestClient,
        on_event: Optional[asyncio.Queue[Event]] = None,
    ):
        """
        Initialize the executor.
        
        Args:
            rest: REST client for execution
            on_event: Queue to emit events to (reducer queue)
        """
        self.rest = rest
        self._out_queue = on_event
        
        # Track inflight calls
        self._inflight: dict[str, InflightCall] = {}
    
    @property
    def inflight(self) -> dict[str, InflightCall]:
        """Currently inflight calls."""
        return self._inflight
    
    async def submit(self, actions: list[OrderAction]) -> None:
        """
        Submit a batch of actions.
        
        Args:
            actions: List of order actions to execute
        """
        for action in actions:
            try:
                if action.action_type == OrderActionType.PLACE:
                    await self.place(action)
                elif action.action_type == OrderActionType.CANCEL:
                    await self.cancel(action.order_id or "")
                elif action.action_type == OrderActionType.REPLACE:
                    # Replace = cancel + place
                    if action.order_id:
                        await self.cancel(action.order_id)
                    await self.place(action)
            except Exception as e:
                logger.error(f"Executor error for {action.client_req_id}: {e}")
                await self.emit_error(e, {"client_req_id": action.client_req_id})
    
    async def place(self, action: OrderAction) -> None:
        """
        Place an order.
        
        Args:
            action: OrderAction with order details
        """
        if not action.order:
            return
        
        now_ms = time_ns() // 1_000_000
        
        # Track inflight
        self._inflight[action.client_req_id] = InflightCall(
            client_req_id=action.client_req_id,
            action_type=OrderActionType.PLACE,
            submitted_at_ms=now_ms,
        )
        
        try:
            # Build request
            request = OrderRequest(
                client_req_id=action.client_req_id,
                token_id=action.order.token_id,
                side=action.order.side,
                price=action.order.price,
                size=action.order.size,
                expires_at_ms=action.order.expires_at_ms,
            )
            
            # Execute
            result = await self.rest.place_order(request)
            
            # Emit result
            if result.success:
                await self.emit_ack(RestOrderAckEvent(
                    event_type=None,
                    ts_local_ms=time_ns() // 1_000_000,
                    client_req_id=result.client_req_id,
                    order_id=result.order_id,
                    success=True,
                ))
            else:
                await self.emit_error(
                    Exception(result.error_msg),
                    {"client_req_id": result.client_req_id}
                )
        
        finally:
            # Remove from inflight
            self._inflight.pop(action.client_req_id, None)
    
    async def cancel(self, order_id: str) -> None:
        """
        Cancel an order.
        
        Args:
            order_id: Order ID to cancel
        """
        if not order_id:
            return
        
        try:
            result = await self.rest.cancel_order(order_id)
            
            if not result.success:
                logger.warning(f"Cancel failed for {order_id}: {result.error_msg}")
        
        except Exception as e:
            logger.error(f"Cancel error for {order_id}: {e}")
    
    async def cancel_all(self, market_id: str) -> None:
        """
        Cancel all orders for a market.
        
        Args:
            market_id: Market ID to cancel orders for
        """
        try:
            result = await self.rest.cancel_all(market_id)
            
            if result.success:
                logger.info(f"Cancel-all completed: {result.cancelled_count} orders")
            else:
                logger.error(f"Cancel-all failed: {result.error_msg}")
        
        except Exception as e:
            logger.error(f"Cancel-all error: {e}")
    
    async def emit_ack(self, ack: RestOrderAckEvent) -> None:
        """Emit an order acknowledgment."""
        if self._out_queue:
            try:
                self._out_queue.put_nowait(ack)
            except asyncio.QueueFull:
                logger.warning("Executor queue full, dropping ack")
    
    async def emit_error(self, err: Exception, context: dict) -> None:
        """Emit an error event."""
        event = RestErrorEvent(
            event_type=None,
            ts_local_ms=time_ns() // 1_000_000,
            client_req_id=context.get("client_req_id", ""),
            reason=str(err),
            recoverable=True,  # Assume recoverable unless proven otherwise
        )
        
        if self._out_queue:
            try:
                self._out_queue.put_nowait(event)
            except asyncio.QueueFull:
                logger.warning("Executor queue full, dropping error")
