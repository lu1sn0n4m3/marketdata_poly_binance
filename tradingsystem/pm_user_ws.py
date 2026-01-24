"""
Polymarket User WebSocket Client (threaded).

Connects to authenticated user WebSocket for order/fill events.
Pushes events to Executor queue - these events MUST NOT be dropped.

Critical safety: triggers cancel-all callback on reconnect.
"""

import logging
import queue
from typing import Optional, Callable

import orjson

from .ws_base import ThreadedWsClient
from .mm_types import (
    Token,
    Side,
    OrderStatus,
    ExecutorEventType,
    OrderAckEvent,
    CancelAckEvent,
    FillEvent,
    now_ms,
)

logger = logging.getLogger(__name__)

# Default WebSocket URL
PM_USER_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/user"


class PolymarketUserWsClient(ThreadedWsClient):
    """
    Polymarket user events WebSocket client (authenticated).

    Responsibilities:
    - Maintain authenticated connection
    - Parse order/trade events
    - Push events to Executor queue (LOSSLESS - never drop)
    - Trigger cancel-all on reconnect (safety)
    """

    def __init__(
        self,
        event_queue: queue.Queue,
        api_key: str = "",
        api_secret: str = "",
        passphrase: str = "",
        on_reconnect: Optional[Callable[[], None]] = None,
        ws_url: str = PM_USER_WS_URL,
        ping_interval: int = 20,
        ping_timeout: int = 60,
    ):
        """
        Initialize user WebSocket client.

        Args:
            event_queue: Queue to push events to (Executor inbox)
            api_key: Polymarket API key
            api_secret: Polymarket API secret
            passphrase: Polymarket passphrase
            on_reconnect: Callback for reconnection (should trigger cancel-all)
            ws_url: WebSocket URL
            ping_interval: Ping interval in seconds
            ping_timeout: Ping timeout in seconds
        """
        super().__init__(
            ws_url=ws_url,
            name="PM-User-WS",
            ping_interval=ping_interval,
            ping_timeout=ping_timeout,
        )

        self._event_queue = event_queue
        self._api_key = api_key
        self._api_secret = api_secret
        self._passphrase = passphrase
        self._on_reconnect = on_reconnect

        # Market configuration
        self._market_ids: list[str] = []

        # Track if this is first connect (no cancel-all needed) or reconnect
        self._first_connect = True

    def set_auth(self, api_key: str, api_secret: str, passphrase: str) -> None:
        """
        Set authentication credentials.

        Args:
            api_key: Polymarket API key
            api_secret: Polymarket API secret
            passphrase: Polymarket passphrase
        """
        self._api_key = api_key
        self._api_secret = api_secret
        self._passphrase = passphrase

    def set_markets(self, market_ids: list[str]) -> None:
        """
        Set markets (condition IDs) to subscribe to.

        Args:
            market_ids: List of market/condition IDs
        """
        self._market_ids = market_ids

    def _subscribe(self) -> None:
        """Send authenticated subscription message."""
        if not self._api_key or not self._api_secret or not self._passphrase:
            logger.warning("PM-User-WS: Auth credentials not set, skipping subscribe")
            return

        if not self._market_ids:
            logger.warning("PM-User-WS: No markets configured, skipping subscribe")
            return

        msg = {
            "type": "USER",
            "auth": {
                "apikey": self._api_key,
                "secret": self._api_secret,
                "passphrase": self._passphrase,
            },
            "markets": self._market_ids,
        }
        self._send(orjson.dumps(msg))
        logger.info(f"PM-User-WS: Subscribed to {len(self._market_ids)} markets")

    def _handle_message(self, data: bytes) -> None:
        """
        Parse and handle message. Push events to Executor queue.

        These events MUST NOT be dropped.
        """
        try:
            msg = orjson.loads(data)

            # Handle array of events
            if isinstance(msg, list):
                for item in msg:
                    self._process_event(item)
            else:
                self._process_event(msg)

        except Exception as e:
            # Log errors - we don't want to silently drop user events
            logger.error(f"PM-User-WS: Parse error: {e}")

    def _process_event(self, data: dict) -> None:
        """Process a single event."""
        event_type = data.get("event_type")

        if event_type == "order":
            self._handle_order_event(data)
        elif event_type == "trade":
            self._handle_trade_event(data)
        # Ignore other event types

    def _handle_order_event(self, data: dict) -> None:
        """Handle order placement/update/cancellation."""
        order_type = data.get("type", "")
        ts = now_ms()

        if order_type == "PLACEMENT":
            # Order placed successfully
            event = OrderAckEvent(
                event_type=ExecutorEventType.ORDER_ACK,
                ts_local_ms=ts,
                client_order_id=data.get("id", ""),
                server_order_id=data.get("id", ""),
                status=OrderStatus.WORKING,
                side=self._parse_side(data.get("side", "")),
                price=self._price_to_cents(data.get("price")),
                size=self._parse_size(data.get("original_size")),
                token=self._outcome_to_token(data.get("outcome")),
            )
            self._enqueue(event)

        elif order_type == "UPDATE":
            # Order update (partial fill indication)
            size_matched = self._parse_size(data.get("size_matched"))
            original_size = self._parse_size(data.get("original_size"))

            # Determine status based on fill state
            if size_matched >= original_size:
                status = OrderStatus.FILLED
            elif size_matched > 0:
                status = OrderStatus.PARTIALLY_FILLED
            else:
                status = OrderStatus.WORKING

            event = OrderAckEvent(
                event_type=ExecutorEventType.ORDER_ACK,
                ts_local_ms=ts,
                client_order_id=data.get("id", ""),
                server_order_id=data.get("id", ""),
                status=status,
                side=self._parse_side(data.get("side", "")),
                price=self._price_to_cents(data.get("price")),
                size=original_size,
                token=self._outcome_to_token(data.get("outcome")),
            )
            self._enqueue(event)

        elif order_type == "CANCELLATION":
            # Order canceled
            event = CancelAckEvent(
                event_type=ExecutorEventType.CANCEL_ACK,
                ts_local_ms=ts,
                server_order_id=data.get("id", ""),
                success=True,
                reason="user_cancel",
            )
            self._enqueue(event)

    def _handle_trade_event(self, data: dict) -> None:
        """Handle trade fill event."""
        status = data.get("status", "")
        ts = now_ms()

        # DEBUG: Log raw trade event data
        logger.debug(f"[TRADE_DEBUG] Raw trade event: {data}")

        # Only process confirmed fills
        if status not in ("CONFIRMED", "MINED", "MATCHED"):
            return

        # Determine if we're taker or maker in this trade
        trader_side = data.get("trader_side", "")

        # Taker fill - only process if WE are the taker
        if trader_side == "TAKER":
            taker_order_id = data.get("taker_order_id", "")
            if taker_order_id:
                event = FillEvent(
                    event_type=ExecutorEventType.FILL,
                    ts_local_ms=ts,
                    server_order_id=taker_order_id,
                    token=self._outcome_to_token(data.get("outcome")),
                    side=self._parse_side(data.get("side", "")),
                    price=self._price_to_cents(data.get("price")),
                    size=self._parse_size(data.get("size")),
                    fee=0.0,
                    ts_exchange=self._parse_timestamp(data.get("timestamp")),
                )
                logger.info(
                    f"[FILL] TAKER: {event.token.name} {event.side.name} "
                    f"{event.size}@{event.price}c order={taker_order_id[:16]}..."
                )
                self._enqueue(event)

        # Maker fills - only process if WE are the maker
        # Note: maker_orders contains ALL makers in this trade, not just us.
        # The executor will filter to only update inventory for orders it tracks.
        elif trader_side == "MAKER":
            for maker in data.get("maker_orders", []):
                maker_order_id = maker.get("order_id", "")
                if not maker_order_id:
                    continue

                # Use the side directly from the maker order data (not inverted!)
                maker_side = self._parse_side(maker.get("side", ""))

                maker_event = FillEvent(
                    event_type=ExecutorEventType.FILL,
                    ts_local_ms=ts,
                    server_order_id=maker_order_id,
                    token=self._outcome_to_token(maker.get("outcome")),
                    side=maker_side,
                    price=self._price_to_cents(maker.get("price")),
                    size=self._parse_size(maker.get("matched_amount")),
                    fee=0.0,
                    ts_exchange=self._parse_timestamp(data.get("timestamp")),
                )
                logger.debug(
                    f"[FILL] MAKER candidate: {maker_event.token.name} {maker_event.side.name} "
                    f"{maker_event.size}@{maker_event.price}c order={maker_order_id[:16]}..."
                )
                self._enqueue(maker_event)

    def _enqueue(self, event) -> None:
        """
        Enqueue event to Executor. MUST NOT DROP.

        Uses blocking put if queue is full.
        """
        try:
            self._event_queue.put_nowait(event)
        except queue.Full:
            # This should never happen with properly sized queue
            # Log error and block to ensure event is not dropped
            logger.error("PM-User-WS: Event queue full! Blocking to enqueue")
            self._event_queue.put(event, block=True)

    def _on_connect(self) -> None:
        """Called after successful connection."""
        if self._first_connect:
            self._first_connect = False
            logger.info("PM-User-WS: Initial connection established")
        else:
            logger.info("PM-User-WS: Reconnected")

    def _on_disconnect(self) -> None:
        """
        Called after disconnection.

        CRITICAL: Trigger cancel-all on reconnect for safety.
        """
        if not self._first_connect and self._on_reconnect:
            logger.warning("PM-User-WS: Disconnect detected, triggering cancel-all")
            try:
                self._on_reconnect()
            except Exception as e:
                logger.error(f"PM-User-WS: Reconnect callback failed: {e}")

    @staticmethod
    def _parse_side(side_str: str) -> Side:
        """Parse side string to enum."""
        return Side.BUY if side_str.upper() == "BUY" else Side.SELL

    @staticmethod
    def _outcome_to_token(outcome: Optional[str]) -> Token:
        """Convert outcome string to Token enum."""
        if outcome and outcome.lower() in ("yes", "up"):
            return Token.YES
        return Token.NO

    @staticmethod
    def _price_to_cents(price_str: Optional[str]) -> int:
        """Convert price string to cents."""
        if not price_str:
            return 0
        try:
            return round(float(price_str) * 100)
        except (ValueError, TypeError):
            return 0

    @staticmethod
    def _parse_size(size_str: Optional[str]) -> int:
        """Parse size string to int."""
        if not size_str:
            return 0
        try:
            return int(float(size_str))
        except (ValueError, TypeError):
            return 0

    @staticmethod
    def _parse_timestamp(ts_str: Optional[str]) -> int:
        """Parse timestamp string to ms."""
        if not ts_str:
            return 0
        try:
            return int(ts_str)
        except (ValueError, TypeError):
            return 0
