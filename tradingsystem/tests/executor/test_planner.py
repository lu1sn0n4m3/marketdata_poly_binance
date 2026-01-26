"""
Unit tests for the executor planner.

These tests validate the core planning logic:
- Reduce-first priority
- Split orders (SELL + BUY when needed)
- Coupled min-size decisions
- Policy application

Key test scenarios from the design spec:
1. "Have 2 YES, target SELL YES 5, min 5" - both sub-min, PASSIVE_FIRST
2. "Have 7 YES, target SELL YES 5" - simple reduce sell
3. "Have 3 NO, target BUY YES 8" - split with one legal leg
"""

import pytest
from dataclasses import dataclass

from tradingsystem.executor.planner import (
    plan_execution,
    OrderKind,
    PlannedOrder,
    LegPlan,
    ExecutionPlan,
)
from tradingsystem.executor.policies import MinSizePolicy
from tradingsystem.executor.state import ReservationLedger
from tradingsystem.types import Token, Side, DesiredQuoteLeg, DesiredQuoteSet, QuoteMode


@dataclass
class MockInventory:
    """Mock inventory for testing."""
    I_yes: int = 0
    I_no: int = 0


def make_intent(
    bid_enabled: bool = False,
    bid_px: int = 50,
    bid_sz: int = 5,
    ask_enabled: bool = False,
    ask_px: int = 50,
    ask_sz: int = 5,
) -> DesiredQuoteSet:
    """Create a test intent."""
    return DesiredQuoteSet(
        created_at_ts=0,
        pm_seq=0,
        bn_seq=0,
        mode=QuoteMode.NORMAL,
        bid_yes=DesiredQuoteLeg(enabled=bid_enabled, px_yes=bid_px, sz=bid_sz),
        ask_yes=DesiredQuoteLeg(enabled=ask_enabled, px_yes=ask_px, sz=ask_sz),
    )


class TestPlannerBasics:
    """Basic planner tests."""

    def test_disabled_legs_produce_empty_plan(self):
        """Disabled legs should produce empty plans."""
        intent = make_intent(bid_enabled=False, ask_enabled=False)
        inventory = MockInventory()
        reservations = ReservationLedger()

        plan = plan_execution(
            intent=intent,
            inventory=inventory,
            reservations=reservations,
            yes_token_id="YES",
            no_token_id="NO",
        )

        assert plan.bid.is_empty
        assert plan.ask.is_empty

    def test_simple_buy_yes_bid(self):
        """Simple bid with no inventory -> BUY YES."""
        intent = make_intent(bid_enabled=True, bid_px=45, bid_sz=10)
        inventory = MockInventory(I_yes=0, I_no=0)
        reservations = ReservationLedger()

        plan = plan_execution(
            intent=intent,
            inventory=inventory,
            reservations=reservations,
            yes_token_id="YES",
            no_token_id="NO",
        )

        assert len(plan.bid.orders) == 1
        order = plan.bid.orders[0]
        assert order.kind == OrderKind.OPEN_BUY  # BUY YES = OPEN_BUY
        assert order.token == Token.YES
        assert order.side == Side.BUY
        assert order.px == 45
        assert order.sz == 10

    def test_simple_sell_yes_ask(self):
        """Simple ask with YES inventory -> SELL YES."""
        intent = make_intent(ask_enabled=True, ask_px=55, ask_sz=10)
        inventory = MockInventory(I_yes=20, I_no=0)
        reservations = ReservationLedger()

        plan = plan_execution(
            intent=intent,
            inventory=inventory,
            reservations=reservations,
            yes_token_id="YES",
            no_token_id="NO",
        )

        assert len(plan.ask.orders) == 1
        order = plan.ask.orders[0]
        assert order.kind == OrderKind.REDUCE_SELL
        assert order.token == Token.YES
        assert order.side == Side.SELL
        assert order.px == 55
        assert order.sz == 10


