#!/usr/bin/env python3
"""
Production-Style End-to-End Test

Tests the full HourMM framework with human-readable output.

Uses a WalkingStrategy that cycles bid prices: 1c -> 2c -> 3c -> 2c -> 1c...
to demonstrate order placement, cancellation, and state management.

REQUIRES CREDENTIALS in deploy/prod.env:
    PM_PRIVATE_KEY=0x...
    PM_FUNDER=0x...
    PM_SIGNATURE_TYPE=1

Usage:
    python tests/test_production_e2e.py

Press Ctrl+C to stop.
"""

import asyncio
import sys
import os
import logging
from datetime import datetime, timezone
from time import time_ns
from typing import Optional

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


# =============================================================================
# LOGGING SETUP - Suppress noisy loggers, keep only what matters
# =============================================================================

def setup_quiet_logging():
    """Configure logging to show only important messages."""
    # Suppress noisy third-party loggers
    for name in [
        "httpx",
        "httpcore", 
        "aiohttp.access",
        "aiohttp.client",
        "websockets",
        "urllib3",
        "asyncio",
    ]:
        logging.getLogger(name).setLevel(logging.WARNING)
    
    # Suppress internal framework noise
    for name in [
        "polymarket_trader.executor",
        "polymarket_trader.reducer",
        "polymarket_trader.decision_loop",
        "polymarket_trader.polymarket_rest",
        "polymarket_trader.polymarket_ws_market",
        "polymarket_trader.polymarket_ws_user",
        "polymarket_trader.snapshot_poller",
        "polymarket_trader.reconciler",
        "binance_pricer.binance_ws",
        "binance_pricer.snapshot_publisher",
    ]:
        logging.getLogger(name).setLevel(logging.WARNING)
    
    # Only show errors from most components
    logging.getLogger("polymarket_trader").setLevel(logging.WARNING)
    logging.getLogger("binance_pricer").setLevel(logging.WARNING)

# Set up basic logging first (for startup), then quiet it
logging.basicConfig(
    level=logging.INFO,
    format='%(message)s'  # Simple format - just the message
)
setup_quiet_logging()


# =============================================================================
# IMPORTS (after logging setup)
# =============================================================================

from binance_pricer.app import ContainerAApp
from binance_pricer.config import AConfig
from binance_pricer.types import HourContext, PricerOutput
from binance_pricer.pricer import Pricer
from polymarket_trader.app import ContainerBApp
from polymarket_trader.config import BConfig
from polymarket_trader.types import (
    DecisionContext, StrategyIntent, DesiredOrder, QuoteSet, OrderPurpose,
    OrderActionType, CanonicalStateView,
)
from polymarket_trader.strategy import Strategy
from polymarket_trader.order_manager import OrderManager, DesiredOrders, WorkingOrders
from polymarket_trader.simple_strategy import (
    SimpleStrategy, StrategyContext, TargetQuotes
)
from polymarket_trader.simple_executor import SimpleExecutor, OpenOrder
from hourmm_common.schemas import BinanceSnapshot
from hourmm_common.enums import Side


# =============================================================================
# DUMMY COMPONENTS
# =============================================================================

class DummyPricer(Pricer):
    """Dummy pricer - always returns p_yes = 0.51."""
    
    @property
    def name(self) -> str:
        return "DummyPricer"
    
    @property
    def ready(self) -> bool:
        return True
    
    def price(self, ctx: HourContext, features: dict[str, float]) -> PricerOutput:
        return PricerOutput(p_yes_fair=0.51, ready=True)
    
    def reset_for_new_hour(self, ctx: HourContext) -> None:
        pass


class WalkingStrategy(SimpleStrategy):
    """
    Simple strategy that walks bid prices up and down: 1c -> 2c -> 3c -> 2c -> 1c...
    
    This demonstrates order placement and cancellation flow.
    Always places exactly 1 bid order at the current price level.
    """
    
    def __init__(self):
        self._tick_count = 0
        # Price levels to walk: 1c, 2c, 3c, 2c, 1c, 2c, 3c...
        self._price_sequence = [0.01, 0.02, 0.03, 0.02]  # Repeating pattern
        self._last_targets: TargetQuotes = None
    
    @property
    def current_price(self) -> float:
        """Current price in the walking sequence."""
        idx = self._tick_count % len(self._price_sequence)
        return self._price_sequence[idx]
    
    def compute_quotes(self, ctx: StrategyContext) -> TargetQuotes:
        """Place a bid at the current walking price."""
        self._tick_count += 1
        
        price = self.current_price
        
        self._last_targets = TargetQuotes(
            bid_price=price,
            bid_size=5.0,
            ask_price=None,  # No ask
            ask_size=0.0,
        )
        return self._last_targets


