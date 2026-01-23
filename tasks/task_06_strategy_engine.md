# Task 06: Strategy Engine

**Priority:** High (Core Logic)
**Estimated Complexity:** Medium
**Dependencies:** Task 01 (Data Types), Task 02 (Caches)

---

## Objective

Implement the Strategy Engine that reads market snapshots and outputs desired quotes in canonical YES space. The Strategy is **stateless** - it doesn't track orders or pending actions.

---

## Context from Design Documents

From `polymarket_mm_framework.md` Section 3:

> **Strategy Engine (stateless intent generator)**
> - Reads snapshots and a small inventory summary
> - Outputs DesiredQuoteSet in canonical YES space:
>   - mode (NORMAL/CAUTION/ONE_SIDED/STOP)
>   - desired YES bid/ask price + size
> - Does **not** track open orders or pending cancels

From Section 5:

> Strategy produces a `DesiredQuoteSet` with at most two legs:
> - Synthetic YES bid: want to BUY YES at price B
> - Synthetic YES ask: want to SELL YES at price A
>
> Strategy does not output "trade NO". Executor decides how to express the quote legally.

---

## Implementation Checklist

### 1. Strategy Input Context

```python
@dataclass(slots=True)
class StrategyInput:
    """
    All inputs required for Strategy to compute quotes.

    Strategy reads this, outputs DesiredQuoteSet. That's it.
    """
    # Time
    now_ms: int
    t_remaining_ms: int  # Time until hour end

    # Market data (from caches)
    pm_snapshot: Optional[PMBookSnapshot]
    bn_snapshot: Optional[BNSnapshot]
    pm_seq: int
    bn_seq: int

    # Inventory summary (from Executor)
    I_yes: int
    I_no: int
    net_E: int
    gross_G: int

    # Fair price from Binance pricer
    fair_price_yes: Optional[int]  # Cents (0-100)

    # Config
    gross_cap: int
    max_position: int
```

### 2. Strategy Base Class

```python
class Strategy(ABC):
    """
    Base class for trading strategies.

    IMPLEMENT:
        compute_quotes(input: StrategyInput) -> DesiredQuoteSet

    The framework handles everything else.
    """

    @property
    def name(self) -> str:
        return self.__class__.__name__

    @abstractmethod
    def compute_quotes(self, inp: StrategyInput) -> DesiredQuoteSet:
        """
        Compute desired quotes given current state.

        Args:
            inp: All market data, inventory, and config

        Returns:
            DesiredQuoteSet with mode and desired YES bid/ask

        Rules:
        - Return mode=STOP if you don't want to quote
        - Prices are in cents (0-100)
        - Sizes are in shares
        - Don't worry about NO token - Executor handles that
        """
        ...

    def should_emit(self, prev: Optional[DesiredQuoteSet], curr: DesiredQuoteSet) -> bool:
        """
        Check if intent should be emitted (debouncing).

        Override for custom debouncing logic.
        Default: emit if mode or prices changed.
        """
        if prev is None:
            return True
        if prev.mode != curr.mode:
            return True
        if prev.bid_yes.px_yes != curr.bid_yes.px_yes:
            return True
        if prev.ask_yes.px_yes != curr.ask_yes.px_yes:
            return True
        return False
```

### 3. Default Market Making Strategy