class TestReduceFirst:
    """Tests for reduce-first priority."""

    def test_bid_with_no_inventory_sells_no_first(self):
        """Bid with NO inventory -> SELL NO (reduce-first)."""
        intent = make_intent(bid_enabled=True, bid_px=45, bid_sz=10)
        inventory = MockInventory(I_yes=0, I_no=15)
        reservations = ReservationLedger()

        plan = plan_execution(
            intent=intent,
            inventory=inventory,
            reservations=reservations,
            yes_token_id="YES",
            no_token_id="NO",
        )

        assert len(plan.bid.orders) == 1
        order = plan.bid.orders[0]
        assert order.kind == OrderKind.REDUCE_SELL
        assert order.token == Token.NO
        assert order.side == Side.SELL
        assert order.px == 55  # Complement of 45
        assert order.sz == 10

    def test_ask_with_yes_inventory_sells_yes_first(self):
        """Ask with YES inventory -> SELL YES (reduce-first)."""
        intent = make_intent(ask_enabled=True, ask_px=55, ask_sz=10)
        inventory = MockInventory(I_yes=15, I_no=0)
        reservations = ReservationLedger()

        plan = plan_execution(
            intent=intent,
            inventory=inventory,
            reservations=reservations,
            yes_token_id="YES",
            no_token_id="NO",
        )

        assert len(plan.ask.orders) == 1
        order = plan.ask.orders[0]
        assert order.kind == OrderKind.REDUCE_SELL
        assert order.token == Token.YES
        assert order.side == Side.SELL
        assert order.px == 55
        assert order.sz == 10


class TestSplitOrders:
    """Tests for split order scenarios."""

    def test_split_ask_sell_yes_plus_buy_no(self):
        """Ask with partial YES -> SELL YES + BUY NO (split)."""
        # Use larger sizes so both legs are >= min_size (5)
        intent = make_intent(ask_enabled=True, ask_px=55, ask_sz=12)
        inventory = MockInventory(I_yes=7, I_no=0)  # Only 7 YES, need 12
        reservations = ReservationLedger()

        plan = plan_execution(
            intent=intent,
            inventory=inventory,
            reservations=reservations,
            yes_token_id="YES",
            no_token_id="NO",
            min_size=5,
        )

        # Should have 2 orders: SELL YES 7 + BUY NO 5 (both >= min_size)
        assert len(plan.ask.orders) == 2

        # Find reduce and complement orders
        reduce_order = next(o for o in plan.ask.orders if o.kind == OrderKind.REDUCE_SELL)
        complement_order = next(o for o in plan.ask.orders if o.kind == OrderKind.COMPLEMENT_BUY)

        assert reduce_order.token == Token.YES
        assert reduce_order.side == Side.SELL
        assert reduce_order.sz == 7

        assert complement_order.token == Token.NO
        assert complement_order.side == Side.BUY
        assert complement_order.sz == 5  # 12 - 7 = 5 (exactly min_size)
        assert complement_order.px == 45  # Complement of 55

    def test_split_bid_sell_no_plus_buy_yes(self):
        """Bid with partial NO -> SELL NO + BUY YES (split)."""
        intent = make_intent(bid_enabled=True, bid_px=45, bid_sz=12)
        inventory = MockInventory(I_yes=0, I_no=7)  # Only 7 NO, need 12
        reservations = ReservationLedger()

        plan = plan_execution(
            intent=intent,
            inventory=inventory,
            reservations=reservations,
            yes_token_id="YES",
            no_token_id="NO",
        )

        # Should have 2 orders: SELL NO 7 + BUY YES 5
        assert len(plan.bid.orders) == 2

        reduce_order = next(o for o in plan.bid.orders if o.kind == OrderKind.REDUCE_SELL)
        open_order = next(o for o in plan.bid.orders if o.kind == OrderKind.OPEN_BUY)

        assert reduce_order.token == Token.NO
        assert reduce_order.side == Side.SELL
        assert reduce_order.sz == 7
        assert reduce_order.px == 55  # Complement of 45

        # BUY YES = OPEN_BUY (opening primary exposure)
        assert open_order.token == Token.YES
        assert open_order.side == Side.BUY
        assert open_order.sz == 5


