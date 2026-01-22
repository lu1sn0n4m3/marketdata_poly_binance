"""Polymarket market WebSocket client."""

import asyncio
import logging
from time import time_ns
from typing import Optional, Callable, Awaitable

import orjson
import websockets
from websockets.exceptions import ConnectionClosed

from .types import Event, MarketBboEvent, TickSizeChangedEvent, WsEvent

logger = logging.getLogger(__name__)


class PolymarketMarketWsClient:
    """
    Polymarket market WebSocket client.
    
    Connects to the public market websocket for book/BBO/tick size updates.
    """
    
    def __init__(
        self,
        ws_url: str = "wss://ws-subscriptions-clob.polymarket.com/ws/market",
        on_event: Optional[Callable[[Event], Awaitable[None]]] = None,
    ):
        """
        Initialize the client.
        
        Args:
            ws_url: WebSocket URL
            on_event: Callback for events
        """
        self._ws_url = ws_url
        self._on_event = on_event
        
        self._market_id: Optional[str] = None
        self._token_ids: list[str] = []
        self._connected = False
        self._running = False
        self._ws: Optional[websockets.WebSocketClientProtocol] = None
        
        self._last_msg_local_ms: Optional[int] = None
        self._current_tick_size: float = 0.01
        
        # Event queue
        self.out_queue: asyncio.Queue[Event] = asyncio.Queue(maxsize=10000)
    
    @property
    def market_id(self) -> Optional[str]:
        """Currently subscribed market ID."""
        return self._market_id
    
    @property
    def connected(self) -> bool:
        """Whether connected."""
        return self._connected
    
    @property
    def last_msg_local_ms(self) -> Optional[int]:
        """Timestamp of last message."""
        return self._last_msg_local_ms
    
    def set_tokens(self, token_ids: list[str], market_id: str) -> None:
        """
        Set tokens to subscribe to.
        
        Args:
            token_ids: List of token IDs (YES and NO)
            market_id: Market identifier
        """
        self._token_ids = token_ids
        self._market_id = market_id
    
    async def connect(self) -> None:
        """Establish WebSocket connection."""
        logger.info(f"Connecting to Polymarket market WS: {self._ws_url[:50]}...")
        
        self._ws = await websockets.connect(
            self._ws_url,
            ping_interval=20,
            ping_timeout=60,
            max_size=2**20,
        )
        self._connected = True
        logger.info("Connected to Polymarket market WS")
        
        # Emit connection event
        await self._emit(WsEvent(
            event_type=None,  # Will be set in __post_init__
            ws_name="market",
            connected=True,
        ))
    
    async def subscribe_market(self) -> None:
        """Subscribe to market updates."""
        if not self._ws or not self._token_ids:
            return
        
        # Subscribe to token updates
        msg = {
            "type": "market",
            "assets_ids": self._token_ids,
        }
        await self._ws.send(orjson.dumps(msg))
        logger.info(f"Subscribed to {len(self._token_ids)} tokens")
    
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
            ws_name="market",
            connected=False,
        ))
    
    async def _emit(self, event: Event) -> None:
        """Emit an event."""
        try:
            self.out_queue.put_nowait(event)
        except asyncio.QueueFull:
            logger.warning("Market WS queue full, dropping event")
        
        if self._on_event:
            try:
                await self._on_event(event)
            except Exception as e:
                logger.warning(f"Error in market WS event callback: {e}")
    
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
            logger.warning(f"Failed to parse market WS message: {e}")
        
        return events
    
    def _parse_event(self, data: dict, recv_ts_ms: int) -> list[Event]:
        """Parse a single event."""
        events: list[Event] = []
        event_type = data.get("event_type", "")
        
        if event_type == "book":
            # Full book snapshot - extract BBO
            bids = data.get("bids", [])
            asks = data.get("asks", [])
            
            if bids and asks:
                # Best bid/ask are last in arrays
                best_bid = float(bids[-1]["price"]) if bids else None
                best_ask = float(asks[-1]["price"]) if asks else None
                
                events.append(MarketBboEvent(
                    event_type=None,
                    ts_local_ms=recv_ts_ms,
                    best_bid=best_bid,
                    best_ask=best_ask,
                    tick_size=self._current_tick_size,
                ))
        
        elif event_type == "price_change":
            # Price update - extract BBO
            changes = data.get("price_changes", [])
            best_bid = None
            best_ask = None
            
            for change in changes:
                if change.get("best_bid") is not None:
                    best_bid = float(change["best_bid"])
                if change.get("best_ask") is not None:
                    best_ask = float(change["best_ask"])
            
            if best_bid is not None or best_ask is not None:
                events.append(MarketBboEvent(
                    event_type=None,
                    ts_local_ms=recv_ts_ms,
                    best_bid=best_bid,
                    best_ask=best_ask,
                    tick_size=self._current_tick_size,
                ))
        
        elif event_type == "tick_size_change":
            # Tick size changed
            new_tick_size = data.get("tick_size")
            if new_tick_size is not None:
                self._current_tick_size = float(new_tick_size)
                events.append(TickSizeChangedEvent(
                    event_type=None,
                    ts_local_ms=recv_ts_ms,
                    new_tick_size=self._current_tick_size,
                ))
        
        elif event_type == "best_bid_ask":
            # Direct BBO update
            best_bid = data.get("best_bid")
            best_ask = data.get("best_ask")
            
            events.append(MarketBboEvent(
                event_type=None,
                ts_local_ms=recv_ts_ms,
                best_bid=float(best_bid) if best_bid else None,
                best_ask=float(best_ask) if best_ask else None,
                tick_size=self._current_tick_size,
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
                await self.subscribe_market()
                backoff = 1.0  # Reset backoff on successful connection
                
                async for message in self._ws:
                    if shutdown_event and shutdown_event.is_set():
                        break
                    if not self._running:
                        break
                    
                    events = self.parse_message(message)
                    for event in events:
                        await self._emit(event)
            
            except ConnectionClosed as e:
                logger.warning(f"Market WS connection closed: {e}")
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(f"Market WS error: {e}")
            finally:
                await self.close()
            
            if shutdown_event and shutdown_event.is_set():
                break
            if not self._running:
                break
            
            # Backoff before reconnecting
            logger.info(f"Market WS reconnecting in {backoff:.1f}s...")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, max_backoff)
        
        logger.info("Market WS client stopped")
    
    def stop(self) -> None:
        """Stop the client."""
        self._running = False
