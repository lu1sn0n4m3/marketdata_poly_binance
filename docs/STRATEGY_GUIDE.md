# Strategy Implementation Guide

How to implement your own trading strategy for the Polymarket trading system.

## Overview

The system is designed to be **simple**:

```
┌─────────────────────────────────────────────────────────────────┐
│                      YOUR STRATEGY                               │
│                                                                  │
│   Input:  StrategyContext (position, prices, fair value)        │
│   Output: TargetQuotes (bid price/size, ask price/size)         │
│                                                                  │
└─────────────────────────────────────────────────────────────────┘
```

**You don't need to worry about:**
- Order IDs or order management
- Cancelling old orders
- Race conditions
- WebSocket handling
- State synchronization

**The framework handles all of that.** You just output: *"I want to bid at X, ask at Y"*.

---

## Quick Start

### 1. Create Your Strategy File

```python
# my_strategy.py
from polymarket_trader.simple_strategy import SimpleStrategy, StrategyContext, TargetQuotes

class MyStrategy(SimpleStrategy):
    
    def compute_quotes(self, ctx: StrategyContext) -> TargetQuotes:
        # Your logic here
        return TargetQuotes(
            bid_price=0.45,
            bid_size=10.0,
            ask_price=0.55,
            ask_size=10.0,
        )
```

### 2. That's It!

The executor will:
- Cancel any orders NOT at $0.45 bid / $0.55 ask
- Place orders to match your targets
- Handle all the complexity

---

## StrategyContext - What You Get

Every tick, your `compute_quotes()` receives a `StrategyContext` with:

```python
@dataclass
class StrategyContext:
    # Time
    now_ms: int                    # Current timestamp
    t_remaining_ms: int            # Time until hour end
    
    # YOUR POSITION (updated by WS when fills happen)
    position: float                # Net YES tokens (+ long, - short)
    
    # MARKET PRICES (from Polymarket book)
    market_bid: float | None       # Best bid
    market_ask: float | None       # Best ask
    tick_size: float               # Usually 0.01
    
    # FAIR PRICE (from your pricer/model)
    fair_price: float | None       # Model's fair value
    btc_price: float | None        # Current BTC price
    
    # YOUR CURRENT ORDERS (from WS)
    open_bid_price: float | None   # Your current bid
    open_bid_size: float           
    open_ask_price: float | None   # Your current ask
    open_ask_size: float
    
    # Token IDs
    yes_token_id: str
    no_token_id: str
```

---

## TargetQuotes - What You Return

```python
@dataclass
class TargetQuotes:
    bid_price: float | None = None   # Set to None = don't bid
    bid_size: float = 0.0
    ask_price: float | None = None   # Set to None = don't ask
    ask_size: float = 0.0
```

### Examples:

```python
# Quote both sides
return TargetQuotes(bid_price=0.45, bid_size=10, ask_price=0.55, ask_size=10)

# Only bid (no ask)
return TargetQuotes(bid_price=0.45, bid_size=10)

# Don't quote at all
return TargetQuotes()
```

---

## Example Strategies

### 1. Simple Market Maker

```python
class SimpleMarketMaker(SimpleStrategy):
    """Quote around fair price with position-based skew."""
    
    def __init__(self, spread=0.02, size=10.0, max_pos=50.0):
        self.spread = spread
        self.size = size
        self.max_pos = max_pos
    
    def compute_quotes(self, ctx: StrategyContext) -> TargetQuotes:
        if ctx.fair_price is None:
            return TargetQuotes()  # Can't quote without fair price
        
        fair = ctx.fair_price
        pos = ctx.position
        
        # Base quotes
        bid = fair - self.spread
        ask = fair + self.spread
        
        # Skew if position large (to mean-revert)
        if pos > self.max_pos * 0.5:
            bid -= 0.01  # Lower bid to reduce long
        elif pos < -self.max_pos * 0.5:
            ask += 0.01  # Raise ask to reduce short
        
        # Don't add to position if at limit
        bid_size = self.size if pos < self.max_pos else 0
        ask_size = self.size if pos > -self.max_pos else 0
        
        return TargetQuotes(
            bid_price=bid if bid_size > 0 else None,
            bid_size=bid_size,
            ask_price=ask if ask_size > 0 else None,
            ask_size=ask_size,
        )
```

### 2. Edge-Based Strategy

