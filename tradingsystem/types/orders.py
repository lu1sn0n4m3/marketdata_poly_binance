"""
Order types.

Types for order specification and lifecycle tracking.
"""

from dataclasses import dataclass

from .core import Token, Side, OrderStatus


@dataclass(slots=True)
class RealOrderSpec:
    """
    Specification for a real order to be placed.

    This is what gets sent to the exchange.
    """
    token: Token
    side: Side
    px: int            # Price in cents
    sz: int            # Size in shares
    token_id: str = ""  # Actual token ID for the exchange
    time_in_force: str = "GTC"
    client_order_id: str = ""

    def matches(self, other: "RealOrderSpec", price_tol: int = 0, size_tol: int = 5) -> bool:
        """Check if this order matches another within tolerance."""
        if self.token != other.token:
            return False
        if self.side != other.side:
            return False
        if abs(self.px - other.px) > price_tol:
            return False
        if abs(self.sz - other.sz) > size_tol:
            return False
        return True


@dataclass(slots=True)
class WorkingOrder:
    """
    A working order on the exchange.

    Tracks order state and filled quantity.

    The `kind` field stores the order's role as assigned by the planner:
    - "reduce_sell": Selling held inventory to reduce exposure
    - "open_buy": Buying primary token to open exposure (BUY YES for bid)
    - "complement_buy": Buying complement token (BUY NO for ask)
    - None: Unknown (for orders synced from exchange)

    The reconciler uses this to match working orders with planned orders.
    """
    client_order_id: str
    server_order_id: str
    order_spec: RealOrderSpec
    status: OrderStatus
    created_ts: int
    last_state_change_ts: int
    filled_sz: int = 0
    kind: str | None = None  # Order role: "reduce_sell", "open_buy", "complement_buy", or None

    @property
    def remaining_sz(self) -> int:
        """Remaining unfilled size."""
        return max(0, self.order_spec.sz - self.filled_sz)

    @property
    def is_terminal(self) -> bool:
        """Check if order is in a terminal state."""
        return self.status in (
            OrderStatus.FILLED,
            OrderStatus.CANCELED,
            OrderStatus.REJECTED,
        )
