"""
Fill integration test - places an order that will get filled and verifies fill notification.

This test places a marketable order (at a price that will execute) to verify
the full flow: Gateway → Exchange → User WS → FillEvent.
"""

import os
import sys
import time
import queue
import asyncio
import logging

# Setup logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Add project root to path for proper imports
project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, project_root)

from tradingsystem.types import Token, Side, RealOrderSpec, ExecutorEventType, FillEvent, OrderAckEvent
from tradingsystem.clients import GammaClient, BitcoinHourlyMarketFinder, get_current_hour_et, PolymarketRestClient
from tradingsystem.gateway import Gateway
from tradingsystem.feeds import PolymarketUserFeed


def load_credentials():
    """Load credentials from environment or prod.env."""
    private_key = os.environ.get("PM_PRIVATE_KEY") or os.environ.get("POLYMARKET_PRIVATE_KEY")
    funder = os.environ.get("PM_FUNDER") or os.environ.get("POLYMARKET_FUNDER_ADDRESS", "")
    signature_type = int(os.environ.get("PM_SIGNATURE_TYPE", "1"))

    if not private_key:
        deploy_dir = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
            "deploy"
        )
        env_files = ["prod.env", "prod.env.template"]

        for env_name in env_files:
            env_file = os.path.join(deploy_dir, env_name)
            if os.path.exists(env_file):
                with open(env_file) as f:
                    for line in f:
                        line = line.strip()
                        if not line or line.startswith("#"):
                            continue
                        if line.startswith("PM_PRIVATE_KEY="):
                            private_key = line.split("=", 1)[1].strip().strip('"').strip("'")
                        elif line.startswith("PM_FUNDER="):
                            funder = line.split("=", 1)[1].strip().strip('"').strip("'")
                        elif line.startswith("PM_SIGNATURE_TYPE="):
                            signature_type = int(line.split("=", 1)[1].strip().strip('"').strip("'"))
                if private_key:
                    break

    if not private_key:
        raise ValueError("PM_PRIVATE_KEY not found")

    return private_key, funder, signature_type


