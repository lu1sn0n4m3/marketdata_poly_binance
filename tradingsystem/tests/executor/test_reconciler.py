"""
Tests for the queue-preserving reconciler.

Tests the replacement policy:
- Price change: always replace
- Size decrease: always replace (risk reduction)
- Size increase < threshold: do nothing (preserve queue)
- Size increase >= threshold: replace (material change)
"""

import pytest
from typing import Literal

from tradingsystem.executor.reconciler import reconcile_slot, _should_replace
from tradingsystem.executor.planner import OrderKind, PlannedOrder, LegPlan
from tradingsystem.executor.state import OrderSlot, SlotState
from tradingsystem.types import Token, Side, RealOrderSpec, WorkingOrder, OrderStatus


def make_working_order(
    token: Token,
    side: Side,
    px: int,
    sz: int,
    filled_sz: int = 0,
    order_id: str = "order_123",
    kind: str | None = None,
) -> WorkingOrder:
    """
    Create a working order for testing.

    If kind is not specified, derives it from (side, token):
    - SELL → "reduce_sell"
    - BUY YES → "open_buy"
    - BUY NO → "complement_buy"
    """
    if kind is None:
        if side == Side.SELL:
            kind = "reduce_sell"
        elif token == Token.YES:
            kind = "open_buy"
        else:
            kind = "complement_buy"

    spec = RealOrderSpec(
        token=token,
        token_id=f"{token.name}_token",
        side=side,
        px=px,
        sz=sz,
    )
    return WorkingOrder(
        client_order_id="client_123",
        server_order_id=order_id,
        order_spec=spec,
        status=OrderStatus.WORKING,
        created_ts=1000,
        last_state_change_ts=1000,
        filled_sz=filled_sz,
        kind=kind,
    )


def make_planned_order(
    token: Token,
    side: Side,
    px: int,
    sz: int,
    kind: OrderKind | None = None,
) -> PlannedOrder:
    """
    Create a planned order for testing.

    If kind is not specified, derives it from (side, token):
    - SELL → REDUCE_SELL (selling inventory to reduce exposure)
    - BUY YES → OPEN_BUY (opening primary exposure)
    - BUY NO → COMPLEMENT_BUY (using complement)

    This matches how the reconciler infers kind from working orders.
    """
    if kind is None:
        if side == Side.SELL:
            kind = OrderKind.REDUCE_SELL
        elif token == Token.YES:
            kind = OrderKind.OPEN_BUY  # BUY YES = opening primary exposure
        else:
            kind = OrderKind.COMPLEMENT_BUY  # BUY NO = complement buy
    return PlannedOrder(
        kind=kind,
        token=token,
        token_id=f"{token.name}_token",
        side=side,
        px=px,
        sz=sz,
    )


