"""Base consumer class with common reconnection and watchdog logic."""

import asyncio
import logging
from abc import ABC, abstractmethod
from time import time_ns
from typing import Callable, Optional

import websockets

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


class BaseConsumer(ABC):
    """
    Base class for websocket consumers.
    
    Provides:
    - Exponential backoff reconnection
    - Watchdog timer for stale connection detection
    - Shutdown event handling
    - Common logging patterns
    
    Subclasses must implement:
    - _get_ws_url(): Return websocket URL
    - _on_connect(ws): Called after connection, can send subscription messages
    - _handle_message(raw, recv_ts_ms): Process incoming message
    - _get_name(): Return consumer name for logging
    """
    
    def __init__(
        self,
        on_row: Callable[[dict], None],
        data_timeout_seconds: float = 300.0,  # 5 minutes
    ):
        self.on_row = on_row
        self._data_timeout_seconds = data_timeout_seconds
        
        self._running = False
        self._backoff = ExponentialBackoff()
        self._last_message_time: Optional[float] = None
    
    @abstractmethod
    def _get_ws_url(self) -> str:
        """Return the websocket URL to connect to."""
        ...
    
    @abstractmethod
    async def _on_connect(self, ws: websockets.WebSocketClientProtocol) -> None:
        """
        Called after websocket connection is established.
        
        Use this to send subscription messages.
        """
        ...
    
    @abstractmethod
    def _handle_message(self, raw: bytes, recv_ts_ms: int) -> None:
        """
        Handle an incoming websocket message.
        
        Args:
            raw: Raw message bytes
            recv_ts_ms: Receive timestamp in milliseconds
        """
        ...
    
    @abstractmethod
    def _get_name(self) -> str:
        """Return consumer name for logging."""
        ...
    
    def _emit_row(self, row: dict) -> None:
        """Emit a normalized row to the callback."""
        self.on_row(row)
    
    def _should_reconnect(self) -> bool:
        """
        Check if connection should be closed and reconnected.
        
        Subclasses can override this to signal reconnection needs
        (e.g., when subscriptions change).
        """
        return False
    
    def _get_last_activity_time(self) -> Optional[float]:
        """
        Get the timestamp of last meaningful activity.
        
        Subclasses can override to check emit time instead of receive time.
        Returns None if no activity yet.
        """
        return self._last_message_time
    
    async def _watchdog(
        self,
        ws: websockets.WebSocketClientProtocol,
        shutdown_event: Optional[asyncio.Event],
    ) -> None:
        """
        Watchdog task to detect stale connections.
        
        Closes the websocket if no meaningful activity for too long.
        Uses _get_last_activity_time() which subclasses can override
        to check emit time instead of just receive time.
        """
        while self._running:
            if shutdown_event and shutdown_event.is_set():
                break
            
            await asyncio.sleep(30)  # Check every 30 seconds
            
            last_activity = self._get_last_activity_time()
            if last_activity is None:
                continue
            
            now = time_ns() / 1_000_000_000
            elapsed = now - last_activity
            
            if elapsed > self._data_timeout_seconds:
                logger.warning(
                    f"{self._get_name()}: No activity for {elapsed:.0f}s, reconnecting..."
                )
                await ws.close()
                break
    
    async def run(self, shutdown_event: Optional[asyncio.Event] = None) -> None:
        """
        Run the consumer with automatic reconnection.
        
        Args:
            shutdown_event: Optional event to signal shutdown.
        """
        self._running = True
        
        while self._running:
            if shutdown_event and shutdown_event.is_set():
                break
            
            try:
                url = self._get_ws_url()
                
                async with websockets.connect(
                    url,
                    ping_interval=20,
                    ping_timeout=60,
                    max_size=2**20,
                    compression=None,
                ) as ws:
                    # Reset backoff on successful connection
                    self._backoff.reset()
                    self._last_message_time = time_ns() / 1_000_000_000
                    
                    # Allow subclass to send subscription messages
                    await self._on_connect(ws)
                    
                    logger.info(f"{self._get_name()}: Connected")
                    
                    # Start watchdog
                    watchdog_task = asyncio.create_task(
                        self._watchdog(ws, shutdown_event)
                    )
                    
                    try:
                        async for message in ws:
                            # Update last message time
                            self._last_message_time = time_ns() / 1_000_000_000
                            
                            if shutdown_event and shutdown_event.is_set():
                                break
                            if not self._running:
                                break
                            
                            # Check if subclass wants to reconnect
                            if self._should_reconnect():
                                logger.info(f"{self._get_name()}: Reconnecting (subscription change)")
                                await ws.close()
                                break
                            
                            try:
                                recv_ts_ms = time_ns() // 1_000_000
                                self._handle_message(message, recv_ts_ms)
                            except Exception as e:
                                logger.warning(
                                    f"{self._get_name()}: Error handling message: {e}"
                                )
                    finally:
                        watchdog_task.cancel()
                        try:
                            await watchdog_task
                        except asyncio.CancelledError:
                            pass
            
            except asyncio.CancelledError:
                break
            except Exception as e:
                backoff = self._backoff.next()
                logger.warning(
                    f"{self._get_name()}: Connection error: {e}, "
                    f"reconnecting in {backoff:.1f}s"
                )
            
            if shutdown_event and shutdown_event.is_set():
                break
            if not self._running:
                break
            
            # Wait before reconnecting
            await asyncio.sleep(self._backoff.next())
    
    def stop(self) -> None:
        """Stop the consumer."""
        self._running = False
