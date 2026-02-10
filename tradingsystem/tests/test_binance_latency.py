#!/usr/bin/env python3
"""
Measure Binance WS server-to-local latency.

Compares Binance server timestamps (T field in trades, E in bookTicker)
against local receive time to measure true feed latency.

Usage:
    source .venv/bin/activate
    python tradingsystem/tests/test_binance_latency.py [duration_seconds]
"""

import time
import threading
import sys
import statistics

import orjson
import websocket

URL = "wss://stream.binance.com:9443/stream?streams=btcusdt@bookTicker/btcusdt@trade"

# Collect latency samples
trade_latencies_ms: list[float] = []
bbo_latencies_ms: list[float] = []


def on_message(ws, message):
    local_ms = time.time() * 1000

    try:
        msg = orjson.loads(message)
    except Exception:
        return

    stream = msg.get("stream", "")
    data = msg.get("data")
    if data is None:
        return

    if "trade" in stream:
        server_ms = data.get("T")
        if server_ms:
            latency = local_ms - server_ms
            trade_latencies_ms.append(latency)

    elif "bookTicker" in stream:
        # bookTicker has "E" (event time) in some API versions
        server_ms = data.get("E")
        if server_ms:
            latency = local_ms - server_ms
            bbo_latencies_ms.append(latency)


def on_open(ws):
    print(f"[CONNECTED] Collecting latency samples...\n")


def on_error(ws, error):
    print(f"[ERROR] {error}")


def run(duration: int = 10):
    print("=" * 60)
    print("  BINANCE WS LATENCY MEASUREMENT")
    print("=" * 60)
    print(f"\n  Duration: {duration}s")
    print(f"  URL: {URL[:55]}...\n")

    ws = websocket.WebSocketApp(
        URL,
        on_open=on_open,
        on_message=on_message,
        on_error=on_error,
    )

    t = threading.Thread(
        target=lambda: ws.run_forever(ping_interval=20, ping_timeout=10),
        daemon=True,
    )
    t.start()

    try:
        time.sleep(duration)
    except KeyboardInterrupt:
        pass

    ws.close()
    t.join(timeout=3.0)

    # --- Report ---
    print("\n" + "=" * 60)
    print("  RESULTS")
    print("=" * 60)

    for label, samples in [("Trade", trade_latencies_ms), ("BBO (bookTicker)", bbo_latencies_ms)]:
        if not samples:
            print(f"\n  {label}: no server timestamps available")
            continue

        samples_sorted = sorted(samples)
        n = len(samples_sorted)
        med = statistics.median(samples_sorted)
        p5 = samples_sorted[int(n * 0.05)]
        p95 = samples_sorted[int(n * 0.95)]
        p99 = samples_sorted[int(n * 0.99)]
        mn = min(samples_sorted)
        mx = max(samples_sorted)
        avg = statistics.mean(samples_sorted)

        print(f"\n  {label} ({n} samples):")
        print(f"    Min:    {mn:>8.1f} ms")
        print(f"    P5:     {p5:>8.1f} ms")
        print(f"    Median: {med:>8.1f} ms")
        print(f"    Mean:   {avg:>8.1f} ms")
        print(f"    P95:    {p95:>8.1f} ms")
        print(f"    P99:    {p99:>8.1f} ms")
        print(f"    Max:    {mx:>8.1f} ms")

    if trade_latencies_ms:
        print(f"\n  NOTE: Latency = local_clock - binance_server_clock.")
        print(f"  Offset includes clock skew between your machine and Binance.")
        print(f"  Jitter (P95-P5) is the more reliable measure of network quality.")
        jitter = 0
        if len(trade_latencies_ms) > 20:
            s = sorted(trade_latencies_ms)
            jitter = s[int(len(s)*0.95)] - s[int(len(s)*0.05)]
        print(f"  Trade jitter (P95-P5): {jitter:.1f} ms")

    print("\n" + "=" * 60)


if __name__ == "__main__":
    duration = int(sys.argv[1]) if len(sys.argv) > 1 else 10
    run(duration)
