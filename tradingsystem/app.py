"""
Market-Making Application.

Wires all components together and manages application lifecycle.
Uses threading for all components (no asyncio in main loop).

Component threading layout:
- Thread 1: PM Market WS → PolymarketCache
- Thread 2: PM User WS → Executor event queue
- Thread 3: Binance Poller → BinanceCache
- Thread 4: Strategy Runner → Executor event queue (direct)
- Thread 5: Executor Actor → Gateway
- Thread 6: Gateway Worker

Note: The executor uses a unified event queue - all inputs (intents, fills, acks)
flow through a single queue for deterministic ordering.
"""

import asyncio
import logging
import queue
import signal
import threading
import time
from typing import Optional

from .config import AppConfig
from .types import (
    MarketInfo,
    now_ms,
)
from .caches import PolymarketCache, BinanceCache
from .feeds import PolymarketMarketFeed, PolymarketUserFeed, BinanceFeed
from .pm_rest_client import PolymarketRestClient
from .gateway import Gateway
from .strategy import (
    Strategy,
    DefaultMMStrategy,
    StrategyConfig,
    StrategyInput,
    StrategyRunner,
)
from .executor import ExecutorActor, ExecutorPolicies, ExecutorMode
from .gamma_client import GammaClient
from .market_finder import BitcoinHourlyMarketFinder

logger = logging.getLogger(__name__)


