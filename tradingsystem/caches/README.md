# Caches Module

Thread-safe caches for market data snapshots. Each cache uses `LatestSnapshotStore` for lock-free reads.

## Architecture

```
LatestSnapshotStore (from snapshot_store.py)
    |
    +-- BinanceCache      Binance BTC price/feature snapshots
    +-- PolymarketCache   Polymarket order book BBO snapshots
```

## BinanceCache

Stores Binance BTC market data polled from the `binance_pricer` HTTP endpoint.

**Data Stored:**
- BBO (best bid/ask price)
- Last trade price, open price
- Hour context (start/end timestamps, time remaining)
- Features: return_1s, ewma_vol_1s, shock_z, signed_volume_1s
- Pricer output: p_yes, p_yes_band_lo, p_yes_band_hi

**Usage:**
```python
from tradingsystem.caches import BinanceCache

cache = BinanceCache()

# Update from HTTP poll response
cache.update_from_poll({
    "symbol": "BTCUSDT",
    "bbo_bid": 100000.0,
    "bbo_ask": 100001.0,
    "p_yes_fair": 0.55,
    "t_remaining_ms": 1800000,
    "features": {"return_1m": 0.005, "ewma_vol": 0.02},
})

# Or update directly
cache.update_direct(
    symbol="BTCUSDT",
    best_bid_px=100000.0,
    best_ask_px=100001.0,
    p_yes=0.55,
)

# Read latest
snapshot, seq = cache.get_latest()
if snapshot:
    print(f"Mid: {snapshot.mid_px}, p_yes: {snapshot.p_yes_cents}c")

# Staleness check (default 1000ms)
if cache.is_stale():
    print("Data is stale!")

# Convenience accessors
mid = cache.get_mid_price()
p_yes = cache.get_p_yes()
remaining = cache.get_time_remaining_ms()
```

**Writer:** `BinanceFeed` (polls HTTP endpoint)
**Readers:** Strategy, Executor

## PolymarketCache

Stores Polymarket order book BBO (best bid/offer) for YES and NO tokens.

**Data Stored:**
- YES book top: best_bid_px, best_bid_sz, best_ask_px, best_ask_sz
- NO book top: best_bid_px, best_bid_sz, best_ask_px, best_ask_sz
- Market identity: market_id, yes_token_id, no_token_id
- Rolling mid price history for micro-features

**Usage:**
```python
from tradingsystem.caches import PolymarketCache

cache = PolymarketCache(history_size=100)

# Set market identity
cache.set_market(
    market_id="condition_123",
    yes_token_id="0xabc...",
    no_token_id="0xdef...",
)

# Update from WebSocket (prices in cents 1-99, sizes in contracts)
cache.update_from_ws(
    yes_bbo=(50, 100, 52, 150),  # (bid_px, bid_sz, ask_px, ask_sz)
    no_bbo=(48, 80, 50, 120),
)

# Partial updates
cache.update_yes_book(bid_px=51, bid_sz=100, ask_px=53, ask_sz=150)
cache.update_no_book(bid_px=47, bid_sz=80, ask_px=49, ask_sz=120)

# Read latest
snapshot, seq = cache.get_latest()
if snapshot:
    print(f"YES mid: {snapshot.yes_mid}c, spread: {snapshot.yes_top.spread}c")
    print(f"NO mid: {snapshot.no_mid}c")

# Staleness check (default 500ms)
if cache.is_stale():
    print("Data is stale!")

# Micro-features
history = cache.get_mid_history()  # [(ts_ms, mid_px), ...]
change = cache.get_recent_mid_change(lookback_ms=1000)
```

**Writer:** `PolymarketMarketFeed` (WebSocket)
**Readers:** Strategy, Executor

## Thread Safety

Both caches follow the single-writer, multiple-reader pattern:
- One thread writes (feed/poller)
- Multiple threads read (strategy, executor, monitoring)
- Lock-free reads via `LatestSnapshotStore`

## Common Interface

Both caches share a common interface:

| Method | Description |
|--------|-------------|
| `get_latest()` | Returns `(snapshot, seq)` tuple |
| `get_age_ms()` | Age of latest snapshot in ms |
| `is_stale(threshold_ms)` | Check if data is stale |
| `has_data` | Property: True if any data received |
| `seq` | Property: Current sequence number |
| `clear()` | Reset cache state |
