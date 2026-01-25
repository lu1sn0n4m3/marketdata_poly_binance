"""
Executor state types.

Defines the core state structures for the executor:
- ReservationLedger: Tracks inventory reserved by pending SELL orders
- OrderSlot: State machine for bid/ask order management
- ExecutorState: Complete executor state
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, Literal, TYPE_CHECKING

if TYPE_CHECKING:
    from ..types import Token, Side, RealOrderSpec, WorkingOrder, DesiredQuoteSet


class SlotState(Enum):
    """
    State machine states for an OrderSlot.

    IDLE:       No pending operations, can accept new reconcile effects
    CANCELING:  Waiting for cancel ack(s) - block all new submissions
    PLACING:    Waiting for place ack(s) - block all new submissions
    RESYNCING:  Blocked during resync - all operations suspended
    """
    IDLE = "idle"
    CANCELING = "canceling"
    PLACING = "placing"
    RESYNCING = "resyncing"


class ExecutorMode(Enum):
    """
    Overall executor mode.

    NORMAL:    Normal operation - strategy intents processed
    RESYNCING: Resync in progress - block new order submissions
    CLOSING:   Emergency close in progress - cancel all, then close positions
    STOPPED:   Terminal state - no more trading, requires restart

    State transitions:
        NORMAL → RESYNCING → NORMAL (resync cycle)
        NORMAL → CLOSING → STOPPED (emergency close)
        RESYNCING → CLOSING → STOPPED (emergency close during resync)
    """
    NORMAL = "normal"
    RESYNCING = "resyncing"
    CLOSING = "closing"
    STOPPED = "stopped"


@dataclass
class PendingPlace:
    """Tracking for pending order placement."""
    action_id: str
    spec: "RealOrderSpec"
    sent_at_ts: int
    order_kind: str  # "reduce_sell", "open_buy", or "complement_buy"


@dataclass
class PendingCancel:
    """Tracking for pending cancellation."""
    action_id: str
    server_order_id: str
    sent_at_ts: int
    was_sell: bool  # True if canceling a SELL order


@dataclass
class OrderSlot:
    """
    Represents one side's quoting (bid or ask).

    State machine invariant:
    - IDLE: Can submit new operations
    - CANCELING/PLACING: Block all new submissions until resolved
    - RESYNCING: Block all until resync completes

    Can have 0, 1, or 2 real orders:
    - 0: No quote active
    - 1: Simple case (BUY YES, SELL NO, etc.)
    - 2: Split case (SELL YES K + BUY NO (S-K))

    Key rule: At most ONE "mutating wave" per slot at a time.
    """
    slot_type: Literal["bid", "ask"]
    state: SlotState = SlotState.IDLE

    # Working orders owned by this slot (server_order_id -> WorkingOrder)
    orders: dict[str, "WorkingOrder"] = field(default_factory=dict)

    # Pending actions for this slot
    pending_places: dict[str, PendingPlace] = field(default_factory=dict)
    pending_cancels: dict[str, PendingCancel] = field(default_factory=dict)

    # Track if we're waiting for a SELL cancel before placing replacement SELL
    sell_blocked_until_cancel_ack: bool = False

    # Rate limiting: last time we submitted any operation (place or cancel)
    last_submit_ts: int = 0

    def can_submit(self) -> bool:
        """Only allow submissions in IDLE state."""
        return self.state == SlotState.IDLE

    def is_rate_limited(self, current_ts: int, min_interval_ms: int) -> bool:
        """
        Check if enough time has passed since last submission.

        Returns True if we're rate limited (should NOT reconcile yet).
        Returns False if it's ok to reconcile.
        """
        if min_interval_ms <= 0:
            return False
        elapsed = current_ts - self.last_submit_ts
        return elapsed < min_interval_ms

    def record_submission(self, ts: int) -> None:
        """Record that we submitted an operation at this timestamp."""
        self.last_submit_ts = ts

    def has_pending_sell_cancel(self) -> bool:
        """Check if there's a pending cancel for a SELL order."""
        return any(pc.was_sell for pc in self.pending_cancels.values())

    def transition_to_canceling(self) -> None:
        """Transition to CANCELING state."""
        if self.state != SlotState.IDLE:
            raise ValueError(f"Cannot transition to CANCELING from {self.state}")
        self.state = SlotState.CANCELING

    def transition_to_placing(self) -> None:
        """Transition to PLACING state."""
        if self.state != SlotState.IDLE:
            raise ValueError(f"Cannot transition to PLACING from {self.state}")
        self.state = SlotState.PLACING

    def on_ack_received(self) -> None:
        """
        Called when a pending operation completes.

        If all pending operations are done, transition back to IDLE.
        """
        if not self.pending_places and not self.pending_cancels:
            if self.state not in (SlotState.RESYNCING,):
                self.state = SlotState.IDLE
                self.sell_blocked_until_cancel_ack = False

    def get_total_reserved(self, token: "Token") -> int:
        """
        Get total inventory reserved by SELL orders in this slot.

        Uses remaining_sz (not original) for accurate reservation.
        """
        from ..types import Side

        total = 0
        for order in self.orders.values():
            if order.order_spec.side == Side.SELL and order.order_spec.token == token:
                total += order.remaining_sz
        return total

    def clear(self) -> None:
        """Clear all state (for resync)."""
        self.orders.clear()
        self.pending_places.clear()
        self.pending_cancels.clear()
        self.sell_blocked_until_cancel_ack = False
        self.state = SlotState.IDLE


