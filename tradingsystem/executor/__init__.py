"""
Executor package for the trading system.

This package provides complement-aware order execution with:
- Split orders (SELL YES + BUY NO when needed)
- Inventory reservation tracking
- Min-size as a first-class constraint
- SELL overlap rule (no replacement until cancel acks)

Usage:
    from tradingsystem.executor import ExecutorActor, ExecutorPolicies

    executor = ExecutorActor(
        gateway=gateway,
        event_queue=event_queue,
        yes_token_id="...",
        no_token_id="...",
        market_id="...",
        policies=ExecutorPolicies(min_size_policy=MinSizePolicy.PASSIVE_FIRST),
    )
    executor.start()

The executor uses a unified event stream - all inputs (intents, fills, acks)
flow through a single queue for deterministic ordering.
"""

# Main actor
from .actor import ExecutorActor

# State types
from .state import (
    ExecutorState,
    ExecutorMode,
    OrderSlot,
    SlotState,
    ReservationLedger,
    PendingPlace,
    PendingCancel,
)

# Planner
from .planner import (
    plan_execution,
    create_execution_report,
    OrderKind,
    PlannedOrder,
    LegPlan,
    ExecutionPlan,
    ExecutionReport,
)

# Reconciler
from .reconciler import reconcile_slot

# Effects
from .effects import (
    ReconcileEffect,
    EffectBatch,
)

# Policies
from .policies import (
    MinSizePolicy,
    ExecutorPolicies,
)

# Errors
from .errors import (
    ErrorCode,
    normalize_error,
)

# PnL
from .pnl import (
    PnLTracker,
    FillRecord,
)

# Trade logging
from .trade_log import TradeLogger

# Sync
from .sync import (
    sync_balances,
    sync_open_orders,
    perform_full_resync,
    enter_resync_mode,
    SyncResult,
)

__all__ = [
    # Actor
    "ExecutorActor",
    # State
    "ExecutorState",
    "ExecutorMode",
    "OrderSlot",
    "SlotState",
    "ReservationLedger",
    "PendingPlace",
    "PendingCancel",
    # Planner
    "plan_execution",
    "create_execution_report",
    "OrderKind",
    "PlannedOrder",
    "LegPlan",
    "ExecutionPlan",
    "ExecutionReport",
    # Reconciler
    "reconcile_slot",
    # Effects
    "ReconcileEffect",
    "EffectBatch",
    # Policies
    "MinSizePolicy",
    "ExecutorPolicies",
    # Errors
    "ErrorCode",
    "normalize_error",
    # PnL
    "PnLTracker",
    "FillRecord",
    # Trade logging
    "TradeLogger",
    # Sync
    "sync_balances",
    "sync_open_orders",
    "perform_full_resync",
    "enter_resync_mode",
    "SyncResult",
]
