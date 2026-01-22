#!/usr/bin/env python3
"""
End-to-End Test: Full HourMM Pipeline

Tests the complete HourMM framework without Docker:
1. Container A (Binance Pricer):
   - BinanceWsClient streams real-time data
   - FeatureEngine computes features
   - Pricer calculates fair value
   - SnapshotPublisher builds snapshots
   - SnapshotServer exposes HTTP endpoint

2. Container B (Polymarket Trader):
   - SnapshotPoller fetches from Container A at 2 Hz
   - Dummy Strategy: Always places BUY YES at $0.01, size 1, TTL 0.5s
   - OrderManager reconciles desired vs actual orders
   - Executor places/cancels orders via PolymarketRestClient

REQUIRES CREDENTIALS in deploy/prod.env:
    PM_PRIVATE_KEY=0x...
    PM_FUNDER=0x...
    PM_SIGNATURE_TYPE=1

Usage:
    python tests/test_end_to_end.py

Press Ctrl+C to stop.
"""

import asyncio
import sys
import os
from datetime import datetime, timezone
from time import time_ns
from typing import Optional
from dataclasses import dataclass

# Add paths for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "services/binance_pricer/src"))
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


# ============================================================================
# Container A Components
# ============================================================================

from binance_pricer.binance_ws import BinanceWsClient
from binance_pricer.types import BinanceEvent, HourContext
from binance_pricer.hour_context import HourContextBuilder
from binance_pricer.feature_engine import DefaultFeatureEngine
from binance_pricer.pricer import BaselinePricer
from binance_pricer.snapshot_store import LatestSnapshotStore
from binance_pricer.snapshot_publisher import SnapshotPublisher
from binance_pricer.snapshot_server import SnapshotServer
from binance_pricer.health import HealthTracker

# ============================================================================
# Container B Components (with Dummy Strategy)
# ============================================================================

from hourmm_common.schemas import BinanceSnapshot
from hourmm_common.enums import Side, OrderPurpose
from polymarket_trader.types import StrategyIntent, QuoteSet, DesiredOrder
from polymarket_trader.gamma_client import GammaClient
from polymarket_trader.polymarket_rest import PolymarketRestClient
from polymarket_trader.snapshot_poller import SnapshotPoller


class DummyStrategy:
    """
    Dummy strategy that always places one BUY YES order at $0.01.
    
    Ignores snapshot pricing. Used for end-to-end testing.
    """
    
    def __init__(self, token_id: str):
        self.token_id = token_id
    
    def compute_intent(self, snapshot: Optional[BinanceSnapshot]) -> StrategyIntent:
        """
        Always return intent to buy YES at $0.01 with size 1.
        
        Args:
            snapshot: Ignored (dummy strategy)
        
        Returns:
            StrategyIntent with single BUY order at $0.01
        """
        # Calculate expiration
        # Polymarket requires: now + 60 seconds (security threshold) + desired TTL
        # Adding extra 2 seconds for clock skew + 2 seconds desired TTL
        now_ms = time_ns() // 1_000_000
        expires_at_ms = now_ms + 64_000  # 62 seconds buffer + 2 seconds TTL
        
        # Create a single buy order at $0.01
        # Minimum size is 5 contracts per Polymarket
        buy_order = DesiredOrder(
            side=Side.BUY,
            price=0.01,
            size=5.0,
            purpose=OrderPurpose.QUOTE,
            expires_at_ms=expires_at_ms,
            token_id=self.token_id
        )
        
        quotes = QuoteSet(
            bid=buy_order,
            ask=None  # No sell orders
        )
        
        return StrategyIntent(
            quotes=quotes
        )


# ============================================================================
# End-to-End Test Runner
# ============================================================================

