"""
Binance snapshot cache.

Maintains atomic snapshots of Binance BTC market data including:
- BBO (best bid/ask)
- Trade data (last trade, open price)
- Hour context (time remaining)
- Derived features and pricer output
"""

from typing import Optional

from ..snapshot_store import LatestSnapshotStore
from ..types import (
    MarketSnapshotMeta,
    BinanceSnapshot,
    now_ms,
    wall_ms,
)


class BinanceCache:
    """
    Binance snapshot cache.

    Updated by BinanceFeed from binance_pricer HTTP endpoint.
    Thread-safe for single writer (poller), multiple readers (Strategy).
    """

    def __init__(self):
        self._store = LatestSnapshotStore[BinanceSnapshot]()
        self._poller_seq: int = 0

    def update_from_poll(self, raw_snapshot: dict) -> int:
        """
        Update from polled HTTP response (binance_pricer schema).

        Converts binance_pricer BinanceSnapshot schema to internal BinanceSnapshot.

        Args:
            raw_snapshot: Dictionary from binance_pricer HTTP endpoint

        Returns:
            New sequence number
        """
        ts = now_ms()
        self._poller_seq += 1

        meta = MarketSnapshotMeta(
            monotonic_ts=ts,
            wall_ts=raw_snapshot.get("ts_local_ms") or wall_ms(),
            feed_seq=self._poller_seq,
            source="BN",
        )

        # Extract features dict
        features = raw_snapshot.get("features", {})

        snapshot = BinanceSnapshot(
            meta=meta,
            symbol=raw_snapshot.get("symbol", "BTCUSDT"),
            best_bid_px=raw_snapshot.get("bbo_bid", 0.0) or 0.0,
            best_ask_px=raw_snapshot.get("bbo_ask", 0.0) or 0.0,
            # Trade data
            last_trade_price=raw_snapshot.get("last_trade_price"),
            open_price=raw_snapshot.get("open_price"),
            # Hour context
            hour_start_ts_ms=raw_snapshot.get("hour_start_ts_ms", 0),
            hour_end_ts_ms=raw_snapshot.get("hour_end_ts_ms", 0),
            t_remaining_ms=raw_snapshot.get("t_remaining_ms", 0),
            # Features
            return_1s=features.get("return_1m") or features.get("return_1s"),
            ewma_vol_1s=features.get("ewma_vol") or features.get("ewma_vol_1s"),
            shock_z=raw_snapshot.get("pricer", {}).get("shock_z") if "pricer" in raw_snapshot else features.get("shock_z"),
            signed_volume_1s=features.get("signed_volume_1s"),
            # Pricer output
            p_yes=raw_snapshot.get("p_yes_fair"),
            p_yes_band_lo=raw_snapshot.get("p_yes_band_lo"),
            p_yes_band_hi=raw_snapshot.get("p_yes_band_hi"),
        )

        return self._store.publish(snapshot)

    def update_direct(
        self,
        symbol: str,
        best_bid_px: float,
        best_ask_px: float,
        last_trade_price: Optional[float] = None,
        open_price: Optional[float] = None,
        hour_start_ts_ms: int = 0,
        hour_end_ts_ms: int = 0,
        t_remaining_ms: int = 0,
        p_yes: Optional[float] = None,
        **features,
    ) -> int:
        """
        Update directly with parsed values.

        Alternative to update_from_poll for direct integration.

        Args:
            symbol: Trading symbol (e.g., "BTCUSDT")
            best_bid_px: Best bid price
            best_ask_px: Best ask price
            last_trade_price: Last trade price
            open_price: Hour open price
            hour_start_ts_ms: Hour start timestamp
            hour_end_ts_ms: Hour end timestamp
            t_remaining_ms: Time remaining to hour end
            p_yes: Fair probability from pricer
            **features: Additional features (return_1s, ewma_vol_1s, shock_z, etc.)

        Returns:
            New sequence number
        """
        ts = now_ms()
        self._poller_seq += 1

        meta = MarketSnapshotMeta(
            monotonic_ts=ts,
            wall_ts=wall_ms(),
            feed_seq=self._poller_seq,
            source="BN",
        )

        snapshot = BinanceSnapshot(
            meta=meta,
            symbol=symbol,
            best_bid_px=best_bid_px,
            best_ask_px=best_ask_px,
            last_trade_price=last_trade_price,
            open_price=open_price,
            hour_start_ts_ms=hour_start_ts_ms,
            hour_end_ts_ms=hour_end_ts_ms,
            t_remaining_ms=t_remaining_ms,
            return_1s=features.get("return_1s"),
            ewma_vol_1s=features.get("ewma_vol_1s"),
            shock_z=features.get("shock_z"),
            signed_volume_1s=features.get("signed_volume_1s"),
            p_yes=p_yes,
            p_yes_band_lo=features.get("p_yes_band_lo"),
            p_yes_band_hi=features.get("p_yes_band_hi"),
        )

        return self._store.publish(snapshot)

    def get_latest(self) -> tuple[Optional[BinanceSnapshot], int]:
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

    def is_stale(self, threshold_ms: int = 1000, ts: Optional[int] = None) -> bool:
        """
        Check if cache is stale.

        Args:
            threshold_ms: Staleness threshold (default 1000ms)
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

    def get_mid_price(self) -> Optional[float]:
        """
        Get current mid price.

        Returns:
            Mid price or None if no data
        """
        snapshot, _ = self._store.read_latest()
        if snapshot is None:
            return None
        return snapshot.mid_px

    def get_p_yes(self) -> Optional[float]:
        """
        Get current fair probability.

        Returns:
            p_yes or None if not available
        """
        snapshot, _ = self._store.read_latest()
        if snapshot is None:
            return None
        return snapshot.p_yes

    def get_time_remaining_ms(self) -> int:
        """
        Get time remaining to hour end.

        Returns:
            Milliseconds remaining, or 0 if no data
        """
        snapshot, _ = self._store.read_latest()
        if snapshot is None:
            return 0
        return snapshot.t_remaining_ms

    def clear(self) -> None:
        """Clear cache state (for hour transitions)."""
        self._store = LatestSnapshotStore[BinanceSnapshot]()
        self._poller_seq = 0