class TestShouldReplace:
    """Tests for the _should_replace function."""

    def test_exact_match_no_replace(self):
        """Exact match should not trigger replacement."""
        working = make_working_order(Token.YES, Side.BUY, px=45, sz=20)
        planned = make_planned_order(Token.YES, Side.BUY, px=45, sz=20)

        should, reason = _should_replace(working, planned, price_tol=0, top_up_threshold=10)

        assert should is False
        assert reason == "exact_match"

    def test_price_change_always_replace(self):
        """Price change should always trigger replacement."""
        working = make_working_order(Token.YES, Side.BUY, px=45, sz=20)
        planned = make_planned_order(Token.YES, Side.BUY, px=47, sz=20)

        should, reason = _should_replace(working, planned, price_tol=0, top_up_threshold=10)

        assert should is True
        assert "price_change" in reason

    def test_price_within_tolerance_no_replace(self):
        """Price within tolerance should not trigger replacement."""
        working = make_working_order(Token.YES, Side.BUY, px=45, sz=20)
        planned = make_planned_order(Token.YES, Side.BUY, px=46, sz=20)

        # With tolerance of 1, price change of 1 is OK
        should, reason = _should_replace(working, planned, price_tol=1, top_up_threshold=10)

        assert should is False
        assert reason == "exact_match"  # Size also matches

    def test_size_decrease_always_replace(self):
        """Size decrease should always trigger replacement (risk reduction)."""
        working = make_working_order(Token.YES, Side.BUY, px=45, sz=20)
        planned = make_planned_order(Token.YES, Side.BUY, px=45, sz=15)

        should, reason = _should_replace(working, planned, price_tol=0, top_up_threshold=10)

        assert should is True
        assert "size_decrease" in reason

    def test_size_increase_below_threshold_no_replace(self):
        """Size increase below threshold should NOT trigger replacement (queue preserve)."""
        working = make_working_order(Token.YES, Side.BUY, px=45, sz=20)
        planned = make_planned_order(Token.YES, Side.BUY, px=45, sz=25)  # +5

        # With threshold of 10, increase of 5 should NOT replace
        should, reason = _should_replace(working, planned, price_tol=0, top_up_threshold=10)

        assert should is False
        assert reason == "size_increase_below_threshold"

    def test_size_increase_at_threshold_replace(self):
        """Size increase at threshold should trigger replacement."""
        working = make_working_order(Token.YES, Side.BUY, px=45, sz=20)
        planned = make_planned_order(Token.YES, Side.BUY, px=45, sz=30)  # +10

        # With threshold of 10, increase of 10 should replace
        should, reason = _should_replace(working, planned, price_tol=0, top_up_threshold=10)

        assert should is True
        assert "size_increase" in reason
        assert ">=threshold" in reason

    def test_size_increase_above_threshold_replace(self):
        """Size increase above threshold should trigger replacement."""
        working = make_working_order(Token.YES, Side.BUY, px=45, sz=20)
        planned = make_planned_order(Token.YES, Side.BUY, px=45, sz=35)  # +15

        should, reason = _should_replace(working, planned, price_tol=0, top_up_threshold=10)

        assert should is True
        assert "size_increase" in reason

    def test_partial_fill_uses_remaining(self):
        """Partial fill should compare remaining_sz, not original sz."""
        # Order for 30, filled 5 -> remaining = 25
        working = make_working_order(Token.YES, Side.BUY, px=45, sz=30, filled_sz=5)
        # Strategy still wants 30 at same price
        planned = make_planned_order(Token.YES, Side.BUY, px=45, sz=30)

        # Remaining is 25, planned is 30 -> increase of 5 (below threshold of 10)
        should, reason = _should_replace(working, planned, price_tol=0, top_up_threshold=10)

        assert should is False  # Should NOT replace to preserve queue!
        assert reason == "size_increase_below_threshold"

    def test_token_mismatch_replace(self):
        """Token mismatch should trigger replacement."""
        working = make_working_order(Token.YES, Side.BUY, px=45, sz=20)
        planned = make_planned_order(Token.NO, Side.BUY, px=45, sz=20)

        should, reason = _should_replace(working, planned, price_tol=0, top_up_threshold=10)

        assert should is True
        assert "token_mismatch" in reason

    def test_side_mismatch_replace(self):
        """Side mismatch should trigger replacement."""
        working = make_working_order(Token.YES, Side.BUY, px=45, sz=20)
        planned = make_planned_order(Token.YES, Side.SELL, px=45, sz=20)

        should, reason = _should_replace(working, planned, price_tol=0, top_up_threshold=10)

        assert should is True
        assert "side_mismatch" in reason