def main():
    """Run the fill integration test."""
    print("=" * 60)
    print("FILL INTEGRATION TEST")
    print("=" * 60)

    # Load credentials
    try:
        private_key, funder, signature_type = load_credentials()
        print(f"✓ Loaded credentials")
    except Exception as e:
        print(f"✗ Failed to load credentials: {e}")
        return

    # Get current market
    print("\n--- Finding Current Market ---")
    gamma = GammaClient()
    finder = BitcoinHourlyMarketFinder(gamma)

    current_hour = get_current_hour_et()
    print(f"Current hour (ET): {current_hour}")

    market_info = asyncio.run(finder.find_current_market())
    if not market_info:
        print("✗ No active market found")
        return

    print(f"✓ Found market: {market_info.slug}")
    print(f"  Condition ID: {market_info.condition_id}")

    # Initialize REST client
    print("\n--- Initializing REST Client ---")
    try:
        rest_client = PolymarketRestClient(
            private_key=private_key,
            funder=funder,
            signature_type=signature_type,
        )
        rest_client._ensure_initialized()
        api_key, api_secret, passphrase = rest_client.api_credentials
        print(f"✓ REST client initialized")
    except Exception as e:
        print(f"✗ Failed to initialize REST client: {e}")
        import traceback
        traceback.print_exc()
        return

    # Setup event queue (shared between Gateway and User WS)
    event_queue = queue.Queue(maxsize=1000)

    # Setup User WebSocket for fill notifications
    print("\n--- Setting up User WebSocket ---")
    user_ws = PolymarketUserFeed(
        event_queue=event_queue,
        api_key=api_key,
        api_secret=api_secret,
        passphrase=passphrase,
    )
    user_ws.set_markets([market_info.condition_id])
    user_ws.start()
    print("✓ User WebSocket started")

    # Wait for WS connection
    time.sleep(2)

    # Setup Gateway
    print("\n--- Setting up Gateway ---")
    gateway_result_queue = queue.Queue(maxsize=100)
    gateway = Gateway(
        rest_client=rest_client,
        result_queue=gateway_result_queue,
        min_action_interval_ms=100,
    )
    gateway.start()
    print("✓ Gateway started")

    # Place a marketable order (high price YES BUY = likely to fill)
    # At 5 cents, if there's any ask at 5c or below, we'll get filled
    # Minimum marketable order is $1, so we need 20+ shares at 5c
    print("\n--- Placing Marketable Order ---")
    print("  Placing YES BUY @ $0.05 x 25 shares (marketable - should fill)")

    spec = RealOrderSpec(
        token=Token.YES,
        token_id=market_info.yes_token_id,
        side=Side.BUY,
        px=5,  # 5 cents - should be marketable
        sz=25,  # 25 shares @ 5c = $1.25 max cost (above $1 min)
        client_order_id=f"fill_test_{int(time.time())}",
    )
    action_id = gateway.submit_place(spec)
    print(f"✓ Submitted order: action_id={action_id}")

    # Wait for Gateway result
    print("\n--- Waiting for Gateway Result ---")
    try:
        gw_result = gateway_result_queue.get(timeout=10)
        print(f"✓ Gateway result: success={gw_result.success}, order_id={gw_result.server_order_id}")
        server_order_id = gw_result.server_order_id
    except queue.Empty:
        print("✗ Timeout waiting for Gateway result")
        gateway.stop()
        user_ws.stop()
        return

    if not gw_result.success:
        print(f"✗ Order placement failed: {gw_result.error_kind}")
        gateway.stop()
        user_ws.stop()
        return

    # Wait for fill event from User WebSocket
    print("\n--- Waiting for Fill Event (10s timeout) ---")
    fills_received = []
    order_acks_received = []
    start = time.time()
    timeout = 10

    while (time.time() - start) < timeout:
        try:
            event = event_queue.get(timeout=0.5)

            if isinstance(event, FillEvent):
                fills_received.append(event)
                print(f"\n✓ FILL EVENT RECEIVED!")
                print(f"  Order ID: {event.server_order_id}")
                print(f"  Token: {event.token.name}")
                print(f"  Side: {event.side.name}")
                print(f"  Price: ${event.price / 100:.2f}")
                print(f"  Size: {event.size}")
                print(f"  Exchange TS: {event.ts_exchange}")

            elif isinstance(event, OrderAckEvent):
                order_acks_received.append(event)
                print(f"\n  Order Ack: status={event.status.name}, id={event.server_order_id[:20]}...")

            else:
                print(f"  Other event: {type(event).__name__}")

        except queue.Empty:
            print(".", end="", flush=True)

    # Summary
    print("\n\n" + "=" * 60)
    print("RESULTS SUMMARY")
    print("=" * 60)
    print(f"Order Acks received: {len(order_acks_received)}")
    print(f"Fill Events received: {len(fills_received)}")

    if fills_received:
        print("\n✓ SUCCESS - Fill notification received through User WebSocket!")
        for i, fill in enumerate(fills_received):
            print(f"\nFill {i+1}:")
            print(f"  Order ID: {fill.server_order_id}")
            print(f"  Token: {fill.token.name}")
            print(f"  Side: {fill.side.name}")
            print(f"  Price: ${fill.price / 100:.2f}")
            print(f"  Size: {fill.size}")
    else:
        print("\n⚠ No fill received - order may not have been marketable")
        print("  (This could mean there was no liquidity at 5 cents)")

    # Cleanup
    print("\n--- Cleanup ---")
    gateway.stop()
    user_ws.stop()
    print("✓ All components stopped")

    print("\n" + "=" * 60)
    print("TEST COMPLETE")
    print("=" * 60)


if __name__ == "__main__":
    main()
