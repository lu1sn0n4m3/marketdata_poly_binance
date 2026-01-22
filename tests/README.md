# HourMM Test Scripts

This directory contains test scripts for validating the HourMM trading framework.

## Prerequisites

1. **Python Dependencies**: Install required packages:
   ```bash
   pip install aiohttp websockets py-clob-client
   ```

2. **Environment Variables**: Set your Polymarket credentials in `deploy/prod.env`:
   ```bash
   PM_PRIVATE_KEY=0x...
   PM_FUNDER=0x...
   PM_SIGNATURE_TYPE=1
   MARKET_SLUG=bitcoin-up-or-down
   ```
   
   All test scripts automatically load from `deploy/prod.env`.

## Test Scripts

### 1. Binance Snapshots Test (`test_binance_snapshots.py`)

Tests Container A (Binance Pricer) components in isolation:
- ✓ BinanceWsClient connection
- ✓ FeatureEngine computation (volatility, returns)
- ✓ BaselinePricer fair value calculation
- ✓ Full snapshot pipeline

```bash
python tests/test_binance_snapshots.py
```

**Expected Output**: Real-time BTC prices, computed features, and snapshots printed to stdout.

---

### 2. Polymarket Market Test (`test_polymarket_market.py`)

Tests Polymarket market discovery and streaming:
- ✓ GammaClient auto-discovers current hourly market (Eastern Time)
- ✓ WebSocket connection to market
- ✓ Real-time BBO and book updates

```bash
python tests/test_polymarket_market.py
```

**Expected Output**: Current market slug, token IDs, and live market data.

---

### 3. Polymarket Orders Test (`test_polymarket_orders.py`)

Tests Polymarket order placement/cancellation using HourMM framework:
- ✓ PolymarketRestClient initialization with `py-clob-client`
- ✓ Order signing and placement
- ✓ Open orders query
- ✓ Order cancellation

**REQUIRES CREDENTIALS** in `deploy/prod.env`.

```bash
python tests/test_polymarket_orders.py
```

**Expected Output**:
- Client credential derivation
- Test orders placed at $0.01 (no execution risk)
- Open orders displayed
- Orders cancelled

---

### 4. End-to-End Test (`test_end_to_end.py`)

**Simple component-level test** - manually wires components together.

**Container A (Binance Pricer)**:
1. BinanceWsClient streams BTCUSDT prices
2. FeatureEngine computes volatility features
3. BaselinePricer calculates fair YES probability
4. SnapshotPublisher builds snapshots at 2 Hz
5. SnapshotServer exposes HTTP endpoint on `http://127.0.0.1:8080`

**Container B (Polymarket Trader)**:
1. GammaClient discovers current hourly market
2. SnapshotPoller fetches from Container A at 2 Hz
3. DummyStrategy generates orders (BUY YES at $0.01, size 5)
4. Orders placed/cancelled every 0.5 seconds

**REQUIRES CREDENTIALS** in `deploy/prod.env`.

```bash
python tests/test_end_to_end.py
```

---

### 5. Production E2E Test (`test_production_e2e.py`) ⭐⭐⭐

**PRODUCTION-READY** test using actual `ContainerAApp` and `ContainerBApp` classes.

This is NOT a toy example - it uses the real application classes exactly as they run in production with Docker.

**What it tests**:
- ✅ `ContainerAApp` with full component wiring (as in production)
- ✅ `ContainerBApp` with full trading pipeline (as in production)
- ✅ All components: WS clients, REST client, StateReducer, RiskEngine, Strategy, OrderManager, Executor
- ✅ Real Binance data streaming
- ✅ Real Polymarket order placement with `OpportunisticQuoteStrategy`
- ✅ Canonical state management with event sourcing
- ✅ Risk limits enforcement
- ✅ Order reconciliation and lifecycle management

**REQUIRES CREDENTIALS** in `deploy/prod.env`.

```bash
python tests/test_production_e2e.py
```

**Expected Output**:
```
======================================================================
  PRODUCTION-STYLE END-TO-END TEST
======================================================================

Using actual ContainerAApp and ContainerBApp classes
Press Ctrl+C to stop.

[Setup] Creating configurations...
[Setup] Container A: BTCUSDT @ 2 Hz
[Setup] Container B: bitcoin-up-or-down
[Setup] Risk limits: $5.0 max order size

[Startup] Starting both containers...

INFO - Starting Container A (Binance Pricer)...
INFO - Container A started successfully
INFO - Starting Container B (Polymarket Trader)...
INFO - Container B started successfully

======================================================================
  RUNTIME: 10.2s
======================================================================

[Container A - Binance Pricer]
  Snapshot seq:    #20
  BTC price:       $90235.67
  p_yes_fair:      0.5234
  Age:             45ms
  Stale:           False

[Container B - Polymarket Trader]
  Session state:   ACTIVE
  Risk mode:       NORMAL
  Market:          bitcoin-up-or-down-january-22-8am-et
  Position (YES):  0.00
  Position (NO):   0.00
  Open orders:     2
  Total committed: 10.00 contracts
```