class TestReconcileSlot:
    """Tests for the reconcile_slot function."""

    def make_slot(self, slot_type: Literal["bid", "ask"] = "bid") -> OrderSlot:
        """Create an empty order slot."""
        return OrderSlot(slot_type=slot_type)

    def test_empty_slot_empty_plan_no_effects(self):
        """Empty slot + empty plan = no effects."""
        slot = self.make_slot()
        plan = LegPlan(orders=[], residual_size=0, aggregated_from=0)

        effects = reconcile_slot(plan, slot)

        assert effects.is_empty
        assert len(effects.cancels) == 0
        assert len(effects.places) == 0

    def test_empty_slot_with_plan_places_order(self):
        """Empty slot with plan should place order."""
        slot = self.make_slot()
        planned = make_planned_order(Token.YES, Side.BUY, px=45, sz=20)
        plan = LegPlan(orders=[planned], residual_size=0, aggregated_from=0)

        effects = reconcile_slot(plan, slot)

        assert len(effects.cancels) == 0
        assert len(effects.places) == 1
        assert effects.places[0].spec.px == 45
        assert effects.places[0].spec.sz == 20

    def test_working_order_not_in_plan_canceled(self):
        """Working order not in plan should be canceled."""
        slot = self.make_slot()
        working = make_working_order(Token.YES, Side.BUY, px=45, sz=20)
        slot.orders[working.server_order_id] = working

        # Empty plan
        plan = LegPlan(orders=[], residual_size=0, aggregated_from=0)

        effects = reconcile_slot(plan, slot)

        assert len(effects.cancels) == 1
        assert effects.cancels[0].order_id == working.server_order_id
        assert len(effects.places) == 0

    def test_matching_order_no_effects(self):
        """Working order matching plan should produce no effects."""
        slot = self.make_slot()
        working = make_working_order(Token.YES, Side.BUY, px=45, sz=20)
        slot.orders[working.server_order_id] = working

        # BUY YES = OPEN_BUY (inferred automatically)
        planned = make_planned_order(Token.YES, Side.BUY, px=45, sz=20)
        plan = LegPlan(orders=[planned], residual_size=0, aggregated_from=0)

        effects = reconcile_slot(plan, slot, top_up_threshold=10)

        assert effects.is_empty

    def test_price_change_cancel_and_place(self):
        """Price change should cancel and place new order."""
        slot = self.make_slot()
        working = make_working_order(Token.YES, Side.BUY, px=45, sz=20)
        slot.orders[working.server_order_id] = working

        # BUY YES = OPEN_BUY (inferred automatically)
        planned = make_planned_order(Token.YES, Side.BUY, px=47, sz=20)  # Different price
        plan = LegPlan(orders=[planned], residual_size=0, aggregated_from=0)

        effects = reconcile_slot(plan, slot, top_up_threshold=10)

        assert len(effects.cancels) == 1
        assert len(effects.places) == 1
        assert effects.places[0].spec.px == 47

    def test_small_size_increase_no_effects(self):
        """Small size increase should NOT cancel/replace (queue preserve)."""
        slot = self.make_slot()
        working = make_working_order(Token.YES, Side.BUY, px=45, sz=20)
        slot.orders[working.server_order_id] = working

        # BUY YES = OPEN_BUY (inferred automatically)
        planned = make_planned_order(Token.YES, Side.BUY, px=45, sz=25)  # +5 (below threshold)
        plan = LegPlan(orders=[planned], residual_size=0, aggregated_from=0)

        effects = reconcile_slot(plan, slot, top_up_threshold=10)

        # Should have NO effects - order is left working
        assert effects.is_empty

    def test_large_size_increase_cancel_and_place(self):
        """Large size increase should cancel and place."""
        slot = self.make_slot()
        working = make_working_order(Token.YES, Side.BUY, px=45, sz=20)
        slot.orders[working.server_order_id] = working

        # BUY YES = OPEN_BUY (inferred automatically)
        planned = make_planned_order(Token.YES, Side.BUY, px=45, sz=35)  # +15 (above threshold)
        plan = LegPlan(orders=[planned], residual_size=0, aggregated_from=0)

        effects = reconcile_slot(plan, slot, top_up_threshold=10)

        assert len(effects.cancels) == 1
        assert len(effects.places) == 1
        assert effects.places[0].spec.sz == 35

    def test_busy_slot_no_effects(self):
        """Busy slot should produce no effects."""
        slot = self.make_slot()
        slot.state = SlotState.CANCELING  # Slot is busy

        planned = make_planned_order(Token.YES, Side.BUY, px=45, sz=20)
        plan = LegPlan(orders=[planned], residual_size=0, aggregated_from=0)

        effects = reconcile_slot(plan, slot)

        assert effects.is_empty

    def test_sell_overlap_rule_blocks_sell_place(self):
        """SELL overlap rule should block SELL place until cancel ack."""
        slot = self.make_slot("ask")
        working = make_working_order(
            Token.YES, Side.SELL, px=55, sz=20,
            order_id="sell_123",
        )
        slot.orders[working.server_order_id] = working

        # New SELL at different price
        planned = make_planned_order(
            Token.YES, Side.SELL, px=57, sz=20,
            kind=OrderKind.REDUCE_SELL,
        )
        plan = LegPlan(orders=[planned], residual_size=0, aggregated_from=0)

        effects = reconcile_slot(plan, slot, top_up_threshold=10)

        # Should have cancel but place is blocked
        assert len(effects.cancels) == 1
        assert len(effects.places) == 0  # SELL place blocked!
        assert effects.sell_blocked_until_cancel_ack is True


