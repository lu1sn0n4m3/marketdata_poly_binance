"""Feature engine for Container A."""

import logging
import math
from abc import ABC, abstractmethod
from collections import deque
from dataclasses import dataclass, field
from typing import Optional

from .types import BinanceEvent, BinanceEventType, HourContext

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class TradeRecord:
    """A trade record for feature computation."""
    price: float
    size: float
    ts_ms: int
    log_return: Optional[float] = None


class FeatureEngine(ABC):
    """
    Abstract base class for feature engines.
    
    Computes rolling features from Binance events.
    Only abstract where the computation might change.
    """
    
    @property
    @abstractmethod
    def feature_names(self) -> list[str]:
        """List of feature names this engine produces."""
        ...
    
    @property
    @abstractmethod
    def ready(self) -> bool:
        """Whether the engine has enough data to produce features."""
        ...
    
    @abstractmethod
    def update(self, event: BinanceEvent, ctx: HourContext) -> None:
        """
        Update features from a new event.
        
        Args:
            event: BinanceEvent (trade or BBO)
            ctx: Current HourContext
        """
        ...
    
    @abstractmethod
    def snapshot(self, ctx: HourContext) -> dict[str, float]:
        """
        Get current feature snapshot.
        
        Args:
            ctx: Current HourContext
        
        Returns:
            Dictionary of feature name -> value
        """
        ...
    
    @abstractmethod
    def reset_for_new_hour(self, ctx: HourContext) -> None:
        """
        Reset features for a new hour.
        
        Args:
            ctx: New HourContext
        """
        ...