```python
class DefaultMMStrategy(Strategy):
    """
    Default market-making strategy.

    Features:
    - Quotes around Binance fair price
    - Widens spread when position is large
    - Switches to CAUTION mode on high volatility
    - Switches to STOP on staleness
    - Reduces size near expiry
    """

    def __init__(
        self,
        base_spread_cents: int = 2,      # 2 cents = 2%
        caution_spread_cents: int = 4,   # 4 cents when cautious
        base_size: int = 100,            # 100 shares
        position_skew_factor: float = 0.01,  # Extra spread per share of position
        vol_caution_threshold: float = 0.03,  # EWMA vol threshold for caution
        near_expiry_ms: int = 5 * 60 * 1000,  # 5 minutes
    ):
        self._base_spread = base_spread_cents
        self._caution_spread = caution_spread_cents
        self._base_size = base_size
        self._position_skew = position_skew_factor
        self._vol_threshold = vol_caution_threshold
        self._near_expiry_ms = near_expiry_ms

    def compute_quotes(self, inp: StrategyInput) -> DesiredQuoteSet:
        now_ms = inp.now_ms

        # Check for STOP conditions
        if self._should_stop(inp):
            return self._stop_intent(inp)

        # Determine mode
        mode = self._determine_mode(inp)

        # Get fair price
        fair = inp.fair_price_yes
        if fair is None:
            return self._stop_intent(inp)

        # Compute spread based on mode
        spread = self._caution_spread if mode == QuoteMode.CAUTION else self._base_spread

        # Position skew: if long, lower bid; if short, raise ask
        skew = int(inp.net_E * self._position_skew)

        # Compute quote prices
        bid_px = fair - spread - max(0, skew)
        ask_px = fair + spread + max(0, -skew)

        # Clamp to valid range
        bid_px = max(1, min(99, bid_px))
        ask_px = max(1, min(99, ask_px))

        # Ensure bid < ask
        if bid_px >= ask_px:
            mid = (bid_px + ask_px) // 2
            bid_px = mid - 1
            ask_px = mid + 1

        # Compute size (reduce near expiry)
        size = self._base_size
        if inp.t_remaining_ms < self._near_expiry_ms:
            size = size // 2

        # Check gross cap - don't increase if near cap
        can_increase_gross = inp.gross_G < inp.gross_cap

        # Check position limits
        bid_enabled = inp.net_E < inp.max_position and can_increase_gross
        ask_enabled = inp.net_E > -inp.max_position and can_increase_gross

        return DesiredQuoteSet(
            created_at_ts=now_ms,
            pm_seq=inp.pm_seq,
            bn_seq=inp.bn_seq,
            mode=mode,
            bid_yes=DesiredQuoteLeg(
                enabled=bid_enabled,
                px_yes=bid_px,
                sz=size if bid_enabled else 0,
            ),
            ask_yes=DesiredQuoteLeg(
                enabled=ask_enabled,
                px_yes=ask_px,
                sz=size if ask_enabled else 0,
            ),
            reason_flags=set(),
        )

    def _should_stop(self, inp: StrategyInput) -> bool:
        """Check if we should stop quoting."""
        # No fair price
        if inp.fair_price_yes is None:
            return True
        # Data too stale (checked elsewhere, but double-check)
        if inp.pm_snapshot is None or inp.bn_snapshot is None:
            return True
        # Gross cap exceeded
        if inp.gross_G >= inp.gross_cap:
            return True
        return False

    def _determine_mode(self, inp: StrategyInput) -> QuoteMode:
        """Determine quoting mode."""
        # Check volatility
        if inp.bn_snapshot and inp.bn_snapshot.ewma_vol_1s:
            if inp.bn_snapshot.ewma_vol_1s > self._vol_threshold:
                return QuoteMode.CAUTION

        # Near expiry -> caution
        if inp.t_remaining_ms < self._near_expiry_ms:
            return QuoteMode.CAUTION

        return QuoteMode.NORMAL

    def _stop_intent(self, inp: StrategyInput) -> DesiredQuoteSet:
        """Return a STOP intent."""
        return DesiredQuoteSet(
            created_at_ts=inp.now_ms,
            pm_seq=inp.pm_seq,
            bn_seq=inp.bn_seq,
            mode=QuoteMode.STOP,
            bid_yes=DesiredQuoteLeg(enabled=False, px_yes=0, sz=0),
            ask_yes=DesiredQuoteLeg(enabled=False, px_yes=0, sz=0),
            reason_flags={"STOP"},
        )
```

### 4. Strategy Runner (Fixed Hz Heartbeat)

