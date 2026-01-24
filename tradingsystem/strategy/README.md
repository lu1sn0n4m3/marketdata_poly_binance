# Strategy Module

The Strategy module is where trading logic lives. Strategies are **stateless** functions that take market state and output desired quotes.

## Core Concept: Stateless Strategies

The key insight is that strategies don't need to track orders or positions themselves. They simply:

1. Look at the current state (market data, inventory, fair value)
2. Decide what quotes they WANT
3. Let the Executor handle the rest

This separation makes strategies easy to write, test, and reason about.

## Architecture

```
┌────────────────────────────────────────────────────────────────┐
│                      Data Sources                              │
│                                                                │
│  PolymarketCache    BinanceCache    Inventory    Pricer        │
│        │                 │              │           │          │
└────────┼─────────────────┼──────────────┼───────────┼──────────┘
         │                 │              │           │
         ▼                 ▼              ▼           ▼
    ┌─────────────────────────────────────────────────────────┐
    │                    get_input()                          │
    │                                                         │
    │  Assembles StrategyInput from all sources               │
    └─────────────────────────────────────────────────────────┘
                              │
                              ▼
    ┌─────────────────────────────────────────────────────────┐
    │                   StrategyRunner                        │
    │                   (20 Hz loop)                          │
    │                                                         │
    │  1. Calls get_input()                                   │
    │  2. Calls strategy.compute_quotes(input)                │
    │  3. Debounces (skips if unchanged)                      │
    │  4. Puts intent in mailbox                              │
    └─────────────────────────────────────────────────────────┘
                              │
                              ▼
    ┌─────────────────────────────────────────────────────────┐
    │                   IntentMailbox                         │
    │                   (single-slot overwrite)               │
    │                                                         │
    │  Only keeps latest intent - older ones discarded        │
    └─────────────────────────────────────────────────────────┘
                              │
                              ▼
    ┌─────────────────────────────────────────────────────────┐
    │                      Executor                           │
    │                                                         │
    │  Consumes intent and materializes to real orders        │
    └─────────────────────────────────────────────────────────┘
```

## Module Structure

```
strategy/
├── __init__.py         # Public exports
├── README.md           # This file
├── base.py             # Strategy ABC, StrategyInput, StrategyConfig
├── default.py          # DefaultMMStrategy (production-ready)
├── runner.py           # StrategyRunner and IntentMailbox
└── examples/
    ├── __init__.py
    ├── dummy.py        # Safe testing (1-cent orders)
    └── dummy_tight.py  # Live testing (near-market orders)
```

## Class Reference

### `StrategyInput`

All data needed by a strategy to compute quotes:

```python
@dataclass
class StrategyInput:
    pm_book: Optional[PolymarketBookSnapshot]  # Order book
    bn_snap: Optional[BinanceSnapshot]         # BTC price data
    inventory: InventoryState                  # Current position
    fair_px_cents: Optional[int]               # Fair value (0-100)
    t_remaining_ms: int                        # Time to expiry
    pm_seq: int = 0                            # Feed sequence
    bn_seq: int = 0                            # Feed sequence

    # Helpers
    is_data_valid: bool      # Have minimum data?
    near_expiry: bool        # Last 5 minutes?
    very_near_expiry: bool   # Last 1 minute?
```

### `Strategy` (Abstract Base Class)

The interface all strategies must implement:

```python
class Strategy(ABC):
    @abstractmethod
    def compute_quotes(self, inp: StrategyInput) -> DesiredQuoteSet:
        """Compute desired quotes from input."""
        pass
```

### `DesiredQuoteSet`

The output of a strategy:

```python
@dataclass
class DesiredQuoteSet:
    created_at_ts: int       # When created
    pm_seq: int              # PM feed sequence
    bn_seq: int              # BN feed sequence
    mode: QuoteMode          # NORMAL, STOP, ONE_SIDED_*
    bid_yes: DesiredQuoteLeg # Bid in YES-space
    ask_yes: DesiredQuoteLeg # Ask in YES-space
    reason_flags: set        # For logging

@dataclass
class DesiredQuoteLeg:
    enabled: bool            # Is this leg active?
    px_yes: int              # Price in cents (0-100)
    sz: int                  # Size in shares
```

### `IntentMailbox`

Single-slot overwrite mailbox:

```python
mailbox = IntentMailbox()
mailbox.put(intent)           # Overwrites any existing
intent = mailbox.get()        # Returns latest, clears mailbox
intent = mailbox.peek()       # Returns latest, doesn't clear
```