def build_strategy_context(
    state_view: CanonicalStateView,
    snapshot: Optional[BinanceSnapshot],
    now_ms: int,
    t_remaining_ms: int,
) -> StrategyContext:
    """Bridge function: Convert state view to StrategyContext."""
    from polymarket_trader.simple_strategy import StrategyContext
    
    # Extract position
    position = state_view.positions.net_exposure if state_view.positions else 0.0
    
    # Extract market prices
    market_bid = state_view.market_view.best_bid
    market_ask = state_view.market_view.best_ask
    tick_size = state_view.market_view.tick_size or 0.01
    
    # Extract fair price
    fair_price = snapshot.p_yes_fair if snapshot else None
    btc_price = snapshot.last_trade_price if snapshot else None
    
    # Extract current open orders
    open_bid_price = None
    open_bid_size = 0.0
    open_ask_price = None
    open_ask_size = 0.0
    
    for order in state_view.open_orders.values():
        if order.side == Side.BUY:
            open_bid_price = order.price
            open_bid_size = order.size
        else:
            open_ask_price = order.price
            open_ask_size = order.size
    
    # Extract token IDs
    yes_token_id = state_view.token_ids.yes_token_id if state_view.token_ids else ""
    no_token_id = state_view.token_ids.no_token_id if state_view.token_ids else ""
    
    return StrategyContext(
        now_ms=now_ms,
        t_remaining_ms=t_remaining_ms,
        position=position,
        market_bid=market_bid,
        market_ask=market_ask,
        tick_size=tick_size,
        fair_price=fair_price,
        btc_price=btc_price,
        open_bid_price=open_bid_price,
        open_bid_size=open_bid_size,
        open_ask_price=open_ask_price,
        open_ask_size=open_ask_size,
        yes_token_id=yes_token_id,
        no_token_id=no_token_id,
    )


def build_open_orders_dict(state_view: CanonicalStateView) -> dict[str, OpenOrder]:
    """Bridge function: Convert state open_orders to SimpleExecutor format."""
    result = {}
    for oid, order in state_view.open_orders.items():
        result[oid] = OpenOrder(
            order_id=oid,
            side=order.side,
            price=order.price,
            size=order.size,
        )
    return result


# =============================================================================
# TEST APP WRAPPERS WITH DISPLAY
# =============================================================================

class TestContainerAApp(ContainerAApp):
    """Container A with dummy pricer."""
    
    def _setup_components(self) -> None:
        super()._setup_components()
        self.pricer = DummyPricer()
        from binance_pricer.snapshot_publisher import SnapshotPublisher
        self.publisher = SnapshotPublisher(
            publish_hz=self.config.snapshot_publish_hz,
            store=self.store,
            ctx_builder=self.hour_ctx,
            feature_engine=self.features,
            pricer=self.pricer,
            health=self.health,
        )


class TestContainerBApp(ContainerBApp):
    """Container B with walking strategy and display hooks."""
    
    def __init__(self, config: BConfig, display_callback=None):
        super().__init__(config)
        self._display_callback = display_callback
        self._strategy_instance: WalkingStrategy = None
        self._simple_executor: SimpleExecutor = None
    
    def _setup_components(self) -> None:
        super()._setup_components()
        # Create and store the strategy
        self._strategy_instance = WalkingStrategy()
        
        # Create simple executor (after rest client is set up)
        # We'll set it up after super()._setup_components() completes
        # But we need to wait for rest client, so do it in a custom method
        
        # Create custom decision loop with display
        self.decision_loop = DisplayDecisionLoop(
            tick_hz=self.config.poll_snapshot_hz,
            state_provider=lambda: self.state.view(),
            snapshot_provider=lambda: self.poller.latest,
            strategy=self._strategy_instance,
            rest_client=self.rest,
            display_callback=self._display_callback,
            strategy_ref=self._strategy_instance,
        )
    
    async def start(self):
        """Start the app and initialize simple executor."""
        # Call parent start (which does all the setup)
        await super().start()
        
        # Wait a bit for market to be selected and state to stabilize
        await asyncio.sleep(1)
        
        # Now create simple executor with token IDs
        state_view = self.state.view()
        if state_view.token_ids:
            self._simple_executor = SimpleExecutor(
                rest=self.rest,
                yes_token_id=state_view.token_ids.yes_token_id,
                no_token_id=state_view.token_ids.no_token_id or "",
                default_ttl_ms=self.config.default_order_ttl_ms,
            )
            self.decision_loop.set_executor(self._simple_executor)


