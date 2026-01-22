"""Binance WebSocket client for Container A."""

import asyncio
import logging
from time import time_ns
from typing import Optional, Callable, Awaitable

import orjson
import websockets
from websockets.exceptions import ConnectionClosed

from .types import BinanceEvent, BinanceEventType, WsHealth

logger = logging.getLogger(__name__)


class ExponentialBackoff:
    """Exponential backoff with jitter for reconnection."""
    
    def __init__(self, min_seconds: float = 1.0, max_seconds: float = 60.0):
        self.min_seconds = min_seconds
        self.max_seconds = max_seconds
        self._current = min_seconds
    
    def reset(self) -> None:
        """Reset backoff to minimum."""
        self._current = self.min_seconds
    
    def next(self) -> float:
        """Get next backoff duration and increase for next time."""
        current = self._current
        self._current = min(self._current * 2, self.max_seconds)
        return current


class BinanceWsClient:
    """
    Binance WebSocket client.
    
    Connects to Binance websocket(s) and emits normalized events.
    This is a concrete implementation, not abstract.
    """
    
    def __init__(
        self,
        symbol: str,
        base_url: str = "wss://stream.binance.com:9443/stream",
        on_event: Optional[Callable[[BinanceEvent], Awaitable[None]]] = None,
    ):
        self.symbol = symbol.upper()
        self._symbol_lower = symbol.lower()
        self._base_url = base_url
        self._on_event = on_event
        
        # Connection state
        self._connected = False
        self._running = False
        self._ws: Optional[websockets.WebSocketClientProtocol] = None
        
        # Health tracking
        self.health = WsHealth()
        
        # Event queue for consumers
        self.out_queue: asyncio.Queue[BinanceEvent] = asyncio.Queue(maxsize=10000)
        
        # Backoff for reconnection
        self._backoff = ExponentialBackoff()
    
    @property
    def connected(self) -> bool:
        """Whether the WebSocket is connected."""
        return self._connected
    
    @property
    def last_event_ts_exchange_ms(self) -> Optional[int]:
        """Timestamp of last event (exchange time)."""
        return self.health.last_event_ts_exchange_ms
    
    @property
    def last_event_ts_local_ms(self) -> Optional[int]:
        """Timestamp of last event (local time)."""
        return self.health.last_event_ts_local_ms
    
    def _get_ws_url(self) -> str:
        """Build the WebSocket URL with stream subscriptions."""
        streams = f"{self._symbol_lower}@bookTicker/{self._symbol_lower}@trade"
        return f"{self._base_url}?streams={streams}"
    
    def parse_message(self, raw: bytes) -> list[BinanceEvent]:
        """
        Parse a raw WebSocket message into normalized events.
        
        Args:
            raw: Raw message bytes
        
        Returns:
            List of BinanceEvent objects (may be empty if parse fails)
        """
        events = []
        recv_ts_ms = time_ns() // 1_000_000
        
        try:
            data = orjson.loads(raw)
            stream_name: str = data.get("stream", "")
            payload: dict = data.get("data", {})
            
            if not stream_name or not payload:
                return events
            
            symbol = payload.get("s", "")
            
            if stream_name.endswith("trade"):
                # Trade event
                side = "sell" if payload.get("m", False) else "buy"
                events.append(BinanceEvent(
                    event_type=BinanceEventType.TRADE,
                    symbol=symbol,
                    ts_exchange_ms=payload.get("E", recv_ts_ms),
                    ts_local_ms=recv_ts_ms,
                    price=float(payload["p"]),
                    size=float(payload["q"]),
                    side=side,
                    trade_id=payload.get("t"),
                ))
            elif stream_name.endswith("bookTicker"):
                # BBO event
                events.append(BinanceEvent(
                    event_type=BinanceEventType.BBO,
                    symbol=symbol,
                    ts_exchange_ms=payload.get("E", recv_ts_ms),
                    ts_local_ms=recv_ts_ms,
                    bid_price=float(payload["b"]),
                    bid_size=float(payload["B"]),
                    ask_price=float(payload["a"]),
                    ask_size=float(payload["A"]),
                    update_id=payload.get("u"),
                ))
        except Exception as e:
            logger.warning(f"Failed to parse Binance message: {e}")
        
        return events
    
    async def emit(self, event: BinanceEvent) -> None:
        """
        Emit an event to the queue and callback.
        
        Args:
            event: BinanceEvent to emit
        """
        # Update health
        self.health.last_event_ts_exchange_ms = event.ts_exchange_ms
        self.health.last_event_ts_local_ms = event.ts_local_ms
        
        # Put in queue (non-blocking, drop if full)
        try:
            self.out_queue.put_nowait(event)
        except asyncio.QueueFull:
            logger.warning("Event queue full, dropping event")
        
        # Call callback if set
        if self._on_event:
            try:
                await self._on_event(event)
            except Exception as e:
                logger.warning(f"Error in event callback: {e}")
    
    async def connect(self) -> None:
        """Establish WebSocket connection."""
        url = self._get_ws_url()
        logger.info(f"Connecting to Binance: {url[:80]}...")
        
        self._ws = await websockets.connect(
            url,
            ping_interval=20,
            ping_timeout=60,
            max_size=2**20,
            compression=None,
        )
        self._connected = True
        self.health.connected = True
        self._backoff.reset()
        
        logger.info(f"Connected to Binance for {self.symbol}")
    
    async def subscribe(self) -> None:
        """
        Subscribe to streams.
        
        For Binance combined streams, subscription happens via URL,
        so this is a no-op but kept for interface consistency.
        """
        pass
    
    async def close(self) -> None:
        """Close the WebSocket connection."""
        self._connected = False
        self.health.connected = False
        
        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None
    
    async def run(self, shutdown_event: Optional[asyncio.Event] = None) -> None:
        """
        Main loop: reads WS frames, parses, emits.
        
        Handles reconnection with exponential backoff.
        """
        self._running = True
        
        while self._running:
            if shutdown_event and shutdown_event.is_set():
                break
            
            try:
                await self.connect()
                await self.subscribe()
                
                async for message in self._ws:
                    if shutdown_event and shutdown_event.is_set():
                        break
                    if not self._running:
                        break
                    
                    events = self.parse_message(message)
                    for event in events:
                        await self.emit(event)
            
            except ConnectionClosed as e:
                logger.warning(f"Binance connection closed: {e}")
                self.health.reconnect_count += 1
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(f"Binance error: {e}")
                self.health.reconnect_count += 1
            finally:
                await self.close()
            
            if shutdown_event and shutdown_event.is_set():
                break
            if not self._running:
                break
            
            # Wait before reconnecting
            backoff = self._backoff.next()
            logger.info(f"Reconnecting in {backoff:.1f}s...")
            await asyncio.sleep(backoff)
        
        logger.info("Binance WS client stopped")
    
    def stop(self) -> None:
        """Stop the client."""
        self._running = False
