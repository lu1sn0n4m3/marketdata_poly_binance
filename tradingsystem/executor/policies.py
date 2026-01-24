"""
Execution policies for the executor.

Defines policies for handling min-size constraints and dust positions.
"""

from dataclasses import dataclass
from enum import Enum


class MinSizePolicy(Enum):
    """
    Policy for handling sub-minimum order sizes.

    PASSIVE_FIRST: Don't place sub-min orders, keep as residual.
                   Let dust accumulate until it reaches min-size.
                   This is the safest default - no crossing, no extra risk.

    AGGREGATE:     Round up the whole target to min-size if feasible.
                   Use when you want to maintain quoting even with dust.
                   May increase position size beyond original intent.

    DUST_CLEANUP:  Use marketable orders to eliminate dust positions.
                   Only for emergency cleanup, not normal operation.
                   Handled separately from passive order planning.
    """
    PASSIVE_FIRST = "passive_first"
    AGGREGATE = "aggregate"
    DUST_CLEANUP = "dust_cleanup"


@dataclass
class ExecutorPolicies:
    """
    Configurable policies for the executor.

    Controls behavior for edge cases and venue constraints.
    """
    # Minimum order size (Polymarket requires 5)
    min_order_size: int = 5

    # Default policy for sub-min legs
    min_size_policy: MinSizePolicy = MinSizePolicy.PASSIVE_FIRST

    # Safety buffer subtracted from available inventory (0 in paper, >0 in prod)
    safety_buffer: int = 0

    # Price tolerance for order matching (cents)
    # Orders within this tolerance are considered "same price"
    price_tolerance: int = 0

    # Queue-preserving replacement policy:
    # - Price change: ALWAYS replace (price differs beyond tolerance)
    # - Size decrease: ALWAYS replace (risk reduction worth losing queue)
    # - Size increase: ONLY replace if increase >= top_up_threshold
    #
    # This prevents churning queue position on small size changes.
    # A partial fill (30→28) won't trigger replacement if strategy still wants 30.
    top_up_threshold: int = 10  # Only replace if size increase >= this

    # Cooldown after cancel-all (ms)
    cooldown_after_cancel_all_ms: int = 3000

    # Timeout for place/cancel operations (ms)
    place_timeout_ms: int = 5000
    cancel_timeout_ms: int = 5000

    # Maximum time to keep tombstoned orders for orphan fill matching (ms)
    tombstone_retention_ms: int = 30000
