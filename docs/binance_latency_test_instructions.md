# End-to-End Binance Latency Test Instructions

## Goal
Run **real end-to-end latency tests using live Binance market data**.

- **binancestreamer (Frankfurt)**  
  Connects to Binance, ingests live data, derives a minimal signal, and streams it over TLS.

- **tradingexecutor-amsterdam (Amsterdam)**  
  Listens over TLS, receives the signal, validates ordering and latency, and logs results.

**Do not modify the transport layer** (TLS, framing, reconnect logic). Only replace the heartbeat payload with real data.

---

## Binance Setup (Streamer Side)

On **binancestreamer**:

1. Use the **Binance public WebSocket API** (no authentication).
2. Subscribe to a **single stream only** to keep noise low:
   - Recommended:
     ```
     wss://stream.binance.com:9443/ws/btcusdt@bookTicker
     ```
3. Do **not** subscribe to depth or trade streams yet.

From each Binance message, extract:
- `symbol`
- `bestBid`
- `bestAsk`
- `eventTime` (Binance E)

Derive:
```
mid = (bestBid + bestAsk) / 2
```

---

## Payload to Send (Frankfurt → Amsterdam)

In `sender.py`, replace the heartbeat payload with **one compact message per Binance update**:

```python
{
  "seq": seq,                    # monotonically increasing integer
  "ts": time.time_ns(),           # Frankfurt send timestamp
  "symbol": "BTCUSDT",
  "mid": mid_price,
  "bid": best_bid,
  "ask": best_ask,
  "binance_ts": event_time_ms
}
```

Rules:
- One message per update
- No buffering
- If updates arrive too fast, **drop stale updates** (keep latest only)

Keep unchanged:
- TLS
- msgpack framing
- reconnect loop

---

## Listener Behavior (Amsterdam)

In `receiver.py` on **tradingexecutor-amsterdam**, for each message:

1. Validate ordering:
   - `seq` must be strictly increasing
2. Measure latency:
   ```python
   latency_us = (time.time_ns() - msg["ts"]) / 1_000
   ```
3. Log:
   - symbol
   - mid price
   - latency (µs)
   - sequence gaps (if any)

Notes:
- Do not send acknowledgements
- Do not block on per-message prints (aggregate stats once per second)

---

## Tests to Implement

All tests must use **real Binance data** (no mocks):

### 1. Connectivity
- Stop Amsterdam listener → ensure Frankfurt retries
- Restart Amsterdam → stream resumes automatically

### 2. Latency Distribution
- Collect p50 / p95 / p99 over 30 seconds
- Expect p50 ≈ 8–12 ms

### 3. Backpressure
- Artificially delay Amsterdam processing
- Verify Frankfurt drops stale updates instead of queueing

### 4. Sequence Integrity
- Ensure no reordering
- Log gaps when drops occur

---

## Explicit Constraints

- ❌ Do not stream raw Binance JSON
- ❌ Do not add REST calls
- ❌ Do not add ACKs or RTT logic
- ❌ Do not modify SSH, firewall, or ports
- ❌ Do not introduce async queues on Amsterdam

This is a **one-way, low-latency signal pipe**, not a general message bus.

---

## Success Criteria

The setup is correct if:

- Amsterdam prints live BTCUSDT updates
- Latency is stable (~9 ms typical)
- No unbounded memory growth
- Frankfurt recovers cleanly from disconnects
