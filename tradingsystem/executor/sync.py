"""
Synchronization logic for the executor.

Handles:
- Startup sync (fetch balances and open orders from REST)
- Resync on integrity incidents (balance errors, unknown fills)
- Tombstone management for orphan fill accounting

KEY DESIGN PRINCIPLES:

1. RESYNCING mode blocks new submissions but continues processing fills
   - Fills during resync update inventory and PnL (never dropped)
   - Tombstoned orders allow matching orphan fills

2. Resync is triggered by:
   - Insufficient balance errors
   - Unknown fill events
   - Manual trigger

3. Tombstones keep recently-canceled orders for a configurable duration
   - Allows matching fills that arrive after cancel was processed locally
   - Expired tombstones are cleaned up periodically
"""

import logging
from dataclasses import dataclass
from typing import Optional, Callable, TYPE_CHECKING

if TYPE_CHECKING:
    from ..types import Token, Side, WorkingOrder, RealOrderSpec, InventoryState
    from .state import ExecutorState, OrderSlot, SlotState, ExecutorMode

logger = logging.getLogger(__name__)


@dataclass
class SyncResult:
    """Result of a sync operation."""
    success: bool
    yes_balance: int = 0
    no_balance: int = 0
    orders_synced: int = 0
    error: Optional[str] = None


def sync_balances(
    state: "ExecutorState",
    rest_client: "PolymarketRestClient",
    yes_token_id: str,
    no_token_id: str,
) -> SyncResult:
    """
    Sync inventory balances from REST API.

    Args:
        state: ExecutorState to update
        rest_client: REST client for API calls
        yes_token_id: YES token ID
        no_token_id: NO token ID

    Returns:
        SyncResult with new balances
    """
    from ..types import now_ms

    try:
        # Fetch balances
        balances = rest_client.get_balances()

        yes_balance = int(balances.get(yes_token_id, 0))
        no_balance = int(balances.get(no_token_id, 0))

        # Update state
        state.inventory.I_yes = yes_balance
        state.inventory.I_no = no_balance
        state.inventory.last_update_ts = now_ms()

        logger.info(f"Synced balances: YES={yes_balance} NO={no_balance}")

        return SyncResult(
            success=True,
            yes_balance=yes_balance,
            no_balance=no_balance,
        )

    except Exception as e:
        logger.error(f"Failed to sync balances: {e}")
        return SyncResult(success=False, error=str(e))


def sync_open_orders(
    state: "ExecutorState",
    rest_client: "PolymarketRestClient",
    market_id: str,
    yes_token_id: str,
    no_token_id: str,
) -> SyncResult:
    """
    Sync open orders from REST API.

    Populates working orders in bid/ask slots based on order type.

    Args:
        state: ExecutorState to update
        rest_client: REST client for API calls
        market_id: Market/condition ID
        yes_token_id: YES token ID
        no_token_id: NO token ID

    Returns:
        SyncResult with count of synced orders
    """
    from ..types import Token, Side, OrderStatus, RealOrderSpec, WorkingOrder, now_ms

    try:
        # Fetch open orders
        orders = rest_client.get_open_orders(market_id)
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
                    token_id = yes_token_id
                elif asset_id == no_token_id:
                    token = Token.NO
                    token_id = no_token_id
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
                    token_id=token_id,
                    side=side,
                    px=price,
                    sz=size,
                )

                # Classify as bid or ask based on token/side
                # Bid: BUY YES or SELL NO (at complement)
                # Ask: SELL YES or BUY NO (at complement)
                is_bid = (token == Token.YES and side == Side.BUY) or \
                         (token == Token.NO and side == Side.SELL)

                # Infer kind from (side, token) for synced orders
                # This ensures all orders have a kind, avoiding inference in reconciler
                if side == Side.SELL:
                    inferred_kind = "reduce_sell"
                elif token == Token.YES:
                    inferred_kind = "open_buy"  # BUY YES = opening primary exposure
                else:
                    inferred_kind = "complement_buy"  # BUY NO = complement buy

                # Create WorkingOrder with inferred kind
                working_order = WorkingOrder(
                    client_order_id=f"synced_{order_id}",
                    server_order_id=order_id,
                    order_spec=spec,
                    status=OrderStatus.WORKING,
                    created_ts=ts,
                    last_state_change_ts=ts,
                    filled_sz=filled,
                    kind=inferred_kind,  # Set kind at sync time
                )

                if is_bid:
                    state.bid_slot.orders[order_id] = working_order
                else:
                    state.ask_slot.orders[order_id] = working_order

                logger.info(
                    f"Synced order: {order_id[:20]}... {token.name} {side.name} "
                    f"{size}@{price}c ({'bid' if is_bid else 'ask'})"
                )
                synced += 1

            except Exception as e:
                logger.warning(f"Failed to sync order: {e}")

        return SyncResult(success=True, orders_synced=synced)

    except Exception as e:
        logger.error(f"Failed to sync open orders: {e}")
        return SyncResult(success=False, error=str(e))


