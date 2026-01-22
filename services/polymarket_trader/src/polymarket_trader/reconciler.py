"""Reconciler for cancel-all and conservative rebuild triggers."""

import asyncio
import logging
from time import time_ns
from typing import Optional

from .polymarket_rest import PolymarketRestClient

logger = logging.getLogger(__name__)


class Reconciler:
    """
    Implements cancel-all and conservative rebuild triggers.
    
    Used when:
    - WebSocket reconnects
    - Unknown/uncertain state detected
    - Operator triggers emergency action
    """
    
    def __init__(
        self,
        rest: PolymarketRestClient,
        cooldown_ms: int = 5000,
    ):
        """
        Initialize the reconciler.
        
        Args:
            rest: REST client for cancel-all
            cooldown_ms: Cooldown period after reconciliation
        """
        self.rest = rest
        self.cooldown_ms = cooldown_ms
        
        self._in_progress = False
        self._last_reconcile_ms: int = 0
        self._reconcile_count: int = 0
    
    @property
    def in_progress(self) -> bool:
        """Whether reconciliation is in progress."""
        return self._in_progress
    
    def is_in_cooldown(self, now_ms: Optional[int] = None) -> bool:
        """
        Check if we're in cooldown period after reconciliation.
        
        Args:
            now_ms: Current timestamp (defaults to current time)
        
        Returns:
            True if in cooldown
        """
        if now_ms is None:
            now_ms = time_ns() // 1_000_000
        
        return (now_ms - self._last_reconcile_ms) < self.cooldown_ms
    
    async def on_reconnect(self, market_id: str) -> None:
        """
        Handle WebSocket reconnection.
        
        Triggers cancel-all and marks reconciliation in progress.
        
        Args:
            market_id: Market ID to cancel orders for
        """
        logger.info(f"Reconciler: Handling reconnect for market {market_id}")
        await self.start_cancel_all(market_id)
    
    async def on_uncertainty(self, reason: str, market_id: str) -> None:
        """
        Handle uncertainty event.
        
        Args:
            reason: Reason for uncertainty
            market_id: Market ID to cancel orders for
        """
        logger.warning(f"Reconciler: Uncertainty detected - {reason}")
        await self.start_cancel_all(market_id)
    
    async def start_cancel_all(self, market_id: str) -> None:
        """
        Start cancel-all operation.
        
        Args:
            market_id: Market ID to cancel orders for
        """
        if self._in_progress:
            logger.warning("Cancel-all already in progress")
            return
        
        self._in_progress = True
        self._reconcile_count += 1
        
        logger.info(f"Starting cancel-all for market {market_id}")
        
        try:
            result = await self.rest.cancel_all(market_id)
            
            if result.success:
                logger.info(f"Cancel-all completed: {result.cancelled_count} orders cancelled")
            else:
                logger.error(f"Cancel-all failed: {result.error_msg}")
        
        except Exception as e:
            logger.error(f"Cancel-all error: {e}")
        
        finally:
            self._last_reconcile_ms = time_ns() // 1_000_000
            self.mark_complete()
    
    def mark_complete(self) -> None:
        """Mark reconciliation as complete."""
        self._in_progress = False
        logger.info("Reconciliation complete - entering cooldown")
