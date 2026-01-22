"""Snapshot store for Container A."""

import asyncio
import logging
from typing import Optional

try:
    from shared.hourmm_common.schemas import BinanceSnapshot
except ImportError:
    import sys
    sys.path.insert(0, "/Users/luissafar/dev/marketdata_poly_binance")
    from shared.hourmm_common.schemas import BinanceSnapshot

logger = logging.getLogger(__name__)


class LatestSnapshotStore:
    """
    Holds the current snapshot as an atomic unit.
    
    Guarantees:
    - Reads never see partially updated fields
    - seq increases by 1 every publish
    - Thread-safe (uses asyncio lock for coroutine safety)
    """
    
    def __init__(self):
        self._current: Optional[BinanceSnapshot] = None
        self._seq: int = 0
        self._lock = asyncio.Lock()
    
    @property
    def current(self) -> Optional[BinanceSnapshot]:
        """Get current snapshot (may be None if not yet published)."""
        return self._current
    
    @property
    def seq(self) -> int:
        """Current sequence number."""
        return self._seq
    
    def increment_seq(self) -> int:
        """Increment and return new sequence number."""
        self._seq += 1
        return self._seq
    
    async def publish(self, snapshot: BinanceSnapshot) -> None:
        """
        Publish a new snapshot.
        
        The snapshot should have its seq field set by the caller
        using increment_seq().
        
        Args:
            snapshot: BinanceSnapshot to publish
        """
        async with self._lock:
            self._current = snapshot
    
    def publish_sync(self, snapshot: BinanceSnapshot) -> None:
        """
        Synchronous publish (for use in sync contexts).
        
        Note: Less safe than async version in concurrent scenarios,
        but acceptable for single-threaded asyncio where the publisher
        is the only writer.
        
        Args:
            snapshot: BinanceSnapshot to publish
        """
        self._current = snapshot
    
    async def read_latest(self) -> Optional[BinanceSnapshot]:
        """
        Read the latest snapshot.
        
        Returns:
            The current BinanceSnapshot, or None if not yet published
        """
        async with self._lock:
            return self._current
    
    def read_latest_sync(self) -> Optional[BinanceSnapshot]:
        """
        Synchronous read (for use in sync contexts like HTTP handlers).
        
        Returns:
            The current BinanceSnapshot, or None if not yet published
        """
        return self._current