```python
class StrategyRunner:
    """
    Runs strategy at fixed frequency and emits intents to Executor.

    From efficiency doc:
    > Strategy should be designed so it *cannot* overwhelm the system:
    > - run on a fixed heartbeat (e.g., 20–50 Hz)
    > - emit intent only when it materially changes
    """

    def __init__(
        self,
        strategy: Strategy,
        pm_cache: PMCache,
        bn_cache: BNCache,
        intent_mailbox: IntentMailbox,
        get_inventory: Callable[[], tuple[int, int]],
        get_fair_price: Callable[[], Optional[int]],
        tick_hz: int = 20,
        gross_cap: int = 1000,
        max_position: int = 500,
    ):
        self._strategy = strategy
        self._pm_cache = pm_cache
        self._bn_cache = bn_cache
        self._intent_mailbox = intent_mailbox
        self._get_inventory = get_inventory
        self._get_fair_price = get_fair_price
        self._tick_hz = tick_hz
        self._gross_cap = gross_cap
        self._max_position = max_position

        self._last_intent: Optional[DesiredQuoteSet] = None
        self._tick_interval_ms = 1000 // tick_hz

    async def run(self, stop_event: asyncio.Event) -> None:
        """Run strategy loop at fixed frequency."""
        while not stop_event.is_set():
            tick_start = time.monotonic_ns() // 1_000_000

            try:
                self._tick()
            except Exception as e:
                logger.error(f"Strategy tick error: {e}")

            # Sleep to maintain fixed Hz
            elapsed = (time.monotonic_ns() // 1_000_000) - tick_start
            sleep_ms = max(0, self._tick_interval_ms - elapsed)
            await asyncio.sleep(sleep_ms / 1000)

    def _tick(self) -> None:
        """Single strategy tick."""
        now_ms = time.monotonic_ns() // 1_000_000

        # Read caches
        pm_snapshot, pm_seq = self._pm_cache.get_latest()
        bn_snapshot, bn_seq = self._bn_cache.get_latest()

        # Check staleness
        if pm_snapshot is None or self._pm_cache.is_stale(now_ms):
            # Emit STOP intent
            self._emit_stop(now_ms, pm_seq, bn_seq)
            return

        if bn_snapshot is None or self._bn_cache.is_stale(now_ms):
            self._emit_stop(now_ms, pm_seq, bn_seq)
            return

        # Get inventory
        I_yes, I_no = self._get_inventory()

        # Get fair price
        fair = self._get_fair_price()

        # Build input
        inp = StrategyInput(
            now_ms=now_ms,
            t_remaining_ms=self._get_t_remaining(now_ms),
            pm_snapshot=pm_snapshot,
            bn_snapshot=bn_snapshot,
            pm_seq=pm_seq,
            bn_seq=bn_seq,
            I_yes=I_yes,
            I_no=I_no,
            net_E=I_yes - I_no,
            gross_G=I_yes + I_no,
            fair_price_yes=fair,
            gross_cap=self._gross_cap,
            max_position=self._max_position,
        )

        # Compute quotes
        intent = self._strategy.compute_quotes(inp)

        # Debounce
        if self._strategy.should_emit(self._last_intent, intent):
            self._intent_mailbox.put(intent)
            self._last_intent = intent

    def _emit_stop(self, now_ms: int, pm_seq: int, bn_seq: int) -> None:
        """Emit STOP intent due to staleness."""
        intent = DesiredQuoteSet(
            created_at_ts=now_ms,
            pm_seq=pm_seq,
            bn_seq=bn_seq,
            mode=QuoteMode.STOP,
            bid_yes=DesiredQuoteLeg(enabled=False, px_yes=0, sz=0),
            ask_yes=DesiredQuoteLeg(enabled=False, px_yes=0, sz=0),
            reason_flags={"STALE_DATA"},
        )
        if self._last_intent is None or self._last_intent.mode != QuoteMode.STOP:
            self._intent_mailbox.put(intent)
            self._last_intent = intent

    def _get_t_remaining(self, now_ms: int) -> int:
        """Get time remaining until hour end."""
        # This should come from market info
        # Placeholder: assume 1 hour from now
        return 60 * 60 * 1000
```

### 5. Intent Mailbox (Coalescing Queue)

```python
class IntentMailbox:
    """
    Single-slot mailbox for strategy intents.

    From efficiency doc:
    > Use a single-slot mailbox (overwriteable) for latest intent
    > If full, replace old intent with newest
    """

    def __init__(self):
        self._latest: Optional[DesiredQuoteSet] = None
        self._lock = threading.Lock()
        self._new_intent = threading.Event()

    def put(self, intent: DesiredQuoteSet) -> None:
        """Put intent, overwriting any existing."""
        with self._lock:
            self._latest = intent
        self._new_intent.set()

    def get(self, timeout: Optional[float] = None) -> Optional[DesiredQuoteSet]:
        """Get latest intent, blocking until available."""
        if self._new_intent.wait(timeout):
            with self._lock:
                intent = self._latest
                self._latest = None
            self._new_intent.clear()
            return intent
        return None

    def get_nowait(self) -> Optional[DesiredQuoteSet]:
        """Get latest intent without blocking."""
        with self._lock:
            intent = self._latest
            self._latest = None
        if intent:
            self._new_intent.clear()
        return intent
```

---

## File Locations

- Create: `services/polymarket_trader/src/polymarket_trader/strategy_engine.py`
- Create: `services/polymarket_trader/src/polymarket_trader/intent_mailbox.py`

---

## Acceptance Criteria

- [ ] Strategy computes quotes in YES-space only
- [ ] Fixed-frequency heartbeat (configurable Hz)
- [ ] Debouncing prevents unnecessary intent emission
- [ ] Intent mailbox coalesces (latest-only)
- [ ] STOP mode emitted on staleness
- [ ] Mode changes based on volatility and time-to-expiry
- [ ] Position skew adjusts spread

---

## Design Notes

From `polymarket_mm_efficiency_design.md` Section 7:

> **Make Strategy fast and stable (debounce + fixed Hz)**
>
> Don't emit a new intent if:
> - desired prices are unchanged, and
> - mode is unchanged, and
> - sizes are unchanged (or within tolerance)
>
> This reduces churn and gateway load.

---

## Testing

Create `tests/test_strategy_engine.py`:
- Test quote computation with various inputs
- Test position skew calculation
- Test mode transitions (NORMAL -> CAUTION)
- Test debouncing logic
- Test staleness triggers STOP
- Test intent mailbox coalescing
