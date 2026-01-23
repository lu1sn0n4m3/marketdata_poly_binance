"""
Gateway integration test - places real orders on Polymarket.

This test places low-value bids ($0.01, $0.02) on the current market
to verify the Gateway can handle real order flow.
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

from tradingsystem.mm_types import Token, Side, RealOrderSpec, ExecutorEventType
from tradingsystem.gamma_client import GammaClient
from tradingsystem.market_finder import BitcoinHourlyMarketFinder, get_current_hour_et
from tradingsystem.pm_rest_client import PolymarketRestClient
from tradingsystem.gateway import Gateway


def load_credentials():
    """Load credentials from environment or prod.env."""
    # Try environment first (check both naming conventions)
    private_key = os.environ.get("PM_PRIVATE_KEY") or os.environ.get("POLYMARKET_PRIVATE_KEY")
    funder = os.environ.get("PM_FUNDER") or os.environ.get("POLYMARKET_FUNDER_ADDRESS", "")
    signature_type = int(os.environ.get("PM_SIGNATURE_TYPE", "1"))

    if not private_key:
        # Try prod.env first, then prod.env.template
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
        raise ValueError("PM_PRIVATE_KEY not found in environment or deploy/*.env files")

    return private_key, funder, signature_type


def main():
    """Run the gateway integration test."""
    print("=" * 60)
    print("GATEWAY INTEGRATION TEST")
    print("=" * 60)

    # Load credentials
    try:
        private_key, funder, signature_type = load_credentials()
        print(f"✓ Loaded credentials (funder: {funder[:10] if funder else 'None'}..., sig_type: {signature_type})")
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
    print(f"  YES Token: {market_info.yes_token_id[:20]}...")
    print(f"  NO Token: {market_info.no_token_id[:20]}...")
    if market_info.reference_price:
        print(f"  Reference price: ${market_info.reference_price:,.2f}")
    else:
        print(f"  Reference price: N/A")

    # Initialize REST client
    print("\n--- Initializing REST Client ---")
    try:
        rest_client = PolymarketRestClient(
            private_key=private_key,
            funder=funder,
            signature_type=signature_type,
        )
        # Force initialization
        rest_client._ensure_initialized()
        print(f"✓ REST client initialized")
        print(f"  API key: {rest_client._api_key[:20]}...")
    except Exception as e:
        print(f"✗ Failed to initialize REST client: {e}")
        import traceback
        traceback.print_exc()
        return

    # Setup Gateway
    print("\n--- Setting up Gateway ---")
    result_queue = queue.Queue(maxsize=100)
    gateway = Gateway(
        rest_client=rest_client,
        result_queue=result_queue,
        min_action_interval_ms=100,  # Slow down for testing
    )
    gateway.start()
    print("✓ Gateway started")

    # Place test orders
    print("\n--- Placing Test Orders ---")

    # Order 1: YES bid at 1 cent
    spec1 = RealOrderSpec(
        token=Token.YES,
        token_id=market_info.yes_token_id,
        side=Side.BUY,
        px=1,  # 1 cent
        sz=10,  # 10 shares = $0.10 max cost
        client_order_id=f"test_1_{int(time.time())}",
    )
    action_id_1 = gateway.submit_place(spec1)
    print(f"✓ Submitted order 1 (YES BUY @ $0.01): action_id={action_id_1}")

    # Order 2: YES bid at 2 cents
    spec2 = RealOrderSpec(
        token=Token.YES,
        token_id=market_info.yes_token_id,
        side=Side.BUY,
        px=2,  # 2 cents
        sz=10,  # 10 shares = $0.20 max cost
        client_order_id=f"test_2_{int(time.time())}",
    )
    action_id_2 = gateway.submit_place(spec2)
    print(f"✓ Submitted order 2 (YES BUY @ $0.02): action_id={action_id_2}")

    # Wait for results
    print("\n--- Waiting for Results ---")
    results = {}
    server_order_ids = []

    start = time.time()
    timeout = 30  # 30 seconds timeout

    while len(results) < 2 and (time.time() - start) < timeout:
        try:
            event = result_queue.get(timeout=1.0)
            results[event.action_id] = event
            print(f"\n✓ Received result for {event.action_id}:")
            print(f"  Success: {event.success}")
            print(f"  Server Order ID: {event.server_order_id}")
            if event.error_kind:
                print(f"  Error: {event.error_kind}")
            if event.server_order_id:
                server_order_ids.append(event.server_order_id)
        except queue.Empty:
            print(".", end="", flush=True)

    print(f"\n\n--- Results Summary ---")
    print(f"Received {len(results)} / 2 results")

    for action_id, event in results.items():
        status = "SUCCESS" if event.success else f"FAILED ({event.error_kind})"
        print(f"  {action_id}: {status}")

    # Cancel any successful orders
    if server_order_ids:
        print(f"\n--- Cancelling {len(server_order_ids)} Order(s) ---")

        for order_id in server_order_ids:
            cancel_action_id = gateway.submit_cancel(order_id)
            print(f"  Submitted cancel for {order_id}: action_id={cancel_action_id}")

        # Wait for cancel results
        cancel_results = 0
        while cancel_results < len(server_order_ids) and (time.time() - start) < timeout + 10:
            try:
                event = result_queue.get(timeout=1.0)
                cancel_results += 1
                status = "CANCELLED" if event.success else f"FAILED ({event.error_kind})"
                print(f"  Cancel result: {status}")
            except queue.Empty:
                pass

    # Cleanup
    print("\n--- Cleanup ---")
    gateway.stop()
    print("✓ Gateway stopped")

    # Final stats
    stats = gateway.stats
    if stats:
        print(f"\n--- Gateway Stats ---")
        print(f"  Actions processed: {stats.actions_processed}")
        print(f"  Actions succeeded: {stats.actions_succeeded}")
        print(f"  Actions failed: {stats.actions_failed}")

    print("\n" + "=" * 60)
    print("TEST COMPLETE")
    print("=" * 60)


if __name__ == "__main__":
    main()