class DisplayDecisionLoop:
    """Decision loop with human-readable display each tick - SIMPLIFIED VERSION."""
    
    def __init__(
        self,
        tick_hz: int,
        state_provider,
        snapshot_provider,
        strategy: SimpleStrategy,
        rest_client,
        display_callback=None,
        strategy_ref=None,
    ):
        self._state_provider = state_provider
        self._snapshot_provider = snapshot_provider
        self._strategy = strategy
        self._rest_client = rest_client
        self._simple_executor: Optional[SimpleExecutor] = None
        self._display_callback = display_callback
        self._strategy_ref = strategy_ref
        self._tick_hz = tick_hz
        self._tick_count = 0
        self._running = False
        
        # Track previous tick's orders for diff
        self._prev_orders: dict = {}
    
    def set_executor(self, executor: SimpleExecutor):
        """Set the simple executor (called after token IDs are known)."""
        self._simple_executor = executor
    
    async def run(self, shutdown_event=None):
        """Run with display output each tick - SIMPLIFIED FLOW."""
        self._running = True
        interval_seconds = 1.0 / self._tick_hz
        
        # Import here to avoid circular imports
        try:
            from shared.hourmm_common.time import ms_until_hour_end
        except ImportError:
            def ms_until_hour_end(now_ms):
                return 3600 * 1000  # Default 1 hour
        
        while self._running:
            if shutdown_event and shutdown_event.is_set():
                break
            
            loop_start = time_ns()
            now_ms = loop_start // 1_000_000
            self._tick_count += 1
            
            try:
                # Get FRESH state
                state_view = self._state_provider()
                snapshot = self._snapshot_provider()
                
                # Capture orders BEFORE this tick (from previous iteration)
                orders_before = dict(self._prev_orders)
                
                # Build simple strategy context
                t_remaining_ms = ms_until_hour_end(now_ms)
                strategy_ctx = build_strategy_context(
                    state_view, snapshot, now_ms, t_remaining_ms
                )
                
                # Run strategy (SIMPLE!)
                targets = self._strategy.compute_quotes(strategy_ctx)
                
                # Get current orders in simple format
                current_orders = build_open_orders_dict(state_view)
                
                # Sync to targets (fire and forget)
                actions_summary = {}
                rest_latency_ms = 0
                
                if self._simple_executor:
                    t0 = time_ns()
                    actions_summary = await self._simple_executor.sync_to_targets(
                        targets, current_orders, now_ms
                    )
                    rest_latency_ms = (time_ns() - t0) // 1_000_000
                
                # Get FRESH state AFTER actions submitted
                # Wait a tiny bit for WS updates
                await asyncio.sleep(0.05)
                state_after = self._state_provider()
                
                # Display the tick
                if self._display_callback:
                    self._display_callback(
                        tick=self._tick_count,
                        state_before=orders_before,
                        state_after=state_after,
                        snapshot=snapshot,
                        strategy=self._strategy_ref,
                        targets=targets,
                        actions_summary=actions_summary,
                        rest_latency_ms=rest_latency_ms,
                    )
                
                # Save current orders for next tick's diff
                self._prev_orders = dict(state_after.open_orders)
            
            except Exception as e:
                import traceback
                print(f"[ERROR] Decision loop: {e}")
                traceback.print_exc()
            
            # Sleep to maintain rate
            elapsed_seconds = (time_ns() - loop_start) / 1_000_000_000
            sleep_seconds = max(0, interval_seconds - elapsed_seconds)
            if sleep_seconds > 0:
                await asyncio.sleep(sleep_seconds)
    
    def stop(self):
        self._running = False


