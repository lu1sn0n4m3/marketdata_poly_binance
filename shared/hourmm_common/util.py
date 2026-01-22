"""Utility functions for the HourMM trading system."""

import logging
from typing import Optional


def setup_logging(
    name: str,
    level: str = "INFO",
    format_str: Optional[str] = None,
) -> logging.Logger:
    """
    Set up structured logging for a component.
    
    Args:
        name: Logger name
        level: Log level (DEBUG, INFO, WARNING, ERROR, CRITICAL)
        format_str: Optional custom format string
    
    Returns:
        Configured logger
    """
    if format_str is None:
        format_str = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
    
    logging.basicConfig(
        level=getattr(logging, level.upper()),
        format=format_str,
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    
    return logging.getLogger(name)


def round_to_tick(price: float, tick_size: float) -> float:
    """
    Round a price to the nearest tick.
    
    Args:
        price: Price to round
        tick_size: Tick size (e.g., 0.01)
    
    Returns:
        Price rounded to nearest tick
    """
    if tick_size <= 0:
        return price
    return round(round(price / tick_size) * tick_size, 10)


def clamp(value: float, min_val: float, max_val: float) -> float:
    """Clamp a value between min and max."""
    return max(min_val, min(max_val, value))
