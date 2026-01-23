"""Polymarket REST client for order management using py-clob-client.

This module wraps the official py-clob-client library to provide
async order placement with proper cryptographic signing.
"""

import asyncio
import logging
import uuid
from concurrent.futures import ThreadPoolExecutor
from time import time_ns
from typing import Optional
from dataclasses import dataclass

from .types import Side

logger = logging.getLogger(__name__)

# Thread pool for running sync py-clob-client calls
_executor = ThreadPoolExecutor(max_workers=4)


@dataclass
class OrderRequest:
    """Order request to send to Polymarket."""
    client_req_id: str
    token_id: str
    side: Side
    price: float
    size: float
    expires_at_ms: int


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
class BatchOrderAck:
    """Acknowledgment from batch order placement."""
    successful: list[str]  # List of order IDs that succeeded
    failed: list[tuple[str, str]]  # List of (client_req_id, error_msg) for failures
    success: bool  # True if all orders succeeded


@dataclass
class BatchCancelAck:
    """Acknowledgment from batch cancellation."""
    cancelled: list[str]  # Order IDs that were cancelled
    not_cancelled: dict[str, str]  # {order_id: reason} for failures
    success: bool  # True if all cancels succeeded


@dataclass
class CancelAllAck:
    """Acknowledgment from cancel-all."""
    market_id: str
    cancelled_count: int
    success: bool
    error_msg: str = ""


@dataclass
class TokenPosition:
    """Position in a specific token."""
    token_id: str
    size: float  # Number of tokens held
    avg_price: float = 0.0  # Average entry price (if available)


