# Task 01: Core Data Types and Structures

**Priority:** Critical (Foundation)
**Estimated Complexity:** Medium
**Dependencies:** None (first task)

---

## Objective

Implement all core data types and dataclasses as specified in the framework design. These types form the foundation for all other components.

---

## Context from Design Documents

From `polymarket_mm_framework.md` Section 10 (Data Model):

> These dataclasses define the shared vocabulary between all components. Use `@dataclass(slots=True)` for memory efficiency on hot-path structures.

---

## Implementation Checklist

### 1. Snapshot Metadata (`MarketSnapshotMeta`)

```python
@dataclass(slots=True)
class MarketSnapshotMeta:
    monotonic_ts: int       # time.monotonic_ns() // 1_000_000
    wall_ts: Optional[int]  # Optional wall clock ms
    feed_seq: int           # Monotonic counter per feed
    source: str             # "PM" or "BN"

    def age_ms(self, now_monotonic_ms: int) -> int:
        """Return age in milliseconds."""
        return now_monotonic_ms - self.monotonic_ts
```

### 2. Polymarket Book Snapshot (`PMBookTop`, `PMBookSnapshot`)

```python
@dataclass(slots=True)
class PMBookTop:
    best_bid_px: int        # Cents (0-100)
    best_bid_sz: int        # Shares
    best_ask_px: int        # Cents (0-100)
    best_ask_sz: int        # Shares

    @property
    def mid_px(self) -> int:
        """Mid price in cents."""
        return (self.best_bid_px + self.best_ask_px) // 2

    @property
    def spread(self) -> int:
        """Spread in cents."""
        return self.best_ask_px - self.best_bid_px

@dataclass(slots=True)
class PMBookSnapshot:
    meta: MarketSnapshotMeta
    market_id: str          # Condition ID
    yes_token_id: str
    no_token_id: str
    yes_top: PMBookTop
    no_top: PMBookTop
    # Optional: levels for deeper book, recent_trades ring buffer
```

### 3. Binance Snapshot (`BNSnapshot`)

```python
@dataclass(slots=True)
class BNSnapshot:
    meta: MarketSnapshotMeta
    symbol: str
    best_bid_px: float
    best_ask_px: float

    @property
    def mid_px(self) -> float:
        return (self.best_bid_px + self.best_ask_px) / 2

    # Derived features (optional, from feature engine)
    return_1s: Optional[float] = None
    ewma_vol_1s: Optional[float] = None
    shock_z: Optional[float] = None
    signed_volume_1s: Optional[float] = None
```

### 4. Inventory State

```python
@dataclass(slots=True)
class InventoryState:
    I_yes: int = 0          # YES shares held
    I_no: int = 0           # NO shares held
    last_update_ts: int = 0

    @property
    def net_E(self) -> int:
        """Net exposure (settlement-relevant)."""
        return self.I_yes - self.I_no

    @property
    def gross_G(self) -> int:
        """Gross exposure (collateral-relevant)."""
        return self.I_yes + self.I_no
```

### 5. Strategy Output Types

```python
class QuoteMode(Enum):
    STOP = auto()
    NORMAL = auto()
    CAUTION = auto()
    ONE_SIDED_BUY = auto()
    ONE_SIDED_SELL = auto()

@dataclass(slots=True)
class DesiredQuoteLeg:
    enabled: bool
    px_yes: int             # Cents (0-100)
    sz: int                 # Shares

@dataclass(slots=True)
class DesiredQuoteSet:
    created_at_ts: int      # Monotonic ms
    pm_seq: int             # Feed sequence for freshness
    bn_seq: int
    mode: QuoteMode
    bid_yes: DesiredQuoteLeg
    ask_yes: DesiredQuoteLeg
    reason_flags: set[str]  # For logging
```

### 6. Order Types

```python
class Token(Enum):
    YES = auto()
    NO = auto()

class Side(Enum):
    BUY = auto()
    SELL = auto()

class OrderStatus(Enum):
    WORKING = auto()
    PENDING_NEW = auto()
    PENDING_CANCEL = auto()
    FILLED = auto()
    CANCELED = auto()
    REJECTED = auto()
    UNKNOWN = auto()

@dataclass(slots=True)
class RealOrderSpec:
    token: Token
    side: Side
    px: int                 # Cents
    sz: int                 # Shares
    time_in_force: str = "GTC"
    client_order_id: str = ""

@dataclass(slots=True)
class WorkingOrder:
    client_order_id: str
    order_spec: RealOrderSpec
    status: OrderStatus
    last_state_change_ts: int
    filled_sz: int = 0
```

### 7. Gateway Actions and Results

```python
class GatewayActionType(Enum):
    PLACE = auto()
    CANCEL = auto()
    CANCEL_ALL = auto()

@dataclass(slots=True)
class GatewayAction:
    action_type: GatewayActionType
    action_id: str
    order_spec: Optional[RealOrderSpec] = None
    client_order_id: Optional[str] = None
    event_id: Optional[str] = None

@dataclass(slots=True)
class GatewayResult:
    action_id: str
    success: bool
    error_kind: Optional[str] = None
    retryable: bool = False
```

### 8. Executor Events (Inbox)

```python
class ExecutorEventType(Enum):
    STRATEGY_INTENT = auto()
    ORDER_ACK = auto()
    CANCEL_ACK = auto()
    FILL = auto()
    GATEWAY_ERROR = auto()
    TIMER_TICK = auto()

@dataclass(slots=True)
class ExecutorEvent:
    event_type: ExecutorEventType
    ts_local_ms: int

@dataclass(slots=True)
class StrategyIntentEvent(ExecutorEvent):
    intent: DesiredQuoteSet

@dataclass(slots=True)
class FillEvent(ExecutorEvent):
    order_id: str
    token: Token
    side: Side
    price: int
    size: int
    fee: float
    ts_exchange: int
```

### 9. Risk State

```python
@dataclass(slots=True)
class RiskState:
    cooldown_until_ts: int = 0
    mode_override: Optional[QuoteMode] = None
    stale_pm: bool = False
    stale_bn: bool = False
    gross_cap_hit: bool = False
    last_cancel_all_ts: int = 0
    error_burst_counter: int = 0
```

---

## File Location

Create: `services/polymarket_trader/src/polymarket_trader/mm_types.py`

---

## Acceptance Criteria

- [ ] All dataclasses use `slots=True` for memory efficiency
- [ ] Prices stored as integers (cents) to avoid float issues
- [ ] All timestamps use monotonic time for logic, wall time only for reporting
- [ ] Properties compute derived values (mid, spread, net_E, gross_G)
- [ ] Enums for all categorical values
- [ ] Type hints on all fields

---

## Integration Notes

- Import from `mm_types` in all other new modules
- Keep existing `types.py` for backward compatibility during migration
- These types align with the framework document's data model specification

---

## Testing

Create `tests/test_mm_types.py`:
- Test `age_ms()` calculation
- Test `net_E` and `gross_G` properties
- Test price boundary validation (0-100 cents)
- Test enum serialization for logging
