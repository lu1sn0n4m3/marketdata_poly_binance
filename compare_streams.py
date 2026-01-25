#!/usr/bin/env python3
"""
Side-by-side comparison of Binance direct vs Polymarket relay streams.

Measures:
1. Relay delay: How much later does a price appear on PM vs Binance?
2. Update rate difference
3. Price accuracy

Usage:
    source .venv/bin/activate && python compare_streams.py [-d SECONDS]
"""

import asyncio
import json
import signal
import sys
import time
from dataclasses import dataclass, field
from typing import Optional

import websockets

# Endpoints
BINANCE_WS = "wss://stream.binance.com:9443/ws/btcusdt@trade"
PM_WS = "wss://ws-live-data.polymarket.com"
SYMBOL = "btcusdt"
PING_INTERVAL = 5.0


@dataclass
class StreamStats:
    """Stats for a single stream."""
    name: str
    msg_count: int = 0
    latencies: list[int] = field(default_factory=list)
    prices: list[tuple[float, float]] = field(default_factory=list)  # (local_ts, price)
    last_price: Optional[float] = None
    last_local_ts: Optional[float] = None
    start_time: float = 0.0


@dataclass
class ComparisonStats:
    """Comparison metrics between streams."""
    # Relay delays: when PM shows a price that Binance showed earlier
    relay_delays: list[float] = field(default_factory=list)  # ms
    # Binance trades keyed by server timestamp (rounded to second)
    # Value: list of (local_ts, price) - ALL trades for that second
    binance_by_ts: dict[int, list[tuple[float, float]]] = field(default_factory=dict)
    # Track matches
    matched_count: int = 0
    unmatched_count: int = 0
    price_match_type: dict[str, int] = field(default_factory=lambda: {"exact": 0, "close": 0, "none": 0})


binance = StreamStats(name="Binance")
pm = StreamStats(name="Polymarket")
comparison = ComparisonStats()
running = True


def record_binance_trade(price: float, local_ts: float, event_ts_ms: int) -> None:
    """Record a trade from Binance with its event timestamp."""
    binance.msg_count += 1
    binance.prices.append((local_ts, price))
    binance.last_price = price
    binance.last_local_ts = local_ts

    # Track Binance direct latency (event timestamp -> local receipt)
    local_ts_ms = int(local_ts * 1000)
    latency = local_ts_ms - event_ts_ms
    if latency > 0:
        binance.latencies.append(latency)

    # Key by timestamp rounded to second (PM sends 1/sec with second-precision ts)
    ts_key = (event_ts_ms // 1000) * 1000  # Round to second

    # Store ALL trades for each second
    if ts_key not in comparison.binance_by_ts:
        comparison.binance_by_ts[ts_key] = []
    comparison.binance_by_ts[ts_key].append((local_ts, price))


def record_pm_price(price: float, local_ts: float, server_ts_ms: int) -> None:
    """Record a price from Polymarket and calculate relay delay."""
    pm.msg_count += 1
    pm.prices.append((local_ts, price))
    pm.last_price = price
    pm.last_local_ts = local_ts
    pm.latencies.append(int(local_ts * 1000) - server_ts_ms)

    # Match on timestamp - PM's server_ts should match a Binance timestamp
    ts_key = server_ts_ms  # PM timestamps are already second-aligned

    if ts_key not in comparison.binance_by_ts:
        comparison.unmatched_count += 1
        # Debug: show first few misses
        if comparison.unmatched_count <= 3:
            available_keys = sorted(comparison.binance_by_ts.keys())[-3:] if comparison.binance_by_ts else []
            print(f"  [MISS] PM ts={ts_key} not in Binance (have: {available_keys})")
        return

    # Find the Binance trade that matches this price
    binance_trades = comparison.binance_by_ts[ts_key]

    # Look for exact price match (within $0.01)
    matching_trade = None
    match_type = "none"

    for b_local_ts, b_price in binance_trades:
        if abs(b_price - price) < 0.01:
            matching_trade = (b_local_ts, b_price)
            match_type = "exact"
            break

    # If no exact match, find closest price
    if not matching_trade:
        closest = min(binance_trades, key=lambda t: abs(t[1] - price))
        if abs(closest[1] - price) < 1.0:  # Within $1
            matching_trade = closest
            match_type = "close"

    if matching_trade:
        binance_local_ts, binance_price = matching_trade

        # Calculate relay delay: how much later PM arrived vs Binance for same price
        relay_delay = (local_ts - binance_local_ts) * 1000  # ms
        if relay_delay > 0:
            comparison.relay_delays.append(relay_delay)
            comparison.matched_count += 1
            comparison.price_match_type[match_type] += 1

            # Debug: show first few matches
            if comparison.matched_count <= 5:
                print(f"  [MATCH-{match_type}] ts={ts_key} Binance=${binance_price:.2f} PM=${price:.2f} "
                      f"delay={relay_delay:.0f}ms (checked {len(binance_trades)} trades)")
    else:
        comparison.price_match_type["none"] += 1
        if comparison.price_match_type["none"] <= 3:
            prices_in_second = [f"${t[1]:.2f}" for t in binance_trades[:5]]
            print(f"  [NO PRICE MATCH] ts={ts_key} PM=${price:.2f} Binance had: {prices_in_second}")


async def binance_stream() -> None:
    """Connect to Binance trade stream."""
    global running
    print(f"[Binance] Connecting to {BINANCE_WS[:50]}...")

    try:
        async with websockets.connect(BINANCE_WS) as ws:
            print("[Binance] Connected")
            binance.start_time = time.monotonic()

            while running:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=1.0)
                    local_ts = time.time()

                    data = json.loads(raw)
                    price = float(data.get("p", 0))
                    event_ts = data.get("T", 0)  # Trade timestamp from Binance

                    if price > 0 and event_ts > 0:
                        record_binance_trade(price, local_ts, event_ts)

                except asyncio.TimeoutError:
                    continue
                except websockets.ConnectionClosed:
                    print("[Binance] Connection closed")
                    break
    except Exception as e:
        print(f"[Binance] Error: {e}")


