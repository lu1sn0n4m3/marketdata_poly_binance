"""
Configuration and market info types.

Types for executor configuration and market discovery.
"""

from dataclasses import dataclass
from time import time
from typing import Optional


@dataclass(slots=True)
class ExecutorConfig:
    """Configuration for Executor."""
    # Staleness thresholds
    pm_stale_threshold_ms: int = 500
    bn_stale_threshold_ms: int = 1000

    # Timeouts
    cancel_timeout_ms: int = 5000
    place_timeout_ms: int = 3000

    # Cooldowns
    cooldown_after_cancel_all_ms: int = 3000

    # Caps
    gross_cap: int = 1000
    max_position: int = 500

    # Tolerances for order matching
    price_tolerance_cents: int = 0
    size_tolerance_shares: int = 5


@dataclass(slots=True)
class MarketInfo:
    """
    Information about a discovered market.
    """
    condition_id: str
    question: str
    slug: str
    yes_token_id: str
    no_token_id: str
    end_time_utc_ms: int
    reference_price: Optional[float] = None  # BTC strike price from question

    @property
    def time_remaining_ms(self) -> int:
        """Time remaining until market end."""
        now_ms = int(time() * 1000)
        return max(0, self.end_time_utc_ms - now_ms)
