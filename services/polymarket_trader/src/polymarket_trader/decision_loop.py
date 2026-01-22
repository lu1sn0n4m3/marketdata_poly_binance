"""Decision loop for Container B - fixed-frequency tick loop."""

import asyncio
import logging
from time import time_ns
from typing import Optional, Callable

from .types import (
    CanonicalState, CanonicalStateView, DecisionContext, BinanceSnapshot,
    StrategyIntent, Side,
)
from .risk import RiskEngine
from .strategy import Strategy
from .order_manager import OrderManager, DesiredOrders, WorkingOrders
from .executor import Executor

try:
    from shared.hourmm_common.time import ms_until_hour_end
except ImportError:
    import sys
    sys.path.insert(0, "/Users/luissafar/dev/marketdata_poly_binance")
    from shared.hourmm_common.time import ms_until_hour_end

logger = logging.getLogger(__name__)


class DecisionLoop:
    """
    Fixed-frequency tick loop for trading decisions.
    
    On each tick:
    1. Gets latest state view (immutable copy)
    2. Gets latest Binance snapshot
    3. Builds decision context
    4. Runs strategy
    5. Applies risk constraints
    6. Reconciles orders
    7. Submits actions
    """
    
    def __init__(
        self,
        tick_hz: int,
        state_provider: Callable[[], CanonicalStateView],
        snapshot_provider: Callable[[], Optional[BinanceSnapshot]],
        risk: RiskEngine,
        strategy: Strategy,
        order_manager: OrderManager,
        executor: Executor,
    ):
        """
        Initialize the decision loop.
        
        Args:
            tick_hz: Tick rate in Hz
            state_provider: Function to get current state view
            snapshot_provider: Function to get latest Binance snapshot
            risk: Risk engine
            strategy: Trading strategy
            order_manager: Order manager
            executor: Order executor
        """
        self.tick_hz = tick_hz
        self._state_provider = state_provider
        self._snapshot_provider = snapshot_provider
        self.risk = risk
        self.strategy = strategy
        self.order_manager = order_manager
        self.executor = executor
        
        self._running = False
        self._tick_count = 0
        self._last_tick_ms = 0
    
    async def run(self, shutdown_event: Optional[asyncio.Event] = None) -> None:
        """
        Main loop running at fixed frequency.
        """
        self._running = True
        interval_seconds = 1.0 / self.tick_hz
        
        logger.info(f"Decision loop started at {self.tick_hz} Hz")
        
        while self._running:
            if shutdown_event and shutdown_event.is_set():
                break
            
            loop_start = time_ns()
            now_ms = loop_start // 1_000_000
            
            try:
                # Get state and snapshot
                state_view = self._state_provider()
                snapshot = self._snapshot_provider()
                
                # Build context
                ctx = self.build_context(state_view, snapshot, now_ms)
                
                # Execute decision step
                await self.step(ctx)
                
                self._tick_count += 1
                self._last_tick_ms = now_ms
            
            except Exception as e:
                logger.error(f"Decision loop error: {e}", exc_info=True)
            
            # Sleep to maintain rate
            elapsed_seconds = (time_ns() - loop_start) / 1_000_000_000
            sleep_seconds = max(0, interval_seconds - elapsed_seconds)
            
            if sleep_seconds > 0:
                await asyncio.sleep(sleep_seconds)
            else:
                # Loop lag
                lag_ms = int((elapsed_seconds - interval_seconds) * 1000)
                if lag_ms > 50:  # Log significant lag
                    logger.warning(f"Decision loop lag: {lag_ms}ms")
        
        logger.info("Decision loop stopped")
    
    def build_context(
        self,
        state_view: CanonicalStateView,
        snapshot: Optional[BinanceSnapshot],
        now_ms: int,
    ) -> DecisionContext:
        """
        Build decision context from current state and snapshot.
        
        Args:
            state_view: Current state view
            snapshot: Latest Binance snapshot
            now_ms: Current timestamp
        
        Returns:
            DecisionContext
        """
        t_remaining_ms = ms_until_hour_end(now_ms)
        
        return DecisionContext(
            state_view=state_view,
            snapshot=snapshot,
            now_ms=now_ms,
            t_remaining_ms=t_remaining_ms,
        )
    
    async def step(self, ctx: DecisionContext) -> None:
        """
        Execute one decision step.
        
        Args:
            ctx: DecisionContext
        """
        state_view = ctx.state_view
        
        # Evaluate risk
        risk_decision = self.risk.evaluate(ctx)
        
        # Run strategy
        intent = self.strategy.on_tick(ctx)
        
        # Clamp intent by risk
        clamped_intent = self.risk.clamp_intent(intent, risk_decision, state_view)
        
        # Handle cancel-all
        if clamped_intent.cancel_all:
            if state_view.active_market:
                await self.executor.cancel_all(state_view.active_market.condition_id)
            return
        
        # Build desired orders
        desired = self._intent_to_desired(clamped_intent, ctx)
        
        # Build working orders from state
        working = self._state_to_working(state_view)
        
        # Reconcile
        actions = self.order_manager.reconcile(desired, working, ctx)
        
        # Submit actions
        if actions:
            await self.executor.submit(actions)
    
    def _intent_to_desired(
        self,
        intent: StrategyIntent,
        ctx: DecisionContext,
    ) -> DesiredOrders:
        """Convert strategy intent to desired orders."""
        desired = DesiredOrders()
        
        if intent.quotes.bid:
            desired.bids.append(intent.quotes.bid)
        
        if intent.quotes.ask:
            desired.asks.append(intent.quotes.ask)
        
        # Add taker actions as well
        for take in intent.take_actions:
            if take.side == Side.BUY:
                desired.bids.append(take)
            else:
                desired.asks.append(take)
        
        return desired
    
    def _state_to_working(self, state_view: CanonicalStateView) -> WorkingOrders:
        """Extract working orders from state."""
        working = WorkingOrders()
        
        for order in state_view.open_orders.values():
            if order.side == Side.BUY:
                working.bids.append(order)
            else:
                working.asks.append(order)
        
        return working
    
    def emit_decision_metrics(self, ctx: DecisionContext) -> None:
        """Emit metrics for observability (placeholder)."""
        # TODO: Add metrics emission
        pass
    
    def stop(self) -> None:
        """Stop the decision loop."""
        self._running = False