class PolymarketRestClient:
    """
    Polymarket REST client for order placement and cancellation.
    
    Uses py-clob-client for proper order signing. All orders are
    cryptographically signed before submission.
    
    Required credentials:
        - private_key: Wallet private key for signing (0x...)
        - funder: Funder/proxy wallet address (0x...)
        - api_key, api_secret, passphrase: L2 API credentials
          (auto-derived if not provided)
    """
    
    def __init__(
        self,
        private_key: str,
        funder: str = "",
        signature_type: int = 1,
        api_key: str = "",
        api_secret: str = "",
        passphrase: str = "",
        chain_id: int = 137,  # Polygon mainnet
    ):
        """
        Initialize the client with signing capability.
        
        Args:
            private_key: Wallet private key (0x prefixed)
            funder: Funder address for proxy wallet (0x prefixed)
            signature_type: Signature type (1=EOA, 2=Proxy wallet)
            api_key: API key (optional, will be derived if not provided)
            api_secret: API secret (optional, will be derived if not provided)
            passphrase: API passphrase (optional, will be derived if not provided)
            chain_id: Chain ID (137 for Polygon)
        """
        self._private_key = private_key
        self._funder = funder
        self._signature_type = signature_type
        self._api_key = api_key
        self._api_secret = api_secret
        self._passphrase = passphrase
        self._chain_id = chain_id
        
        self._client = None
        self._initialized = False
        
        # Health tracking
        self._consecutive_errors: int = 0
        self._last_error_ms: int = 0
        
        # Rate limiting
        self._last_request_ms: int = 0
        self._min_request_interval_ms: int = 50
    
    def _init_client(self) -> bool:
        """Initialize the py-clob-client (sync, call from thread pool)."""
        if self._initialized and self._client is not None:
            return True
        
        try:
            from py_clob_client.client import ClobClient
            from py_clob_client.clob_types import ApiCreds
            
            # Ensure private key has 0x prefix
            private_key = self._private_key
            if private_key and not private_key.startswith("0x"):
                private_key = "0x" + private_key
            
            # Create client
            # Note: funder must be passed even if empty string, not None
            self._client = ClobClient(
                host="https://clob.polymarket.com",
                chain_id=self._chain_id,
                key=private_key,
                funder=self._funder,
                signature_type=self._signature_type,
            )
            
            # Set or derive API credentials
            if self._api_key and self._api_secret and self._passphrase:
                self._client.set_api_creds(ApiCreds(
                    api_key=self._api_key,
                    api_secret=self._api_secret,
                    api_passphrase=self._passphrase,
                ))
                logger.info("Polymarket client initialized with provided API credentials")
            else:
                # Derive credentials from private key
                creds = self._client.create_or_derive_api_creds()
                self._client.set_api_creds(creds)
                # Persist derived creds for downstream clients (e.g., user WS)
                self._api_key = creds.api_key
                self._api_secret = creds.api_secret
                self._passphrase = creds.api_passphrase
                logger.info("Polymarket client initialized with derived API credentials")
            
            self._initialized = True
            return True
            
        except ImportError as e:
            logger.error(f"py-clob-client not installed: {e}")
            logger.error("Install with: pip install py-clob-client")
            return False
        except Exception as e:
            logger.error(f"Failed to initialize Polymarket client: {e}")
            return False
    
    async def initialize(self) -> bool:
        """Initialize the client asynchronously."""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(_executor, self._init_client)
    
    def _place_order_sync(self, order: OrderRequest) -> OrderAck:
        """Place an order synchronously (called from thread pool)."""
        if not self._init_client():
            return OrderAck(
                client_req_id=order.client_req_id,
                order_id="",
                success=False,
                error_msg="Client not initialized",
            )
        
        try:
            from py_clob_client.clob_types import OrderArgs, OrderType
            from py_clob_client.order_builder.constants import BUY, SELL
            
            # Convert side - compare by name to avoid enum instance mismatch
            is_buy = (order.side.name == "BUY" if hasattr(order.side, 'name') 
                     else str(order.side) == "Side.BUY")
            order_side = BUY if is_buy else SELL
            
            # Convert expiration from milliseconds to seconds (Unix timestamp)
            expiration_seconds = int(order.expires_at_ms / 1000)
            
            # Create order args with expiration
            order_args = OrderArgs(
                token_id=order.token_id,
                price=float(order.price),
                size=float(order.size),
                side=order_side,
                expiration=expiration_seconds,
            )
            
            # Create signed order
            signed_order = self._client.create_order(order_args)
            
            # Post the order as GTD (Good-til-Date)
            response = self._client.post_order(signed_order, OrderType.GTD)
            
            # Parse response
            if response:
                success = response.get("success", False) if isinstance(response, dict) else True
                order_id = ""
                
                if isinstance(response, dict):
                    order_id = response.get("orderID", response.get("id", ""))
                    error_msg = response.get("errorMsg", "")
                    
                    if not success and error_msg:
                        self._consecutive_errors += 1
                        self._last_error_ms = time_ns() // 1_000_000
                        return OrderAck(
                            client_req_id=order.client_req_id,
                            order_id="",
                            success=False,
                            error_msg=error_msg,
                        )
                
                self._consecutive_errors = 0
                return OrderAck(
                    client_req_id=order.client_req_id,
                    order_id=order_id,
                    success=True,
                )
            else:
                self._consecutive_errors += 1
                self._last_error_ms = time_ns() // 1_000_000
                return OrderAck(
                    client_req_id=order.client_req_id,
                    order_id="",
                    success=False,
                    error_msg="Empty response from API",
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
    
    async def place_order(self, order: OrderRequest) -> OrderAck:
        """
        Place an order asynchronously.

        The order is signed using the private key before submission.

        Args:
            order: OrderRequest to place

        Returns:
            OrderAck with result
        """
        # Rate limiting
        now_ms = time_ns() // 1_000_000
        elapsed = now_ms - self._last_request_ms
        if elapsed < self._min_request_interval_ms:
            await asyncio.sleep((self._min_request_interval_ms - elapsed) / 1000)
        self._last_request_ms = time_ns() // 1_000_000

        # Execute in thread pool
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(_executor, self._place_order_sync, order)

    def _place_orders_sync(self, orders: list[OrderRequest]) -> BatchOrderAck:
        """Place multiple orders in a single batch (called from thread pool)."""
        if not self._init_client():
            return BatchOrderAck(
                successful=[],
                failed=[(o.client_req_id, "Client not initialized") for o in orders],
                success=False,
            )

        if not orders:
            return BatchOrderAck(successful=[], failed=[], success=True)

        try:
            from py_clob_client.clob_types import OrderArgs, OrderType, PostOrdersArgs
            from py_clob_client.order_builder.constants import BUY, SELL

            # Build PostOrdersArgs list for batch API
            post_orders_args = []
            for order in orders:
                is_buy = (order.side.name == "BUY" if hasattr(order.side, 'name')
                         else str(order.side) == "Side.BUY")
                order_side = BUY if is_buy else SELL
                expiration_seconds = int(order.expires_at_ms / 1000)

                order_args = OrderArgs(
                    token_id=order.token_id,
                    price=float(order.price),
                    size=float(order.size),
                    side=order_side,
                    expiration=expiration_seconds,
                )
                signed_order = self._client.create_order(order_args)
                post_orders_args.append(PostOrdersArgs(
                    order=signed_order,
                    orderType=OrderType.GTD,
                    postOnly=False,
                ))

            # Post all orders in a batch
            response = self._client.post_orders(post_orders_args)

            # Parse response
            successful = []
            failed = []

            if response:
                # Response format depends on py-clob-client version
                if isinstance(response, list):
                    for i, resp in enumerate(response):
                        if isinstance(resp, dict):
                            if resp.get("success", True) and not resp.get("errorMsg"):
                                order_id = resp.get("orderID", resp.get("id", ""))
                                successful.append(order_id)
                            else:
                                client_id = orders[i].client_req_id if i < len(orders) else ""
                                failed.append((client_id, resp.get("errorMsg", "Unknown error")))
                        else:
                            successful.append(str(resp))
                elif isinstance(response, dict):
                    # Single response for batch
                    if response.get("success", True):
                        order_ids = response.get("orderIDs", [])
                        successful.extend(order_ids)
                    else:
                        for order in orders:
                            failed.append((order.client_req_id, response.get("errorMsg", "Batch failed")))

            self._consecutive_errors = 0 if not failed else self._consecutive_errors + 1
            return BatchOrderAck(
                successful=successful,
                failed=failed,
                success=len(failed) == 0,
            )

        except Exception as e:
            self._consecutive_errors += 1
            self._last_error_ms = time_ns() // 1_000_000
            logger.error(f"Batch order placement error: {e}")
            return BatchOrderAck(
                successful=[],
                failed=[(o.client_req_id, str(e)) for o in orders],
                success=False,
            )

    async def place_orders(self, orders: list[OrderRequest]) -> BatchOrderAck:
        """
        Place multiple orders in a single batch request.

        This is more efficient than calling place_order() multiple times.

        Args:
            orders: List of OrderRequests to place

        Returns:
            BatchOrderAck with results
        """
        if not orders:
            return BatchOrderAck(successful=[], failed=[], success=True)

        # Rate limiting
        now_ms = time_ns() // 1_000_000
        elapsed = now_ms - self._last_request_ms
        if elapsed < self._min_request_interval_ms:
            await asyncio.sleep((self._min_request_interval_ms - elapsed) / 1000)
        self._last_request_ms = time_ns() // 1_000_000

        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(_executor, self._place_orders_sync, orders)

    def _cancel_orders_sync(self, order_ids: list[str]) -> BatchCancelAck:
        """Cancel multiple orders in a single batch (called from thread pool)."""
        if not self._init_client():
            return BatchCancelAck(
                cancelled=[],
                not_cancelled={oid: "Client not initialized" for oid in order_ids},
                success=False,
            )

        if not order_ids:
            return BatchCancelAck(cancelled=[], not_cancelled={}, success=True)

        try:
            # Use batch cancel - py-clob-client's cancel_orders() for multiple order IDs
            response = self._client.cancel_orders(order_ids)

            # Parse response
            cancelled = []
            not_cancelled = {}

            if response:
                if isinstance(response, dict):
                    cancelled = response.get("canceled", [])
                    not_cancelled = response.get("not_canceled", {})
                elif isinstance(response, list):
                    cancelled = response

            self._consecutive_errors = 0 if not not_cancelled else self._consecutive_errors + 1
            return BatchCancelAck(
                cancelled=cancelled,
                not_cancelled=not_cancelled,
                success=len(not_cancelled) == 0,
            )

        except Exception as e:
            self._consecutive_errors += 1
            self._last_error_ms = time_ns() // 1_000_000
            logger.error(f"Batch cancel error: {e}")
            return BatchCancelAck(
                cancelled=[],
                not_cancelled={oid: str(e) for oid in order_ids},
                success=False,
            )

    async def cancel_orders(self, order_ids: list[str]) -> BatchCancelAck:
        """
        Cancel multiple orders in a single batch request.

        This is more efficient than calling cancel_order() multiple times.

        Args:
            order_ids: List of order IDs to cancel

        Returns:
            BatchCancelAck with results
        """
        if not order_ids:
            return BatchCancelAck(cancelled=[], not_cancelled={}, success=True)

        # Rate limiting
        now_ms = time_ns() // 1_000_000
        elapsed = now_ms - self._last_request_ms
        if elapsed < self._min_request_interval_ms:
            await asyncio.sleep((self._min_request_interval_ms - elapsed) / 1000)
        self._last_request_ms = time_ns() // 1_000_000

        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(_executor, self._cancel_orders_sync, order_ids)

    def _cancel_order_sync(self, order_id: str) -> CancelAck:
        """Cancel an order synchronously (called from thread pool)."""
        if not self._init_client():
            return CancelAck(
                order_id=order_id,
                success=False,
                error_msg="Client not initialized",
            )
        
        try:
            # Cancel the order
            response = self._client.cancel(order_id)
            
            # Parse response
            if response:
                canceled = response.get("canceled", []) if isinstance(response, dict) else []
                not_canceled = response.get("not_canceled", {}) if isinstance(response, dict) else {}
                
                if order_id in canceled or not not_canceled:
                    self._consecutive_errors = 0
                    return CancelAck(order_id=order_id, success=True)
                else:
                    error = not_canceled.get(order_id, "Unknown error")
                    self._consecutive_errors += 1
                    self._last_error_ms = time_ns() // 1_000_000
                    return CancelAck(
                        order_id=order_id,
                        success=False,
                        error_msg=str(error),
                    )
            else:
                # Empty response often means success
                self._consecutive_errors = 0
                return CancelAck(order_id=order_id, success=True)
        
        except Exception as e:
            self._consecutive_errors += 1
            self._last_error_ms = time_ns() // 1_000_000
            return CancelAck(
                order_id=order_id,
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
        # Rate limiting
        now_ms = time_ns() // 1_000_000
        elapsed = now_ms - self._last_request_ms
        if elapsed < self._min_request_interval_ms:
            await asyncio.sleep((self._min_request_interval_ms - elapsed) / 1000)
        self._last_request_ms = time_ns() // 1_000_000
        
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(_executor, self._cancel_order_sync, order_id)
    
    def _cancel_all_sync(self, market_id: str = "") -> CancelAllAck:
        """Cancel all orders synchronously (called from thread pool)."""
        if not self._init_client():
            return CancelAllAck(
                market_id=market_id,
                cancelled_count=0,
                success=False,
                error_msg="Client not initialized",
            )
        
        try:
            # Cancel all orders
            response = self._client.cancel_all()
            
            # Parse response
            if response:
                canceled = response.get("canceled", []) if isinstance(response, dict) else []
                not_canceled = response.get("not_canceled", {}) if isinstance(response, dict) else {}
                
                cancelled_count = len(canceled) if isinstance(canceled, list) else 0
                
                if not not_canceled:
                    self._consecutive_errors = 0
                    return CancelAllAck(
                        market_id=market_id,
                        cancelled_count=cancelled_count,
                        success=True,
                    )
                else:
                    # Some orders couldn't be cancelled
                    self._consecutive_errors += 1
                    self._last_error_ms = time_ns() // 1_000_000
                    return CancelAllAck(
                        market_id=market_id,
                        cancelled_count=cancelled_count,
                        success=False,
                        error_msg=f"Failed to cancel {len(not_canceled)} orders",
                    )
            else:
                # Empty response often means success (no orders to cancel)
                self._consecutive_errors = 0
                return CancelAllAck(
                    market_id=market_id,
                    cancelled_count=0,
                    success=True,
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
    
    def _get_open_orders_sync(self) -> list:
        """Get open orders synchronously (called from thread pool)."""
        if not self._init_client():
            return []
        
        try:
            # Get open orders from py-clob-client
            orders = self._client.get_orders()
            return orders if orders else []
        except Exception as e:
            logger.error(f"Failed to get open orders: {e}")
            return []
    
    async def get_open_orders(self) -> list:
        """
        Get all open orders asynchronously.
        
        Returns:
            List of open orders
        """
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(_executor, self._get_open_orders_sync)
    
    async def cancel_all(self, market_id: str = "") -> CancelAllAck:
        """
        Cancel all orders (optionally for a specific market).
        
        Args:
            market_id: Optional market ID to cancel orders for (not used currently)
        
        Returns:
            CancelAllAck with result
        """
        # Rate limiting
        now_ms = time_ns() // 1_000_000
        elapsed = now_ms - self._last_request_ms
        if elapsed < self._min_request_interval_ms:
            await asyncio.sleep((self._min_request_interval_ms - elapsed) / 1000)
        self._last_request_ms = time_ns() // 1_000_000
        
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(_executor, self._cancel_all_sync, market_id)
    
    async def healthcheck(self) -> bool:
        """
        Check if the REST API is healthy.
        
        Returns:
            True if healthy
        """
        # Consider unhealthy if too many consecutive errors
        if self._consecutive_errors >= 5:
            return False
        
        # Verify client is initialized
        if not self._initialized:
            initialized = await self.initialize()
            return initialized
        
        return True
    
    async def close(self) -> None:
        """Close the client (cleanup)."""
        # py-clob-client doesn't require explicit cleanup
        pass
    
    @property
    def is_healthy(self) -> bool:
        """Whether the client appears healthy based on recent errors."""
        return self._consecutive_errors < 5
    
    @property
    def last_error_ms(self) -> int:
        """Timestamp of last error."""
        return self._last_error_ms

    @property
    def api_key(self) -> str:
        """Current API key (provided or derived)."""
        return self._api_key

    @property
    def api_secret(self) -> str:
        """Current API secret (provided or derived)."""
        return self._api_secret

    @property
    def passphrase(self) -> str:
        """Current API passphrase (provided or derived)."""
        return self._passphrase


def _generate_client_req_id() -> str:
    """Generate a unique client request ID."""
    return str(uuid.uuid4())[:16]
