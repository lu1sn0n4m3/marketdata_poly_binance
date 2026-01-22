"""Market scheduler for hourly session management."""

import logging
from typing import Optional
from time import time_ns

from .types import MarketInfo, CanonicalStateView, SessionState
from .gamma_client import GammaClient

try:
    from shared.hourmm_common.time import (
        get_hour_id,
        get_hour_boundaries_ms,
        ms_until_hour_end,
    )
except ImportError:
    import sys
    sys.path.insert(0, "/Users/luissafar/dev/marketdata_poly_binance")
    from shared.hourmm_common.time import (
        get_hour_id,
        get_hour_boundaries_ms,
        ms_until_hour_end,
    )

logger = logging.getLogger(__name__)


class MarketScheduler:
    """
    Responsible for session selection and transitions.
    
    Selects the current active hour market and manages transitions
    to the next hour market.
    """
    
    def __init__(
        self,
        gamma: GammaClient,
        market_slug: str,
    ):
        """
        Initialize the scheduler.
        
        Args:
            gamma: GammaClient for market discovery
            market_slug: Base market slug to use
        """
        self.gamma = gamma
        self.market_slug = market_slug
        
        self._active_market: Optional[MarketInfo] = None
        self._active_hour_id: Optional[str] = None
    
    @property
    def active_market(self) -> Optional[MarketInfo]:
        """Currently active market."""
        return self._active_market
    
    @property
    def active_hour_id(self) -> Optional[str]:
        """Currently active hour ID."""
        return self._active_hour_id
    
    async def select_market_for_now(self, now_ms: int) -> Optional[MarketInfo]:
        """
        Select the market for the current time.
        
        Args:
            now_ms: Current timestamp in milliseconds
        
        Returns:
            MarketInfo for the current hour, or None if not found
        """
        market = await self.gamma.get_current_hour_market(
            market_slug=self.market_slug,
            now_ms=now_ms,
        )
        
        if market:
            self._active_market = market
            self._active_hour_id = get_hour_id(now_ms)
            logger.info(
                f"Selected market for {self._active_hour_id}: "
                f"{market.market_slug} ({market.condition_id[:16]}...)"
            )
        else:
            logger.warning(f"No market found for {self.market_slug} at {now_ms}")
        
        return market
    
    def should_roll_market(
        self,
        now_ms: int,
        state: Optional[CanonicalStateView] = None,
    ) -> bool:
        """
        Check if we should roll to a new market.
        
        Args:
            now_ms: Current timestamp in milliseconds
            state: Current state view (optional)
        
        Returns:
            True if we should roll to a new market
        """
        current_hour_id = get_hour_id(now_ms)
        
        # Roll if hour has changed
        if self._active_hour_id != current_hour_id:
            return True
        
        # Roll if state indicates DONE
        if state and state.session_state == SessionState.DONE:
            return True
        
        return False
    
    async def roll_to_next_market(self, now_ms: int) -> Optional[MarketInfo]:
        """
        Roll to the next hourly market.
        
        Args:
            now_ms: Current timestamp in milliseconds
        
        Returns:
            New MarketInfo, or None if not found
        """
        old_hour_id = self._active_hour_id
        new_market = await self.select_market_for_now(now_ms)
        
        if new_market:
            logger.info(f"Rolled market: {old_hour_id} -> {self._active_hour_id}")
        else:
            logger.warning(f"Failed to roll market from {old_hour_id}")
        
        return new_market
    
    def get_session_phase(
        self,
        now_ms: int,
        t_wind_ms: int,
        t_flatten_ms: int,
        hard_cutoff_ms: int,
    ) -> SessionState:
        """
        Determine the session phase based on time remaining.
        
        Args:
            now_ms: Current timestamp
            t_wind_ms: Time before hour end to start WIND_DOWN
            t_flatten_ms: Time before hour end to start FLATTEN
            hard_cutoff_ms: Time before hour end to go DONE
        
        Returns:
            SessionState for the current phase
        """
        t_remaining_ms = ms_until_hour_end(now_ms)
        
        if t_remaining_ms <= hard_cutoff_ms:
            return SessionState.DONE
        elif t_remaining_ms <= t_flatten_ms:
            return SessionState.FLATTEN
        elif t_remaining_ms <= t_wind_ms:
            return SessionState.WIND_DOWN
        else:
            return SessionState.ACTIVE
