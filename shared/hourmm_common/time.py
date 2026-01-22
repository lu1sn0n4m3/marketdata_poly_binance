"""Time utilities for the HourMM trading system."""

from datetime import datetime, timezone, timedelta
from typing import Tuple
import time


def get_current_ms() -> int:
    """Get current time in milliseconds since epoch."""
    return int(time.time() * 1000)


def get_hour_id(ts_ms: int) -> str:
    """
    Get hour identifier string from timestamp.
    
    Args:
        ts_ms: Timestamp in milliseconds
    
    Returns:
        Hour ID in ISO format, e.g., "2026-01-22T14:00:00Z"
    """
    dt = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
    hour_start = dt.replace(minute=0, second=0, microsecond=0)
    return hour_start.strftime("%Y-%m-%dT%H:%M:%SZ")


def get_current_hour_id() -> str:
    """Get the current hour ID."""
    return get_hour_id(get_current_ms())


def get_hour_boundaries_ms(ts_ms: int) -> Tuple[int, int]:
    """
    Get hour start and end timestamps for a given timestamp.
    
    Args:
        ts_ms: Timestamp in milliseconds
    
    Returns:
        Tuple of (hour_start_ts_ms, hour_end_ts_ms)
    """
    dt = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
    hour_start = dt.replace(minute=0, second=0, microsecond=0)
    hour_end = hour_start + timedelta(hours=1)
    return (
        int(hour_start.timestamp() * 1000),
        int(hour_end.timestamp() * 1000),
    )


def ms_until_hour_end(ts_ms: int) -> int:
    """
    Get milliseconds remaining until end of current hour.
    
    Args:
        ts_ms: Current timestamp in milliseconds
    
    Returns:
        Milliseconds remaining (>= 0)
    """
    _, hour_end_ms = get_hour_boundaries_ms(ts_ms)
    return max(0, hour_end_ms - ts_ms)


def get_next_hour_boundary_ms() -> int:
    """Get the next hour boundary timestamp in milliseconds."""
    now_ms = get_current_ms()
    _, hour_end_ms = get_hour_boundaries_ms(now_ms)
    return hour_end_ms


def seconds_until_next_hour() -> float:
    """Get seconds until the next UTC hour boundary."""
    now_ms = get_current_ms()
    remaining_ms = ms_until_hour_end(now_ms)
    return remaining_ms / 1000.0


def hour_id_to_timestamp_ms(hour_id: str) -> int:
    """
    Convert hour ID string to timestamp in milliseconds.
    
    Args:
        hour_id: Hour ID in ISO format, e.g., "2026-01-22T14:00:00Z"
    
    Returns:
        Timestamp in milliseconds
    """
    dt = datetime.fromisoformat(hour_id.replace("Z", "+00:00"))
    return int(dt.timestamp() * 1000)
