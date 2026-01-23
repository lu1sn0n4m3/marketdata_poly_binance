"""
Base WebSocket client utilities for threaded connections.

Uses websocket-client for blocking/threaded WebSocket operations.
"""

import logging
import random
import threading
from typing import Optional, Callable
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

    Provides:
    - Connection management with exponential backoff
    - Run loop in dedicated thread
    - Stop signal handling
    - Reconnection logic
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
        self._ws: Optional[websocket.WebSocket] = None
        self._connected = False
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._backoff = ExponentialBackoff()
        self._reconnect_count = 0

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
                self._connect_and_listen()
            except websocket.WebSocketException as e:
                if not self._stop_event.is_set():
                    self._connected = False
                    self._reconnect_count += 1
                    delay = self._backoff.next_delay()
                    logger.warning(f"{self._name}: Disconnected ({e}), reconnecting in {delay:.1f}s")
                    self._on_disconnect()
                    self._stop_event.wait(timeout=delay)
            except Exception as e:
                if not self._stop_event.is_set():
                    logger.error(f"{self._name}: Error: {e}")
                    self._on_disconnect()
                    self._stop_event.wait(timeout=1.0)

        logger.info(f"{self._name}: Run loop exited")

    def _connect_and_listen(self) -> None:
        """Connect and process messages."""
        logger.info(f"{self._name}: Connecting to {self._ws_url[:60]}...")

        self._ws = websocket.WebSocket()
        self._ws.settimeout(self._ping_timeout)

        self._ws.connect(
            self._ws_url,
            skip_utf8_validation=True,
        )

        self._connected = True
        self._backoff.reset()
        logger.info(f"{self._name}: Connected")

        # Send subscription
        self._subscribe()
        self._on_connect()

        # Message loop
        while not self._stop_event.is_set():
            try:
                # Use timeout to check stop event periodically
                self._ws.settimeout(1.0)
                opcode, data = self._ws.recv_data(control_frame=True)

                if opcode == websocket.ABNF.OPCODE_CLOSE:
                    logger.info(f"{self._name}: Received close frame")
                    break
                elif opcode == websocket.ABNF.OPCODE_PING:
                    self._ws.pong(data)
                elif opcode in (websocket.ABNF.OPCODE_TEXT, websocket.ABNF.OPCODE_BINARY):
                    self._handle_message(data)

            except websocket.WebSocketTimeoutException:
                # Timeout is expected, just check stop event
                continue
            except websocket.WebSocketConnectionClosedException:
                logger.info(f"{self._name}: Connection closed")
                break

        self._connected = False
        try:
            self._ws.close()
        except Exception:
            pass

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
            self._ws.send(data)