async def pm_stream() -> None:
    """Connect to Polymarket crypto prices stream."""
    global running
    print(f"[PM] Connecting to {PM_WS}...")

    try:
        async with websockets.connect(PM_WS) as ws:
            print("[PM] Connected")

            # Subscribe
            sub_msg = {
                "action": "subscribe",
                "subscriptions": [{"topic": "crypto_prices", "type": "update"}]
            }
            await ws.send(json.dumps(sub_msg))
            pm.start_time = time.monotonic()

            # Ping task
            async def ping():
                while running:
                    await asyncio.sleep(PING_INTERVAL)
                    try:
                        await ws.send(json.dumps({"action": "ping"}))
                    except:
                        break

            ping_task = asyncio.create_task(ping())

            try:
                while running:
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=1.0)
                        local_ts = time.time()
                        local_ts_ms = int(local_ts * 1000)

                        data = json.loads(raw)

                        topic = data.get("topic", "")
                        payload = data.get("payload", {})
                        symbol = payload.get("symbol", "")

                        if topic == "crypto_prices" and symbol.lower() == SYMBOL:
                            price = float(payload.get("value", 0))
                            server_ts = payload.get("timestamp", 0)  # Binance timestamp

                            if price > 0 and server_ts > 0:
                                record_pm_price(price, local_ts, server_ts)

                    except asyncio.TimeoutError:
                        continue
                    except json.JSONDecodeError:
                        continue
                    except websockets.ConnectionClosed:
                        print("[PM] Connection closed")
                        break
            finally:
                ping_task.cancel()
                try:
                    await ping_task
                except asyncio.CancelledError:
                    pass

    except Exception as e:
        print(f"[PM] Error: {e}")


