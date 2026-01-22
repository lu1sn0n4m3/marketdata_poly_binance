"""Hour context builder for Container A."""

import logging
from typing import Optional
from time import time_ns

from .types import HourContext, BinanceEvent, BinanceEventType

try:
    from shared.hourmm_common.time import (
        get_hour_id,
        get_hour_boundaries_ms,
        ms_until_hour_end,
        get_current_ms,
    )
except ImportError:
    # Fallback for when running outside container
    import sys
    sys.path.insert(0, "/Users/luissafar/dev/marketdata_poly_binance")
    from shared.hourmm_common.time import (
        get_hour_id,
        get_hour_boundaries_ms,
        ms_until_hour_end,
        get_current_ms,
    )

logger = logging.getLogger(__name__)


class HourContextBuilder:
    """
    Maintains hour boundaries and open price.
    
    Responsibilities:
    - Track current hour ID and boundaries
    - Set open price from first qualifying trade (immutable within hour)
    - Track last trade price
    - Roll to new hour when boundary is crossed
    """
    
    def __init__(self):
        # Current hour state
        self._hour_id: Optional[str] = None
        self._hour_start_ts_ms: int = 0
        self._hour_end_ts_ms: int = 0
        
        # Price state
        self._open_price: Optional[float] = None
        self._last_trade_price: Optional[float] = None
        self._last_trade_ts_exchange_ms: Optional[int] = None
        
        # BBO state
        self._bbo_bid: Optional[float] = None
        self._bbo_ask: Optional[float] = None
        self._bbo_ts_exchange_ms: Optional[int] = None
        
        # Initialize for current hour
        self._initialize_hour(get_current_ms())
    
    @property
    def hour_id(self) -> str:
        """Current hour ID."""
        return self._hour_id or ""
    
    @property
    def hour_start_ts_ms(self) -> int:
        """Hour start timestamp in milliseconds."""
        return self._hour_start_ts_ms
    
    @property
    def hour_end_ts_ms(self) -> int:
        """Hour end timestamp in milliseconds."""
        return self._hour_end_ts_ms
    
    @property
    def open_price(self) -> Optional[float]:
        """Hour open price (immutable once set)."""
        return self._open_price
    
    @property
    def last_trade_price(self) -> Optional[float]:
        """Last trade price observed."""
        return self._last_trade_price
    
    @property
    def last_trade_ts_exchange_ms(self) -> Optional[int]:
        """Timestamp of last trade (exchange time)."""
        return self._last_trade_ts_exchange_ms
    
    def _initialize_hour(self, ts_ms: int) -> None:
        """Initialize hour state for the given timestamp."""
        self._hour_id = get_hour_id(ts_ms)
        self._hour_start_ts_ms, self._hour_end_ts_ms = get_hour_boundaries_ms(ts_ms)
        logger.info(f"Initialized hour context: {self._hour_id}")
    
    def reset_for_new_hour(self, hour_start_ts_ms: int) -> None:
        """
        Reset state for a new hour.
        
        Called when hour boundary is crossed.
        
        Args:
            hour_start_ts_ms: Start timestamp of the new hour
        """
        old_hour_id = self._hour_id
        
        self._hour_id = get_hour_id(hour_start_ts_ms)
        self._hour_start_ts_ms, self._hour_end_ts_ms = get_hour_boundaries_ms(hour_start_ts_ms)
        
        # Reset open price - will be set by first trade
        self._open_price = None
        
        # Keep last trade price for continuity but clear timestamp
        self._last_trade_ts_exchange_ms = None
        
        # Keep BBO state for continuity
        
        logger.info(f"Hour rolled: {old_hour_id} -> {self._hour_id}")
    
    def update_clock(self, now_ts_ms: int) -> None:
        """
        Update clock and roll hour when needed.
        
        Call this periodically to detect hour boundary crossings.
        
        Args:
            now_ts_ms: Current timestamp in milliseconds
        """
        if now_ts_ms >= self._hour_end_ts_ms:
            # Hour boundary crossed, roll to new hour
            self.reset_for_new_hour(now_ts_ms)
    
    def update_from_trade(
        self,
        price: float,
        ts_exchange_ms: int,
        ts_local_ms: int,
    ) -> None:
        """
        Update context from a trade event.
        
        Args:
            price: Trade price
            ts_exchange_ms: Exchange timestamp
            ts_local_ms: Local timestamp
        """
        # Check for hour boundary crossing based on exchange time
        if ts_exchange_ms >= self._hour_end_ts_ms:
            self.reset_for_new_hour(ts_exchange_ms)
        
        # Set open price if not yet set (first trade in hour)
        if self._open_price is None:
            self._open_price = price
            logger.info(f"Hour {self._hour_id} open price set: {price}")
        
        # Always update last trade
        self._last_trade_price = price
        self._last_trade_ts_exchange_ms = ts_exchange_ms
    
    def update_from_bbo(
        self,
        bid_price: float,
        ask_price: float,
        ts_exchange_ms: int,
    ) -> None:
        """
        Update context from a BBO event.
        
        Args:
            bid_price: Best bid price
            ask_price: Best ask price
            ts_exchange_ms: Exchange timestamp
        """
        self._bbo_bid = bid_price
        self._bbo_ask = ask_price
        self._bbo_ts_exchange_ms = ts_exchange_ms
    
    def update_from_event(self, event: BinanceEvent) -> None:
        """
        Update context from a BinanceEvent.
        
        Args:
            event: BinanceEvent (trade or BBO)
        """
        if event.event_type == BinanceEventType.TRADE:
            if event.price is not None:
                self.update_from_trade(
                    price=event.price,
                    ts_exchange_ms=event.ts_exchange_ms,
                    ts_local_ms=event.ts_local_ms,
                )
        elif event.event_type == BinanceEventType.BBO:
            if event.bid_price is not None and event.ask_price is not None:
                self.update_from_bbo(
                    bid_price=event.bid_price,
                    ask_price=event.ask_price,
                    ts_exchange_ms=event.ts_exchange_ms,
                )
    
    def get_context(self, now_ts_ms: int) -> HourContext:
        """
        Get current hour context.
        
        Args:
            now_ts_ms: Current timestamp for computing time remaining
        
        Returns:
            HourContext snapshot
        """
        # Ensure hour is current
        self.update_clock(now_ts_ms)
        
        t_remaining_ms = max(0, self._hour_end_ts_ms - now_ts_ms)
        
        return HourContext(
            hour_id=self._hour_id or "",
            hour_start_ts_ms=self._hour_start_ts_ms,
            hour_end_ts_ms=self._hour_end_ts_ms,
            t_remaining_ms=t_remaining_ms,
            open_price=self._open_price,
            last_trade_price=self._last_trade_price,
            last_trade_ts_exchange_ms=self._last_trade_ts_exchange_ms,
            bbo_bid=self._bbo_bid,
            bbo_ask=self._bbo_ask,
            bbo_ts_exchange_ms=self._bbo_ts_exchange_ms,
        )
