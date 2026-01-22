"""Snapshot poller for Container B - polls Container A for latest snapshot."""

import asyncio
import logging
from time import time_ns
from typing import Optional

import aiohttp
import orjson

from .types import Event, BinanceSnapshotEvent, BinanceSnapshot

logger = logging.getLogger(__name__)


class SnapshotPoller:
    """
    Polls Container A for the latest Binance snapshot at a fixed rate.
    
    Pull-based to keep execution deterministic and prevent
    event-driven re-entrancy.
    """
    
    def __init__(
        self,
        url: str = "http://binance_pricer:8080/snapshot/latest",
        poll_hz: int = 50,
        timeout_seconds: float = 2.0,
    ):
        """
        Initialize the poller.
        
        Args:
            url: URL to poll for snapshot
            poll_hz: Polling rate in Hz
            timeout_seconds: Request timeout
        """
        self.url = url
        self.poll_hz = poll_hz
        self.timeout_seconds = timeout_seconds
        
        self._running = False
        self._session: Optional[aiohttp.ClientSession] = None
        
        # Latest snapshot
        self._latest: Optional[BinanceSnapshot] = None
        
        # Event queue
        self.out_queue: asyncio.Queue[Event] = asyncio.Queue(maxsize=100)
    
    @property
    def latest(self) -> Optional[BinanceSnapshot]:
        """Latest fetched snapshot."""
        return self._latest
    
    async def _get_session(self) -> aiohttp.ClientSession:
        """Get or create HTTP session."""
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=self.timeout_seconds)
            self._session = aiohttp.ClientSession(timeout=timeout)
        return self._session
    
    async def close(self) -> None:
        """Close the HTTP session."""
        if self._session and not self._session.closed:
            await self._session.close()
    
    async def fetch_latest(self) -> Optional[BinanceSnapshot]:
        """
        Fetch the latest snapshot from Container A.
        
        Returns:
            BinanceSnapshot or None on error
        """
        try:
            session = await self._get_session()
            
            async with session.get(self.url) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return BinanceSnapshot.from_dict(data)
                else:
                    logger.warning(f"Snapshot fetch returned {resp.status}")
                    return None
        
        except asyncio.TimeoutError:
            logger.warning("Snapshot fetch timed out")
            return None
        
        except Exception as e:
            logger.warning(f"Snapshot fetch error: {e}")
            return None
    
    async def emit(self, snapshot: BinanceSnapshot) -> None:
        """
        Emit a snapshot event.
        
        Args:
            snapshot: BinanceSnapshot to emit
        """
        event = BinanceSnapshotEvent(
            event_type=None,
            ts_local_ms=time_ns() // 1_000_000,
            snapshot=snapshot,
        )
        
        try:
            self.out_queue.put_nowait(event)
        except asyncio.QueueFull:
            # Drop oldest and add new
            try:
                self.out_queue.get_nowait()
                self.out_queue.put_nowait(event)
            except asyncio.QueueEmpty:
                pass
    
    async def run(self, shutdown_event: Optional[asyncio.Event] = None) -> None:
        """
        Main polling loop.
        
        Fetches snapshots at the configured rate.
        """
        self._running = True
        interval_seconds = 1.0 / self.poll_hz
        
        logger.info(f"Snapshot poller started at {self.poll_hz} Hz -> {self.url}")
        
        while self._running:
            if shutdown_event and shutdown_event.is_set():
                break
            
            loop_start = time_ns()
            
            try:
                snapshot = await self.fetch_latest()
                
                if snapshot:
                    self._latest = snapshot
                    await self.emit(snapshot)
            
            except Exception as e:
                logger.error(f"Poller error: {e}")
            
            # Sleep to maintain rate
            elapsed_seconds = (time_ns() - loop_start) / 1_000_000_000
            sleep_seconds = max(0, interval_seconds - elapsed_seconds)
            
            if sleep_seconds > 0:
                await asyncio.sleep(sleep_seconds)
        
        await self.close()
        logger.info("Snapshot poller stopped")
    
    def stop(self) -> None:
        """Stop the poller."""
        self._running = False
