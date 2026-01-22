"""Shared data schemas for the HourMM trading system."""

from dataclasses import dataclass, field
from typing import Optional


@dataclass(frozen=True, slots=True)
class BinanceSnapshot:
    """
    Snapshot from Container A (Binance Pricer) to Container B (Polymarket Trader).
    
    Contract invariants:
    - A snapshot read returns a self-consistent structure (no partially updated fields).
    - seq increases by 1 every publish.
    - stale must be computed in Container A and passed through unchanged.
    """
    # Sequence and timestamps
    seq: int  # Monotonic sequence for snapshots; increments on publish
    ts_local_ms: int  # Local time of snapshot creation (monotonic reference for age)
    ts_exchange_ms: int  # Exchange event time of last incorporated Binance event
    age_ms: int  # Now - last Binance event receive time (local)
    stale: bool  # True if age_ms exceeds configured threshold
    
    # Hour context
    hour_id: str  # UTC hour identifier (e.g., "2026-01-22T14:00:00Z")
    hour_start_ts_ms: int  # Hour start in epoch ms (UTC)
    hour_end_ts_ms: int  # Hour end in epoch ms (UTC)
    t_remaining_ms: int  # Time remaining to hour end (>=0)
    
    # Price data
    open_price: Optional[float]  # Hour open reference; immutable once set
    last_trade_price: Optional[float]  # Latest trade price
    bbo_bid: Optional[float]  # Latest best bid
    bbo_ask: Optional[float]  # Latest best ask
    mid: Optional[float]  # Derived mid price
    
    # Computed features
    features: dict = field(default_factory=dict)  # e.g., realized vol, ewma vol, imbalance
    
    # Pricer output
    p_yes_fair: Optional[float] = None  # Fair YES price/probability in [0,1]
    p_yes_band_lo: Optional[float] = None  # Optional lower uncertainty bound
    p_yes_band_hi: Optional[float] = None  # Optional upper uncertainty bound
    
    def to_dict(self) -> dict:
        """Serialize to dictionary for JSON transport."""
        return {
            "seq": self.seq,
            "ts_local_ms": self.ts_local_ms,
            "ts_exchange_ms": self.ts_exchange_ms,
            "age_ms": self.age_ms,
            "stale": self.stale,
            "hour_id": self.hour_id,
            "hour_start_ts_ms": self.hour_start_ts_ms,
            "hour_end_ts_ms": self.hour_end_ts_ms,
            "t_remaining_ms": self.t_remaining_ms,
            "open_price": self.open_price,
            "last_trade_price": self.last_trade_price,
            "bbo_bid": self.bbo_bid,
            "bbo_ask": self.bbo_ask,
            "mid": self.mid,
            "features": self.features,
            "p_yes_fair": self.p_yes_fair,
            "p_yes_band_lo": self.p_yes_band_lo,
            "p_yes_band_hi": self.p_yes_band_hi,
        }
    
    @classmethod
    def from_dict(cls, data: dict) -> "BinanceSnapshot":
        """Deserialize from dictionary."""
        return cls(
            seq=data["seq"],
            ts_local_ms=data["ts_local_ms"],
            ts_exchange_ms=data["ts_exchange_ms"],
            age_ms=data["age_ms"],
            stale=data["stale"],
            hour_id=data["hour_id"],
            hour_start_ts_ms=data["hour_start_ts_ms"],
            hour_end_ts_ms=data["hour_end_ts_ms"],
            t_remaining_ms=data["t_remaining_ms"],
            open_price=data.get("open_price"),
            last_trade_price=data.get("last_trade_price"),
            bbo_bid=data.get("bbo_bid"),
            bbo_ask=data.get("bbo_ask"),
            mid=data.get("mid"),
            features=data.get("features", {}),
            p_yes_fair=data.get("p_yes_fair"),
            p_yes_band_lo=data.get("p_yes_band_lo"),
            p_yes_band_hi=data.get("p_yes_band_hi"),
        )


@dataclass(slots=True)
class MarketView:
    """Polymarket market view (internal to Container B)."""
    market_id: str  # Identifier for current hour market
    tick_size: float = 0.01  # Current tick size, dynamic
    best_bid: Optional[float] = None  # Current BBO in [0,1]
    best_ask: Optional[float] = None
    book_ts_local_ms: int = 0  # Local time last market update was received
    ws_age_ms: int = 0  # Age since last market WS message


@dataclass(slots=True)
class TokenIds:
    """Token identifiers for a Polymarket market."""
    yes_token_id: str
    no_token_id: str


@dataclass(slots=True)
class MarketInfo:
    """Information about a Polymarket market."""
    condition_id: str
    market_slug: str
    question: str
    tokens: TokenIds
    end_date_iso: Optional[str] = None
    active: bool = True
