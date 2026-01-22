"""Binance Pricer - Container A of the HourMM trading system.

This container is responsible for:
- Binance websocket ingestion (trades, BBO)
- Hour context tracking (open price, time remaining)
- Feature computation (volatility, returns)
- Fair price/probability estimation
- Publishing atomic snapshots for Container B
"""

__version__ = "0.1.0"
