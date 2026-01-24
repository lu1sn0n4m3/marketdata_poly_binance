"""
Executor Actor for trading system.

Single-writer component that owns all trading state.
Processes events deterministically and manages order lifecycle.

Key design:
- Single-writer: only Executor modifies state
- Event-driven: processes events from inbox queue
- Order materialization: converts YES-space intents to real orders
- Position-aware: sell YES if holding, buy NO at complement otherwise
"""

import json
import logging
import os
import queue
import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional, Callable

from .types import (
    Token,
    Side,
    OrderStatus,
    QuoteMode,
    ExecutorEventType,
    ExecutorConfig,
    InventoryState,
    RiskState,
    WorkingOrder,
    RealOrderSpec,
    DesiredQuoteSet,
    GatewayResultEvent,
    OrderAckEvent,
    CancelAckEvent,
    FillEvent,
    TimerTickEvent,
    StrategyIntentEvent,
    now_ms,
)
from .strategy import IntentMailbox
from .gateway import Gateway

logger = logging.getLogger(__name__)


# =============================================================================
# TRADE LOGGER
# =============================================================================


class TradeLogger:
    """
    Logs trades to a JSON file for analysis.

    Each fill is appended as a JSON line to the file.
    """

    def __init__(self, output_dir: str = "trades"):
        """
        Initialize trade logger.

        Args:
            output_dir: Directory to store trade logs
        """
        self._output_dir = Path(output_dir)
        self._output_dir.mkdir(parents=True, exist_ok=True)

        # Create filename with timestamp
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self._filepath = self._output_dir / f"trades_{timestamp}.jsonl"
        self._file = open(self._filepath, "a")

        logger.info(f"Trade log: {self._filepath}")

    def log_fill(
        self,
        ts: int,
        token: str,
        side: str,
        size: int,
        price: int,
        order_id: str,
        realized_pnl: int,
        inventory_yes: int,
        inventory_no: int,
        net_position: int,
    ) -> None:
        """Log a fill to the trade file."""
        record = {
            "ts": ts,
            "time": datetime.fromtimestamp(ts / 1000).isoformat(),
            "token": token,
            "side": side,
            "size": size,
            "price_cents": price,
            "value_usd": round(size * price / 100, 4),
            "order_id": order_id[:20] + "..." if len(order_id) > 20 else order_id,
            "realized_pnl_cents": realized_pnl,
            "realized_pnl_usd": round(realized_pnl / 100, 4),
            "inventory": {
                "yes": inventory_yes,
                "no": inventory_no,
                "net": net_position,
            },
        }

        self._file.write(json.dumps(record) + "\n")
        self._file.flush()  # Ensure immediate write

    def close(self) -> None:
        """Close the trade log file."""
        if self._file:
            self._file.close()
            logger.info(f"Trade log closed: {self._filepath}")


# =============================================================================
# EXECUTOR STATE
# =============================================================================


@dataclass
class PendingPlace:
    """Tracking for pending order placement."""
    action_id: str
    spec: RealOrderSpec
    sent_at_ts: int
    is_bid: bool  # True for bid leg, False for ask leg


@dataclass
class PendingCancel:
    """Tracking for pending cancellation."""
    action_id: str
    server_order_id: str
    sent_at_ts: int


@dataclass
class FillRecord:
    """Record of a single fill for PnL tracking."""
    ts: int
    token: "Token"
    side: "Side"
    size: int
    price: int  # cents
    order_id: str


