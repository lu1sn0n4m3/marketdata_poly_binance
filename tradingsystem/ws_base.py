"""
Base WebSocket client utilities for threaded connections.

Uses websocket-client WebSocketApp for proper event-driven WebSocket handling.
"""

import logging
import random
import threading
import time
from typing import Optional
from abc import ABC, abstractmethod

import websocket

logger = logging.getLogger(__name__)


class ExponentialBackoff:
    """Exponential backoff with jitter for reconnection."""

    def __init__(self, min_seconds: float = 1.0, max_seconds: float = 60.0):
        self.min_seconds = min_seconds
        self.max_seconds = max_seconds
        self._attempts = 0
        self._lock = threading.Lock()

    def next_delay(self) -> float:
        """Get next delay with exponential backoff and jitter."""
        with self._lock:
            delay = min(self.min_seconds * (2 ** self._attempts), self.max_seconds)
            self._attempts += 1
            # Add jitter (50-100% of delay)
            return delay * (0.5 + random.random() * 0.5)

    def reset(self) -> None:
        """Reset attempt counter."""
        with self._lock:
            self._attempts = 0


class ThreadedWsClient(ABC):
    """
    Base class for threaded WebSocket clients.

    Uses WebSocketApp for proper event-driven handling with automatic
    ping/pong and reconnection support.
    """

    def __init__(
        self,
        ws_url: str,
        name: str = "WS",
        ping_interval: int = 20,
        ping_timeout: int = 60,
    ):
        self._ws_url = ws_url
        self._name = name
        self._ping_interval = ping_interval
        self._ping_timeout = ping_timeout

        # Connection state
        self._ws: Optional[websocket.WebSocketApp] = None
        self._connected = False
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._backoff = ExponentialBackoff()
        self._reconnect_count = 0

        # Message queue for sending (thread-safe)
        self._send_queue: list[bytes] = []
        self._send_lock = threading.Lock()

    @property
    def connected(self) -> bool:
        """Check if connected."""
        return self._connected

    @property
    def reconnect_count(self) -> int:
        """Number of reconnections."""
        return self._reconnect_count

    def start(self) -> None:
        """Start the WebSocket client in a background thread."""
        if self._thread and self._thread.is_alive():
            logger.warning(f"{self._name}: Already running")
            return

        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run_loop,
            name=f"{self._name}-thread",
            daemon=True,
        )
        self._thread.start()
        logger.info(f"{self._name}: Started background thread")

    def stop(self, timeout: float = 5.0) -> None:
        """Stop the WebSocket client."""
        logger.info(f"{self._name}: Stopping...")
        self._stop_event.set()

        if self._ws:
            try:
                self._ws.close()
            except Exception:
                pass

        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=timeout)
            if self._thread.is_alive():
                logger.warning(f"{self._name}: Thread did not stop in time")

        self._connected = False
        logger.info(f"{self._name}: Stopped")

    def _run_loop(self) -> None:
        """Main run loop with reconnection."""
        while not self._stop_event.is_set():
            try:
                self._connect_and_run()
            except Exception as e:
                if not self._stop_event.is_set():
                    logger.error(f"{self._name}: Error: {e}")

            if not self._stop_event.is_set():
                self._connected = False
                self._reconnect_count += 1
                delay = self._backoff.next_delay()
                logger.warning(f"{self._name}: Reconnecting in {delay:.1f}s")
                self._on_disconnect()
                self._stop_event.wait(timeout=delay)

        logger.info(f"{self._name}: Run loop exited")

    def _connect_and_run(self) -> None:
        """Create WebSocketApp and run it."""
        logger.info(f"{self._name}: Connecting to {self._ws_url[:60]}...")

        self._ws = websocket.WebSocketApp(
            self._ws_url,
            on_open=self._ws_on_open,
            on_message=self._ws_on_message,
            on_error=self._ws_on_error,
            on_close=self._ws_on_close,
            on_ping=self._ws_on_ping,
        )

        # run_forever blocks until connection closes
        # Note: websocket-client requires ping_interval > ping_timeout
        # We use ping_interval for keepalive, ping_timeout=10 for response wait
        self._ws.run_forever(
            ping_interval=self._ping_interval,
            ping_timeout=10,
            skip_utf8_validation=True,
        )

    def _ws_on_open(self, ws) -> None:
        """WebSocketApp open callback."""
        logger.info(f"{self._name}: Connected")
        self._connected = True
        self._backoff.reset()

        # Send subscription
        self._subscribe()
        self._on_connect()

    def _ws_on_message(self, ws, message) -> None:
        """WebSocketApp message callback."""
        # Message can be str or bytes
        if isinstance(message, str):
            message = message.encode('utf-8')
        self._handle_message(message)

    def _ws_on_error(self, ws, error) -> None:
        """WebSocketApp error callback."""
        if not self._stop_event.is_set():
            logger.warning(f"{self._name}: WebSocket error: {error}")

    def _ws_on_close(self, ws, close_status_code, close_msg) -> None:
        """WebSocketApp close callback."""
        self._connected = False
        if not self._stop_event.is_set():
            logger.info(f"{self._name}: Connection closed (code={close_status_code})")

    def _ws_on_ping(self, ws, message) -> None:
        """WebSocketApp ping callback - pong is automatic."""
        pass  # WebSocketApp handles pong automatically

    @abstractmethod
    def _subscribe(self) -> None:
        """Send subscription message. Override in subclass."""
        pass

    @abstractmethod
    def _handle_message(self, data: bytes) -> None:
        """Handle incoming message. Override in subclass."""
        pass

    def _on_connect(self) -> None:
        """Called after successful connection. Override if needed."""
        pass

    def _on_disconnect(self) -> None:
        """Called after disconnection. Override if needed."""
        pass

    def _send(self, data: bytes) -> None:
        """Send data to WebSocket."""
        if self._ws and self._connected:
            try:
                self._ws.send(data)
            except Exception as e:
                logger.warning(f"{self._name}: Send error: {e}")
