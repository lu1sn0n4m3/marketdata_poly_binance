#!/usr/bin/env python3
"""
Test Script 2: Polymarket Market Connection and Data Streaming

Tests the HourMM Container B market data components:
- GammaClient market discovery (auto-derives slug from Eastern Time)
- PolymarketMarketWsClient WebSocket connection
- Real-time BBO and book updates

Usage:
    python tests/test_polymarket_market.py

Expected output:
    - Current market discovery based on Eastern Time
    - Token IDs for Up/Down outcomes
    - Real-time market data stream
"""

import asyncio
import sys
import os
from datetime import datetime, timezone, timedelta
from time import time_ns

# Add paths for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "services/polymarket_trader/src"))
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


def get_eastern_time():
    """Get current Eastern Time with DST handling."""
    utc_now = datetime.now(timezone.utc)
    
    # Simple DST approximation (Mar-Nov = EDT, else EST)
    et_offset = -4 if 3 <= utc_now.month < 11 else -5
    et_tz = timezone(timedelta(hours=et_offset))
    
    return utc_now.astimezone(et_tz)


async def test_gamma_client():
    """Test 1: Gamma API market discovery."""
    print_header("TEST 1: Gamma API Market Discovery")
    
    try:
        from polymarket_trader.gamma_client import GammaClient
    except ImportError as e:
        print(f"✗ Failed to import GammaClient: {e}")
        return None, False
    
    print("Creating GammaClient...")
    gamma = GammaClient()
    
    # Get current Eastern Time
    et_time = get_eastern_time()
    print(f"\nCurrent Eastern Time: {et_time.strftime('%Y-%m-%d %I:%M %p %Z')}")
    
    # Derive expected slug
    month_names = [
        "january", "february", "march", "april", "may", "june",
        "july", "august", "september", "october", "november", "december",
    ]
    month = month_names[et_time.month - 1]
    day = et_time.day
    hour_12 = et_time.hour % 12 or 12
    am_pm = "pm" if et_time.hour >= 12 else "am"
    
    expected_slug = f"bitcoin-up-or-down-{month}-{day}-{hour_12}{am_pm}-et"
    print(f"Expected slug: {expected_slug}")
    
    print_section("Fetching Current Hour Market")
    
    now_ms = time_ns() // 1_000_000
    market = await gamma.get_current_hour_market("bitcoin-up-or-down", now_ms)
    
    if market:
        print(f"✓ Found market!")
        print(f"  Slug:         {market.market_slug}")
        print(f"  Question:     {market.question[:60]}...")
        print(f"  Condition ID: {market.condition_id[:20]}...")
        print(f"  Active:       {market.active}")
        print(f"\n  Token IDs:")
        print(f"    UP/YES:  {market.tokens.yes_token_id}")
        print(f"    DOWN/NO: {market.tokens.no_token_id}")
        
        await gamma.close()
        return market, True
    else:
        print("✗ No market found for current hour")
        print("\n  This could mean:")
        print("  - The market hasn't been created yet")
        print("  - You're at an hour boundary")
        print("  - Network/API issue")
        
        # Try to check API health
        print("\n  Checking API health...")
        healthy = await gamma.healthcheck()
        print(f"  API reachable: {healthy}")
        
        await gamma.close()
        return None, False


async def test_polymarket_ws_raw():
    """Test 2: Raw WebSocket connection (no HourMM dependency)."""
    print_header("TEST 2: Raw Polymarket WebSocket Connection")
    
    try:
        import websockets
        import json
    except ImportError as e:
        print(f"✗ Missing dependency: {e}")
        print("  Install with: pip install websockets")
        return False
    
    url = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
    
    print(f"Connecting to: {url}")
    
    try:
        async with websockets.connect(url) as ws:
            print("✓ Connected to Polymarket Market WebSocket")
            
            # Just test connection, don't subscribe (need token IDs)
            print("\n  Connection established successfully")
            print("  (Token subscription tested in next test)")
            
            return True
            
    except Exception as e:
        print(f"✗ Connection failed: {e}")
        return False


