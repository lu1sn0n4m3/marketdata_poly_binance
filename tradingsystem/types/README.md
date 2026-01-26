# Trading System Types

This module contains all type definitions for the trading system. Types are organized into logical submodules for maintainability, with backward-compatible re-exports from the main `__init__.py`.

## Module Structure

```
types/
├── __init__.py      # Re-exports all types for backward compatibility
├── core.py          # Core enums (Token, Side, OrderStatus, etc.)
├── utils.py         # Utility functions (timestamps, price conversions)
├── market_data.py   # Market data snapshots (Polymarket, Binance)
├── strategy.py      # Strategy output types (DesiredQuoteSet)
├── orders.py        # Order specification and lifecycle
├── events.py        # Executor event types
├── gateway.py       # Gateway action/result types
├── state.py         # Inventory and risk state
└── config.py        # Configuration types
```

## Usage

```python
# Backward compatible (imports from __init__.py)
from tradingsystem.types import Token, Side, now_ms

# Explicit imports (preferred for new code)
from tradingsystem.types.core import Token, Side
from tradingsystem.types.utils import now_ms
```

---

## Core Enums (`core.py`)

Fundamental vocabulary used throughout the codebase.

| Enum | Values | Description |
|------|--------|-------------|
| `Token` | `YES`, `NO` | Binary outcome token |
| `Side` | `BUY`, `SELL` | Order side |
| `OrderStatus` | `PENDING_NEW`, `WORKING`, `PENDING_CANCEL`, `PARTIALLY_FILLED`, `FILLED`, `CANCELED`, `REJECTED`, `UNKNOWN` | Order lifecycle status |
| `QuoteMode` | `STOP`, `NORMAL`, `CAUTION`, `ONE_SIDED_BUY`, `ONE_SIDED_SELL` | Strategy quoting mode |
| `ExecutorEventType` | `STRATEGY_INTENT`, `ORDER_ACK`, `CANCEL_ACK`, `FILL`, `GATEWAY_RESULT`, `TIMER_TICK`, `TEST_BARRIER` | Event types processed by Executor |
| `GatewayActionType` | `PLACE`, `CANCEL`, `CANCEL_ALL` | Gateway action types |

---

## Utility Functions (`utils.py`)

Pure functions for timestamps and price conversions.

| Function | Description |
|----------|-------------|
| `now_ms()` | Current monotonic timestamp in milliseconds |
| `wall_ms()` | Current wall clock timestamp in milliseconds |
| `price_to_cents(price_str)` | Convert price string (e.g., "0.55") to cents (55) |
| `cents_to_price(cents)` | Convert cents to decimal price |

---

## Market Data (`market_data.py`)

Types for representing market data from Polymarket and Binance.

### `MarketSnapshotMeta`
Metadata for market data snapshots. Tracks timing and sequence for staleness detection.

### `PolymarketBookTop`
Top-of-book for a single token. Prices in cents (0-100), sizes in shares.
- Properties: `mid_px`, `spread`, `has_bid`, `has_ask`

### `PolymarketBookSnapshot`
Complete Polymarket book snapshot for YES and NO tokens.
- Properties: `yes_mid`, `synthetic_mid`, `arbitrage_yes_top`
- The `arbitrage_yes_top` property computes true tradable prices considering both YES and NO books (since YES + NO = 100).

### `BinanceSnapshot`
Binance market data snapshot used for fair value estimation and shock detection.
- Includes: BBO, trade data, hour context, derived features (return, volatility, shock)
- Optional pricer output: `p_yes`, `p_yes_band_lo`, `p_yes_band_hi`

---

## Strategy Types (`strategy.py`)

Types representing the Strategy's desired quote state.

### `DesiredQuoteLeg`
A single leg (bid or ask) of the desired quote in YES space.
- Fields: `enabled`, `px_yes` (price in cents), `sz` (size in shares)

### `DesiredQuoteSet`
Complete desired quote state from Strategy. Always expressed in canonical YES space.
- Fields: `mode`, `bid_yes`, `ask_yes`, `pm_seq`, `bn_seq`, `reason_flags`
- Class method: `stop()` - creates a STOP intent

