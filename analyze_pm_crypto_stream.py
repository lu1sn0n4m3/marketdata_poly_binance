#!/usr/bin/env python3
"""
Polymarket RTDS Crypto Prices Stream Analyzer.

Compares Polymarket's crypto price relay (sourced from Binance) against
direct Binance feed to measure relay latency overhead.

Usage:
    source .venv/bin/activate && python analyze_pm_crypto_stream.py [-d SECONDS]
"""

import asyncio
import json
import signal
import sys
import time
from dataclasses import dataclass, field
from typing import Optional

import websockets

# Polymarket RTDS WebSocket
PM_WS_URL = "wss://ws-live-data.polymarket.com"
SYMBOL = "btcusdt"
PING_INTERVAL = 5.0  # docs recommend every 5 seconds


@dataclass
class Stats:
    """Collected statistics."""
    # Latency (server timestamp to local receipt)
    latencies: list[int] = field(default_factory=list)
    # Message counts
    update_count: int = 0
    other_count: int = 0
    # Price tracking
    prices: list[float] = field(default_factory=list)
    price_changes: list[float] = field(default_factory=list)
    # Timing
    start_time: float = 0.0
    connection_time_ms: float = 0.0
    inter_msg_times: list[float] = field(default_factory=list)
    # Last values
    last_price: Optional[float] = None
    last_latency: Optional[int] = None
    last_server_ts: Optional[int] = None
    # Errors
    errors: list[str] = field(default_factory=list)


stats = Stats()


def process_message(data: dict, local_ts_ms: int) -> None:
    """Process a single message from RTDS."""
    topic = data.get("topic", "")
    msg_type = data.get("type", "")

    # Outer timestamp (message envelope)
    outer_ts = data.get("timestamp")

    payload = data.get("payload", {})

    # Inner timestamp (price timestamp)
    inner_ts = payload.get("timestamp")
    symbol = payload.get("symbol", "")
    value = payload.get("value")

    # Use the most relevant timestamp for latency
    server_ts = inner_ts or outer_ts

    if topic == "crypto_prices" and msg_type == "update":
        # Filter for our symbol (client-side since server filter format is unclear)
        if symbol and symbol.lower() != SYMBOL:
            return
        stats.update_count += 1

        if server_ts:
            try:
                latency = local_ts_ms - int(server_ts)
                if latency > 0:  # Sanity check
                    stats.latencies.append(latency)
                    stats.last_latency = latency
                    stats.last_server_ts = int(server_ts)
            except (ValueError, TypeError):
                pass

        if value is not None:
            try:
                price = float(value)
                if stats.last_price is not None:
                    change = price - stats.last_price
                    if change != 0:
                        stats.price_changes.append(change)
                stats.prices.append(price)
                stats.last_price = price
            except (ValueError, TypeError):
                pass
    else:
        stats.other_count += 1


