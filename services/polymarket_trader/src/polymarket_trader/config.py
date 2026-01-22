"""Configuration for Container B (Polymarket Trader)."""

import os
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class BConfig:
    """
    Configuration container for Polymarket Trader.
    
    Loaded from environment variables with sensible defaults.
    """
    # Snapshot polling
    poll_snapshot_hz: int = 50  # Poll rate from Container A
    snapshot_url: str = "http://binance_pricer:8080/snapshot/latest"
    
    # Market discovery
    gamma_api_url: str = "https://gamma-api.polymarket.com"
    market_slug: str = "bitcoin-btc-price-on-january-31"  # Base market slug
    
    # Polymarket WebSocket URLs
    pm_ws_market_url: str = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
    pm_ws_user_url: str = "wss://ws-subscriptions-clob.polymarket.com/ws/user"
    
    # Polymarket REST API
    pm_rest_url: str = "https://clob.polymarket.com"
    
    # Authentication (loaded from env)
    pm_api_key: str = ""
    pm_api_secret: str = ""
    pm_passphrase: str = ""
    pm_private_key: str = ""  # For signing
    
    # SQLite journal
    sqlite_path: str = "/data/journal.db"
    
    # Session timers (in milliseconds)
    session_timers: dict = field(default_factory=lambda: {
        "T_wind_ms": 10 * 60 * 1000,  # Start WIND_DOWN 10 min before hour end
        "T_flatten_ms": 5 * 60 * 1000,  # Start FLATTEN 5 min before hour end
        "hard_cutoff_ms": 1 * 60 * 1000,  # Stop all trading 1 min before hour end
        "stabilization_window_ms": 5000,  # Wait 5s after reconnect before resuming
    })
    
    # Risk limits
    max_reserved_capital: float = 1000.0  # Max capital per hour market
    max_position_size: float = 500.0  # Max position in tokens
    max_order_size: float = 100.0  # Max single order size
    max_open_orders: int = 4  # Max working orders (2 per side)
    stale_snapshot_threshold_ms: int = 1000  # Treat Binance data as stale after this
    
    # Order management
    replace_min_ticks: int = 2  # Only replace if price moves by this many ticks
    replace_min_age_ms: int = 500  # Minimum time before replacing an order
    default_order_ttl_ms: int = 5000  # Default order expiration
    
    # Control plane
    control_host: str = "127.0.0.1"
    control_port: int = 9000
    
    # Logging
    log_level: str = "INFO"
    
    @classmethod
    def from_env(cls) -> "BConfig":
        """Load configuration from environment variables."""
        session_timers = {
            "T_wind_ms": int(os.getenv("T_WIND_MS", str(10 * 60 * 1000))),
            "T_flatten_ms": int(os.getenv("T_FLATTEN_MS", str(5 * 60 * 1000))),
            "hard_cutoff_ms": int(os.getenv("HARD_CUTOFF_MS", str(1 * 60 * 1000))),
            "stabilization_window_ms": int(os.getenv("STABILIZATION_WINDOW_MS", "5000")),
        }
        
        return cls(
            poll_snapshot_hz=int(os.getenv("POLL_SNAPSHOT_HZ", "50")),
            snapshot_url=os.getenv(
                "SNAPSHOT_URL",
                "http://binance_pricer:8080/snapshot/latest"
            ),
            gamma_api_url=os.getenv(
                "GAMMA_API_URL",
                "https://gamma-api.polymarket.com"
            ),
            market_slug=os.getenv("MARKET_SLUG", "bitcoin-btc-price-on-january-31"),
            pm_ws_market_url=os.getenv(
                "PM_WS_MARKET_URL",
                "wss://ws-subscriptions-clob.polymarket.com/ws/market"
            ),
            pm_ws_user_url=os.getenv(
                "PM_WS_USER_URL",
                "wss://ws-subscriptions-clob.polymarket.com/ws/user"
            ),
            pm_rest_url=os.getenv("PM_REST_URL", "https://clob.polymarket.com"),
            pm_api_key=os.getenv("PM_API_KEY", ""),
            pm_api_secret=os.getenv("PM_API_SECRET", ""),
            pm_passphrase=os.getenv("PM_PASSPHRASE", ""),
            pm_private_key=os.getenv("PM_PRIVATE_KEY", ""),
            sqlite_path=os.getenv("SQLITE_PATH", "/data/journal.db"),
            session_timers=session_timers,
            max_reserved_capital=float(os.getenv("MAX_RESERVED_CAPITAL", "1000.0")),
            max_position_size=float(os.getenv("MAX_POSITION_SIZE", "500.0")),
            max_order_size=float(os.getenv("MAX_ORDER_SIZE", "100.0")),
            max_open_orders=int(os.getenv("MAX_OPEN_ORDERS", "4")),
            stale_snapshot_threshold_ms=int(os.getenv("STALE_SNAPSHOT_THRESHOLD_MS", "1000")),
            replace_min_ticks=int(os.getenv("REPLACE_MIN_TICKS", "2")),
            replace_min_age_ms=int(os.getenv("REPLACE_MIN_AGE_MS", "500")),
            default_order_ttl_ms=int(os.getenv("DEFAULT_ORDER_TTL_MS", "5000")),
            control_host=os.getenv("CONTROL_HOST", "127.0.0.1"),
            control_port=int(os.getenv("CONTROL_PORT", "9000")),
            log_level=os.getenv("LOG_LEVEL", "INFO"),
        )
    
    def validate(self) -> None:
        """Validate configuration values."""
        if self.poll_snapshot_hz < 1 or self.poll_snapshot_hz > 100:
            raise ValueError("poll_snapshot_hz must be between 1 and 100")
        
        if self.max_reserved_capital <= 0:
            raise ValueError("max_reserved_capital must be positive")
        
        if self.max_order_size <= 0:
            raise ValueError("max_order_size must be positive")
        
        if self.max_open_orders < 0:
            raise ValueError("max_open_orders must be non-negative")
        
        if self.control_port < 1 or self.control_port > 65535:
            raise ValueError("control_port must be between 1 and 65535")
