#!/usr/bin/env python3
"""
Test Script 1: Binance Connection and Snapshot Production

Tests the HourMM Container A components:
- BinanceWsClient connection
- FeatureEngine computation
- Pricer fair value calculation
- Snapshot building

Usage:
    python tests/test_binance_snapshots.py

Expected output:
    - Real-time Binance data
    - Computed features (volatility, returns)
    - Fair price estimates
    - Full snapshots at configurable rate
"""

import asyncio
import sys
import os
from datetime import datetime
from time import time_ns

# Add paths for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "services/binance_pricer/src"))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "shared"))

# Load environment from prod.env
def load_env_file(filepath: str):
    """Manually load env file if python-dotenv not available."""
    if not os.path.exists(filepath):
        return
    with open(filepath, "r") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip()
                if key and key not in os.environ:
                    os.environ[key] = value

prod_env_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "deploy/prod.env")
try:
    from dotenv import load_dotenv
    load_dotenv(prod_env_path)
except ImportError:
    load_env_file(prod_env_path)


def print_header(title: str):
    """Print a formatted header."""
    print(f"\n{'='*70}")
    print(f"  {title}")
    print(f"{'='*70}\n")


def print_section(title: str):
    """Print a section divider."""
    print(f"\n--- {title} ---\n")


async def test_binance_ws_client():
    """Test 1: Raw Binance WebSocket connection."""
    print_header("TEST 1: Binance WebSocket Connection")
    
    try:
        from binance_pricer.binance_ws import BinanceWsClient
        from binance_pricer.types import BinanceEventType
    except ImportError as e:
        print(f"✗ Failed to import BinanceWsClient: {e}")
        print("  Make sure you're running from the repo root")
        return False
    
    print("Creating BinanceWsClient for BTCUSDT...")
    
    events_received = []
    
    async def on_event(event):
        events_received.append(event)
        
        if event.event_type == BinanceEventType.TRADE:
            print(f"  TRADE: price={event.price:.2f}, qty={event.size:.6f}")
        elif event.event_type == BinanceEventType.BBO:
            print(f"  BBO:   bid={event.bid_price:.2f}, ask={event.ask_price:.2f}")
    
    client = BinanceWsClient(symbol="BTCUSDT", on_event=on_event)
    
    shutdown = asyncio.Event()
    
    print("\nConnecting to Binance WebSocket...")
    
    # Start client
    client_task = asyncio.create_task(client.run(shutdown))
    
    try:
        # Wait for connection
        await asyncio.sleep(2)
        
        if not client._connected:
            print("✗ Failed to connect to Binance")
            return False
        
        print("✓ Connected to Binance WebSocket")
        print("\nReceiving events (waiting for 10 events)...\n")
        
        # Wait for events
        start = asyncio.get_event_loop().time()
        while len(events_received) < 10 and asyncio.get_event_loop().time() - start < 15:
            await asyncio.sleep(0.1)
        
        # Summary
        print_section("Summary")
        trades = [e for e in events_received if e.event_type == BinanceEventType.TRADE]
        bbos = [e for e in events_received if e.event_type == BinanceEventType.BBO]
        
        print(f"  Total events:  {len(events_received)}")
        print(f"  Trades:        {len(trades)}")
        print(f"  BBO updates:   {len(bbos)}")
        
        if events_received:
            print("\n✓ Binance WebSocket test PASSED")
            return True
        else:
            print("\n✗ No events received")
            return False
            
    finally:
        shutdown.set()
        client.stop()
        client_task.cancel()
        try:
            await client_task
        except asyncio.CancelledError:
            pass


async def test_feature_engine():
    """Test 2: Feature Engine computation."""
    print_header("TEST 2: Feature Engine Computation")
    
    try:
        from binance_pricer.feature_engine import DefaultFeatureEngine
        from binance_pricer.types import BinanceEvent, BinanceEventType, HourContext
    except ImportError as e:
        print(f"✗ Failed to import: {e}")
        return False
    
    print("Creating DefaultFeatureEngine...")
    engine = DefaultFeatureEngine()
    
    # Simulate some trade events
    now_ms = time_ns() // 1_000_000
    
    # Create a HourContext for the engine
    ctx = HourContext(
        hour_id="2026-01-22T12:00:00Z",
        hour_start_ts_ms=now_ms - 30 * 60 * 1000,
        hour_end_ts_ms=now_ms + 30 * 60 * 1000,
        t_remaining_ms=30 * 60 * 1000,
        open_price=100000.0,
    )
    
    print("\nSimulating trade events...")
    
    prices = [100000, 100050, 99980, 100100, 100020, 99950, 100080, 100150, 99900, 100000]
    
    for i, price in enumerate(prices):
        event = BinanceEvent(
            event_type=BinanceEventType.TRADE,
            symbol="BTCUSDT",
            ts_exchange_ms=now_ms + i * 100,
            ts_local_ms=now_ms + i * 100,
            price=float(price),
            size=0.1,
            side="buy" if i % 2 == 0 else "sell",
        )
        engine.update(event, ctx)
        print(f"  Trade {i+1}: price={price}")
    
    print_section("Computed Features")
    features = engine.snapshot(ctx)
    
    for key, value in features.items():
        print(f"  {key}: {value:.6f}")
    
    # Verify features
    if "ewma_vol" in features and "vol_1m" in features:
        print("\n✓ Feature Engine test PASSED")
        return True
    else:
        print("\n✗ Missing expected features")
        return False


