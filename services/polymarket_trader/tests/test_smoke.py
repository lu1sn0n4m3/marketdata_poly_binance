"""Smoke tests for Container B wiring."""

import asyncio
import sys
sys.path.insert(0, "/Users/luissafar/dev/marketdata_poly_binance")
sys.path.insert(0, "/Users/luissafar/dev/marketdata_poly_binance/services/polymarket_trader/src")

from polymarket_trader.config import BConfig
from polymarket_trader.types import (
    CanonicalState, RiskMode, SessionState, Side, OrderPurpose,
    DesiredOrder, StrategyIntent, DecisionContext, CanonicalStateView,
    LimitState, HealthState, PositionState, MarketView, BinanceSnapshot,
)
from polymarket_trader.risk import RiskEngine
from polymarket_trader.strategy import OpportunisticQuoteStrategy
from polymarket_trader.order_manager import OrderManager, DesiredOrders, WorkingOrders
from polymarket_trader.quote_composer import QuoteComposer


def test_config_from_env():
    """Test BConfig loads correctly."""
    config = BConfig.from_env()
    assert config.poll_snapshot_hz == 50
    assert config.control_port == 9000
    config.validate()
    print("✓ BConfig loads and validates")


def test_canonical_state_creation():
    """Test CanonicalState creation and view."""
    state = CanonicalState()
    
    assert state.session_state == SessionState.BOOT_SYNC
    assert state.risk_mode == RiskMode.HALT
    assert len(state.open_orders) == 0
    
    # Get view
    view = state.view()
    assert view.session_state == SessionState.BOOT_SYNC
    print("✓ CanonicalState creation and view")


def test_risk_engine_evaluation():
    """Test RiskEngine evaluates context."""
    limits = LimitState(
        max_reserved_capital=1000.0,
        max_position_size=500.0,
        max_order_size=100.0,
        max_open_orders=4,
    )
    
    session_timers = {
        "T_wind_ms": 10 * 60 * 1000,
        "T_flatten_ms": 5 * 60 * 1000,
        "hard_cutoff_ms": 60 * 1000,
    }
    
    risk = RiskEngine(
        limits=limits,
        session_timers=session_timers,
        stale_threshold_ms=1000,
    )
    
    # Create a context with healthy state
    import time
    now_ms = int(time.time() * 1000)
    
    state_view = CanonicalStateView(
        session_state=SessionState.ACTIVE,
        risk_mode=RiskMode.NORMAL,
        active_market=None,
        token_ids=None,
        positions=PositionState(),
        open_orders={},
        market_view=MarketView(market_id="test", best_bid=0.5, best_ask=0.51),
        health=HealthState(
            market_ws_connected=True,
            user_ws_connected=True,
            rest_healthy=True,
            binance_snapshot_stale=False,
        ),
        limits=limits,
        latest_snapshot=None,
    )
    
    # Create a non-stale snapshot
    snapshot = BinanceSnapshot(
        seq=1,
        ts_local_ms=now_ms,
        ts_exchange_ms=now_ms - 10,
        age_ms=50,
        stale=False,
        hour_id="2026-01-22T14:00:00Z",
        hour_start_ts_ms=now_ms - 30 * 60 * 1000,  # 30 min ago
        hour_end_ts_ms=now_ms + 30 * 60 * 1000,  # 30 min from now
        t_remaining_ms=30 * 60 * 1000,
        open_price=50000.0,
        last_trade_price=50100.0,
        bbo_bid=50050.0,
        bbo_ask=50150.0,
        mid=50100.0,
        features={},
        p_yes_fair=0.55,
    )
    
    ctx = DecisionContext(
        state_view=state_view,
        snapshot=snapshot,
        now_ms=now_ms,
        t_remaining_ms=30 * 60 * 1000,
    )
    
    decision = risk.evaluate(ctx)
    
    assert decision.allowed_mode == RiskMode.NORMAL
    assert decision.can_quote == True
    print(f"✓ RiskEngine evaluation: mode={decision.allowed_mode.name}, can_quote={decision.can_quote}")


def test_strategy_on_tick():
    """Test OpportunisticQuoteStrategy generates intent."""
    from polymarket_trader.types import TokenIds
    
    strategy = OpportunisticQuoteStrategy(
        min_edge_ticks=2,
        base_spread_ticks=3,
        base_size=10.0,
    )
    
    assert strategy.name == "OpportunisticQuote"
    assert strategy.enabled
    
    # Create context with fair price and market
    import time
    now_ms = int(time.time() * 1000)
    
    limits = LimitState()
    
    state_view = CanonicalStateView(
        session_state=SessionState.ACTIVE,
        risk_mode=RiskMode.NORMAL,
        active_market=None,
        token_ids=TokenIds(yes_token_id="token123", no_token_id="token456"),
        positions=PositionState(),
        open_orders={},
        market_view=MarketView(
            market_id="test",
            tick_size=0.01,
            best_bid=0.48,  # Fair is 0.55, so edge exists
            best_ask=0.52,
        ),
        health=HealthState(market_ws_connected=True, user_ws_connected=True),
        limits=limits,
        latest_snapshot=None,
    )
    
    snapshot = BinanceSnapshot(
        seq=1,
        ts_local_ms=now_ms,
        ts_exchange_ms=now_ms - 10,
        age_ms=50,
        stale=False,
        hour_id="2026-01-22T14:00:00Z",
        hour_start_ts_ms=now_ms - 30 * 60 * 1000,
        hour_end_ts_ms=now_ms + 30 * 60 * 1000,
        t_remaining_ms=30 * 60 * 1000,
        open_price=50000.0,
        last_trade_price=50100.0,
        bbo_bid=50050.0,
        bbo_ask=50150.0,
        mid=50100.0,
        features={},
        p_yes_fair=0.55,  # Fair is 0.55
    )
    
    ctx = DecisionContext(
        state_view=state_view,
        snapshot=snapshot,
        now_ms=now_ms,
        t_remaining_ms=30 * 60 * 1000,
    )
    
    intent = strategy.on_tick(ctx)
    
    # Should have quotes since there's edge
    print(f"✓ Strategy intent: bid={intent.quotes.bid is not None}, ask={intent.quotes.ask is not None}")


