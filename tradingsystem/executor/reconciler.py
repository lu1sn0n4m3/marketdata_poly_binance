"""
Reconciler for the executor.

Compares planned orders (from planner) with working orders (in slot)
and produces effects (cancel/place) to achieve the desired state.

KEY DESIGN PRINCIPLES:

1. QUEUE-PRESERVING REPLACEMENT POLICY
   - Price change: ALWAYS replace (price must match)
   - Size decrease: ALWAYS replace (risk reduction worth losing queue)
   - Size increase < threshold: DO NOTHING (preserve queue position)
   - Size increase >= threshold: REPLACE (material size change)

   This prevents churning queue position on small size changes.
   A partial fill (30→28) won't trigger replacement if strategy still wants 30.

2. Match by OrderKind first, then token/side
   - Prevents flapping when orders have different tokens/sides

3. NEVER place replacement SELL until existing SELL is canceled
   - The exchange still has the old sell live until cancel ack
   - Inventory is still reserved on-venue
   - Placing early causes "insufficient balance" errors

4. Return empty batch if slot is busy
   - Slot has pending operations = wait for them to complete
   - No overlapping "mutating waves"

5. Cancels always execute before places
   - Ensures we don't exceed position limits
"""

import logging
from typing import TYPE_CHECKING, Tuple

from .effects import ReconcileEffect, EffectBatch
from .planner import OrderKind, LegPlan

if TYPE_CHECKING:
    from ..types import RealOrderSpec, WorkingOrder
    from .state import OrderSlot
    from .planner import PlannedOrder

logger = logging.getLogger(__name__)


def reconcile_slot(
    plan: LegPlan,
    slot: "OrderSlot",
    price_tol: int = 0,
    top_up_threshold: int = 10,
) -> EffectBatch:
    """
    Compare planned orders with working orders and produce effects.

    Uses QUEUE-PRESERVING replacement policy:
    - Price change: always replace
    - Size decrease: always replace (risk reduction)
    - Size increase < threshold: do nothing (preserve queue)
    - Size increase >= threshold: replace (material change)

    Args:
        plan: LegPlan from planner (0, 1, or 2 planned orders)
        slot: OrderSlot with current working orders
        price_tol: Price tolerance for matching (cents)
        top_up_threshold: Only replace on size increase if >= this threshold

    Returns:
        EffectBatch with cancel/place effects
    """
    # If slot is busy, don't produce any effects
    if not slot.can_submit():
        logger.debug(f"Slot {slot.slot_type} is busy (state={slot.state.name}), skipping reconcile")
        return EffectBatch()

    cancels: list[ReconcileEffect] = []
    places: list[ReconcileEffect] = []
    sell_being_canceled = False

    # Index working orders by kind for matching
    working_by_kind = _index_orders_by_kind(slot.orders, slot.slot_type)
    planned_by_kind = {o.kind: o for o in plan.orders}

    # Step 1: Evaluate each working order
    for kind, working in working_by_kind.items():
        planned = planned_by_kind.get(kind)

        if planned is None:
            # Working order not in plan - cancel it
            cancels.append(ReconcileEffect(
                action="cancel",
                slot_type=slot.slot_type,
                order_id=working.server_order_id,
                kind=kind,
            ))
            if _is_sell_order(working):
                sell_being_canceled = True
            logger.debug(
                f"Canceling {kind.name} order {working.server_order_id[:20]}... "
                f"(not in plan)"
            )
        else:
            # Check if we should replace this order
            should_replace, reason = _should_replace(
                working, planned, price_tol, top_up_threshold
            )

            if should_replace:
                cancels.append(ReconcileEffect(
                    action="cancel",
                    slot_type=slot.slot_type,
                    order_id=working.server_order_id,
                    kind=kind,
                ))
                if _is_sell_order(working):
                    sell_being_canceled = True
                logger.debug(
                    f"Canceling {kind.name} order {working.server_order_id[:20]}... "
                    f"({reason})"
                )
            # else: order is good, leave it working

    # Step 2: Determine which planned orders need to be placed
    blocked_sells: list[ReconcileEffect] = []

    for kind, planned in planned_by_kind.items():
        working = working_by_kind.get(kind)

        needs_place = False
        if working is None:
            # No working order - need to place
            needs_place = True
        else:
            # Check if we decided to cancel the working order
            was_canceled = any(
                c.order_id == working.server_order_id for c in cancels
            )
            if was_canceled:
                needs_place = True

        if needs_place:
            effect = _create_place_effect(planned, slot.slot_type)

            # Check SELL overlap rule
            if _is_sell_planned(planned) and sell_being_canceled:
                # Block this SELL - will be placed after cancel ack
                blocked_sells.append(effect)
                logger.debug(
                    f"Blocking {kind.name} SELL place until cancel ack "
                    f"(sell overlap rule)"
                )
            else:
                places.append(effect)

    # If we blocked any SELLs, set the flag
    sell_blocked = len(blocked_sells) > 0

    return EffectBatch(
        cancels=cancels,
        places=places,
        sell_blocked_until_cancel_ack=sell_blocked,
    )


