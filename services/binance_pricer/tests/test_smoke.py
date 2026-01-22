"""Smoke tests for Container A wiring."""

import asyncio
import sys
sys.path.insert(0, "/Users/luissafar/dev/marketdata_poly_binance")
sys.path.insert(0, "/Users/luissafar/dev/marketdata_poly_binance/services/binance_pricer/src")

from binance_pricer.config import AConfig
from binance_pricer.types import BinanceEvent, BinanceEventType, HourContext
from binance_pricer.binance_ws import BinanceWsClient
from binance_pricer.hour_context import HourContextBuilder
from binance_pricer.feature_engine import DefaultFeatureEngine
from binance_pricer.pricer import BaselinePricer
from binance_pricer.snapshot_store import LatestSnapshotStore
from binance_pricer.snapshot_publisher import SnapshotPublisher
from binance_pricer.health import HealthTracker
from shared.hourmm_common.schemas import BinanceSnapshot


def test_config_from_env():
    """Test AConfig loads correctly."""
    config = AConfig.from_env()
    assert config.symbol == "BTCUSDT"
    assert config.http_port == 8080
    config.validate()
    print("✓ Config loads and validates")


def test_binance_ws_client_creation():
    """Test BinanceWsClient can be instantiated."""
    client = BinanceWsClient(symbol="BTCUSDT")
    assert client.symbol == "BTCUSDT"
    assert not client.connected
    print("✓ BinanceWsClient instantiates")


def test_hour_context_builder():
    """Test HourContextBuilder tracks hours correctly."""
    builder = HourContextBuilder()
    
    # Get current context
    import time
    now_ms = int(time.time() * 1000)
    ctx = builder.get_context(now_ms)
    
    assert ctx.hour_id is not None
    assert ctx.hour_start_ts_ms <= now_ms
    assert ctx.hour_end_ts_ms > now_ms
    assert ctx.t_remaining_ms >= 0
    print(f"✓ HourContextBuilder: hour={ctx.hour_id}, remaining={ctx.t_remaining_ms}ms")


def test_feature_engine():
    """Test DefaultFeatureEngine computes features."""
    engine = DefaultFeatureEngine()
    
    assert "vol_1m" in engine.feature_names
    assert not engine.ready  # No data yet
    
    # Simulate a trade
    import time
    now_ms = int(time.time() * 1000)
    
    ctx_builder = HourContextBuilder()
    ctx = ctx_builder.get_context(now_ms)
    
    event = BinanceEvent(
        event_type=BinanceEventType.TRADE,
        symbol="BTCUSDT",
        ts_exchange_ms=now_ms,
        ts_local_ms=now_ms,
        price=50000.0,
        size=0.1,
        side="buy",
    )
    
    engine.update(event, ctx)
    features = engine.snapshot(ctx)
    
    assert "order_imbalance" in features
    print(f"✓ FeatureEngine computes features: {list(features.keys())}")


def test_pricer():
    """Test BaselinePricer computes probabilities."""
    pricer = BaselinePricer()
    
    assert pricer.name == "BaselinePricer"
    
    # Create a context with open price
    ctx = HourContext(
        hour_id="2026-01-22T14:00:00Z",
        hour_start_ts_ms=1737554400000,
        hour_end_ts_ms=1737558000000,
        t_remaining_ms=1800000,  # 30 minutes remaining
        open_price=50000.0,
        last_trade_price=50100.0,  # Slightly up
        bbo_bid=50050.0,
        bbo_ask=50150.0,
    )
    
    features = {"ewma_vol": 0.02}
    output = pricer.price(ctx, features)
    
    assert output.p_yes_fair is not None
    assert 0.0 <= output.p_yes_fair <= 1.0
    print(f"✓ Pricer output: p_yes_fair={output.p_yes_fair:.4f}")


def test_snapshot_store():
    """Test LatestSnapshotStore stores and retrieves snapshots."""
    store = LatestSnapshotStore()
    
    assert store.current is None
    assert store.seq == 0
    
    # Create a snapshot
    snapshot = BinanceSnapshot(
        seq=store.increment_seq(),
        ts_local_ms=1000,
        ts_exchange_ms=999,
        age_ms=1,
        stale=False,
        hour_id="2026-01-22T14:00:00Z",
        hour_start_ts_ms=1737554400000,
        hour_end_ts_ms=1737558000000,
        t_remaining_ms=1800000,
        open_price=50000.0,
        last_trade_price=50100.0,
        bbo_bid=50050.0,
        bbo_ask=50150.0,
        mid=50100.0,
        features={},
        p_yes_fair=0.55,
    )
    
    store.publish_sync(snapshot)
    
    retrieved = store.read_latest_sync()
    assert retrieved is not None
    assert retrieved.seq == 1
    assert retrieved.p_yes_fair == 0.55
    print("✓ SnapshotStore stores and retrieves")


def test_snapshot_publisher_build():
    """Test SnapshotPublisher builds snapshots."""
    import time
    now_ms = int(time.time() * 1000)
    
    store = LatestSnapshotStore()
    ctx_builder = HourContextBuilder()
    feature_engine = DefaultFeatureEngine()
    pricer = BaselinePricer()
    health = HealthTracker()
    
    # Simulate receiving an event
    health.update_on_event(now_ms, now_ms - 10)
    
    # Update context with a trade
    ctx_builder.update_from_trade(50000.0, now_ms - 5, now_ms)
    
    publisher = SnapshotPublisher(
        publish_hz=50,
        store=store,
        ctx_builder=ctx_builder,
        feature_engine=feature_engine,
        pricer=pricer,
        health=health,
    )
    
    snapshot = publisher.build_snapshot(now_ms)
    
    assert snapshot.seq == 1
    assert snapshot.open_price == 50000.0
    assert snapshot.stale == False or snapshot.age_ms > 500
    print(f"✓ SnapshotPublisher builds snapshot: seq={snapshot.seq}")


def test_snapshot_serialization():
    """Test BinanceSnapshot serializes to/from dict."""
    snapshot = BinanceSnapshot(
        seq=1,
        ts_local_ms=1000,
        ts_exchange_ms=999,
        age_ms=1,
        stale=False,
        hour_id="2026-01-22T14:00:00Z",
        hour_start_ts_ms=1737554400000,
        hour_end_ts_ms=1737558000000,
        t_remaining_ms=1800000,
        open_price=50000.0,
        last_trade_price=50100.0,
        bbo_bid=50050.0,
        bbo_ask=50150.0,
        mid=50100.0,
        features={"vol_1m": 0.015},
        p_yes_fair=0.55,
    )
    
    data = snapshot.to_dict()
    restored = BinanceSnapshot.from_dict(data)
    
    assert restored.seq == snapshot.seq
    assert restored.p_yes_fair == snapshot.p_yes_fair
    assert restored.features == snapshot.features
    print("✓ Snapshot serialization round-trip")


if __name__ == "__main__":
    print("\n=== Container A Smoke Tests ===\n")
    test_config_from_env()
    test_binance_ws_client_creation()
    test_hour_context_builder()
    test_feature_engine()
    test_pricer()
    test_snapshot_store()
    test_snapshot_publisher_build()
    test_snapshot_serialization()
    print("\n=== All Container A Smoke Tests Passed ===\n")
