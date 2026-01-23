"""
Core data types for the market-making system.

All dataclasses use slots=True for memory efficiency.
Prices are stored as integers (cents 0-100) to avoid float issues.
Timestamps use monotonic time for logic, wall time only for reporting.
"""

from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Optional
from time import monotonic_ns, time


# =============================================================================
# ENUMS
# =============================================================================


class Token(Enum):
    """Binary outcome token."""
    YES = auto()
    NO = auto()


class Side(Enum):
    """Order side."""
    BUY = auto()
    SELL = auto()


class OrderStatus(Enum):
    """Order lifecycle status."""
    PENDING_NEW = auto()
    WORKING = auto()
    PENDING_CANCEL = auto()
    PARTIALLY_FILLED = auto()
    FILLED = auto()
    CANCELED = auto()
    REJECTED = auto()
    UNKNOWN = auto()


class QuoteMode(Enum):
    """Strategy quoting mode."""
    STOP = auto()
    NORMAL = auto()
    CAUTION = auto()
    ONE_SIDED_BUY = auto()
    ONE_SIDED_SELL = auto()


class ExecutorEventType(Enum):
    """Event types processed by the Executor."""
    STRATEGY_INTENT = auto()
    ORDER_ACK = auto()
    CANCEL_ACK = auto()
    FILL = auto()
    GATEWAY_RESULT = auto()
    TIMER_TICK = auto()


class GatewayActionType(Enum):
    """Gateway action types."""
    PLACE = auto()
    CANCEL = auto()
    CANCEL_ALL = auto()


# =============================================================================
# SNAPSHOT METADATA
# =============================================================================


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


# =============================================================================
# POLYMARKET BOOK SNAPSHOT
# =============================================================================


@dataclass(slots=True)
class PMBookTop:
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
class PMBookSnapshot:
    """
    Complete Polymarket book snapshot for YES and NO tokens.
    """
    meta: MarketSnapshotMeta
    market_id: str  # Condition ID
    yes_token_id: str
    no_token_id: str
    yes_top: PMBookTop
    no_top: PMBookTop

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


# =============================================================================
# BINANCE SNAPSHOT
# =============================================================================


@dataclass(slots=True)
class BNSnapshot:
    """
    Binance market data snapshot.

    Used for fair value estimation and shock detection.
    """
    meta: MarketSnapshotMeta
    symbol: str
    best_bid_px: float
    best_ask_px: float

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


# =============================================================================
# INVENTORY STATE
# =============================================================================


@dataclass(slots=True)
class InventoryState:
    """
    Current inventory state.

    Tracks YES and NO token holdings.
    """
    I_yes: int = 0  # YES shares held
    I_no: int = 0   # NO shares held
    last_update_ts: int = 0

    @property
    def net_E(self) -> int:
        """
        Net exposure (settlement-relevant).

        Positive = net long YES, negative = net short YES.
        """
        return self.I_yes - self.I_no

    @property
    def gross_G(self) -> int:
        """
        Gross exposure (collateral-relevant).

        Total shares held regardless of side.
        """
        return self.I_yes + self.I_no

    def update_from_fill(
        self,
        token: Token,
        side: Side,
        size: int,
        ts: int,
    ) -> None:
        """Update inventory from a fill."""
        if token == Token.YES:
            if side == Side.BUY:
                self.I_yes += size
            else:
                self.I_yes -= size
        else:  # NO
            if side == Side.BUY:
                self.I_no += size
            else:
                self.I_no -= size
        self.last_update_ts = ts


# =============================================================================
# STRATEGY OUTPUT (INTENT)
# =============================================================================


@dataclass(slots=True)
class DesiredQuoteLeg:
    """
    A single leg of the desired quote.

    Represents either the bid or ask side in YES space.
    """
    enabled: bool
    px_yes: int  # Price in cents (0-100)
    sz: int      # Size in shares


@dataclass(slots=True)
class DesiredQuoteSet:
    """
    Complete desired quote state from Strategy.

    Always expressed in canonical YES space.
    Executor handles materialization to real orders.
    """
    created_at_ts: int  # Monotonic ms when created
    pm_seq: int         # PM feed sequence for freshness
    bn_seq: int         # BN feed sequence for freshness
    mode: QuoteMode
    bid_yes: DesiredQuoteLeg  # Synthetic BUY YES
    ask_yes: DesiredQuoteLeg  # Synthetic SELL YES
    reason_flags: set = field(default_factory=set)  # Logging reasons

    @classmethod
    def stop(cls, ts: int, pm_seq: int = 0, bn_seq: int = 0, reason: str = "STOP") -> "DesiredQuoteSet":
        """Create a STOP intent."""
        return cls(
            created_at_ts=ts,
            pm_seq=pm_seq,
            bn_seq=bn_seq,
            mode=QuoteMode.STOP,
            bid_yes=DesiredQuoteLeg(enabled=False, px_yes=0, sz=0),
            ask_yes=DesiredQuoteLeg(enabled=False, px_yes=0, sz=0),
            reason_flags={reason},
        )


# =============================================================================
# ORDER TYPES
# =============================================================================


@dataclass(slots=True)
class RealOrderSpec:
    """
    Specification for a real order to be placed.

    This is what gets sent to the exchange.
    """
    token: Token
    side: Side
    px: int            # Price in cents
    sz: int            # Size in shares
    time_in_force: str = "GTC"
    client_order_id: str = ""

    @property
    def token_id(self) -> str:
        """Placeholder - actual token ID set by executor."""
        return ""

    def matches(self, other: "RealOrderSpec", price_tol: int = 0, size_tol: int = 5) -> bool:
        """Check if this order matches another within tolerance."""
        if self.token != other.token:
            return False
        if self.side != other.side:
            return False
        if abs(self.px - other.px) > price_tol:
            return False
        if abs(self.sz - other.sz) > size_tol:
            return False
        return True


