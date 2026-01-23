# Task 09: Application Wiring and Main Loop

**Priority:** Critical (Integration)
**Estimated Complexity:** High
**Dependencies:** Tasks 01-08 (All components)

---

## Objective

Wire all components together into a working application with proper startup sequence, shutdown handling, and component coordination.

---

## Context from Design Documents

From `polymarket_mm_efficiency_design.md` Section 9:

> **A good default threading layout (single event):**
> - Thread 1: Polymarket book WS → PMCache
> - Thread 2: Polymarket user WS → Executor high-priority queue
> - Thread 3: Binance WS → BNCache
> - Thread 4: Strategy heartbeat → intent mailbox/queue
> - Thread 5: Executor actor → emits actions
> - Thread 6: Gateway worker (optional but recommended)
>
> On 2 vCPUs, 5–6 threads is fine because most are I/O bound.

From `polymarket_mm_framework.md` Section 1:

> **Component graph:**
> 1. Market Data Feeds → Snapshot Stores
> 2. Strategy Engine reads snapshots → outputs DesiredQuoteSet
> 3. Executor receives intents and user events → Gateway
> 4. Gateway performs order operations

---

## Implementation Checklist

### 1. Application Config

```python
@dataclass
class AppConfig:
    """Application configuration."""
    # Market
    market_slug: str = ""  # If empty, auto-discover

    # API credentials
    pm_api_key: str = ""
    pm_api_secret: str = ""
    pm_passphrase: str = ""
    pm_private_key: str = ""
    pm_funder: str = ""
    pm_signature_type: int = 1

    # URLs
    pm_ws_market_url: str = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
    pm_ws_user_url: str = "wss://ws-subscriptions-clob.polymarket.com/ws/user"
    pm_rest_url: str = "https://clob.polymarket.com"
    gamma_api_url: str = "https://gamma-api.polymarket.com"
    binance_snapshot_url: str = "http://localhost:8080/snapshot/latest"

    # Strategy
    strategy_hz: int = 20
    base_spread_cents: int = 2
    base_size: int = 100

    # Executor
    pm_stale_threshold_ms: int = 500
    bn_stale_threshold_ms: int = 1000
    cancel_timeout_ms: int = 5000
    cooldown_after_cancel_all_ms: int = 3000

    # Limits
    gross_cap: int = 1000
    max_position: int = 500

    # Logging
    log_level: str = "INFO"

    @classmethod
    def from_env(cls) -> "AppConfig":
        """Load config from environment variables."""
        return cls(
            market_slug=os.getenv("PM_MARKET_SLUG", ""),
            pm_api_key=os.getenv("PM_API_KEY", ""),
            pm_api_secret=os.getenv("PM_API_SECRET", ""),
            pm_passphrase=os.getenv("PM_PASSPHRASE", ""),
            pm_private_key=os.getenv("PM_PRIVATE_KEY", ""),
            pm_funder=os.getenv("PM_FUNDER", ""),
            pm_signature_type=int(os.getenv("PM_SIGNATURE_TYPE", "1")),
            binance_snapshot_url=os.getenv("BINANCE_SNAPSHOT_URL", "http://localhost:8080/snapshot/latest"),
            strategy_hz=int(os.getenv("STRATEGY_HZ", "20")),
            gross_cap=int(os.getenv("GROSS_CAP", "1000")),
            max_position=int(os.getenv("MAX_POSITION", "500")),
            log_level=os.getenv("LOG_LEVEL", "INFO"),
        )
```

### 2. Main Application Class

