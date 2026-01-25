#!/usr/bin/env python3
"""
Binance BTC Stream Analyzer.

Analyzes:
1. Server-to-local latency for BBO and Trade streams
2. Message rates and types
3. BBO spread and trade statistics

Usage:
    source .venv/bin/activate && python analyze_binance_stream.py [-d SECONDS]
"""

import asyncio
import json
import signal
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Optional

import websockets

# Binance combined stream URL
SYMBOL = "btcusdt"
WS_URL = f"wss://stream.binance.com:9443/stream?streams={SYMBOL}@bookTicker/{SYMBOL}@trade"


@dataclass
class Stats:
    """Collected statistics."""
    # Latency
    bbo_latencies: list[int] = field(default_factory=list)
    trade_latencies: list[int] = field(default_factory=list)
    # Counts
    bbo_count: int = 0
    trade_count: int = 0
    # BBO data
    spreads: list[float] = field(default_factory=list)
    bid_sizes: list[float] = field(default_factory=list)
    ask_sizes: list[float] = field(default_factory=list)
    # Trade data
    trade_sizes: list[float] = field(default_factory=list)
    buy_count: int = 0
    sell_count: int = 0
    buy_volume: float = 0.0
    sell_volume: float = 0.0
    # Timing
    start_time: float = 0.0
    inter_msg_times: list[float] = field(default_factory=list)
    # Last values for display
    last_bbo: Optional[dict] = None
    last_trade: Optional[dict] = None


stats = Stats()


def process_message(stream: str, payload: dict, local_ts_ms: int) -> None:
    """Process a single Binance message."""
    # Note: bookTicker has NO server timestamp, only trade has "E"
    event_ts = payload.get("E", 0) or payload.get("T", 0)
    latency = local_ts_ms - event_ts if event_ts else None

    if stream.endswith("bookTicker"):
        stats.bbo_count += 1
        # Note: bookTicker has NO server timestamp - latency not measurable
        if latency is not None and latency > 0:
            stats.bbo_latencies.append(latency)

        bid_px = float(payload["b"])
        ask_px = float(payload["a"])
        bid_sz = float(payload["B"])
        ask_sz = float(payload["A"])

        spread = ask_px - bid_px
        stats.spreads.append(spread)
        stats.bid_sizes.append(bid_sz)
        stats.ask_sizes.append(ask_sz)

        stats.last_bbo = {
            "bid": f"${bid_px:,.2f} x {bid_sz:.4f}",
            "ask": f"${ask_px:,.2f} x {ask_sz:.4f}",
            "spread": f"${spread:.2f}",
            "latency": latency,
        }

    elif stream.endswith("trade"):
        stats.trade_count += 1
        if latency is not None and latency > 0:
            stats.trade_latencies.append(latency)

        price = float(payload["p"])
        size = float(payload["q"])
        is_sell = payload.get("m", False)  # m=True means buyer is maker = sell aggressor

        stats.trade_sizes.append(size)
        if is_sell:
            stats.sell_count += 1
            stats.sell_volume += size
        else:
            stats.buy_count += 1
            stats.buy_volume += size

        stats.last_trade = {
            "price": f"${price:,.2f}",
            "size": f"{size:.4f} BTC",
            "side": "SELL" if is_sell else "BUY",
            "latency": latency,
        }


def print_latency_stats(name: str, samples: list[int]) -> None:
    """Print latency statistics for a stream."""
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

    print(f"  {name}:")
    print(f"    Samples: {n:,}")
    print(f"    Min/Avg/P50/P95/P99/Max: {min_lat}/{avg:.0f}/{p50}/{p95}/{p99}/{max_lat} ms")