def perform_full_resync(
    state: "ExecutorState",
    rest_client: "PolymarketRestClient",
    market_id: str,
    yes_token_id: str,
    no_token_id: str,
    safety_buffer: int = 0,
) -> SyncResult:
    """
    Perform a full resync: balances + open orders + rebuild reservations.

    This is called after entering RESYNCING mode and canceling all orders.

    Args:
        state: ExecutorState to update
        rest_client: REST client for API calls
        market_id: Market/condition ID
        yes_token_id: YES token ID
        no_token_id: NO token ID
        safety_buffer: Safety buffer for reservation ledger

    Returns:
        SyncResult with combined results
    """
    from .state import SlotState, ExecutorMode

    # Sync balances
    balance_result = sync_balances(state, rest_client, yes_token_id, no_token_id)
    if not balance_result.success:
        return balance_result

    # Clear slots before syncing orders
    state.bid_slot.clear()
    state.ask_slot.clear()

    # Sync open orders
    order_result = sync_open_orders(
        state, rest_client, market_id, yes_token_id, no_token_id
    )
    if not order_result.success:
        return order_result

    # Rebuild reservations from working orders
    state.rebuild_reservations(safety_buffer)

    # Exit RESYNCING mode
    state.mode = ExecutorMode.NORMAL
    state.bid_slot.state = SlotState.IDLE
    state.ask_slot.state = SlotState.IDLE

    logger.info(
        f"Resync complete: YES={balance_result.yes_balance} NO={balance_result.no_balance} "
        f"orders={order_result.orders_synced}"
    )

    return SyncResult(
        success=True,
        yes_balance=balance_result.yes_balance,
        no_balance=balance_result.no_balance,
        orders_synced=order_result.orders_synced,
    )


def enter_resync_mode(
    state: "ExecutorState",
    reason: str,
    tombstone_retention_ms: int = 30000,
) -> None:
    """
    Enter RESYNCING mode.

    - Sets mode to RESYNCING
    - Sets all slots to RESYNCING state
    - Tombstones current orders for orphan fill accounting

    Args:
        state: ExecutorState to update
        reason: Reason for resync (for logging)
        tombstone_retention_ms: How long to keep tombstones
    """
    from ..types import now_ms
    from .state import SlotState, ExecutorMode

    logger.warning(f"Entering resync mode: {reason}")
    ts = now_ms()

    # Set mode
    state.mode = ExecutorMode.RESYNCING

    # Set slot states
    state.bid_slot.state = SlotState.RESYNCING
    state.ask_slot.state = SlotState.RESYNCING

    # Tombstone all current orders for orphan fill accounting
    expiry_ts = ts + tombstone_retention_ms

    for slot in [state.bid_slot, state.ask_slot]:
        for order_id, order in slot.orders.items():
            state.tombstoned_orders[order_id] = order
            state.tombstone_expiry_ts[order_id] = expiry_ts

    tombstoned_count = len(state.tombstoned_orders)
    logger.info(f"Tombstoned {tombstoned_count} orders for orphan fill accounting")


def is_tombstoned_order(state: "ExecutorState", order_id: str) -> bool:
    """Check if an order ID is in the tombstone map."""
    return order_id in state.tombstoned_orders


def get_tombstoned_order(
    state: "ExecutorState",
    order_id: str,
) -> Optional["WorkingOrder"]:
    """Get a tombstoned order by ID, if it exists."""
    return state.tombstoned_orders.get(order_id)


def cleanup_expired_tombstones(
    state: "ExecutorState",
    current_ts: int,
) -> int:
    """
    Clean up expired tombstones.

    Args:
        state: ExecutorState to clean
        current_ts: Current timestamp

    Returns:
        Number of tombstones removed
    """
    return state.clear_tombstones(current_ts, 0)  # retention already in expiry_ts


# Type stub for REST client (actual implementation in pm_rest_client.py)
class PolymarketRestClient:
    """Type stub for Polymarket REST client."""

    def get_balances(self) -> dict[str, float]:
        """Get token balances."""
        ...

    def get_open_orders(self, market_id: str) -> list[dict]:
        """Get open orders for a market."""
        ...