### `StrategyRunner`

Runs strategy at fixed Hz:

```python
runner = StrategyRunner(
    strategy=my_strategy,
    mailbox=intent_mailbox,
    get_input=get_input_fn,   # Callback to get StrategyInput
    tick_hz=20,               # 20 ticks per second
)
runner.start()
# ... runs in background thread ...
runner.stop()

# Stats
runner.stats  # {"ticks": 1000, "intents_produced": 50, ...}
```

## Implementing Your Own Strategy

### Step 1: Subclass Strategy

```python
from tradingsystem.strategy import Strategy, StrategyInput
from tradingsystem.types import DesiredQuoteSet, DesiredQuoteLeg, QuoteMode, now_ms

class MyStrategy(Strategy):
    def __init__(self, spread_cents: int = 5):
        self.spread = spread_cents

    def compute_quotes(self, inp: StrategyInput) -> DesiredQuoteSet:
        ts = now_ms()

        # 1. Check if we have valid data
        if not inp.is_data_valid:
            return DesiredQuoteSet.stop(ts=ts, reason="NO_DATA")

        # 2. Compute your prices
        fair = inp.fair_px_cents
        half_spread = self.spread // 2
        bid_px = max(1, fair - half_spread)
        ask_px = min(99, fair + half_spread)

        # 3. Return desired quotes
        return DesiredQuoteSet(
            created_at_ts=ts,
            pm_seq=inp.pm_seq,
            bn_seq=inp.bn_seq,
            mode=QuoteMode.NORMAL,
            bid_yes=DesiredQuoteLeg(enabled=True, px_yes=bid_px, sz=10),
            ask_yes=DesiredQuoteLeg(enabled=True, px_yes=ask_px, sz=10),
        )
```

### Step 2: Wire It Up

```python
from tradingsystem.strategy import StrategyRunner, IntentMailbox

# Create components
strategy = MyStrategy(spread_cents=4)
mailbox = IntentMailbox()

# Define how to get input
def get_input():
    return StrategyInput(
        pm_book=pm_cache.get_snapshot(),
        bn_snap=bn_cache.get_snapshot(),
        inventory=executor.get_inventory(),
        fair_px_cents=pricer.fair_cents,
        t_remaining_ms=market.time_remaining_ms,
        pm_seq=pm_cache.seq,
        bn_seq=bn_cache.seq,
    )

# Create and start runner
runner = StrategyRunner(strategy, mailbox, get_input, tick_hz=20)
runner.start()

# Executor consumes from mailbox
while running:
    intent = mailbox.get()
    if intent:
        executor.process_intent(intent)
```

### Step 3: Handle Edge Cases

Your strategy should handle:

1. **Missing data**: Return `DesiredQuoteSet.stop(reason="NO_DATA")`
2. **Near expiry**: Consider tighter spreads or stopping
3. **Position limits**: Go one-sided when at max position
4. **Volatility**: Widen spreads in high vol

Example with edge cases:

```python
def compute_quotes(self, inp: StrategyInput) -> DesiredQuoteSet:
    ts = now_ms()

    # No data - stop
    if not inp.is_data_valid:
        return DesiredQuoteSet.stop(ts=ts, reason="NO_DATA")

    # Near expiry - stop
    if inp.very_near_expiry:
        return DesiredQuoteSet.stop(ts=ts, reason="EXPIRY")

    # At max position - go one-sided
    if inp.inventory.net_E >= MAX_POSITION:
        return self._ask_only(inp, ts)
    if inp.inventory.net_E <= -MAX_POSITION:
        return self._bid_only(inp, ts)

    # Normal quoting
    return self._quote_both_sides(inp, ts)
```

## YES-Space: Why It Matters

Strategies always output quotes in **YES-space**:

- `bid_yes`: Price to BUY YES token
- `ask_yes`: Price to SELL YES token

The Executor handles materializing these to actual orders:

```
YES-Space Quote          →    Actual Order
─────────────────────────────────────────────────
bid_yes at 45 cents      →    BUY YES at 0.45
ask_yes at 55 cents      →    SELL YES at 0.55
                         →    (or BUY NO at 0.45)
```

This abstraction means your strategy doesn't need to think about:
- Which token to actually trade (YES or NO)
- Order lifecycle management
- Existing orders that need amending

## QuoteMode: Controlling What Gets Quoted