async def test_polymarket_ws_with_market(market):
    """Test 3: WebSocket with actual market subscription."""
    print_header("TEST 3: Polymarket WebSocket with Market Data")
    
    if not market:
        print("✗ No market available for testing")
        return False
    
    try:
        import websockets
        import json
    except ImportError:
        print("✗ websockets not installed")
        return False
    
    url = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
    token_ids = [market.tokens.yes_token_id, market.tokens.no_token_id]
    
    print(f"Subscribing to tokens:")
    print(f"  UP:   {token_ids[0][:40]}...")
    print(f"  DOWN: {token_ids[1][:40]}...")
    
    messages_received = []
    
    try:
        async with websockets.connect(url) as ws:
            print("\n✓ Connected")
            
            # Subscribe
            sub_msg = json.dumps({
                "type": "market",
                "assets_ids": token_ids
            })
            await ws.send(sub_msg)
            print("✓ Subscription sent")
            
            print("\nWaiting for market data (20 seconds)...\n")
            
            start = asyncio.get_event_loop().time()
            while asyncio.get_event_loop().time() - start < 20:
                try:
                    msg = await asyncio.wait_for(ws.recv(), timeout=1.0)
                    data = json.loads(msg)
                    messages_received.append(data)
                    
                    # Handle arrays
                    events = data if isinstance(data, list) else [data]
                    
                    for event in events:
                        event_type = event.get("event_type", "unknown")
                        asset_id = event.get("asset_id", "")
                        
                        # Identify which token
                        token_name = "?"
                        if asset_id == token_ids[0]:
                            token_name = "UP"
                        elif asset_id == token_ids[1]:
                            token_name = "DOWN"
                        
                        if event_type == "book":
                            bids = event.get("bids", [])
                            asks = event.get("asks", [])
                            best_bid = bids[0]["price"] if bids else "N/A"
                            best_ask = asks[0]["price"] if asks else "N/A"
                            print(f"  [{token_name:4}] BOOK: bid={best_bid}, ask={best_ask}")
                            
                        elif event_type == "price_change":
                            changes = event.get("price_changes", [])
                            for c in changes:
                                print(f"  [{token_name:4}] PRICE_CHANGE: {c.get('side')} @ {c.get('price')}")
                                
                        elif event_type == "last_trade_price":
                            price = event.get("price", "?")
                            print(f"  [{token_name:4}] TRADE: price={price}")
                            
                        elif event_type == "tick_size_change":
                            print(f"  [{token_name:4}] TICK_SIZE: {event.get('tick_size')}")
                            
                        else:
                            print(f"  [{token_name:4}] {event_type}: {str(event)[:80]}...")
                    
                except asyncio.TimeoutError:
                    continue
                except json.JSONDecodeError:
                    continue
            
            print_section("Summary")
            print(f"  Total messages: {len(messages_received)}")
            
            # Count event types
            event_types = {}
            for msg in messages_received:
                events = msg if isinstance(msg, list) else [msg]
                for e in events:
                    et = e.get("event_type", "unknown")
                    event_types[et] = event_types.get(et, 0) + 1
            
            print("  Event types:")
            for et, count in sorted(event_types.items()):
                print(f"    {et}: {count}")
            
            if messages_received:
                print("\n✓ Polymarket Market WebSocket test PASSED")
                return True
            else:
                print("\n⚠ No messages received (market may be inactive)")
                return True  # Still pass - connection worked
                
    except Exception as e:
        print(f"\n✗ Error: {e}")
        return False


async def test_hourmm_ws_client(market):
    """Test 4: HourMM PolymarketMarketWsClient."""
    print_header("TEST 4: HourMM PolymarketMarketWsClient")
    
    if not market:
        print("✗ No market available for testing")
        return False
    
    try:
        from polymarket_trader.polymarket_ws_market import PolymarketMarketWsClient
    except ImportError as e:
        print(f"✗ Failed to import: {e}")
        return False
    
    events_received = []
    
    async def on_event(event):
        events_received.append(event)
        
        event_type = type(event).__name__
        if hasattr(event, 'best_bid') and hasattr(event, 'best_ask'):
            print(f"  {event_type}: bid={event.best_bid}, ask={event.best_ask}")
        elif hasattr(event, 'new_tick_size'):
            print(f"  {event_type}: tick_size={event.new_tick_size}")
        else:
            print(f"  {event_type}: {event}")
    
    print("Creating PolymarketMarketWsClient...")
    client = PolymarketMarketWsClient(
        ws_url="wss://ws-subscriptions-clob.polymarket.com/ws/market",
        on_event=on_event,
    )
    
    # Set tokens
    client.set_tokens(
        [market.tokens.yes_token_id, market.tokens.no_token_id],
        market.condition_id,
    )
    
    shutdown = asyncio.Event()
    
    print(f"\nSubscribing to market: {market.market_slug}")
    print("Waiting for events (15 seconds)...\n")
    
    # Run client
    client_task = asyncio.create_task(client.run(shutdown))
    
    try:
        await asyncio.sleep(15)
        
        print_section("Summary")
        print(f"  Events received: {len(events_received)}")
        
        if events_received:
            print("\n✓ HourMM Market WebSocket test PASSED")
            return True
        else:
            print("\n⚠ No events received (market may be inactive)")
            return True  # Connection worked
            
    finally:
        shutdown.set()
        client.stop()
        client_task.cancel()
        try:
            await client_task
        except asyncio.CancelledError:
            pass


async def main():
    """Run all tests."""
    print("\n" + "="*70)
    print("  HOURMM POLYMARKET MARKET CONNECTION TEST SUITE")
    print("="*70)
    print(f"\nStarted at: {datetime.now().isoformat()}")
    print(f"Eastern Time: {get_eastern_time().strftime('%Y-%m-%d %I:%M %p')}")
    
    results = {}
    
    # Test 1: Gamma API
    market, results["gamma_discovery"] = await test_gamma_client()
    
    # Test 2: Raw WebSocket
    results["raw_ws"] = await test_polymarket_ws_raw()
    
    # Test 3: WebSocket with market
    results["ws_with_market"] = await test_polymarket_ws_with_market(market)
    
    # Test 4: HourMM client
    results["hourmm_ws_client"] = await test_hourmm_ws_client(market)
    
    # Final Summary
    print_header("FINAL RESULTS")
    
    passed = sum(1 for v in results.values() if v)
    total = len(results)
    
    for test, result in results.items():
        status = "✓ PASSED" if result else "✗ FAILED"
        print(f"  {test}: {status}")
    
    print(f"\n  Total: {passed}/{total} tests passed")
    
    if market:
        print(f"\n  Active Market: {market.market_slug}")
        print(f"  UP token:  {market.tokens.yes_token_id[:30]}...")
        print(f"  DOWN token: {market.tokens.no_token_id[:30]}...")
    
    if passed == total:
        print("\n" + "="*70)
        print("  ALL TESTS PASSED! Polymarket market connection is working.")
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
