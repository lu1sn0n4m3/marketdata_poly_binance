"""Type definitions for Container A (Binance Pricer)."""

from dataclasses import dataclass, field
from typing import Optional
from enum import Enum, auto


class BinanceEventType(Enum):
    """Types of Binance events."""
    TRADE = auto()
    BBO = auto()


@dataclass(slots=True)
class BinanceEvent:
    """
    Normalized Binance event (trade or BBO update).
    
    All events carry both exchange and local timestamps.
    """
    event_type: BinanceEventType
    symbol: str
    ts_exchange_ms: int  # Exchange timestamp
    ts_local_ms: int  # Local receive timestamp
    
    # Trade fields (only set for TRADE events)
    price: Optional[float] = None
    size: Optional[float] = None
    side: Optional[str] = None  # "buy" or "sell"
    trade_id: Optional[int] = None
    
    # BBO fields (only set for BBO events)
    bid_price: Optional[float] = None
    bid_size: Optional[float] = None
    ask_price: Optional[float] = None
    ask_size: Optional[float] = None
    update_id: Optional[int] = None


@dataclass(slots=True)
class HourContext:
    """
    Hour context maintained by HourContextBuilder.
    
    Tracks hour boundaries and price references.
    """
    hour_id: str  # e.g., "2026-01-22T14:00:00Z"
    hour_start_ts_ms: int
    hour_end_ts_ms: int
    t_remaining_ms: int  # Time remaining in this hour
    
    open_price: Optional[float] = None  # Immutable once set
    last_trade_price: Optional[float] = None
    last_trade_ts_exchange_ms: Optional[int] = None
    
    # BBO state
    bbo_bid: Optional[float] = None
    bbo_ask: Optional[float] = None
    bbo_ts_exchange_ms: Optional[int] = None
    
    @property
    def mid(self) -> Optional[float]:
        """Compute mid price from BBO."""
        if self.bbo_bid is not None and self.bbo_ask is not None:
            return (self.bbo_bid + self.bbo_ask) / 2
        return self.last_trade_price


@dataclass(slots=True)
class PricerOutput:
    """
    Output from the Pricer component.
    
    Contains fair probability estimate and optional uncertainty bounds.
    """
    p_yes_fair: Optional[float]  # Fair YES probability in [0,1]
    p_yes_band_lo: Optional[float] = None  # Lower uncertainty bound
    p_yes_band_hi: Optional[float] = None  # Upper uncertainty bound
    ready: bool = False  # Whether pricer has enough data


@dataclass(slots=True)
class WsHealth:
    """WebSocket health state."""
    connected: bool = False
    last_event_ts_local_ms: Optional[int] = None
    last_event_ts_exchange_ms: Optional[int] = None
    reconnect_count: int = 0
