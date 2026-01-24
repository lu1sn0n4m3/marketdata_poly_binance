"""
Reconciliation effects for the executor.

Defines the effects (actions) produced by the reconciler.
Effects are executed by the actor against the gateway.
"""

from dataclasses import dataclass, field
from typing import Optional, Literal, TYPE_CHECKING

if TYPE_CHECKING:
    from ..types import RealOrderSpec
    from .planner import OrderKind


@dataclass
class ReconcileEffect:
    """
    Single action to take on the venue.

    Either a cancel or a place operation.
    """
    action: Literal["cancel", "place"]
    slot_type: Literal["bid", "ask"]

    # For cancel actions
    order_id: Optional[str] = None

    # For place actions
    spec: Optional["RealOrderSpec"] = None
    kind: Optional["OrderKind"] = None  # For matching with plan


@dataclass
class EffectBatch:
    """
    Batch of effects with ordering rules.

    CRITICAL INVARIANTS:
    1. Cancels execute before places
    2. If canceling a SELL, do NOT place replacement SELL in same batch
       (must wait for cancel ack due to inventory reservation on venue)

    The sell_blocked_until_cancel_ack flag indicates that SELL places
    were removed from this batch and should be placed after cancel acks.
    """
    cancels: list[ReconcileEffect] = field(default_factory=list)
    places: list[ReconcileEffect] = field(default_factory=list)

    # True if we had to block SELL placement due to pending SELL cancel
    sell_blocked_until_cancel_ack: bool = False

    @property
    def is_empty(self) -> bool:
        """True if no effects in this batch."""
        return len(self.cancels) == 0 and len(self.places) == 0

    @property
    def total_effects(self) -> int:
        """Total number of effects."""
        return len(self.cancels) + len(self.places)

    def has_sell_cancel(self) -> bool:
        """Check if this batch contains a SELL order cancellation."""
        from ..types import Side
        for cancel in self.cancels:
            # We don't have direct access to the order spec in cancel effects
            # The caller should track this via was_sell in PendingCancel
            pass
        return False  # Caller should use slot.has_pending_sell_cancel() instead