class TestMultiOrderPlans:
    """Tests for slots with 2 orders (split order scenarios)."""

    def make_slot(self, slot_type: Literal["bid", "ask"] = "bid") -> OrderSlot:
        """Create an empty order slot."""
        return OrderSlot(slot_type=slot_type)

    def test_two_working_two_matching_no_effects(self):
        """Slot has 2 working orders, plan has 2 matching → no effects."""
        slot = self.make_slot("ask")

        # Working orders: SELL YES + BUY NO (split order scenario)
        working_sell = make_working_order(
            Token.YES, Side.SELL, px=55, sz=10,
            order_id="sell_order",
        )
        working_buy = make_working_order(
            Token.NO, Side.BUY, px=45, sz=15,
            order_id="buy_order",
        )
        slot.orders[working_sell.server_order_id] = working_sell
        slot.orders[working_buy.server_order_id] = working_buy

        # Plan matches both exactly
        planned_sell = make_planned_order(
            Token.YES, Side.SELL, px=55, sz=10,
            kind=OrderKind.REDUCE_SELL,
        )
        planned_buy = make_planned_order(
            Token.NO, Side.BUY, px=45, sz=15,
            kind=OrderKind.COMPLEMENT_BUY,
        )
        plan = LegPlan(orders=[planned_sell, planned_buy], residual_size=0, aggregated_from=0)

        effects = reconcile_slot(plan, slot, top_up_threshold=10)

        assert effects.is_empty
        assert len(effects.cancels) == 0
        assert len(effects.places) == 0

    def test_two_working_one_planned_cancels_extra(self):
        """Slot has 2 working orders, plan has 1 → cancel the extra one."""
        slot = self.make_slot("ask")

        # Working orders: SELL YES + BUY NO
        working_sell = make_working_order(
            Token.YES, Side.SELL, px=55, sz=10,
            order_id="sell_order",
        )
        working_buy = make_working_order(
            Token.NO, Side.BUY, px=45, sz=15,
            order_id="buy_order",
        )
        slot.orders[working_sell.server_order_id] = working_sell
        slot.orders[working_buy.server_order_id] = working_buy

        # Plan only has SELL (inventory increased, don't need complement anymore)
        planned_sell = make_planned_order(
            Token.YES, Side.SELL, px=55, sz=10,
            kind=OrderKind.REDUCE_SELL,
        )
        plan = LegPlan(orders=[planned_sell], residual_size=0, aggregated_from=0)

        effects = reconcile_slot(plan, slot, top_up_threshold=10)

        # Should cancel the BUY NO (COMPLEMENT_BUY kind not in plan)
        assert len(effects.cancels) == 1
        assert effects.cancels[0].order_id == "buy_order"
        assert effects.cancels[0].kind == OrderKind.COMPLEMENT_BUY
        assert len(effects.places) == 0

    def test_one_working_two_planned_places_missing(self):
        """Slot has 1 working order, plan has 2 → place the missing one."""
        slot = self.make_slot("ask")

        # Only SELL YES is working
        working_sell = make_working_order(
            Token.YES, Side.SELL, px=55, sz=10,
            order_id="sell_order",
        )
        slot.orders[working_sell.server_order_id] = working_sell

        # Plan has both SELL YES + BUY NO (split order)
        planned_sell = make_planned_order(
            Token.YES, Side.SELL, px=55, sz=10,
            kind=OrderKind.REDUCE_SELL,
        )
        planned_buy = make_planned_order(
            Token.NO, Side.BUY, px=45, sz=15,
            kind=OrderKind.COMPLEMENT_BUY,
        )
        plan = LegPlan(orders=[planned_sell, planned_buy], residual_size=0, aggregated_from=0)

        effects = reconcile_slot(plan, slot, top_up_threshold=10)

        # Should place only the BUY NO
        assert len(effects.cancels) == 0
        assert len(effects.places) == 1
        assert effects.places[0].spec.token == Token.NO
        assert effects.places[0].spec.side == Side.BUY
        assert effects.places[0].spec.px == 45
        assert effects.places[0].spec.sz == 15
        assert effects.places[0].kind == OrderKind.COMPLEMENT_BUY

    def test_two_working_one_mismatches_price_cancel_and_place(self):
        """Slot has 2 working orders, one mismatches price → cancel and place that one."""
        slot = self.make_slot("bid")

        # Working orders: SELL NO + BUY YES (bid side split)
        working_sell = make_working_order(
            Token.NO, Side.SELL, px=55, sz=10,
            order_id="sell_order",
        )
        working_buy = make_working_order(
            Token.YES, Side.BUY, px=45, sz=15,
            order_id="buy_order",
        )
        slot.orders[working_sell.server_order_id] = working_sell
        slot.orders[working_buy.server_order_id] = working_buy

        # Plan has different price for BUY YES (OPEN_BUY for bid leg)
        planned_sell = make_planned_order(
            Token.NO, Side.SELL, px=55, sz=10,  # matches
            kind=OrderKind.REDUCE_SELL,
        )
        planned_buy = make_planned_order(
            Token.YES, Side.BUY, px=47, sz=15,  # price changed!
            kind=OrderKind.OPEN_BUY,  # BUY YES = OPEN_BUY
        )
        plan = LegPlan(orders=[planned_sell, planned_buy], residual_size=0, aggregated_from=0)

        effects = reconcile_slot(plan, slot, top_up_threshold=10)

        # Should cancel BUY and place new BUY (SELL matches)
        assert len(effects.cancels) == 1
        assert effects.cancels[0].order_id == "buy_order"
        assert len(effects.places) == 1
        assert effects.places[0].spec.px == 47
        assert effects.places[0].kind == OrderKind.OPEN_BUY

    def test_two_working_sell_mismatches_sell_overlap_blocks(self):
        """Slot has 2 working, SELL mismatches → cancel SELL and block replacement."""
        slot = self.make_slot("ask")

        # Working: SELL YES + BUY NO
        working_sell = make_working_order(
            Token.YES, Side.SELL, px=55, sz=10,
            order_id="sell_order",
        )
        working_buy = make_working_order(
            Token.NO, Side.BUY, px=45, sz=15,
            order_id="buy_order",
        )
        slot.orders[working_sell.server_order_id] = working_sell
        slot.orders[working_buy.server_order_id] = working_buy

        # Plan has different price for SELL YES
        planned_sell = make_planned_order(
            Token.YES, Side.SELL, px=57, sz=10,  # price changed!
            kind=OrderKind.REDUCE_SELL,
        )
        planned_buy = make_planned_order(
            Token.NO, Side.BUY, px=45, sz=15,  # matches
            kind=OrderKind.COMPLEMENT_BUY,
        )
        plan = LegPlan(orders=[planned_sell, planned_buy], residual_size=0, aggregated_from=0)

        effects = reconcile_slot(plan, slot, top_up_threshold=10)

        # Should cancel SELL, block SELL replacement, BUY matches so no action
        assert len(effects.cancels) == 1
        assert effects.cancels[0].order_id == "sell_order"
        assert len(effects.places) == 0  # SELL blocked by overlap rule
        assert effects.sell_blocked_until_cancel_ack is True


