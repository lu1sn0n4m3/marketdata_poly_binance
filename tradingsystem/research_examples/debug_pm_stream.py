#!/usr/bin/env python3
"""
Debug script to stream raw Polymarket WebSocket messages for Bitcoin hourly market.

Usage:
    python -m tradingsystem.debug_pm_stream

Automatically finds the current Bitcoin hourly market and streams raw WS messages.
Measures SERVER-TO-LOCAL latency by comparing Polymarket's timestamp with local time.
"""

import asyncio
import json
import signal
import sys
import time
import websockets

from .clients import build_market_slug, get_current_hour_et, GammaClient


def signal_handler(signum, frame):
    """Handle termination signals gracefully."""
    print("\n\nReceived termination signal...")
    print_latency_summary()
    sys.exit(0)

PM_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"

# Track timing
start_time = None

# Latency statistics
latency_samples: list[int] = []


async def find_bitcoin_market():
    """Find current Bitcoin hourly market."""
    slug = build_market_slug()
    print(f"Looking for Bitcoin market: {slug}")

    gamma = GammaClient()
    try:
        event = await gamma.get_event_by_slug(slug)
        if not event:
            print(f"No event found for slug: {slug}")
            return None, None

        markets = event.get("markets", [])
        if not markets:
            event_id = event.get("id")
            if event_id:
                markets = await gamma.get_markets_for_event(str(event_id))

        if not markets:
            print(f"No markets in event")
            return None, None

        market = markets[0]

        # Get token IDs
        clob_ids = market.get("clobTokenIds")
        if isinstance(clob_ids, str):
            clob_ids = json.loads(clob_ids)

        yes_token = clob_ids[0] if clob_ids and len(clob_ids) > 0 else None
        no_token = clob_ids[1] if clob_ids and len(clob_ids) > 1 else None

        print(f"Found market: {market.get('question', '')[:60]}...")
        print(f"YES token: {yes_token}")
        print(f"NO token: {no_token}")

        return yes_token, no_token

    finally:
        await gamma.close()


async def stream_market(yes_token: str, no_token: str, duration_sec: int = 0, quiet: bool = False):
    """
    Stream raw messages from Polymarket market WebSocket.

    Args:
        yes_token: YES token ID
        no_token: NO token ID
        duration_sec: Run for this many seconds then stop (0 = forever)
        quiet: If True, don't print individual messages (only summary)
    """
    global start_time, latency_samples
    print(f"\nConnecting to {PM_WS_URL}...")
    print("-" * 80)

    async with websockets.connect(PM_WS_URL) as ws:
        # Subscribe to BOTH tokens
        sub_msg = {
            "type": "market",
            "assets_ids": [yes_token, no_token],
        }
        await ws.send(json.dumps(sub_msg))
        print(f"Subscribed to YES and NO tokens")
        if duration_sec > 0:
            print(f"Running for {duration_sec} seconds...")
        print("-" * 80)

        msg_count = 0
        start_time = time.monotonic()
        last_msg_time = start_time

        while True:
            try:
                # Check duration
                if duration_sec > 0:
                    elapsed = time.monotonic() - start_time
                    if elapsed >= duration_sec:
                        print(f"\nDuration reached ({duration_sec}s)")
                        break

                raw = await asyncio.wait_for(ws.recv(), timeout=1.0)
                local_ts_ms = int(time.time() * 1000)  # Wall clock for server comparison
                now = time.monotonic()
                msg_count += 1
                delta_ms = (now - last_msg_time) * 1000
                elapsed_ms = (now - start_time) * 1000
                last_msg_time = now

                try:
                    data = json.loads(raw)

                    # Handle both single objects and arrays
                    items = data if isinstance(data, list) else [data]

                    for item in items:
                        # Extract server timestamp and compute latency
                        server_ts = item.get("timestamp") if isinstance(item, dict) else None
                        server_to_local_ms = None
                        if server_ts:
                            server_ts_int = int(server_ts)
                            server_to_local_ms = local_ts_ms - server_ts_int
                            latency_samples.append(server_to_local_ms)

                        if not quiet:
                            print_event(msg_count, item, yes_token, no_token, elapsed_ms, delta_ms, server_to_local_ms)

                except json.JSONDecodeError:
                    if not quiet:
                        print(f"[{msg_count}] RAW: {raw[:200]}...")

            except asyncio.TimeoutError:
                # Check duration on timeout too
                continue
            except websockets.ConnectionClosed:
                print("Connection closed")
                break
            except KeyboardInterrupt:
                print("\nStopped by user")
                break

    # Print latency summary
    print_latency_summary()


