"""State reducer for Container B - single-writer canonical state management."""

import asyncio
import logging
from time import time_ns
from typing import Optional, Callable, Awaitable

from .types import (
    Event, EventType, CanonicalState, RiskMode, SessionState,
    BinanceSnapshotEvent, MarketBboEvent, TickSizeChangedEvent,
    UserOrderEvent, UserTradeEvent, RestOrderAckEvent, RestErrorEvent,
    ControlCommandEvent, WsEvent, OrderState, OrderStatus, Side, OrderPurpose,
)
from .journal_sqlite import EventJournalSQLite
from .market_scheduler import MarketScheduler

logger = logging.getLogger(__name__)


class StateReducer:
    """
    Single-writer canonical state reducer.

    INVARIANT: This is the ONLY component allowed to mutate CanonicalState.

    All events flow through the reducer queue and are processed sequentially.
    """

    def __init__(
        self,
        state: CanonicalState,
        journal: EventJournalSQLite,
        scheduler: MarketScheduler,
        on_reconnect: Optional[Callable[[str], Awaitable[None]]] = None,
    ):
        """
        Initialize the reducer.

        Args:
            state: CanonicalState to manage
            journal: EventJournalSQLite for persistence
            scheduler: MarketScheduler for session transitions
            on_reconnect: Callback when reconnect/reconciliation needed
        """
        self.state = state
        self.journal = journal
        self.scheduler = scheduler
        self._on_reconnect = on_reconnect

        # Event queue - all events flow through here
        self.queue: asyncio.Queue[Event] = asyncio.Queue(maxsize=10000)

        self._running = False
        self._snapshot_interval_events = 100  # Snapshot every N events
        self._event_count = 0

        # Track order IDs from orders WE placed
        self._our_order_ids: set[str] = set()

        # Track seen trade IDs to deduplicate replayed historical trades
        self._seen_trade_ids: set[str] = set()
    
    async def run(self, shutdown_event: Optional[asyncio.Event] = None) -> None:
        """
        Main loop: get event, reduce, persist.
        """
        self._running = True
        logger.info("State reducer started")
        
        while self._running:
            if shutdown_event and shutdown_event.is_set():
                break
            
            try:
                # Wait for event with timeout
                try:
                    event = await asyncio.wait_for(self.queue.get(), timeout=1.0)
                except asyncio.TimeoutError:
                    continue
                
                # Process event
                await self.dispatch(event)
                
                self._event_count += 1
                
                # Periodic snapshot
                if self._event_count % self._snapshot_interval_events == 0:
                    self.journal.append_snapshot(self.state)
            
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Reducer error: {e}", exc_info=True)
        
        logger.info("State reducer stopped")
    
    async def dispatch(self, event: Event) -> None:
        """
        Dispatch an event to the appropriate handler.
        
        Args:
            event: Event to process
        """
        # Journal the event
        self.journal.append(event)
        
        # Dispatch based on type
        if isinstance(event, BinanceSnapshotEvent):
            self._reduce_binance_snapshot(event)
        elif isinstance(event, MarketBboEvent):
            self._reduce_market_bbo(event)
        elif isinstance(event, TickSizeChangedEvent):
            self._reduce_tick_size_changed(event)
        elif isinstance(event, UserOrderEvent):
            self._reduce_user_order(event)
        elif isinstance(event, UserTradeEvent):
            self._reduce_user_trade(event)
        elif isinstance(event, RestOrderAckEvent):
            self._reduce_rest_order_ack(event)
        elif isinstance(event, RestErrorEvent):
            self._reduce_rest_error(event)
        elif isinstance(event, ControlCommandEvent):
            await self._reduce_control_command(event)
        elif isinstance(event, WsEvent):
            await self._reduce_ws_event(event)
    
    def reduce(self, event: Event) -> None:
        """
        Synchronous reduce (for testing).
        
        Args:
            event: Event to reduce
        """
        asyncio.create_task(self.dispatch(event))
    
    def _reduce_binance_snapshot(self, event: BinanceSnapshotEvent) -> None:
        """Handle Binance snapshot event."""
        if event.snapshot:
            self.state.latest_snapshot = event.snapshot
            self.state.health.binance_snapshot_stale = event.snapshot.stale
            self.state.health.binance_snapshot_age_ms = event.snapshot.age_ms
    
    def _reduce_market_bbo(self, event: MarketBboEvent) -> None:
        """Handle market BBO event."""
        # Route to correct YES/NO fields based on token_id
        token_ids = self.state.token_ids
        is_yes_token = token_ids and event.token_id == token_ids.yes_token_id
        is_no_token = token_ids and event.token_id == token_ids.no_token_id

        if is_yes_token:
            if event.best_bid is not None:
                self.state.market_view.yes_best_bid = event.best_bid
            if event.best_ask is not None:
                self.state.market_view.yes_best_ask = event.best_ask
        elif is_no_token:
            if event.best_bid is not None:
                self.state.market_view.no_best_bid = event.best_bid
            if event.best_ask is not None:
                self.state.market_view.no_best_ask = event.best_ask

        # Also update legacy combined fields (for backward compatibility)
        # Use YES token prices if available, otherwise use whatever we got
        if event.best_bid is not None:
            self.state.market_view.best_bid = event.best_bid
        if event.best_ask is not None:
            self.state.market_view.best_ask = event.best_ask

        self.state.market_view.tick_size = event.tick_size
        self.state.market_view.book_ts_local_ms = event.ts_local_ms
        self.state.health.market_ws_last_msg_ms = event.ts_local_ms
    
    def _reduce_tick_size_changed(self, event: TickSizeChangedEvent) -> None:
        """Handle tick size change event."""
        old_tick = self.state.market_view.tick_size
        self.state.market_view.tick_size = event.new_tick_size
        logger.info(f"Tick size changed: {old_tick} -> {event.new_tick_size}")
    
    def _reduce_user_order(self, event: UserOrderEvent) -> None:
        """Handle user order event."""
        order_id = event.order_id
        
        if event.status in (OrderStatus.FILLED, OrderStatus.CANCELLED, OrderStatus.EXPIRED):
            # Order is terminal - remove from open orders
            if order_id in self.state.open_orders:
                del self.state.open_orders[order_id]
                logger.debug(f"Removed terminal order {order_id}: {event.status.name}")
        else:
            # Update or add order
            if order_id in self.state.open_orders:
                # Update existing
                order = self.state.open_orders[order_id]
                order.status = event.status
                order.filled = event.filled
                order.remaining = event.remaining
                order.last_update_ms = event.ts_local_ms
            else:
                # New order (might be from recovery or external)
                self.state.open_orders[order_id] = OrderState(
                    order_id=order_id,
                    client_req_id="",
                    side=event.side,
                    price=event.price,
                    size=event.size,
                    filled=event.filled,
                    remaining=event.remaining,
                    status=event.status,
                    token_id=event.token_id,
                    purpose=OrderPurpose.QUOTE,  # Default, unknown
                    created_at_ms=event.ts_local_ms,
                    expires_at_ms=0,
                    last_update_ms=event.ts_local_ms,
                )
        
        self.state.health.user_ws_last_msg_ms = event.ts_local_ms
    
    def _reduce_user_trade(self, event: UserTradeEvent) -> None:
        """Handle user trade event."""
        # Deduplicate: skip trades we've already processed
        if event.trade_id and event.trade_id in self._seen_trade_ids:
            print(f"[TRADE] SKIP duplicate trade_id: {event.trade_id[:20]}...")
            return

        # Mark as seen
        if event.trade_id:
            self._seen_trade_ids.add(event.trade_id)

        token_id = event.token_id
        size = event.size
        token_ids = self.state.token_ids

        # Identify which token was traded
        is_yes_token = token_ids and token_id == token_ids.yes_token_id
        is_no_token = token_ids and token_id == token_ids.no_token_id

        token_name = "YES" if is_yes_token else ("NO" if is_no_token else "UNKNOWN")
        print(f"[TRADE] {event.side.name} {size} {token_name} @ {event.price}")

        if is_yes_token:
            # YES token: BUY increases yes_tokens, SELL decreases
            if event.side == Side.BUY:
                self.state.positions.yes_tokens += size
            else:
                self.state.positions.yes_tokens -= size
        elif is_no_token:
            # NO token: BUY increases no_tokens, SELL decreases
            if event.side == Side.BUY:
                self.state.positions.no_tokens += size
            else:
                self.state.positions.no_tokens -= size
        else:
            print(f"[TRADE] WARNING: Unknown token {token_id[:30]}...")

        print(
            f"[TRADE] Position updated -> YES={self.state.positions.yes_tokens}, "
            f"NO={self.state.positions.no_tokens}"
        )

        self.state.health.user_ws_last_msg_ms = event.ts_local_ms
    
    def _reduce_rest_order_ack(self, event: RestOrderAckEvent) -> None:
        """Handle REST order acknowledgment."""
        if event.success:
            logger.debug(f"Order ack: {event.client_req_id} -> {event.order_id}")
            # Track this order as OURS so we can filter trades
            if event.order_id:
                self._our_order_ids.add(event.order_id)
                print(f"[REDUCER] Tracking our order: {event.order_id[:20]}...")
            # Order will be tracked via WS updates
        else:
            logger.warning(f"Order failed: {event.client_req_id}")
    
    def _reduce_rest_error(self, event: RestErrorEvent) -> None:
        """Handle REST error event."""
        logger.warning(f"REST error: {event.client_req_id} - {event.reason}")
        
        self.state.health.rest_healthy = event.recoverable
        self.state.health.rest_last_error_ms = event.ts_local_ms
        
        # Escalate to conservative mode on repeated errors
        if not event.recoverable:
            self.state.risk_mode = RiskMode.HALT
            logger.warning("Escalated to HALT due to unrecoverable REST error")
    
    async def _reduce_control_command(self, event: ControlCommandEvent) -> None:
        """Handle control command event."""
        cmd_type = event.command_type
        payload = event.payload
        
        logger.info(f"Control command: {cmd_type} {payload}")
        
        if cmd_type == "pause_quoting":
            self.state.limits.quoting_enabled = not payload.get("pause", True)
        
        elif cmd_type == "set_mode":
            mode_str = payload.get("mode", "").upper()
            if mode_str in RiskMode.__members__:
                self.state.risk_mode = RiskMode[mode_str]
        
        elif cmd_type == "flatten_now":
            self.state.session_state = SessionState.FLATTEN
            self.state.risk_mode = RiskMode.FLATTEN
        
        elif cmd_type == "set_limits":
            if "max_reserved_capital" in payload:
                self.state.limits.max_reserved_capital = float(payload["max_reserved_capital"])
            if "max_order_size" in payload:
                self.state.limits.max_order_size = float(payload["max_order_size"])
            if "max_position_size" in payload:
                self.state.limits.max_position_size = float(payload["max_position_size"])
    
    async def _reduce_ws_event(self, event: WsEvent) -> None:
        """Handle WebSocket connection event."""
        if event.ws_name == "market":
            self.state.health.market_ws_connected = event.connected
            if event.connected:
                self.state.health.market_ws_last_msg_ms = event.ts_local_ms
        elif event.ws_name == "user":
            self.state.health.user_ws_connected = event.connected
            if event.connected:
                self.state.health.user_ws_last_msg_ms = event.ts_local_ms
        
        if not event.connected:
            # WS disconnected - trigger conservative mode
            logger.warning(f"WS {event.ws_name} disconnected - escalating to HALT")
            self.state.risk_mode = RiskMode.HALT
            
            # Trigger reconciliation
            if self._on_reconnect and self.state.market_view.market_id:
                await self._on_reconnect(self.state.market_view.market_id)
        else:
            # WS reconnected
            if event.ws_name == "user":
                # Stay in HALT until we verify state
                logger.info(f"WS {event.ws_name} reconnected - awaiting reconciliation")
            
            # If all connections are healthy, resume NORMAL trading
            if (
                self.state.risk_mode == RiskMode.HALT and
                self.state.session_state == SessionState.ACTIVE and
                self.state.health.market_ws_connected and
                self.state.health.user_ws_connected and
                self.state.health.rest_healthy
            ):
                logger.info("All connections healthy - resuming NORMAL trading")
                self.state.risk_mode = RiskMode.NORMAL
    
    async def drain_pending(self) -> int:
        """
        Process all pending events in the queue immediately.

        Call this before reading state to ensure you have the freshest data.

        Returns:
            Number of events drained
        """
        count = 0
        while True:
            try:
                event = self.queue.get_nowait()
                await self.dispatch(event)
                self._event_count += 1
                count += 1
            except asyncio.QueueEmpty:
                break
        return count

    def stop(self) -> None:
        """Stop the reducer."""
        self._running = False
