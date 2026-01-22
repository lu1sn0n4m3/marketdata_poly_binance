"""Main application for Container A (Binance Pricer)."""

import asyncio
import logging
import signal
from typing import Optional

from .config import AConfig
from .types import BinanceEvent
from .binance_ws import BinanceWsClient
from .hour_context import HourContextBuilder
from .feature_engine import DefaultFeatureEngine
from .pricer import BaselinePricer
from .health import HealthTracker
from .snapshot_store import LatestSnapshotStore
from .snapshot_publisher import SnapshotPublisher
from .snapshot_server import SnapshotServer

logger = logging.getLogger(__name__)


class ContainerAApp:
    """
    Main application that wires everything together.
    
    Component graph:
    BinanceWsClient --> HourContextBuilder --> FeatureEngine --> Pricer
          |                    |                   |             |
          └────────────────► HealthTracker ◄──────┴─────────────┘
                                          |
                                  LatestSnapshotStore --> SnapshotServer
    """
    
    def __init__(self, config: AConfig):
        """
        Initialize the application.
        
        Args:
            config: Application configuration
        """
        self.config = config
        
        # Validate config
        config.validate()
        
        # Components
        self.binance: Optional[BinanceWsClient] = None
        self.hour_ctx: Optional[HourContextBuilder] = None
        self.features: Optional[DefaultFeatureEngine] = None
        self.pricer: Optional[BaselinePricer] = None
        self.health: Optional[HealthTracker] = None
        self.store: Optional[LatestSnapshotStore] = None
        self.publisher: Optional[SnapshotPublisher] = None
        self.server: Optional[SnapshotServer] = None
        
        # Control
        self._shutdown_event = asyncio.Event()
        self._tasks: list[asyncio.Task] = []
    
    def _setup_components(self) -> None:
        """Initialize all components."""
        # Health tracker
        self.health = HealthTracker(
            stale_threshold_ms=self.config.stale_threshold_ms
        )
        
        # Hour context builder
        self.hour_ctx = HourContextBuilder()
        
        # Feature engine
        self.features = DefaultFeatureEngine(
            vol_windows_ms={
                "vol_1m": self.config.feature_windows.get("vol_1m", 60000),
                "vol_5m": self.config.feature_windows.get("vol_5m", 300000),
            },
            ewma_alpha=self.config.feature_windows.get("ewma_alpha", 0.1),
        )
        
        # Pricer
        self.pricer = BaselinePricer(
            base_vol=self.config.pricer_params.get("base_vol", 0.02),
            vol_floor=self.config.pricer_params.get("vol_floor", 0.005),
            vol_cap=self.config.pricer_params.get("vol_cap", 0.10),
        )
        
        # Snapshot store
        self.store = LatestSnapshotStore()
        
        # Snapshot publisher
        self.publisher = SnapshotPublisher(
            publish_hz=self.config.snapshot_publish_hz,
            store=self.store,
            ctx_builder=self.hour_ctx,
            feature_engine=self.features,
            pricer=self.pricer,
            health=self.health,
        )
        
        # HTTP server
        self.server = SnapshotServer(
            store=self.store,
            host=self.config.http_host,
            port=self.config.http_port,
        )
        
        # Binance WS client (created last as it needs event handler)
        self.binance = BinanceWsClient(
            symbol=self.config.symbol,
            base_url=self.config.binance_ws_url,
            on_event=self._on_binance_event,
        )
    
    async def _on_binance_event(self, event: BinanceEvent) -> None:
        """
        Handle a Binance event.
        
        Updates all components that need to process the event.
        
        Args:
            event: BinanceEvent
        """
        # Update health
        self.health.update_on_event(
            ts_local_ms=event.ts_local_ms,
            ts_exchange_ms=event.ts_exchange_ms,
        )
        
        # Update hour context
        self.hour_ctx.update_from_event(event)
        
        # Get current context for feature update
        ctx = self.hour_ctx.get_context(event.ts_local_ms)
        
        # Update features
        self.features.update(event, ctx)
    
    async def start(self) -> None:
        """Start the application."""
        logger.info("Starting Container A (Binance Pricer)...")
        
        # Setup components
        self._setup_components()
        
        # Start HTTP server
        await self.server.start()
        
        # Start background tasks
        self._tasks = [
            asyncio.create_task(
                self.binance.run(self._shutdown_event),
                name="binance_ws"
            ),
            asyncio.create_task(
                self.publisher.run(self._shutdown_event),
                name="publisher"
            ),
        ]
        
        logger.info("Container A started")
    
    async def stop(self) -> None:
        """Stop the application."""
        logger.info("Stopping Container A...")
        
        # Signal shutdown
        self._shutdown_event.set()
        
        # Stop components
        if self.binance:
            self.binance.stop()
        if self.publisher:
            self.publisher.stop()
        
        # Cancel tasks
        for task in self._tasks:
            task.cancel()
        
        # Wait for tasks to complete
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        
        # Stop server
        if self.server:
            await self.server.stop()
        
        logger.info("Container A stopped")
    
    async def run(self) -> None:
        """
        Run the application until shutdown signal.
        
        Handles SIGINT and SIGTERM for graceful shutdown.
        """
        # Setup signal handlers
        loop = asyncio.get_event_loop()
        
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(
                sig,
                lambda: asyncio.create_task(self._handle_signal())
            )
        
        try:
            await self.start()
            
            # Wait for shutdown
            await self._shutdown_event.wait()
        finally:
            await self.stop()
    
    async def _handle_signal(self) -> None:
        """Handle shutdown signal."""
        logger.info("Received shutdown signal")
        self._shutdown_event.set()


def main() -> None:
    """Entry point for the application."""
    try:
        from shared.hourmm_common.util import setup_logging
    except ImportError:
        import sys
        sys.path.insert(0, "/Users/luissafar/dev/marketdata_poly_binance")
        from shared.hourmm_common.util import setup_logging
    
    # Load config
    config = AConfig.from_env()
    
    # Setup logging
    setup_logging("binance_pricer", level=config.log_level)
    
    logger.info(f"Starting with config: symbol={config.symbol}, port={config.http_port}")
    
    # Create and run app
    app = ContainerAApp(config)
    asyncio.run(app.run())


if __name__ == "__main__":
    main()