# =============================================================================
# DISPLAY FUNCTIONS
# =============================================================================

def get_side_str(order) -> str:
    """Get side as string, handling enum comparison issues."""
    if hasattr(order.side, 'name'):
        return order.side.name
    return str(order.side).split('.')[-1]


def format_order(order) -> str:
    """Format an order for display."""
    side = get_side_str(order)
    return f"{side} {order.size:.0f} @ ${order.price:.2f}"


def format_orders_table(orders: dict) -> str:
    """Format open orders as a table."""
    if not orders:
        return "  (none)"
    
    lines = []
    # Sort by price for readability
    sorted_orders = sorted(orders.items(), key=lambda x: x[1].price)
    for i, (oid, order) in enumerate(sorted_orders, 1):
        short_id = oid[:10] + "..."
        side = get_side_str(order)
        status = order.status.name if hasattr(order.status, 'name') else str(order.status)
        lines.append(f"  {side:4} {order.size:4.0f} @ ${order.price:.2f}  {short_id}")
    
    return "\n".join(lines)


def format_action(action) -> str:
    """Format an action for display."""
    action_type = action.action_type.name
    if action.order:
        side = get_side_str(action.order)
        return f"{action_type}: {side} {action.order.size:.0f} @ ${action.order.price:.2f}"
    elif action.order_id:
        short_id = action.order_id[:10] + "..."
        return f"{action_type}: {short_id}"
    return action_type


def display_tick(
    tick: int,
    state_before: dict,
    state_after,
    snapshot,
    strategy,
    targets: TargetQuotes,
    actions_summary: dict,
    rest_latency_ms: int = 0,
):
    """Display a single tick in human-readable format - SIMPLIFIED."""
    now = datetime.now()
    
    lines = []
    
    # Header with timing
    lines.append("")
    lines.append(f"{'━' * 70}")
    header = f" TICK #{tick:04d}  │  {now.strftime('%H:%M:%S.%f')[:-3]}"
    if rest_latency_ms > 0:
        header += f"  │  REST: {rest_latency_ms}ms"
    lines.append(header)
    lines.append(f"{'━' * 70}")
    
    # Strategy wants (from targets)
    if targets:
        if targets.bid_price:
            lines.append(f" WANT: BID {targets.bid_size:.0f} @ ${targets.bid_price:.2f}")
        if targets.ask_price:
            lines.append(f" WANT: ASK {targets.ask_size:.0f} @ ${targets.ask_price:.2f}")
        if not targets.bid_price and not targets.ask_price:
            lines.append(f" WANT: (nothing)")
    else:
        lines.append(f" WANT: (nothing)")
    
    # BTC price (compact)
    if snapshot:
        lines.append(f" BTC:  ${snapshot.last_trade_price or 0:,.2f}")
    
    # Actions taken THIS tick
    lines.append("")
    if actions_summary:
        places = actions_summary.get("places", [])
        cancels = actions_summary.get("cancels", [])
        kept = actions_summary.get("kept", [])
        
        if places or cancels:
            lines.append(f" ACTIONS: {len(places)} place, {len(cancels)} cancel  (took {rest_latency_ms}ms)")
            for action in places:
                lines.append(f"  → PLACE: {action}")
            for action in cancels:
                lines.append(f"  → CANCEL: {action}")
        elif kept:
            lines.append(f" ACTIONS: (kept {len(kept)} orders)")
        else:
            lines.append(" ACTIONS: (none)")
    else:
        lines.append(" ACTIONS: (none)")
    
    # Current open orders (AFTER actions)
    current_orders = state_after.open_orders if state_after else {}
    lines.append("")
    lines.append(f" OPEN ORDERS ({len(current_orders)}):")
    lines.append(format_orders_table(current_orders))
    
    # Diff from previous tick
    added = set(current_orders.keys()) - set(state_before.keys())
    removed = set(state_before.keys()) - set(current_orders.keys())
    
    if added or removed:
        lines.append("")
        lines.append(" WS CONFIRMED:")
        for oid in added:
            order = current_orders[oid]
            lines.append(f"  ✓ ARRIVED: {format_order(order)}")
        for oid in removed:
            if oid in state_before:
                order = state_before[oid]
                lines.append(f"  ✗ GONE: {format_order(order)}")
    
    print("\n".join(lines))


