"""In-memory buffers per stream per hour."""

from typing import Dict, List, Tuple
from collections import defaultdict
from time import time_ns


BufferKey = Tuple[str, str, str, int]  # (venue, stream_id, date, hour)


class StreamBuffer:
    """
    Manages in-memory buffers per (venue, stream_id, date, hour).
    
    Buffers are append-only and cleared only after flush or finalization.
    """
    
    def __init__(self):
        self._buffers: Dict[BufferKey, List[dict]] = defaultdict(list)
        self._last_flush_time: Dict[BufferKey, float] = {}
        self._flush_threshold_rows = 10_000  # Immediate flush for high-volume streams
        self._flush_threshold_seconds = 300.0  # 5 minutes - reasonable checkpoint frequency
    
    def append(self, venue: str, stream_id: str, date: str, hour: int, row: dict):
        """
        Append a row to the appropriate buffer.
        
        Args:
            venue: venue name
            stream_id: stream identifier
            date: UTC date string (YYYY-MM-DD)
            hour: UTC hour (0-23)
            row: normalized row dictionary
        """
        key = (venue, stream_id, date, hour)
        # If this is a new buffer (was flushed before), reset the flush time
        if key not in self._buffers and key in self._last_flush_time:
            # Buffer was recreated after a flush, reset the timer
            self._last_flush_time[key] = time_ns() / 1_000_000_000
        self._buffers[key].append(row)
    
    def get_buffers_to_flush(self) -> List[Tuple[BufferKey, List[dict]]]:
        """
        Get buffers that should be flushed based on thresholds.
        
        Returns:
            List of (key, rows) tuples for buffers that need flushing
        """
        now = time_ns() / 1_000_000_000  # seconds
        to_flush = []
        
        for key, rows in list(self._buffers.items()):
            should_flush = False
            
            # Check row count threshold
            if len(rows) >= self._flush_threshold_rows:
                should_flush = True
            
            # Check time threshold (every 5 minutes)
            if key in self._last_flush_time:
                elapsed = now - self._last_flush_time[key]
                if elapsed >= self._flush_threshold_seconds:
                    should_flush = True
            else:
                # First time seeing this buffer, mark it
                self._last_flush_time[key] = now
                # Don't flush immediately, wait for threshold
            
            if should_flush:
                to_flush.append((key, rows))
                # Clear buffer and update flush time
                del self._buffers[key]
                self._last_flush_time[key] = now
        
        return to_flush
    
    def get_buffer(self, venue: str, stream_id: str, date: str, hour: int) -> List[dict]:
        """Get all rows for a specific buffer (for finalization)."""
        key = (venue, stream_id, date, hour)
        return self._buffers.get(key, [])
    
    def clear_buffer(self, venue: str, stream_id: str, date: str, hour: int):
        """Clear a specific buffer (after finalization)."""
        key = (venue, stream_id, date, hour)
        if key in self._buffers:
            del self._buffers[key]
        if key in self._last_flush_time:
            del self._last_flush_time[key]
    
    def get_all_active_keys(self) -> List[BufferKey]:
        """Get all active buffer keys (for finalization)."""
        return list(self._buffers.keys())
    
    def get_buffer_count(self) -> int:
        """Get number of active buffers."""
        return len(self._buffers)