def print_summary() -> None:
    """Print full analysis summary."""
    elapsed = time.monotonic() - stats.start_time
    total_msgs = stats.bbo_count + stats.trade_count

    print("\n" + "=" * 70)
    print("BINANCE BTCUSDT STREAM ANALYSIS")
    print("=" * 70)

    # Timing
    print(f"\n  Duration: {elapsed:.1f}s")
    print(f"  Total messages: {total_msgs:,}")
    print(f"  Rate: {total_msgs / elapsed:.1f} msg/s")

    # Message breakdown
    print(f"\n  MESSAGE TYPES:")
    print(f"    BBO (bookTicker): {stats.bbo_count:,} ({stats.bbo_count/max(1,total_msgs)*100:.1f}%)")
    print(f"    Trade:            {stats.trade_count:,} ({stats.trade_count/max(1,total_msgs)*100:.1f}%)")

    # Latency
    print(f"\n  LATENCY (Server -> Local):")
    if not stats.bbo_latencies:
        print(f"    BBO: N/A (bookTicker has no server timestamp)")
    else:
        print_latency_stats("BBO", stats.bbo_latencies)
    print_latency_stats("Trade", stats.trade_latencies)

    # BBO stats
    if stats.spreads:
        avg_spread = sum(stats.spreads) / len(stats.spreads)
        min_spread = min(stats.spreads)
        max_spread = max(stats.spreads)
        avg_bid_sz = sum(stats.bid_sizes) / len(stats.bid_sizes)
        avg_ask_sz = sum(stats.ask_sizes) / len(stats.ask_sizes)

        print(f"\n  BBO STATISTICS:")
        print(f"    Avg spread: ${avg_spread:.2f}")
        print(f"    Min/Max spread: ${min_spread:.2f} / ${max_spread:.2f}")
        print(f"    Avg bid size: {avg_bid_sz:.4f} BTC")
        print(f"    Avg ask size: {avg_ask_sz:.4f} BTC")

    # Trade stats
    if stats.trade_count > 0:
        avg_trade_sz = sum(stats.trade_sizes) / len(stats.trade_sizes)
        total_volume = stats.buy_volume + stats.sell_volume

        print(f"\n  TRADE STATISTICS:")
        print(f"    Total trades: {stats.trade_count:,}")
        print(f"    Buy/Sell: {stats.buy_count:,} / {stats.sell_count:,}")
        print(f"    Buy volume: {stats.buy_volume:.4f} BTC ({stats.buy_volume/max(0.0001,total_volume)*100:.1f}%)")
        print(f"    Sell volume: {stats.sell_volume:.4f} BTC ({stats.sell_volume/max(0.0001,total_volume)*100:.1f}%)")
        print(f"    Avg trade size: {avg_trade_sz:.6f} BTC")
        print(f"    Trades/sec: {stats.trade_count / elapsed:.1f}")

    # Last values
    if stats.last_bbo:
        print(f"\n  LAST BBO:")
        print(f"    Bid: {stats.last_bbo['bid']}")
        print(f"    Ask: {stats.last_bbo['ask']}")
        print(f"    Spread: {stats.last_bbo['spread']}")

    print("\n" + "=" * 70)


async def stream_and_analyze(duration: int = 60) -> None:
    """Connect to Binance WS and collect stats."""
    print(f"Connecting to Binance {SYMBOL.upper()} streams...")
    print(f"URL: {WS_URL[:60]}...")

    async with websockets.connect(WS_URL) as ws:
        print(f"Connected. Running for {duration}s...")
        print("-" * 70)

        stats.start_time = time.monotonic()
        last_msg_time = stats.start_time
        last_print = stats.start_time

        while True:
            try:
                elapsed = time.monotonic() - stats.start_time
                if elapsed >= duration:
                    print(f"\nDuration reached ({duration}s)")
                    break

                raw = await asyncio.wait_for(ws.recv(), timeout=1.0)
                local_ts_ms = int(time.time() * 1000)
                now = time.monotonic()

                # Track inter-message timing
                stats.inter_msg_times.append(now - last_msg_time)
                last_msg_time = now

                data = json.loads(raw)
                stream = data.get("stream", "")
                payload = data.get("data", {})

                process_message(stream, payload, local_ts_ms)

                # Periodic status (every 5s)
                if now - last_print >= 5.0:
                    bbo_rate = stats.bbo_count / elapsed
                    trade_rate = stats.trade_count / elapsed
                    avg_bbo_lat = sum(stats.bbo_latencies[-100:]) / max(1, min(100, len(stats.bbo_latencies)))
                    print(f"  {elapsed:.0f}s: BBO={stats.bbo_count:,} ({bbo_rate:.0f}/s, lat={avg_bbo_lat:.0f}ms) "
                          f"Trades={stats.trade_count:,} ({trade_rate:.1f}/s)")
                    last_print = now

            except asyncio.TimeoutError:
                continue
            except websockets.ConnectionClosed:
                print("Connection closed")
                break


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
    parser = argparse.ArgumentParser(description="Analyze Binance WS stream")
    parser.add_argument("-d", "--duration", type=int, default=60,
                        help="Duration in seconds (default: 60)")
    args = parser.parse_args()

    asyncio.run(main(args.duration))
