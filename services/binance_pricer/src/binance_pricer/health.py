"""Health tracking for Container A."""

import logging
from time import time_ns
from typing import Optional

logger = logging.getLogger(__name__)


class HealthTracker:
    """
    Tracks liveness/staleness of the Binance feed.
    
    Used to determine when data is stale and should not be trusted
    for trading decisions.
    """
    
    def __init__(self, stale_threshold_ms: int = 500):
        """
        Initialize the health tracker.
        
        Args:
            stale_threshold_ms: Threshold in ms after which data is considered stale
        """
        self.stale_threshold_ms = stale_threshold_ms
        self._last_binance_event_local_ms: Optional[int] = None
        self._last_binance_event_exchange_ms: Optional[int] = None
    
    @property
    def last_binance_event_local_ms(self) -> Optional[int]:
        """Timestamp of last event (local time)."""
        return self._last_binance_event_local_ms
    
    @property
    def last_binance_event_exchange_ms(self) -> Optional[int]:
        """Timestamp of last event (exchange time)."""
        return self._last_binance_event_exchange_ms
    
    def update_on_event(self, ts_local_ms: int, ts_exchange_ms: Optional[int] = None) -> None:
        """
        Update health state on receiving an event.
        
        Args:
            ts_local_ms: Local receive timestamp
            ts_exchange_ms: Exchange timestamp (if available)
        """
        self._last_binance_event_local_ms = ts_local_ms
        if ts_exchange_ms is not None:
            self._last_binance_event_exchange_ms = ts_exchange_ms
    
    def age_ms(self, now_ms: Optional[int] = None) -> int:
        """
        Get age in milliseconds since last event.
        
        Args:
            now_ms: Current timestamp (defaults to current time)
        
        Returns:
            Age in milliseconds, or a large value if no events received
        """
        if self._last_binance_event_local_ms is None:
            return 999999  # Large value indicating no data
        
        if now_ms is None:
            now_ms = time_ns() // 1_000_000
        
        return now_ms - self._last_binance_event_local_ms
    
    def is_stale(self, now_ms: Optional[int] = None) -> bool:
        """
        Check if data is stale.
        
        Args:
            now_ms: Current timestamp (defaults to current time)
        
        Returns:
            True if data is stale
        """
        return self.age_ms(now_ms) > self.stale_threshold_ms