```python
class QuoteMode(Enum):
    STOP = auto()            # Cancel all, stop quoting
    NORMAL = auto()          # Quote both sides
    CAUTION = auto()         # Quote but be careful (wider spreads)
    ONE_SIDED_BUY = auto()   # Only bid
    ONE_SIDED_SELL = auto()  # Only ask
```

The Executor interprets these:

| Mode | Bid | Ask | Use Case |
|------|-----|-----|----------|
| `STOP` | ❌ | ❌ | Emergency exit, no data |
| `NORMAL` | ✓ | ✓ | Normal trading |
| `ONE_SIDED_BUY` | ✓ | ❌ | Max short, only want to buy |
| `ONE_SIDED_SELL` | ❌ | ✓ | Max long, only want to sell |

## Debouncing: Avoiding Unnecessary Work

The StrategyRunner debounces intents before publishing:

```
Tick 1: bid=45, ask=55  →  PUBLISH (first intent)
Tick 2: bid=45, ask=55  →  skip (unchanged)
Tick 3: bid=45, ask=55  →  skip (unchanged)
Tick 4: bid=46, ask=55  →  PUBLISH (price changed)
Tick 5: bid=46, ask=55  →  skip (unchanged)
```

Changes that trigger publishing:
- Mode changed
- Bid/ask enabled status changed
- Price changed by ≥ 1 cent
- Size changed by ≥ 5 shares

## Example Strategies

### DummyStrategy (Safe Testing)

Places orders at 1 cent that will never fill:

```python
from tradingsystem.strategy import DummyStrategy

strategy = DummyStrategy(size=5)
# Outputs: bid_yes=1c, ask_yes=99c
# These won't fill - good for testing plumbing
```

### DummyTightStrategy (Live Testing)

Places orders at the BBO that WILL fill:

```python
from tradingsystem.strategy import DummyTightStrategy

strategy = DummyTightStrategy(size=5)
# Outputs: bid_yes=best_bid, ask_yes=best_ask
# WARNING: These WILL fill! Use with caution.
```

### DefaultMMStrategy (Production)

Full-featured market-making strategy:

```python
from tradingsystem.strategy import DefaultMMStrategy, StrategyConfig

config = StrategyConfig(
    base_spread_cents=4,
    max_position=200,
    skew_per_share_cents=0.02,
)
strategy = DefaultMMStrategy(config)
# Features: fair value quoting, position skew, vol adjustment
```

## Testing Your Strategy

Strategies are easy to test because they're stateless:

```python
def test_my_strategy():
    strategy = MyStrategy()

    # Create mock input
    inp = StrategyInput(
        pm_book=mock_book,
        bn_snap=mock_snap,
        inventory=InventoryState(I_yes=0, I_no=0),
        fair_px_cents=50,
        t_remaining_ms=3600_000,
    )

    # Compute quotes
    result = strategy.compute_quotes(inp)

    # Assert
    assert result.mode == QuoteMode.NORMAL
    assert result.bid_yes.px_yes == 48
    assert result.ask_yes.px_yes == 52
```

## Common Patterns

### Position Skew

Lean prices to reduce inventory:

```python
def _compute_skew(self, net_pos: int) -> int:
    # Positive position → positive skew → lower quotes → more sells
    skew = int(net_pos * 0.01)  # 1 cent per 100 shares
    return max(-5, min(5, skew))  # Cap at ±5 cents
```

### Volatility-Based Spreads

Widen in high vol:

```python
def _compute_spread(self, inp: StrategyInput) -> int:
    base = 4  # 4 cents base spread
    if inp.bn_snap and inp.bn_snap.shock_z:
        if abs(inp.bn_snap.shock_z) > 2.0:
            return int(base * 1.5)  # 50% wider
    return base
```

### Expiry Behavior

Get more aggressive near expiry:

```python
def compute_quotes(self, inp: StrategyInput) -> DesiredQuoteSet:
    if inp.very_near_expiry:  # Last minute
        return DesiredQuoteSet.stop(reason="EXPIRY")

    if inp.near_expiry:  # Last 5 minutes
        spread = 2  # Tighter spread
        size = 5    # Smaller size
    else:
        spread = 4
        size = 25
```

## Debugging

Enable debug logging:

```python
import logging
logging.getLogger("tradingsystem.strategy").setLevel(logging.DEBUG)
```

Check runner stats:

```python
print(runner.stats)
# {'ticks': 1000, 'intents_produced': 50, 'last_intent_mode': 'NORMAL'}
```

Peek at latest intent:

```python
print(mailbox.peek())
# DesiredQuoteSet(mode=NORMAL, bid_yes=45, ask_yes=55, ...)
```
