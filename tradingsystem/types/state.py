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

    Tracks YES and NO token holdings.
    """
    I_yes: int = 0  # YES shares held
    I_no: int = 0   # NO shares held
    last_update_ts: int = 0

    @property
    def net_E(self) -> int:
        """
        Net exposure (settlement-relevant).

        Positive = net long YES, negative = net short YES.
        """
        return self.I_yes - self.I_no

    @property
    def gross_G(self) -> int:
        """
        Gross exposure (collateral-relevant).

        Total shares held regardless of side.
        """
        return self.I_yes + self.I_no

    def update_from_fill(
        self,
        token: Token,
        side: Side,
        size: int,
        ts: int,
    ) -> None:
        """Update inventory from a fill."""
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
