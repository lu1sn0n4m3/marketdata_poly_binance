"""
Gateway types.

Types for Gateway actions and results.
"""

from dataclasses import dataclass
from typing import Optional

from .core import GatewayActionType
from .orders import RealOrderSpec


@dataclass(slots=True)
class GatewayAction:
    """
    An action to be performed by the Gateway.
    """
    action_type: GatewayActionType
    action_id: str
    order_spec: Optional[RealOrderSpec] = None  # For PLACE
    client_order_id: Optional[str] = None       # For CANCEL (our ID)
    server_order_id: Optional[str] = None       # For CANCEL (exchange ID)
    market_id: Optional[str] = None             # For CANCEL_ALL


@dataclass(slots=True)
class GatewayResult:
    """
    Result of a Gateway action.
    """
    action_id: str
    success: bool
    server_order_id: Optional[str] = None  # For successful PLACE
    error_kind: Optional[str] = None
    retryable: bool = False
