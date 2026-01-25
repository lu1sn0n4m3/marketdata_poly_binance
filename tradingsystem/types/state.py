"""
State management types.

Types for tracking inventory and risk state.
"""

from dataclasses import dataclass
from typing import Optional

from .core import Token, Side, QuoteMode


@dataclass(slots=True)
class InventoryState:
    """
    Current inventory state.

    Tracks YES and NO token holdings, including pending (MATCHED but not MINED).

    CRITICAL DISTINCTION:
    - Settled inventory (I_yes, I_no): Tokens that are MINED on blockchain.
      These can be SOLD because they're actually in the wallet.
    - Pending inventory (pending_yes, pending_no): Tokens that are MATCHED
      but not yet MINED. These CANNOT be sold yet, but the strategy should
      "see" them to avoid placing duplicate orders.

    For STRATEGY decisions (should I place another BID?):
        Use effective_yes/effective_no (settled + pending)
        This prevents duplicate bidding while fills are mining.

    For EXECUTOR SELL orders (how much can I sell?):
        Use I_yes/I_no ONLY (settled inventory)
        Can't sell tokens that aren't in the wallet yet.
    """
    # Settled inventory (MINED on blockchain - can SELL these)
    I_yes: int = 0  # YES shares actually held in wallet
    I_no: int = 0   # NO shares actually held in wallet

    # Pending inventory (MATCHED but not MINED - can't SELL yet)
    pending_yes: int = 0  # YES shares matched, awaiting mining
    pending_no: int = 0   # NO shares matched, awaiting mining

    last_update_ts: int = 0

    @property
    def net_E(self) -> int:
        """
        Net exposure (settlement-relevant) - SETTLED only.

        Positive = net long YES, negative = net short YES.
        """
        return self.I_yes - self.I_no

    @property
    def gross_G(self) -> int:
        """
        Gross exposure (collateral-relevant) - SETTLED only.

        Total shares held regardless of side.
        """
        return self.I_yes + self.I_no

    @property
    def effective_yes(self) -> int:
        """
        Effective YES position for STRATEGY decisions.

        Includes pending fills so strategy doesn't place duplicate bids.
        """
        return self.I_yes + self.pending_yes

    @property
    def effective_no(self) -> int:
        """
        Effective NO position for STRATEGY decisions.

        Includes pending fills so strategy doesn't place duplicate bids.
        """
        return self.I_no + self.pending_no

    @property
    def effective_net_E(self) -> int:
        """
        Effective net exposure for STRATEGY decisions.

        Includes pending fills.
        """
        return self.effective_yes - self.effective_no

    @property
    def effective_gross_G(self) -> int:
        """
        Effective gross exposure for STRATEGY decisions.

        Includes pending fills.
        """
        return self.effective_yes + self.effective_no

    def update_from_fill(
        self,
        token: Token,
        side: Side,
        size: int,
        ts: int,
        is_pending: bool = False,
    ) -> None:
        """
        Update inventory from a fill.

        This method handles fills that don't go through the MATCHED->MINED cycle
        (e.g., backward compatibility or direct updates).

        For the MATCHED->MINED cycle, use:
        - update_from_fill(..., is_pending=True) for MATCHED
        - settle_pending() for MINED

        Args:
            token: YES or NO
            side: BUY or SELL
            size: Number of shares
            ts: Timestamp
            is_pending: If True, this is a MATCHED fill (not yet MINED).
                       Only tracks pending for BUY fills.
        """
        if is_pending:
            # MATCHED fill
            if side == Side.BUY:
                # BUY MATCHED: tokens coming in, add to pending
                if token == Token.YES:
                    self.pending_yes += size
                else:
                    self.pending_no += size
            # SELL MATCHED: tokens still in wallet, just reserved for transfer
            # Don't change inventory - will decrement at MINED time
        else:
            # Settled/MINED fill - update settled inventory directly
            if token == Token.YES:
                if side == Side.BUY:
                    self.I_yes += size
                else:
                    self.I_yes -= size
            else:  # NO
                if side == Side.BUY:
                    self.I_no += size
                else:
                    self.I_no -= size
        self.last_update_ts = ts

    def settle_pending(
        self,
        token: Token,
        side: Side,
        size: int,
        ts: int,
    ) -> None:
        """
        Settle a pending fill (when MINED arrives after MATCHED).

        This is called when a MINED notification arrives for a trade
        that was previously MATCHED.

        For BUY: Move from pending to settled (tokens arrived in wallet).
        For SELL: Decrement settled inventory (tokens left wallet).

        Args:
            token: YES or NO
            side: BUY or SELL
            size: Number of shares
            ts: Timestamp
        """
        if side == Side.BUY:
            # BUY MINED: tokens arrived, move from pending to settled
            if token == Token.YES:
                self.pending_yes = max(0, self.pending_yes - size)
                self.I_yes += size
            else:
                self.pending_no = max(0, self.pending_no - size)
                self.I_no += size
        else:
            # SELL MINED: tokens left wallet, decrement settled
            if token == Token.YES:
                self.I_yes -= size
            else:
                self.I_no -= size
        self.last_update_ts = ts


@dataclass(slots=True)
class RiskState:
    """
    Risk management state tracked by Executor.
    """
    cooldown_until_ts: int = 0
    mode_override: Optional[QuoteMode] = None
    stale_pm: bool = False
    stale_bn: bool = False
    gross_cap_hit: bool = False
    last_cancel_all_ts: int = 0
    error_burst_counter: int = 0

    def is_in_cooldown(self, now_ms: int) -> bool:
        """Check if currently in cooldown."""
        return now_ms < self.cooldown_until_ts

    def set_cooldown(self, now_ms: int, duration_ms: int) -> None:
        """Set cooldown period."""
        self.cooldown_until_ts = now_ms + duration_ms