class TestSellOverlapEdgeCases:
    """Tests for SELL overlap rule edge cases."""

    def make_slot(self, slot_type: Literal["bid", "ask"] = "ask") -> OrderSlot:
        """Create an empty order slot."""
        return OrderSlot(slot_type=slot_type)

    def test_buy_replacement_not_blocked_by_sell_cancel(self):
        """BUY replacement should NOT be blocked when canceling a SELL."""
        slot = self.make_slot("ask")

        # Working: SELL YES + BUY NO
        working_sell = make_working_order(
            Token.YES, Side.SELL, px=55, sz=10,
            order_id="sell_order",
        )
        working_buy = make_working_order(
            Token.NO, Side.BUY, px=45, sz=15,
            order_id="buy_order",
        )
        slot.orders[working_sell.server_order_id] = working_sell
        slot.orders[working_buy.server_order_id] = working_buy

        # Plan: cancel SELL (not in plan) but replace BUY at new price
        planned_buy = make_planned_order(
            Token.NO, Side.BUY, px=43, sz=15,  # price changed!
            kind=OrderKind.COMPLEMENT_BUY,
        )
        plan = LegPlan(orders=[planned_buy], residual_size=0, aggregated_from=0)

        effects = reconcile_slot(plan, slot, top_up_threshold=10)

        # Should cancel both (SELL not in plan, BUY price changed)
        # BUY replacement should NOT be blocked
        assert len(effects.cancels) == 2
        cancel_ids = {c.order_id for c in effects.cancels}
        assert "sell_order" in cancel_ids
        assert "buy_order" in cancel_ids

        # BUY replacement should be placed (not blocked by SELL overlap)
        assert len(effects.places) == 1
        assert effects.places[0].spec.side == Side.BUY
        assert effects.places[0].spec.px == 43

        # SELL overlap flag should still be True (there's a SELL being canceled)
        # but BUY was still placed
        assert effects.sell_blocked_until_cancel_ack is False  # No SELL in plan to block

    def test_sell_to_buy_transition_not_blocked(self):
        """Changing from SELL to BUY (different kind) should work."""
        slot = self.make_slot("ask")

        # Working: Only SELL YES
        working_sell = make_working_order(
            Token.YES, Side.SELL, px=55, sz=10,
            order_id="sell_order",
        )
        slot.orders[working_sell.server_order_id] = working_sell

        # Plan: Only BUY NO (inventory depleted, switching to complement-only)
        planned_buy = make_planned_order(
            Token.NO, Side.BUY, px=45, sz=20,
            kind=OrderKind.COMPLEMENT_BUY,
        )
        plan = LegPlan(orders=[planned_buy], residual_size=0, aggregated_from=0)

        effects = reconcile_slot(plan, slot, top_up_threshold=10)

        # Should cancel SELL and place BUY (different kinds, not blocked)
        assert len(effects.cancels) == 1
        assert effects.cancels[0].order_id == "sell_order"

        assert len(effects.places) == 1
        assert effects.places[0].spec.side == Side.BUY
        assert effects.places[0].kind == OrderKind.COMPLEMENT_BUY

        # BUY is not blocked by SELL overlap (it's not a SELL replacement)
        assert effects.sell_blocked_until_cancel_ack is False

    def test_only_sell_being_replaced_triggers_block(self):
        """Only actual SELL→SELL replacement should be blocked."""
        slot = self.make_slot("ask")

        # Working: Only SELL YES
        working_sell = make_working_order(
            Token.YES, Side.SELL, px=55, sz=10,
            order_id="sell_order",
        )
        slot.orders[working_sell.server_order_id] = working_sell

        # Plan: Replace SELL at new price
        planned_sell = make_planned_order(
            Token.YES, Side.SELL, px=57, sz=10,
            kind=OrderKind.REDUCE_SELL,
        )
        plan = LegPlan(orders=[planned_sell], residual_size=0, aggregated_from=0)

        effects = reconcile_slot(plan, slot, top_up_threshold=10)

        # Cancel issued, but SELL place is blocked
        assert len(effects.cancels) == 1
        assert len(effects.places) == 0
        assert effects.sell_blocked_until_cancel_ack is True


