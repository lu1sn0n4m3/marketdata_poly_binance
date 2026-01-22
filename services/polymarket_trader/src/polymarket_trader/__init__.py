"""Polymarket Trader - Container B of the HourMM trading system.

This container is responsible for:
- Polymarket market discovery (Gamma API)
- Market and user WebSocket connectivity
- Canonical state management (single-writer reducer)
- Risk enforcement and limits
- Strategy execution
- Order management and execution
- Operator controls
"""

__version__ = "0.1.0"
