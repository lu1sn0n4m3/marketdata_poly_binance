"""
Utility functions for timestamps and price conversions.

These are pure functions with no dependencies on other types.
"""

from time import monotonic_ns, time
from typing import Optional


def now_ms() -> int:
    """Get current monotonic timestamp in milliseconds."""
    return monotonic_ns() // 1_000_000


def wall_ms() -> int:
    """Get current wall clock timestamp in milliseconds."""
    return int(time() * 1000)


def price_to_cents(price_str: Optional[str]) -> int:
    """Convert price string (e.g., "0.55") to cents."""
    if not price_str:
        return 0
    try:
        return int(float(price_str) * 100)
    except (ValueError, TypeError):
        return 0


def cents_to_price(cents: int) -> float:
    """Convert cents to decimal price."""
    return cents / 100.0
