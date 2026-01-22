"""Risk engine for Container B."""

import logging
from typing import Optional

from .types import (
    RiskMode, SessionState, DecisionContext, RiskDecision,
    CanonicalStateView, LimitState, DesiredOrder, StrategyIntent,
)

logger = logging.getLogger(__name__)


class RiskEngine:
    """
    Hard limits and mode gating.
    
    Not abstract - this is the concrete risk implementation.
    Always enforces limits regardless of strategy.
    """
    
    def __init__(
        self,
        limits: LimitState,
        session_timers: dict,
        stale_threshold_ms: int = 1000,
    ):
        """
        Initialize the risk engine.
        
        Args:
            limits: Initial limit configuration
            session_timers: Dict with T_wind_ms, T_flatten_ms, hard_cutoff_ms
            stale_threshold_ms: Threshold for treating Binance data as stale
        """
        self.limits = limits
        self.session_timers = session_timers
        self.stale_threshold_ms = stale_threshold_ms
    
    def evaluate(self, ctx: DecisionContext) -> RiskDecision:
        """
        Evaluate current context and determine risk constraints.
        
        Args:
            ctx: DecisionContext with state and snapshot
        
        Returns:
            RiskDecision with allowed actions
        """
        state = ctx.state_view
        snapshot = ctx.snapshot
        t_remaining_ms = ctx.t_remaining_ms
        
        # Start with current limits
        limits = state.limits
        
        # Determine effective risk mode
        allowed_mode = self._determine_mode(state, snapshot, t_remaining_ms)
        
        # Compute max new exposure based on mode and time
        max_new_exposure = self._compute_max_new_exposure(
            state, allowed_mode, t_remaining_ms
        )
        
        # Determine if quoting/taking allowed
        can_quote = (
            allowed_mode in (RiskMode.NORMAL, RiskMode.REDUCE_ONLY) and
            limits.quoting_enabled and
            state.health.market_ws_connected and
            state.health.user_ws_connected
        )
        
        can_take = (
            allowed_mode == RiskMode.NORMAL and
            limits.taking_enabled and
            state.health.rest_healthy
        )
        
        # Compute target inventory based on time
        target_inventory = self.compute_target_inventory(t_remaining_ms)
        
        # Build reason string
        reason = self._build_reason(state, snapshot, allowed_mode)
        
        return RiskDecision(
            allowed_mode=allowed_mode,
            max_new_exposure=max_new_exposure,
            can_quote=can_quote,
            can_take=can_take,
            target_inventory=target_inventory,
            reason=reason,
        )
    
    def _determine_mode(
        self,
        state: CanonicalStateView,
        snapshot,
        t_remaining_ms: int,
    ) -> RiskMode:
        """Determine the effective risk mode."""
        # Start with state's mode
        mode = state.risk_mode
        
        # Escalate based on conditions
        if mode == RiskMode.HALT:
            return RiskMode.HALT
        
        # Check connectivity
        if not state.health.market_ws_connected or not state.health.user_ws_connected:
            return RiskMode.HALT
        
        # Check Binance staleness
        if snapshot and snapshot.stale:
            if mode == RiskMode.NORMAL:
                mode = RiskMode.REDUCE_ONLY
        
        if state.health.binance_snapshot_stale:
            if mode == RiskMode.NORMAL:
                mode = RiskMode.REDUCE_ONLY
        
        # Check REST health
        if not state.health.rest_healthy:
            return RiskMode.HALT
        
        # Check session phase
        T_flatten = self.session_timers.get("T_flatten_ms", 5 * 60 * 1000)
        T_wind = self.session_timers.get("T_wind_ms", 10 * 60 * 1000)
        hard_cutoff = self.session_timers.get("hard_cutoff_ms", 60 * 1000)
        
        if t_remaining_ms <= hard_cutoff:
            return RiskMode.HALT
        elif t_remaining_ms <= T_flatten:
            if mode in (RiskMode.NORMAL, RiskMode.REDUCE_ONLY):
                mode = RiskMode.FLATTEN
        elif t_remaining_ms <= T_wind:
            if mode == RiskMode.NORMAL:
                mode = RiskMode.REDUCE_ONLY
        
        return mode
    
    def _compute_max_new_exposure(
        self,
        state: CanonicalStateView,
        mode: RiskMode,
        t_remaining_ms: int,
    ) -> float:
        """Compute maximum new exposure allowed."""
        if mode in (RiskMode.HALT, RiskMode.FLATTEN):
            return 0.0
        
        limits = state.limits
        
        # Current exposure
        current_exposure = abs(state.positions.net_exposure)
        
        # Time-based scaling
        hour_ms = 60 * 60 * 1000
        time_factor = min(1.0, t_remaining_ms / hour_ms)
        
        # Available headroom
        max_position = limits.max_position_size * time_factor
        available = max(0.0, max_position - current_exposure)
        
        if mode == RiskMode.REDUCE_ONLY:
            # Only allow reducing exposure
            return 0.0
        
        return min(available, limits.max_order_size)
    
    def is_action_allowed(
        self,
        action: DesiredOrder,
        risk_decision: RiskDecision,
        state: CanonicalStateView,
    ) -> bool:
        """
        Check if a proposed action is allowed.
        
        Args:
            action: Proposed order action
            risk_decision: Current risk decision
            state: Current state view
        
        Returns:
            True if action is allowed
        """
        mode = risk_decision.allowed_mode
        limits = state.limits
        
        # HALT allows nothing
        if mode == RiskMode.HALT:
            return False
        
        # Check size limits
        if action.size > limits.max_order_size:
            return False
        
        # Check open order limit
        if len(state.open_orders) >= limits.max_open_orders:
            return False
        
        # REDUCE_ONLY: only allow actions that reduce exposure
        if mode == RiskMode.REDUCE_ONLY:
            # Would need to check if this reduces position
            # Simplified: allow sells if long, buys if short
            net_pos = state.positions.net_exposure
            from .types import Side
            if net_pos > 0 and action.side == Side.BUY:
                return False  # Can't increase long
            if net_pos < 0 and action.side == Side.SELL:
                return False  # Can't increase short
        
        # FLATTEN: only allow unwind actions
        if mode == RiskMode.FLATTEN:
            from .types import OrderPurpose
            if action.purpose != OrderPurpose.UNWIND:
                return False
        
        return True
    
    def clamp_intent(
        self,
        intent: StrategyIntent,
        risk_decision: RiskDecision,
        state: CanonicalStateView,
    ) -> StrategyIntent:
        """
        Clamp strategy intent to risk limits.
        
        Args:
            intent: Raw strategy intent
            risk_decision: Current risk decision
            state: Current state view
        
        Returns:
            Clamped intent
        """
        clamped = StrategyIntent()
        
        # If HALT, cancel all
        if risk_decision.allowed_mode == RiskMode.HALT:
            clamped.cancel_all = True
            return clamped
        
        # Clamp quotes first, then check if allowed
        if intent.quotes.bid:
            clamped_bid = self._clamp_order(intent.quotes.bid, state.limits)
            if self.is_action_allowed(clamped_bid, risk_decision, state):
                clamped.quotes.bid = clamped_bid
        
        if intent.quotes.ask:
            clamped_ask = self._clamp_order(intent.quotes.ask, state.limits)
            if self.is_action_allowed(clamped_ask, risk_decision, state):
                clamped.quotes.ask = clamped_ask
        
        # Clamp taker actions
        for take in intent.take_actions:
            clamped_take = self._clamp_order(take, state.limits)
            if self.is_action_allowed(clamped_take, risk_decision, state):
                clamped.take_actions.append(clamped_take)
        
        # Use risk-adjusted target inventory
        clamped.target_inventory = risk_decision.target_inventory
        
        return clamped
    
    def _clamp_order(self, order: DesiredOrder, limits: LimitState) -> DesiredOrder:
        """Clamp a single order to limits."""
        from .types import DesiredOrder
        return DesiredOrder(
            side=order.side,
            price=order.price,
            size=min(order.size, limits.max_order_size),
            purpose=order.purpose,
            expires_at_ms=order.expires_at_ms,
            token_id=order.token_id,
        )
    
    def compute_target_inventory(self, t_remaining_ms: int) -> float:
        """
        Compute time-dependent target inventory.
        
        Inventory target decays to zero as hour end approaches.
        
        Args:
            t_remaining_ms: Time remaining in hour
        
        Returns:
            Target inventory (0 at hour end)
        """
        hour_ms = 60 * 60 * 1000
        T_flatten = self.session_timers.get("T_flatten_ms", 5 * 60 * 1000)
        
        if t_remaining_ms <= T_flatten:
            return 0.0
        
        # Linear decay from max to 0
        fraction = (t_remaining_ms - T_flatten) / (hour_ms - T_flatten)
        return self.limits.max_position_size * max(0.0, fraction)
    
    def compute_reserved_capital(self, state: CanonicalStateView) -> float:
        """
        Compute conservative reserved capital.
        
        Args:
            state: Current state view
        
        Returns:
            Capital reserved (worst-case)
        """
        # Capital from positions
        pos_capital = (
            state.positions.yes_tokens +
            state.positions.no_tokens
        )  # Simplified - actual calc depends on prices
        
        # Capital from open orders (worst case)
        order_capital = sum(
            order.remaining * order.price
            for order in state.open_orders.values()
        )
        
        return pos_capital + order_capital
    
    def _build_reason(
        self,
        state: CanonicalStateView,
        snapshot,
        mode: RiskMode,
    ) -> str:
        """Build explanation for risk decision."""
        reasons = []
        
        if not state.health.market_ws_connected:
            reasons.append("market_ws_down")
        if not state.health.user_ws_connected:
            reasons.append("user_ws_down")
        if snapshot and snapshot.stale:
            reasons.append("binance_stale")
        if not state.health.rest_healthy:
            reasons.append("rest_unhealthy")
        
        return f"mode={mode.name} reasons=[{','.join(reasons) or 'none'}]"