```python
class MMApplication:
    """
    Main market-making application.

    Wires together all components and manages lifecycle.
    """

    def __init__(self, config: AppConfig, strategy: Optional[Strategy] = None):
        self._config = config
        self._custom_strategy = strategy

        # Components (initialized in setup)
        self._gamma: Optional[GammaClient] = None
        self._market_finder: Optional[BitcoinHourlyMarketFinder] = None
        self._market_scheduler: Optional[MarketScheduler] = None

        self._pm_cache: Optional[PMCache] = None
        self._bn_cache: Optional[BNCache] = None

        self._pm_market_ws: Optional[PolymarketMarketWsClient] = None
        self._pm_user_ws: Optional[PolymarketUserWsClient] = None
        self._bn_poller: Optional[BinanceSnapshotPoller] = None

        self._rest_client: Optional[PolymarketRestClient] = None
        self._gateway: Optional[Gateway] = None

        self._intent_mailbox: Optional[IntentMailbox] = None
        self._strategy_runner: Optional[StrategyRunner] = None
        self._executor: Optional[ExecutorActor] = None

        # Control
        self._stop_event = asyncio.Event()
        self._tasks: list[asyncio.Task] = []

        # Current market
        self._current_market: Optional[FoundMarket] = None

    def _setup_components(self) -> None:
        """Initialize all components."""
        # Caches
        self._pm_cache = PMCache()
        self._bn_cache = BNCache()

        # Intent mailbox (coalescing queue)
        self._intent_mailbox = IntentMailbox()

        # Gamma client and market finder
        self._gamma = GammaClient(base_url=self._config.gamma_api_url)
        self._market_finder = BitcoinHourlyMarketFinder(self._gamma)
        self._market_scheduler = MarketScheduler(
            gamma=self._gamma,
            on_market_change=self._on_market_change,
        )

        # REST client
        self._rest_client = PolymarketRestClient(
            base_url=self._config.pm_rest_url,
            private_key=self._config.pm_private_key,
            funder=self._config.pm_funder,
            api_key=self._config.pm_api_key,
            api_secret=self._config.pm_api_secret,
            passphrase=self._config.pm_passphrase,
        )

        # Executor (creates high-priority queue internally)
        executor_config = ExecutorConfig(
            pm_stale_threshold_ms=self._config.pm_stale_threshold_ms,
            bn_stale_threshold_ms=self._config.bn_stale_threshold_ms,
            cancel_timeout_ms=self._config.cancel_timeout_ms,
            cooldown_after_cancel_all_ms=self._config.cooldown_after_cancel_all_ms,
            gross_cap=self._config.gross_cap,
            max_position=self._config.max_position,
        )
        self._executor = ExecutorActor(
            gateway=None,  # Set after gateway created
            pm_cache=self._pm_cache,
            intent_mailbox=self._intent_mailbox,
            config=executor_config,
        )

        # Gateway (uses executor's queue for results)
        self._gateway = Gateway(
            rest_client=self._rest_client,
            result_queue=self._executor.high_priority_queue,
        )
        self._executor._gateway = self._gateway

        # Market WS (feeds PMCache)
        self._pm_market_ws = PolymarketMarketWsClient(
            ws_url=self._config.pm_ws_market_url,
            pm_cache=self._pm_cache,
        )

        # User WS (feeds Executor high-priority queue)
        self._pm_user_ws = PolymarketUserWsClient(
            ws_url=self._config.pm_ws_user_url,
            api_key=self._config.pm_api_key,
            api_secret=self._config.pm_api_secret,
            passphrase=self._config.pm_passphrase,
            executor_queue=self._executor.high_priority_queue,
            on_reconnect=self._on_user_ws_reconnect,
        )

        # Binance snapshot poller (feeds BNCache)
        self._bn_poller = BinanceSnapshotPoller(
            url=self._config.binance_snapshot_url,
            cache=self._bn_cache,
            poll_hz=50,
        )

        # Strategy
        strategy = self._custom_strategy or DefaultMMStrategy(
            base_spread_cents=self._config.base_spread_cents,
            base_size=self._config.base_size,
        )

        self._strategy_runner = StrategyRunner(
            strategy=strategy,
            pm_cache=self._pm_cache,
            bn_cache=self._bn_cache,
            intent_mailbox=self._intent_mailbox,
            get_inventory=self._get_inventory,
            get_fair_price=self._get_fair_price,
            tick_hz=self._config.strategy_hz,
            gross_cap=self._config.gross_cap,
            max_position=self._config.max_position,
        )

    async def _on_market_change(self, market: FoundMarket) -> None:
        """Handle market change event."""
        logger.info(f"Market changed to: {market.question}")
        self._current_market = market

        # Update WS clients
        self._pm_market_ws.set_tokens(
            yes_token_id=market.yes_token_id,
            no_token_id=market.no_token_id,
            market_id=market.condition_id,
        )
        self._pm_user_ws.set_markets([market.condition_id])

        # Update Executor
        self._executor.set_market(
            market_id=market.condition_id,
            yes_token_id=market.yes_token_id,
            no_token_id=market.no_token_id,
        )

    async def _on_user_ws_reconnect(self) -> None:
        """Handle user WS reconnection - trigger cancel-all."""
        logger.warning("User WS reconnected - canceling all orders")
        self._executor._cancel_all_orders()

    def _get_inventory(self) -> tuple[int, int]:
        """Get current inventory from Executor."""
        inv = self._executor._state.inventory
        return (inv.I_yes, inv.I_no)

    def _get_fair_price(self) -> Optional[int]:
        """Get fair price from Binance snapshot."""
        snapshot, _ = self._bn_cache.get_latest()
        if snapshot is None:
            return None
        # Convert probability to cents
        # This assumes binance_pricer provides p_yes in [0, 1]
        # For now, use mid as placeholder
        return int(snapshot.mid_px * 100)  # Convert to cents

    async def start(self) -> None:
        """Start the application."""
        logger.info("Starting MM Application...")

        # Setup components
        self._setup_components()

        # Initialize REST client (derive API keys)
        if not await self._rest_client.initialize():
            raise RuntimeError("Failed to initialize REST client")

        # Update User WS with derived credentials
        self._pm_user_ws.set_auth(
            api_key=self._rest_client.api_key,
            api_secret=self._rest_client.api_secret,
            passphrase=self._rest_client.passphrase,
        )

        # Select initial market
        market = await self._market_scheduler.select_market_for_now()
        if not market:
            raise RuntimeError("No Bitcoin market found for current hour")

        logger.info(f"Selected market: {market.question}")
        logger.info(f"YES token: {market.yes_token_id}")
        logger.info(f"NO token: {market.no_token_id}")

        # Start all tasks
        self._tasks = [
            asyncio.create_task(
                self._pm_market_ws.run(self._stop_event),
                name="pm_market_ws"
            ),
            asyncio.create_task(
                self._pm_user_ws.run(self._stop_event),
                name="pm_user_ws"
            ),
            asyncio.create_task(
                self._bn_poller.run(self._stop_event),
                name="bn_poller"
            ),
            asyncio.create_task(
                self._strategy_runner.run(self._stop_event),
                name="strategy"
            ),
            asyncio.create_task(
                self._executor.run(self._stop_event),
                name="executor"
            ),
            self._gateway.start(self._stop_event),
        ]

        logger.info("MM Application started")

    async def stop(self) -> None:
        """Stop the application."""
        logger.info("Stopping MM Application...")

        # Signal stop
        self._stop_event.set()

        # Cancel tasks
        for task in self._tasks:
            task.cancel()

        # Wait for tasks
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)

        # Close clients
        if self._gamma:
            await self._gamma.close()
        if self._rest_client:
            await self._rest_client.close()

        logger.info("MM Application stopped")

    async def run(self) -> None:
        """Run until shutdown signal."""
        # Setup signal handlers
        loop = asyncio.get_event_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(
                sig,
                lambda: asyncio.create_task(self._handle_signal())
            )

        try:
            await self.start()
            await self._stop_event.wait()
        finally:
            await self.stop()

    async def _handle_signal(self) -> None:
        """Handle shutdown signal."""
        logger.info("Received shutdown signal")
        self._stop_event.set()


def main() -> None:
    """Entry point."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )

    config = AppConfig.from_env()
    app = MMApplication(config)
    asyncio.run(app.run())


if __name__ == "__main__":
    main()
```