@dataclass(slots=True)
class WorkingOrder:
    """
    A working order on the exchange.

    Tracks order state and filled quantity.
    """
    client_order_id: str
    server_order_id: str
    order_spec: RealOrderSpec
    status: OrderStatus
    created_ts: int
    last_state_change_ts: int
    filled_sz: int = 0

    @property
    def remaining_sz(self) -> int:
        """Remaining unfilled size."""
        return max(0, self.order_spec.sz - self.filled_sz)

    @property
    def is_terminal(self) -> bool:
        """Check if order is in a terminal state."""
        return self.status in (
            OrderStatus.FILLED,
            OrderStatus.CANCELED,
            OrderStatus.REJECTED,
        )


# =============================================================================
# GATEWAY ACTIONS AND RESULTS
# =============================================================================


@dataclass(slots=True)
class GatewayAction:
    """
    An action to be performed by the Gateway.
    """
    action_type: GatewayActionType
    action_id: str
    order_spec: Optional[RealOrderSpec] = None  # For PLACE
    client_order_id: Optional[str] = None       # For CANCEL
    market_id: Optional[str] = None             # For CANCEL_ALL


@dataclass(slots=True)
class GatewayResult:
    """
    Result of a Gateway action.
    """
    action_id: str
    success: bool
    server_order_id: Optional[str] = None  # For successful PLACE
    error_kind: Optional[str] = None
    retryable: bool = False


# =============================================================================
# EXECUTOR EVENTS
# =============================================================================


@dataclass(slots=True)
class ExecutorEvent:
    """Base class for Executor inbox events."""
    event_type: ExecutorEventType
    ts_local_ms: int


@dataclass(slots=True)
class StrategyIntentEvent(ExecutorEvent):
    """New intent from Strategy."""
    intent: DesiredQuoteSet

    def __post_init__(self):
        self.event_type = ExecutorEventType.STRATEGY_INTENT


@dataclass(slots=True)
class OrderAckEvent(ExecutorEvent):
    """Order placement acknowledged."""
    client_order_id: str
    server_order_id: str
    status: OrderStatus
    side: Side
    price: int
    size: int
    token: Token

    def __post_init__(self):
        self.event_type = ExecutorEventType.ORDER_ACK


@dataclass(slots=True)
class CancelAckEvent(ExecutorEvent):
    """Order cancellation acknowledged."""
    server_order_id: str
    success: bool
    reason: str = ""

    def __post_init__(self):
        self.event_type = ExecutorEventType.CANCEL_ACK


@dataclass(slots=True)
class FillEvent(ExecutorEvent):
    """Order fill event."""
    server_order_id: str
    token: Token
    side: Side
    price: int  # Cents
    size: int   # Shares filled
    fee: float
    ts_exchange: int

    def __post_init__(self):
        self.event_type = ExecutorEventType.FILL


@dataclass(slots=True)
class GatewayResultEvent(ExecutorEvent):
    """Result from Gateway action."""
    action_id: str
    success: bool
    server_order_id: Optional[str] = None
    error_kind: Optional[str] = None
    retryable: bool = False

    def __post_init__(self):
        self.event_type = ExecutorEventType.GATEWAY_RESULT


@dataclass(slots=True)
class TimerTickEvent(ExecutorEvent):
    """Periodic timer tick."""

    def __post_init__(self):
        self.event_type = ExecutorEventType.TIMER_TICK


# =============================================================================
# RISK STATE
# =============================================================================


@dataclass(slots=True)
class RiskState:
    """
    Risk management state tracked by Executor.
    """
    cooldown_until_ts: int = 0
    mode_override: Optional[QuoteMode] = None
    stale_pm: bool = False
    stale_bn: bool = False
    gross_cap_hit: bool = False
    last_cancel_all_ts: int = 0
    error_burst_counter: int = 0

    def is_in_cooldown(self, now_ms: int) -> bool:
        """Check if currently in cooldown."""
        return now_ms < self.cooldown_until_ts

    def set_cooldown(self, now_ms: int, duration_ms: int) -> None:
        """Set cooldown period."""
        self.cooldown_until_ts = now_ms + duration_ms


# =============================================================================
# EXECUTOR CONFIG
# =============================================================================


@dataclass(slots=True)
class ExecutorConfig:
    """Configuration for Executor."""
    # Staleness thresholds
    pm_stale_threshold_ms: int = 500
    bn_stale_threshold_ms: int = 1000

    # Timeouts
    cancel_timeout_ms: int = 5000
    place_timeout_ms: int = 3000

    # Cooldowns
    cooldown_after_cancel_all_ms: int = 3000

    # Caps
    gross_cap: int = 1000
    max_position: int = 500

    # Tolerances for order matching
    price_tolerance_cents: int = 0
    size_tolerance_shares: int = 5


# =============================================================================
# MARKET INFO
# =============================================================================


@dataclass(slots=True)
class MarketInfo:
    """
    Information about a discovered market.
    """
    condition_id: str
    question: str
    slug: str
    yes_token_id: str
    no_token_id: str
    end_time_utc_ms: int
    reference_price: Optional[float] = None  # BTC strike price from question

    @property
    def time_remaining_ms(self) -> int:
        """Time remaining until market end."""
        now_ms = int(time() * 1000)
        return max(0, self.end_time_utc_ms - now_ms)


# =============================================================================
# UTILITY FUNCTIONS
# =============================================================================


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
