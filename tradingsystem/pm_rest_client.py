"""
Polymarket REST Client for order operations.

Uses py-clob-client for order signing and submission.
Handles place, cancel, and cancel-all operations.
"""

import logging
from dataclasses import dataclass
from typing import Optional
from enum import Enum

from .mm_types import Token, Side

logger = logging.getLogger(__name__)


class OrderType(Enum):
    """Order type for Polymarket."""
    GTC = "GTC"  # Good-till-cancelled
    GTD = "GTD"  # Good-till-date
    FOK = "FOK"  # Fill-or-kill


@dataclass(slots=True)
class OrderResult:
    """Result of an order operation."""
    success: bool
    order_id: Optional[str] = None
    error_msg: Optional[str] = None
    retryable: bool = False


@dataclass(slots=True)
class CancelResult:
    """Result of a cancel operation."""
    success: bool
    error_msg: Optional[str] = None
    not_found: bool = False


class PolymarketRestClient:
    """
    Polymarket REST client using py-clob-client for order signing.

    Thread-safe for concurrent use from Gateway worker.
    """

    def __init__(
        self,
        private_key: str,
        funder: str = "",
        signature_type: int = 1,
        host: str = "https://clob.polymarket.com",
        chain_id: int = 137,
    ):
        """
        Initialize the REST client.

        Args:
            private_key: Wallet private key (0x prefixed)
            funder: Funder address for proxy wallets (0x prefixed)
            signature_type: 1 for EOA, 2 for Gnosis Safe proxy
            host: CLOB API host URL
            chain_id: Polygon chain ID (137 for mainnet)
        """
        self._private_key = private_key
        self._funder = funder
        self._signature_type = signature_type
        self._host = host
        self._chain_id = chain_id

        self._client = None
        self._initialized = False

        # API credentials (derived on first use)
        self._api_key: Optional[str] = None
        self._api_secret: Optional[str] = None
        self._passphrase: Optional[str] = None

    def _ensure_initialized(self) -> None:
        """Initialize the CLOB client lazily."""
        if self._initialized:
            return

        try:
            from py_clob_client.client import ClobClient

            self._client = ClobClient(
                host=self._host,
                chain_id=self._chain_id,
                key=self._private_key,
                funder=self._funder if self._funder else None,
                signature_type=self._signature_type,
            )

            # Derive API credentials
            creds = self._client.derive_api_key()
            self._api_key = creds.api_key
            self._api_secret = creds.api_secret
            self._passphrase = creds.api_passphrase

            # Set the credentials on the client for authenticated requests
            self._client.set_api_creds(creds)

            self._initialized = True
            logger.info("PolymarketRestClient initialized")

        except Exception as e:
            logger.error(f"Failed to initialize CLOB client: {e}")
            raise

    @property
    def api_credentials(self) -> tuple[str, str, str]:
        """
        Get API credentials for WebSocket authentication.

        Returns:
            Tuple of (api_key, api_secret, passphrase)
        """
        self._ensure_initialized()
        return (self._api_key, self._api_secret, self._passphrase)

    def place_order(
        self,
        token_id: str,
        side: Side,
        price: float,
        size: int,
        order_type: OrderType = OrderType.GTC,
        expiration: Optional[int] = None,
    ) -> OrderResult:
        """
        Place a single order.

        Args:
            token_id: Token ID (YES or NO token)
            side: BUY or SELL
            price: Price in [0, 1] range
            size: Size in shares (integer)
            order_type: GTC, GTD, or FOK
            expiration: Unix timestamp for GTD orders

        Returns:
            OrderResult with success status and order_id
        """
        self._ensure_initialized()

        try:
            from py_clob_client.order_builder.constants import BUY, SELL
            from py_clob_client.clob_types import OrderArgs

            # Convert side
            clob_side = BUY if side == Side.BUY else SELL

            # Build order args
            order_args = OrderArgs(
                token_id=token_id,
                price=price,
                size=float(size),
                side=clob_side,
            )

            # Build and sign order
            order = self._client.create_order(order_args)

            # Submit order
            response = self._client.post_order(order, order_type.value)

            if response and response.get("success"):
                order_id = response.get("orderID") or response.get("order_id", "")
                return OrderResult(success=True, order_id=order_id)
            else:
                error_msg = response.get("error", "Unknown error") if response else "No response"
                return OrderResult(
                    success=False,
                    error_msg=error_msg,
                    retryable=self._is_retryable_error(error_msg),
                )

        except Exception as e:
            error_msg = str(e)
            logger.warning(f"Order placement failed: {error_msg}")
            return OrderResult(
                success=False,
                error_msg=error_msg,
                retryable=self._is_retryable_error(error_msg),
            )

    def place_orders_batch(
        self,
        orders: list[tuple[str, Side, float, int]],
        order_type: OrderType = OrderType.GTC,
    ) -> list[OrderResult]:
        """
        Place multiple orders in a single request.

        Args:
            orders: List of (token_id, side, price, size) tuples
            order_type: Order type for all orders

        Returns:
            List of OrderResult for each order
        """
        self._ensure_initialized()

        results = []
        try:
            from py_clob_client.order_builder.constants import BUY, SELL
            from py_clob_client.clob_types import OrderArgs

            # Build all orders
            clob_orders = []
            for token_id, side, price, size in orders:
                clob_side = BUY if side == Side.BUY else SELL
                order_args = OrderArgs(
                    token_id=token_id,
                    price=price,
                    size=float(size),
                    side=clob_side,
                )
                order = self._client.create_order(order_args)
                clob_orders.append(order)

            # Submit batch
            response = self._client.post_orders(clob_orders, order_type.value)

            if response and isinstance(response, list):
                for i, resp in enumerate(response):
                    if resp.get("success"):
                        order_id = resp.get("orderID") or resp.get("order_id", "")
                        results.append(OrderResult(success=True, order_id=order_id))
                    else:
                        error_msg = resp.get("error", "Unknown error")
                        results.append(OrderResult(
                            success=False,
                            error_msg=error_msg,
                            retryable=self._is_retryable_error(error_msg),
                        ))
            else:
                # All failed
                error_msg = str(response) if response else "No response"
                results = [
                    OrderResult(success=False, error_msg=error_msg, retryable=True)
                    for _ in orders
                ]

        except Exception as e:
            error_msg = str(e)
            logger.warning(f"Batch order placement failed: {error_msg}")
            results = [
                OrderResult(
                    success=False,
                    error_msg=error_msg,
                    retryable=self._is_retryable_error(error_msg),
                )
                for _ in orders
            ]

        return results

    def cancel_order(self, order_id: str) -> CancelResult:
        """
        Cancel a single order.

        Args:
            order_id: Order ID to cancel

        Returns:
            CancelResult with success status
        """
        self._ensure_initialized()

        try:
            response = self._client.cancel(order_id)

            # Response format: {"canceled": ["order_id", ...], "not_canceled": {...}}
            if response:
                canceled = response.get("canceled", [])
                not_canceled = response.get("not_canceled", {})

                if order_id in canceled:
                    return CancelResult(success=True)
                elif order_id in not_canceled:
                    error_msg = str(not_canceled.get(order_id, "Not canceled"))
                    if "not found" in error_msg.lower():
                        return CancelResult(success=True, not_found=True)
                    return CancelResult(success=False, error_msg=error_msg)
                elif canceled:
                    # Order was canceled (may have different ID format)
                    return CancelResult(success=True)
                else:
                    return CancelResult(success=False, error_msg="Unknown response format")
            else:
                return CancelResult(success=False, error_msg="No response")

        except Exception as e:
            error_msg = str(e)
            if "not found" in error_msg.lower():
                return CancelResult(success=True, not_found=True)
            logger.warning(f"Order cancel failed: {error_msg}")
            return CancelResult(success=False, error_msg=error_msg)

    def cancel_orders_batch(self, order_ids: list[str]) -> list[CancelResult]:
        """
        Cancel multiple orders in a single request.

        Args:
            order_ids: List of order IDs to cancel

        Returns:
            List of CancelResult for each order
        """
        self._ensure_initialized()

        try:
            response = self._client.cancel_orders(order_ids)

            if response and response.get("success"):
                return [CancelResult(success=True) for _ in order_ids]
            else:
                error_msg = response.get("error", "Unknown error") if response else "No response"
                return [CancelResult(success=False, error_msg=error_msg) for _ in order_ids]

        except Exception as e:
            error_msg = str(e)
            logger.warning(f"Batch cancel failed: {error_msg}")
            return [CancelResult(success=False, error_msg=error_msg) for _ in order_ids]

    def cancel_all(self, market_id: str) -> CancelResult:
        """
        Cancel all orders for a market.

        FAST PATH: This should never be rate-limited.

        Args:
            market_id: Market/condition ID

        Returns:
            CancelResult with success status
        """
        self._ensure_initialized()

        try:
            response = self._client.cancel_all()

            # Response format: {"canceled": [...], "not_canceled": {...}}
            if response:
                canceled = response.get("canceled", [])
                # If we got any canceled orders or an empty not_canceled, it's success
                if canceled or not response.get("not_canceled"):
                    return CancelResult(success=True)
                else:
                    return CancelResult(success=False, error_msg="Some orders not canceled")
            else:
                return CancelResult(success=False, error_msg="No response")

        except Exception as e:
            error_msg = str(e)
            logger.warning(f"Cancel all failed: {error_msg}")
            return CancelResult(success=False, error_msg=error_msg)

    def cancel_market_orders(self, market_id: str) -> CancelResult:
        """
        Cancel all orders for a specific market.

        Args:
            market_id: Market/condition ID

        Returns:
            CancelResult with success status
        """
        self._ensure_initialized()

        try:
            response = self._client.cancel_market_orders(market_id)

            # Response format: {"canceled": [...], "not_canceled": {...}}
            if response:
                canceled = response.get("canceled", [])
                if canceled or not response.get("not_canceled"):
                    return CancelResult(success=True)
                else:
                    return CancelResult(success=False, error_msg="Some orders not canceled")
            else:
                return CancelResult(success=False, error_msg="No response")

        except Exception as e:
            error_msg = str(e)
            logger.warning(f"Cancel market orders failed: {error_msg}")
            return CancelResult(success=False, error_msg=error_msg)

    def get_open_orders(self, market_id: Optional[str] = None) -> list[dict]:
        """
        Get open orders, optionally filtered by market.

        Args:
            market_id: Optional market/condition ID filter

        Returns:
            List of open orders
        """
        self._ensure_initialized()

        try:
            from py_clob_client.clob_types import OpenOrderParams

            if market_id:
                params = OpenOrderParams(market=market_id)
                response = self._client.get_orders(params=params)
            else:
                response = self._client.get_orders()

            return response if response else []

        except Exception as e:
            logger.warning(f"Get open orders failed: {e}")
            return []

    @staticmethod
    def _is_retryable_error(error_msg: str) -> bool:
        """Determine if an error is retryable."""
        if not error_msg:
            return False
        error_lower = error_msg.lower()
        retryable_patterns = [
            "timeout",
            "connection",
            "503",
            "502",
            "504",
            "429",  # Rate limit
            "temporary",
            "retry",
        ]
        return any(p in error_lower for p in retryable_patterns)
