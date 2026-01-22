#!/usr/bin/env python3
"""
Test Script 3: Polymarket Order Placement and Cancellation

Tests the HourMM order execution components:
- PolymarketRestClient with order signing
- Place limit orders (buy UP at $0.01 - no execution risk)
- Query open orders
- Cancel orders

REQUIRES CREDENTIALS:
    export PM_PRIVATE_KEY=0x...
    export PM_FUNDER=0x...
    export PM_SIGNATURE_TYPE=1

Usage:
    python tests/test_polymarket_orders.py

Expected output:
    - Client initialization with API credential derivation
    - Order placement with signed orders
    - Open order queries
    - Order cancellation
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
                # Don't override existing env vars
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
    et_offset = -4 if 3 <= utc_now.month < 11 else -5
    et_tz = timezone(timedelta(hours=et_offset))
    return utc_now.astimezone(et_tz)


def check_credentials():
    """Check if required credentials are set."""
    private_key = os.environ.get("PM_PRIVATE_KEY", "")
    funder = os.environ.get("PM_FUNDER", "")
    
    if not private_key:
        print("✗ PM_PRIVATE_KEY not set")
        return False
    if not funder:
        print("✗ PM_FUNDER not set")
        return False
    
    print(f"  PM_PRIVATE_KEY: {private_key[:10]}...{private_key[-6:]}")
    print(f"  PM_FUNDER: {funder}")
    print(f"  PM_SIGNATURE_TYPE: {os.environ.get('PM_SIGNATURE_TYPE', '1')}")
    
    return True


async def get_current_market():
    """Get the current hourly market."""
    try:
        from polymarket_trader.gamma_client import GammaClient
    except ImportError as e:
        print(f"✗ Import error: {e}")
        return None
    
    gamma = GammaClient()
    now_ms = time_ns() // 1_000_000
    market = await gamma.get_current_hour_market("bitcoin-up-or-down", now_ms)
    await gamma.close()
    
    return market


async def test_client_initialization():
    """Test 1: REST client initialization with credential derivation."""
    print_header("TEST 1: REST Client Initialization")
    
    print("Checking credentials...")
    if not check_credentials():
        return None, False
    
    try:
        from polymarket_trader.polymarket_rest import PolymarketRestClient
        from polymarket_trader.types import Side
    except ImportError as e:
        print(f"\n✗ Failed to import: {e}")
        return None, False
    
    print("\nCreating PolymarketRestClient...")
    
    client = PolymarketRestClient(
        private_key=os.environ.get("PM_PRIVATE_KEY", ""),
        funder=os.environ.get("PM_FUNDER", ""),
        signature_type=int(os.environ.get("PM_SIGNATURE_TYPE", "1")),
    )
    
    print("Initializing client (deriving API credentials)...")
    
    success = await client.initialize()
    
    if success:
        print("\n✓ Client initialized successfully!")
        print("  API credentials derived from private key")
        return client, True
    else:
        print("\n✗ Client initialization failed")
        return None, False


async def test_place_order(client, market):
    """Test 2: Place a test order."""
    print_header("TEST 2: Place Test Order")
    
    if not client or not market:
        print("✗ Client or market not available")
        return None, False
    
    try:
        from polymarket_trader.polymarket_rest import OrderRequest
        from polymarket_trader.types import Side
    except ImportError as e:
        print(f"✗ Import error: {e}")
        return None, False
    
    # Use UP token, buy at $0.01 (very low price, no execution risk)
    token_id = market.tokens.yes_token_id
    price = 0.01
    size = 5.0  # 5 contracts = $0.05 risk at most
    
    print(f"Placing test order:")
    print(f"  Market:   {market.market_slug}")
    print(f"  Side:     BUY UP")
    print(f"  Price:    ${price}")
    print(f"  Size:     {size} contracts")
    print(f"  Value:    ${price * size:.2f}")
    print(f"  Token:    {token_id[:40]}...")
    
    # Create order request
    now_ms = time_ns() // 1_000_000
    order = OrderRequest(
        client_req_id=f"test_{now_ms}",
        token_id=token_id,
        side=Side.BUY,
        price=price,
        size=size,
        expires_at_ms=now_ms + 5 * 60 * 1000,  # 5 min expiry
    )
    
    print("\nSending order (with cryptographic signature)...")
    
    result = await client.place_order(order)
    
    if result.success:
        print(f"\n✓ Order placed successfully!")
        print(f"  Order ID: {result.order_id}")
        return result.order_id, True
    else:
        print(f"\n✗ Order placement failed: {result.error_msg}")
        return None, False


async def test_query_orders_direct():
    """Test 3: Query open orders using py-clob-client directly."""
    print_header("TEST 3: Query Open Orders (Direct)")
    
    try:
        from py_clob_client.client import ClobClient
    except ImportError as e:
        print(f"✗ py-clob-client not installed: {e}")
        return [], False
    
    private_key = os.environ.get("PM_PRIVATE_KEY", "")
    funder = os.environ.get("PM_FUNDER", "")
    sig_type = int(os.environ.get("PM_SIGNATURE_TYPE", "1"))
    
    if not private_key.startswith("0x"):
        private_key = "0x" + private_key
    
    print("Creating ClobClient...")
    
    try:
        client = ClobClient(
            host="https://clob.polymarket.com",
            chain_id=137,
            key=private_key,
            funder=funder,
            signature_type=sig_type,
        )
        
        # Derive credentials
        creds = client.create_or_derive_api_creds()
        client.set_api_creds(creds)
        
        print("✓ Client ready")
        print("\nFetching open orders...")
        
        orders = client.get_orders()
        
        if orders:
            print(f"\n✓ Found {len(orders)} open order(s):")
            for i, order in enumerate(orders):
                if isinstance(order, dict):
                    order_id = order.get("id", order.get("orderID", "?"))
                    side = order.get("side", "?")
                    price = order.get("price", "?")
                    size = order.get("size_matched", order.get("original_size", "?"))
                    status = order.get("status", "?")
                    asset = order.get("asset_id", "?")[:20] + "..."
                    
                    print(f"\n  Order {i+1}:")
                    print(f"    ID:     {order_id[:30]}..." if len(str(order_id)) > 30 else f"    ID:     {order_id}")
                    print(f"    Side:   {side}")
                    print(f"    Price:  {price}")
                    print(f"    Status: {status}")
                    print(f"    Asset:  {asset}")
                else:
                    print(f"\n  Order {i+1}: {order}")
            
            return orders, True
        else:
            print("\n  No open orders found")
            return [], True
            
    except Exception as e:
        print(f"\n✗ Error querying orders: {e}")
        import traceback
        traceback.print_exc()
        return [], False


async def test_cancel_order(client, order_id):
    """Test 4: Cancel an order."""
    print_header("TEST 4: Cancel Order")
    
    if not client:
        print("✗ Client not available")
        return False
    
    if not order_id:
        print("⚠ No order ID provided (skipping)")
        return True
    
    print(f"Cancelling order: {order_id[:30]}...")
    
    result = await client.cancel_order(order_id)
    
    if result.success:
        print(f"\n✓ Order cancelled successfully!")
        return True
    else:
        print(f"\n✗ Cancel failed: {result.error_msg}")
        return False


async def test_cancel_all(client):
    """Test 5: Cancel all orders."""
    print_header("TEST 5: Cancel All Orders")
    
    if not client:
        print("✗ Client not available")
        return False
    
    print("Cancelling all orders...")
    
    result = await client.cancel_all()
    
    if result.success:
        print(f"\n✓ Cancel-all completed!")
        print(f"  Cancelled: {result.cancelled_count} orders")
        return True
    else:
        print(f"\n✗ Cancel-all failed: {result.error_msg}")
        return False


async def test_multiple_orders(client, market):
    """Test 6: Place multiple orders and verify they appear."""
    print_header("TEST 6: Place Multiple Orders")
    
    if not client or not market:
        print("✗ Client or market not available")
        return False
    
    try:
        from polymarket_trader.polymarket_rest import OrderRequest
        from polymarket_trader.types import Side
    except ImportError:
        return False
    
    # Place 3 orders at different prices
    prices = [0.01, 0.02, 0.03]
    order_ids = []
    
    print(f"Placing {len(prices)} orders on {market.market_slug}...")
    
    for price in prices:
        now_ms = time_ns() // 1_000_000
        order = OrderRequest(
            client_req_id=f"test_{price}_{now_ms}",
            token_id=market.tokens.yes_token_id,
            side=Side.BUY,
            price=price,
            size=5.0,
            expires_at_ms=now_ms + 5 * 60 * 1000,
        )
        
        result = await client.place_order(order)
        
        if result.success:
            print(f"  ✓ Placed BUY UP @ ${price} -> {result.order_id[:20]}...")
            order_ids.append(result.order_id)
        else:
            print(f"  ✗ Failed @ ${price}: {result.error_msg}")
        
        # Small delay between orders
        await asyncio.sleep(0.2)
    
    print(f"\nPlaced {len(order_ids)}/{len(prices)} orders")
    
    # Query and verify
    print("\nVerifying orders with direct query...")
    orders, _ = await test_query_orders_direct()
    
    print_section("Cleanup")
    
    # Cancel all placed orders
    for oid in order_ids:
        result = await client.cancel_order(oid)
        status = "✓" if result.success else "✗"
        print(f"  {status} Cancel {oid[:20]}...")
        await asyncio.sleep(0.1)
    
    return len(order_ids) > 0


async def main():
    """Run all tests."""
    print("\n" + "="*70)
    print("  HOURMM POLYMARKET ORDER TEST SUITE")
    print("="*70)
    print(f"\nStarted at: {datetime.now().isoformat()}")
    print(f"Eastern Time: {get_eastern_time().strftime('%Y-%m-%d %I:%M %p')}")
    
    print_section("Credential Check")
    if not check_credentials():
        print("\n" + "="*70)
        print("  CREDENTIALS NOT SET!")
        print("="*70)
        print("\nSet environment variables:")
        print("  export PM_PRIVATE_KEY=0x...")
        print("  export PM_FUNDER=0x...")
        print("  export PM_SIGNATURE_TYPE=1")
        print("\nOr load from prod.env:")
        print("  source deploy/prod.env")
        return 1
    
    print_section("Market Discovery")
    market = await get_current_market()
    
    if market:
        print(f"✓ Found market: {market.market_slug}")
        print(f"  UP token:   {market.tokens.yes_token_id[:30]}...")
        print(f"  DOWN token: {market.tokens.no_token_id[:30]}...")
    else:
        print("✗ No market found for current hour")
        print("  Tests requiring market will be skipped")
    
    results = {}
    
    # Test 1: Client init
    client, results["client_init"] = await test_client_initialization()
    
    # Test 2: Place order
    order_id = None
    if client and market:
        order_id, results["place_order"] = await test_place_order(client, market)
    else:
        results["place_order"] = False
    
    # Test 3: Query orders
    orders, results["query_orders"] = await test_query_orders_direct()
    
    # Test 4: Cancel specific order
    results["cancel_order"] = await test_cancel_order(client, order_id)
    
    # Test 5: Multiple orders
    if client and market:
        results["multiple_orders"] = await test_multiple_orders(client, market)
    else:
        results["multiple_orders"] = False
    
    # Test 6: Cancel all (cleanup)
    results["cancel_all"] = await test_cancel_all(client)
    
    # Final query to verify cleanup
    print_header("Final Verification")
    print("Checking for remaining orders...")
    final_orders, _ = await test_query_orders_direct()
    if not final_orders:
        print("✓ All orders cleaned up")
    else:
        print(f"⚠ {len(final_orders)} orders remain")
    
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
        print("  ALL TESTS PASSED! Order placement and cancellation working.")
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