def print_summary(duration: float) -> None:
    """Print comparison summary."""
    print("\n" + "=" * 70)
    print("BINANCE vs POLYMARKET STREAM COMPARISON")
    print("=" * 70)

    print(f"\n  Duration: {duration:.1f}s")

    # Message rates
    print(f"\n  UPDATE RATES:")
    print(f"    Binance:    {binance.msg_count:,} msgs ({binance.msg_count/duration:.1f}/s)")
    print(f"    Polymarket: {pm.msg_count:,} msgs ({pm.msg_count/duration:.1f}/s)")
    print(f"    Ratio:      {binance.msg_count/max(1,pm.msg_count):.1f}x more on Binance")

    # Binance direct latency (trade timestamp -> local)
    binance_avg_lat = 0
    if binance.latencies:
        binance_avg_lat = sum(binance.latencies) / len(binance.latencies)
        sorted_b = sorted(binance.latencies)
        n_b = len(sorted_b)
        b_p50 = sorted_b[n_b // 2]
        b_p95 = sorted_b[int(n_b * 0.95)] if n_b >= 20 else sorted_b[-1]

        print(f"\n  BINANCE DIRECT LATENCY (trade timestamp -> local):")
        print(f"    Min/Avg/P50/P95/Max: {min(binance.latencies)}/{binance_avg_lat:.0f}/{b_p50}/{b_p95}/{max(binance.latencies)} ms")

    # Polymarket latency (server -> local)
    pm_avg_lat = 0
    if pm.latencies:
        pm_avg_lat = sum(pm.latencies) / len(pm.latencies)
        sorted_lat = sorted(pm.latencies)
        n = len(sorted_lat)
        p50 = sorted_lat[n // 2]
        p95 = sorted_lat[int(n * 0.95)] if n >= 20 else sorted_lat[-1]

        print(f"\n  POLYMARKET LATENCY (Binance timestamp -> local):")
        print(f"    Min/Avg/P50/P95/Max: {min(pm.latencies)}/{pm_avg_lat:.0f}/{p50}/{p95}/{max(pm.latencies)} ms")

    # Direct comparison
    if binance.latencies and pm.latencies:
        diff = pm_avg_lat - binance_avg_lat
        print(f"\n  ==> LATENCY DIFFERENCE: Polymarket is {diff:.0f}ms slower than direct Binance")

    # RELAY DELAY - the key metric!
    if comparison.relay_delays:
        delays = comparison.relay_delays
        avg_delay = sum(delays) / len(delays)
        sorted_d = sorted(delays)
        n = len(sorted_d)
        p50 = sorted_d[n // 2]
        p95 = sorted_d[int(n * 0.95)] if n >= 20 else sorted_d[-1]

        print(f"\n  *** RELAY DELAY (Binance -> Polymarket) ***")
        print(f"    Method: Match by timestamp (PM reports which Binance second it's from)")
        print(f"    Matched samples:    {comparison.matched_count}")
        print(f"    Unmatched PM msgs:  {comparison.unmatched_count} (Binance data not yet received)")
        print(f"    Min/Avg/P50/P95/Max: {min(delays):.0f}/{avg_delay:.0f}/{p50:.0f}/{p95:.0f}/{max(delays):.0f} ms")
        print(f"    This is how much LATER prices appear on PM vs Binance direct")
    else:
        print(f"\n  RELAY DELAY: No matching timestamps found")
        print(f"    Matched: {comparison.matched_count}, Unmatched: {comparison.unmatched_count}")

    # Price matching quality
    print(f"\n  PRICE MATCHING QUALITY:")
    print(f"    Exact matches (<$0.01):  {comparison.price_match_type['exact']}")
    print(f"    Close matches (<$1.00):  {comparison.price_match_type['close']}")
    print(f"    No price match:          {comparison.price_match_type['none']}")

    # Last prices
    print(f"\n  LAST PRICES:")
    if binance.last_price:
        print(f"    Binance:    ${binance.last_price:,.2f}")
    if pm.last_price:
        print(f"    Polymarket: ${pm.last_price:,.2f}")
    if binance.last_price and pm.last_price:
        diff = pm.last_price - binance.last_price
        print(f"    Difference: ${diff:+.2f}")

    print("\n" + "=" * 70)
    print("CONCLUSION:")
    if comparison.relay_delays:
        avg_delay = sum(comparison.relay_delays) / len(comparison.relay_delays)
        print(f"  Polymarket relay adds ~{avg_delay:.0f}ms latency vs direct Binance")
        print(f"  Binance sends {binance.msg_count/max(1,pm.msg_count):.0f}x more updates (every trade vs 1/sec)")
        print(f"  For time-sensitive trading: use Binance direct")
        print(f"  Polymarket crypto_prices is fine for display/reference (~{avg_delay:.0f}ms stale)")
    else:
        print(f"  Could not calculate relay delay - no timestamp matches")
    print("=" * 70)


async def run_comparison(duration: int) -> None:
    """Run both streams and compare."""
    global running

    print(f"Running comparison for {duration}s...")
    print("-" * 70)

    # Start both streams
    binance_task = asyncio.create_task(binance_stream())
    pm_task = asyncio.create_task(pm_stream())

    # Progress updates
    start = time.monotonic()
    last_print = start

    while time.monotonic() - start < duration:
        await asyncio.sleep(1.0)
        now = time.monotonic()

        if now - last_print >= 5.0:
            elapsed = now - start
            relay_avg = sum(comparison.relay_delays) / len(comparison.relay_delays) if comparison.relay_delays else 0
            print(f"  {elapsed:.0f}s: Binance={binance.msg_count} PM={pm.msg_count} "
                  f"relay_delay={relay_avg:.0f}ms")
            last_print = now

    running = False

    # Wait for tasks to finish
    await asyncio.sleep(1.0)
    binance_task.cancel()
    pm_task.cancel()

    try:
        await binance_task
    except asyncio.CancelledError:
        pass
    try:
        await pm_task
    except asyncio.CancelledError:
        pass

    print_summary(duration)


def signal_handler(signum, frame):
    """Handle Ctrl+C."""
    global running
    print("\n\nInterrupted!")
    running = False


async def main(duration: int = 60) -> None:
    """Main entry point."""
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    await run_comparison(duration)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Compare Binance vs Polymarket streams")
    parser.add_argument("-d", "--duration", type=int, default=60,
                        help="Duration in seconds (default: 60)")
    args = parser.parse_args()

    asyncio.run(main(args.duration))
