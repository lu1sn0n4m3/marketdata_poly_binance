"""
PnL tracking for the executor.

Tracks fills and calculates realized/unrealized PnL using FIFO accounting.
"""

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..types import Token, Side


@dataclass
class FillRecord:
    """Record of a single fill for PnL tracking."""
    ts: int
    token: "Token"
    side: "Side"
    size: int
    price: int  # cents
    order_id: str


@dataclass
class PnLTracker:
    """
    Tracks PnL from fills.

    Uses FIFO accounting for realized PnL calculation.
    All values in cents (1 cent = 0.01 USDC).
    """
    # Fill history
    fills: list[FillRecord] = field(default_factory=list)

    # Running totals
    total_buy_value: int = 0      # Total spent buying (cents)
    total_buy_shares: int = 0     # Total shares bought
    total_sell_value: int = 0     # Total received selling (cents)
    total_sell_shares: int = 0    # Total shares sold

    # Realized PnL (from closed roundtrips)
    realized_pnl_cents: int = 0

    # For FIFO cost tracking: list of (price, remaining_size)
    # Positive = long position (bought), negative = short position (sold)
    cost_basis_queue: list[tuple[int, int]] = field(default_factory=list)

    def record_fill(
        self,
        ts: int,
        token: "Token",
        side: "Side",
        size: int,
        price: int,
        order_id: str,
    ) -> tuple[int, int]:
        """
        Record a fill and update PnL.

        Returns: (realized_pnl_this_fill, current_avg_cost_basis)
        """
        from ..types import Side, Token

        self.fills.append(FillRecord(ts, token, side, size, price, order_id))

        # Convert to YES-space for consistent accounting
        # NO token trades are converted: price becomes (100 - price)
        # and the side is effectively flipped
        #
        # BUY YES at P  = add long YES at cost P
        # SELL YES at P = close long YES, receive P
        # BUY NO at P   = add short YES at effective price (100-P)
        # SELL NO at P  = close short YES at effective price (100-P)

        if token == Token.NO:
            # Convert NO to YES-space
            effective_price = 100 - price
            # BUY NO = short YES, SELL NO = cover short (like buying YES)
            is_adding_long = (side == Side.SELL)  # SELL NO = add long YES exposure
        else:
            effective_price = price
            is_adding_long = (side == Side.BUY)   # BUY YES = add long YES exposure

        fill_value = size * price  # Original value for volume tracking
        realized_this_fill = 0

        if is_adding_long:
            self.total_buy_value += fill_value
            self.total_buy_shares += size

            # Check if closing short position (FIFO)
            remaining = size
            while remaining > 0 and self.cost_basis_queue and self.cost_basis_queue[0][1] < 0:
                cost_price, cost_sz = self.cost_basis_queue[0]
                close_sz = min(remaining, abs(cost_sz))
                # PnL = (sold_price - buy_price) * shares
                # We shorted at cost_price, now covering at effective_price
                realized_this_fill += (cost_price - effective_price) * close_sz
                remaining -= close_sz
                if close_sz == abs(cost_sz):
                    self.cost_basis_queue.pop(0)
                else:
                    self.cost_basis_queue[0] = (cost_price, cost_sz + close_sz)

            # Remaining is new long position
            if remaining > 0:
                self.cost_basis_queue.append((effective_price, remaining))
        else:  # Reducing long / adding short
            self.total_sell_value += fill_value
            self.total_sell_shares += size

            # Check if closing long position (FIFO)
            remaining = size
            while remaining > 0 and self.cost_basis_queue and self.cost_basis_queue[0][1] > 0:
                cost_price, cost_sz = self.cost_basis_queue[0]
                close_sz = min(remaining, cost_sz)
                # PnL = (sell_price - cost_price) * shares
                realized_this_fill += (effective_price - cost_price) * close_sz
                remaining -= close_sz
                if close_sz == cost_sz:
                    self.cost_basis_queue.pop(0)
                else:
                    self.cost_basis_queue[0] = (cost_price, cost_sz - close_sz)

            # Remaining is new short position
            if remaining > 0:
                self.cost_basis_queue.append((effective_price, -remaining))

        self.realized_pnl_cents += realized_this_fill

        # Calculate average cost basis
        avg_cost = self._avg_cost_basis()

        return realized_this_fill, avg_cost

    def _avg_cost_basis(self) -> int:
        """Get average cost basis of open position (cents)."""
        total_cost = 0
        total_sz = 0
        for price, sz in self.cost_basis_queue:
            total_cost += price * abs(sz)
            total_sz += abs(sz)
        return total_cost // total_sz if total_sz > 0 else 0

    def get_position(self) -> int:
        """Get current position from cost basis queue."""
        return sum(sz for _, sz in self.cost_basis_queue)

    def get_unrealized_pnl(self, mark_price: int) -> int:
        """Get unrealized PnL at given mark price (cents)."""
        unrealized = 0
        for cost_price, sz in self.cost_basis_queue:
            if sz > 0:  # Long
                unrealized += (mark_price - cost_price) * sz
            else:  # Short
                unrealized += (cost_price - mark_price) * abs(sz)
        return unrealized

    def get_summary(self, mark_price: int = 50) -> dict:
        """Get PnL summary."""
        position = self.get_position()
        unrealized = self.get_unrealized_pnl(mark_price)
        total_pnl = self.realized_pnl_cents + unrealized

        return {
            "fills_count": len(self.fills),
            "total_bought": self.total_buy_shares,
            "total_sold": self.total_sell_shares,
            "avg_buy_px": self.total_buy_value / self.total_buy_shares if self.total_buy_shares else 0,
            "avg_sell_px": self.total_sell_value / self.total_sell_shares if self.total_sell_shares else 0,
            "position": position,
            "avg_cost": self._avg_cost_basis(),
            "realized_pnl_cents": self.realized_pnl_cents,
            "unrealized_pnl_cents": unrealized,
            "total_pnl_cents": total_pnl,
            "realized_pnl_usd": self.realized_pnl_cents / 100,
            "total_pnl_usd": total_pnl / 100,
        }
