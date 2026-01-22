"""Custom exceptions for the HourMM trading system."""


class HourMMError(Exception):
    """Base exception for HourMM errors."""
    pass


class StaleDataError(HourMMError):
    """Raised when data is stale and cannot be used for trading decisions."""
    pass


class ConnectionError(HourMMError):
    """Raised when a connection fails or is lost."""
    pass


class ConfigurationError(HourMMError):
    """Raised when configuration is invalid."""
    pass


class ReconciliationError(HourMMError):
    """Raised when state reconciliation fails."""
    pass


class OrderError(HourMMError):
    """Raised when an order operation fails."""
    pass


class RiskLimitError(HourMMError):
    """Raised when a risk limit would be violated."""
    pass