@dataclass
class PnLTracker:
    """
    Tracks PnL from fills.

    Uses FIFO accounting for realized PnL calculation.
    All values in cents (1 cent = 0.01 USDC).
    """
    # Fill history
    fills: list[FillRecord] = field(default_factory=list)

    # Running totals
    total_buy_value: int = 0      # Total spent buying (cents)
    total_buy_shares: int = 0     # Total shares bought
    total_sell_value: int = 0     # Total received selling (cents)
    total_sell_shares: int = 0    # Total shares sold

    # Realized PnL (from closed roundtrips)
    realized_pnl_cents: int = 0

    # For FIFO cost tracking: list of (price, remaining_size)
    # Positive = long position (bought), negative = short position (sold)
    cost_basis_queue: list[tuple[int, int]] = field(default_factory=list)

    def record_fill(
        self,
        ts: int,
        token: "Token",
        side: "Side",
        size: int,
        price: int,
        order_id: str,
    ) -> tuple[int, int]:
        """
        Record a fill and update PnL.

        Returns: (realized_pnl_this_fill, current_avg_cost_basis)
        """
        from .types import Side, Token

        self.fills.append(FillRecord(ts, token, side, size, price, order_id))

        # Convert to YES-space for consistent accounting
        # NO token trades are converted: price becomes (100 - price)
        # and the side is effectively flipped
        #
        # BUY YES at P  = add long YES at cost P
        # SELL YES at P = close long YES, receive P
        # BUY NO at P   = add short YES at effective price (100-P)
        # SELL NO at P  = close short YES at effective price (100-P)

        if token == Token.NO:
            # Convert NO to YES-space
            effective_price = 100 - price
            # BUY NO = short YES, SELL NO = cover short (like buying YES)
            is_adding_long = (side == Side.SELL)  # SELL NO = add long YES exposure
        else:
            effective_price = price
            is_adding_long = (side == Side.BUY)   # BUY YES = add long YES exposure

        fill_value = size * price  # Original value for volume tracking
        realized_this_fill = 0

        if is_adding_long:
            self.total_buy_value += fill_value
            self.total_buy_shares += size

            # Check if closing short position (FIFO)
            remaining = size
            while remaining > 0 and self.cost_basis_queue and self.cost_basis_queue[0][1] < 0:
                cost_price, cost_sz = self.cost_basis_queue[0]
                close_sz = min(remaining, abs(cost_sz))
                # PnL = (sold_price - buy_price) * shares
                # We shorted at cost_price, now covering at effective_price
                realized_this_fill += (cost_price - effective_price) * close_sz
                remaining -= close_sz
                if close_sz == abs(cost_sz):
                    self.cost_basis_queue.pop(0)
                else:
                    self.cost_basis_queue[0] = (cost_price, cost_sz + close_sz)

            # Remaining is new long position
            if remaining > 0:
                self.cost_basis_queue.append((effective_price, remaining))
        else:  # Reducing long / adding short
            self.total_sell_value += fill_value
            self.total_sell_shares += size

            # Check if closing long position (FIFO)
            remaining = size
            while remaining > 0 and self.cost_basis_queue and self.cost_basis_queue[0][1] > 0:
                cost_price, cost_sz = self.cost_basis_queue[0]
                close_sz = min(remaining, cost_sz)
                # PnL = (sell_price - cost_price) * shares
                realized_this_fill += (effective_price - cost_price) * close_sz
                remaining -= close_sz
                if close_sz == cost_sz:
                    self.cost_basis_queue.pop(0)
                else:
                    self.cost_basis_queue[0] = (cost_price, cost_sz - close_sz)

            # Remaining is new short position
            if remaining > 0:
                self.cost_basis_queue.append((effective_price, -remaining))

        self.realized_pnl_cents += realized_this_fill

        # Calculate average cost basis
        avg_cost = self._avg_cost_basis()

        return realized_this_fill, avg_cost

    def _avg_cost_basis(self) -> int:
        """Get average cost basis of open position (cents)."""
        total_cost = 0
        total_sz = 0
        for price, sz in self.cost_basis_queue:
            total_cost += price * abs(sz)
            total_sz += abs(sz)
        return total_cost // total_sz if total_sz > 0 else 0

    def get_position(self) -> int:
        """Get current position from cost basis queue."""
        return sum(sz for _, sz in self.cost_basis_queue)

    def get_unrealized_pnl(self, mark_price: int) -> int:
        """Get unrealized PnL at given mark price (cents)."""
        unrealized = 0
        for cost_price, sz in self.cost_basis_queue:
            if sz > 0:  # Long
                unrealized += (mark_price - cost_price) * sz
            else:  # Short
                unrealized += (cost_price - mark_price) * abs(sz)
        return unrealized

    def get_summary(self, mark_price: int = 50) -> dict:
        """Get PnL summary."""
        position = self.get_position()
        unrealized = self.get_unrealized_pnl(mark_price)
        total_pnl = self.realized_pnl_cents + unrealized

        return {
            "fills_count": len(self.fills),
            "total_bought": self.total_buy_shares,
            "total_sold": self.total_sell_shares,
            "avg_buy_px": self.total_buy_value / self.total_buy_shares if self.total_buy_shares else 0,
            "avg_sell_px": self.total_sell_value / self.total_sell_shares if self.total_sell_shares else 0,
            "position": position,
            "avg_cost": self._avg_cost_basis(),
            "realized_pnl_cents": self.realized_pnl_cents,
            "unrealized_pnl_cents": unrealized,
            "total_pnl_cents": total_pnl,
            "realized_pnl_usd": self.realized_pnl_cents / 100,
            "total_pnl_usd": total_pnl / 100,
        }


