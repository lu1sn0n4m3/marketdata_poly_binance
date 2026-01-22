"""Main application for Container B (Polymarket Trader)."""

import asyncio
import logging
import signal
from time import time_ns
from typing import Optional

from .config import BConfig
from .types import (
    CanonicalState, CanonicalStateView, Event, RiskMode, SessionState,
    LimitState, BinanceSnapshot,
)
from .gamma_client import GammaClient
from .market_scheduler import MarketScheduler
from .polymarket_ws_market import PolymarketMarketWsClient
from .polymarket_ws_user import PolymarketUserWsClient
from .polymarket_rest import PolymarketRestClient
from .journal_sqlite import EventJournalSQLite
from .reducer import StateReducer
from .reconciler import Reconciler
from .risk import RiskEngine
from .strategy import OpportunisticQuoteStrategy
from .order_manager import OrderManager
from .executor import Executor
from .snapshot_poller import SnapshotPoller
from .decision_loop import DecisionLoop
from .control_plane import ControlPlaneServer

logger = logging.getLogger(__name__)


class ContainerBApp:
    """
    Main application that wires everything together.
    
    Component graph:
    GammaClient -> MarketScheduler -> (market selection)
    PolymarketMarketWsClient -+
    PolymarketUserWsClient ---+-> reducer_queue -> StateReducer -> CanonicalState
    Executor acks/errors -----+                          |
    ControlPlane commands ----+--------------------------|
                                                         |
    DecisionLoop (50Hz) -> RiskEngine -> Strategy -> OrderManager -> Executor
    """
    
    def __init__(self, config: BConfig):
        """
        Initialize the application.
        
        Args:
            config: Application configuration
        """
        self.config = config
        config.validate()
        
        # Canonical state
        self.state = CanonicalState()
        
        # Components (initialized in _setup_components)
        self.gamma: Optional[GammaClient] = None
        self.scheduler: Optional[MarketScheduler] = None
        self.market_ws: Optional[PolymarketMarketWsClient] = None
        self.user_ws: Optional[PolymarketUserWsClient] = None
        self.rest: Optional[PolymarketRestClient] = None
        self.journal: Optional[EventJournalSQLite] = None
        self.reducer: Optional[StateReducer] = None
        self.reconciler: Optional[Reconciler] = None
        self.risk: Optional[RiskEngine] = None
        self.strategy: Optional[OpportunisticQuoteStrategy] = None
        self.order_manager: Optional[OrderManager] = None
        self.executor: Optional[Executor] = None
        self.poller: Optional[SnapshotPoller] = None
        self.decision_loop: Optional[DecisionLoop] = None
        self.control_plane: Optional[ControlPlaneServer] = None
        
        # Control
        self._shutdown_event = asyncio.Event()
        self._tasks: list[asyncio.Task] = []
    
    def _setup_components(self) -> None:
        """Initialize all components."""
        # Initialize limits from config
        limits = LimitState(
            max_reserved_capital=self.config.max_reserved_capital,
            max_position_size=self.config.max_position_size,
            max_order_size=self.config.max_order_size,
            max_open_orders=self.config.max_open_orders,
        )
        self.state.limits = limits
        
        # Gamma client
        self.gamma = GammaClient(base_url=self.config.gamma_api_url)
        
        # Market scheduler
        self.scheduler = MarketScheduler(
            gamma=self.gamma,
            market_slug=self.config.market_slug,
        )
        
        # REST client
        self.rest = PolymarketRestClient(
            base_url=self.config.pm_rest_url,
            api_key=self.config.pm_api_key,
            api_secret=self.config.pm_api_secret,
            passphrase=self.config.pm_passphrase,
        )
        
        # Reconciler
        self.reconciler = Reconciler(
            rest=self.rest,
            cooldown_ms=self.config.session_timers.get("stabilization_window_ms", 5000),
        )
        
        # Journal
        self.journal = EventJournalSQLite(
            db_path=self.config.sqlite_path,
            enabled=True,
        )
        
        # Reducer
        self.reducer = StateReducer(
            state=self.state,
            journal=self.journal,
            scheduler=self.scheduler,
            on_reconnect=self._on_ws_reconnect,
        )
        
        # WebSocket clients
        self.market_ws = PolymarketMarketWsClient(
            ws_url=self.config.pm_ws_market_url,
            on_event=self._forward_to_reducer,
        )
        
        self.user_ws = PolymarketUserWsClient(
            ws_url=self.config.pm_ws_user_url,
            api_key=self.config.pm_api_key,
            api_secret=self.config.pm_api_secret,
            passphrase=self.config.pm_passphrase,
            on_event=self._forward_to_reducer,
        )
        
        # Snapshot poller
        self.poller = SnapshotPoller(
            url=self.config.snapshot_url,
            poll_hz=self.config.poll_snapshot_hz,
        )
        
        # Risk engine
        self.risk = RiskEngine(
            limits=limits,
            session_timers=self.config.session_timers,
            stale_threshold_ms=self.config.stale_snapshot_threshold_ms,
        )
        
        # Strategy
        self.strategy = OpportunisticQuoteStrategy()
        
        # Order manager
        self.order_manager = OrderManager(
            max_working_orders=self.config.max_open_orders,
            replace_min_ticks=self.config.replace_min_ticks,
            replace_min_age_ms=self.config.replace_min_age_ms,
        )
        
        # Executor
        self.executor = Executor(
            rest=self.rest,
            on_event=self.reducer.queue,
        )
        
        # Decision loop
        self.decision_loop = DecisionLoop(
            tick_hz=self.config.poll_snapshot_hz,
            state_provider=lambda: self.state.view(),
            snapshot_provider=lambda: self.poller.latest,
            risk=self.risk,
            strategy=self.strategy,
            order_manager=self.order_manager,
            executor=self.executor,
        )
        
        # Control plane
        self.control_plane = ControlPlaneServer(
            bind_host=self.config.control_host,
            port=self.config.control_port,
            out_queue=self.reducer.queue,
        )
        self.control_plane.set_status_callback(self._get_status)
    
    async def _forward_to_reducer(self, event: Event) -> None:
        """Forward event to reducer queue."""
        try:
            self.reducer.queue.put_nowait(event)
        except asyncio.QueueFull:
            logger.warning("Reducer queue full, dropping event")
    
    async def _on_ws_reconnect(self, market_id: str) -> None:
        """Handle WebSocket reconnection."""
        await self.reconciler.on_reconnect(market_id)
    
    def _get_status(self) -> dict:
        """Get current status for control plane."""
        return {
            "session_state": self.state.session_state.name,
            "risk_mode": self.state.risk_mode.name,
            "positions": {
                "yes_tokens": self.state.positions.yes_tokens,
                "no_tokens": self.state.positions.no_tokens,
            },
            "open_orders": len(self.state.open_orders),
            "health": {
                "market_ws": self.state.health.market_ws_connected,
                "user_ws": self.state.health.user_ws_connected,
                "rest": self.state.health.rest_healthy,
                "binance_stale": self.state.health.binance_snapshot_stale,
            },
            "market_id": self.state.market_view.market_id,
        }
    
    async def _select_initial_market(self) -> bool:
        """Select the initial hourly market."""
        now_ms = time_ns() // 1_000_000
        market = await self.scheduler.select_market_for_now(now_ms)
        
        if market:
            self.state.active_market = market
            self.state.token_ids = market.tokens
            self.state.market_view.market_id = market.condition_id
            
            # Configure WS clients with token IDs
            self.market_ws.set_tokens(
                [market.tokens.yes_token_id, market.tokens.no_token_id],
                market.condition_id,
            )
            
            logger.info(f"Selected market: {market.market_slug}")
            return True
        else:
            logger.error("Failed to select initial market")
            return False
    
    async def start(self) -> None:
        """Start the application."""
        logger.info("Starting Container B (Polymarket Trader)...")
        
        # Setup components
        self._setup_components()
        
        # Initialize journal
        self.journal.init_schema()
        
        # Select initial market
        if not await self._select_initial_market():
            logger.error("Cannot start without market - retrying...")
            # Retry logic could go here
        
        # Start control plane
        await self.control_plane.start()
        
        # Start background tasks
        self._tasks = [
            asyncio.create_task(
                self.reducer.run(self._shutdown_event),
                name="reducer"
            ),
            asyncio.create_task(
                self.market_ws.run(self._shutdown_event),
                name="market_ws"
            ),
            asyncio.create_task(
                self.user_ws.run(self._shutdown_event),
                name="user_ws"
            ),
            asyncio.create_task(
                self.poller.run(self._shutdown_event),
                name="poller"
            ),
            asyncio.create_task(
                self.decision_loop.run(self._shutdown_event),
                name="decision_loop"
            ),
        ]
        
        # Transition to ACTIVE after initialization
        self.state.session_state = SessionState.ACTIVE
        self.state.risk_mode = RiskMode.NORMAL
        self.state.session_start_ms = time_ns() // 1_000_000
        
        logger.info("Container B started")
    
    async def stop(self) -> None:
        """Stop the application."""
        logger.info("Stopping Container B...")
        
        # Signal shutdown
        self._shutdown_event.set()
        
        # Stop components
        if self.decision_loop:
            self.decision_loop.stop()
        if self.poller:
            self.poller.stop()
        if self.market_ws:
            self.market_ws.stop()
        if self.user_ws:
            self.user_ws.stop()
        if self.reducer:
            self.reducer.stop()
        
        # Cancel tasks
        for task in self._tasks:
            task.cancel()
        
        # Wait for tasks
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        
        # Stop control plane
        if self.control_plane:
            await self.control_plane.stop()
        
        # Close clients
        if self.gamma:
            await self.gamma.close()
        if self.rest:
            await self.rest.close()
        
        # Close journal
        if self.journal:
            self.journal.close()
        
        logger.info("Container B stopped")
    
    async def run(self) -> None:
        """
        Run the application until shutdown signal.
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
    config = BConfig.from_env()
    
    # Setup logging
    setup_logging("polymarket_trader", level=config.log_level)
    
    logger.info(f"Starting with config: market_slug={config.market_slug}")
    
    # Create and run app
    app = ContainerBApp(config)
    asyncio.run(app.run())


if __name__ == "__main__":
    main()