# =============================================================================
# MAIN TEST RUNNER
# =============================================================================

class ProductionE2ETest:
    """Production-style e2e test with clean display."""
    
    def __init__(self):
        self.container_a: ContainerAApp = None
        self.container_b: ContainerBApp = None
        self.start_time = None
    
    def create_container_a_config(self) -> AConfig:
        return AConfig(
            symbol="BTCUSDT",
            snapshot_publish_hz=2,
            http_host="127.0.0.1",
            http_port=8080,
            stale_threshold_ms=5000,
            log_level="WARNING",  # Quiet
        )
    
    def create_container_b_config(self) -> BConfig:
        pm_private_key = os.getenv("PM_PRIVATE_KEY", "")
        pm_funder = os.getenv("PM_FUNDER", "")
        pm_signature_type = int(os.getenv("PM_SIGNATURE_TYPE", "1"))
        market_slug = os.getenv("MARKET_SLUG", "bitcoin-up-or-down")
        
        if not pm_private_key or not pm_funder:
            raise ValueError(
                "Missing PM_PRIVATE_KEY or PM_FUNDER in environment.\n"
                "Set them in deploy/prod.env or export them."
            )
        
        return BConfig(
            poll_snapshot_hz=2,  # 2 Hz = 500ms per tick
            snapshot_url="http://127.0.0.1:8080/snapshot/latest",
            market_slug=market_slug,
            pm_private_key=pm_private_key,
            pm_funder=pm_funder,
            pm_signature_type=pm_signature_type,
            max_reserved_capital=50.0,
            max_position_size=50.0,
            max_order_size=10.0,
            max_open_orders=4,
            default_order_ttl_ms=64_000,
            replace_min_age_ms=0,  # Allow immediate replace
            replace_min_ticks=1,   # Replace if price moves 1 tick
            sqlite_path="/tmp/hourmm_test_journal.db",
            control_host="127.0.0.1",
            control_port=9000,
            log_level="WARNING",  # Quiet
        )
    
    async def run_container_a(self):
        try:
            await self.container_a.start()
            await self.container_a._shutdown_event.wait()
        except Exception as e:
            print(f"[ERROR] Container A: {e}")
            raise
    
    async def run_container_b(self):
        try:
            await asyncio.sleep(3)  # Wait for Container A
            await self.container_b.start()
            await self.container_b._shutdown_event.wait()
        except Exception as e:
            print(f"[ERROR] Container B: {e}")
            raise
    
    async def run(self):
        try:
            # Print header
            print("\n" + "=" * 60)
            print(" HOURMM E2E TEST - Walking Strategy Demo")
            print("=" * 60)
            print()
            print(" Strategy: Walks bid price 1c -> 2c -> 3c -> 2c -> 1c...")
            print(" Frequency: 2 Hz (500ms per tick)")
            print()
            print(" Press Ctrl+C to stop")
            print()
            print("=" * 60)
            
            self.start_time = datetime.now(timezone.utc)
            
            # Create configs
            config_a = self.create_container_a_config()
            config_b = self.create_container_b_config()
            
            print(f" Market: {config_b.market_slug}")
            print(f" Max order size: {config_b.max_order_size}")
            print("=" * 60)
            print()
            print(" Starting containers...")
            print()
            
            # Create apps
            self.container_a = TestContainerAApp(config_a)
            self.container_b = TestContainerBApp(
                config_b,
                display_callback=display_tick,
            )
            
            # Run
            await asyncio.gather(
                self.run_container_a(),
                self.run_container_b(),
            )
        
        except KeyboardInterrupt:
            print("\n")
            print("=" * 60)
            print(" Shutting down...")
            print("=" * 60)
            
            if self.container_a:
                self.container_a._shutdown_event.set()
            if self.container_b:
                self.container_b._shutdown_event.set()
            
            await asyncio.sleep(1)
            print(" Done.")
        
        except Exception as e:
            print(f"\n[FATAL] {e}")
            raise


async def main():
    test = ProductionE2ETest()
    await test.run()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nStopped by user")
        sys.exit(0)
    except Exception as e:
        print(f"\nTest failed: {e}")
        sys.exit(1)