class TestPartialFillReconcile:
    """End-to-end tests for partial fill handling at reconcile level."""

    def make_slot(self, slot_type: Literal["bid", "ask"] = "bid") -> OrderSlot:
        """Create an empty order slot."""
        return OrderSlot(slot_type=slot_type)

    def test_partial_fill_queue_preserved(self):
        """
        End-to-end: Partial fill should preserve queue position.

        Scenario: Order for 30, partially filled to 25 remaining.
        Strategy still wants 30 at same price.
        Reconciler should NOT cancel (small size increase below threshold).
        """
        slot = self.make_slot()

        # Working: BUY YES 30, filled 5 → remaining = 25
        working = make_working_order(
            Token.YES, Side.BUY, px=45, sz=30,
            filled_sz=5,  # Partially filled!
            order_id="partial_order",
        )
        slot.orders[working.server_order_id] = working

        # Strategy still wants 30 (BUY YES = OPEN_BUY, inferred automatically)
        planned = make_planned_order(Token.YES, Side.BUY, px=45, sz=30)
        plan = LegPlan(orders=[planned], residual_size=0, aggregated_from=0)

        effects = reconcile_slot(plan, slot, top_up_threshold=10)

        # Size diff = 30 - 25 = +5, below threshold of 10
        # Should NOT cancel - preserve queue position!
        assert effects.is_empty
        assert len(effects.cancels) == 0
        assert len(effects.places) == 0

    def test_partial_fill_large_increase_replaces(self):
        """
        Partial fill with large size increase should replace.

        Scenario: Order for 20, filled 5 → remaining = 15.
        Strategy now wants 30 at same price.
        Size diff = 30 - 15 = +15 >= threshold, should replace.
        """
        slot = self.make_slot()

        # Working: BUY YES 20, filled 5 → remaining = 15
        working = make_working_order(
            Token.YES, Side.BUY, px=45, sz=20,
            filled_sz=5,
            order_id="partial_order",
        )
        slot.orders[working.server_order_id] = working

        # Strategy wants 30 now (BUY YES = OPEN_BUY, inferred automatically)
        planned = make_planned_order(Token.YES, Side.BUY, px=45, sz=30)
        plan = LegPlan(orders=[planned], residual_size=0, aggregated_from=0)

        effects = reconcile_slot(plan, slot, top_up_threshold=10)

        # Size diff = 30 - 15 = +15 >= threshold
        # Should cancel and replace
        assert len(effects.cancels) == 1
        assert effects.cancels[0].order_id == "partial_order"
        assert len(effects.places) == 1
        assert effects.places[0].spec.sz == 30

    def test_partial_fill_decrease_replaces(self):
        """
        Partial fill with size decrease should always replace (risk reduction).

        Scenario: Order for 30, filled 5 → remaining = 25.
        Strategy now wants only 20 (reducing risk).
        Should replace despite losing queue.
        """
        slot = self.make_slot()

        # Working: BUY YES 30, filled 5 → remaining = 25
        working = make_working_order(
            Token.YES, Side.BUY, px=45, sz=30,
            filled_sz=5,
            order_id="partial_order",
        )
        slot.orders[working.server_order_id] = working

        # Strategy wants only 20 now (BUY YES = OPEN_BUY, inferred automatically)
        planned = make_planned_order(Token.YES, Side.BUY, px=45, sz=20)
        plan = LegPlan(orders=[planned], residual_size=0, aggregated_from=0)

        effects = reconcile_slot(plan, slot, top_up_threshold=10)

        # Size diff = 20 - 25 = -5 (decrease)
        # Should cancel and replace for risk reduction
        assert len(effects.cancels) == 1
        assert effects.cancels[0].order_id == "partial_order"
        assert len(effects.places) == 1
        assert effects.places[0].spec.sz == 20

    def test_partial_fill_price_change_replaces(self):
        """
        Partial fill with price change should always replace.

        Scenario: Order for 30 at 45c, filled 5 → remaining = 25.
        Strategy wants 30 at 47c (price moved).
        Should replace despite fill progress.
        """
        slot = self.make_slot()

        # Working: BUY YES 30 @ 45c, filled 5
        working = make_working_order(
            Token.YES, Side.BUY, px=45, sz=30,
            filled_sz=5,
            order_id="partial_order",
        )
        slot.orders[working.server_order_id] = working

        # Strategy wants 30 at new price (BUY YES = OPEN_BUY, inferred automatically)
        planned = make_planned_order(Token.YES, Side.BUY, px=47, sz=30)  # Price changed
        plan = LegPlan(orders=[planned], residual_size=0, aggregated_from=0)

        effects = reconcile_slot(plan, slot, top_up_threshold=10)

        # Price changed - must replace regardless of fill state
        assert len(effects.cancels) == 1
        assert len(effects.places) == 1
        assert effects.places[0].spec.px == 47
        assert effects.places[0].spec.sz == 30


