"""
Binance data feeds (threaded).

BinanceFeed: WebSocket feed for real-time BBO and trade data from Binance.
BinancePricerPoller: Optional HTTP poller for pricer enrichment (p_yes, features).
PricerEnrichment: Thread-safe container for pricer-computed fields.
"""

import logging
import threading
import time
from typing import Optional

import orjson
import requests

from .websocket_base import ThreadedWsClient
from ..caches import BinanceCache

logger = logging.getLogger(__name__)

BINANCE_WS_URL = "wss://stream.binance.com:9443/stream"


class PricerEnrichment:
    """
    Thread-safe container for pricer-computed fields.

    Written by BinancePricerPoller, read by BinanceFeed.
    Stores the latest pricer snapshot fields (p_yes, features, hour context)
    so BinanceFeed can merge them into WS-driven snapshots.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._data: dict = {}

    def update(self, raw: dict) -> None:
        """Store latest pricer snapshot."""
        with self._lock:
            self._data = dict(raw)

    def get(self) -> dict:
        """Get latest pricer data (returns copy)."""
        with self._lock:
            return dict(self._data)


class BinanceFeed(ThreadedWsClient):
    """
    Binance WebSocket feed for real-time BBO and trade data.

    Connects to Binance combined stream (bookTicker + trade) and updates
    BinanceCache with sub-millisecond latency on each BBO change.

    Optionally merges pricer enrichment (p_yes, features) from a
    PricerEnrichment container if provided.
    """

    def __init__(
        self,
        cache: BinanceCache,
        symbol: str = "BTCUSDT",
        enrichment: Optional[PricerEnrichment] = None,
        ws_url: str = BINANCE_WS_URL,
    ):
        # Build combined stream URL
        sym = symbol.lower()
        full_url = f"{ws_url}?streams={sym}@bookTicker/{sym}@trade"

        super().__init__(
            ws_url=full_url,
            name="BinanceFeed",
            ping_interval=20,
            ping_timeout=60,
        )

        self._cache = cache
        self._symbol = symbol
        self._enrichment = enrichment

        # Track last trade price (updated on trade, published with next BBO)
        self._last_trade_price: Optional[float] = None

        # Stats
        self._bbo_count = 0
        self._trade_count = 0
        self._error_count = 0
        self._last_error: Optional[str] = None

    def _subscribe(self) -> None:
        """No subscription needed - streams are in the URL path."""
        pass

    def _handle_message(self, data: bytes) -> None:
        """Parse combined stream message and update cache."""
        try:
            msg = orjson.loads(data)
        except Exception as e:
            self._error_count += 1
            self._last_error = f"parse: {e}"
            return

        stream = msg.get("stream", "")
        payload = msg.get("data")
        if payload is None:
            return

        if "bookTicker" in stream:
            self._on_bbo(payload)
        elif "trade" in stream:
            self._on_trade(payload)

    def _on_bbo(self, data: dict) -> None:
        """Handle bookTicker update — publish to cache immediately."""
        self._bbo_count += 1

        try:
            bid = float(data["b"])
            ask = float(data["a"])
        except (KeyError, ValueError, TypeError) as e:
            self._error_count += 1
            self._last_error = f"bbo_parse: {e}"
            return

        # Merge pricer enrichment if available
        enrichment = self._enrichment.get() if self._enrichment else {}

        # Extract pricer fields from enrichment
        features = enrichment.get("features", {})

        self._cache.update_direct(
            symbol=self._symbol,
            best_bid_px=bid,
            best_ask_px=ask,
            last_trade_price=self._last_trade_price,
            # Hour context from pricer (if available)
            open_price=enrichment.get("open_price"),
            hour_start_ts_ms=enrichment.get("hour_start_ts_ms", 0),
            hour_end_ts_ms=enrichment.get("hour_end_ts_ms", 0),
            t_remaining_ms=enrichment.get("t_remaining_ms", 0),
            # Pricer output
            p_yes=enrichment.get("p_yes_fair"),
            # Features
            return_1s=features.get("return_1m") or features.get("return_1s"),
            ewma_vol_1s=features.get("ewma_vol") or features.get("ewma_vol_1s"),
            shock_z=(
                enrichment.get("pricer", {}).get("shock_z")
                if "pricer" in enrichment
                else features.get("shock_z")
            ),
            signed_volume_1s=features.get("signed_volume_1s"),
            p_yes_band_lo=enrichment.get("p_yes_band_lo"),
            p_yes_band_hi=enrichment.get("p_yes_band_hi"),
        )

    def _on_trade(self, data: dict) -> None:
        """Handle trade update — store for next BBO publish."""
        self._trade_count += 1
        try:
            self._last_trade_price = float(data["p"])
        except (KeyError, ValueError, TypeError):
            pass

    @property
    def is_running(self) -> bool:
        """Check if feed is running."""
        return self._thread is not None and self._thread.is_alive()

    @property
    def stats(self) -> dict:
        """Get feed statistics."""
        return {
            "bbo_count": self._bbo_count,
            "trade_count": self._trade_count,
            "error_count": self._error_count,
            "last_error": self._last_error,
            "connected": self._connected,
            "reconnect_count": self._reconnect_count,
        }


class BinancePricerPoller:
    """
    Binance pricer HTTP poller — enriches BinanceFeed with computed features.

    Polls the binance_pricer HTTP endpoint for p_yes, volatility, hour context,
    and stores results in a PricerEnrichment container.

    This is optional. If the pricer is not running, BinanceFeed still provides
    real-time BBO/trade data and the strategy falls back to PM mid for fair value.
    """

    def __init__(
        self,
        enrichment: PricerEnrichment,
        url: str = "http://localhost:8080/snapshot/latest",
        poll_hz: int = 5,
        timeout_s: float = 1.0,
    ):
        self._enrichment = enrichment
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
            logger.warning("BinancePricerPoller already running")
            return

        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run_loop,
            name="BinancePricerPoller",
            daemon=True,
        )
        self._thread.start()
        logger.info(f"BinancePricerPoller started at {1/self._poll_interval:.0f} Hz")

    def stop(self, timeout: float = 2.0) -> None:
        """Stop the poller thread."""
        logger.info("BinancePricerPoller stopping...")
        self._stop_event.set()

        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=timeout)
            if self._thread.is_alive():
                logger.warning("BinancePricerPoller did not stop in time")

        logger.info(
            f"BinancePricerPoller stopped. Polls={self._poll_count}, "
            f"Success={self._success_count}, Errors={self._error_count}"
        )

    def _run_loop(self) -> None:
        """Main polling loop."""
        next_poll = time.monotonic()

        while not self._stop_event.is_set():
            now = time.monotonic()

            if now < next_poll:
                sleep_time = next_poll - now
                if self._stop_event.wait(sleep_time):
                    break

            next_poll = time.monotonic() + self._poll_interval
            self._poll()

    def _poll(self) -> None:
        """Single poll iteration."""
        self._poll_count += 1

        try:
            resp = requests.get(self._url, timeout=self._timeout)

            if resp.status_code == 200:
                data = resp.json()
                self._enrichment.update(data)
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
            if self._error_count % 100 == 1:
                logger.debug(f"BinancePricerPoller: connection error to {self._url}")

        except Exception as e:
            self._error_count += 1
            self._last_error = str(e)
            logger.debug(f"BinancePricerPoller: {e}")

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
