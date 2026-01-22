# HourMM Developer Guide

This guide explains how to extend the HourMM framework with custom pricers, strategies, and feature engines.

## Table of Contents

1. [Architecture for Extension](#architecture-for-extension)
2. [Implementing a Custom Pricer](#implementing-a-custom-pricer)
3. [Implementing a Custom Strategy](#implementing-a-custom-strategy)
4. [Implementing a Custom Feature Engine](#implementing-a-custom-feature-engine)
5. [Testing Your Implementations](#testing-your-implementations)
6. [Integrating with the App](#integrating-with-the-app)
7. [Best Practices](#best-practices)

---

## Architecture for Extension

The HourMM framework is designed with **pluggable components** where customization is expected:

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                          PLUGGABLE COMPONENTS                                │
│                                                                              │
│  Container A (Binance Pricer)                                               │
│  ┌─────────────────────────┐  ┌─────────────────────────┐                   │
│  │   FeatureEngine (ABC)   │  │     Pricer (ABC)        │                   │
│  │   ─────────────────     │  │     ──────────          │                   │
│  │   • DefaultFeatureEngine│  │   • BaselinePricer      │                   │
│  │   • YOUR FeatureEngine  │  │   • YOUR Pricer         │                   │
│  └─────────────────────────┘  └─────────────────────────┘                   │
│                                                                              │
│  Container B (Polymarket Trader)                                            │
│  ┌─────────────────────────┐                                                │
│  │     Strategy (ABC)      │                                                │
│  │     ────────────        │                                                │
│  │   • OpportunisticQuote  │                                                │
│  │   • DirectionalTaker    │                                                │
│  │   • YOUR Strategy       │                                                │
│  └─────────────────────────┘                                                │
│                                                                              │
│  CONCRETE (not meant for extension):                                        │
│  • BinanceWsClient, RiskEngine, OrderManager, Executor, StateReducer        │
└─────────────────────────────────────────────────────────────────────────────┘
```

### Key Interfaces

| Component | Location | Purpose |
|-----------|----------|---------|
| `Pricer` | `services/binance_pricer/src/binance_pricer/pricer.py` | Computes fair YES probability |
| `FeatureEngine` | `services/binance_pricer/src/binance_pricer/feature_engine.py` | Computes features from market data |
| `Strategy` | `services/polymarket_trader/src/polymarket_trader/strategy.py` | Generates trading intents |

---

## Implementing a Custom Pricer

A pricer converts market features into a fair YES probability for the hourly market.

### The Pricer Interface

```python
from abc import ABC, abstractmethod
from binance_pricer.types import HourContext, PricerOutput

class Pricer(ABC):
    """Abstract base class for pricers."""
    
    @property
    @abstractmethod
    def name(self) -> str:
        """Name of this pricer."""
        ...
    
    @property
    @abstractmethod
    def ready(self) -> bool:
        """Whether the pricer has enough data."""
        ...
    
    def update(self, ctx: HourContext, features: dict[str, float]) -> None:
        """Optional: called with each new data point."""
        pass
    
    @abstractmethod
    def price(self, ctx: HourContext, features: dict[str, float]) -> PricerOutput:
        """Compute fair price/probability."""
        ...
    
    @abstractmethod
    def reset_for_new_hour(self, ctx: HourContext) -> None:
        """Reset state for a new hour."""
        ...
```

### Example: Black-Scholes Inspired Pricer

```python
"""
Custom pricer using Black-Scholes-style probability calculation.

Location: services/binance_pricer/src/binance_pricer/pricer_blackscholes.py
"""

import math
import logging
from typing import Optional

from .types import HourContext, PricerOutput
from .pricer import Pricer

logger = logging.getLogger(__name__)


class BlackScholesPricer(Pricer):
    """
    Black-Scholes inspired pricer for hourly binary markets.
    
    For a market where YES wins if close > open:
    P(YES) = N(d2) where d2 = (ln(S/K) - 0.5*σ²*T) / (σ*sqrt(T))
    
    Here:
    - S = current price
    - K = open price (strike)
    - σ = volatility (from features)
    - T = time remaining as fraction of hour
    """
    
    def __init__(
        self,
        vol_feature_name: str = "ewma_vol",
        default_vol: float = 0.02,
        vol_floor: float = 0.001,
        vol_cap: float = 0.15,
    ):
        """
        Initialize the pricer.
        
        Args:
            vol_feature_name: Which feature to use for volatility
            default_vol: Default volatility if feature unavailable
            vol_floor: Minimum volatility
            vol_cap: Maximum volatility
        """
        self._vol_feature = vol_feature_name
        self._default_vol = default_vol
        self._vol_floor = vol_floor
        self._vol_cap = vol_cap
        
        self._update_count = 0
        self._hour_id: Optional[str] = None
    
    @property
    def name(self) -> str:
        return "BlackScholesPricer"
    
    @property
    def ready(self) -> bool:
        # Need some data to be confident
        return self._update_count >= 10
    
    def update(self, ctx: HourContext, features: dict[str, float]) -> None:
        """Track updates for readiness."""
        self._update_count += 1
    
    @staticmethod
    def _norm_cdf(x: float) -> float:
        """Standard normal CDF."""
        return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))
    
    def _get_volatility(self, features: dict[str, float]) -> float:
        """Extract volatility from features."""
        vol = features.get(self._vol_feature, 0.0)
        
        if vol <= 0:
            vol = features.get("vol_1m", self._default_vol)
        
        return max(self._vol_floor, min(self._vol_cap, vol))
    
    def price(self, ctx: HourContext, features: dict[str, float]) -> PricerOutput:
        """
        Compute fair YES probability using Black-Scholes d2.
        """
        # Validate inputs
        if ctx.open_price is None or ctx.open_price <= 0:
            return PricerOutput(p_yes_fair=0.5, ready=False)
        
        current_price = ctx.mid
        if current_price is None:
            current_price = ctx.last_trade_price
        
        if current_price is None or current_price <= 0:
            return PricerOutput(p_yes_fair=0.5, ready=False)
        
        # Time remaining as fraction of hour (avoid division by zero)
        T = max(0.0001, ctx.t_remaining_ms / (60 * 60 * 1000))
        
        # Get volatility (hourly)
        sigma = self._get_volatility(features)
        
        # Black-Scholes d2
        # d2 = (ln(S/K) - 0.5*σ²*T) / (σ*sqrt(T))
        S = current_price
        K = ctx.open_price
        
        sqrt_T = math.sqrt(T)
        sigma_sqrt_T = sigma * sqrt_T
        
        if sigma_sqrt_T < 0.0001:
            # Near expiry with no vol - step function
            p_yes = 1.0 if S > K else 0.0
            return PricerOutput(
                p_yes_fair=max(0.01, min(0.99, p_yes)),
                ready=True,
            )
        
        d2 = (math.log(S / K) - 0.5 * sigma * sigma * T) / sigma_sqrt_T
        
        # P(YES) = N(d2)
        p_yes = self._norm_cdf(d2)
        p_yes = max(0.01, min(0.99, p_yes))
        
        # Uncertainty bands: ±1 sigma price move
        # d2_lo = d2 - 1, d2_hi = d2 + 1
        p_yes_lo = max(0.01, min(0.99, self._norm_cdf(d2 - 1)))
        p_yes_hi = max(0.01, min(0.99, self._norm_cdf(d2 + 1)))
        
        return PricerOutput(
            p_yes_fair=p_yes,
            p_yes_band_lo=p_yes_lo,
            p_yes_band_hi=p_yes_hi,
            ready=self.ready,
        )
    
    def reset_for_new_hour(self, ctx: HourContext) -> None:
        """Reset for new hour."""
        logger.info(f"{self.name}: Reset for hour {ctx.hour_id}")
        self._hour_id = ctx.hour_id
        self._update_count = 0
```

### Example: Machine Learning Pricer (Stub)

```python
"""
ML-based pricer that loads a pre-trained model.

Location: services/binance_pricer/src/binance_pricer/pricer_ml.py
"""

import logging
from typing import Optional
import pickle  # or use joblib, torch, etc.

from .types import HourContext, PricerOutput
from .pricer import Pricer

logger = logging.getLogger(__name__)


class MLPricer(Pricer):
    """
    Machine learning pricer that uses a pre-trained model.
    
    The model should accept a feature dict and output a probability.
    """
    
    def __init__(self, model_path: str = "/models/pricer_model.pkl"):
        """
        Initialize with a pre-trained model.
        
        Args:
            model_path: Path to serialized model
        """
        self._model_path = model_path
        self._model = None
        self._feature_names: list[str] = []
        self._load_model()
    
    def _load_model(self) -> None:
        """Load the model from disk."""
        try:
            with open(self._model_path, "rb") as f:
                data = pickle.load(f)
                self._model = data["model"]
                self._feature_names = data.get("feature_names", [])
            logger.info(f"Loaded ML model from {self._model_path}")
        except Exception as e:
            logger.warning(f"Could not load model: {e}")
            self._model = None
    
    @property
    def name(self) -> str:
        return "MLPricer"
    
    @property
    def ready(self) -> bool:
        return self._model is not None
    
    def price(self, ctx: HourContext, features: dict[str, float]) -> PricerOutput:
        """Use model to predict probability."""
        if not self.ready:
            return PricerOutput(p_yes_fair=0.5, ready=False)
        
        # Build feature vector
        X = self._build_features(ctx, features)
        
        try:
            # Model should output probability
            p_yes = float(self._model.predict_proba([X])[0][1])
            p_yes = max(0.01, min(0.99, p_yes))
            
            return PricerOutput(
                p_yes_fair=p_yes,
                ready=True,
            )
        except Exception as e:
            logger.warning(f"Model prediction failed: {e}")
            return PricerOutput(p_yes_fair=0.5, ready=False)
    
    def _build_features(self, ctx: HourContext, features: dict[str, float]) -> list:
        """Build feature vector for model."""
        # Include hour context
        X = []
        
        # Time features
        X.append(ctx.t_remaining_ms / (60 * 60 * 1000))  # Fraction of hour
        
        # Price features
        if ctx.open_price and ctx.open_price > 0:
            current = ctx.mid or ctx.last_trade_price or ctx.open_price
            X.append(current / ctx.open_price - 1)  # Return from open
        else:
            X.append(0.0)
        
        # Market features
        for name in self._feature_names:
            X.append(features.get(name, 0.0))
        
        return X
    
    def reset_for_new_hour(self, ctx: HourContext) -> None:
        """Nothing to reset for stateless model."""
        pass
```

---

## Implementing a Custom Strategy

A strategy takes the current context (state, snapshot) and outputs trading intent.

### The Strategy Interface

```python
from abc import ABC, abstractmethod
from polymarket_trader.types import DecisionContext, StrategyIntent

class Strategy(ABC):
    """Abstract base class for strategies."""
    
    @property
    @abstractmethod
    def name(self) -> str:
        """Strategy name."""
        ...
    
    @property
    @abstractmethod
    def enabled(self) -> bool:
        """Whether strategy is enabled."""
        ...
    
    @abstractmethod
    def on_tick(self, ctx: DecisionContext) -> StrategyIntent:
        """
        Called on each decision tick (50Hz).
        
        Returns:
            StrategyIntent with desired quotes and/or taker actions
        """
        ...
    
    def on_session_start(self, ctx: DecisionContext) -> None:
        """Optional: called at session start."""
        pass
    
    def on_session_end(self, ctx: DecisionContext) -> None:
        """Optional: called at session end."""
        pass
```

### The StrategyIntent Structure

```python
@dataclass
class StrategyIntent:
    """Output from strategy."""
    quotes: QuoteSet  # Desired bid/ask quotes
    take_actions: list[DesiredOrder]  # Aggressive orders
    target_inventory: Optional[float]  # Optional inventory target
    cancel_all: bool  # Emergency cancel all
```

### Example: Momentum Strategy

```python
"""
Momentum strategy that trades in the direction of recent price movement.

Location: services/polymarket_trader/src/polymarket_trader/strategy_momentum.py
"""

import logging
from typing import Optional

from .types import (
    DecisionContext, StrategyIntent, DesiredOrder, QuoteSet,
    Side, OrderPurpose,
)

try:
    from shared.hourmm_common.util import round_to_tick
except ImportError:
    import sys
    sys.path.insert(0, "/Users/luissafar/dev/marketdata_poly_binance")
    from shared.hourmm_common.util import round_to_tick

from .strategy import Strategy

logger = logging.getLogger(__name__)


class MomentumStrategy(Strategy):
    """
    Momentum-based strategy.
    
    Logic:
    - If price is trending up (fair > open by threshold), favor buying
    - If price is trending down (fair < open by threshold), favor selling
    - Skew quotes in the direction of momentum
    """
    
    def __init__(
        self,
        momentum_threshold: float = 0.02,  # 2% move triggers momentum
        base_spread_ticks: int = 3,
        base_size: float = 10.0,
        momentum_size_multiplier: float = 1.5,  # Increase size with momentum
    ):
        """
        Initialize the strategy.
        
        Args:
            momentum_threshold: Price change threshold to detect momentum
            base_spread_ticks: Base spread from fair
            base_size: Base order size
            momentum_size_multiplier: Size multiplier when momentum detected
        """
        self._momentum_threshold = momentum_threshold
        self._base_spread_ticks = base_spread_ticks
        self._base_size = base_size
        self._momentum_mult = momentum_size_multiplier
        self._enabled = True
    
    @property
    def name(self) -> str:
        return "MomentumStrategy"
    
    @property
    def enabled(self) -> bool:
        return self._enabled
    
    def on_tick(self, ctx: DecisionContext) -> StrategyIntent:
        """Generate momentum-based intent."""
        intent = StrategyIntent()
        
        snapshot = ctx.snapshot
        state = ctx.state_view
        
        if not snapshot or snapshot.p_yes_fair is None:
            return intent
        
        if state.market_view.best_bid is None or state.market_view.best_ask is None:
            return intent
        
        # Get token ID
        token_id = ""
        if state.token_ids:
            token_id = state.token_ids.yes_token_id
        if not token_id:
            return intent
        
        fair = snapshot.p_yes_fair
        tick_size = state.market_view.tick_size
        
        # Detect momentum from features
        pct_change = snapshot.features.get("pct_change_from_open", 0.0)
        momentum_up = pct_change > self._momentum_threshold
        momentum_down = pct_change < -self._momentum_threshold
        
        # Compute size
        size = self._base_size
        if momentum_up or momentum_down:
            size *= self._momentum_mult
        
        # Time decay
        time_factor = max(0.2, ctx.t_remaining_ms / (60 * 60 * 1000))
        size *= time_factor
        
        expires_at_ms = ctx.now_ms + 5000
        
        # Skew based on momentum
        if momentum_up:
            # Trending up: more aggressive on bid, wider on ask
            bid_spread = self._base_spread_ticks - 1
            ask_spread = self._base_spread_ticks + 2
        elif momentum_down:
            # Trending down: wider on bid, more aggressive on ask
            bid_spread = self._base_spread_ticks + 2
            ask_spread = self._base_spread_ticks - 1
        else:
            bid_spread = self._base_spread_ticks
            ask_spread = self._base_spread_ticks
        
        # Build bid
        bid_price = round_to_tick(fair - bid_spread * tick_size, tick_size)
        bid_price = max(0.01, min(0.99, bid_price))
        
        intent.quotes.bid = DesiredOrder(
            side=Side.BUY,
            price=bid_price,
            size=size,
            purpose=OrderPurpose.QUOTE,
            expires_at_ms=expires_at_ms,
            token_id=token_id,
        )
        
        # Build ask
        ask_price = round_to_tick(fair + ask_spread * tick_size, tick_size)
        ask_price = max(0.01, min(0.99, ask_price))
        
        intent.quotes.ask = DesiredOrder(
            side=Side.SELL,
            price=ask_price,
            size=size,
            purpose=OrderPurpose.QUOTE,
            expires_at_ms=expires_at_ms,
            token_id=token_id,
        )
        
        return intent
    
    def on_session_start(self, ctx: DecisionContext) -> None:
        logger.info(f"{self.name}: Session started")
    
    def on_session_end(self, ctx: DecisionContext) -> None:
        logger.info(f"{self.name}: Session ended")
```

### Example: Inventory-Focused Strategy

```python
"""
Inventory-focused strategy that prioritizes position management.

Location: services/polymarket_trader/src/polymarket_trader/strategy_inventory.py
"""

import logging
from typing import Optional

from .types import (
    DecisionContext, StrategyIntent, DesiredOrder, QuoteSet,
    Side, OrderPurpose,
)
from .strategy import Strategy

try:
    from shared.hourmm_common.util import round_to_tick
except ImportError:
    import sys
    sys.path.insert(0, "/Users/luissafar/dev/marketdata_poly_binance")
    from shared.hourmm_common.util import round_to_tick

logger = logging.getLogger(__name__)


class InventoryFocusedStrategy(Strategy):
    """
    Strategy that adjusts quotes based on inventory risk.
    
    Key features:
    - Strong skew to reduce inventory
    - Size reduction when inventory is high
    - More aggressive unwind as time runs out
    """
    
    def __init__(
        self,
        max_inventory: float = 100.0,
        base_spread_ticks: int = 3,
        max_skew_ticks: int = 5,
        base_size: float = 10.0,
    ):
        self._max_inventory = max_inventory
        self._base_spread_ticks = base_spread_ticks
        self._max_skew_ticks = max_skew_ticks
        self._base_size = base_size
        self._enabled = True
    
    @property
    def name(self) -> str:
        return "InventoryFocusedStrategy"
    
    @property
    def enabled(self) -> bool:
        return self._enabled
    
    def on_tick(self, ctx: DecisionContext) -> StrategyIntent:
        """Generate inventory-aware intent."""
        intent = StrategyIntent()
        
        snapshot = ctx.snapshot
        state = ctx.state_view
        
        if not snapshot or snapshot.p_yes_fair is None:
            return intent
        
        if state.market_view.best_bid is None:
            return intent
        
        token_id = ""
        if state.token_ids:
            token_id = state.token_ids.yes_token_id
        if not token_id:
            return intent
        
        fair = snapshot.p_yes_fair
        tick_size = state.market_view.tick_size
        net_inventory = state.positions.net_exposure
        
        # Calculate inventory ratio
        inv_ratio = net_inventory / self._max_inventory
        inv_ratio = max(-1.0, min(1.0, inv_ratio))
        
        # Time urgency
        time_factor = ctx.t_remaining_ms / (60 * 60 * 1000)
        urgency = 1.0 - time_factor  # 0 at start, 1 at end
        
        # Skew: positive inventory -> skew down (favor selling)
        skew_ticks = -inv_ratio * self._max_skew_ticks * (1.0 + urgency)
        skew = skew_ticks * tick_size
        
        # Size: reduce when inventory high
        size = self._base_size * (1.0 - 0.5 * abs(inv_ratio))
        size *= time_factor  # Also reduce near hour end
        size = max(1.0, size)
        
        expires_at_ms = ctx.now_ms + 5000
        
        # Build quotes with skew
        bid_price = round_to_tick(
            fair - self._base_spread_ticks * tick_size + skew,
            tick_size
        )
        ask_price = round_to_tick(
            fair + self._base_spread_ticks * tick_size + skew,
            tick_size
        )
        
        bid_price = max(0.01, min(0.99, bid_price))
        ask_price = max(0.01, min(0.99, ask_price))
        
        # Only quote the side that reduces inventory (or both if neutral)
        if inv_ratio < 0.3:  # Short or neutral-ish, can buy
            intent.quotes.bid = DesiredOrder(
                side=Side.BUY,
                price=bid_price,
                size=size,
                purpose=OrderPurpose.QUOTE,
                expires_at_ms=expires_at_ms,
                token_id=token_id,
            )
        
        if inv_ratio > -0.3:  # Long or neutral-ish, can sell
            intent.quotes.ask = DesiredOrder(
                side=Side.SELL,
                price=ask_price,
                size=size,
                purpose=OrderPurpose.QUOTE,
                expires_at_ms=expires_at_ms,
                token_id=token_id,
            )
        
        return intent
```

---

## Implementing a Custom Feature Engine

Feature engines compute rolling statistics from raw market data.

### The FeatureEngine Interface

```python
from abc import ABC, abstractmethod
from binance_pricer.types import BinanceEvent, HourContext

class FeatureEngine(ABC):
    """Abstract base class for feature engines."""
    
    @property
    @abstractmethod
    def feature_names(self) -> list[str]:
        """List of feature names this engine produces."""
        ...
    
    @property
    @abstractmethod
    def ready(self) -> bool:
        """Whether the engine has enough data."""
        ...
    
    @abstractmethod
    def update(self, event: BinanceEvent, ctx: HourContext) -> None:
        """Update features from a new event."""
        ...
    
    @abstractmethod
    def snapshot(self, ctx: HourContext) -> dict[str, float]:
        """Get current feature snapshot."""
        ...
    
    @abstractmethod
    def reset_for_new_hour(self, ctx: HourContext) -> None:
        """Reset for a new hour."""
        ...
```

### Example: Microstructure Feature Engine

```python
"""
Feature engine that computes microstructure features.

Location: services/binance_pricer/src/binance_pricer/feature_engine_micro.py
"""

import math
import logging
from collections import deque
from typing import Optional

from .types import BinanceEvent, BinanceEventType, HourContext
from .feature_engine import FeatureEngine

logger = logging.getLogger(__name__)


class MicrostructureFeatureEngine(FeatureEngine):
    """
    Computes microstructure features:
    - Microprice (volume-weighted mid)
    - Spread
    - Trade intensity
    - Buy/sell pressure
    """
    
    def __init__(
        self,
        window_trades: int = 100,
        window_ms: int = 60000,
    ):
        self._window_trades = window_trades
        self._window_ms = window_ms
        
        # Trade history
        self._trades: deque = deque(maxlen=window_trades)
        
        # BBO state
        self._last_bid: Optional[float] = None
        self._last_ask: Optional[float] = None
        self._last_bid_size: Optional[float] = None
        self._last_ask_size: Optional[float] = None
        
        # Counters
        self._buy_count = 0
        self._sell_count = 0
        self._buy_volume = 0.0
        self._sell_volume = 0.0
        
        self._trade_count = 0
    
    @property
    def feature_names(self) -> list[str]:
        return [
            "microprice",
            "spread",
            "spread_bps",
            "trade_intensity",
            "buy_pressure",
            "vwap",
        ]
    
    @property
    def ready(self) -> bool:
        return self._trade_count >= 10
    
    def update(self, event: BinanceEvent, ctx: HourContext) -> None:
        """Update from event."""
        if event.event_type == BinanceEventType.TRADE:
            self._update_trade(event)
        elif event.event_type == BinanceEventType.BBO:
            self._update_bbo(event)
    
    def _update_trade(self, event: BinanceEvent) -> None:
        """Update from trade."""
        if event.price is None or event.size is None:
            return
        
        self._trades.append({
            "price": event.price,
            "size": event.size,
            "side": event.side,
            "ts_ms": event.ts_exchange_ms,
        })
        
        if event.side == "buy":
            self._buy_count += 1
            self._buy_volume += event.size
        else:
            self._sell_count += 1
            self._sell_volume += event.size
        
        self._trade_count += 1
    
    def _update_bbo(self, event: BinanceEvent) -> None:
        """Update from BBO."""
        self._last_bid = event.bid_price
        self._last_ask = event.ask_price
        self._last_bid_size = event.bid_size
        self._last_ask_size = event.ask_size
    
    def snapshot(self, ctx: HourContext) -> dict[str, float]:
        """Get features."""
        features = {}
        
        # Microprice (volume-weighted mid)
        microprice = self._compute_microprice()
        features["microprice"] = microprice if microprice else 0.0
        
        # Spread
        if self._last_bid and self._last_ask:
            spread = self._last_ask - self._last_bid
            mid = (self._last_bid + self._last_ask) / 2
            features["spread"] = spread
            features["spread_bps"] = (spread / mid) * 10000 if mid > 0 else 0
        else:
            features["spread"] = 0.0
            features["spread_bps"] = 0.0
        
        # Trade intensity (trades per second in window)
        now_ms = ctx.hour_end_ts_ms - ctx.t_remaining_ms
        recent_trades = [t for t in self._trades if t["ts_ms"] > now_ms - self._window_ms]
        features["trade_intensity"] = len(recent_trades) / (self._window_ms / 1000)
        
        # Buy pressure
        total_volume = self._buy_volume + self._sell_volume
        if total_volume > 0:
            features["buy_pressure"] = self._buy_volume / total_volume
        else:
            features["buy_pressure"] = 0.5
        
        # VWAP
        features["vwap"] = self._compute_vwap()
        
        return features
    
    def _compute_microprice(self) -> Optional[float]:
        """Compute volume-weighted microprice."""
        if not self._last_bid or not self._last_ask:
            return None
        if not self._last_bid_size or not self._last_ask_size:
            return (self._last_bid + self._last_ask) / 2
        
        total_size = self._last_bid_size + self._last_ask_size
        if total_size == 0:
            return (self._last_bid + self._last_ask) / 2
        
        # Microprice: weighted by opposite-side size
        microprice = (
            self._last_bid * self._last_ask_size +
            self._last_ask * self._last_bid_size
        ) / total_size
        
        return microprice
    
    def _compute_vwap(self) -> float:
        """Compute VWAP from recent trades."""
        if not self._trades:
            return 0.0
        
        total_value = sum(t["price"] * t["size"] for t in self._trades)
        total_volume = sum(t["size"] for t in self._trades)
        
        if total_volume == 0:
            return 0.0
        
        return total_value / total_volume
    
    def reset_for_new_hour(self, ctx: HourContext) -> None:
        """Reset counters for new hour."""
        logger.info(f"MicrostructureEngine: Reset for {ctx.hour_id}")
        self._buy_count = 0
        self._sell_count = 0
        self._buy_volume = 0.0
        self._sell_volume = 0.0
        # Keep trades for continuity
```

---

## Testing Your Implementations

### Unit Test Template for Pricer

```python
"""Test custom pricer."""
import sys
sys.path.insert(0, "/path/to/marketdata_poly_binance")
sys.path.insert(0, "/path/to/marketdata_poly_binance/services/binance_pricer/src")

from binance_pricer.types import HourContext
from binance_pricer.pricer_blackscholes import BlackScholesPricer  # Your pricer


def test_pricer_basic():
    """Test basic functionality."""
    pricer = BlackScholesPricer()
    
    assert pricer.name == "BlackScholesPricer"
    assert not pricer.ready  # No updates yet
    
    # Create context
    ctx = HourContext(
        hour_id="2026-01-22T14:00:00Z",
        hour_start_ts_ms=1737554400000,
        hour_end_ts_ms=1737558000000,
        t_remaining_ms=1800000,  # 30 min left
        open_price=50000.0,
        last_trade_price=50500.0,  # Up 1%
        bbo_bid=50450.0,
        bbo_ask=50550.0,
    )
    
    features = {"ewma_vol": 0.02}
    
    # Update a few times
    for _ in range(15):
        pricer.update(ctx, features)
    
    assert pricer.ready
    
    output = pricer.price(ctx, features)
    
    print(f"p_yes_fair = {output.p_yes_fair:.4f}")
    
    # Price is up, should be > 0.5
    assert output.p_yes_fair > 0.5
    assert 0.0 < output.p_yes_fair < 1.0


def test_pricer_edge_cases():
    """Test edge cases."""
    pricer = BlackScholesPricer()
    
    # Missing open price
    ctx = HourContext(
        hour_id="test",
        hour_start_ts_ms=0,
        hour_end_ts_ms=3600000,
        t_remaining_ms=1800000,
        open_price=None,
        last_trade_price=50000.0,
    )
    
    output = pricer.price(ctx, {})
    assert output.p_yes_fair == 0.5  # Default when no open
    assert not output.ready


if __name__ == "__main__":
    test_pricer_basic()
    test_pricer_edge_cases()
    print("✓ All pricer tests passed")
```

### Unit Test Template for Strategy

```python
"""Test custom strategy."""
import sys
sys.path.insert(0, "/path/to/marketdata_poly_binance")
sys.path.insert(0, "/path/to/marketdata_poly_binance/services/polymarket_trader/src")

import time
from polymarket_trader.types import (
    DecisionContext, CanonicalStateView, SessionState, RiskMode,
    PositionState, HealthState, LimitState, MarketView, TokenIds,
    BinanceSnapshot,
)
from polymarket_trader.strategy_momentum import MomentumStrategy  # Your strategy


def test_strategy_basic():
    """Test basic intent generation."""
    strategy = MomentumStrategy()
    
    assert strategy.name == "MomentumStrategy"
    assert strategy.enabled
    
    now_ms = int(time.time() * 1000)
    
    # Create state view
    state_view = CanonicalStateView(
        session_state=SessionState.ACTIVE,
        risk_mode=RiskMode.NORMAL,
        active_market=None,
        token_ids=TokenIds(yes_token_id="token123", no_token_id="token456"),
        positions=PositionState(),
        open_orders={},
        market_view=MarketView(
            market_id="test",
            tick_size=0.01,
            best_bid=0.48,
            best_ask=0.52,
        ),
        health=HealthState(market_ws_connected=True, user_ws_connected=True),
        limits=LimitState(),
        latest_snapshot=None,
    )
    
    # Create snapshot with momentum (5% up)
    snapshot = BinanceSnapshot(
        seq=1,
        ts_local_ms=now_ms,
        ts_exchange_ms=now_ms - 10,
        age_ms=50,
        stale=False,
        hour_id="test",
        hour_start_ts_ms=now_ms - 30 * 60 * 1000,
        hour_end_ts_ms=now_ms + 30 * 60 * 1000,
        t_remaining_ms=30 * 60 * 1000,
        open_price=50000.0,
        last_trade_price=52500.0,  # 5% up
        bbo_bid=52450.0,
        bbo_ask=52550.0,
        mid=52500.0,
        features={"pct_change_from_open": 0.05},  # 5% momentum
        p_yes_fair=0.70,
    )
    
    ctx = DecisionContext(
        state_view=state_view,
        snapshot=snapshot,
        now_ms=now_ms,
        t_remaining_ms=30 * 60 * 1000,
    )
    
    intent = strategy.on_tick(ctx)
    
    print(f"Bid: {intent.quotes.bid}")
    print(f"Ask: {intent.quotes.ask}")
    
    # Should have quotes
    assert intent.quotes.bid is not None or intent.quotes.ask is not None


if __name__ == "__main__":
    test_strategy_basic()
    print("✓ All strategy tests passed")
```

---

## Integrating with the App

### Using a Custom Pricer in Container A

Edit `services/binance_pricer/src/binance_pricer/app.py`:

```python
from .pricer import BaselinePricer
from .pricer_blackscholes import BlackScholesPricer  # Your custom pricer

class ContainerAApp:
    def _setup_components(self) -> None:
        # ... other setup ...
        
        # Use your custom pricer instead of BaselinePricer
        self.pricer = BlackScholesPricer(
            vol_feature_name="ewma_vol",
            default_vol=0.02,
        )
        
        # ... rest of setup ...
```

### Using a Custom Strategy in Container B

Edit `services/polymarket_trader/src/polymarket_trader/app.py`:

```python
from .strategy import OpportunisticQuoteStrategy
from .strategy_momentum import MomentumStrategy  # Your custom strategy

class ContainerBApp:
    def _setup_components(self) -> None:
        # ... other setup ...
        
        # Use your custom strategy
        self.strategy = MomentumStrategy(
            momentum_threshold=0.02,
            base_spread_ticks=3,
            base_size=10.0,
        )
        
        # ... rest of setup ...
```

### Combining Multiple Strategies

```python
"""Composite strategy that combines multiple strategies."""

from .strategy import Strategy
from .types import DecisionContext, StrategyIntent, Side

class CompositeStrategy(Strategy):
    """Combines multiple strategies."""
    
    def __init__(self, strategies: list[Strategy]):
        self._strategies = strategies
        self._enabled = True
    
    @property
    def name(self) -> str:
        names = [s.name for s in self._strategies]
        return f"Composite({', '.join(names)})"
    
    @property
    def enabled(self) -> bool:
        return self._enabled
    
    def on_tick(self, ctx: DecisionContext) -> StrategyIntent:
        """Merge intents from all strategies."""
        combined = StrategyIntent()
        
        for strategy in self._strategies:
            if not strategy.enabled:
                continue
            
            intent = strategy.on_tick(ctx)
            
            # Take first bid/ask from any strategy that provides one
            if intent.quotes.bid and not combined.quotes.bid:
                combined.quotes.bid = intent.quotes.bid
            if intent.quotes.ask and not combined.quotes.ask:
                combined.quotes.ask = intent.quotes.ask
            
            # Collect all taker actions
            combined.take_actions.extend(intent.take_actions)
        
        return combined
```

---

## Best Practices

### 1. Keep Pricers Stateless (If Possible)

Stateless pricers are easier to test and reason about:

```python
# Good: Stateless computation
def price(self, ctx, features) -> PricerOutput:
    return self._compute_from_inputs(ctx, features)

# Be careful: Stateful computation
def price(self, ctx, features) -> PricerOutput:
    self._update_internal_state(ctx, features)  # Side effect!
    return self._compute_from_state()
```

### 2. Handle Missing Data Gracefully

```python
def price(self, ctx, features) -> PricerOutput:
    # Always have a fallback
    if ctx.open_price is None:
        return PricerOutput(p_yes_fair=0.5, ready=False)
    
    vol = features.get("vol", self._default_vol)
    # ... rest of computation
```

### 3. Log Important Decisions

```python
def on_tick(self, ctx) -> StrategyIntent:
    intent = StrategyIntent()
    
    # Log when making significant decisions
    if large_position:
        logger.info(f"{self.name}: Large position detected, adjusting quotes")
    
    return intent
```

### 4. Test Edge Cases

- What happens at hour boundaries?
- What if data is stale?
- What if volatility is zero?
- What if the position is at the limit?

### 5. Use Type Hints

The framework uses type hints throughout. Follow the same pattern:

```python
def compute_fair(
    self,
    current: float,
    strike: float,
    vol: float,
    t: float,
) -> float:
    """
    Compute fair probability.
    
    Args:
        current: Current price
        strike: Strike price (open)
        vol: Volatility (annualized)
        t: Time remaining (years)
    
    Returns:
        Fair probability in [0, 1]
    """
    # ...
```

### 6. Respect Risk Engine Decisions

Your strategy will be clamped by the risk engine. Don't try to work around it:

```python
# Strategy output is just an "intent"
# The risk engine will:
# - Clamp sizes to limits
# - Block orders in HALT mode
# - Only allow reduces in REDUCE_ONLY mode
# - Enforce position limits

# Trust the system - your job is to compute the optimal intent
# given no constraints, and let risk handle the rest
```

---

## Summary

| To Create | Inherit From | Key Method |
|-----------|--------------|------------|
| Custom Pricer | `Pricer` | `price(ctx, features) -> PricerOutput` |
| Custom Strategy | `Strategy` | `on_tick(ctx) -> StrategyIntent` |
| Custom Features | `FeatureEngine` | `snapshot(ctx) -> dict[str, float]` |

The framework handles all the hard parts (connectivity, state management, risk, execution). Your job is to implement the "brain" - how to value the market and when to trade.