class DefaultFeatureEngine(FeatureEngine):
    """
    Default feature engine implementation.
    
    Computes:
    - Realized volatility (multiple windows)
    - EWMA volatility
    - Log returns
    - Order imbalance
    - Price change from open
    """
    
    def __init__(
        self,
        vol_windows_ms: Optional[dict[str, int]] = None,
        ewma_alpha: float = 0.1,
        max_history_ms: int = 10 * 60 * 1000,  # 10 minutes
    ):
        """
        Initialize the feature engine.
        
        Args:
            vol_windows_ms: Dictionary of window name -> window size in ms
            ewma_alpha: Smoothing factor for EWMA (0 < alpha <= 1)
            max_history_ms: Maximum history to keep
        """
        self._vol_windows_ms = vol_windows_ms or {
            "vol_1m": 60 * 1000,
            "vol_5m": 5 * 60 * 1000,
        }
        self._ewma_alpha = ewma_alpha
        self._max_history_ms = max_history_ms
        
        # Trade history (circular buffer)
        self._trades: deque[TradeRecord] = deque(maxlen=10000)
        
        # Running state
        self._last_price: Optional[float] = None
        self._ewma_vol: Optional[float] = None
        self._trade_count: int = 0
        
        # Order imbalance
        self._buy_volume: float = 0.0
        self._sell_volume: float = 0.0
        
        # Hour-specific tracking
        self._hour_id: Optional[str] = None
    
    @property
    def feature_names(self) -> list[str]:
        """List of feature names."""
        names = list(self._vol_windows_ms.keys())
        names.extend([
            "ewma_vol",
            "log_return_1m",
            "order_imbalance",
            "pct_change_from_open",
            "trade_count",
        ])
        return names
    
    @property
    def ready(self) -> bool:
        """Whether we have enough data."""
        return self._trade_count >= 10
    
    def update(self, event: BinanceEvent, ctx: HourContext) -> None:
        """Update features from event."""
        if event.event_type == BinanceEventType.TRADE:
            self._update_from_trade(event)
        # BBO events don't directly update features but could be used
        # for microprice computation in a more advanced engine
    
    def _update_from_trade(self, event: BinanceEvent) -> None:
        """Update from a trade event."""
        if event.price is None or event.size is None:
            return
        
        price = event.price
        size = event.size
        ts_ms = event.ts_exchange_ms
        
        # Compute log return
        log_return = None
        if self._last_price is not None and self._last_price > 0:
            log_return = math.log(price / self._last_price)
            
            # Update EWMA volatility
            if self._ewma_vol is None:
                self._ewma_vol = abs(log_return)
            else:
                self._ewma_vol = (
                    self._ewma_alpha * abs(log_return) +
                    (1 - self._ewma_alpha) * self._ewma_vol
                )
        
        # Store trade
        trade = TradeRecord(
            price=price,
            size=size,
            ts_ms=ts_ms,
            log_return=log_return,
        )
        self._trades.append(trade)
        
        # Update imbalance
        if event.side == "buy":
            self._buy_volume += size
        else:
            self._sell_volume += size
        
        self._last_price = price
        self._trade_count += 1
        
        # Prune old trades
        self._prune_old_trades(ts_ms)
    
    def _prune_old_trades(self, now_ms: int) -> None:
        """Remove trades older than max history."""
        cutoff = now_ms - self._max_history_ms
        while self._trades and self._trades[0].ts_ms < cutoff:
            self._trades.popleft()
    
    def _compute_realized_vol(self, window_ms: int, now_ms: int) -> Optional[float]:
        """
        Compute realized volatility over a window.
        
        Uses sum of squared log returns, annualized.
        """
        cutoff = now_ms - window_ms
        returns = [
            t.log_return for t in self._trades
            if t.ts_ms >= cutoff and t.log_return is not None
        ]
        
        if len(returns) < 2:
            return None
        
        # Sum of squared returns
        sum_sq = sum(r * r for r in returns)
        
        # Annualize (assuming ~252 trading days, 24 hours)
        # Scale factor: sqrt(365.25 * 24 * 3600 * 1000 / window_ms)
        annualization = math.sqrt(365.25 * 24 * 3600 * 1000 / window_ms)
        
        return math.sqrt(sum_sq) * annualization
    
    def _compute_log_return(self, window_ms: int, now_ms: int) -> Optional[float]:
        """Compute total log return over a window."""
        cutoff = now_ms - window_ms
        
        # Find first and last price in window
        prices_in_window = [
            t.price for t in self._trades if t.ts_ms >= cutoff
        ]
        
        if len(prices_in_window) < 2:
            return None
        
        first_price = prices_in_window[0]
        last_price = prices_in_window[-1]
        
        if first_price <= 0:
            return None
        
        return math.log(last_price / first_price)
    
    def _compute_order_imbalance(self) -> float:
        """Compute order imbalance as (buy - sell) / (buy + sell)."""
        total = self._buy_volume + self._sell_volume
        if total == 0:
            return 0.0
        return (self._buy_volume - self._sell_volume) / total
    
    def snapshot(self, ctx: HourContext) -> dict[str, float]:
        """Get current feature snapshot."""
        now_ms = ctx.hour_end_ts_ms - ctx.t_remaining_ms
        
        features: dict[str, float] = {}
        
        # Realized volatilities for each window
        for name, window_ms in self._vol_windows_ms.items():
            vol = self._compute_realized_vol(window_ms, now_ms)
            features[name] = vol if vol is not None else 0.0
        
        # EWMA volatility
        features["ewma_vol"] = self._ewma_vol if self._ewma_vol is not None else 0.0
        
        # 1-minute log return
        log_ret = self._compute_log_return(60 * 1000, now_ms)
        features["log_return_1m"] = log_ret if log_ret is not None else 0.0
        
        # Order imbalance
        features["order_imbalance"] = self._compute_order_imbalance()
        
        # Percent change from open
        if ctx.open_price is not None and ctx.open_price > 0 and self._last_price is not None:
            features["pct_change_from_open"] = (self._last_price - ctx.open_price) / ctx.open_price
        else:
            features["pct_change_from_open"] = 0.0
        
        # Trade count
        features["trade_count"] = float(self._trade_count)
        
        return features
    
    def reset_for_new_hour(self, ctx: HourContext) -> None:
        """Reset for a new hour."""
        logger.info(f"FeatureEngine: Resetting for hour {ctx.hour_id}")
        
        # Keep some history for continuity but reset counters
        self._hour_id = ctx.hour_id
        self._trade_count = len(self._trades)
        self._buy_volume = 0.0
        self._sell_volume = 0.0
        # Keep _ewma_vol, _last_price, and recent trades for continuity