def print_latency_summary():
    """Print latency statistics summary."""
    global latency_samples
    if not latency_samples:
        print("\nNo latency samples collected.")
        return

    samples = latency_samples
    n = len(samples)
    avg = sum(samples) / n
    min_lat = min(samples)
    max_lat = max(samples)
    sorted_samples = sorted(samples)
    p50 = sorted_samples[n // 2]
    p95 = sorted_samples[int(n * 0.95)] if n >= 20 else sorted_samples[-1]
    p99 = sorted_samples[int(n * 0.99)] if n >= 100 else sorted_samples[-1]

    print("\n" + "=" * 60)
    print("LATENCY SUMMARY (Server -> Local)")
    print("=" * 60)
    print(f"  Samples: {n}")
    print(f"  Min:     {min_lat}ms")
    print(f"  Avg:     {avg:.1f}ms")
    print(f"  P50:     {p50}ms")
    print(f"  P95:     {p95}ms")
    print(f"  P99:     {p99}ms")
    print(f"  Max:     {max_lat}ms")
    print("=" * 60)


def print_event(count: int, data: dict, yes_token: str, no_token: str, elapsed_ms: float, delta_ms: float, server_to_local_ms: int | None = None):
    """Print a single event with relevant fields highlighted."""
    event_type = data.get("event_type", "unknown")
    asset_id = data.get("asset_id", "")

    # Label as YES or NO
    if asset_id == yes_token:
        token_label = "YES"
    elif asset_id == no_token:
        token_label = "NO"
    else:
        token_label = asset_id[:16] + "..." if asset_id else "?"

    # Build time string with server latency if available
    latency_str = f" lat={server_to_local_ms}ms" if server_to_local_ms is not None else ""
    time_str = f"t={elapsed_ms:.0f}ms (+{delta_ms:.0f}ms){latency_str}"

    if event_type == "book":
        bids = data.get("bids", [])
        asks = data.get("asks", [])
        # BIDS: sorted ascending, best bid (highest) is LAST
        # ASKS: sorted descending, best ask (lowest) is LAST
        best_bid = bids[-1] if bids else None
        best_ask = asks[-1] if asks else None
        print(f"[{count}] BOOK {token_label} {time_str}")
        print(f"       best_bid={best_bid}")
        print(f"       best_ask={best_ask}")
        print(f"       total_bids={len(bids)} total_asks={len(asks)}")

    elif event_type == "price_change":
        changes = data.get("price_changes", [])
        for c in changes:
            c_asset = c.get("asset_id", "")
            if c_asset == yes_token:
                c_label = "YES"
            elif c_asset == no_token:
                c_label = "NO"
            else:
                c_label = c_asset[:16] + "..."

            print(f"[{count}] PRICE_CHANGE {c_label} {time_str}")
            print(f"       best_bid={c.get('best_bid')!r} best_ask={c.get('best_ask')!r}")

    elif event_type == "best_bid_ask":
        print(f"[{count}] BEST_BID_ASK {token_label} {time_str}")
        print(f"       best_bid={data.get('best_bid')!r} best_ask={data.get('best_ask')!r}")

    elif event_type == "last_trade_price":
        print(f"[{count}] LAST_TRADE {token_label} {time_str} price={data.get('price')}")

    else:
        # Unknown event - print full
        print(f"[{count}] {event_type.upper()} {time_str}: {json.dumps(data, indent=2)[:300]}")

    print()


async def main_async(duration_sec: int = 0, quiet: bool = False):
    yes_token, no_token = await find_bitcoin_market()
    if not yes_token or not no_token:
        print("Failed to find Bitcoin market")
        sys.exit(1)

    await stream_market(yes_token, no_token, duration_sec=duration_sec, quiet=quiet)


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Stream Polymarket WebSocket messages")
    parser.add_argument(
        "-d", "--duration",
        type=int,
        default=0,
        help="Run for N seconds then exit (0 = forever)",
    )
    parser.add_argument(
        "-q", "--quiet",
        action="store_true",
        help="Quiet mode - only show latency summary",
    )
    args = parser.parse_args()

    # Register signal handlers
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    asyncio.run(main_async(duration_sec=args.duration, quiet=args.quiet))


if __name__ == "__main__":
    main()
