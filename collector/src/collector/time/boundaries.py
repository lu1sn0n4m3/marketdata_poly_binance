"""UTC hour boundary detection and time utilities."""

from datetime import datetime, timezone, timedelta
from typing import Tuple


def get_current_utc_hour() -> Tuple[str, int]:
    """
    Get current UTC date (YYYY-MM-DD) and hour (0-23).
    
    Returns:
        (date_str, hour) tuple
    """
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%d"), now.hour


def get_utc_hour_for_timestamp(ts_ms: int) -> Tuple[str, int]:
    """
    Get UTC date and hour for a given timestamp.
    
    Args:
        ts_ms: Timestamp in milliseconds
    
    Returns:
        (date_str, hour) tuple
    """
    dt = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
    return dt.strftime("%Y-%m-%d"), dt.hour


def get_next_hour_boundary_utc() -> datetime:
    """
    Get the next UTC hour boundary (e.g., if now is 14:30, return 15:00:00).
    
    Returns:
        datetime object at the next hour boundary in UTC
    """
    now = datetime.now(timezone.utc)
    next_hour = (now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1))
    return next_hour


def seconds_until_next_hour() -> float:
    """
    Get seconds until the next UTC hour boundary.
    
    Returns:
        seconds as float
    """
    next_boundary = get_next_hour_boundary_utc()
    now = datetime.now(timezone.utc)
    delta = (next_boundary - now).total_seconds()
    return max(0.0, delta)
