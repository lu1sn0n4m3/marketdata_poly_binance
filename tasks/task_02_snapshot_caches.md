# Task 02: Snapshot Caches (PMCache & BNCache)

**Priority:** Critical (Infrastructure)
**Estimated Complexity:** Medium
**Dependencies:** Task 01 (Data Types)

---

## Objective

Implement atomic, low-contention snapshot caches for Polymarket and Binance market data. These caches decouple high-frequency WS updates from the Strategy and Executor.

---

## Context from Design Documents

From `polymarket_mm_efficiency_design.md` Section 2:

> A major performance mistake is to push every market-data message into the same queue the Executor consumes.
>
> Instead:
> - Caches always hold the latest view
> - Strategy reads caches at fixed Hz
> - Executor consumes only low-rate, critical events

From `polymarket_mm_framework.md` Section 2:

> Snapshot Stores must allow fast, low-contention reads by Strategy and (optionally) Executor staleness checks.

---

## Implementation Checklist

### 1. Base Snapshot Store Pattern

Reference the existing `services/binance_pricer/src/binance_pricer/snapshot_store.py` for the pattern:

```python
class LatestSnapshotStore(Generic[T]):
    """
    Thread-safe atomic snapshot store.

    - Single writer (WS handler)
    - Multiple readers (Strategy, Executor staleness checks)
    - Immutable snapshots swapped atomically
    """

    def __init__(self):
        self._current: Optional[T] = None
        self._seq: int = 0
        self._lock = threading.Lock()  # For sync access

    def publish(self, snapshot: T) -> int:
        """Atomically publish new snapshot, returns sequence number."""
        with self._lock:
            self._seq += 1
            self._current = snapshot
            return self._seq

    def read_latest(self) -> tuple[Optional[T], int]:
        """Read latest snapshot and its sequence number."""
        with self._lock:
            return self._current, self._seq

    def get_seq(self) -> int:
        """Get current sequence without copying snapshot."""
        return self._seq
```

### 2. PMCache (Polymarket Book Cache)

```python
class PMCache:
    """
    Polymarket order book snapshot cache.

    Maintains:
    - Latest YES book (TOB + optional levels)
    - Latest NO book (TOB + optional levels)
    - Combined synthetic view
    - Rolling history for micro-features (optional)
    """

    def __init__(self, history_size: int = 100):
        self._store = LatestSnapshotStore[PMBookSnapshot]()
        self._history: deque[tuple[int, int]] = deque(maxlen=history_size)  # (ts, mid)

    def update_from_ws(
        self,
        yes_bbo: Optional[tuple[int, int, int, int]],  # (bid_px, bid_sz, ask_px, ask_sz)
        no_bbo: Optional[tuple[int, int, int, int]],
        market_id: str,
        yes_token_id: str,
        no_token_id: str,
    ) -> int:
        """
        Called by WS handler with parsed BBO data.
        Returns feed sequence number.
        """
        now_ms = time.monotonic_ns() // 1_000_000

        yes_top = PMBookTop(*yes_bbo) if yes_bbo else PMBookTop(0, 0, 100, 0)
        no_top = PMBookTop(*no_bbo) if no_bbo else PMBookTop(0, 0, 100, 0)

        meta = MarketSnapshotMeta(
            monotonic_ts=now_ms,
            wall_ts=int(time.time() * 1000),
            feed_seq=self._store.get_seq() + 1,
            source="PM",
        )

        snapshot = PMBookSnapshot(
            meta=meta,
            market_id=market_id,
            yes_token_id=yes_token_id,
            no_token_id=no_token_id,
            yes_top=yes_top,
            no_top=no_top,
        )

        seq = self._store.publish(snapshot)

        # Update history for micro-features
        self._history.append((now_ms, yes_top.mid_px))

        return seq

    def get_latest(self) -> tuple[Optional[PMBookSnapshot], int]:
        """Get latest snapshot and sequence."""
        return self._store.read_latest()

    def get_age_ms(self, now_ms: int) -> int:
        """Get age of latest snapshot in ms."""
        snapshot, _ = self._store.read_latest()
        if snapshot is None:
            return 999999
        return snapshot.meta.age_ms(now_ms)

    def is_stale(self, now_ms: int, threshold_ms: int = 500) -> bool:
        """Check if cache is stale."""
        return self.get_age_ms(now_ms) > threshold_ms
```

### 3. BNCache (Binance Cache) - Integration

The Binance snapshot is already provided by the binance_pricer service via HTTP polling. Create a simple cache that holds the polled snapshot:

```python
class BNCache:
    """
    Binance snapshot cache.

    Updated by SnapshotPoller from binance_pricer HTTP endpoint.
    """

    def __init__(self):
        self._store = LatestSnapshotStore[BNSnapshot]()
        self._poller_seq: int = 0

    def update_from_poll(self, raw_snapshot: dict) -> int:
        """
        Update from polled HTTP response.

        Converts binance_pricer schema to internal BNSnapshot.
        """
        now_ms = time.monotonic_ns() // 1_000_000
        self._poller_seq += 1

        meta = MarketSnapshotMeta(
            monotonic_ts=now_ms,
            wall_ts=raw_snapshot.get("wall_ts_ms"),
            feed_seq=self._poller_seq,
            source="BN",
        )

        snapshot = BNSnapshot(
            meta=meta,
            symbol=raw_snapshot.get("symbol", "BTCUSDT"),
            best_bid_px=raw_snapshot.get("bbo_bid", 0.0),
            best_ask_px=raw_snapshot.get("bbo_ask", 0.0),
            return_1s=raw_snapshot.get("features", {}).get("return_1m"),
            ewma_vol_1s=raw_snapshot.get("features", {}).get("ewma_vol"),
            shock_z=raw_snapshot.get("pricer", {}).get("shock_z"),
        )

        return self._store.publish(snapshot)

    def get_latest(self) -> tuple[Optional[BNSnapshot], int]:
        """Get latest snapshot and sequence."""
        return self._store.read_latest()

    def get_age_ms(self, now_ms: int) -> int:
        """Get age of latest snapshot in ms."""
        snapshot, _ = self._store.read_latest()
        if snapshot is None:
            return 999999
        return snapshot.meta.age_ms(now_ms)

    def is_stale(self, now_ms: int, threshold_ms: int = 1000) -> bool:
        """Check if cache is stale."""
        return self.get_age_ms(now_ms) > threshold_ms
```

---

## File Locations

- Create: `services/polymarket_trader/src/polymarket_trader/pm_cache.py`
- Create: `services/polymarket_trader/src/polymarket_trader/bn_cache.py`

---

## Acceptance Criteria

- [ ] Thread-safe atomic swaps (no partial reads)
- [ ] Sequence numbers for freshness tracking
- [ ] `is_stale()` method for quick staleness checks
- [ ] No blocking in read path
- [ ] Age calculation using monotonic time

---

## Design Notes from Efficiency Document

From Section 6:

> **Snapshot cache best practice:**
> - Build an immutable snapshot object
> - Swap a single reference to "latest"
> - Keep a small ring buffer of last N mids/trades for Strategy

---

## Integration Points

- **PMCache** receives updates from `PolymarketMarketWsClient`
- **BNCache** receives updates from `SnapshotPoller` (existing component)
- **Strategy** reads both caches at fixed Hz
- **Executor** may read cache metadata for staleness enforcement

---

## Testing

Create `tests/test_caches.py`:
- Test atomic publish/read under concurrent access
- Test staleness detection
- Test sequence number monotonicity
- Test age calculation accuracy