**Key Differences from `test_end_to_end.py`**:
- ✅ Uses real App classes (not manual component wiring)
- ✅ Uses `OpportunisticQuoteStrategy` (not DummyStrategy)
- ✅ Full state management with `StateReducer` and event journal
- ✅ Real risk engine with limit enforcement
- ✅ Order manager with desired-state reconciliation
- ✅ Complete trading pipeline as it runs in production

---

## OLD: Simple Component Test (`test_end_to_end.py`)

For comparison, the original `test_end_to_end.py` manually wires components:

**Container A (Binance Pricer)**:
1. BinanceWsClient streams BTCUSDT prices
2. FeatureEngine computes volatility features
3. BaselinePricer calculates fair YES probability
4. SnapshotPublisher builds snapshots at 2 Hz
5. SnapshotServer exposes HTTP endpoint on `http://127.0.0.1:8080`

**Container B (Polymarket Trader)**:
1. GammaClient discovers current hourly market
2. SnapshotPoller fetches from Container A at 2 Hz
3. DummyStrategy generates orders (BUY YES at $0.01, size 1, TTL 500ms)
4. OrderManager reconciles desired vs actual orders
5. Executor places/cancels orders via PolymarketRestClient

**REQUIRES CREDENTIALS** in `deploy/prod.env`.

```bash
python tests/test_end_to_end.py
```

**Expected Behavior**:
- Container A streams Binance data and builds snapshots
- Container B polls snapshots and places test orders every 0.5 seconds
- Previous orders are cancelled before placing new ones
- Statistics printed every 10 seconds
- Press `Ctrl+C` to stop

**What You'll See**:
```
=== Setting up Container A (Binance Pricer) ===
✓ Container A components initialized
  - BinanceWsClient: BTCUSDT
  - SnapshotPublisher: 2 Hz
  - SnapshotServer: http://127.0.0.1:8080

=== Setting up Container B (Polymarket Trader) ===
  ✓ Found market: bitcoin-up-or-down-january-22-7am-et
  ✓ YES token ID: 123456...
  ✓ PolymarketRestClient initialized
  ✓ DummyStrategy: Always BUY YES at $0.01, size 1, TTL 500ms

=== Starting Trading Loop ===

[Snapshot #1] Placing order:
  Token: 123456...
  Side: BUY
  Price: $0.01
  Size: 1
  BTC Price: $104523.45
  ✓ Order placed: abc-123-def

  Canceling previous order: abc-123-def

[Snapshot #2] Placing order:
  ...

--- Stats (after 10s) ---
  Snapshots received: 20
  Orders placed: 20
  Orders cancelled: 19
```

---

## Running All Tests

```bash
# Test individual components first
python tests/test_binance_snapshots.py
python tests/test_polymarket_market.py
python tests/test_polymarket_orders.py

# Then run full end-to-end test
python tests/test_end_to_end.py
```

## Troubleshooting

### Import Errors
Tests automatically add paths for HourMM modules. If you see import errors, verify:
```bash
ls services/binance_pricer/src/binance_pricer/
ls services/polymarket_trader/src/polymarket_trader/
ls shared/hourmm_common/
```

### Missing Credentials
If you see "PM_PRIVATE_KEY not set":
1. Check `deploy/prod.env` exists and has credentials
2. Verify the file is readable
3. Try manually sourcing: `source deploy/prod.env && python tests/test_polymarket_orders.py`

### Order Placement Fails
- **"Invalid signature"**: Check `PM_PRIVATE_KEY`, `PM_FUNDER`, and `PM_SIGNATURE_TYPE` match your wallet
- **"Insufficient balance"**: Verify your wallet has USDC on Polygon
- **"Market not found"**: The hourly market might not exist yet or has ended

### WebSocket Connection Issues
- Binance: Check internet connectivity and firewall
- Polymarket: Verify `wss://ws-subscriptions-clob.polymarket.com` is accessible

## Notes

- **Testing Wallet**: Use a wallet with limited funds (~$10) to avoid accidental capital loss
- **Order Safety**: Test scripts use $0.01 buy orders to minimize execution risk
- **Rate Limits**: Polymarket has rate limits; avoid running multiple test instances simultaneously
- **Market Hours**: Polymarket hourly markets may not always be available
