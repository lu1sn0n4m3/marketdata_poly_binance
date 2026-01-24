"""
Normalized error codes for gateway results.

Provides stable error classification independent of raw error messages.
"""

from enum import Enum
from typing import Optional


class ErrorCode(Enum):
    """Normalized error codes for gateway results."""
    SUCCESS = "success"
    INSUFFICIENT_BALANCE = "insufficient_balance"
    RATE_LIMIT = "rate_limit"
    TIMEOUT = "timeout"
    ORDER_NOT_FOUND = "order_not_found"
    INVALID_PRICE = "invalid_price"
    INVALID_SIZE = "invalid_size"
    MARKET_CLOSED = "market_closed"
    MIN_SIZE_VIOLATION = "min_size_violation"
    UNKNOWN = "unknown"


def normalize_error(
    error_msg: Optional[str] = None,
    exception_name: Optional[str] = None,
) -> ErrorCode:
    """
    Map raw error strings to normalized codes.

    Args:
        error_msg: Raw error message from the exchange
        exception_name: Exception class name if available

    Returns:
        Normalized ErrorCode
    """
    msg = (error_msg or "").lower()
    exc = (exception_name or "").lower()

    # Check for insufficient balance
    if any(kw in msg for kw in ("balance", "insufficient", "not enough")):
        return ErrorCode.INSUFFICIENT_BALANCE

    # Check for rate limiting
    if any(kw in msg for kw in ("rate", "limit", "429", "throttle")):
        return ErrorCode.RATE_LIMIT

    # Check for timeout
    if "timeout" in msg or "timeout" in exc:
        return ErrorCode.TIMEOUT

    # Check for order not found
    if any(kw in msg for kw in ("not found", "not exist", "unknown order")):
        return ErrorCode.ORDER_NOT_FOUND

    # Check for invalid price
    if any(kw in msg for kw in ("invalid price", "price out of range", "tick size")):
        return ErrorCode.INVALID_PRICE

    # Check for invalid/min size
    if any(kw in msg for kw in ("minimum", "min size", "size too small")):
        return ErrorCode.MIN_SIZE_VIOLATION
    if any(kw in msg for kw in ("invalid size", "size out of range")):
        return ErrorCode.INVALID_SIZE

    # Check for market closed
    if any(kw in msg for kw in ("closed", "not trading", "halted")):
        return ErrorCode.MARKET_CLOSED

    return ErrorCode.UNKNOWN