```python
class EdgeStrategy(SimpleStrategy):
    """Only quote when there's edge vs the market."""
    
    def __init__(self, min_edge=0.03, size=20.0):
        self.min_edge = min_edge
        self.size = size
    
    def compute_quotes(self, ctx: StrategyContext) -> TargetQuotes:
        if ctx.fair_price is None or ctx.market_bid is None:
            return TargetQuotes()
        
        fair = ctx.fair_price
        
        # Only bid if fair price is above market bid + edge
        bid = None
        if fair > ctx.market_bid + self.min_edge:
            bid = ctx.market_bid + 0.01  # Improve by 1 tick
        
        # Only ask if fair price is below market ask - edge
        ask = None
        if fair < ctx.market_ask - self.min_edge:
            ask = ctx.market_ask - 0.01  # Improve by 1 tick
        
        return TargetQuotes(
            bid_price=bid,
            bid_size=self.size if bid else 0,
            ask_price=ask,
            ask_size=self.size if ask else 0,
        )
```

### 3. Inventory Liquidation

```python
class LiquidationStrategy(SimpleStrategy):
    """Aggressively close position near hour end."""
    
    def compute_quotes(self, ctx: StrategyContext) -> TargetQuotes:
        pos = ctx.position
        
        # Nothing to liquidate
        if abs(pos) < 1:
            return TargetQuotes()
        
        # Only liquidate in last 5 minutes
        if ctx.t_remaining_ms > 5 * 60 * 1000:
            return TargetQuotes()
        
        # Long: sell aggressively
        if pos > 0:
            return TargetQuotes(
                ask_price=ctx.market_bid,  # Cross spread to sell
                ask_size=pos,
            )
        
        # Short: buy aggressively  
        else:
            return TargetQuotes(
                bid_price=ctx.market_ask,  # Cross spread to buy
                bid_size=abs(pos),
            )
```

---

## How Fills Work

When your order gets filled:

1. **WebSocket sends trade event** → Position updates automatically
2. **Next tick**: `ctx.position` reflects the fill
3. **Your strategy sees it** and can react

```python
def compute_quotes(self, ctx: StrategyContext) -> TargetQuotes:
    # If we got filled on bid, position went up
    # Strategy naturally reacts by maybe widening bid
    
    if ctx.position > 20:
        # We're getting long, be more conservative on bids
        bid = ctx.fair_price - 0.03  # wider
    else:
        bid = ctx.fair_price - 0.02  # normal
    
    return TargetQuotes(bid_price=bid, bid_size=10)
```

### Optional: Fill Callback

```python
class MyStrategy(SimpleStrategy):
    
    def on_fill(self, side: str, price: float, size: float):
        """Called when you get filled. Override for custom handling."""
        print(f"GOT FILLED: {side} {size} @ {price}")
        # Maybe send alert, log to file, etc.
```

---

## Tips

### 1. Always Handle None Values

```python
def compute_quotes(self, ctx: StrategyContext) -> TargetQuotes:
    # Fair price might be None during startup
    if ctx.fair_price is None:
        return TargetQuotes()  # Don't quote
    
    # Market prices might be None
    if ctx.market_bid is None:
        return TargetQuotes()
```

### 2. Respect Position Limits

```python
MAX_POSITION = 100

def compute_quotes(self, ctx: StrategyContext) -> TargetQuotes:
    pos = ctx.position
    
    # Don't bid if already at max long
    bid = None if pos >= MAX_POSITION else fair - 0.02
    
    # Don't ask if already at max short
    ask = None if pos <= -MAX_POSITION else fair + 0.02
```

### 3. Use Time Remaining

```python
def compute_quotes(self, ctx: StrategyContext) -> TargetQuotes:
    # Reduce size near hour end
    time_left_pct = ctx.t_remaining_ms / (60 * 60 * 1000)
    size = 10.0 * max(0.1, time_left_pct)
    
    # Stop quoting in last minute
    if ctx.t_remaining_ms < 60_000:
        return TargetQuotes()
```

### 4. Don't Over-Engineer

The simplest strategies often work best. Start with:
1. Quote around fair price
2. Skew based on position
3. Respect limits

Then iterate based on real performance.

---

## Testing Your Strategy

Use the test harness:

```python
# tests/test_production_e2e.py

class MyTestStrategy(SimpleStrategy):
    def compute_quotes(self, ctx: StrategyContext) -> TargetQuotes:
        # Test with fixed quotes at safe prices
        return TargetQuotes(
            bid_price=0.01,  # Won't get filled
            bid_size=5.0,
            ask_price=0.99,  # Won't get filled
            ask_size=5.0,
        )
```

Run:
```bash
python tests/test_production_e2e.py
```

---

## Summary

| You Do | Framework Does |
|--------|----------------|
| Return target quotes | Place/cancel orders |
| Check `ctx.position` | Track fills via WS |
| Respect limits | Handle race conditions |
| Log important events | Sync state from WS |

**Keep it simple. Output targets. Let the framework handle the rest.**
