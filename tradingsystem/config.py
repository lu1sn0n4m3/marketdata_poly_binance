"""
Application configuration.

Loads settings from environment variables with sensible defaults.
"""

import os
from dataclasses import dataclass


@dataclass
class AppConfig:
    """Application configuration."""

    # API credentials
    pm_private_key: str = ""
    pm_funder: str = ""
    pm_signature_type: int = 1

    # URLs
    pm_ws_market_url: str = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
    pm_ws_user_url: str = "wss://ws-subscriptions-clob.polymarket.com/ws/user"
    pm_rest_url: str = "https://clob.polymarket.com"
    gamma_api_url: str = "https://gamma-api.polymarket.com"
    binance_snapshot_url: str = "http://localhost:8080/snapshot/latest"

    # Strategy
    strategy_hz: int = 50
    base_spread_cents: int = 3
    base_size: int = 25
    min_size: int = 10
    max_size: int = 100
    skew_per_share_cents: float = 0.01
    max_skew_cents: int = 5

    # Executor
    pm_stale_threshold_ms: int = 500
    bn_stale_threshold_ms: int = 1000
    cancel_timeout_ms: int = 5000
    place_timeout_ms: int = 3000
    cooldown_after_cancel_all_ms: int = 3000

    # Limits
    gross_cap: int = 1000
    max_position: int = 500

    # Gateway
    gateway_rate_limit_ms: int = 25  # 40 actions/sec

    # Binance poller
    binance_poll_hz: int = 20

    # Logging
    log_level: str = "INFO"

    @classmethod
    def from_env(cls) -> "AppConfig":
        """Load config from environment variables."""
        return cls(
            # Credentials
            pm_private_key=os.getenv("PM_PRIVATE_KEY", ""),
            pm_funder=os.getenv("PM_FUNDER", ""),
            pm_signature_type=int(os.getenv("PM_SIGNATURE_TYPE", "1")),

            # URLs
            pm_ws_market_url=os.getenv(
                "PM_WS_MARKET_URL",
                "wss://ws-subscriptions-clob.polymarket.com/ws/market",
            ),
            pm_ws_user_url=os.getenv(
                "PM_WS_USER_URL",
                "wss://ws-subscriptions-clob.polymarket.com/ws/user",
            ),
            pm_rest_url=os.getenv("PM_REST_URL", "https://clob.polymarket.com"),
            gamma_api_url=os.getenv("GAMMA_API_URL", "https://gamma-api.polymarket.com"),
            binance_snapshot_url=os.getenv(
                "BINANCE_SNAPSHOT_URL",
                "http://localhost:8080/snapshot/latest",
            ),

            # Strategy
            strategy_hz=int(os.getenv("STRATEGY_HZ", "50")),
            base_spread_cents=int(os.getenv("BASE_SPREAD_CENTS", "3")),
            base_size=int(os.getenv("BASE_SIZE", "25")),
            min_size=int(os.getenv("MIN_SIZE", "10")),
            max_size=int(os.getenv("MAX_SIZE", "100")),
            skew_per_share_cents=float(os.getenv("SKEW_PER_SHARE_CENTS", "0.01")),
            max_skew_cents=int(os.getenv("MAX_SKEW_CENTS", "5")),

            # Executor
            pm_stale_threshold_ms=int(os.getenv("PM_STALE_THRESHOLD_MS", "500")),
            bn_stale_threshold_ms=int(os.getenv("BN_STALE_THRESHOLD_MS", "1000")),
            cancel_timeout_ms=int(os.getenv("CANCEL_TIMEOUT_MS", "5000")),
            place_timeout_ms=int(os.getenv("PLACE_TIMEOUT_MS", "3000")),
            cooldown_after_cancel_all_ms=int(os.getenv("COOLDOWN_AFTER_CANCEL_ALL_MS", "3000")),

            # Limits
            gross_cap=int(os.getenv("GROSS_CAP", "1000")),
            max_position=int(os.getenv("MAX_POSITION", "500")),

            # Gateway
            gateway_rate_limit_ms=int(os.getenv("GATEWAY_RATE_LIMIT_MS", "25")),

            # Binance
            binance_poll_hz=int(os.getenv("BINANCE_POLL_HZ", "20")),

            # Logging
            log_level=os.getenv("LOG_LEVEL", "INFO"),
        )

    @classmethod
    def from_env_file(cls, path: str) -> "AppConfig":
        """
        Load config from .env file, then environment variables.

        Environment variables override file values.
        """
        # Read env file if exists
        if os.path.exists(path):
            with open(path) as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    if "=" in line:
                        key, value = line.split("=", 1)
                        key = key.strip()
                        value = value.strip().strip("'\"")
                        # Only set if not already in environment
                        if key not in os.environ:
                            os.environ[key] = value

        return cls.from_env()

    def validate(self) -> list[str]:
        """Validate configuration, return list of errors."""
        errors = []

        if not self.pm_private_key:
            errors.append("PM_PRIVATE_KEY is required")

        if self.strategy_hz < 1 or self.strategy_hz > 100:
            errors.append("STRATEGY_HZ must be between 1 and 100")

        if self.base_spread_cents < 1:
            errors.append("BASE_SPREAD_CENTS must be at least 1")

        if self.max_position <= 0:
            errors.append("MAX_POSITION must be positive")

        if self.gross_cap <= 0:
            errors.append("GROSS_CAP must be positive")

        return errors
