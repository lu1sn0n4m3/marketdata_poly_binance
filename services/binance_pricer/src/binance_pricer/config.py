"""Configuration for Container A (Binance Pricer)."""

import os
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class AConfig:
    """
    Configuration container for Binance Pricer.
    
    Loaded from environment variables with sensible defaults.
    """
    # Binance settings
    symbol: str = "BTCUSDT"
    binance_ws_url: str = "wss://stream.binance.com:9443/stream"
    
    # Staleness detection
    stale_threshold_ms: int = 500  # Mark stale if no event for this long
    
    # Snapshot publishing
    snapshot_publish_hz: int = 50  # Publish rate (20-50 recommended)
    
    # Feature engine settings
    feature_windows: dict = field(default_factory=lambda: {
        "vol_5m": 5 * 60 * 1000,  # 5 minute volatility window
        "vol_1m": 1 * 60 * 1000,  # 1 minute volatility window
        "ewma_alpha": 0.1,  # EWMA smoothing factor
        "return_window_ms": 60 * 1000,  # 1 minute return window
    })
    
    # Pricer settings
    pricer_params: dict = field(default_factory=lambda: {
        "base_vol": 0.02,  # Default volatility if not enough data
        "vol_floor": 0.005,  # Minimum volatility
        "vol_cap": 0.10,  # Maximum volatility
    })
    
    # HTTP server settings
    http_host: str = "0.0.0.0"
    http_port: int = 8080
    
    # Logging
    log_level: str = "INFO"
    
    @classmethod
    def from_env(cls) -> "AConfig":
        """Load configuration from environment variables."""
        feature_windows = {
            "vol_5m": int(os.getenv("FEATURE_VOL_5M_MS", str(5 * 60 * 1000))),
            "vol_1m": int(os.getenv("FEATURE_VOL_1M_MS", str(1 * 60 * 1000))),
            "ewma_alpha": float(os.getenv("FEATURE_EWMA_ALPHA", "0.1")),
            "return_window_ms": int(os.getenv("FEATURE_RETURN_WINDOW_MS", str(60 * 1000))),
        }
        
        pricer_params = {
            "base_vol": float(os.getenv("PRICER_BASE_VOL", "0.02")),
            "vol_floor": float(os.getenv("PRICER_VOL_FLOOR", "0.005")),
            "vol_cap": float(os.getenv("PRICER_VOL_CAP", "0.10")),
        }
        
        return cls(
            symbol=os.getenv("BINANCE_SYMBOL", "BTCUSDT"),
            binance_ws_url=os.getenv(
                "BINANCE_WS_URL",
                "wss://stream.binance.com:9443/stream"
            ),
            stale_threshold_ms=int(os.getenv("STALE_THRESHOLD_MS", "500")),
            snapshot_publish_hz=int(os.getenv("SNAPSHOT_PUBLISH_HZ", "50")),
            feature_windows=feature_windows,
            pricer_params=pricer_params,
            http_host=os.getenv("HTTP_HOST", "0.0.0.0"),
            http_port=int(os.getenv("HTTP_PORT", "8080")),
            log_level=os.getenv("LOG_LEVEL", "INFO"),
        )
    
    def validate(self) -> None:
        """Validate configuration values."""
        if not self.symbol:
            raise ValueError("symbol must not be empty")
        
        if self.stale_threshold_ms <= 0:
            raise ValueError("stale_threshold_ms must be positive")
        
        if self.snapshot_publish_hz < 1 or self.snapshot_publish_hz > 100:
            raise ValueError("snapshot_publish_hz must be between 1 and 100")
        
        if self.http_port < 1 or self.http_port > 65535:
            raise ValueError("http_port must be between 1 and 65535")
