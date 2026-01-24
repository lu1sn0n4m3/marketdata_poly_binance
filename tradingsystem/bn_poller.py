"""
Binance snapshot poller (threaded).

Polls the binance_pricer HTTP endpoint and updates BNCache.
Runs in a dedicated thread at configurable Hz.
"""

import logging
import threading
import time
from typing import Optional

import requests

from .bn_cache import BNCache

logger = logging.getLogger(__name__)


class BinanceSnapshotPoller:
    """
    Polls Binance pricer HTTP endpoint and updates BNCache.

    Runs in a dedicated thread, polling at configurable rate.
    Falls back gracefully when endpoint is unavailable.
    """

    def __init__(
        self,
        cache: BNCache,
        url: str = "http://localhost:8080/snapshot/latest",
        poll_hz: int = 20,
        timeout_s: float = 1.0,
    ):
        """
        Initialize poller.

        Args:
            cache: BNCache to update
            url: URL of binance_pricer endpoint
            poll_hz: Polling frequency (default 20 Hz)
            timeout_s: HTTP request timeout
        """
        self._cache = cache
        self._url = url
        self._poll_interval = 1.0 / poll_hz
        self._timeout = timeout_s

        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

        # Stats
        self._poll_count = 0
        self._success_count = 0
        self._error_count = 0
        self._last_error: Optional[str] = None

    def start(self) -> None:
        """Start the poller thread."""
        if self._thread and self._thread.is_alive():
            logger.warning("BinancePoller already running")
            return

        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run_loop,
            name="Binance-Poller",
            daemon=True,
        )
        self._thread.start()
        logger.info(f"BinancePoller started at {1/self._poll_interval:.0f} Hz")

    def stop(self, timeout: float = 2.0) -> None:
        """Stop the poller thread."""
        logger.info("BinancePoller stopping...")
        self._stop_event.set()

        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=timeout)
            if self._thread.is_alive():
                logger.warning("BinancePoller did not stop in time")

        logger.info(
            f"BinancePoller stopped. Polls={self._poll_count}, "
            f"Success={self._success_count}, Errors={self._error_count}"
        )

    def _run_loop(self) -> None:
        """Main polling loop."""
        next_poll = time.monotonic()

        while not self._stop_event.is_set():
            now = time.monotonic()

            # Sleep until next poll
            if now < next_poll:
                sleep_time = next_poll - now
                if self._stop_event.wait(sleep_time):
                    break

            # Schedule next poll
            next_poll = time.monotonic() + self._poll_interval

            # Poll
            self._poll()

    def _poll(self) -> None:
        """Single poll iteration."""
        self._poll_count += 1

        try:
            resp = requests.get(self._url, timeout=self._timeout)

            if resp.status_code == 200:
                data = resp.json()
                self._cache.update_from_poll(data)
                self._success_count += 1
                self._last_error = None
            else:
                self._error_count += 1
                self._last_error = f"HTTP {resp.status_code}"

        except requests.exceptions.Timeout:
            self._error_count += 1
            self._last_error = "timeout"

        except requests.exceptions.ConnectionError:
            self._error_count += 1
            self._last_error = "connection_error"
            # Don't spam logs when service is unavailable
            if self._error_count % 100 == 1:
                logger.debug(f"BinancePoller: connection error to {self._url}")

        except Exception as e:
            self._error_count += 1
            self._last_error = str(e)
            logger.debug(f"BinancePoller: {e}")

    @property
    def is_running(self) -> bool:
        """Check if poller is running."""
        return self._thread is not None and self._thread.is_alive()

    @property
    def stats(self) -> dict:
        """Get poller statistics."""
        return {
            "poll_count": self._poll_count,
            "success_count": self._success_count,
            "error_count": self._error_count,
            "last_error": self._last_error,
            "success_rate": self._success_count / max(1, self._poll_count),
        }

    def set_url(self, url: str) -> None:
        """Update the poll URL (for testing or configuration changes)."""
        self._url = url