---

## Order Types (`orders.py`)

Types for order specification and lifecycle tracking.

### `RealOrderSpec`
Specification for a real order to be placed on the exchange.
- Fields: `token`, `side`, `px` (cents), `sz` (shares), `token_id`, `time_in_force`, `client_order_id`
- Method: `matches(other, price_tol, size_tol)` - check if orders match within tolerance

### `WorkingOrder`
A working order on the exchange. Tracks order state and filled quantity.
- Fields: `client_order_id`, `server_order_id`, `order_spec`, `status`, `filled_sz`, `kind`
- The `kind` field stores order role: `"reduce_sell"`, `"open_buy"`, `"complement_buy"`, or `None`
- Properties: `remaining_sz`, `is_terminal`

---

## Event Types (`events.py`)

All events for the Executor's event-driven architecture.

### `ExecutorEvent`
Base class for all Executor inbox events.

### Event Subclasses

| Event | Description | Key Fields |
|-------|-------------|------------|
| `StrategyIntentEvent` | New intent from Strategy | `intent: DesiredQuoteSet` |
| `OrderAckEvent` | Order placement acknowledged | `client_order_id`, `server_order_id`, `status` |
| `CancelAckEvent` | Order cancellation acknowledged | `server_order_id`, `success`, `reason` |
| `FillEvent` | Order fill event | `token`, `side`, `price`, `size`, `is_pending` |
| `GatewayResultEvent` | Result from Gateway action | `action_id`, `success`, `error_kind` |
| `TimerTickEvent` | Periodic timer tick | - |
| `BarrierEvent` | Test synchronization barrier | `barrier_processed` |

### Fill Event Settlement

`FillEvent` has two phases on Polymarket:
1. **MATCHED** (`is_pending=True`): Order matched off-chain. Tokens NOT in wallet yet.
2. **MINED** (`is_pending=False`): Transaction confirmed on-chain (~17-19 seconds later). Tokens in wallet.

---

## Gateway Types (`gateway.py`)

Types for Gateway actions and results.

### `GatewayAction`
An action to be performed by the Gateway.
- Fields: `action_type`, `action_id`, `order_spec`, `client_order_id`, `server_order_id`, `market_id`

### `GatewayResult`
Result of a Gateway action.
- Fields: `action_id`, `success`, `server_order_id`, `error_kind`, `retryable`

---

## State Types (`state.py`)

Types for tracking inventory and risk state.

### `InventoryState`
Tracks YES and NO token holdings, including pending (MATCHED but not MINED).

**Critical distinction:**
- **Settled** (`I_yes`, `I_no`): Tokens MINED on blockchain. Can be SOLD.
- **Pending** (`pending_yes`, `pending_no`): Tokens MATCHED but not MINED. Cannot be sold yet.

| Use Case | Which Fields |
|----------|--------------|
| Strategy decisions (should I bid?) | `effective_yes`, `effective_no` (settled + pending) |
| Executor SELL orders (how much can I sell?) | `I_yes`, `I_no` only (settled) |

Methods:
- `update_from_fill()` - Update inventory from a fill
- `settle_pending()` - Settle a pending fill (when MINED arrives)

### `RiskState`
Risk management state tracked by Executor.
- Fields: `cooldown_until_ts`, `mode_override`, `stale_pm`, `stale_bn`, `gross_cap_hit`
- Methods: `is_in_cooldown()`, `set_cooldown()`

---

## Configuration Types (`config.py`)

### `ExecutorConfig`
Configuration for Executor.
- Staleness thresholds: `pm_stale_threshold_ms`, `bn_stale_threshold_ms`
- Timeouts: `cancel_timeout_ms`, `place_timeout_ms`
- Caps: `gross_cap`, `max_position`
- Tolerances: `price_tolerance_cents`, `size_tolerance_shares`

### `MarketInfo`
Information about a discovered market.
- Fields: `condition_id`, `question`, `slug`, `yes_token_id`, `no_token_id`, `end_time_utc_ms`
- Property: `time_remaining_ms`