class TestMinSizeCoupledDecisions:
    """Tests for coupled min-size decisions (the hard cases)."""

    def test_trace1_have_2_target_5_passive_first(self):
        """
        Trace 1: Have 2 YES, target SELL YES 5, min 5
        Split would be: SELL YES 2 + BUY NO 3 (both sub-min)
        PASSIVE_FIRST: Place nothing, residual = 5
        """
        intent = make_intent(ask_enabled=True, ask_px=55, ask_sz=5)
        inventory = MockInventory(I_yes=2, I_no=0)
        reservations = ReservationLedger()

        plan = plan_execution(
            intent=intent,
            inventory=inventory,
            reservations=reservations,
            yes_token_id="YES",
            no_token_id="NO",
            policy=MinSizePolicy.PASSIVE_FIRST,
            min_size=5,
        )

        # Both legs sub-min -> place nothing
        assert plan.ask.is_empty
        assert plan.ask.residual_size == 5
        assert plan.ask.policy_applied == MinSizePolicy.PASSIVE_FIRST

    def test_trace1_have_2_target_5_aggregate(self):
        """
        Trace 1 with AGGREGATE: Have 2 YES, target SELL YES 5, min 5
        Split would be: SELL YES 2 + BUY NO 3 (both sub-min)
        AGGREGATE: Round up to BUY NO 5, residual = 2 (dust)
        """
        intent = make_intent(ask_enabled=True, ask_px=55, ask_sz=5)
        inventory = MockInventory(I_yes=2, I_no=0)
        reservations = ReservationLedger()

        plan = plan_execution(
            intent=intent,
            inventory=inventory,
            reservations=reservations,
            yes_token_id="YES",
            no_token_id="NO",
            policy=MinSizePolicy.AGGREGATE,
            min_size=5,
        )

        # Aggregate: BUY NO 5 (rounded up from 3)
        assert len(plan.ask.orders) == 1
        order = plan.ask.orders[0]
        assert order.kind == OrderKind.COMPLEMENT_BUY
        assert order.token == Token.NO
        assert order.side == Side.BUY
        assert order.sz == 5
        assert plan.ask.aggregated_from == 5  # Original target
        assert plan.ask.residual_size == 2  # The 2 YES we couldn't sell

    def test_trace2_have_7_target_5(self):
        """
        Trace 2: Have 7 YES, target SELL YES 5
        No split needed, simple SELL YES 5
        """
        intent = make_intent(ask_enabled=True, ask_px=55, ask_sz=5)
        inventory = MockInventory(I_yes=7, I_no=0)
        reservations = ReservationLedger()

        plan = plan_execution(
            intent=intent,
            inventory=inventory,
            reservations=reservations,
            yes_token_id="YES",
            no_token_id="NO",
            min_size=5,
        )

        assert len(plan.ask.orders) == 1
        order = plan.ask.orders[0]
        assert order.kind == OrderKind.REDUCE_SELL
        assert order.token == Token.YES
        assert order.side == Side.SELL
        assert order.sz == 5
        assert plan.ask.residual_size == 0

    def test_trace3_have_3_no_target_8(self):
        """
        Trace 3: Have 3 NO, target BUY YES 8
        Split: SELL NO 3 (sub-min), BUY YES 5 (legal)
        Result: Only BUY YES 5, residual = 3
        """
        intent = make_intent(bid_enabled=True, bid_px=45, bid_sz=8)
        inventory = MockInventory(I_yes=0, I_no=3)
        reservations = ReservationLedger()

        plan = plan_execution(
            intent=intent,
            inventory=inventory,
            reservations=reservations,
            yes_token_id="YES",
            no_token_id="NO",
            min_size=5,
        )

        # Only open buy is legal (BUY YES = OPEN_BUY)
        assert len(plan.bid.orders) == 1
        order = plan.bid.orders[0]
        assert order.kind == OrderKind.OPEN_BUY  # BUY YES = OPEN_BUY
        assert order.token == Token.YES
        assert order.side == Side.BUY
        assert order.sz == 5  # 8 - 3 = 5
        assert plan.bid.residual_size == 3  # The 3 NO we couldn't sell

    def test_reduce_legal_complement_submin(self):
        """
        Have 7 NO, target BUY YES 10
        Split: SELL NO 7 (legal), BUY YES 3 (sub-min)
        Result: Only SELL NO 7, residual = 3
        """
        intent = make_intent(bid_enabled=True, bid_px=45, bid_sz=10)
        inventory = MockInventory(I_yes=0, I_no=7)
        reservations = ReservationLedger()

        plan = plan_execution(
            intent=intent,
            inventory=inventory,
            reservations=reservations,
            yes_token_id="YES",
            no_token_id="NO",
            min_size=5,
        )

        assert len(plan.bid.orders) == 1
        order = plan.bid.orders[0]
        assert order.kind == OrderKind.REDUCE_SELL
        assert order.token == Token.NO
        assert order.side == Side.SELL
        assert order.sz == 7
        assert plan.bid.residual_size == 3  # The 3 BUY YES we couldn't place