class MMApplication:
    """
    Main market-making application.

    Wires together all components and manages lifecycle:
    - Startup: discover market, initialize components, start threads
    - Running: monitor health, handle market transitions
    - Shutdown: graceful stop of all components
    """

    def __init__(
        self,
        config: AppConfig,
        strategy: Optional[Strategy] = None,
    ):
        """
        Initialize application.

        Args:
            config: Application configuration
            strategy: Optional custom strategy (uses DefaultMMStrategy if None)
        """
        self._config = config
        self._custom_strategy = strategy

        # Components (initialized in _setup_components)
        self._pm_cache: Optional[PolymarketCache] = None
        self._bn_cache: Optional[BinanceCache] = None

        self._pm_market_ws: Optional[PolymarketMarketFeed] = None
        self._pm_user_ws: Optional[PolymarketUserFeed] = None
        self._bn_poller: Optional[BinanceFeed] = None

        self._rest_client: Optional[PolymarketRestClient] = None
        self._gateway: Optional[Gateway] = None

        self._strategy_runner: Optional[StrategyRunner] = None
        self._executor: Optional[ExecutorActor] = None

        self._event_queue: Optional[queue.Queue] = None

        # Market discovery
        self._gamma: Optional[GammaClient] = None
        self._market_finder: Optional[BitcoinHourlyMarketFinder] = None
        self._current_market: Optional[MarketInfo] = None

        # Control
        self._stop_event = threading.Event()
        self._running = False

    def _setup_components(self) -> None:
        """Initialize all components."""
        cfg = self._config

        # Caches
        self._pm_cache = PolymarketCache()
        self._bn_cache = BinanceCache()

        # Event queue for executor (unified: fills, acks, intents from User WS and Strategy)
        self._event_queue = queue.Queue(maxsize=1000)

        # REST client
        self._rest_client = PolymarketRestClient(
            private_key=cfg.pm_private_key,
            funder=cfg.pm_funder,
            signature_type=cfg.pm_signature_type,
        )
        # Initialize REST client (derive API credentials)
        self._rest_client._ensure_initialized()

        # Gateway
        self._gateway = Gateway(
            rest_client=self._rest_client,
            result_queue=self._event_queue,
            min_action_interval_ms=cfg.gateway_rate_limit_ms,
        )

        # Gamma client for market discovery
        self._gamma = GammaClient()
        self._market_finder = BitcoinHourlyMarketFinder(self._gamma)

        # Market WS (feeds PolymarketCache)
        self._pm_market_ws = PolymarketMarketFeed(
            pm_cache=self._pm_cache,
            ws_url=cfg.pm_ws_market_url,
        )

        # User WS (feeds event queue for fills/acks)
        api_key, api_secret, passphrase = self._rest_client.api_credentials
        # Get our wallet address for matching MAKER fills
        maker_address = self._rest_client.maker_address or cfg.pm_funder
        self._pm_user_ws = PolymarketUserFeed(
            event_queue=self._event_queue,
            api_key=api_key,
            api_secret=api_secret,
            passphrase=passphrase,
            maker_address=maker_address,
            on_reconnect=self._on_user_ws_reconnect,
            ws_url=cfg.pm_ws_user_url,
        )

        # Binance poller (only if URL provided)
        if cfg.binance_snapshot_url:
            self._bn_poller = BinanceFeed(
                cache=self._bn_cache,
                url=cfg.binance_snapshot_url,
                poll_hz=cfg.binance_poll_hz,
            )

        # Strategy config
        strategy_config = StrategyConfig(
            base_spread_cents=cfg.base_spread_cents,
            base_size=cfg.base_size,
            min_size=cfg.min_size,
            max_size=cfg.max_size,
            skew_per_share_cents=cfg.skew_per_share_cents,
            max_skew_cents=cfg.max_skew_cents,
            max_position=cfg.max_position,
        )

        # Strategy
        strategy = self._custom_strategy or DefaultMMStrategy(strategy_config)

        # Strategy runner - push intents directly to event queue
        self._strategy_runner = StrategyRunner(
            strategy=strategy,
            get_input=self._get_strategy_input,
            event_queue=self._event_queue,  # Direct to executor event queue
            tick_hz=cfg.strategy_hz,
        )

        logger.info("All components initialized")

    def _setup_executor(self, market: MarketInfo) -> None:
        """Setup executor with market info."""
        cfg = self._config

        # Create executor policies from config
        # Note: top_up_threshold controls queue-preserving behavior:
        # - Only replace orders on size increase if the increase >= threshold
        # - This prevents losing queue position on small size changes
        executor_policies = ExecutorPolicies(
            min_order_size=5,  # Polymarket minimum
            cancel_timeout_ms=cfg.cancel_timeout_ms,
            place_timeout_ms=cfg.place_timeout_ms,
            cooldown_after_cancel_all_ms=cfg.cooldown_after_cancel_all_ms,
            price_tolerance=0,
            top_up_threshold=10,  # Only replace on size increase >= 10
        )

        self._executor = ExecutorActor(
            gateway=self._gateway,
            event_queue=self._event_queue,
            yes_token_id=market.yes_token_id,
            no_token_id=market.no_token_id,
            market_id=market.condition_id,
            rest_client=self._rest_client,  # For resync operations
            policies=executor_policies,
            on_cancel_all=self._on_cancel_all,
        )

    def _sync_existing_orders(self, market: MarketInfo) -> None:
        """
        Sync existing open orders from exchange.

        Called at startup to prevent duplicate orders after restarts/crashes.
        """
        logger.info("Checking for existing open orders...")

        try:
            # Fetch open orders for this market
            orders = self._rest_client.get_open_orders(market_id=market.condition_id)

            if not orders:
                logger.info("No existing open orders found")
                return

            # Sync into executor state (uses token IDs from executor init)
            synced = self._executor.sync_open_orders(orders=orders)

            if synced > 0:
                logger.info(f"Synced {synced} existing open orders")
            else:
                logger.info("No orders synced (may have been filtered)")

        except Exception as e:
            logger.warning(f"Failed to sync open orders: {e}")
            # Continue anyway - better to risk duplicates than fail to start

    def _get_strategy_input(self) -> StrategyInput:
        """Build StrategyInput from current state."""
        # Get PM book
        pm_book, pm_seq = self._pm_cache.get_latest()

        # Get BN snapshot
        bn_snap, bn_seq = self._bn_cache.get_latest()

        # Get inventory from executor
        if self._executor:
            inventory = self._executor.inventory
        else:
            from .types import InventoryState
            inventory = InventoryState()

        # Get fair price from BN cache
        fair_px_cents = None
        if bn_snap and bn_snap.p_yes is not None:
            fair_px_cents = bn_snap.p_yes_cents
        elif pm_book:
            # Fallback to PM mid if no BN data
            fair_px_cents = pm_book.yes_mid

        # Get time remaining
        t_remaining_ms = 0
        if self._current_market:
            t_remaining_ms = self._current_market.time_remaining_ms
        elif bn_snap:
            t_remaining_ms = bn_snap.t_remaining_ms

        return StrategyInput(
            pm_book=pm_book,
            bn_snap=bn_snap,
            inventory=inventory,
            fair_px_cents=fair_px_cents,
            t_remaining_ms=t_remaining_ms,
            pm_seq=pm_seq,
            bn_seq=bn_seq,
        )

    def _on_user_ws_reconnect(self) -> None:
        """Handle user WS reconnection - trigger cancel-all."""
        logger.warning("User WS reconnected - canceling all orders")
        if self._executor and self._current_market:
            self._gateway.submit_cancel_all(self._current_market.condition_id)

    def _on_cancel_all(self) -> None:
        """Callback when executor triggers cancel-all."""
        logger.warning("Executor triggered cancel-all")

    async def _discover_market_async(self) -> Optional[MarketInfo]:
        """Discover current market asynchronously."""
        market = await self._market_finder.find_current_market()
        return market

    def _discover_market(self) -> Optional[MarketInfo]:
        """Discover current market (sync wrapper)."""
        return asyncio.run(self._discover_market_async())

    def start(self) -> None:
        """Start the application."""
        logger.info("Starting MM Application...")

        # Validate config
        errors = self._config.validate()
        if errors:
            for error in errors:
                logger.error(f"Config error: {error}")
            raise ValueError(f"Invalid configuration: {errors}")

        # Setup components
        self._setup_components()

        # Discover market
        logger.info("Discovering current market...")
        market = self._discover_market()
        if not market:
            raise RuntimeError("No Bitcoin hourly market found for current hour")

        self._current_market = market
        logger.info(f"Selected market: {market.question}")
        logger.info(f"  Condition ID: {market.condition_id}")
        logger.info(f"  YES token: {market.yes_token_id[:30]}...")
        logger.info(f"  NO token: {market.no_token_id[:30]}...")
        if market.reference_price:
            logger.info(f"  Reference price: ${market.reference_price:,.2f}")
        logger.info(f"  Time remaining: {market.time_remaining_ms / 1000:.0f}s")

        # Configure WS clients with token IDs
        self._pm_market_ws.set_tokens(
            yes_token_id=market.yes_token_id,
            no_token_id=market.no_token_id,
            market_id=market.condition_id,
        )
        self._pm_user_ws.set_markets([market.condition_id])
        self._pm_user_ws.set_tokens(
            yes_token_id=market.yes_token_id,
            no_token_id=market.no_token_id,
        )

        # Setup executor with market info
        self._setup_executor(market)

        # Sync existing open orders (prevents duplicates after restart)
        self._sync_existing_orders(market)

        # Start all components
        self._stop_event.clear()

        # Start data feeds
        self._pm_market_ws.start()
        self._pm_user_ws.start()
        if self._bn_poller:
            self._bn_poller.start()

        # Wait for initial data
        logger.info("Waiting for initial market data...")
        time.sleep(2)

        # Start trading components
        self._gateway.start()
        self._executor.start()
        self._strategy_runner.start()

        self._running = True
        logger.info("MM Application started successfully")

    def emergency_close(self) -> None:
        """
        Trigger emergency close: cancel all orders and close all positions.

        This initiates a graceful but aggressive shutdown sequence:
        1. Cancel all open orders
        2. Place marketable SELLs to close any inventory
        3. Transition executor to STOPPED state

        Safe to call multiple times (idempotent after first call).
        """
        if self._executor:
            self._executor.emergency_close()

    def stop(self) -> None:
        """Stop the application."""
        logger.info("Stopping MM Application...")
        self._stop_event.set()

        # Skip cancel-all if executor already handled it (CLOSING/STOPPED)
        should_cancel_all = True
        if self._executor:
            mode = self._executor.mode
            if mode in (ExecutorMode.CLOSING, ExecutorMode.STOPPED):
                logger.info(f"Executor in {mode.value} mode, skipping cancel-all")
                should_cancel_all = False

        # Cancel all orders before stopping (unless already in emergency close)
        if should_cancel_all and self._gateway and self._current_market:
            logger.info("Cancelling all orders...")
            try:
                self._gateway.submit_cancel_all(self._current_market.condition_id)
                # Give it a moment to process
                time.sleep(0.5)
            except Exception as e:
                logger.warning(f"Cancel-all failed: {e}")

        # Stop in reverse order of startup
        if self._strategy_runner:
            self._strategy_runner.stop()

        if self._executor:
            self._executor.stop()

        if self._gateway:
            self._gateway.stop()

        if self._bn_poller:
            self._bn_poller.stop()

        if self._pm_user_ws:
            self._pm_user_ws.stop()

        if self._pm_market_ws:
            self._pm_market_ws.stop()

        self._running = False
        logger.info("MM Application stopped")

    def run(self) -> None:
        """
        Run until shutdown signal.

        Signal handling:
        - First SIGINT/SIGTERM: Emergency close (cancel all + close positions)
        - Second signal or STOPPED: Full shutdown
        """
        # Track emergency close state
        emergency_close_triggered = False

        def signal_handler(signum, frame):
            nonlocal emergency_close_triggered

            if not emergency_close_triggered:
                # First signal: trigger emergency close
                logger.warning(f"Received signal {signum} - initiating emergency close")
                logger.warning("Press Ctrl+C again to force immediate shutdown")
                emergency_close_triggered = True

                if self._executor:
                    self._executor.emergency_close()
            else:
                # Second signal: force stop
                logger.warning(f"Received signal {signum} again - forcing shutdown")
                self._stop_event.set()

        signal.signal(signal.SIGINT, signal_handler)
        signal.signal(signal.SIGTERM, signal_handler)

        try:
            self.start()

            # Main loop - monitor health and handle transitions
            while not self._stop_event.is_set():
                self._stop_event.wait(1.0)

                # Check if executor reached STOPPED state
                if self._executor and self._executor.mode == ExecutorMode.STOPPED:
                    logger.info("Executor reached STOPPED state, shutting down")
                    break

                # Check component health (unless in emergency close)
                if not emergency_close_triggered and not self._is_healthy():
                    logger.error("Component health check failed")
                    break

        except KeyboardInterrupt:
            logger.info("Keyboard interrupt received")
        except Exception as e:
            logger.exception(f"Application error: {e}")
        finally:
            self.stop()

    def _is_healthy(self) -> bool:
        """Check if all components are running."""
        if not self._pm_market_ws or not self._pm_market_ws._thread or not self._pm_market_ws._thread.is_alive():
            logger.warning("PM Market WS not running")
            return False

        if not self._pm_user_ws or not self._pm_user_ws._thread or not self._pm_user_ws._thread.is_alive():
            logger.warning("PM User WS not running")
            return False

        if not self._gateway or not self._gateway.is_running:
            logger.warning("Gateway not running")
            return False

        if not self._executor or not self._executor.is_running:
            logger.warning("Executor not running")
            return False

        if not self._strategy_runner or not self._strategy_runner.is_running:
            logger.warning("Strategy runner not running")
            return False

        return True

    @property
    def is_running(self) -> bool:
        """Check if application is running."""
        return self._running

    @property
    def current_market(self) -> Optional[MarketInfo]:
        """Get current market."""
        return self._current_market

    def get_stats(self) -> dict:
        """Get application statistics."""
        stats = {
            "running": self._running,
            "market": self._current_market.slug if self._current_market else None,
        }

        if self._pm_cache:
            stats["pm_cache_seq"] = self._pm_cache.seq
            stats["pm_cache_stale"] = self._pm_cache.is_stale()

        if self._bn_cache:
            stats["bn_cache_seq"] = self._bn_cache.seq
            stats["bn_cache_stale"] = self._bn_cache.is_stale()

        if self._bn_poller:
            stats["bn_poller"] = self._bn_poller.stats

        if self._strategy_runner:
            stats["strategy"] = self._strategy_runner.stats

        if self._gateway:
            gw_stats = self._gateway.stats
            if gw_stats:
                stats["gateway"] = {
                    "actions_processed": gw_stats.actions_processed,
                    "actions_succeeded": gw_stats.actions_succeeded,
                    "actions_failed": gw_stats.actions_failed,
                }

        if self._executor:
            inv = self._executor.inventory
            stats["inventory"] = {
                "yes": inv.I_yes,
                "no": inv.I_no,
                "net_E": inv.net_E,
                "gross_G": inv.gross_G,
            }

            # Get executor mode
            stats["executor"] = {
                "mode": self._executor.mode.name if hasattr(self._executor, "mode") else "UNKNOWN",
            }

            # Get PnL stats with current mid price for unrealized calculation
            mid_price = 50  # Default
            if self._pm_cache and self._pm_cache.has_data:
                book, _ = self._pm_cache.get_latest()
                if book and book.yes_top.best_bid_px and book.yes_top.best_ask_px:
                    mid_price = (book.yes_top.best_bid_px + book.yes_top.best_ask_px) // 2
            stats["pnl"] = self._executor.get_pnl_stats(mid_price)

        return stats


def main() -> None:
    """Entry point."""
    import sys

    # Setup logging
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )

    # Load config
    config = AppConfig.from_env()

    # Override log level if configured
    if config.log_level:
        logging.getLogger().setLevel(config.log_level)

    # Create and run application
    app = MMApplication(config)
    app.run()


if __name__ == "__main__":
    main()