### 3. Binance Snapshot Poller

```python
class BinanceSnapshotPoller:
    """
    Polls Binance pricer HTTP endpoint and updates BNCache.
    """

    def __init__(
        self,
        url: str,
        cache: BNCache,
        poll_hz: int = 50,
    ):
        self._url = url
        self._cache = cache
        self._poll_interval_ms = 1000 // poll_hz
        self._session: Optional[aiohttp.ClientSession] = None

    async def run(self, stop_event: asyncio.Event) -> None:
        """Run polling loop."""
        self._session = aiohttp.ClientSession()

        try:
            while not stop_event.is_set():
                tick_start = time.monotonic_ns() // 1_000_000

                try:
                    await self._poll()
                except Exception as e:
                    logger.debug(f"Binance poll failed: {e}")

                elapsed = (time.monotonic_ns() // 1_000_000) - tick_start
                sleep_ms = max(0, self._poll_interval_ms - elapsed)
                await asyncio.sleep(sleep_ms / 1000)

        finally:
            await self._session.close()

    async def _poll(self) -> None:
        """Single poll iteration."""
        async with self._session.get(self._url, timeout=aiohttp.ClientTimeout(total=1)) as resp:
            if resp.status == 200:
                data = await resp.json()
                self._cache.update_from_poll(data)
```

---

## File Locations

- Create: `services/polymarket_trader/src/polymarket_trader/mm_app.py`
- Create: `services/polymarket_trader/src/polymarket_trader/bn_poller.py`

---

## Startup Sequence

1. Load configuration from environment
2. Initialize REST client (derive API credentials)
3. Find current Bitcoin hourly market via Gamma API
4. Configure WS clients with token IDs
5. Start all async tasks:
   - Market WS → PMCache
   - User WS → Executor queue
   - Binance poller → BNCache
   - Strategy runner → Intent mailbox
   - Executor actor → Gateway
   - Gateway worker

---

## Shutdown Sequence

1. Set stop event
2. Cancel all tasks
3. Wait for graceful shutdown
4. Close HTTP sessions

---

## Acceptance Criteria

- [ ] Application starts and discovers current market
- [ ] All WS connections established
- [ ] Strategy running at configured Hz
- [ ] Executor processing events
- [ ] Graceful shutdown on SIGINT/SIGTERM
- [ ] Proper error handling and logging

---

## Testing

Create `tests/test_mm_app.py`:
- Test startup sequence (mock Gamma, WS)
- Test shutdown sequence
- Test market change handling
- Test reconnection handling
