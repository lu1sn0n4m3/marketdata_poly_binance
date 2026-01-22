# HourMM Usage Guide

This guide explains how to set up, configure, test, and operate the HourMM trading system.

## Table of Contents

1. [Architecture Overview](#architecture-overview)
2. [Prerequisites](#prerequisites)
3. [Configuration](#configuration)
4. [Testing Connections](#testing-connections)
5. [Running the System](#running-the-system)
6. [Operator Controls](#operator-controls)
7. [Monitoring](#monitoring)
8. [Troubleshooting](#troubleshooting)

---

## Architecture Overview

HourMM consists of two Docker containers:

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                              VPS / Server                                    │
│                                                                              │
│  ┌─────────────────────────┐     HTTP Poll      ┌────────────────────────┐  │
│  │   Container A           │    (50Hz)          │   Container B          │  │
│  │   binance_pricer        │ ◄──────────────────│   polymarket_trader    │  │
│  │                         │                    │                        │  │
│  │  • Binance WS           │   /snapshot/latest │  • Polymarket WS       │  │
│  │  • Feature Engine       │                    │  • Risk Engine         │  │
│  │  • Pricer               │                    │  • Strategy            │  │
│  │  • Snapshot Publisher   │                    │  • Order Manager       │  │
│  └─────────────────────────┘                    │  • Executor            │  │
│           ▲                                     │  • Control Plane :9000 │  │
│           │ WebSocket                           └────────────────────────┘  │
│           │                                              ▲  │               │
│           │                                              │  │ WebSocket     │
│           │                                              │  │ + REST        │
│           │                                              │  ▼               │
└───────────┼──────────────────────────────────────────────┼──────────────────┘
            │                                              │
            ▼                                              ▼
     ┌──────────────┐                            ┌──────────────────┐
     │   Binance    │                            │   Polymarket     │
     │   (Public)   │                            │   (Auth Required)│
     └──────────────┘                            └──────────────────┘
```

### Data Flow

1. **Container A** ingests Binance BTC trades/BBO via public WebSocket
2. **Container A** computes features (volatility, returns) and fair YES probability
3. **Container A** publishes atomic snapshots at 50Hz via HTTP
4. **Container B** polls Container A for the latest snapshot
5. **Container B** runs the decision loop: Risk → Strategy → OrderManager → Executor
6. **Container B** places/cancels orders on Polymarket via REST API

---

## Prerequisites

### Required Credentials

| Credential | Required By | Description |
|------------|-------------|-------------|
| `PM_PRIVATE_KEY` | Container B | Your wallet private key for signing orders (0x prefixed) |
| `PM_FUNDER` | Container B | Your funder address (0x prefixed) |
| `PM_SIGNATURE_TYPE` | Container B | `1` for EOA, `2` for proxy wallet |

### Optional Credentials

| Credential | Required By | Description |
|------------|-------------|-------------|
| `PM_API_KEY` | Container B | Auto-derived from private key if not provided |
| `PM_API_SECRET` | Container B | Auto-derived from private key if not provided |
| `PM_PASSPHRASE` | Container B | Auto-derived from private key if not provided |

### How to Get Your Polymarket Credentials

1. **Private Key (`PM_PRIVATE_KEY`)**:
   - This is the private key of your Ethereum wallet that you use with Polymarket
   - Export from MetaMask: Settings → Security & Privacy → Export Private Key
   - Must be 0x prefixed (e.g., `0xefc98e5b73c363a72a482450718ab3d4cdf23e1ce21bdbb5d08cfe45f0ffd273`)

2. **Funder Address (`PM_FUNDER`)**:
   - This is your funder/trading address
   - Must be 0x prefixed (e.g., `0x322fA7fbC46fF5D34316a5dDaECDd8ffef851F6f`)

3. **Signature Type (`PM_SIGNATURE_TYPE`)**:
   - Use `1` for EOA (Externally Owned Account) - direct wallet signing
   - Use `2` for proxy wallet (Safe multisig)

### No Credentials Needed

- **Binance** - Uses public WebSocket streams (no API key required)
- **Gamma API** - Public API for market discovery (no key required)

### System Requirements

- Docker and Docker Compose
- Python 3.12+ (for local testing)
- Network access to:
  - `wss://stream.binance.com:9443` (Binance WebSocket)
  - `wss://ws-subscriptions-clob.polymarket.com` (Polymarket WebSocket)
  - `https://clob.polymarket.com` (Polymarket REST API)
  - `https://gamma-api.polymarket.com` (Market Discovery)

---

## Configuration

### Environment Variables

Create a `deploy/prod.env` file with your credentials:

```bash
# =============================================================================
# Polymarket Credentials (REQUIRED for trading)
# =============================================================================

# Your wallet private key for signing orders (0x prefixed)
PM_PRIVATE_KEY=0xefc98e5b73c363a72a482450718ab3d4cdf23e1ce21bdbb5d08cfe45f0ffd273

# Your funder address (0x prefixed)
PM_FUNDER=0x322fA7fbC46fF5D34316a5dDaECDd8ffef851F6f

# Signature type: 1=EOA (direct wallet), 2=Proxy wallet
PM_SIGNATURE_TYPE=1

# =============================================================================
# Optional: L2 API Credentials (auto-derived from private key if not set)
# =============================================================================
# PM_API_KEY=your_api_key
# PM_API_SECRET=your_api_secret
# PM_PASSPHRASE=your_passphrase

# =============================================================================
# Trading Configuration
# =============================================================================
# Base market slug - date/time appended automatically (Eastern Time)
# Example: bitcoin-up-or-down -> bitcoin-up-or-down-january-22-7am-et
MARKET_SLUG=bitcoin-up-or-down

# Risk limits (in USDC) - adjust based on your wallet size
MAX_RESERVED_CAPITAL=5.0
MAX_POSITION_SIZE=5.0
MAX_ORDER_SIZE=5.0
MAX_OPEN_ORDERS=4
```

**Important**: Replace the placeholder values above with your actual credentials!

### Container A Configuration (Binance Pricer)

| Variable | Default | Description |
|----------|---------|-------------|
| `BINANCE_SYMBOL` | `BTCUSDT` | Trading pair to track |
| `HTTP_PORT` | `8080` | Snapshot server port |
| `STALE_THRESHOLD_MS` | `500` | Mark data stale after N ms without updates |
| `SNAPSHOT_PUBLISH_HZ` | `50` | Snapshot publish rate |
| `LOG_LEVEL` | `INFO` | Logging verbosity |

### Container B Configuration (Polymarket Trader)

| Variable | Default | Description |
|----------|---------|-------------|
| `SNAPSHOT_URL` | `http://binance_pricer:8080/snapshot/latest` | Container A endpoint |
| `POLL_SNAPSHOT_HZ` | `50` | Polling rate |
| `MARKET_SLUG` | `bitcoin-up-or-down` | Base market slug (date/time appended automatically) |
| `SQLITE_PATH` | `/data/journal.db` | SQLite journal location |
| `CONTROL_PORT` | `9000` | Operator control plane port |
| `MAX_RESERVED_CAPITAL` | `1000.0` | Max capital per hour |
| `MAX_POSITION_SIZE` | `500.0` | Max position size |
| `MAX_ORDER_SIZE` | `100.0` | Max single order size |
| `MAX_OPEN_ORDERS` | `4` | Max working orders |
| `T_WIND_MS` | `600000` | Start WIND_DOWN 10 min before hour end |
| `T_FLATTEN_MS` | `300000` | Start FLATTEN 5 min before hour end |
| `HARD_CUTOFF_MS` | `60000` | Stop trading 1 min before hour end |

---

## Testing Connections

### 1. Test Binance WebSocket (No Credentials Needed)

```python
#!/usr/bin/env python3
"""Test Binance WebSocket connection."""
import asyncio
import websockets
import json

async def test_binance():
    url = "wss://stream.binance.com:9443/stream?streams=btcusdt@trade/btcusdt@bookTicker"
    
    print(f"Connecting to Binance: {url[:60]}...")
    
    async with websockets.connect(url) as ws:
        print("✓ Connected to Binance WebSocket")
        
        # Receive a few messages
        for i in range(5):
            msg = await asyncio.wait_for(ws.recv(), timeout=5.0)
            data = json.loads(msg)
            stream = data.get("stream", "")
            
            if "trade" in stream:
                payload = data["data"]
                print(f"  Trade: {payload['p']} @ {payload['q']} BTC")
            elif "bookTicker" in stream:
                payload = data["data"]
                print(f"  BBO: {payload['b']} / {payload['a']}")
        
        print("✓ Binance connection test PASSED")

if __name__ == "__main__":
    asyncio.run(test_binance())
```

Save as `test_binance.py` and run:
```bash
python test_binance.py
```

### 2. Test Polymarket Market WebSocket (No Credentials Needed)

```python
#!/usr/bin/env python3
"""Test Polymarket Market WebSocket (public)."""
import asyncio
import websockets
import json

async def test_polymarket_market_ws():
    url = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
    
    # You need a valid token ID - get one from Gamma API first
    # This is just a connection test
    print(f"Connecting to Polymarket Market WS...")
    
    async with websockets.connect(url) as ws:
        print("✓ Connected to Polymarket Market WebSocket")
        
        # Note: To receive data, you need to subscribe with valid token IDs
        # Example subscription (replace with real token IDs):
        # await ws.send(json.dumps({
        #     "type": "market",
        #     "assets_ids": ["token_id_1", "token_id_2"]
        # }))
        
        print("✓ Polymarket Market WS connection test PASSED")
        print("  (Subscribe with valid token IDs to receive data)")

if __name__ == "__main__":
    asyncio.run(test_polymarket_market_ws())
```

### 3. Test Gamma API (Market Discovery)

```python
#!/usr/bin/env python3
"""Test Gamma API for market discovery."""
import asyncio
import aiohttp
from datetime import datetime, timezone, timedelta

async def test_gamma_api():
    base_url = "https://gamma-api.polymarket.com"
    
    print("Testing Gamma API...")
    
    async with aiohttp.ClientSession() as session:
        # Test basic connectivity
        async with session.get(f"{base_url}/events", params={"limit": 1}) as resp:
            if resp.status == 200:
                print("✓ Gamma API is reachable")
            else:
                print(f"✗ Gamma API returned {resp.status}")
                return
        
        # Try to find an hourly BTC market
        # Derive slug based on current Eastern Time
        utc_now = datetime.now(timezone.utc)
        et_tz = timezone(timedelta(hours=-5))
        et_time = utc_now.astimezone(et_tz)
        
        month_names = [
            "january", "february", "march", "april", "may", "june",
            "july", "august", "september", "october", "november", "december",
        ]
        
        month = month_names[et_time.month - 1]
        day = et_time.day
        hour_12 = et_time.hour % 12 or 12
        am_pm = "pm" if et_time.hour >= 12 else "am"
        
        # Common BTC hourly market slug pattern
        slug = f"bitcoin-up-or-down-{month}-{day}-{hour_12}{am_pm}-et"
        
        print(f"  Looking for market: {slug}")
        
        async with session.get(f"{base_url}/events", params={"slug": slug}) as resp:
            if resp.status == 200:
                data = await resp.json()
                if data:
                    event = data[0] if isinstance(data, list) else data
                    markets = event.get("markets", [])
                    
                    if markets:
                        market = markets[0]
                        print(f"✓ Found market: {market.get('question', 'N/A')}")
                        
                        token_ids = market.get("clobTokenIds", [])
                        if isinstance(token_ids, str):
                            import json as js
                            token_ids = js.loads(token_ids)
                        
                        if token_ids:
                            print(f"  YES token: {token_ids[0][:20]}...")
                            print(f"  NO token:  {token_ids[1][:20]}...")
                        
                        print("✓ Gamma API test PASSED")
                        return
            
            print(f"  Market '{slug}' not found (may not exist yet)")
            print("  This is OK - markets are created closer to their hour")

if __name__ == "__main__":
    asyncio.run(test_gamma_api())
```

### 4. Test Polymarket Order Signing (Requires Credentials)

This test verifies that order signing works correctly using the `py-clob-client` library:

```python
#!/usr/bin/env python3
"""Test Polymarket order signing with py-clob-client."""
import os

def test_polymarket_signing():
    """Test that we can initialize the client and sign orders."""
    
    private_key = os.environ.get("PM_PRIVATE_KEY", "")
    funder = os.environ.get("PM_FUNDER", "")
    sig_type = int(os.environ.get("PM_SIGNATURE_TYPE", "2"))
    
    if not private_key:
        print("✗ PM_PRIVATE_KEY not set")
        print("  export PM_PRIVATE_KEY=0x_your_private_key")
        return
    
    if not funder:
        print("✗ PM_FUNDER not set")
        print("  export PM_FUNDER=0x_your_proxy_wallet_address")
        return
    
    print("Testing Polymarket order signing...")
    
    try:
        from py_clob_client.client import ClobClient
        from py_clob_client.clob_types import OrderArgs
        from py_clob_client.order_builder.constants import BUY
        
        # Ensure 0x prefix
        if not private_key.startswith("0x"):
            private_key = "0x" + private_key
        
        # Initialize client
        client = ClobClient(
            host="https://clob.polymarket.com",
            chain_id=137,
            key=private_key,
            funder=funder,
            signature_type=sig_type,
        )
        print("✓ ClobClient initialized")
        
        # Derive API credentials
        creds = client.create_or_derive_api_creds()
        client.set_api_creds(creds)
        print("✓ API credentials derived from private key")
        print(f"  API Key: {creds.api_key[:20]}...")
        
        # Test order creation (signing only, don't post)
        # Use a dummy token ID - this won't be posted
        test_order = OrderArgs(
            token_id="0" * 64,  # Dummy token
            price=0.50,
            size=10.0,
            side=BUY,
        )
        
        signed = client.create_order(test_order)
        if signed:
            print("✓ Order signing works correctly")
            print(f"  Signature present: {'signature' in str(signed)}")
        
        # Test authenticated endpoint
        try:
            orders = client.get_orders()
            print(f"✓ Authentication successful")
            print(f"  Open orders: {len(orders) if orders else 0}")
        except Exception as e:
            print(f"⚠ Could not fetch orders: {e}")
        
        print("✓ Polymarket signing test PASSED")
        
    except ImportError:
        print("✗ py-clob-client not installed")
        print("  pip install py-clob-client")
    except Exception as e:
        print(f"✗ Test failed: {e}")

if __name__ == "__main__":
    test_polymarket_signing()
```

Run with credentials:
```bash
export PM_PRIVATE_KEY=0xefc98e5b73c363a72a482450718ab3d4cdf23e1ce21bdbb5d08cfe45f0ffd273
export PM_FUNDER=0x322fA7fbC46fF5D34316a5dDaECDd8ffef851F6f
export PM_SIGNATURE_TYPE=1

# Install dependency
pip install py-clob-client

# Run test
python test_polymarket_signing.py
```

**Important**: Replace the placeholder values above with your actual credentials!

### 5. Test Container A Standalone

```bash
# Run Container A only
docker compose up -d binance_pricer

# Wait for startup
sleep 10

# Test the snapshot endpoint
curl http://localhost:8080/snapshot/latest | python -m json.tool

# Check health
curl http://localhost:8080/health | python -m json.tool

# Expected output:
# {
#   "healthy": true,
#   "has_snapshot": true,
#   "stale": false,
#   "seq": 1234,
#   "age_ms": 45
# }
```

### 6. Run Smoke Tests Locally

```bash
# Test Container A components
cd /path/to/marketdata_poly_binance
python services/binance_pricer/tests/test_smoke.py

# Test Container B components
python services/polymarket_trader/tests/test_smoke.py
```

---

## Running the System

### Start Both Containers

```bash
# Make sure prod.env exists with credentials
cd /path/to/marketdata_poly_binance

# Start the HourMM system
docker compose up -d binance_pricer polymarket_trader

# View logs
docker compose logs -f binance_pricer polymarket_trader
```

### Start in Dry-Run Mode (No Orders)

To test without placing real orders, you can set quoting disabled at startup:

```bash
# Start containers
docker compose up -d binance_pricer polymarket_trader

# Immediately disable quoting
curl -X POST http://localhost:9000/pause_quoting -d '{"pause": true}'
```

### Stop the System

```bash
docker compose down binance_pricer polymarket_trader
```

---

## Operator Controls

The control plane runs on `localhost:9000` (Container B only).

### Check System Status

```bash
curl http://localhost:9000/status | python -m json.tool
```

Response:
```json
{
  "session_state": "ACTIVE",
  "risk_mode": "NORMAL",
  "positions": {
    "yes_tokens": 50.0,
    "no_tokens": 0.0
  },
  "open_orders": 2,
  "health": {
    "market_ws": true,
    "user_ws": true,
    "rest": true,
    "binance_stale": false
  },
  "market_id": "0x1234..."
}
```

### Pause Quoting

```bash
# Pause (stop placing new quotes)
curl -X POST http://localhost:9000/pause_quoting \
  -H "Content-Type: application/json" \
  -d '{"pause": true}'

# Resume
curl -X POST http://localhost:9000/pause_quoting \
  -H "Content-Type: application/json" \
  -d '{"pause": false}'
```

### Change Risk Mode

```bash
# Set to REDUCE_ONLY (no new exposure)
curl -X POST http://localhost:9000/set_mode \
  -H "Content-Type: application/json" \
  -d '{"mode": "REDUCE_ONLY"}'

# Available modes: NORMAL, REDUCE_ONLY, FLATTEN, HALT
```

### Emergency Flatten

```bash
# Immediately flatten all positions
curl -X POST http://localhost:9000/flatten_now
```

### Adjust Limits

```bash
curl -X POST http://localhost:9000/set_limits \
  -H "Content-Type: application/json" \
  -d '{
    "max_reserved_capital": 500.0,
    "max_order_size": 50.0
  }'
```

---

## Monitoring

### Log Files

```bash
# View live logs
docker compose logs -f binance_pricer polymarket_trader

# Container A logs
docker logs hourmm-binance-pricer -f

# Container B logs
docker logs hourmm-polymarket-trader -f
```

### Key Log Messages to Watch

```
# Good: Normal operation
INFO | Container A started
INFO | Connected to Binance for BTCUSDT
INFO | Hour 2026-01-22T14:00:00Z open price set: 50000.0
INFO | Selected market: bitcoin-up-or-down-january-22-2pm-et

# Warning: Degraded state
WARNING | Binance stale beyond threshold
WARNING | WS user disconnected - escalating to HALT

# Critical: Manual intervention may be needed
ERROR | Cancel-all failed
ERROR | REST authentication failed
```

### SQLite Journal

The event journal is stored in the `hourmm_journal` Docker volume:

```bash
# Access the journal
docker exec -it hourmm-polymarket-trader sqlite3 /data/journal.db

# View recent events
SELECT * FROM events ORDER BY id DESC LIMIT 10;

# View state snapshots
SELECT * FROM snapshots ORDER BY id DESC LIMIT 5;
```

---

## Troubleshooting

### Container A Not Publishing Snapshots

1. Check Binance connection:
   ```bash
   docker logs hourmm-binance-pricer | grep -i "binance"
   ```

2. Verify snapshot endpoint:
   ```bash
   curl http://localhost:8080/snapshot/latest
   ```

3. Check if data is stale:
   ```bash
   curl http://localhost:8080/health
   ```

### Container B Not Placing Orders

1. Check risk mode:
   ```bash
   curl http://localhost:9000/status
   ```
   If `risk_mode` is `HALT`, check WebSocket connections.

2. Check Polymarket credentials:
   - **REQUIRED**: Verify `PM_PRIVATE_KEY` and `PM_FUNDER` are set
   - `PM_PRIVATE_KEY` must be your wallet's private key (0x prefixed)
   - `PM_FUNDER` must be your funder address (0x prefixed)
   - `PM_SIGNATURE_TYPE` should be `1` for EOA or `2` for proxy wallet
   - Run the signing test script above to verify

3. Check for signing errors in logs:
   ```bash
   docker logs hourmm-polymarket-trader | grep -i "sign\|auth\|credentials"
   ```

4. Check market discovery:
   ```bash
   docker logs hourmm-polymarket-trader | grep -i "market"
   ```

5. Common signing errors:
   - `"Invalid signature"` - Wrong private key or funder address
   - `"Unauthorized"` - API credentials invalid (try removing PM_API_KEY etc. to auto-derive)
   - `"Client not initialized"` - py-clob-client failed to start

### WebSocket Disconnections

The system automatically reconnects with exponential backoff. If disconnections are frequent:

1. Check network connectivity
2. Check if Binance/Polymarket have rate limits
3. Review the reconnection logs for error messages

### Position Stuck at End of Hour

If the system didn't flatten before hour end:

1. Check `T_FLATTEN_MS` configuration (default: 5 min before hour end)
2. Review logs around hour boundary
3. Manually flatten via control plane:
   ```bash
   curl -X POST http://localhost:9000/flatten_now
   ```

---

## Next Steps

- See [HOURMM_DEVELOPER_GUIDE.md](./HOURMM_DEVELOPER_GUIDE.md) for implementing custom pricers and strategies
- Review the whitepaper (`hourmm_whitepaper.tex`) for detailed system design
- Check the framework specification (`hourmm_framework.md`) for class interfaces
