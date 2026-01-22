"""Pricer for Container A."""

import logging
import math
from abc import ABC, abstractmethod
from typing import Optional

from .types import HourContext, PricerOutput

logger = logging.getLogger(__name__)


class Pricer(ABC):
    """
    Abstract base class for pricers.
    
    Turns features + context into fair YES probability/price and uncertainty.
    This is a PLUGGABLE interface - different pricing models can be swapped.
    """
    
    @property
    @abstractmethod
    def name(self) -> str:
        """Name of this pricer."""
        ...
    
    @property
    @abstractmethod
    def ready(self) -> bool:
        """Whether the pricer has enough data to produce output."""
        ...
    
    def update(self, ctx: HourContext, features: dict[str, float]) -> None:
        """
        Optional update method called with each new data point.
        
        Default implementation does nothing.
        
        Args:
            ctx: Current HourContext
            features: Current features
        """
        pass
    
    @abstractmethod
    def price(self, ctx: HourContext, features: dict[str, float]) -> PricerOutput:
        """
        Compute fair price/probability.
        
        Args:
            ctx: Current HourContext
            features: Current features
        
        Returns:
            PricerOutput with fair probability and optional bands
        """
        ...
    
    @abstractmethod
    def reset_for_new_hour(self, ctx: HourContext) -> None:
        """
        Reset pricer state for a new hour.
        
        Args:
            ctx: New HourContext
        """
        ...


class BaselinePricer(Pricer):
    """
    Baseline pricer implementation.
    
    Uses a simple model:
    - Computes probability based on current price vs open price
    - Accounts for time remaining using a simplified Gaussian model
    - Adjusts for volatility
    
    For hourly markets where YES wins if close > open:
    - P(YES) = Phi((current - open) / (vol * sqrt(t_remaining)))
    
    This is a placeholder - real implementations would use more
    sophisticated models (e.g., GBM, local vol, microstructure).
    """
    
    def __init__(
        self,
        base_vol: float = 0.02,  # Base hourly volatility
        vol_floor: float = 0.005,
        vol_cap: float = 0.10,
    ):
        """
        Initialize the pricer.
        
        Args:
            base_vol: Default volatility if not computed from data
            vol_floor: Minimum volatility to use
            vol_cap: Maximum volatility to use
        """
        self._base_vol = base_vol
        self._vol_floor = vol_floor
        self._vol_cap = vol_cap
        
        self._update_count = 0
        self._hour_id: Optional[str] = None
    
    @property
    def name(self) -> str:
        return "BaselinePricer"
    
    @property
    def ready(self) -> bool:
        return self._update_count >= 10
    
    def update(self, ctx: HourContext, features: dict[str, float]) -> None:
        """Track updates."""
        self._update_count += 1
    
    def _get_volatility(self, features: dict[str, float]) -> float:
        """Get volatility estimate from features."""
        # Prefer EWMA vol if available
        vol = features.get("ewma_vol", 0.0)
        
        if vol <= 0:
            vol = features.get("vol_1m", 0.0)
        
        if vol <= 0:
            vol = features.get("vol_5m", 0.0)
        
        if vol <= 0:
            vol = self._base_vol
        
        # Clamp to bounds
        return max(self._vol_floor, min(self._vol_cap, vol))
    
    @staticmethod
    def _norm_cdf(x: float) -> float:
        """
        Standard normal CDF approximation.
        
        Uses error function approximation.
        """
        return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))
    
    def price(self, ctx: HourContext, features: dict[str, float]) -> PricerOutput:
        """
        Compute fair YES probability.
        
        For a market where YES wins if close > open:
        P(YES) = Phi((log(current/open)) / (vol * sqrt(t)))
        
        Where t is time remaining as a fraction of the hour.
        """
        # Check if we have required data
        if ctx.open_price is None or ctx.open_price <= 0:
            return PricerOutput(p_yes_fair=0.5, ready=False)
        
        current_price = ctx.mid
        if current_price is None:
            current_price = ctx.last_trade_price
        
        if current_price is None or current_price <= 0:
            return PricerOutput(p_yes_fair=0.5, ready=False)
        
        # Time remaining as fraction of hour (0 to 1)
        t_remaining = ctx.t_remaining_ms / (60 * 60 * 1000)
        t_remaining = max(0.001, t_remaining)  # Avoid division by zero
        
        # Get volatility (hourly)
        vol = self._get_volatility(features)
        
        # Volatility scaled by sqrt of time remaining
        vol_scaled = vol * math.sqrt(t_remaining)
        
        # Avoid division by zero
        if vol_scaled < 0.0001:
            # At hour end with no vol, use step function
            p_yes = 1.0 if current_price > ctx.open_price else 0.0
            return PricerOutput(
                p_yes_fair=p_yes,
                p_yes_band_lo=p_yes,
                p_yes_band_hi=p_yes,
                ready=True,
            )
        
        # Log return from open
        log_return = math.log(current_price / ctx.open_price)
        
        # Z-score
        z = log_return / vol_scaled
        
        # Probability
        p_yes = self._norm_cdf(z)
        
        # Clamp to valid probability range
        p_yes = max(0.01, min(0.99, p_yes))
        
        # Uncertainty bands (±1 sigma move)
        # These represent where the probability could be if price moves
        z_lo = (log_return - vol_scaled) / vol_scaled
        z_hi = (log_return + vol_scaled) / vol_scaled
        
        p_yes_lo = max(0.01, min(0.99, self._norm_cdf(z_lo)))
        p_yes_hi = max(0.01, min(0.99, self._norm_cdf(z_hi)))
        
        return PricerOutput(
            p_yes_fair=p_yes,
            p_yes_band_lo=p_yes_lo,
            p_yes_band_hi=p_yes_hi,
            ready=self.ready,
        )
    
    def reset_for_new_hour(self, ctx: HourContext) -> None:
        """Reset for a new hour."""
        logger.info(f"Pricer: Resetting for hour {ctx.hour_id}")
        self._hour_id = ctx.hour_id
        self._update_count = 0
