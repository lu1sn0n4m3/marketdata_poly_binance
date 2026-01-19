#!/usr/bin/env python3
"""
Quick Binance latency test (Frankfurt VPS)

Measures: Binance exchange timestamp → local receive time

Usage:
    uv run --with websockets test_binance_direct.py
"""

import asyncio
import json
import time
import statistics

try:
    import websockets
except ImportError:
    print("Run: uv run --with websockets test_binance_direct.py")
    exit(1)

WS_URL = "wss://stream.binance.com:9443/ws/btcusdt@trade"
DURATION = 30  # seconds


async def main():
    print(f"[BINANCE] Connecting to {WS_URL}...")
    
    latencies = []
    start = time.time()
    
    try:
        async with websockets.connect(WS_URL) as ws:
            print(f"[BINANCE] Connected, measuring for {DURATION}s...")
            
            async for msg in ws:
                data = json.loads(msg)
                
                # Trade message has "T" (trade time) and "E" (event time)
                exch_ts = data.get("E")  # Event time in ms
                if not exch_ts:
                    continue
                
                recv_ts = int(time.time() * 1000)
                latency_ms = recv_ts - exch_ts
                latencies.append(latency_ms)
                
                elapsed = time.time() - start
                if elapsed >= DURATION:
                    break
                
                # Print progress every second
                if len(latencies) % 50 == 0:
                    p50 = statistics.median(latencies[-100:]) if len(latencies) >= 100 else statistics.median(latencies)
                    print(f"  [{elapsed:5.1f}s] {len(latencies)} trades | p50={p50:.1f}ms")
    
    except Exception as e:
        print(f"[ERROR] {e}")
        return
    
    # Final results
    if latencies:
        print(f"\n{'='*50}")
        print(f"BINANCE → FRANKFURT LATENCY ({len(latencies)} trades)")
        print(f"{'='*50}")
        print(f"  Min:    {min(latencies):6.1f} ms")
        print(f"  Max:    {max(latencies):6.1f} ms")
        print(f"  Mean:   {statistics.mean(latencies):6.1f} ms")
        print(f"  Median: {statistics.median(latencies):6.1f} ms")
        print(f"  StdDev: {statistics.stdev(latencies):6.1f} ms")
        
        sorted_lat = sorted(latencies)
        p95 = sorted_lat[int(len(sorted_lat) * 0.95)]
        p99 = sorted_lat[int(len(sorted_lat) * 0.99)]
        print(f"  P95:    {p95:6.1f} ms")
        print(f"  P99:    {p99:6.1f} ms")
        print(f"{'='*50}")


if __name__ == "__main__":
    asyncio.run(main())
