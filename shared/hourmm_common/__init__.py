"""HourMM Common - Shared schemas, enums, and utilities for the HourMM trading system."""

from .enums import RiskMode, SessionState, OrderPurpose, Side
from .schemas import BinanceSnapshot, MarketView, TokenIds, MarketInfo
from .time import (
    get_hour_id,
    get_hour_boundaries_ms,
    get_current_hour_id,
    ms_until_hour_end,
)
from .errors import HourMMError, StaleDataError, ConnectionError

__all__ = [
    # Enums
    "RiskMode",
    "SessionState",
    "OrderPurpose",
    "Side",
    # Schemas
    "BinanceSnapshot",
    "MarketView",
    "TokenIds",
    "MarketInfo",
    # Time utilities
    "get_hour_id",
    "get_hour_boundaries_ms",
    "get_current_hour_id",
    "ms_until_hour_end",
    # Errors
    "HourMMError",
    "StaleDataError",
    "ConnectionError",
]
