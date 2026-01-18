"""In-memory buffers per stream per event type per hour."""

from typing import Dict, List, Tuple
from collections import defaultdict
from time import time_ns


# Buffer key: (venue, stream_id, event_type, date, hour)
BufferKey = Tuple[str, str, str, str, int]


class StreamBuffer:
    """
    Manages in-memory buffers per (venue, stream_id, event_type, date, hour).
    
    Separating by event_type ensures BBO and trade data are never mixed,
    resulting in clean parquet files with no NULL pollution.
    
    Buffers are append-only and cleared only after flush or finalization.
    """
    
    def __init__(
        self,
        flush_threshold_rows: int = 10_000,
        flush_threshold_seconds: float = 300.0,  # 5 minutes
    ):
        self._buffers: Dict[BufferKey, List[dict]] = defaultdict(list)
        self._last_flush_time: Dict[BufferKey, float] = {}
        self._flush_threshold_rows = flush_threshold_rows
        self._flush_threshold_seconds = flush_threshold_seconds
    
    def append(
        self,
        venue: str,
        stream_id: str,
        event_type: str,
        date: str,
        hour: int,
        row: dict,
    ) -> None:
        """
        Append a row to the appropriate buffer.
        
        Args:
            venue: Venue name (e.g., "binance", "polymarket")
            stream_id: Stream identifier (e.g., "BTCUSDT", "bitcoin-up-or-down")
            event_type: Event type ("bbo" or "trade")
            date: UTC date string (YYYY-MM-DD)
            hour: UTC hour (0-23)
            row: Normalized row dictionary
        """
        key = (venue, stream_id, event_type, date, hour)
        
        # Initialize flush time for new buffers
        if key not in self._buffers and key not in self._last_flush_time:
            self._last_flush_time[key] = time_ns() / 1_000_000_000
        
        self._buffers[key].append(row)
    
    def get_buffers_to_flush(self) -> List[Tuple[BufferKey, List[dict]]]:
        """
        Get buffers that should be flushed based on thresholds.
        
        A buffer is flushed when:
        - Row count exceeds threshold (immediate flush for high-volume streams)
        - Time since last flush exceeds threshold
        
        Returns:
            List of (key, rows) tuples for buffers that need flushing.
            Keys are (venue, stream_id, event_type, date, hour).
        """
        now = time_ns() / 1_000_000_000  # seconds
        to_flush = []
        
        for key, rows in list(self._buffers.items()):
            if not rows:
                continue
            
            should_flush = False
            
            # Check row count threshold
            if len(rows) >= self._flush_threshold_rows:
                should_flush = True
            
            # Check time threshold
            if key in self._last_flush_time:
                elapsed = now - self._last_flush_time[key]
                if elapsed >= self._flush_threshold_seconds:
                    should_flush = True
            
            if should_flush:
                to_flush.append((key, rows))
                # Clear buffer and update flush time
                del self._buffers[key]
                self._last_flush_time[key] = now
        
        return to_flush
    
    def get_buffer(
        self,
        venue: str,
        stream_id: str,
        event_type: str,
        date: str,
        hour: int,
    ) -> List[dict]:
        """
        Get all rows for a specific buffer (for finalization).
        
        Returns:
            List of rows, or empty list if buffer doesn't exist.
        """
        key = (venue, stream_id, event_type, date, hour)
        return self._buffers.get(key, [])
    
    def clear_buffer(
        self,
        venue: str,
        stream_id: str,
        event_type: str,
        date: str,
        hour: int,
    ) -> None:
        """Clear a specific buffer (after finalization)."""
        key = (venue, stream_id, event_type, date, hour)
        if key in self._buffers:
            del self._buffers[key]
        if key in self._last_flush_time:
            del self._last_flush_time[key]
    
    def get_all_active_keys(self) -> List[BufferKey]:
        """
        Get all active buffer keys.
        
        Returns:
            List of (venue, stream_id, event_type, date, hour) tuples.
        """
        return list(self._buffers.keys())
    
    def get_buffer_count(self) -> int:
        """Get number of active buffers."""
        return len(self._buffers)
    
    def get_total_rows(self) -> int:
        """Get total number of rows across all buffers."""
        return sum(len(rows) for rows in self._buffers.values())
