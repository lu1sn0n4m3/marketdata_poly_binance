"""Polymarket REST client for order management."""

import asyncio
import logging
import uuid
from time import time_ns
from typing import Optional
from dataclasses import dataclass

import aiohttp
import orjson

from .types import DesiredOrder, Side, OrderPurpose

logger = logging.getLogger(__name__)


@dataclass
class OrderRequest:
    """Order request to send to Polymarket."""
    client_req_id: str
    token_id: str
    side: Side
    price: float
    size: float
    expires_at_ms: int
    
    def to_api_dict(self) -> dict:
        """Convert to API format."""
        return {
            "tokenID": self.token_id,
            "side": "BUY" if self.side == Side.BUY else "SELL",
            "price": str(self.price),
            "size": str(self.size),
            "expiration": str(self.expires_at_ms // 1000),  # Convert to seconds
            "type": "GTC",  # Good-til-cancelled (will actually use expiration)
        }


@dataclass
class OrderAck:
    """Acknowledgment from order placement."""
    client_req_id: str
    order_id: str
    success: bool
    error_msg: str = ""


@dataclass
class CancelAck:
    """Acknowledgment from order cancellation."""
    order_id: str
    success: bool
    error_msg: str = ""


@dataclass
class CancelAllAck:
    """Acknowledgment from cancel-all."""
    market_id: str
    cancelled_count: int
    success: bool
    error_msg: str = ""


class PolymarketRestClient:
    """
    Polymarket REST client for order placement and cancellation.
    
    Handles authentication, rate limiting, and error recovery.
    """
    
    def __init__(
        self,
        base_url: str = "https://clob.polymarket.com",
        api_key: str = "",
        api_secret: str = "",
        passphrase: str = "",
        private_key: str = "",
        timeout_seconds: float = 10.0,
    ):
        """
        Initialize the client.
        
        Args:
            base_url: REST API base URL
            api_key: API key
            api_secret: API secret
            passphrase: API passphrase
            private_key: Private key for signing (if needed)
            timeout_seconds: Request timeout
        """
        self.base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._api_secret = api_secret
        self._passphrase = passphrase
        self._private_key = private_key
        self.timeout_seconds = timeout_seconds
        
        self._session: Optional[aiohttp.ClientSession] = None
        self._last_request_ms: int = 0
        self._min_request_interval_ms: int = 50  # Rate limiting
        
        # Health tracking
        self._consecutive_errors: int = 0
        self._last_error_ms: int = 0
    
    async def _get_session(self) -> aiohttp.ClientSession:
        """Get or create HTTP session."""
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=self.timeout_seconds)
            headers = {}
            if self._api_key:
                headers["POLY-API-KEY"] = self._api_key
            if self._passphrase:
                headers["POLY-PASSPHRASE"] = self._passphrase
            
            self._session = aiohttp.ClientSession(
                timeout=timeout,
                headers=headers,
            )
        return self._session
    
    async def close(self) -> None:
        """Close the HTTP session."""
        if self._session and not self._session.closed:
            await self._session.close()
    
    async def _rate_limit(self) -> None:
        """Apply rate limiting between requests."""
        now_ms = time_ns() // 1_000_000
        elapsed = now_ms - self._last_request_ms
        
        if elapsed < self._min_request_interval_ms:
            await asyncio.sleep((self._min_request_interval_ms - elapsed) / 1000)
        
        self._last_request_ms = time_ns() // 1_000_000
    
    def _generate_client_req_id(self) -> str:
        """Generate a unique client request ID."""
        return str(uuid.uuid4())[:16]
    
    async def place_order(self, order: OrderRequest) -> OrderAck:
        """
        Place an order.
        
        Args:
            order: OrderRequest to place
        
        Returns:
            OrderAck with result
        """
        await self._rate_limit()
        
        try:
            session = await self._get_session()
            url = f"{self.base_url}/order"
            payload = order.to_api_dict()
            
            async with session.post(url, json=payload) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    self._consecutive_errors = 0
                    
                    return OrderAck(
                        client_req_id=order.client_req_id,
                        order_id=data.get("orderID", data.get("id", "")),
                        success=True,
                    )
                else:
                    error_text = await resp.text()
                    self._consecutive_errors += 1
                    self._last_error_ms = time_ns() // 1_000_000
                    
                    logger.warning(f"Order placement failed: {resp.status} - {error_text}")
                    
                    return OrderAck(
                        client_req_id=order.client_req_id,
                        order_id="",
                        success=False,
                        error_msg=error_text,
                    )
        
        except Exception as e:
            self._consecutive_errors += 1
            self._last_error_ms = time_ns() // 1_000_000
            
            logger.error(f"Order placement error: {e}")
            
            return OrderAck(
                client_req_id=order.client_req_id,
                order_id="",
                success=False,
                error_msg=str(e),
            )
    
    async def cancel_order(self, order_id: str) -> CancelAck:
        """
        Cancel an order.
        
        Args:
            order_id: Order ID to cancel
        
        Returns:
            CancelAck with result
        """
        await self._rate_limit()
        
        try:
            session = await self._get_session()
            url = f"{self.base_url}/order/{order_id}"
            
            async with session.delete(url) as resp:
                if resp.status in (200, 204):
                    self._consecutive_errors = 0
                    return CancelAck(order_id=order_id, success=True)
                else:
                    error_text = await resp.text()
                    self._consecutive_errors += 1
                    self._last_error_ms = time_ns() // 1_000_000
                    
                    return CancelAck(
                        order_id=order_id,
                        success=False,
                        error_msg=error_text,
                    )
        
        except Exception as e:
            self._consecutive_errors += 1
            self._last_error_ms = time_ns() // 1_000_000
            
            return CancelAck(
                order_id=order_id,
                success=False,
                error_msg=str(e),
            )
    
    async def cancel_all(self, market_id: str = "") -> CancelAllAck:
        """
        Cancel all orders (optionally for a specific market).
        
        Args:
            market_id: Optional market ID to cancel orders for
        
        Returns:
            CancelAllAck with result
        """
        await self._rate_limit()
        
        try:
            session = await self._get_session()
            url = f"{self.base_url}/orders"
            params = {}
            if market_id:
                params["market"] = market_id
            
            async with session.delete(url, params=params if params else None) as resp:
                if resp.status in (200, 204):
                    data = await resp.json() if resp.status == 200 else {}
                    self._consecutive_errors = 0
                    
                    return CancelAllAck(
                        market_id=market_id,
                        cancelled_count=data.get("cancelled", 0),
                        success=True,
                    )
                else:
                    error_text = await resp.text()
                    self._consecutive_errors += 1
                    self._last_error_ms = time_ns() // 1_000_000
                    
                    return CancelAllAck(
                        market_id=market_id,
                        cancelled_count=0,
                        success=False,
                        error_msg=error_text,
                    )
        
        except Exception as e:
            self._consecutive_errors += 1
            self._last_error_ms = time_ns() // 1_000_000
            
            return CancelAllAck(
                market_id=market_id,
                cancelled_count=0,
                success=False,
                error_msg=str(e),
            )
    
    async def healthcheck(self) -> bool:
        """
        Check if the REST API is healthy.
        
        Returns:
            True if healthy
        """
        # Consider unhealthy if too many consecutive errors
        if self._consecutive_errors >= 5:
            return False
        
        try:
            session = await self._get_session()
            url = f"{self.base_url}/time"  # Simple endpoint to check connectivity
            
            async with session.get(url) as resp:
                healthy = resp.status == 200
                if healthy:
                    self._consecutive_errors = 0
                return healthy
        
        except Exception as e:
            logger.warning(f"REST healthcheck failed: {e}")
            return False
    
    @property
    def is_healthy(self) -> bool:
        """Whether the client appears healthy based on recent errors."""
        return self._consecutive_errors < 5
    
    @property
    def last_error_ms(self) -> int:
        """Timestamp of last error."""
        return self._last_error_ms