def _should_replace(
    working: "WorkingOrder",
    planned: "PlannedOrder",
    price_tol: int,
    top_up_threshold: int,
) -> Tuple[bool, str]:
    """
    Determine if working order should be replaced with planned order.

    Queue-preserving replacement policy:
    - Token/side mismatch: REPLACE (shouldn't happen, but handle it)
    - Price change: REPLACE (price must match)
    - Size decrease: REPLACE (risk reduction worth losing queue)
    - Size increase < threshold: DO NOTHING (preserve queue position)
    - Size increase >= threshold: REPLACE (material size change)

    Returns:
        (should_replace, reason) tuple
    """
    spec = working.order_spec
    remaining = working.remaining_sz

    # Token and side must match exactly
    if spec.token != planned.token:
        return True, "token_mismatch"
    if spec.side != planned.side:
        return True, "side_mismatch"

    # Price change: always replace
    if abs(spec.px - planned.px) > price_tol:
        return True, f"price_change_{spec.px}c->{planned.px}c"

    # Size comparison: compare remaining (after partial fills) to planned
    size_diff = planned.sz - remaining

    if size_diff < 0:
        # Size decrease: always replace (risk reduction)
        return True, f"size_decrease_{remaining}->{planned.sz}"

    if size_diff == 0:
        # Exact match
        return False, "exact_match"

    # Size increase: only replace if material (>= threshold)
    if size_diff >= top_up_threshold:
        return True, f"size_increase_{remaining}->{planned.sz}(+{size_diff}>=threshold)"

    # Size increase below threshold: preserve queue position
    logger.info(
        f"[QUEUE_PRESERVE] Ignoring top-up {remaining}->{planned.sz} "
        f"(+{size_diff} < threshold {top_up_threshold})"
    )
    return False, "size_increase_below_threshold"


def _index_orders_by_kind(
    orders: dict[str, "WorkingOrder"],
    slot_type: str,
) -> dict[OrderKind, "WorkingOrder"]:
    """
    Index working orders by their OrderKind.

    Uses stored kind when available (set by planner at placement).
    Falls back to inference only for synced orders (kind=None).
    """
    result: dict[OrderKind, "WorkingOrder"] = {}
    for order in orders.values():
        kind = _get_kind(order, slot_type)
        # If multiple orders of same kind, take the first
        # (shouldn't happen in normal operation)
        if kind not in result:
            result[kind] = order
    return result


def _get_kind(order: "WorkingOrder", slot_type: str) -> OrderKind:
    """
    Get OrderKind for a working order.

    Uses stored kind (preferred - set by planner or at sync time).
    Falls back to inference ONLY as emergency fallback - this should not happen
    in normal operation since both planner and sync set kind.
    """
    # Use stored kind if available
    if order.kind is not None:
        return _kind_str_to_enum(order.kind)

    # WARNING: kind=None should not happen after planner or sync sets it
    # This is an emergency fallback - log warning for visibility
    logger.warning(
        f"Order {order.server_order_id[:20]}... has kind=None - "
        f"inferring from spec (this should not happen in normal operation)"
    )

    # Fallback: infer from (side, token)
    return _infer_kind_from_spec(order)


def _kind_str_to_enum(kind_str: str) -> OrderKind:
    """Convert kind string to OrderKind enum."""
    if kind_str == "reduce_sell":
        return OrderKind.REDUCE_SELL
    elif kind_str == "open_buy":
        return OrderKind.OPEN_BUY
    elif kind_str == "complement_buy":
        return OrderKind.COMPLEMENT_BUY
    else:
        # Unknown kind - shouldn't happen, but treat as REDUCE_SELL for safety
        logger.warning(f"Unknown order kind: {kind_str}, defaulting to REDUCE_SELL")
        return OrderKind.REDUCE_SELL


def _infer_kind_from_spec(order: "WorkingOrder") -> OrderKind:
    """
    Infer OrderKind from order spec (side, token).

    FALLBACK ONLY - used for synced orders where kind is unknown.
    For orders placed by planner, use stored kind instead.

    SELL = REDUCE_SELL (selling inventory to reduce exposure)
    BUY YES = OPEN_BUY (opening primary exposure)
    BUY NO = COMPLEMENT_BUY (using complement)
    """
    from ..types import Side, Token

    spec = order.order_spec
    if spec.side == Side.SELL:
        return OrderKind.REDUCE_SELL

    # BUY: determine from token
    if spec.token == Token.YES:
        return OrderKind.OPEN_BUY
    else:
        return OrderKind.COMPLEMENT_BUY


def _is_sell_order(order: "WorkingOrder") -> bool:
    """Check if working order is a SELL."""
    from ..types import Side
    return order.order_spec.side == Side.SELL


def _is_sell_planned(planned: "PlannedOrder") -> bool:
    """Check if planned order is a SELL."""
    from ..types import Side
    return planned.side == Side.SELL


def _create_place_effect(
    planned: "PlannedOrder",
    slot_type: str,
) -> ReconcileEffect:
    """Create a place effect from a planned order."""
    from ..types import RealOrderSpec

    spec = RealOrderSpec(
        token=planned.token,
        token_id=planned.token_id,
        side=planned.side,
        px=planned.px,
        sz=planned.sz,
    )

    return ReconcileEffect(
        action="place",
        slot_type=slot_type,
        spec=spec,
        kind=planned.kind,
    )