class TestStoredKindBehavior:
    """
    Tests that verify the reconciler uses stored kind on WorkingOrder.

    The correct flow is:
    - Planner decides kind → executor places → WorkingOrder stores kind
    - Reconciler uses stored kind for matching

    Inference (from side/token) is only used as fallback for synced orders
    where kind is unknown.
    """

    def make_slot(self, slot_type: Literal["bid", "ask"] = "bid") -> OrderSlot:
        """Create an empty order slot."""
        return OrderSlot(slot_type=slot_type)

    def test_uses_stored_kind_for_matching(self):
        """
        Reconciler should use stored kind, not infer from spec.

        This test verifies that a working order with explicit kind="open_buy"
        matches a planned order with kind=OPEN_BUY.
        """
        slot = self.make_slot()

        # Working order with explicit kind
        working = make_working_order(
            Token.YES, Side.BUY, px=45, sz=10,
            kind="open_buy",  # Explicitly set kind
            order_id="order_123",
        )
        slot.orders[working.server_order_id] = working

        # Planned order with matching kind
        planned = make_planned_order(
            Token.YES, Side.BUY, px=45, sz=10,
            kind=OrderKind.OPEN_BUY,
        )
        plan = LegPlan(orders=[planned], residual_size=0, aggregated_from=0)

        effects = reconcile_slot(plan, slot, top_up_threshold=10)

        # Should match - no effects needed
        assert effects.is_empty

    def test_falls_back_to_inference_for_synced_orders(self):
        """
        For synced orders (kind=None), reconciler should infer from spec.

        This simulates orders loaded from exchange where we don't know
        the original planner intent.
        """
        slot = self.make_slot()

        # Synced order with no stored kind (kind=None)
        spec = RealOrderSpec(
            token=Token.YES,
            token_id="YES_token",
            side=Side.BUY,
            px=45,
            sz=10,
        )
        working = WorkingOrder(
            client_order_id="synced_order",
            server_order_id="synced_123",
            order_spec=spec,
            status=OrderStatus.WORKING,
            created_ts=1000,
            last_state_change_ts=1000,
            kind=None,  # No kind - synced from exchange
        )
        slot.orders[working.server_order_id] = working

        # Planned order - BUY YES should match inferred OPEN_BUY
        planned = make_planned_order(
            Token.YES, Side.BUY, px=45, sz=10,
            kind=OrderKind.OPEN_BUY,
        )
        plan = LegPlan(orders=[planned], residual_size=0, aggregated_from=0)

        effects = reconcile_slot(plan, slot, top_up_threshold=10)

        # Should match via inference
        assert effects.is_empty

    def test_different_kinds_cancel_and_place(self):
        """
        Working and planned with different kinds should not match.

        This tests that kind mismatch (even with same spec) causes replacement.
        """
        slot = self.make_slot("ask")

        # Working: BUY NO with kind="complement_buy"
        working = make_working_order(
            Token.NO, Side.BUY, px=45, sz=10,
            kind="complement_buy",
            order_id="order_123",
        )
        slot.orders[working.server_order_id] = working

        # Planned: Same BUY NO but as REDUCE_SELL kind
        # (This is an unusual case but tests kind-based matching)
        planned = make_planned_order(
            Token.YES, Side.SELL, px=55, sz=10,  # Different order entirely
            kind=OrderKind.REDUCE_SELL,
        )
        plan = LegPlan(orders=[planned], residual_size=0, aggregated_from=0)

        effects = reconcile_slot(plan, slot, top_up_threshold=10)

        # Different kinds - should cancel the complement_buy and place the reduce_sell
        assert len(effects.cancels) == 1
        assert effects.cancels[0].order_id == "order_123"
        assert effects.cancels[0].kind == OrderKind.COMPLEMENT_BUY

        # Should place REDUCE_SELL (SELL replacement blocked by SELL overlap rule)
        # Wait, there's no existing SELL, so should place
        assert len(effects.places) == 1
        assert effects.places[0].kind == OrderKind.REDUCE_SELL