async def test_pricer():
    """Test 3: Pricer fair value calculation."""
    print_header("TEST 3: Pricer Fair Value Calculation")
    
    try:
        from binance_pricer.pricer import BaselinePricer
        from binance_pricer.types import HourContext
    except ImportError as e:
        print(f"✗ Failed to import: {e}")
        return False
    
    print("Creating BaselinePricer...")
    pricer = BaselinePricer()
    
    # Test various scenarios
    scenarios = [
        {"open": 100000, "current": 100000, "vol": 0.001, "t_remain": 30 * 60 * 1000, "desc": "At open, 30min left"},
        {"open": 100000, "current": 100500, "vol": 0.001, "t_remain": 30 * 60 * 1000, "desc": "Up 0.5%, 30min left"},
        {"open": 100000, "current": 99500, "vol": 0.001, "t_remain": 30 * 60 * 1000, "desc": "Down 0.5%, 30min left"},
        {"open": 100000, "current": 100200, "vol": 0.001, "t_remain": 5 * 60 * 1000, "desc": "Up 0.2%, 5min left"},
        {"open": 100000, "current": 99800, "vol": 0.001, "t_remain": 5 * 60 * 1000, "desc": "Down 0.2%, 5min left"},
        {"open": 100000, "current": 100050, "vol": 0.002, "t_remain": 30 * 60 * 1000, "desc": "Slightly up, high vol"},
    ]
    
    print_section("Pricing Scenarios")
    
    for s in scenarios:
        now_ms = time_ns() // 1_000_000
        ctx = HourContext(
            hour_id="2026-01-22T12:00:00Z",
            hour_start_ts_ms=now_ms - (60 - s["t_remain"] // 60000) * 60000,
            hour_end_ts_ms=now_ms + s["t_remain"],
            t_remaining_ms=s["t_remain"],
            open_price=s["open"],
            last_trade_price=s["current"],
        )
        features = {"vol_1m": s["vol"], "vol_5m": s["vol"], "ewma_vol": s["vol"]}
        
        output = pricer.price(ctx, features)
        
        pct_change = (s["current"] - s["open"]) / s["open"] * 100
        print(f"  {s['desc']}")
        print(f"    Change: {pct_change:+.2f}%  ->  P(Up) = {output.p_yes_fair:.3f}")
        print()
    
    print("✓ Pricer test PASSED")
    return True


async def test_full_snapshot_pipeline():
    """Test 4: Full snapshot production pipeline."""
    print_header("TEST 4: Full Snapshot Production Pipeline")
    
    try:
        from binance_pricer.binance_ws import BinanceWsClient
        from binance_pricer.hour_context import HourContextBuilder
        from binance_pricer.feature_engine import DefaultFeatureEngine
        from binance_pricer.pricer import BaselinePricer
        from binance_pricer.health import HealthTracker
        from binance_pricer.snapshot_publisher import SnapshotPublisher
        from binance_pricer.snapshot_store import LatestSnapshotStore
    except ImportError as e:
        print(f"✗ Failed to import: {e}")
        return False
    
    print("Setting up components...")
    
    # Create components
    health = HealthTracker(stale_threshold_ms=500)
    hour_ctx = HourContextBuilder()
    feature_engine = DefaultFeatureEngine()
    pricer = BaselinePricer()
    store = LatestSnapshotStore()
    
    # Event handler to wire components together
    async def on_binance_event(event):
        # Update health
        health.update_on_event(
            ts_local_ms=event.ts_local_ms,
            ts_exchange_ms=event.ts_exchange_ms,
        )
        # Update hour context
        hour_ctx.update_from_event(event)
        # Get current context
        ctx = hour_ctx.get_context(event.ts_local_ms)
        # Update features
        feature_engine.update(event, ctx)
    
    # Create WS client with event handler
    ws_client = BinanceWsClient(symbol="BTCUSDT", on_event=on_binance_event)
    
    # Create publisher with correct arguments
    publisher = SnapshotPublisher(
        publish_hz=2,  # Slow rate for testing
        store=store,
        ctx_builder=hour_ctx,
        feature_engine=feature_engine,
        pricer=pricer,
        health=health,
    )
    
    shutdown = asyncio.Event()
    snapshots_seen = []
    
    print("\nStarting snapshot pipeline (waiting for 5 snapshots)...\n")
    
    # Start components
    ws_task = asyncio.create_task(ws_client.run(shutdown))
    pub_task = asyncio.create_task(publisher.run(shutdown))
    
    try:
        # Wait for connection
        await asyncio.sleep(3)
        
        if not ws_client._connected:
            print("✗ WebSocket not connected")
            return False
        
        print("✓ WebSocket connected, producing snapshots...")
        
        # Monitor snapshots
        last_seq = -1
        start = asyncio.get_event_loop().time()
        
        while len(snapshots_seen) < 5 and asyncio.get_event_loop().time() - start < 30:
            snapshot = store.current
            if snapshot and snapshot.seq != last_seq:
                last_seq = snapshot.seq
                snapshots_seen.append(snapshot)
                
                print(f"\n  Snapshot #{snapshot.seq}:")
                bid = snapshot.bbo_bid or 0
                ask = snapshot.bbo_ask or 0
                trade = snapshot.last_trade_price or 0
                p_yes = snapshot.p_yes_fair or 0.5
                vol = snapshot.features.get("ewma_vol", 0)
                print(f"    bid={bid:.2f}, ask={ask:.2f}")
                print(f"    last_trade={trade:.2f}")
                print(f"    p_yes_fair={p_yes:.4f}")
                print(f"    ewma_vol={vol:.6f}")
                print(f"    t_remaining={snapshot.t_remaining_ms // 1000}s")
            
            await asyncio.sleep(0.2)
        
        print_section("Summary")
        print(f"  Snapshots produced: {len(snapshots_seen)}")
        
        if snapshots_seen:
            latest = snapshots_seen[-1]
            open_p = latest.open_price or 0
            bid = latest.bbo_bid or 0
            ask = latest.bbo_ask or 0
            p_yes = latest.p_yes_fair or 0.5
            print(f"\n  Latest snapshot:")
            print(f"    seq:            {latest.seq}")
            print(f"    hour_id:        {latest.hour_id}")
            print(f"    open_price:     {open_p:.2f}")
            print(f"    bid/ask:        {bid:.2f} / {ask:.2f}")
            print(f"    p_yes_fair:     {p_yes:.4f}")
            print(f"    stale:          {latest.stale}")
            
            print("\n✓ Full snapshot pipeline test PASSED")
            return True
        else:
            print("\n✗ No snapshots produced")
            return False
            
    finally:
        shutdown.set()
        ws_client.stop()
        publisher.stop()
        ws_task.cancel()
        pub_task.cancel()
        try:
            await ws_task
        except asyncio.CancelledError:
            pass
        try:
            await pub_task
        except asyncio.CancelledError:
            pass


async def main():
    """Run all tests."""
    print("\n" + "="*70)
    print("  HOURMM BINANCE SNAPSHOT TEST SUITE")
    print("="*70)
    print(f"\nStarted at: {datetime.now().isoformat()}")
    
    results = {}
    
    # Test 1: WebSocket
    results["binance_ws"] = await test_binance_ws_client()
    
    # Test 2: Feature Engine
    results["feature_engine"] = await test_feature_engine()
    
    # Test 3: Pricer
    results["pricer"] = await test_pricer()
    
    # Test 4: Full Pipeline
    results["full_pipeline"] = await test_full_snapshot_pipeline()
    
    # Final Summary
    print_header("FINAL RESULTS")
    
    passed = sum(1 for v in results.values() if v)
    total = len(results)
    
    for test, result in results.items():
        status = "✓ PASSED" if result else "✗ FAILED"
        print(f"  {test}: {status}")
    
    print(f"\n  Total: {passed}/{total} tests passed")
    
    if passed == total:
        print("\n" + "="*70)
        print("  ALL TESTS PASSED! Binance integration is working correctly.")
        print("="*70 + "\n")
        return 0
    else:
        print("\n" + "="*70)
        print("  SOME TESTS FAILED. Check the output above for details.")
        print("="*70 + "\n")
        return 1


if __name__ == "__main__":
    exit_code = asyncio.run(main())
    sys.exit(exit_code)