def test_order_manager_reconcile():
    """Test OrderManager reconciliation."""
    manager = OrderManager(
        max_working_orders=4,
        replace_min_ticks=2,
        replace_min_age_ms=500,
    )
    
    import time
    now_ms = int(time.time() * 1000)
    
    # Create context
    limits = LimitState()
    state_view = CanonicalStateView(
        session_state=SessionState.ACTIVE,
        risk_mode=RiskMode.NORMAL,
        active_market=None,
        token_ids=None,
        positions=PositionState(),
        open_orders={},
        market_view=MarketView(market_id="test", tick_size=0.01),
        health=HealthState(),
        limits=limits,
        latest_snapshot=None,
    )
    
    ctx = DecisionContext(
        state_view=state_view,
        snapshot=None,
        now_ms=now_ms,
        t_remaining_ms=30 * 60 * 1000,
    )
    
    # Create desired orders
    desired = DesiredOrders()
    desired.bids.append(DesiredOrder(
        side=Side.BUY,
        price=0.50,
        size=10.0,
        purpose=OrderPurpose.QUOTE,
        expires_at_ms=now_ms + 5000,
        token_id="token123",
    ))
    
    # Empty working orders
    working = WorkingOrders()
    
    actions = manager.reconcile(desired, working, ctx)
    
    # Should have one PLACE action
    from polymarket_trader.types import OrderActionType
    
    assert len(actions) == 1
    assert actions[0].action_type == OrderActionType.PLACE
    print(f"✓ OrderManager reconciliation: {len(actions)} actions")


def test_quote_composer():
    """Test QuoteComposer creates quotes."""
    composer = QuoteComposer(
        min_spread_ticks=2,
        replace_min_ticks=2,
    )
    
    import time
    now_ms = int(time.time() * 1000)
    
    limits = LimitState()
    state_view = CanonicalStateView(
        session_state=SessionState.ACTIVE,
        risk_mode=RiskMode.NORMAL,
        active_market=None,
        token_ids=None,
        positions=PositionState(),
        open_orders={},
        market_view=MarketView(market_id="test", tick_size=0.01),
        health=HealthState(),
        limits=limits,
        latest_snapshot=None,
    )
    
    ctx = DecisionContext(
        state_view=state_view,
        snapshot=None,
        now_ms=now_ms,
        t_remaining_ms=30 * 60 * 1000,
    )
    
    quotes = composer.compose(
        fair=0.50,
        ctx=ctx,
        size=10.0,
        token_id="token123",
    )
    
    assert quotes.bid is not None
    assert quotes.ask is not None
    assert quotes.bid.price < 0.50
    assert quotes.ask.price > 0.50
    print(f"✓ QuoteComposer: bid={quotes.bid.price}, ask={quotes.ask.price}")


def test_risk_clamp_intent():
    """Test RiskEngine clamps strategy intent."""
    from polymarket_trader.types import TokenIds
    
    limits = LimitState(
        max_order_size=50.0,  # Smaller than intent
        max_open_orders=10,
    )
    
    risk = RiskEngine(
        limits=limits,
        session_timers={
            "T_wind_ms": 10 * 60 * 1000,
            "T_flatten_ms": 5 * 60 * 1000,
            "hard_cutoff_ms": 60 * 1000,
        },
        stale_threshold_ms=1000,
    )
    
    import time
    now_ms = int(time.time() * 1000)
    
    state_view = CanonicalStateView(
        session_state=SessionState.ACTIVE,
        risk_mode=RiskMode.NORMAL,
        active_market=None,
        token_ids=TokenIds(yes_token_id="token123", no_token_id="token456"),
        positions=PositionState(),
        open_orders={},
        market_view=MarketView(market_id="test"),
        health=HealthState(
            market_ws_connected=True,
            user_ws_connected=True,
            rest_healthy=True,
            binance_snapshot_stale=False,
        ),
        limits=limits,
        latest_snapshot=None,
    )
    
    # Create intent with large size
    intent = StrategyIntent()
    intent.quotes.bid = DesiredOrder(
        side=Side.BUY,
        price=0.50,
        size=100.0,  # Larger than limit
        purpose=OrderPurpose.QUOTE,
        expires_at_ms=now_ms + 5000,
        token_id="token123",
    )
    
    from polymarket_trader.types import RiskDecision
    risk_decision = RiskDecision(
        allowed_mode=RiskMode.NORMAL,
        max_new_exposure=100.0,
        can_quote=True,
        can_take=True,
        target_inventory=None,
    )
    
    clamped = risk.clamp_intent(intent, risk_decision, state_view)
    
    assert clamped.quotes.bid is not None
    assert clamped.quotes.bid.size == 50.0  # Clamped to limit
    print(f"✓ Risk clamps intent: size={clamped.quotes.bid.size}")


if __name__ == "__main__":
    print("\n=== Container B Smoke Tests ===\n")
    test_config_from_env()
    test_canonical_state_creation()
    test_risk_engine_evaluation()
    test_strategy_on_tick()
    test_order_manager_reconcile()
    test_quote_composer()
    test_risk_clamp_intent()
    print("\n=== All Container B Smoke Tests Passed ===\n")
