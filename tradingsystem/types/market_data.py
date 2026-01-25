"""
Market data snapshot types.

Types for representing market data from Polymarket and Binance.
"""

from dataclasses import dataclass
from typing import Optional


@dataclass(slots=True)
class MarketSnapshotMeta:
    """
    Metadata for market data snapshots.

    Tracks timing and sequence for staleness detection.
    """
    monotonic_ts: int  # time.monotonic_ns() // 1_000_000
    wall_ts: Optional[int]  # Optional wall clock ms
    feed_seq: int  # Monotonic sequence counter per feed
    source: str  # "PM" or "BN"

    def age_ms(self, now_monotonic_ms: int) -> int:
        """Return age in milliseconds."""
        return now_monotonic_ms - self.monotonic_ts


@dataclass(slots=True)
class PolymarketBookTop:
    """
    Top-of-book for a single token.

    Prices in cents (0-100), sizes in shares.
    """
    best_bid_px: int = 0
    best_bid_sz: int = 0
    best_ask_px: int = 100
    best_ask_sz: int = 0

    @property
    def mid_px(self) -> int:
        """Mid price in cents."""
        if self.best_bid_px == 0 and self.best_ask_px == 100:
            return 50  # No valid quotes
        return (self.best_bid_px + self.best_ask_px) // 2

    @property
    def spread(self) -> int:
        """Spread in cents."""
        return self.best_ask_px - self.best_bid_px

    @property
    def has_bid(self) -> bool:
        """Check if there's a valid bid."""
        return self.best_bid_sz > 0

    @property
    def has_ask(self) -> bool:
        """Check if there's a valid ask."""
        return self.best_ask_sz > 0


@dataclass(slots=True)
class PolymarketBookSnapshot:
    """
    Complete Polymarket book snapshot for YES and NO tokens.
    """
    meta: MarketSnapshotMeta
    market_id: str  # Condition ID
    yes_token_id: str
    no_token_id: str
    yes_top: PolymarketBookTop
    no_top: PolymarketBookTop

    @property
    def yes_mid(self) -> int:
        """YES token mid price in cents."""
        return self.yes_top.mid_px

    @property
    def synthetic_mid(self) -> int:
        """
        Synthetic mid considering both books.

        Uses YES book if available, otherwise derives from NO.
        """
        if self.yes_top.has_bid or self.yes_top.has_ask:
            return self.yes_top.mid_px
        # Derive from NO: YES_mid = 100 - NO_mid
        return 100 - self.no_top.mid_px

    @property
    def arbitrage_yes_top(self) -> PolymarketBookTop:
        """
        Arbitrage-adjusted YES BBO considering both books.

        On Polymarket, YES + NO = 100. So you can effectively:
        - Buy YES at min(YES ask, 100 - NO bid)
        - Sell YES at max(YES bid, 100 - NO ask)

        This gives the true tradable prices, not just raw book levels.
        """
        # Compute arbitrage-adjusted prices
        # YES bid = max(raw YES bid, 100 - NO ask)
        arb_bid_px = max(self.yes_top.best_bid_px, 100 - self.no_top.best_ask_px)
        # YES ask = min(raw YES ask, 100 - NO bid)
        arb_ask_px = min(self.yes_top.best_ask_px, 100 - self.no_top.best_bid_px)

        # Use size from whichever side provides the better price
        if self.yes_top.best_bid_px >= 100 - self.no_top.best_ask_px:
            arb_bid_sz = self.yes_top.best_bid_sz
        else:
            arb_bid_sz = self.no_top.best_ask_sz

        if self.yes_top.best_ask_px <= 100 - self.no_top.best_bid_px:
            arb_ask_sz = self.yes_top.best_ask_sz
        else:
            arb_ask_sz = self.no_top.best_bid_sz

        return PolymarketBookTop(
            best_bid_px=arb_bid_px,
            best_bid_sz=arb_bid_sz,
            best_ask_px=arb_ask_px,
            best_ask_sz=arb_ask_sz,
        )


@dataclass(slots=True)
class BinanceSnapshot:
    """
    Binance market data snapshot.

    Used for fair value estimation and shock detection.
    Includes BBO, trade data, hour context, and derived features.
    """
    meta: MarketSnapshotMeta
    symbol: str
    best_bid_px: float
    best_ask_px: float

    # Trade data
    last_trade_price: Optional[float] = None
    open_price: Optional[float] = None  # Hour open reference price

    # Hour context
    hour_start_ts_ms: int = 0
    hour_end_ts_ms: int = 0
    t_remaining_ms: int = 0

    # Derived features from feature engine (optional)
    return_1s: Optional[float] = None
    ewma_vol_1s: Optional[float] = None
    shock_z: Optional[float] = None
    signed_volume_1s: Optional[float] = None

    # Pricer output (optional)
    p_yes: Optional[float] = None  # Fair probability [0, 1]
    p_yes_band_lo: Optional[float] = None
    p_yes_band_hi: Optional[float] = None

    @property
    def mid_px(self) -> float:
        """Mid price."""
        return (self.best_bid_px + self.best_ask_px) / 2

    @property
    def p_yes_cents(self) -> Optional[int]:
        """Fair probability as cents (0-100)."""
        if self.p_yes is None:
            return None
        return max(1, min(99, int(self.p_yes * 100)))

    @property
    def price_vs_open(self) -> Optional[float]:
        """Current price relative to hour open (as ratio)."""
        if self.open_price is None or self.open_price == 0:
            return None
        return self.mid_px / self.open_price

    @property
    def return_from_open(self) -> Optional[float]:
        """Return from hour open price."""
        ratio = self.price_vs_open
        if ratio is None:
            return None
        return ratio - 1.0
