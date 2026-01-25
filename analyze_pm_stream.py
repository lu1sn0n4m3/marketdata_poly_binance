#!/usr/bin/env python3
"""
Polymarket Bitcoin Market Stream Analyzer.

Self-contained script to analyze:
1. Server-to-local latency (connection stability)
2. Message types and their structures
3. Book update patterns

Usage:
    source .venv/bin/activate && python analyze_pm_stream.py [--duration SECONDS]
"""

import asyncio
import json
import signal
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

import aiohttp
import websockets

# Constants
GAMMA_API = "https://gamma-api.polymarket.com"
PM_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
ET = ZoneInfo("America/New_York")
MONTH_NAMES = ["january", "february", "march", "april", "may", "june",
               "july", "august", "september", "october", "november", "december"]


@dataclass
class Stats:
    """Collected statistics."""
    latency_samples: list[int] = field(default_factory=list)
    msg_counts: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    msg_examples: dict[str, dict] = field(default_factory=dict)
    inter_msg_times: list[float] = field(default_factory=list)
    book_depths: list[tuple[int, int]] = field(default_factory=list)  # (bid_levels, ask_levels)
    price_change_fields: set = field(default_factory=set)
    start_time: float = 0.0
    total_msgs: int = 0
    connection_time_ms: float = 0.0
    # Track price_change specifics
    price_change_sizes: list[float] = field(default_factory=list)  # order sizes in price_change
    price_change_zero_sizes: int = 0  # count of size=0 (cancellations)
    price_change_has_bbo_size: int = 0  # count that have BBO size info


stats = Stats()


def build_market_slug() -> str:
    """Build current hour's market slug."""
    now = datetime.now(ET).replace(minute=0, second=0, microsecond=0)
    month = MONTH_NAMES[now.month - 1]
    day = now.day
    hour_24 = now.hour

    if hour_24 == 0:
        hour_12, period = 12, "am"
    elif hour_24 < 12:
        hour_12, period = hour_24, "am"
    elif hour_24 == 12:
        hour_12, period = 12, "pm"
    else:
        hour_12, period = hour_24 - 12, "pm"

    return f"bitcoin-up-or-down-{month}-{day}-{hour_12}{period}-et"


async def find_bitcoin_market() -> tuple[Optional[str], Optional[str], Optional[str]]:
    """Find current Bitcoin hourly market tokens."""
    slug = build_market_slug()
    print(f"Looking for market: {slug}")

    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
        url = f"{GAMMA_API}/events/slug/{slug}"
        async with session.get(url) as resp:
            if resp.status != 200:
                print(f"Failed to fetch event: {resp.status}")
                return None, None, None
            event = await resp.json()

        markets = event.get("markets", [])
        if not markets:
            event_id = event.get("id")
            if event_id:
                url = f"{GAMMA_API}/markets?event_id={event_id}"
                async with session.get(url) as resp:
                    if resp.status == 200:
                        markets = await resp.json()

        if not markets:
            print("No markets found")
            return None, None, None

        market = markets[0]
        clob_ids = market.get("clobTokenIds")
        if isinstance(clob_ids, str):
            clob_ids = json.loads(clob_ids)

        yes_token = clob_ids[0] if clob_ids else None
        no_token = clob_ids[1] if clob_ids and len(clob_ids) > 1 else None
        question = market.get("question", "")[:70]

        print(f"Market: {question}...")
        print(f"YES: {yes_token}")
        print(f"NO:  {no_token}")

        return yes_token, no_token, question


def process_event(data: dict, yes_token: str, no_token: str, local_ts_ms: int) -> None:
    """Process a single WS event and update stats."""
    event_type = data.get("event_type", "unknown")
    stats.msg_counts[event_type] += 1
    stats.total_msgs += 1

    # Store first example of each type
    if event_type not in stats.msg_examples:
        stats.msg_examples[event_type] = data

    # Latency from server timestamp
    server_ts = data.get("timestamp")
    if server_ts:
        try:
            latency = local_ts_ms - int(server_ts)
            stats.latency_samples.append(latency)
        except (ValueError, TypeError):
            pass

    # Event-specific analysis
    if event_type == "book":
        bids = data.get("bids", [])
        asks = data.get("asks", [])
        stats.book_depths.append((len(bids), len(asks)))

    elif event_type == "price_change":
        changes = data.get("price_changes", [])
        for c in changes:
            stats.price_change_fields.update(c.keys())
            # Track order size (the specific order that changed)
            try:
                sz = float(c.get("size", 0))
                stats.price_change_sizes.append(sz)
                if sz == 0:
                    stats.price_change_zero_sizes += 1
            except (ValueError, TypeError):
                pass
            # Check if there's any BBO size info (there isn't - just prices)
            if "best_bid_size" in c or "best_ask_size" in c:
                stats.price_change_has_bbo_size += 1


