"""
Default market-making strategy implementation.

This is a production-ready strategy that demonstrates:
- Fair value based quoting
- Position-based skew (lean against inventory)
- Volatility-aware spread adjustment
- Near-expiry behavior modification
"""

import logging
from typing import Optional

from ..types import (
    QuoteMode,
    DesiredQuoteSet,
    DesiredQuoteLeg,
    now_ms,
)
from .base import Strategy, StrategyInput, StrategyConfig

logger = logging.getLogger(__name__)


class DefaultMMStrategy(Strategy):
    """
    Default market-making strategy.

    This strategy quotes around a fair value with position-based skew
    and volatility-aware spread adjustment.

    Features:
        - Mid-market quoting around fair value (from pricer/Binance)
        - Position-based skew: leans prices to reduce inventory
        - Volatility spread adjustment: widens in high vol
        - Near-expiry behavior: tighter spreads, smaller sizes
        - Position limits: goes one-sided at max position

    How it works:
        1. Get fair value from StrategyInput (comes from pricer)
        2. Compute spread based on volatility and time to expiry
        3. Compute skew based on current position
        4. Place bid at (fair - skew - spread/2)
        5. Place ask at (fair - skew + spread/2)

    Configuration:
        Pass a StrategyConfig to customize parameters.
        See StrategyConfig for all available options.

    Example:
        config = StrategyConfig(
            base_spread_cents=4,
            max_position=200,
        )
        strategy = DefaultMMStrategy(config)
    """

    def __init__(self, config: Optional[StrategyConfig] = None):
        """
        Initialize with optional configuration.

        Args:
            config: Strategy configuration. Uses defaults if not provided.
        """
        self.config = config or StrategyConfig()

    def compute_quotes(self, inp: StrategyInput) -> DesiredQuoteSet:
        """Compute desired quotes based on current market state."""
        ts = now_ms()

        # Check data validity
        if not inp.is_data_valid:
            return DesiredQuoteSet.stop(
                ts=ts,
                pm_seq=inp.pm_seq,
                bn_seq=inp.bn_seq,
                reason="NO_DATA",
            )

        # Very near expiry - stop quoting entirely
        if inp.very_near_expiry:
            return DesiredQuoteSet.stop(
                ts=ts,
                pm_seq=inp.pm_seq,
                bn_seq=inp.bn_seq,
                reason="EXPIRY_IMMINENT",
            )

        # Compute spread
        spread = self._compute_spread(inp)
        half_spread = spread // 2

        # Compute skew based on position
        skew = self._compute_skew(inp)

        # Compute fair price with skew
        fair = inp.fair_px_cents
        mid_with_skew = fair - skew  # Positive skew pushes quotes down

        # Compute bid and ask prices
        bid_px = max(1, mid_with_skew - half_spread)
        ask_px = min(99, mid_with_skew + half_spread)

        # Ensure bid < ask
        if bid_px >= ask_px:
            ask_px = min(99, bid_px + 1)

        # Compute sizes
        bid_sz, ask_sz = self._compute_sizes(inp)

        # Determine mode (normal, one-sided, etc.)
        mode = self._determine_mode(inp)

        # Build reason flags for logging
        reason_flags = set()
        if inp.near_expiry:
            reason_flags.add("NEAR_EXPIRY")
        if abs(skew) > 0:
            reason_flags.add("POSITION_SKEW")

        bid_enabled = mode in (QuoteMode.NORMAL, QuoteMode.ONE_SIDED_BUY)
        ask_enabled = mode in (QuoteMode.NORMAL, QuoteMode.ONE_SIDED_SELL)

        return DesiredQuoteSet(
            created_at_ts=ts,
            pm_seq=inp.pm_seq,
            bn_seq=inp.bn_seq,
            mode=mode,
            bid_yes=DesiredQuoteLeg(
                enabled=bid_enabled and bid_sz > 0,
                px_yes=bid_px,
                sz=bid_sz,
            ),
            ask_yes=DesiredQuoteLeg(
                enabled=ask_enabled and ask_sz > 0,
                px_yes=ask_px,
                sz=ask_sz,
            ),
            reason_flags=reason_flags,
        )

    def _compute_spread(self, inp: StrategyInput) -> int:
        """
        Compute bid-ask spread based on conditions.

        Spread is affected by:
        - Base spread from config
        - Volatility (wider in high vol)
        - Time to expiry (tighter near expiry)
        """
        cfg = self.config
        spread = cfg.base_spread_cents

        # Volatility adjustment
        if inp.bn_snap and inp.bn_snap.shock_z is not None:
            if abs(inp.bn_snap.shock_z) > cfg.high_vol_threshold:
                spread = int(spread * cfg.vol_spread_multiplier)

        # Near expiry - tighter spreads (more aggressive)
        if inp.near_expiry:
            spread = int(spread * cfg.expiry_spread_multiplier)

        # Clamp to bounds
        return max(cfg.min_spread_cents, min(cfg.max_spread_cents, spread))

    def _compute_skew(self, inp: StrategyInput) -> int:
        """
        Compute position-based skew (cents).

        Skew pushes quotes in the direction that would reduce inventory.
        If long, skew is positive -> quotes move down -> more likely to sell.
        If short, skew is negative -> quotes move up -> more likely to buy.

        IMPORTANT: Uses EFFECTIVE inventory (settled + pending) so that
        pending fills from MATCHED orders are considered. This prevents
        duplicate bids while fills are being mined on the blockchain.
        """
        cfg = self.config
        # Use effective_net_E to include pending inventory
        # This prevents placing duplicate bids after MATCHED but before MINED
        net_pos = inp.inventory.effective_net_E

        # Skew per share of net position
        skew = int(net_pos * cfg.skew_per_share_cents)

        # Clamp to max skew
        return max(-cfg.max_skew_cents, min(cfg.max_skew_cents, skew))

    def _compute_sizes(self, inp: StrategyInput) -> tuple[int, int]:
        """
        Compute bid and ask sizes.

        Sizes are affected by:
        - Base size from config
        - Time to expiry (smaller near expiry)
        - Position (reduce size on side that would increase exposure)

        IMPORTANT: Uses EFFECTIVE inventory (settled + pending) so that
        pending fills are considered. This prevents oversized bids
        while fills are being mined on the blockchain.
        """
        cfg = self.config
        base = cfg.base_size

        # Near expiry - reduce size
        if inp.near_expiry:
            base = int(base * cfg.expiry_size_reduction)

        # Position-based size adjustment
        # Use effective_net_E to include pending inventory
        net_pos = inp.inventory.effective_net_E

        # Reduce bid size if already long (including pending)
        if net_pos > 0:
            bid_reduction = min(1.0, net_pos / cfg.max_position)
            bid_sz = int(base * (1 - bid_reduction * 0.5))
        else:
            bid_sz = base

        # Reduce ask size if already short (including pending)
        if net_pos < 0:
            ask_reduction = min(1.0, -net_pos / cfg.max_position)
            ask_sz = int(base * (1 - ask_reduction * 0.5))
        else:
            ask_sz = base

        # Clamp to bounds
        bid_sz = max(cfg.min_size, min(cfg.max_size, bid_sz))
        ask_sz = max(cfg.min_size, min(cfg.max_size, ask_sz))

        return bid_sz, ask_sz

    def _determine_mode(self, inp: StrategyInput) -> QuoteMode:
        """
        Determine quoting mode based on conditions.

        At max position, goes one-sided to avoid increasing exposure.

        IMPORTANT: Uses EFFECTIVE inventory (settled + pending) so that
        pending fills are considered. This prevents exceeding max position
        while fills are being mined on the blockchain.
        """
        cfg = self.config
        # Use effective_net_E to include pending inventory
        net_pos = inp.inventory.effective_net_E

        # At max long position (including pending) - only sell
        if net_pos >= cfg.max_position:
            return QuoteMode.ONE_SIDED_SELL

        # At max short position (including pending) - only buy
        if net_pos <= -cfg.max_position:
            return QuoteMode.ONE_SIDED_BUY

        return QuoteMode.NORMAL
