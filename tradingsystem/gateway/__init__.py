"""
Gateway module for order operations.

The Gateway provides a non-blocking interface for submitting orders to
Polymarket. It handles rate limiting, priority queuing for cancel-all
operations, and background processing.

Public API:
    Gateway: Main interface for submitting actions
    GatewayStats: Statistics for monitoring

Internal (exposed for testing):
    ActionDeque: Priority queue implementation
    GatewayWorker: Background processing thread

Example:
    from tradingsystem.gateway import Gateway

    gateway = Gateway(rest_client, result_queue)
    gateway.start()

    # Submit orders (non-blocking)
    gateway.submit_place(order_spec)
    gateway.submit_cancel(order_id)
    gateway.submit_cancel_all(market_id)  # Priority!

    gateway.stop()
"""

from .gateway import Gateway
from .worker import GatewayWorker, GatewayStats
from .action_deque import ActionDeque

__all__ = [
    "Gateway",
    "GatewayWorker",
    "GatewayStats",
    "ActionDeque",
]