@dataclass
class ReservationLedger:
    """
    Tracks inventory reserved by live SELL orders.

    IMPORTANT: This is a VIEW computed from working orders.
    Use incremental updates for performance, but always support
    full rebuild from working orders via rebuild_from_slots().

    CRITICAL: Available inventory uses SETTLED inventory only!
    Pending inventory (MATCHED but not MINED) cannot be sold because
    tokens are not yet in the wallet. The planner should pass
    inventory.I_yes/I_no (settled) NOT inventory.effective_yes/no.

    Available inventory = settled - reserved - safety_buffer
    """
    reserved_yes: int = 0  # YES shares reserved by SELL YES orders
    reserved_no: int = 0   # NO shares reserved by SELL NO orders

    # Safety buffer for production (0 in paper mode)
    safety_buffer_yes: int = 0
    safety_buffer_no: int = 0

    def available_yes(self, settled_yes: int) -> int:
        """
        Calculate available YES inventory for new SELL orders.

        Safe available = settled - reserved - buffer
        """
        return max(0, settled_yes - self.reserved_yes - self.safety_buffer_yes)

    def available_no(self, settled_no: int) -> int:
        """
        Calculate available NO inventory for new SELL orders.

        Safe available = settled - reserved - buffer
        """
        return max(0, settled_no - self.reserved_no - self.safety_buffer_no)

    def reserve_yes(self, size: int) -> None:
        """Reserve YES inventory for a pending SELL YES."""
        self.reserved_yes += size

    def release_yes(self, size: int) -> None:
        """Release YES reservation (after fill or cancel)."""
        self.reserved_yes = max(0, self.reserved_yes - size)

    def reserve_no(self, size: int) -> None:
        """Reserve NO inventory for a pending SELL NO."""
        self.reserved_no += size

    def release_no(self, size: int) -> None:
        """Release NO reservation (after fill or cancel)."""
        self.reserved_no = max(0, self.reserved_no - size)

    @classmethod
    def rebuild_from_slots(
        cls,
        slots: list[OrderSlot],
        safety_buffer: int = 0,
    ) -> "ReservationLedger":
        """
        Rebuild ledger from working orders in slots.

        Called after:
        - resync
        - cancel-all
        - any integrity incident
        - optionally on timer tick for verification

        Uses REMAINING size (not original) for accurate reservation.
        """
        from ..types import Token, Side

        reserved_yes = 0
        reserved_no = 0

        for slot in slots:
            for order in slot.orders.values():
                if order.order_spec.side == Side.SELL:
                    remaining = order.remaining_sz
                    if order.order_spec.token == Token.YES:
                        reserved_yes += remaining
                    else:
                        reserved_no += remaining

        return cls(
            reserved_yes=reserved_yes,
            reserved_no=reserved_no,
            safety_buffer_yes=safety_buffer,
            safety_buffer_no=safety_buffer,
        )


@dataclass
class ExecutorState:
    """
    Complete executor state.

    All mutable state owned by the Executor.
    Single-writer: only the ExecutorActor modifies this.
    """
    # Overall mode
    mode: ExecutorMode = ExecutorMode.NORMAL

    # Inventory (settled positions)
    inventory: "InventoryState" = field(default_factory=lambda: _default_inventory())

    # Reservation ledger (derived from working orders)
    reservations: ReservationLedger = field(default_factory=ReservationLedger)

    # Order slots (one per side)
    bid_slot: OrderSlot = field(default_factory=lambda: OrderSlot(slot_type="bid"))
    ask_slot: OrderSlot = field(default_factory=lambda: OrderSlot(slot_type="ask"))

    # Risk state
    risk: "RiskState" = field(default_factory=lambda: _default_risk())

    # Last processed intent
    last_intent: Optional["DesiredQuoteSet"] = None
    last_intent_ts: int = 0

    # PnL tracking
    pnl: "PnLTracker" = field(default_factory=lambda: _default_pnl())

    # Tombstoned orders for orphan fill accounting during resync
    # Map of server_order_id -> WorkingOrder for recently-canceled orders
    tombstoned_orders: dict[str, "WorkingOrder"] = field(default_factory=dict)
    tombstone_expiry_ts: dict[str, int] = field(default_factory=dict)

    def get_all_working_orders(self) -> dict[str, "WorkingOrder"]:
        """Get all working orders across both slots."""
        result = {}
        result.update(self.bid_slot.orders)
        result.update(self.ask_slot.orders)
        return result

    def find_order(self, server_order_id: str) -> tuple[Optional[OrderSlot], Optional["WorkingOrder"]]:
        """Find an order by server ID, returning (slot, order) or (None, None)."""
        if server_order_id in self.bid_slot.orders:
            return self.bid_slot, self.bid_slot.orders[server_order_id]
        if server_order_id in self.ask_slot.orders:
            return self.ask_slot, self.ask_slot.orders[server_order_id]
        return None, None

    def rebuild_reservations(self, safety_buffer: int = 0) -> None:
        """Rebuild reservations from current working orders."""
        self.reservations = ReservationLedger.rebuild_from_slots(
            [self.bid_slot, self.ask_slot],
            safety_buffer=safety_buffer,
        )

    def clear_tombstones(self, current_ts: int, retention_ms: int) -> int:
        """
        Clear expired tombstones.

        Returns number of tombstones cleared.
        """
        expired = [
            order_id for order_id, expiry_ts in self.tombstone_expiry_ts.items()
            if current_ts > expiry_ts
        ]
        for order_id in expired:
            self.tombstoned_orders.pop(order_id, None)
            self.tombstone_expiry_ts.pop(order_id, None)
        return len(expired)


# Helper functions for default_factory to avoid circular imports
def _default_inventory():
    from ..types import InventoryState
    return InventoryState()


def _default_risk():
    from ..types import RiskState
    return RiskState()


def _default_pnl():
    from .pnl import PnLTracker
    return PnLTracker()
