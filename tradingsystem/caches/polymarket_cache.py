"""
Polymarket order book snapshot cache.

Maintains atomic snapshots of YES/NO book state with:
- Thread-safe reads/writes
- Staleness detection
- Rolling history for micro-features
"""

import time
from collections import deque
from typing import Optional

from ..snapshot_store import LatestSnapshotStore
from ..types import (
    MarketSnapshotMeta,
    PolymarketBookTop,
    PolymarketBookSnapshot,
    now_ms,
    wall_ms,
)


class PolymarketCache:
    """
    Polymarket order book snapshot cache.

    Maintains:
    - Latest YES book (TOB)
    - Latest NO book (TOB)
    - Combined synthetic view
    - Rolling history for micro-features

    Thread-safe for single writer (WS handler), multiple readers (Strategy).
    """

    def __init__(self, history_size: int = 100):
        """
        Initialize PolymarketCache.

        Args:
            history_size: Number of historical mid prices to retain
        """
        self._store = LatestSnapshotStore[PolymarketBookSnapshot]()
        self._history: deque[tuple[int, int]] = deque(maxlen=history_size)
        self._history_size = history_size

        # Market identity (set on first update)
        self._market_id: str = ""
        self._yes_token_id: str = ""
        self._no_token_id: str = ""

    def set_market(self, market_id: str, yes_token_id: str, no_token_id: str) -> None:
        """
        Set market identity for this cache.

        Should be called once when market is selected.
        """
        self._market_id = market_id
        self._yes_token_id = yes_token_id
        self._no_token_id = no_token_id

    def update_from_ws(
        self,
        yes_bbo: Optional[tuple[int, int, int, int]],  # (bid_px, bid_sz, ask_px, ask_sz)
        no_bbo: Optional[tuple[int, int, int, int]],
        market_id: Optional[str] = None,
        yes_token_id: Optional[str] = None,
        no_token_id: Optional[str] = None,
    ) -> int:
        """
        Update cache from WebSocket BBO data.

        Called by WS handler with parsed BBO tuples.
        Prices in cents (1-99), sizes in contracts.

        Args:
            yes_bbo: YES book top (bid_px, bid_sz, ask_px, ask_sz) or None
            no_bbo: NO book top (bid_px, bid_sz, ask_px, ask_sz) or None
            market_id: Optional market ID override
            yes_token_id: Optional YES token ID override
            no_token_id: Optional NO token ID override

        Returns:
            Feed sequence number
        """
        ts = now_ms()
        wall = wall_ms()

        # Use provided IDs or fall back to stored ones
        mid = market_id or self._market_id
        yid = yes_token_id or self._yes_token_id
        nid = no_token_id or self._no_token_id

        # Build TOB objects
        # Default: no bid (0), ask at 100 (no liquidity)
        if yes_bbo:
            yes_top = PolymarketBookTop(
                best_bid_px=yes_bbo[0],
                best_bid_sz=yes_bbo[1],
                best_ask_px=yes_bbo[2],
                best_ask_sz=yes_bbo[3],
            )
        else:
            yes_top = PolymarketBookTop(best_bid_px=0, best_bid_sz=0, best_ask_px=100, best_ask_sz=0)

        if no_bbo:
            no_top = PolymarketBookTop(
                best_bid_px=no_bbo[0],
                best_bid_sz=no_bbo[1],
                best_ask_px=no_bbo[2],
                best_ask_sz=no_bbo[3],
            )
        else:
            no_top = PolymarketBookTop(best_bid_px=0, best_bid_sz=0, best_ask_px=100, best_ask_sz=0)

        meta = MarketSnapshotMeta(
            monotonic_ts=ts,
            wall_ts=wall,
            feed_seq=self._store.get_seq() + 1,
            source="PM",
        )

        snapshot = PolymarketBookSnapshot(
            meta=meta,
            market_id=mid,
            yes_token_id=yid,
            no_token_id=nid,
            yes_top=yes_top,
            no_top=no_top,
        )

        seq = self._store.publish(snapshot)

        # Update history for micro-features (ts, mid_px)
        self._history.append((ts, yes_top.mid_px))

        return seq

    def update_yes_book(
        self,
        bid_px: int,
        bid_sz: int,
        ask_px: int,
        ask_sz: int,
    ) -> int:
        """
        Update only YES book, preserving NO book from last snapshot.

        Convenience method for single-asset updates.
        """
        current, _ = self._store.read_latest()
        no_bbo = None
        if current:
            no_bbo = (
                current.no_top.best_bid_px,
                current.no_top.best_bid_sz,
                current.no_top.best_ask_px,
                current.no_top.best_ask_sz,
            )
        return self.update_from_ws(
            yes_bbo=(bid_px, bid_sz, ask_px, ask_sz),
            no_bbo=no_bbo,
        )

    def update_no_book(
        self,
        bid_px: int,
        bid_sz: int,
        ask_px: int,
        ask_sz: int,
    ) -> int:
        """
        Update only NO book, preserving YES book from last snapshot.

        Convenience method for single-asset updates.
        """
        current, _ = self._store.read_latest()
        yes_bbo = None
        if current:
            yes_bbo = (
                current.yes_top.best_bid_px,
                current.yes_top.best_bid_sz,
                current.yes_top.best_ask_px,
                current.yes_top.best_ask_sz,
            )
        return self.update_from_ws(
            yes_bbo=yes_bbo,
            no_bbo=(bid_px, bid_sz, ask_px, ask_sz),
        )

    def get_latest(self) -> tuple[Optional[PolymarketBookSnapshot], int]:
        """
        Get latest snapshot and sequence.

        Returns:
            Tuple of (snapshot, sequence_number)
        """
        return self._store.read_latest()

    def get_age_ms(self, ts: Optional[int] = None) -> int:
        """
        Get age of latest snapshot in milliseconds.

        Args:
            ts: Reference timestamp (defaults to now_ms())

        Returns:
            Age in ms, or 999999 if no snapshot
        """
        if ts is None:
            ts = now_ms()
        snapshot, _ = self._store.read_latest()
        if snapshot is None:
            return 999999
        return snapshot.meta.age_ms(ts)

    def is_stale(self, threshold_ms: int = 500, ts: Optional[int] = None) -> bool:
        """
        Check if cache is stale.

        Args:
            threshold_ms: Staleness threshold (default 500ms)
            ts: Reference timestamp (defaults to now_ms())

        Returns:
            True if stale or no data
        """
        return self.get_age_ms(ts) > threshold_ms

    @property
    def has_data(self) -> bool:
        """Check if any snapshot has been published."""
        return self._store.has_data

    @property
    def seq(self) -> int:
        """Current sequence number."""
        return self._store.get_seq()

    def get_mid_history(self) -> list[tuple[int, int]]:
        """
        Get recent mid price history.

        Returns:
            List of (timestamp_ms, mid_px) tuples, oldest first
        """
        return list(self._history)

    def get_recent_mid_change(self, lookback_ms: int = 1000) -> Optional[int]:
        """
        Get mid price change over lookback period.

        Args:
            lookback_ms: Lookback period in milliseconds

        Returns:
            Price change in cents, or None if insufficient data
        """
        if len(self._history) < 2:
            return None

        current_ts, current_mid = self._history[-1]
        cutoff = current_ts - lookback_ms

        # Find oldest point within lookback
        for ts, mid in self._history:
            if ts >= cutoff:
                return current_mid - mid

        # All history is within lookback, use oldest
        _, oldest_mid = self._history[0]
        return current_mid - oldest_mid

    def clear(self) -> None:
        """Clear cache state (for market transitions)."""
        self._store = LatestSnapshotStore[PolymarketBookSnapshot]()
        self._history.clear()
