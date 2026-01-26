"""
Polymarket User Feed (threaded).

Connects to authenticated user WebSocket for order/fill events.
Pushes events to Executor queue - these events MUST NOT be dropped.

Critical safety: triggers cancel-all callback on reconnect.
"""

import logging
import os
import queue
from datetime import datetime
from typing import Optional, Callable

import orjson

from .websocket_base import ThreadedWsClient
from ..types import (
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


class PolymarketUserFeed(ThreadedWsClient):
    """
    Polymarket user events feed (authenticated).

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
        maker_address: str = "",
        on_reconnect: Optional[Callable[[], None]] = None,
        ws_url: str = PM_USER_WS_URL,
        ping_interval: int = 20,
        ping_timeout: int = 60,
    ):
        """
        Initialize user feed.

        Args:
            event_queue: Queue to push events to (Executor inbox)
            api_key: Polymarket API key
            api_secret: Polymarket API secret
            passphrase: Polymarket passphrase
            maker_address: Our wallet/funder address for matching MAKER fills
            on_reconnect: Callback for reconnection (should trigger cancel-all)
            ws_url: WebSocket URL
            ping_interval: Ping interval in seconds
            ping_timeout: Ping timeout in seconds
        """
        super().__init__(
            ws_url=ws_url,
            name="PolymarketUserFeed",
            ping_interval=ping_interval,
            ping_timeout=ping_timeout,
        )

        self._event_queue = event_queue
        self._api_key = api_key
        self._api_secret = api_secret
        self._passphrase = passphrase
        self._maker_address = maker_address.lower() if maker_address else ""
        self._on_reconnect = on_reconnect

        # Market configuration
        self._market_ids: list[str] = []

        # Token IDs for filtering - only process fills for OUR market
        self._yes_token_id: str = ""
        self._no_token_id: str = ""

        # Track if this is first connect (no cancel-all needed) or reconnect
        self._first_connect = True

        # Session start timestamp (ms) - filter out fills older than this
        # Set when the feed starts to ignore historical fills replayed on connect
        self._session_start_ts: int = 0

        # Debug: file handle for logging raw trade events
        self._debug_trade_file = None

    def start(self) -> None:
        """
        Start the user feed.

        Sets session start timestamp to filter out historical fills
        that may be replayed on WebSocket connect.
        """
        # Record session start time BEFORE connecting
        # Add a small buffer (5 seconds) to account for clock skew
        self._session_start_ts = now_ms() - 5000
        logger.info(f"PolymarketUserFeed: Session start ts={self._session_start_ts}")

        # Open debug file for raw trade events
        os.makedirs("debug", exist_ok=True)
        ts_str = datetime.now().strftime("%Y%m%d_%H%M%S")
        debug_path = f"debug/raw_trades_{ts_str}.jsonl"
        self._debug_trade_file = open(debug_path, "w")
        logger.info(f"PolymarketUserFeed: Debug trade log: {debug_path}")

        super().start()

    def stop(self, timeout: float = 5.0) -> None:
        """Stop the user feed."""
        super().stop(timeout)
        if self._debug_trade_file:
            self._debug_trade_file.close()
            self._debug_trade_file = None
            logger.info("PolymarketUserFeed: Debug trade log closed")

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

    def set_tokens(self, yes_token_id: str, no_token_id: str) -> None:
        """
        Set token IDs for filtering fills.

        Only fills for these tokens will be processed.
        This prevents processing fills from OTHER markets.

        Args:
            yes_token_id: YES token ID for this market
            no_token_id: NO token ID for this market
        """
        self._yes_token_id = yes_token_id
        self._no_token_id = no_token_id
        logger.info(f"PolymarketUserFeed: Token filter set - YES={yes_token_id[:20]}... NO={no_token_id[:20]}...")

    def set_maker_address(self, maker_address: str) -> None:
        """
        Set our wallet/maker address for filtering MAKER fills.

        Args:
            maker_address: Our wallet address (funder address)
        """
        self._maker_address = maker_address.lower() if maker_address else ""
        logger.info(f"PolymarketUserFeed: Maker address set: {self._maker_address[:20]}..." if self._maker_address else "PolymarketUserFeed: No maker address set")

    def _subscribe(self) -> None:
        """Send authenticated subscription message."""
        if not self._api_key or not self._api_secret or not self._passphrase:
            logger.warning("PolymarketUserFeed: Auth credentials not set, skipping subscribe")
            return

        if not self._market_ids:
            logger.warning("PolymarketUserFeed: No markets configured, skipping subscribe")
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
        logger.info(f"PolymarketUserFeed: Subscribed to {len(self._market_ids)} markets")

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
            logger.error(f"PolymarketUserFeed: Parse error: {e}")

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

        # Filter by asset_id - only process orders for OUR market
        asset_id = data.get("asset_id", "")
        if self._yes_token_id and self._no_token_id:
            if asset_id and asset_id not in (self._yes_token_id, self._no_token_id):
                logger.debug(f"[ORDER] Ignoring order event for other market: asset_id={asset_id[:20]}...")
                return

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
        """
        Handle trade fill event.

        Polymarket fill lifecycle:
        1. MATCHED: Order matched off-chain (immediate). Tokens NOT in wallet yet.
        2. MINED: Transaction confirmed on-chain (~17-19 seconds later). Tokens in wallet.
        3. CONFIRMED: Same as MINED (alternative status).

        We process BOTH MATCHED and MINED events:
        - MATCHED (is_pending=True): Update pending inventory immediately.
          Strategy sees this to avoid duplicate orders.
          Executor CANNOT place SELL orders on pending inventory.

        - MINED (is_pending=False): Settle the pending inventory.
          Move from pending to settled. Now can be sold.
        """
        status = data.get("status", "")
        ts = now_ms()
        trade_id = data.get("id", "")  # Unique trade ID for deduplication
        trader_side = data.get("trader_side", "")  # "TAKER" or "MAKER"

        # DEBUG: Save raw trade event to file for analysis
        # NOTE: Removed flush() - was causing 10-100ms latency per fill
        # File will flush automatically on buffer full or program exit
        if self._debug_trade_file:
            debug_entry = {
                "local_ts": ts,
                "session_start_ts": self._session_start_ts,
                "raw": data,
            }
            self._debug_trade_file.write(orjson.dumps(debug_entry).decode() + "\n")
            # self._debug_trade_file.flush()  # REMOVED: 10-100ms latency killer

        # DEBUG: Log raw trade event data (DEBUG level to avoid hot path latency)
        logger.debug(f"[TRADE_DEBUG] Raw trade event: status={status} trader_side={trader_side} outcome={data.get('outcome')} side={data.get('side')} size={data.get('size')} price={data.get('price')}")

        # Determine if this is a pending (MATCHED) or settled (MINED/CONFIRMED) fill
        if status == "MATCHED":
            is_pending = True
            logger.debug(f"[FILL] Processing MATCHED fill (pending): trade_id={trade_id[:20]}...")
        elif status in ("CONFIRMED", "MINED"):
            is_pending = False
            logger.debug(f"[FILL] Processing {status} fill (settled): trade_id={trade_id[:20]}...")
        else:
            # Unknown status - ignore
            logger.debug(f"[FILL] Ignoring unknown fill status: status={status}")
            return

        # Filter by asset_id - only process fills for OUR market's tokens
        # This prevents processing fills from OTHER markets we may have orders on
        asset_id = data.get("asset_id", "")
        if self._yes_token_id and self._no_token_id:
            if asset_id and asset_id not in (self._yes_token_id, self._no_token_id):
                logger.debug(f"[FILL] Ignoring fill for other market: asset_id={asset_id[:20]}...")
                return

        # Filter out historical fills from before this session started
        # The WebSocket may replay recent fills on connect
        trade_ts = self._parse_timestamp(data.get("timestamp"))
        if trade_ts > 0 and self._session_start_ts > 0 and trade_ts < self._session_start_ts:
            logger.warning(
                f"[FILL] IGNORED historical fill: ts={trade_ts} < session_start={self._session_start_ts}"
            )
            return

        # Use trader_side to determine our role in this trade
        if trader_side == "TAKER":
            # We are the taker - use top-level fields
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
                    trade_id=trade_id,
                    role="TAKER",
                    is_pending=is_pending,  # MATCHED=pending, MINED/CONFIRMED=settled
                )
                pending_str = "PENDING" if is_pending else "SETTLED"
                logger.info(
                    f"[FILL] TAKER ({pending_str}): {event.token.name} {event.side.name} "
                    f"{event.size}@{event.price}c order={taker_order_id[:16]}... trade={trade_id[:16]}..."
                )
                self._enqueue(event)

        elif trader_side == "MAKER":
            # We are ONE of the makers - filter to find only our orders
            # maker_orders contains ALL makers in this trade, not just us
            for maker in data.get("maker_orders", []):
                # Only process our orders - match by maker_address (wallet address)
                # The "owner" field is a UUID that doesn't match API key format
                maker_addr = (maker.get("maker_address") or "").lower()
                if self._maker_address and maker_addr != self._maker_address:
                    continue

                # Filter by asset_id - only process fills for OUR market
                maker_asset_id = maker.get("asset_id", "")
                if self._yes_token_id and self._no_token_id:
                    if maker_asset_id and maker_asset_id not in (self._yes_token_id, self._no_token_id):
                        logger.debug(f"[FILL] Ignoring MAKER fill for other market: asset_id={maker_asset_id[:20]}...")
                        continue

                maker_order_id = maker.get("order_id", "")
                if not maker_order_id:
                    continue

                # Use maker's actual order side from the data (NOT flipped!)
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
                    trade_id=trade_id,
                    role="MAKER",
                    is_pending=is_pending,  # MATCHED=pending, MINED/CONFIRMED=settled
                )
                pending_str = "PENDING" if is_pending else "SETTLED"
                logger.info(
                    f"[FILL] MAKER ({pending_str}): {maker_event.token.name} {maker_event.side.name} "
                    f"{maker_event.size}@{maker_event.price}c order={maker_order_id[:16]}... trade={trade_id[:16]}..."
                )
                self._enqueue(maker_event)
        else:
            # Unknown trader_side - log for debugging
            logger.warning(f"[FILL] Unknown trader_side={trader_side} for trade={trade_id[:16]}...")

    def _enqueue(self, event) -> None:
        """
        Enqueue event to Executor. MUST NOT DROP.

        Uses bounded blocking with timeout to avoid indefinite hang.
        If timeout expires, escalates but still enqueues (fills must not be dropped).
        """
        try:
            self._event_queue.put_nowait(event)
        except queue.Full:
            # Queue full - use bounded blocking with escalation
            logger.warning("PolymarketUserFeed: Event queue full, blocking with timeout")
            try:
                # Wait up to 5s - if executor is stuck longer, we have bigger problems
                self._event_queue.put(event, block=True, timeout=5.0)
            except queue.Full:
                # Timeout expired - executor is severely behind or stuck
                # CRITICAL: Still must enqueue (fills cannot be dropped)
                # Log critical and force blocking put
                logger.critical(
                    "PolymarketUserFeed: Queue blocked >5s - executor may be stuck! "
                    "Consider emergency close."
                )
                self._event_queue.put(event, block=True)

    def _on_connect(self) -> None:
        """Called after successful connection."""
        if self._first_connect:
            self._first_connect = False
            logger.info("PolymarketUserFeed: Initial connection established")
        else:
            logger.info("PolymarketUserFeed: Reconnected")

    def _on_disconnect(self) -> None:
        """
        Called after disconnection.

        CRITICAL: Trigger cancel-all on reconnect for safety.
        """
        if not self._first_connect and self._on_reconnect:
            logger.warning("PolymarketUserFeed: Disconnect detected, triggering cancel-all")
            try:
                self._on_reconnect()
            except Exception as e:
                logger.error(f"PolymarketUserFeed: Reconnect callback failed: {e}")

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
