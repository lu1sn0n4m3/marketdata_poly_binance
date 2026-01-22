"""Snapshot publisher for Container A."""

import asyncio
import logging
from time import time_ns
from typing import Optional

try:
    from shared.hourmm_common.schemas import BinanceSnapshot
except ImportError:
    import sys
    sys.path.insert(0, "/Users/luissafar/dev/marketdata_poly_binance")
    from shared.hourmm_common.schemas import BinanceSnapshot

from .types import HourContext
from .snapshot_store import LatestSnapshotStore
from .hour_context import HourContextBuilder
from .feature_engine import FeatureEngine
from .pricer import Pricer
from .health import HealthTracker

logger = logging.getLogger(__name__)


class SnapshotPublisher:
    """
    Builds and publishes snapshots at a fixed rate.
    
    Coordinates all components to build a consistent snapshot:
    - HourContextBuilder: hour boundaries and prices
    - FeatureEngine: rolling features
    - Pricer: fair probability
    - HealthTracker: staleness detection
    """
    
    def __init__(
        self,
        publish_hz: int,
        store: LatestSnapshotStore,
        ctx_builder: HourContextBuilder,
        feature_engine: FeatureEngine,
        pricer: Pricer,
        health: HealthTracker,
    ):
        """
        Initialize the publisher.
        
        Args:
            publish_hz: Publish rate in Hz (snapshots per second)
            store: LatestSnapshotStore to publish to
            ctx_builder: HourContextBuilder for hour context
            feature_engine: FeatureEngine for features
            pricer: Pricer for fair probability
            health: HealthTracker for staleness
        """
        self.publish_hz = publish_hz
        self.store = store
        self.ctx_builder = ctx_builder
        self.feature_engine = feature_engine
        self.pricer = pricer
        self.health = health
        
        self._running = False
        self._last_hour_id: Optional[str] = None
    
    def build_snapshot(self, now_ms: int) -> BinanceSnapshot:
        """
        Build a snapshot for the current state.
        
        Args:
            now_ms: Current timestamp in milliseconds
        
        Returns:
            BinanceSnapshot
        """
        # Get hour context
        ctx = self.ctx_builder.get_context(now_ms)
        
        # Check for hour rollover
        if self._last_hour_id is not None and ctx.hour_id != self._last_hour_id:
            # Hour changed - reset engines
            self.feature_engine.reset_for_new_hour(ctx)
            self.pricer.reset_for_new_hour(ctx)
            logger.info(f"Hour rollover detected: {self._last_hour_id} -> {ctx.hour_id}")
        
        self._last_hour_id = ctx.hour_id
        
        # Get features
        features = self.feature_engine.snapshot(ctx)
        
        # Get pricer output
        self.pricer.update(ctx, features)
        pricer_output = self.pricer.price(ctx, features)
        
        # Get health/staleness
        age_ms = self.health.age_ms(now_ms)
        stale = self.health.is_stale(now_ms)
        
        # Get exchange timestamp from health tracker
        ts_exchange_ms = self.health.last_binance_event_exchange_ms or 0
        
        # Build snapshot
        seq = self.store.increment_seq()
        
        return BinanceSnapshot(
            seq=seq,
            ts_local_ms=now_ms,
            ts_exchange_ms=ts_exchange_ms,
            age_ms=age_ms,
            stale=stale,
            hour_id=ctx.hour_id,
            hour_start_ts_ms=ctx.hour_start_ts_ms,
            hour_end_ts_ms=ctx.hour_end_ts_ms,
            t_remaining_ms=ctx.t_remaining_ms,
            open_price=ctx.open_price,
            last_trade_price=ctx.last_trade_price,
            bbo_bid=ctx.bbo_bid,
            bbo_ask=ctx.bbo_ask,
            mid=ctx.mid,
            features=features,
            p_yes_fair=pricer_output.p_yes_fair,
            p_yes_band_lo=pricer_output.p_yes_band_lo,
            p_yes_band_hi=pricer_output.p_yes_band_hi,
        )
    
    async def run(self, shutdown_event: Optional[asyncio.Event] = None) -> None:
        """
        Main publish loop.
        
        Publishes snapshots at the configured rate.
        
        Args:
            shutdown_event: Optional event to signal shutdown
        """
        self._running = True
        interval_seconds = 1.0 / self.publish_hz
        
        logger.info(f"Snapshot publisher started at {self.publish_hz} Hz")
        
        while self._running:
            if shutdown_event and shutdown_event.is_set():
                break
            
            loop_start = time_ns()
            
            try:
                now_ms = time_ns() // 1_000_000
                snapshot = self.build_snapshot(now_ms)
                self.store.publish_sync(snapshot)
            except Exception as e:
                logger.error(f"Error building snapshot: {e}", exc_info=True)
            
            # Sleep to maintain rate
            elapsed_seconds = (time_ns() - loop_start) / 1_000_000_000
            sleep_seconds = max(0, interval_seconds - elapsed_seconds)
            
            if sleep_seconds > 0:
                await asyncio.sleep(sleep_seconds)
        
        logger.info("Snapshot publisher stopped")
    
    def stop(self) -> None:
        """Stop the publisher."""
        self._running = False