def print_latency_stats(name: str, samples: list[int]) -> None:
    """Print latency statistics."""
    if not samples:
        print(f"  {name}: No samples")
        return

    n = len(samples)
    avg = sum(samples) / n
    sorted_s = sorted(samples)
    p50 = sorted_s[n // 2]
    p95 = sorted_s[int(n * 0.95)] if n >= 20 else sorted_s[-1]
    p99 = sorted_s[int(n * 0.99)] if n >= 100 else sorted_s[-1]
    min_lat = min(samples)
    max_lat = max(samples)
    std_dev = (sum((x - avg) ** 2 for x in samples) / n) ** 0.5

    print(f"  {name}:")
    print(f"    Samples: {n:,}")
    print(f"    Min/Avg/P50/P95/P99/Max: {min_lat}/{avg:.0f}/{p50}/{p95}/{p99}/{max_lat} ms")
    print(f"    Std Dev: {std_dev:.1f} ms")


def print_summary() -> None:
    """Print full analysis summary."""
    elapsed = time.monotonic() - stats.start_time
    total_msgs = stats.update_count + stats.other_count

    print("\n" + "=" * 70)
    print("POLYMARKET RTDS CRYPTO PRICES ANALYSIS")
    print("=" * 70)

    # Timing
    print(f"\n  Duration: {elapsed:.1f}s")
    print(f"  Connection time: {stats.connection_time_ms:.1f}ms")
    print(f"  Total messages: {total_msgs:,}")
    print(f"  Rate: {total_msgs / max(1, elapsed):.1f} msg/s")

    # Message breakdown
    print(f"\n  MESSAGE TYPES:")
    print(f"    Price updates: {stats.update_count:,} ({stats.update_count/max(1,total_msgs)*100:.1f}%)")
    print(f"    Other:         {stats.other_count:,}")

    # Latency
    print(f"\n  LATENCY (Server -> Local):")
    print_latency_stats("Crypto Prices", stats.latencies)

    # Price stats
    if stats.prices:
        avg_price = sum(stats.prices) / len(stats.prices)
        min_price = min(stats.prices)
        max_price = max(stats.prices)
        price_range = max_price - min_price

        print(f"\n  PRICE STATISTICS ({SYMBOL.upper()}):")
        print(f"    Updates: {len(stats.prices):,}")
        print(f"    Avg price: ${avg_price:,.2f}")
        print(f"    Min/Max: ${min_price:,.2f} / ${max_price:,.2f}")
        print(f"    Range: ${price_range:.2f}")
        print(f"    Updates/sec: {len(stats.prices) / max(1, elapsed):.1f}")

        if stats.price_changes:
            avg_change = sum(abs(c) for c in stats.price_changes) / len(stats.price_changes)
            print(f"    Avg price change: ${avg_change:.2f}")

    # Inter-message timing
    if stats.inter_msg_times:
        avg_gap = sum(stats.inter_msg_times) / len(stats.inter_msg_times) * 1000
        max_gap = max(stats.inter_msg_times) * 1000
        min_gap = min(stats.inter_msg_times) * 1000
        print(f"\n  MESSAGE TIMING:")
        print(f"    Min/Avg/Max gap: {min_gap:.1f}/{avg_gap:.1f}/{max_gap:.1f} ms")

    # Errors
    if stats.errors:
        print(f"\n  ERRORS: {len(stats.errors)}")
        for e in stats.errors[:5]:
            print(f"    - {e}")

    # Last values
    if stats.last_price:
        print(f"\n  LAST UPDATE:")
        print(f"    Price: ${stats.last_price:,.2f}")
        print(f"    Latency: {stats.last_latency}ms")

    print("\n" + "=" * 70)
    print("NOTE: This stream sources from Binance. Compare with analyze_binance_stream.py")
    print("      to measure Polymarket's relay overhead.")
    print("=" * 70)


async def ping_task(ws) -> None:
    """Send periodic pings to keep connection alive."""
    while True:
        try:
            await asyncio.sleep(PING_INTERVAL)
            ping_msg = json.dumps({"action": "ping"})
            await ws.send(ping_msg)
        except Exception:
            break


async def stream_and_analyze(duration: int = 60) -> None:
    """Connect to Polymarket RTDS and collect stats."""
    print(f"Connecting to Polymarket RTDS...")
    print(f"URL: {PM_WS_URL}")

    conn_start = time.monotonic()
    async with websockets.connect(PM_WS_URL) as ws:
        stats.connection_time_ms = (time.monotonic() - conn_start) * 1000
        print(f"Connected in {stats.connection_time_ms:.0f}ms")

        # Subscribe to all crypto_prices, filter client-side for our symbol
        # (server-side filter format is undocumented/broken)
        sub_msg = {
            "action": "subscribe",
            "subscriptions": [
                {
                    "topic": "crypto_prices",
                    "type": "update"
                }
            ]
        }
        await ws.send(json.dumps(sub_msg))
        print(f"Subscribed to crypto_prices for {SYMBOL.upper()}")
        print(f"Running for {duration}s...")
        print("-" * 70)

        # Start ping task
        ping = asyncio.create_task(ping_task(ws))

        stats.start_time = time.monotonic()
        last_msg_time = stats.start_time
        last_print = stats.start_time

        try:
            while True:
                elapsed = time.monotonic() - stats.start_time
                if elapsed >= duration:
                    print(f"\nDuration reached ({duration}s)")
                    break

                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=1.0)
                    local_ts_ms = int(time.time() * 1000)
                    now = time.monotonic()

                    # Track inter-message timing
                    stats.inter_msg_times.append(now - last_msg_time)
                    last_msg_time = now

                    data = json.loads(raw)
                    process_message(data, local_ts_ms)

                    # Periodic status (every 5s)
                    if now - last_print >= 5.0:
                        avg_lat = sum(stats.latencies[-100:]) / max(1, min(100, len(stats.latencies))) if stats.latencies else 0
                        print(f"  {elapsed:.0f}s: {stats.update_count:,} updates, "
                              f"lat={avg_lat:.0f}ms, "
                              f"price=${stats.last_price:,.2f}" if stats.last_price else f"  {elapsed:.0f}s: waiting...")
                        last_print = now

                except asyncio.TimeoutError:
                    continue
                except websockets.ConnectionClosed as e:
                    stats.errors.append(f"Connection closed: {e}")
                    print(f"Connection closed: {e}")
                    break
                except json.JSONDecodeError as e:
                    stats.errors.append(f"JSON error: {e}")

        finally:
            ping.cancel()
            try:
                await ping
            except asyncio.CancelledError:
                pass


def signal_handler(signum, frame):
    """Handle Ctrl+C gracefully."""
    print("\n\nInterrupted!")
    print_summary()
    sys.exit(0)


async def main(duration: int = 60) -> None:
    """Main entry point."""
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    await stream_and_analyze(duration)
    print_summary()


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Analyze Polymarket RTDS crypto stream")
    parser.add_argument("-d", "--duration", type=int, default=60,
                        help="Duration in seconds (default: 60)")
    args = parser.parse_args()

    asyncio.run(main(args.duration))