@dataclass
class ExecutorState:
    """
    Complete executor state.

    All mutable state owned by the Executor.
    """
    # Inventory
    inventory: InventoryState = field(default_factory=InventoryState)

    # Working orders keyed by server_order_id
    working_orders: dict[str, WorkingOrder] = field(default_factory=dict)

    # Pending operations keyed by action_id
    pending_places: dict[str, PendingPlace] = field(default_factory=dict)
    pending_cancels: dict[str, PendingCancel] = field(default_factory=dict)

    # Risk state
    risk: RiskState = field(default_factory=RiskState)

    # Last processed intent
    last_intent: Optional[DesiredQuoteSet] = None
    last_intent_ts: int = 0

    # Order ID tracking
    bid_order_id: Optional[str] = None  # Current bid order server ID
    ask_order_id: Optional[str] = None  # Current ask order server ID

    # PnL tracking
    pnl: PnLTracker = field(default_factory=PnLTracker)

    def get_working_bid(self) -> Optional[WorkingOrder]:
        """Get current working bid order if any."""
        if self.bid_order_id and self.bid_order_id in self.working_orders:
            return self.working_orders[self.bid_order_id]
        return None

    def get_working_ask(self) -> Optional[WorkingOrder]:
        """Get current working ask order if any."""
        if self.ask_order_id and self.ask_order_id in self.working_orders:
            return self.working_orders[self.ask_order_id]
        return None


# =============================================================================
# ORDER MATERIALIZER
# =============================================================================


class OrderMaterializer:
    """
    Converts YES-space intents to real orders.

    Position-aware routing:
    - YES bid: if holding NO, sell NO; otherwise buy YES
    - YES ask: if holding YES, sell YES; otherwise buy NO
    """

    def __init__(
        self,
        yes_token_id: str,
        no_token_id: str,
    ):
        self._yes_token_id = yes_token_id
        self._no_token_id = no_token_id

    def materialize_bid(
        self,
        px_yes: int,
        sz: int,
        inventory: InventoryState,
        min_size: int = 5,
    ) -> RealOrderSpec:
        """
        Materialize YES bid to real order.

        If we have NO position, prefer SELL NO to reduce exposure.
        Otherwise, BUY YES.

        Args:
            px_yes: YES bid price in cents
            sz: Size in shares
            inventory: Current inventory
            min_size: Minimum order size (Polymarket requires 5)

        Returns:
            RealOrderSpec for the order to place
        """
        # If holding NO, sell NO at complement price
        # But only if we have enough to meet minimum order size
        if inventory.I_no >= min_size:
            return RealOrderSpec(
                token=Token.NO,
                token_id=self._no_token_id,
                side=Side.SELL,
                px=100 - px_yes,  # Complement price
                sz=min(sz, inventory.I_no),
            )

        # Otherwise, buy YES (no inventory limit on buying)
        return RealOrderSpec(
            token=Token.YES,
            token_id=self._yes_token_id,
            side=Side.BUY,
            px=px_yes,
            sz=sz,
        )

    def materialize_ask(
        self,
        px_yes: int,
        sz: int,
        inventory: InventoryState,
        min_size: int = 5,
    ) -> RealOrderSpec:
        """
        Materialize YES ask to real order.

        If we have YES position, prefer SELL YES to reduce exposure.
        Otherwise, BUY NO at complement.

        Args:
            px_yes: YES ask price in cents
            sz: Size in shares
            inventory: Current inventory
            min_size: Minimum order size (Polymarket requires 5)

        Returns:
            RealOrderSpec for the order to place
        """
        # If holding YES, sell YES
        # But only if we have enough to meet minimum order size
        if inventory.I_yes >= min_size:
            return RealOrderSpec(
                token=Token.YES,
                token_id=self._yes_token_id,
                side=Side.SELL,
                px=px_yes,
                sz=min(sz, inventory.I_yes),
            )

        # Otherwise, buy NO at complement (no inventory limit on buying)
        return RealOrderSpec(
            token=Token.NO,
            token_id=self._no_token_id,
            side=Side.BUY,
            px=100 - px_yes,  # Complement price
            sz=sz,
        )


# =============================================================================
# EXECUTOR ACTOR
# =============================================================================