def print_latency_stats() -> None:
    """Print detailed latency statistics."""
    samples = stats.latency_samples
    if not samples:
        print("\nNo latency samples (no timestamps in messages)")
        return

    n = len(samples)
    avg = sum(samples) / n
    min_lat = min(samples)
    max_lat = max(samples)
    sorted_s = sorted(samples)
    p50 = sorted_s[n // 2]
    p95 = sorted_s[int(n * 0.95)] if n >= 20 else sorted_s[-1]
    p99 = sorted_s[int(n * 0.99)] if n >= 100 else sorted_s[-1]
    std_dev = (sum((x - avg) ** 2 for x in samples) / n) ** 0.5

    # Jitter analysis (variation between consecutive samples)
    jitters = [abs(samples[i] - samples[i-1]) for i in range(1, len(samples))]
    avg_jitter = sum(jitters) / len(jitters) if jitters else 0

    # Outliers (>2x P95)
    outlier_threshold = p95 * 2
    outliers = [x for x in samples if x > outlier_threshold]

    print("\n" + "=" * 70)
    print("LATENCY ANALYSIS (Server -> Local)")
    print("=" * 70)
    print(f"  Samples:     {n:,}")
    print(f"  Min:         {min_lat:,} ms")
    print(f"  Avg:         {avg:.1f} ms")
    print(f"  Std Dev:     {std_dev:.1f} ms")
    print(f"  P50:         {p50:,} ms")
    print(f"  P95:         {p95:,} ms")
    print(f"  P99:         {p99:,} ms")
    print(f"  Max:         {max_lat:,} ms")
    print(f"  Avg Jitter:  {avg_jitter:.1f} ms")
    print(f"  Outliers:    {len(outliers)} (>{outlier_threshold}ms)")

    # Distribution buckets
    buckets = [(0, 50), (50, 100), (100, 200), (200, 500), (500, 1000), (1000, float('inf'))]
    print("\n  Latency Distribution:")
    for low, high in buckets:
        count = sum(1 for x in samples if low <= x < high)
        pct = count / n * 100
        bar = "#" * int(pct / 2)
        label = f"{low}-{high}ms" if high != float('inf') else f">{low}ms"
        print(f"    {label:>10}: {count:5,} ({pct:5.1f}%) {bar}")


def print_message_stats() -> None:
    """Print message type analysis."""
    print("\n" + "=" * 70)
    print("MESSAGE TYPE ANALYSIS")
    print("=" * 70)

    total = stats.total_msgs
    for event_type, count in sorted(stats.msg_counts.items(), key=lambda x: -x[1]):
        pct = count / total * 100 if total > 0 else 0
        print(f"\n  {event_type.upper()} ({count:,} msgs, {pct:.1f}%)")

        example = stats.msg_examples.get(event_type, {})
        keys = [k for k in example.keys() if k != "event_type"]
        print(f"    Fields: {', '.join(keys)}")

        # Show specific details per type
        if event_type == "book":
            if example:
                bids = example.get("bids", [])
                asks = example.get("asks", [])
                print(f"    Example: {len(bids)} bids, {len(asks)} asks")
                if bids:
                    print(f"    Bid format: {bids[0]}")
                if asks:
                    print(f"    Ask format: {asks[0]}")

        elif event_type == "price_change":
            print(f"    Change fields: {stats.price_change_fields}")
            changes = example.get("price_changes", [])
            if changes:
                print(f"    Example: {json.dumps(changes[0], indent=6)}")
            # Clarify what data is available
            n_changes = len(stats.price_change_sizes)
            if n_changes > 0:
                nonzero = [s for s in stats.price_change_sizes if s > 0]
                print(f"\n    ORDER SIZE ANALYSIS (the 'size' field = specific order, NOT BBO size):")
                print(f"      Total changes:    {n_changes}")
                print(f"      size > 0 (add):   {len(nonzero)} ({len(nonzero)/n_changes*100:.1f}%)")
                print(f"      size = 0 (cancel): {stats.price_change_zero_sizes} ({stats.price_change_zero_sizes/n_changes*100:.1f}%)")
                if nonzero:
                    print(f"      Avg order size:   {sum(nonzero)/len(nonzero):.1f}")
                print(f"\n    BBO SIZE IN price_change: NO (only best_bid/best_ask PRICES)")
                print(f"    To get BBO sizes: use 'book' snapshots + apply deltas")

        elif event_type == "last_trade_price":
            if example:
                print(f"    Fields: price={example.get('price')}, size={example.get('size')}, "
                      f"side={example.get('side')}, fee_rate_bps={example.get('fee_rate_bps')}")

        elif event_type == "best_bid_ask":
            if example:
                print(f"    best_bid={example.get('best_bid')}, best_ask={example.get('best_ask')}, "
                      f"spread={example.get('spread')}")


def print_timing_stats() -> None:
    """Print timing and throughput stats."""
    print("\n" + "=" * 70)
    print("TIMING & THROUGHPUT")
    print("=" * 70)

    elapsed = time.monotonic() - stats.start_time
    print(f"  Duration:        {elapsed:.1f}s")
    print(f"  Connection time: {stats.connection_time_ms:.1f}ms")
    print(f"  Total messages:  {stats.total_msgs:,}")
    print(f"  Msgs/sec:        {stats.total_msgs / elapsed:.2f}")

    if stats.inter_msg_times:
        avg_gap = sum(stats.inter_msg_times) / len(stats.inter_msg_times) * 1000
        max_gap = max(stats.inter_msg_times) * 1000
        print(f"  Avg msg gap:     {avg_gap:.1f}ms")
        print(f"  Max msg gap:     {max_gap:.1f}ms")

    if stats.book_depths:
        avg_bids = sum(d[0] for d in stats.book_depths) / len(stats.book_depths)
        avg_asks = sum(d[1] for d in stats.book_depths) / len(stats.book_depths)
        print(f"  Avg book depth:  {avg_bids:.1f} bids, {avg_asks:.1f} asks")


def print_summary() -> None:
    """Print full analysis summary."""
    print_latency_stats()
    print_message_stats()
    print_timing_stats()
    print("\n" + "=" * 70)


async def stream_and_analyze(yes_token: str, no_token: str, duration_sec: int = 60) -> None:
    """Stream messages and collect stats."""
    print(f"\nConnecting to {PM_WS_URL}...")

    conn_start = time.monotonic()
    async with websockets.connect(PM_WS_URL) as ws:
        stats.connection_time_ms = (time.monotonic() - conn_start) * 1000

        # Subscribe
        sub_msg = {"type": "market", "assets_ids": [yes_token, no_token]}
        await ws.send(json.dumps(sub_msg))
        print(f"Subscribed. Running for {duration_sec}s...")
        print("-" * 70)

        stats.start_time = time.monotonic()
        last_msg_time = stats.start_time

        while True:
            try:
                elapsed = time.monotonic() - stats.start_time
                if elapsed >= duration_sec:
                    print(f"\nDuration reached ({duration_sec}s)")
                    break

                raw = await asyncio.wait_for(ws.recv(), timeout=1.0)
                local_ts_ms = int(time.time() * 1000)
                now = time.monotonic()

                # Track inter-message timing
                stats.inter_msg_times.append(now - last_msg_time)
                last_msg_time = now

                data = json.loads(raw)
                items = data if isinstance(data, list) else [data]

                for item in items:
                    if isinstance(item, dict):
                        process_event(item, yes_token, no_token, local_ts_ms)

                # Progress indicator
                if stats.total_msgs % 100 == 0:
                    print(f"  {stats.total_msgs} msgs, {elapsed:.0f}s elapsed, "
                          f"avg lat: {sum(stats.latency_samples[-100:]) / min(100, len(stats.latency_samples)):.0f}ms"
                          if stats.latency_samples else f"  {stats.total_msgs} msgs")

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

    yes_token, no_token, _ = await find_bitcoin_market()
    if not yes_token or not no_token:
        print("Failed to find Bitcoin market")
        sys.exit(1)

    await stream_and_analyze(yes_token, no_token, duration)
    print_summary()


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Analyze Polymarket WS stream")
    parser.add_argument("-d", "--duration", type=int, default=60,
                        help="Duration in seconds (default: 60)")
    args = parser.parse_args()

    asyncio.run(main(args.duration))