class TestReservations:
    """Tests for reservation-aware planning."""

    def test_available_inventory_respects_reservations(self):
        """Planning should use available (settled - reserved) inventory."""
        intent = make_intent(ask_enabled=True, ask_px=55, ask_sz=10)
        inventory = MockInventory(I_yes=15, I_no=0)
        # 10 YES already reserved
        reservations = ReservationLedger(reserved_yes=10)

        plan = plan_execution(
            intent=intent,
            inventory=inventory,
            reservations=reservations,
            yes_token_id="YES",
            no_token_id="NO",
            min_size=5,
        )

        # Available YES = 15 - 10 = 5
        # Split: SELL YES 5 + BUY NO 5
        assert len(plan.ask.orders) == 2

        reduce_order = next(o for o in plan.ask.orders if o.kind == OrderKind.REDUCE_SELL)
        assert reduce_order.sz == 5  # Only 5 available, not 10

    def test_safety_buffer_reduces_available(self):
        """Safety buffer should reduce available inventory."""
        intent = make_intent(ask_enabled=True, ask_px=55, ask_sz=10)
        inventory = MockInventory(I_yes=12, I_no=0)
        # Safety buffer of 5
        reservations = ReservationLedger(safety_buffer_yes=5)

        plan = plan_execution(
            intent=intent,
            inventory=inventory,
            reservations=reservations,
            yes_token_id="YES",
            no_token_id="NO",
            min_size=5,
        )

        # Available YES = 12 - 0 - 5 = 7
        # Split: SELL YES 7 + BUY NO 3 (3 is sub-min -> residual)
        assert len(plan.ask.orders) == 1

        order = plan.ask.orders[0]
        assert order.kind == OrderKind.REDUCE_SELL
        assert order.sz == 7
        assert plan.ask.residual_size == 3


class TestEdgeCases:
    """Edge case tests."""

    def test_zero_size_intent(self):
        """Zero size intent should produce empty plan."""
        intent = make_intent(ask_enabled=True, ask_px=55, ask_sz=0)
        inventory = MockInventory(I_yes=10, I_no=0)
        reservations = ReservationLedger()

        plan = plan_execution(
            intent=intent,
            inventory=inventory,
            reservations=reservations,
            yes_token_id="YES",
            no_token_id="NO",
        )

        # Zero size -> empty
        assert plan.ask.is_empty

    def test_submin_simple_buy_passive(self):
        """Sub-min simple BUY should be residual in PASSIVE_FIRST."""
        intent = make_intent(bid_enabled=True, bid_px=45, bid_sz=3)  # Sub-min
        inventory = MockInventory(I_yes=0, I_no=0)
        reservations = ReservationLedger()

        plan = plan_execution(
            intent=intent,
            inventory=inventory,
            reservations=reservations,
            yes_token_id="YES",
            no_token_id="NO",
            policy=MinSizePolicy.PASSIVE_FIRST,
            min_size=5,
        )

        assert plan.bid.is_empty
        assert plan.bid.residual_size == 3

    def test_submin_simple_buy_aggregate(self):
        """Sub-min simple BUY should be rounded up in AGGREGATE."""
        intent = make_intent(bid_enabled=True, bid_px=45, bid_sz=3)  # Sub-min
        inventory = MockInventory(I_yes=0, I_no=0)
        reservations = ReservationLedger()

        plan = plan_execution(
            intent=intent,
            inventory=inventory,
            reservations=reservations,
            yes_token_id="YES",
            no_token_id="NO",
            policy=MinSizePolicy.AGGREGATE,
            min_size=5,
        )

        assert len(plan.bid.orders) == 1
        order = plan.bid.orders[0]
        assert order.sz == 5  # Rounded up
        assert plan.bid.aggregated_from == 3

    def test_full_inventory_covers_target(self):
        """When inventory fully covers target, no split needed."""
        intent = make_intent(ask_enabled=True, ask_px=55, ask_sz=5)
        inventory = MockInventory(I_yes=100, I_no=0)  # Way more than needed
        reservations = ReservationLedger()

        plan = plan_execution(
            intent=intent,
            inventory=inventory,
            reservations=reservations,
            yes_token_id="YES",
            no_token_id="NO",
        )

        assert len(plan.ask.orders) == 1
        order = plan.ask.orders[0]
        assert order.kind == OrderKind.REDUCE_SELL
        assert order.sz == 5  # Target, not full inventory