class ExecutorActor:
    """
    Main executor actor - single-writer for all trading state.

    Processes events from inbox queue:
    - StrategyIntentEvent: new quote intent from strategy
    - GatewayResultEvent: result from order placement/cancel
    - OrderAckEvent: order acknowledged by exchange
    - CancelAckEvent: cancel acknowledged
    - FillEvent: order fill
    - TimerTickEvent: periodic maintenance

    All state modifications happen here.
    """

    def __init__(
        self,
        gateway: Gateway,
        mailbox: IntentMailbox,
        event_queue: queue.Queue,
        config: ExecutorConfig,
        yes_token_id: str,
        no_token_id: str,
        market_id: str,
        on_cancel_all: Optional[Callable[[], None]] = None,
    ):
        """
        Initialize executor.

        Args:
            gateway: Gateway for order submission
            mailbox: IntentMailbox for strategy intents
            event_queue: Queue for incoming events (fills, acks)
            config: Executor configuration
            yes_token_id: YES token ID for this market
            no_token_id: NO token ID for this market
            market_id: Market/condition ID for cancel-all
            on_cancel_all: Optional callback when cancel-all is triggered
        """
        self._gateway = gateway
        self._mailbox = mailbox
        self._event_queue = event_queue
        self._config = config
        self._market_id = market_id
        self._on_cancel_all = on_cancel_all

        # State
        self._state = ExecutorState()
        self._materializer = OrderMaterializer(yes_token_id, no_token_id)

        # Deduplication: track processed fills to avoid double-counting
        # Key: (order_id, size, price) - unique per fill event
        self._processed_fills: set[tuple[str, int, int]] = set()

        # Threading
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

        # Client order ID counter
        self._client_order_counter = 0
        self._counter_lock = threading.Lock()

        # Trade logger
        self._trade_logger = TradeLogger()

        # Session start time - used to filter out old fills from WS
        self._session_start_ts: int = 0

    def start(self) -> None:
        """Start executor thread."""
        if self._thread and self._thread.is_alive():
            logger.warning("ExecutorActor already running")
            return

        # Record session start time
        self._session_start_ts = now_ms()
        logger.info(f"Session started at {self._session_start_ts}")

        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run_loop,
            name="Executor-Actor",
            daemon=True,
        )
        self._thread.start()
        logger.info("ExecutorActor started")

    def stop(self, timeout: float = 5.0) -> None:
        """Stop executor thread."""
        logger.info("ExecutorActor stopping...")
        self._stop_event.set()

        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=timeout)
            if self._thread.is_alive():
                logger.warning("ExecutorActor did not stop in time")

        # Close trade logger
        if self._trade_logger:
            self._trade_logger.close()

        logger.info("ExecutorActor stopped")

    def get_pnl_stats(self, mark_price: int = 50) -> dict:
        """
        Get current PnL statistics.

        Args:
            mark_price: Current market mid price for unrealized PnL calc (cents)

        Returns:
            Dict with fills_count, total_bought, total_sold, avg_buy_px, avg_sell_px,
            position, realized_pnl_cents, unrealized_pnl_cents, total_pnl_cents,
            realized_pnl_usd, total_pnl_usd
        """
        return self._state.pnl.get_summary(mark_price)

    def get_fill_history(self) -> list[FillRecord]:
        """Get list of all recorded fills."""
        return list(self._state.pnl.fills)

    def _run_loop(self) -> None:
        """Main event processing loop."""
        while not self._stop_event.is_set():
            try:
                # Check for new intent from mailbox
                intent = self._mailbox.get()
                if intent:
                    self._handle_intent(intent)

                # Check for events from queue
                try:
                    event = self._event_queue.get(timeout=0.01)
                    self._handle_event(event)
                except queue.Empty:
                    pass

            except Exception as e:
                logger.error(f"Executor loop error: {e}")

    def _handle_event(self, event) -> None:
        """Route event to appropriate handler."""
        ts = now_ms()

        if isinstance(event, GatewayResultEvent):
            self._on_gateway_result(event, ts)
        elif isinstance(event, OrderAckEvent):
            self._on_order_ack(event, ts)
        elif isinstance(event, CancelAckEvent):
            self._on_cancel_ack(event, ts)
        elif isinstance(event, FillEvent):
            self._on_fill(event, ts)
        elif isinstance(event, TimerTickEvent):
            self._on_timer_tick(ts)
        else:
            logger.warning(f"Unknown event type: {type(event)}")

    # -------------------------------------------------------------------------
    # INTENT HANDLING
    # -------------------------------------------------------------------------

    def _handle_intent(self, intent: DesiredQuoteSet) -> None:
        """Handle new strategy intent."""
        ts = now_ms()
        state = self._state

        # Check cooldown
        if state.risk.is_in_cooldown(ts):
            logger.debug("In cooldown, ignoring intent")
            return

        # Store intent
        state.last_intent = intent
        state.last_intent_ts = ts

        # Handle STOP mode
        if intent.mode == QuoteMode.STOP:
            self._cancel_all_orders("STOP_MODE")
            return

        # Reconcile bid
        if intent.bid_yes.enabled:
            self._reconcile_bid(intent, ts)
        else:
            self._cancel_bid_if_exists(ts)

        # Reconcile ask
        if intent.ask_yes.enabled:
            self._reconcile_ask(intent, ts)
        else:
            self._cancel_ask_if_exists(ts)

    def _reconcile_bid(self, intent: DesiredQuoteSet, ts: int) -> None:
        """Reconcile bid order with intent."""
        state = self._state
        cfg = self._config
        leg = intent.bid_yes

        # Materialize the desired order
        desired = self._materializer.materialize_bid(
            px_yes=leg.px_yes,
            sz=leg.sz,
            inventory=state.inventory,
        )

        # Check if current bid matches
        current = state.get_working_bid()
        if current and not current.is_terminal:
            # Check if matches within tolerance
            if current.order_spec.matches(
                desired,
                price_tol=cfg.price_tolerance_cents,
                size_tol=cfg.size_tolerance_shares,
            ):
                return  # No action needed

            # Cancel existing bid
            self._submit_cancel(current.server_order_id, ts)

        # Place new bid if no pending
        if not self._has_pending_bid():
            self._submit_place(desired, is_bid=True, ts=ts)

    def _reconcile_ask(self, intent: DesiredQuoteSet, ts: int) -> None:
        """Reconcile ask order with intent."""
        state = self._state
        cfg = self._config
        leg = intent.ask_yes

        # Materialize the desired order
        desired = self._materializer.materialize_ask(
            px_yes=leg.px_yes,
            sz=leg.sz,
            inventory=state.inventory,
        )

        # Check if current ask matches
        current = state.get_working_ask()
        if current and not current.is_terminal:
            # Check if matches within tolerance
            if current.order_spec.matches(
                desired,
                price_tol=cfg.price_tolerance_cents,
                size_tol=cfg.size_tolerance_shares,
            ):
                return  # No action needed

            # Cancel existing ask
            self._submit_cancel(current.server_order_id, ts)

        # Place new ask if no pending
        if not self._has_pending_ask():
            self._submit_place(desired, is_bid=False, ts=ts)

    def _cancel_bid_if_exists(self, ts: int) -> None:
        """Cancel current bid if exists."""
        current = self._state.get_working_bid()
        if current and not current.is_terminal:
            self._submit_cancel(current.server_order_id, ts)

    def _cancel_ask_if_exists(self, ts: int) -> None:
        """Cancel current ask if exists."""
        current = self._state.get_working_ask()
        if current and not current.is_terminal:
            self._submit_cancel(current.server_order_id, ts)

    def _has_pending_bid(self) -> bool:
        """Check if there's a pending bid placement."""
        for p in self._state.pending_places.values():
            if p.is_bid:
                return True
        return False

    def _has_pending_ask(self) -> bool:
        """Check if there's a pending ask placement."""
        for p in self._state.pending_places.values():
            if not p.is_bid:
                return True
        return False

    def _reconcile_after_order_change(self, ts: int) -> None:
        """
        Re-reconcile with last intent after order canceled/filled.

        This ensures we re-place orders if the strategy still wants them.
        Called after cancel acks and full fills.
        """
        state = self._state

        # Check cooldown
        if state.risk.is_in_cooldown(ts):
            logger.debug("In cooldown, skipping post-cancel reconcile")
            return

        # Need a valid last intent
        if not state.last_intent:
            return

        intent = state.last_intent

        # Don't reconcile if in STOP mode
        if intent.mode == QuoteMode.STOP:
            return

        # Re-reconcile bid if missing
        if intent.bid_yes.enabled and state.bid_order_id is None:
            if not self._has_pending_bid():
                logger.info("Re-placing bid after order change")
                self._reconcile_bid(intent, ts)

        # Re-reconcile ask if missing
        if intent.ask_yes.enabled and state.ask_order_id is None:
            if not self._has_pending_ask():
                logger.info("Re-placing ask after order change")
                self._reconcile_ask(intent, ts)

    # -------------------------------------------------------------------------
    # ORDER SUBMISSION
    # -------------------------------------------------------------------------

    def _submit_place(self, spec: RealOrderSpec, is_bid: bool, ts: int) -> None:
        """Submit order placement to gateway."""
        # Generate client order ID
        spec.client_order_id = self._next_client_order_id()

        # Submit to gateway
        action_id = self._gateway.submit_place(spec)

        # Track pending
        self._state.pending_places[action_id] = PendingPlace(
            action_id=action_id,
            spec=spec,
            sent_at_ts=ts,
            is_bid=is_bid,
        )

        logger.debug(f"Submitted place: {action_id} is_bid={is_bid} {spec.token.name} {spec.side.name} @ {spec.px}c x {spec.sz}")

    def _submit_cancel(self, server_order_id: str, ts: int) -> None:
        """Submit cancellation to gateway."""
        # Check if already cancelling
        for pc in self._state.pending_cancels.values():
            if pc.server_order_id == server_order_id:
                return

        action_id = self._gateway.submit_cancel(server_order_id)

        self._state.pending_cancels[action_id] = PendingCancel(
            action_id=action_id,
            server_order_id=server_order_id,
            sent_at_ts=ts,
        )

        logger.debug(f"Submitted cancel: {action_id} for {server_order_id}")

    def _cancel_all_orders(self, reason: str) -> None:
        """Cancel all orders (fast path)."""
        ts = now_ms()
        state = self._state

        logger.warning(f"Cancel-all triggered: {reason}")

        # Submit cancel-all
        self._gateway.submit_cancel_all(self._market_id)

        # Update risk state
        state.risk.last_cancel_all_ts = ts
        state.risk.set_cooldown(ts, self._config.cooldown_after_cancel_all_ms)

        # Clear our tracked orders
        state.bid_order_id = None
        state.ask_order_id = None
        state.working_orders.clear()
        state.pending_places.clear()
        state.pending_cancels.clear()

        # Callback
        if self._on_cancel_all:
            try:
                self._on_cancel_all()
            except Exception as e:
                logger.error(f"Cancel-all callback failed: {e}")

    def _next_client_order_id(self) -> str:
        """Generate unique client order ID."""
        with self._counter_lock:
            self._client_order_counter += 1
            return f"exec_{self._client_order_counter}_{now_ms()}"

    # -------------------------------------------------------------------------
    # EVENT HANDLERS
    # -------------------------------------------------------------------------

    def _on_gateway_result(self, event: GatewayResultEvent, ts: int) -> None:
        """Handle gateway result."""
        state = self._state
        action_id = event.action_id

        # Check if this is a place result
        if action_id in state.pending_places:
            pending = state.pending_places.pop(action_id)

            if event.success and event.server_order_id:
                # Create working order
                order = WorkingOrder(
                    client_order_id=pending.spec.client_order_id,
                    server_order_id=event.server_order_id,
                    order_spec=pending.spec,
                    status=OrderStatus.PENDING_NEW,
                    created_ts=ts,
                    last_state_change_ts=ts,
                )
                state.working_orders[event.server_order_id] = order

                # Track as bid or ask
                if pending.is_bid:
                    state.bid_order_id = event.server_order_id
                else:
                    state.ask_order_id = event.server_order_id

                logger.debug(f"Order placed: {event.server_order_id}")
            else:
                logger.warning(f"Place failed: {event.error_kind}")

        # Check if this is a cancel result
        elif action_id in state.pending_cancels:
            pending = state.pending_cancels.pop(action_id)

            if event.success:
                # Remove from working orders
                if pending.server_order_id in state.working_orders:
                    del state.working_orders[pending.server_order_id]

                # Clear bid/ask tracking
                if state.bid_order_id == pending.server_order_id:
                    state.bid_order_id = None
                if state.ask_order_id == pending.server_order_id:
                    state.ask_order_id = None

                logger.debug(f"Cancel confirmed: {pending.server_order_id}")
            else:
                logger.warning(f"Cancel failed: {event.error_kind}")

    def _on_order_ack(self, event: OrderAckEvent, ts: int) -> None:
        """Handle order acknowledgment from exchange."""
        state = self._state
        order_id = event.server_order_id

        if order_id in state.working_orders:
            order = state.working_orders[order_id]
            order.status = event.status
            order.last_state_change_ts = ts
            logger.debug(f"Order ack: {order_id} status={event.status.name}")

    def _on_cancel_ack(self, event: CancelAckEvent, ts: int) -> None:
        """Handle cancel acknowledgment from exchange."""
        state = self._state
        order_id = event.server_order_id

        if event.success and order_id in state.working_orders:
            order = state.working_orders[order_id]
            order.status = OrderStatus.CANCELED
            order.last_state_change_ts = ts

            # Track which side was canceled
            was_bid = state.bid_order_id == order_id
            was_ask = state.ask_order_id == order_id

            # Clear tracking
            if was_bid:
                state.bid_order_id = None
            if was_ask:
                state.ask_order_id = None

            logger.info(f"Cancel ack: {order_id[:20]}... ({'bid' if was_bid else 'ask' if was_ask else '?'})")

            # Re-reconcile with last intent to re-place if strategy still wants it
            self._reconcile_after_order_change(ts)

    def _on_fill(self, event: FillEvent, ts: int) -> None:
        """Handle fill event."""
        state = self._state
        order_id = event.server_order_id

        # Filter out old fills from before this session started
        # WS may deliver historical fills on reconnect
        if event.ts_exchange < self._session_start_ts:
            logger.debug(
                f"[FILL] Ignoring old fill from before session: "
                f"fill_ts={event.ts_exchange} session_start={self._session_start_ts}"
            )
            return

        # Only process fills for orders WE track
        if order_id not in state.working_orders:
            logger.debug(
                f"[FILL] Ignoring fill for unknown order: {order_id[:20] if order_id else 'N/A'}..."
            )
            return

        # Deduplication: skip if we've already processed this exact fill
        fill_key = (order_id, event.size, event.price)
        if fill_key in self._processed_fills:
            logger.warning(
                f"[FILL] Skipping duplicate fill: order={order_id[:20]}... "
                f"size={event.size} price={event.price}c"
            )
            return
        self._processed_fills.add(fill_key)

        # DEBUG: Log fill event details
        logger.info(
            f"[FILL_DEBUG] Processing fill: order={order_id[:20] if order_id else 'N/A'}... "
            f"token={event.token.name} side={event.side.name} size={event.size} price={event.price}c"
        )
        logger.info(
            f"[FILL_DEBUG] Inventory BEFORE: YES={state.inventory.I_yes} NO={state.inventory.I_no} net_E={state.inventory.net_E}"
        )

        # Update inventory (only for our orders)
        state.inventory.update_from_fill(
            token=event.token,
            side=event.side,
            size=event.size,
            ts=ts,
        )

        # Update working order
        if order_id in state.working_orders:
            order = state.working_orders[order_id]
            order.filled_sz += event.size
            order.last_state_change_ts = ts

            # Check if fully filled
            if order.filled_sz >= order.order_spec.sz:
                order.status = OrderStatus.FILLED

                # Clear tracking
                if state.bid_order_id == order_id:
                    state.bid_order_id = None
                if state.ask_order_id == order_id:
                    state.ask_order_id = None

                # NOTE: Don't call _reconcile_after_order_change() here!
                # After a fill, inventory changed, so last_intent is stale.
                # Wait for strategy to tick with fresh inventory and produce
                # a new intent that reflects the position change.
            else:
                # Partial fill - check if remaining size is below minimum
                remaining_sz = order.order_spec.sz - order.filled_sz
                MIN_ORDER_SIZE = 5  # Polymarket minimum
                if 0 < remaining_sz < MIN_ORDER_SIZE:
                    logger.info(
                        f"[FILL] Partial fill left {remaining_sz} remaining (< {MIN_ORDER_SIZE}), "
                        f"cancelling rest of order {order_id[:20]}..."
                    )
                    self._submit_cancel(order_id, ts)

        # Record fill for PnL tracking
        realized_pnl, avg_cost = state.pnl.record_fill(
            ts=ts,
            token=event.token,
            side=event.side,
            size=event.size,
            price=event.price,
            order_id=order_id,
        )

        logger.info(
            f"[FILL_DEBUG] Inventory AFTER: YES={state.inventory.I_yes} NO={state.inventory.I_no} net_E={state.inventory.net_E}"
        )

        # Log fill with PnL info
        side_str = "BUY" if event.side == Side.BUY else "SELL"
        fill_value_usd = (event.size * event.price) / 100
        logger.info(
            f"💰 FILL: {side_str} {event.size}x{event.token.name}@{event.price}c "
            f"(${fill_value_usd:.2f}) | PnL: realized={realized_pnl}c "
            f"total={state.pnl.realized_pnl_cents}c (${state.pnl.realized_pnl_cents/100:.2f})"
        )

        # Write to trade log file
        self._trade_logger.log_fill(
            ts=ts,
            token=event.token.name,
            side=side_str,
            size=event.size,
            price=event.price,
            order_id=order_id,
            realized_pnl=realized_pnl,
            inventory_yes=state.inventory.I_yes,
            inventory_no=state.inventory.I_no,
            net_position=state.inventory.net_E,
        )

    def _on_timer_tick(self, ts: int) -> None:
        """Handle periodic timer tick for maintenance."""
        self._check_timeouts(ts)
        self._check_staleness(ts)

    def _check_timeouts(self, ts: int) -> None:
        """Check for timed-out pending operations."""
        state = self._state
        cfg = self._config

        # Check place timeouts
        timed_out_places = []
        for action_id, pending in state.pending_places.items():
            if ts - pending.sent_at_ts > cfg.place_timeout_ms:
                timed_out_places.append(action_id)

        for action_id in timed_out_places:
            logger.warning(f"Place timed out: {action_id}")
            del state.pending_places[action_id]

        # Check cancel timeouts
        timed_out_cancels = []
        for action_id, pending in state.pending_cancels.items():
            if ts - pending.sent_at_ts > cfg.cancel_timeout_ms:
                timed_out_cancels.append(action_id)

        for action_id in timed_out_cancels:
            logger.warning(f"Cancel timed out: {action_id}")
            del state.pending_cancels[action_id]

    def _check_staleness(self, ts: int) -> None:
        """Check for stale data conditions."""
        # This would check PM/BN data staleness
        # For now, just a placeholder
        pass

    # -------------------------------------------------------------------------
    # PUBLIC ACCESSORS
    # -------------------------------------------------------------------------

    @property
    def state(self) -> ExecutorState:
        """Get current state (read-only access)."""
        return self._state

    @property
    def inventory(self) -> InventoryState:
        """Get current inventory."""
        return self._state.inventory

    @property
    def is_running(self) -> bool:
        """Check if executor is running."""
        return self._thread is not None and self._thread.is_alive()

    def set_inventory(self, yes: int, no: int) -> None:
        """Set inventory state (for initialization)."""
        self._state.inventory.I_yes = yes
        self._state.inventory.I_no = no
        self._state.inventory.last_update_ts = now_ms()

    def sync_open_orders(
        self,
        orders: list[dict],
        yes_token_id: str,
        no_token_id: str,
    ) -> int:
        """
        Sync existing open orders from exchange into executor state.

        Call this at startup BEFORE starting the executor thread.
        This prevents duplicate orders after crashes/restarts.

        Args:
            orders: List of open orders from REST API (Polymarket format)
            yes_token_id: YES token ID
            no_token_id: NO token ID

        Returns:
            Number of orders synced
        """
        ts = now_ms()
        synced = 0

        for order in orders:
            try:
                # Parse order data
                order_id = order.get("id") or order.get("order_id", "")
                asset_id = order.get("asset_id", "")
                side_str = order.get("side", "").upper()
                price_str = order.get("price", "0")
                size_str = order.get("original_size") or order.get("size", "0")
                filled_str = order.get("size_matched", "0")
                status_str = order.get("status", "").upper()

                # Skip if no order ID or not active
                if not order_id:
                    continue
                if status_str not in ("LIVE", "OPEN", ""):
                    continue

                # Determine token type
                if asset_id == yes_token_id:
                    token = Token.YES
                elif asset_id == no_token_id:
                    token = Token.NO
                else:
                    logger.debug(f"Skipping order with unknown asset: {asset_id[:20]}...")
                    continue

                # Parse side
                side = Side.BUY if side_str == "BUY" else Side.SELL

                # Parse price/size
                price = int(float(price_str) * 100)  # Convert to cents
                size = int(float(size_str))
                filled = int(float(filled_str)) if filled_str else 0

                # Create RealOrderSpec
                spec = RealOrderSpec(
                    token=token,
                    token_id=asset_id,
                    side=side,
                    px=price,
                    sz=size,
                )

                # Create WorkingOrder
                working_order = WorkingOrder(
                    client_order_id=f"synced_{order_id}",
                    server_order_id=order_id,
                    order_spec=spec,
                    status=OrderStatus.WORKING,
                    created_ts=ts,
                    last_state_change_ts=ts,
                    filled_sz=filled,
                )

                # Add to state
                self._state.working_orders[order_id] = working_order

                # Classify as bid or ask based on token/side
                # Bid: BUY YES or SELL NO (at complement)
                # Ask: SELL YES or BUY NO (at complement)
                is_bid = (token == Token.YES and side == Side.BUY) or \
                         (token == Token.NO and side == Side.SELL)

                if is_bid and self._state.bid_order_id is None:
                    self._state.bid_order_id = order_id
                elif not is_bid and self._state.ask_order_id is None:
                    self._state.ask_order_id = order_id

                logger.info(
                    f"Synced order: {order_id[:20]}... {token.name} {side.name} "
                    f"{size}@{price}c ({'bid' if is_bid else 'ask'})"
                )
                synced += 1

            except Exception as e:
                logger.warning(f"Failed to sync order: {e}")

        return synced