class EndToEndTest:
    """Orchestrates the full HourMM pipeline test."""
    
    def __init__(self):
        # Container A components
        self.binance_ws: Optional[BinanceWsClient] = None
        self.health: Optional[HealthTracker] = None
        self.hour_ctx_builder: Optional[HourContextBuilder] = None
        self.feature_engine: Optional[DefaultFeatureEngine] = None
        self.pricer: Optional[BaselinePricer] = None
        self.snapshot_store: Optional[LatestSnapshotStore] = None
        self.snapshot_publisher: Optional[SnapshotPublisher] = None
        self.snapshot_server: Optional[SnapshotServer] = None
        
        # Container B components
        self.gamma_client: Optional[GammaClient] = None
        self.rest_client: Optional[PolymarketRestClient] = None
        self.snapshot_poller: Optional[SnapshotPoller] = None
        self.strategy: Optional[DummyStrategy] = None
        
        # State
        self.token_id_yes: Optional[str] = None
        self.market_slug: Optional[str] = None
        self.snapshots_received = 0
        self.orders_placed = 0
        self.orders_cancelled = 0
        self.order_counter = 0
        
    async def setup_container_a(self):
        """Initialize Container A (Binance Pricer)."""
        print("\n=== Setting up Container A (Binance Pricer) ===\n")
        
        # Initialize components
        self.health = HealthTracker(stale_threshold_ms=5000)
        self.hour_ctx_builder = HourContextBuilder()
        self.feature_engine = DefaultFeatureEngine()
        self.pricer = BaselinePricer()
        self.snapshot_store = LatestSnapshotStore()
        
        # Create Binance event handler
        async def on_binance_event(event: BinanceEvent):
            """Handle incoming Binance events."""
            # Update health
            self.health.update_on_event(
                ts_local_ms=event.ts_local_ms,
                ts_exchange_ms=event.ts_exchange_ms
            )
            
            # Update hour context
            self.hour_ctx_builder.update_from_event(event)
            
            # Get current context
            ctx = self.hour_ctx_builder.get_context(event.ts_local_ms)
            
            # Update features
            self.feature_engine.update(event, ctx)
        
        # Create Binance WebSocket client
        self.binance_ws = BinanceWsClient(
            symbol="BTCUSDT",
            on_event=on_binance_event
        )
        
        # Create snapshot publisher (2 Hz)
        self.snapshot_publisher = SnapshotPublisher(
            publish_hz=2.0,
            store=self.snapshot_store,
            ctx_builder=self.hour_ctx_builder,
            feature_engine=self.feature_engine,
            pricer=self.pricer,
            health=self.health
        )
        
        # Create HTTP server
        self.snapshot_server = SnapshotServer(
            store=self.snapshot_store,
            host="127.0.0.1",
            port=8080
        )
        
        print("✓ Container A components initialized")
        print("  - BinanceWsClient: BTCUSDT")
        print("  - SnapshotPublisher: 2 Hz")
        print("  - SnapshotServer: http://127.0.0.1:8080")
    
    async def setup_container_b(self):
        """Initialize Container B (Polymarket Trader)."""
        print("\n=== Setting up Container B (Polymarket Trader) ===\n")
        
        # Get credentials from environment
        private_key = os.environ.get("PM_PRIVATE_KEY", "")
        funder = os.environ.get("PM_FUNDER", "")
        sig_type = int(os.environ.get("PM_SIGNATURE_TYPE", "1"))
        market_slug_base = os.environ.get("MARKET_SLUG", "bitcoin-up-or-down")
        
        if not private_key or not funder:
            raise ValueError("Missing PM_PRIVATE_KEY or PM_FUNDER in environment")
        
        print(f"  Private Key: {private_key[:10]}...{private_key[-6:]}")
        print(f"  Funder: {funder}")
        print(f"  Signature Type: {sig_type}")
        
        # Initialize Gamma client for market discovery
        self.gamma_client = GammaClient()
        
        # Discover current market
        print(f"\n  Discovering market for: {market_slug_base}")
        now_ms = time_ns() // 1_000_000
        market_info = await self.gamma_client.get_current_hour_market(market_slug_base, now_ms)
        
        if not market_info:
            raise ValueError(f"Could not find current market for {market_slug_base}")
        
        self.market_slug = market_info.market_slug
        self.token_id_yes = market_info.tokens.yes_token_id
        
        print(f"  ✓ Found market: {self.market_slug}")
        print(f"  ✓ YES token ID: {self.token_id_yes}")
        
        # Initialize REST client
        self.rest_client = PolymarketRestClient(
            private_key=private_key,
            funder=funder,
            signature_type=sig_type
        )
        await self.rest_client.initialize()
        print(f"  ✓ PolymarketRestClient initialized")
        
        # Initialize snapshot poller (2 Hz to match publisher)
        self.snapshot_poller = SnapshotPoller(
            url="http://127.0.0.1:8080/snapshot/latest",
            poll_hz=2
        )
        print(f"  ✓ SnapshotPoller: 2 Hz")
        
        # Initialize dummy strategy
        self.strategy = DummyStrategy(token_id=self.token_id_yes)
        print(f"  ✓ DummyStrategy: Always BUY YES at $0.01, size 5 (min), TTL 500ms")
    
    async def run_container_a(self, shutdown_event: asyncio.Event):
        """Run Container A tasks."""
        await asyncio.gather(
            self.binance_ws.run(shutdown_event),
            self.snapshot_publisher.run(shutdown_event),
            self.snapshot_server.start()
        )
    
    async def run_container_b(self, shutdown_event: asyncio.Event):
        """Run Container B decision loop."""
        # Start snapshot poller
        asyncio.create_task(self.snapshot_poller.run(shutdown_event))
        
        # Wait for first snapshot
        await asyncio.sleep(2)
        
        print("\n=== Starting Trading Loop ===\n")
        
        # Check open orders before starting
        print("Checking account status...")
        try:
            open_orders = await self.rest_client.get_open_orders()
            print(f"  Open orders before start: {len(open_orders) if open_orders else 0}")
        except Exception as e:
            print(f"  ✗ Failed to check orders: {e}")
        
        # Cancel all existing orders before starting
        print("\nCancelling all existing orders...")
        try:
            await self.rest_client.cancel_all()
            await asyncio.sleep(1)  # Wait for cancellations to process
            print("✓ All orders cancelled")
            
            # Verify cancellation
            open_orders = await self.rest_client.get_open_orders()
            print(f"  Open orders after cancel: {len(open_orders) if open_orders else 0}\n")
        except Exception as e:
            print(f"✗ Cancel all failed: {e}\n")
        
        last_order_id = None
        
        while not shutdown_event.is_set():
            try:
                # Get latest snapshot
                snapshot = self.snapshot_poller.latest
                
                if snapshot:
                    self.snapshots_received += 1
                    
                    # Compute intent from dummy strategy
                    intent = self.strategy.compute_intent(snapshot)
                    
                    # Cancel previous order if exists
                    if last_order_id:
                        try:
                            cancel_result = await self.rest_client.cancel_order(last_order_id)
                            if cancel_result.success:
                                self.orders_cancelled += 1
                                print(f"  ✓ Cancelled previous order: {last_order_id[:16]}...")
                            else:
                                print(f"  ⚠ Cancel unsuccessful: {cancel_result.error_msg}")
                        except Exception as e:
                            print(f"  ✗ Cancel failed: {e}")
                    
                    # Place new order
                    if intent.quotes.bid:
                        order = intent.quotes.bid
                        btc_price = snapshot.last_trade_price or 0
                        
                        print(f"\n[Snapshot #{snapshot.seq}] BTC: ${btc_price:.2f} | Placing BUY at $0.01")
                        
                        try:
                            from polymarket_trader.polymarket_rest import OrderRequest
                            
                            # Generate unique client request ID
                            client_req_id = f"e2e_test_{self.order_counter}_{time_ns()}"
                            self.order_counter += 1
                            
                            order_req = OrderRequest(
                                client_req_id=client_req_id,
                                token_id=order.token_id,
                                side=order.side,
                                price=order.price,
                                size=order.size,
                                expires_at_ms=order.expires_at_ms
                            )
                            
                            ack = await self.rest_client.place_order(order_req)
                            
                            if ack.success and ack.order_id:
                                last_order_id = ack.order_id
                                self.orders_placed += 1
                                print(f"  ✓ Order placed: {ack.order_id[:16]}...")
                            else:
                                last_order_id = None
                                print(f"  ✗ Order failed: {ack.error_msg}")
                            
                        except Exception as e:
                            print(f"  ✗ Order exception: {e}")
                            last_order_id = None
                        
                        # Query and display open orders
                        try:
                            open_orders = await self.rest_client.get_open_orders()
                            if open_orders:
                                print(f"  📋 Open orders: {len(open_orders)}")
                                total_size = sum(float(o.get('original_size', 0)) for o in open_orders if isinstance(o, dict))
                                print(f"     Total size committed: {total_size:.2f} contracts")
                                for i, o in enumerate(open_orders[:3]):  # Show first 3
                                    if isinstance(o, dict):
                                        oid = o.get('id', 'N/A')[:12]
                                        size = o.get('original_size', 0)
                                        price = o.get('price', 0)
                                        print(f"     [{i+1}] {oid}... size={size} @ ${price}")
                            else:
                                print(f"  📋 Open orders: 0")
                        except Exception as e:
                            print(f"  ✗ Failed to get open orders: {e}")
                
                # Poll at 2 Hz (matches snapshot rate)
                # Note: Due to Polymarket's 60-second minimum expiration,
                # orders live longer than ideal, but we cancel them before placing new ones
                await asyncio.sleep(0.5)
                
            except asyncio.CancelledError:
                # Cleanup: cancel last order
                if last_order_id:
                    try:
                        await self.rest_client.cancel_order(last_order_id)
                    except:
                        pass
                raise
            except Exception as e:
                print(f"\n✗ Error in trading loop: {e}")
                await asyncio.sleep(1)
    
    async def print_stats(self, shutdown_event: asyncio.Event):
        """Periodically print statistics."""
        while not shutdown_event.is_set():
            try:
                await asyncio.wait_for(shutdown_event.wait(), timeout=10.0)
                break
            except asyncio.TimeoutError:
                print(f"\n--- Stats (after 10s) ---")
                print(f"  Snapshots received: {self.snapshots_received}")
                print(f"  Orders placed: {self.orders_placed}")
                print(f"  Orders cancelled: {self.orders_cancelled}")
    
    async def run(self):
        """Run the full end-to-end test."""
        shutdown_event = asyncio.Event()
        
        try:
            # Setup both containers
            await self.setup_container_a()
            await self.setup_container_b()
            
            print("\n" + "="*70)
            print("  STARTING END-TO-END TEST")
            print("="*70)
            print("\nPress Ctrl+C to stop.\n")
            
            # Run all tasks
            await asyncio.gather(
                self.run_container_a(shutdown_event),
                self.run_container_b(shutdown_event),
                self.print_stats(shutdown_event)
            )
            
        except KeyboardInterrupt:
            print("\n\n=== Shutting down ===\n")
            shutdown_event.set()
            await asyncio.sleep(0.5)  # Give tasks time to clean up
            
            # Cleanup
            if self.gamma_client:
                await self.gamma_client.close()
            
            print(f"Final Stats:")
            print(f"  Snapshots received: {self.snapshots_received}")
            print(f"  Orders placed: {self.orders_placed}")
            print(f"  Orders cancelled: {self.orders_cancelled}")
            print("\n✓ Test completed")


async def main():
    """Main entry point."""
    test = EndToEndTest()
    await test.run()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n✓ Stopped by user")
        sys.exit(0)
