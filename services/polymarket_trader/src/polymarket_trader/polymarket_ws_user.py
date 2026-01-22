"""Polymarket user WebSocket client."""

import asyncio
import logging
from time import time_ns
from typing import Optional, Callable, Awaitable

import orjson
import websockets
from websockets.exceptions import ConnectionClosed

from .types import Event, UserOrderEvent, UserTradeEvent, WsEvent, Side, OrderStatus

logger = logging.getLogger(__name__)


class PolymarketUserWsClient:
    """
    Polymarket user WebSocket client.
    
    Connects to the authenticated user websocket for order/fill updates.
    """
    
    def __init__(
        self,
        ws_url: str = "wss://ws-subscriptions-clob.polymarket.com/ws/user",
        api_key: str = "",
        api_secret: str = "",
        passphrase: str = "",
        on_event: Optional[Callable[[Event], Awaitable[None]]] = None,
    ):
        """
        Initialize the client.
        
        Args:
            ws_url: WebSocket URL
            api_key: API key for authentication
            api_secret: API secret
            passphrase: API passphrase
            on_event: Callback for events
        """
        self._ws_url = ws_url
        self._api_key = api_key
        self._api_secret = api_secret
        self._passphrase = passphrase
        self._on_event = on_event
        
        self._connected = False
        self._running = False
        self._ws: Optional[websockets.WebSocketClientProtocol] = None
        
        self._last_msg_local_ms: Optional[int] = None
        self._market_ids: list[str] = []
        
        # Event queue
        self.out_queue: asyncio.Queue[Event] = asyncio.Queue(maxsize=10000)
    
    @property
    def connected(self) -> bool:
        """Whether connected."""
        return self._connected
    
    @property
    def last_msg_local_ms(self) -> Optional[int]:
        """Timestamp of last message."""
        return self._last_msg_local_ms

    def set_markets(self, market_ids: list[str]) -> None:
        """Set market IDs (condition IDs) to subscribe to."""
        self._market_ids = market_ids

    def set_auth(self, api_key: str, api_secret: str, passphrase: str) -> None:
        """Update auth credentials (e.g., after REST derivation)."""
        self._api_key = api_key
        self._api_secret = api_secret
        self._passphrase = passphrase
    
    async def connect(self) -> None:
        """Establish WebSocket connection."""
        logger.info(f"Connecting to Polymarket user WS: {self._ws_url[:50]}...")
        
        # Build connection kwargs
        connect_kwargs = {
            "ping_interval": 20,
            "ping_timeout": 60,
            "max_size": 2**20,
        }
        
        # Polymarket user WS auth is provided in subscribe payload (not headers)
        self._ws = await websockets.connect(self._ws_url, **connect_kwargs)
        self._connected = True
        logger.info("Connected to Polymarket user WS")
        
        # Emit connection event
        await self._emit(WsEvent(
            event_type=None,
            ws_name="user",
            connected=True,
        ))
    
    async def subscribe_user(self) -> None:
        """Subscribe to user updates."""
        if not self._ws:
            return
        
        if not self._api_key or not self._api_secret or not self._passphrase:
            logger.warning("User WS auth credentials missing; cannot subscribe")
            return
        if not self._market_ids:
            logger.warning("User WS markets missing; cannot subscribe")
            return
        
        # Subscribe to user channel with auth payload
        msg = {
            "type": "USER",
            "auth": {
                "apikey": self._api_key,
                "secret": self._api_secret,
                "passphrase": self._passphrase,
            },
            "markets": self._market_ids,
        }
        await self._ws.send(orjson.dumps(msg))
        logger.info("Subscribed to user channel")
    
    async def close(self) -> None:
        """Close the connection."""
        self._connected = False
        
        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None
        
        # Emit disconnect event
        await self._emit(WsEvent(
            event_type=None,
            ws_name="user",
            connected=False,
        ))
    
    async def _emit(self, event: Event) -> None:
        """Emit an event."""
        try:
            self.out_queue.put_nowait(event)
        except asyncio.QueueFull:
            logger.warning("User WS queue full, dropping event")
        
        if self._on_event:
            try:
                await self._on_event(event)
            except Exception as e:
                logger.warning(f"Error in user WS event callback: {e}")
    
    def _parse_order_status(self, status_str: str) -> OrderStatus:
        """Parse order status string to enum."""
        status_map = {
            "live": OrderStatus.OPEN,
            "open": OrderStatus.OPEN,
            "matched": OrderStatus.FILLED,
            "filled": OrderStatus.FILLED,
            "cancelled": OrderStatus.CANCELLED,
            "canceled": OrderStatus.CANCELLED,
            "expired": OrderStatus.EXPIRED,
            "rejected": OrderStatus.REJECTED,
        }
        return status_map.get(status_str.lower(), OrderStatus.UNKNOWN)
    
    def _parse_side(self, side_str: str) -> Side:
        """Parse side string to enum."""
        return Side.BUY if side_str.upper() == "BUY" else Side.SELL
    
    def parse_message(self, raw: bytes) -> list[Event]:
        """
        Parse a WebSocket message.
        
        Args:
            raw: Raw message bytes
        
        Returns:
            List of events
        """
        events: list[Event] = []
        recv_ts_ms = time_ns() // 1_000_000
        self._last_msg_local_ms = recv_ts_ms
        
        try:
            data = orjson.loads(raw)
            
            if isinstance(data, list):
                for item in data:
                    events.extend(self._parse_event(item, recv_ts_ms))
            else:
                events.extend(self._parse_event(data, recv_ts_ms))
                
        except Exception as e:
            logger.warning(f"Failed to parse user WS message: {e}")
        
        return events
    
    def _parse_event(self, data: dict, recv_ts_ms: int) -> list[Event]:
        """Parse a single event."""
        events: list[Event] = []
        event_type = data.get("event_type", data.get("type", ""))
        
        if event_type in ("order", "order_update"):
            # Order update
            order_data = data.get("order", data)
            
            # Parse filled/remaining
            original_size = float(order_data.get("size", order_data.get("original_size", 0)))
            remaining = float(order_data.get("size_matched", order_data.get("remaining", 0)))
            # Note: Polymarket might use different field names
            filled = original_size - remaining if original_size > 0 else 0
            
            events.append(UserOrderEvent(
                event_type=None,
                ts_local_ms=recv_ts_ms,
                order_id=order_data.get("id", order_data.get("order_id", "")),
                status=self._parse_order_status(order_data.get("status", "")),
                side=self._parse_side(order_data.get("side", "BUY")),
                price=float(order_data.get("price", 0)),
                size=original_size,
                filled=filled,
                remaining=remaining,
                token_id=order_data.get("asset_id", order_data.get("token_id", "")),
            ))
        
        elif event_type in ("trade", "fill"):
            # Trade/fill
            trade_data = data.get("trade", data)
            
            events.append(UserTradeEvent(
                event_type=None,
                ts_local_ms=recv_ts_ms,
                trade_id=trade_data.get("id", trade_data.get("trade_id", "")),
                order_id=trade_data.get("order_id", ""),
                side=self._parse_side(trade_data.get("side", "BUY")),
                price=float(trade_data.get("price", 0)),
                size=float(trade_data.get("size", trade_data.get("amount", 0))),
                token_id=trade_data.get("asset_id", trade_data.get("token_id", "")),
            ))
        
        return events
    
    async def run(self, shutdown_event: Optional[asyncio.Event] = None) -> None:
        """
        Main loop: connect, subscribe, and process messages.
        """
        self._running = True
        backoff = 1.0
        max_backoff = 60.0
        
        while self._running:
            if shutdown_event and shutdown_event.is_set():
                break
            
            try:
                await self.connect()
                await self.subscribe_user()
                backoff = 1.0  # Reset backoff
                
                async for message in self._ws:
                    if shutdown_event and shutdown_event.is_set():
                        break
                    if not self._running:
                        break
                    
                    events = self.parse_message(message)
                    for event in events:
                        await self._emit(event)
            
            except ConnectionClosed as e:
                logger.warning(f"User WS connection closed: {e}")
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(f"User WS error: {e}")
            finally:
                await self.close()
            
            if shutdown_event and shutdown_event.is_set():
                break
            if not self._running:
                break
            
            # Backoff before reconnecting
            logger.info(f"User WS reconnecting in {backoff:.1f}s...")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, max_backoff)
        
        logger.info("User WS client stopped")
    
    def stop(self) -> None:
        """Stop the client."""
        self._running = False
