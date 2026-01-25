"""
Execution planner for the executor.

Converts YES-space strategy intents into executable order plans,
handling complement symmetry, reduce-first priority, and min-size constraints.

KEY DESIGN PRINCIPLES:

1. Strategy stays in YES-space (ignorant of venue mechanics)
   - Strategy expresses "want bid at X, ask at Y" in YES-space
   - Planner translates to real orders (YES/NO, BUY/SELL)

2. Reduce-first priority
   - If holding NO, prefer SELL NO for a bid (reduces exposure)
   - If holding YES, prefer SELL YES for an ask (reduces exposure)

3. Split orders when needed
   - "SELL YES 5" with only 2 YES available becomes:
     SELL YES 2 + BUY NO 3 (but see min-size below)

4. Min-size is PLAN-LEVEL, not per-leg
   - Split legs are COUPLED decisions
   - If both legs are sub-min, treat as paired decision
   - Never output illegal orders (all orders >= min_size)

5. Pure function: (intent, state_snapshot) -> ExecutionPlan
   - No side effects, deterministic
   - Easy to test and reason about
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, TYPE_CHECKING

from .policies import MinSizePolicy

if TYPE_CHECKING:
    from ..types import Token, Side, DesiredQuoteSet, InventoryState
    from .state import ReservationLedger


class OrderKind(Enum):
    """
    Role of order in execution plan.

    Used by reconciler to match planned orders with working orders
    without flapping between different order types.

    Roles:
    - REDUCE_SELL: Selling held inventory (YES or NO) to reduce gross exposure
    - OPEN_BUY: Buying primary token to open/increase exposure (BUY YES for bid)
    - COMPLEMENT_BUY: Buying complement token to express opposite side (BUY NO for ask)

    Mapping for YES-space intents:

    YES bid ("want long YES"):
    - If reducing NO inventory: REDUCE_SELL (SELL NO)
    - Remainder: OPEN_BUY (BUY YES) - opening YES exposure

    YES ask ("want short YES"):
    - If reducing YES inventory: REDUCE_SELL (SELL YES)
    - Remainder: COMPLEMENT_BUY (BUY NO) - using complement to express ask

    Key insight: BUY YES is almost always OPEN_BUY, BUY NO is almost always COMPLEMENT_BUY.
    The reconciler infers kind from (slot_type, token, side) to match correctly.
    """
    REDUCE_SELL = "reduce_sell"       # Selling held inventory (reduces exposure)
    OPEN_BUY = "open_buy"             # Buying primary token (BUY YES for bid)
    COMPLEMENT_BUY = "complement_buy"  # Buying complement token (BUY NO for ask)


@dataclass
class PlannedOrder:
    """
    Single order in an execution plan.

    Always represents a legal order (size >= min_size).
    """
    kind: OrderKind
    token: "Token"
    side: "Side"
    px: int
    sz: int
    token_id: str = ""  # Filled in by caller


@dataclass
class LegPlan:
    """
    Plan for one side (bid or ask).

    Contains 0, 1, or 2 orders (never orders with illegal sizes).
    """
    orders: list[PlannedOrder] = field(default_factory=list)

    # Size we couldn't place due to min-size constraints
    residual_size: int = 0

    # If we aggregated (rounded up), what was the original target?
    # 0 means no aggregation was applied
    aggregated_from: int = 0

    # Which policy was applied
    policy_applied: MinSizePolicy = MinSizePolicy.PASSIVE_FIRST

    @property
    def total_planned_size(self) -> int:
        """Total size across all planned orders."""
        return sum(o.sz for o in self.orders)

    @property
    def is_empty(self) -> bool:
        """True if no orders planned."""
        return len(self.orders) == 0


@dataclass
class ExecutionPlan:
    """
    Complete execution plan for both sides.

    Output of the planner, input to the reconciler.
    """
    bid: LegPlan
    ask: LegPlan
    timestamp_ms: int

    @property
    def is_empty(self) -> bool:
        """True if no orders on either side."""
        return self.bid.is_empty and self.ask.is_empty


def plan_execution(
    intent: "DesiredQuoteSet",
    inventory: "InventoryState",
    reservations: "ReservationLedger",
    yes_token_id: str,
    no_token_id: str,
    policy: MinSizePolicy = MinSizePolicy.PASSIVE_FIRST,
    min_size: int = 5,
) -> ExecutionPlan:
    """
    Build execution plan from strategy intent.

    KEY INVARIANTS:
    1. Never plan SELL order larger than available inventory
    2. Reduce-first: prioritize sells from held inventory
    3. Split orders when needed (SELL YES K + BUY NO (S-K))
    4. Min-size applies at PLAN LEVEL, not per-leg:
       - If split produces two sub-min legs, treat as coupled decision
       - Either aggregate whole target, or place only executable portion
    5. Every order in output is legal (>= min_size or empty plan)

    CRITICAL - SETTLED vs PENDING INVENTORY:
    SELL orders use SETTLED inventory only (I_yes, I_no), NOT pending.
    Pending inventory (MATCHED but not MINED) cannot be sold because
    tokens are not yet in the wallet. This is why we pass inventory.I_yes
    to available_yes() rather than inventory.effective_yes.

    The STRATEGY sees effective inventory (settled + pending) to avoid
    placing duplicate orders, but the PLANNER uses settled inventory
    for SELL sizing.

    Args:
        intent: Strategy's desired quote state (YES-space)
        inventory: Current inventory (uses settled portion for SELL sizing)
        reservations: Reservation ledger for available inventory
        yes_token_id: YES token ID for this market
        no_token_id: NO token ID for this market
        policy: MinSizePolicy for handling sub-min situations
        min_size: Minimum order size (default 5 for Polymarket)

    Returns:
        ExecutionPlan with legal orders only
    """
    from ..types import now_ms

    # Calculate available inventory - SETTLED ONLY (not pending)
    # Pending inventory cannot be sold - it's not in the wallet yet
    avail_yes = reservations.available_yes(inventory.I_yes)
    avail_no = reservations.available_no(inventory.I_no)

    # Plan each leg
    bid_plan = _plan_bid_leg(
        leg=intent.bid_yes,
        avail_no=avail_no,
        yes_token_id=yes_token_id,
        no_token_id=no_token_id,
        policy=policy,
        min_size=min_size,
    )

    ask_plan = _plan_ask_leg(
        leg=intent.ask_yes,
        avail_yes=avail_yes,
        yes_token_id=yes_token_id,
        no_token_id=no_token_id,
        policy=policy,
        min_size=min_size,
    )

    return ExecutionPlan(
        bid=bid_plan,
        ask=ask_plan,
        timestamp_ms=now_ms(),
    )


def _plan_bid_leg(
    leg: "DesiredQuoteLeg",
    avail_no: int,
    yes_token_id: str,
    no_token_id: str,
    policy: MinSizePolicy,
    min_size: int,
) -> LegPlan:
    """
    Plan YES bid (want to go long YES).

    Priority: SELL NO (reduce) > BUY YES (complement)

    If we have NO inventory, prefer selling it to reduce exposure.
    Otherwise, buy YES.

    CRITICAL: Split legs are coupled for min-size decisions.
    """
    from ..types import DesiredQuoteLeg

    if not leg.enabled:
        return LegPlan(policy_applied=policy)

    target_size = leg.sz
    px_yes = leg.px_yes
    px_no = 100 - px_yes  # Complement price

    # How much can we reduce by selling NO?
    reduce_size = min(target_size, avail_no)
    complement_size = target_size - reduce_size

    # Skip min-size for marketable dust cleanup orders
    # (Polymarket has no min_size for marketable orders)
    effective_min_size = 0 if leg.skip_min_size else min_size

    # Apply min-size as a COUPLED decision
    return _apply_min_size_to_split(
        reduce_size=reduce_size,
        complement_size=complement_size,
        reduce_token_id=no_token_id,
        reduce_px=px_no,
        complement_token_id=yes_token_id,
        complement_px=px_yes,
        target_size=target_size,
        policy=policy,
        min_size=effective_min_size,
        is_bid=True,
    )


def _plan_ask_leg(
    leg: "DesiredQuoteLeg",
    avail_yes: int,
    yes_token_id: str,
    no_token_id: str,
    policy: MinSizePolicy,
    min_size: int,
) -> LegPlan:
    """
    Plan YES ask (want to go short YES / reduce long YES).

    Priority: SELL YES (reduce) > BUY NO (complement)

    If we have YES inventory, prefer selling it to reduce exposure.
    Otherwise, buy NO.

    CRITICAL: Split legs are coupled for min-size decisions.
    """
    from ..types import DesiredQuoteLeg

    if not leg.enabled:
        return LegPlan(policy_applied=policy)

    target_size = leg.sz
    px_yes = leg.px_yes
    px_no = 100 - px_yes  # Complement price

    # How much can we reduce by selling YES?
    reduce_size = min(target_size, avail_yes)
    complement_size = target_size - reduce_size

    # Skip min-size for marketable dust cleanup orders
    # (Polymarket has no min_size for marketable orders)
    effective_min_size = 0 if leg.skip_min_size else min_size

    # Apply min-size as a COUPLED decision
    return _apply_min_size_to_split(
        reduce_size=reduce_size,
        complement_size=complement_size,
        reduce_token_id=yes_token_id,
        reduce_px=px_yes,
        complement_token_id=no_token_id,
        complement_px=px_no,
        target_size=target_size,
        policy=policy,
        min_size=effective_min_size,
        is_bid=False,
    )


def _apply_min_size_to_split(
    reduce_size: int,
    complement_size: int,
    reduce_token_id: str,
    reduce_px: int,
    complement_token_id: str,
    complement_px: int,
    target_size: int,
    policy: MinSizePolicy,
    min_size: int,
    is_bid: bool,
) -> LegPlan:
    """
    Apply min-size policy to a potentially split order.

    This is the core logic for handling the "have 2, target 5, min 5" problem.

    Cases:
    1. No split needed (reduce_size == 0 or reduce_size == target_size)
       - Single order, apply min-size directly
    2. Both legs >= min_size
       - Place both (full split)
    3. Reduce >= min_size, complement < min_size
       - Place reduce only, complement becomes residual
    4. Reduce < min_size, complement >= min_size
       - Place complement only, reduce becomes residual (dust)
    5. Both < min_size (the hard case)
       - PASSIVE_FIRST: Place nothing, whole target is residual
       - AGGREGATE: Round up to min_size (prefer complement buy, no inventory constraint)
       - DUST_CLEANUP: Not handled here (separate mechanism)

    Args:
        reduce_size: Size for reducing sell (SELL YES or SELL NO)
        complement_size: Size for complement buy (BUY NO or BUY YES)
        reduce_token_id: Token ID for reduce leg
        reduce_px: Price for reduce leg (in cents)
        complement_token_id: Token ID for complement leg
        complement_px: Price for complement leg (in cents)
        target_size: Original target size from intent
        policy: MinSizePolicy to apply
        min_size: Minimum order size
        is_bid: True for bid leg (reduce=SELL NO, complement=BUY YES)

    Returns:
        LegPlan with legal orders only
    """
    from ..types import Token, Side

    orders = []
    residual = 0
    aggregated_from = 0

    # Determine tokens and buy kind based on leg type
    # - Bid: reduce=SELL NO, buy=BUY YES (OPEN_BUY - opening YES exposure)
    # - Ask: reduce=SELL YES, buy=BUY NO (COMPLEMENT_BUY - using complement)
    if is_bid:
        reduce_token = Token.NO
        buy_token = Token.YES
        buy_kind = OrderKind.OPEN_BUY  # BUY YES opens primary exposure
    else:
        reduce_token = Token.YES
        buy_token = Token.NO
        buy_kind = OrderKind.COMPLEMENT_BUY  # BUY NO is the complement for ask

    # Check legal sizes
    reduce_legal = reduce_size >= min_size
    complement_legal = complement_size >= min_size

    # Case 1a: No inventory to reduce, simple buy
    if reduce_size == 0:
        if complement_legal:
            orders.append(PlannedOrder(
                kind=buy_kind,
                token=buy_token,
                side=Side.BUY,
                px=complement_px,
                sz=complement_size,
                token_id=complement_token_id,
            ))
        elif policy == MinSizePolicy.AGGREGATE:
            # Round up to minimum
            aggregated_from = complement_size
            orders.append(PlannedOrder(
                kind=buy_kind,
                token=buy_token,
                side=Side.BUY,
                px=complement_px,
                sz=min_size,
                token_id=complement_token_id,
            ))
        else:
            # PASSIVE_FIRST: can't place, keep as residual
            residual = complement_size

        return LegPlan(
            orders=orders,
            residual_size=residual,
            aggregated_from=aggregated_from,
            policy_applied=policy,
        )

    # Case 1b: Reduce covers full target, simple reduce sell
    if reduce_size >= target_size:
        if reduce_size >= min_size:
            orders.append(PlannedOrder(
                kind=OrderKind.REDUCE_SELL,
                token=reduce_token,
                side=Side.SELL,
                px=reduce_px,
                sz=target_size,  # Use target, not available (don't over-sell)
                token_id=reduce_token_id,
            ))
        elif policy == MinSizePolicy.AGGREGATE:
            # Can't aggregate a SELL beyond available inventory!
            # Fall back to residual
            residual = target_size
        else:
            residual = target_size

        return LegPlan(
            orders=orders,
            residual_size=residual,
            aggregated_from=aggregated_from,
            policy_applied=policy,
        )

    # Cases 2-5: Split scenario (0 < reduce_size < target_size)

    if reduce_legal and complement_legal:
        # Case 2: Both legal - full split
        orders.append(PlannedOrder(
            kind=OrderKind.REDUCE_SELL,
            token=reduce_token,
            side=Side.SELL,
            px=reduce_px,
            sz=reduce_size,
            token_id=reduce_token_id,
        ))
        orders.append(PlannedOrder(
            kind=buy_kind,
            token=buy_token,
            side=Side.BUY,
            px=complement_px,
            sz=complement_size,
            token_id=complement_token_id,
        ))

    elif reduce_legal and not complement_legal:
        # Case 3: Only reduce is legal
        orders.append(PlannedOrder(
            kind=OrderKind.REDUCE_SELL,
            token=reduce_token,
            side=Side.SELL,
            px=reduce_px,
            sz=reduce_size,
            token_id=reduce_token_id,
        ))
        residual = complement_size  # Can't place this

    elif not reduce_legal and complement_legal:
        # Case 4: Only buy is legal
        orders.append(PlannedOrder(
            kind=buy_kind,
            token=buy_token,
            side=Side.BUY,
            px=complement_px,
            sz=complement_size,
            token_id=complement_token_id,
        ))
        residual = reduce_size  # Dust that couldn't reduce

    else:
        # Case 5: Both sub-min - coupled decision
        if policy == MinSizePolicy.PASSIVE_FIRST:
            # Place nothing, keep entire target as residual
            residual = target_size

        elif policy == MinSizePolicy.AGGREGATE:
            # Round up whole target to min_size
            # IMPORTANT: Prefer buy (no inventory constraint)
            # Cannot aggregate SELL beyond available inventory
            aggregated_from = target_size
            orders.append(PlannedOrder(
                kind=buy_kind,
                token=buy_token,
                side=Side.BUY,
                px=complement_px,
                sz=min_size,
                token_id=complement_token_id,
            ))
            # The reduce_size portion becomes residual (dust we couldn't sell)
            residual = reduce_size

        # DUST_CLEANUP is handled separately via marketable orders

    return LegPlan(
        orders=orders,
        residual_size=residual,
        aggregated_from=aggregated_from,
        policy_applied=policy,
    )


@dataclass
class ExecutionReport:
    """
    Report of what executor actually did vs intent.

    Used for logging, metrics, and debugging "why isn't my strategy quoting 5/5".
    """
    intent_bid_px: int
    intent_bid_sz: int
    intent_ask_px: int
    intent_ask_sz: int

    # What we actually planned
    bid_orders_planned: int  # 0, 1, or 2
    ask_orders_planned: int
    bid_planned_size: int
    ask_planned_size: int
    bid_residual: int        # Size we couldn't place
    ask_residual: int
    bid_aggregated: bool     # True if we rounded up
    ask_aggregated: bool

    # What policy was applied
    bid_policy: MinSizePolicy
    ask_policy: MinSizePolicy


def create_execution_report(
    intent: "DesiredQuoteSet",
    plan: ExecutionPlan,
) -> ExecutionReport:
    """Create an execution report from intent and plan."""
    return ExecutionReport(
        intent_bid_px=intent.bid_yes.px_yes if intent.bid_yes.enabled else 0,
        intent_bid_sz=intent.bid_yes.sz if intent.bid_yes.enabled else 0,
        intent_ask_px=intent.ask_yes.px_yes if intent.ask_yes.enabled else 0,
        intent_ask_sz=intent.ask_yes.sz if intent.ask_yes.enabled else 0,
        bid_orders_planned=len(plan.bid.orders),
        ask_orders_planned=len(plan.ask.orders),
        bid_planned_size=plan.bid.total_planned_size,
        ask_planned_size=plan.ask.total_planned_size,
        bid_residual=plan.bid.residual_size,
        ask_residual=plan.ask.residual_size,
        bid_aggregated=plan.bid.aggregated_from > 0,
        ask_aggregated=plan.ask.aggregated_from > 0,
        bid_policy=plan.bid.policy_applied,
        ask_policy=plan.ask.policy_applied,
    )
