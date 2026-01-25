#!/usr/bin/env python3
"""
Test script for PolymarketMarketFeed with internal book tracking.

Verifies:
1. BBO sizes are populated (not zero after book updates)
2. NO is correctly derived from YES (100 - price)
3. Internal book is maintained consistently
4. Cache receives valid data

Usage:
    source .venv/bin/activate && python test_pm_feed.py [-d SECONDS]
"""

import argparse
import asyncio
import json
import signal
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo

import aiohttp

# Add project to path
sys.path.insert(0, ".")

from tradingsystem.caches import PolymarketCache
from tradingsystem.feeds.polymarket_market_feed import PolymarketMarketFeed

ET = ZoneInfo("America/New_York")
GAMMA_API = "https://gamma-api.polymarket.com"
MONTH_NAMES = ["january", "february", "march", "april", "may", "june",
               "july", "august", "september", "october", "november", "december"]


@dataclass
class TestStats:
    """Test statistics."""
    snapshots: int = 0
    snapshots_with_yes_bid_sz: int = 0
    snapshots_with_yes_ask_sz: int = 0
    no_derivation_correct: int = 0
    no_derivation_errors: list = field(default_factory=list)
    bbo_history: list = field(default_factory=list)  # For display
    book_depths: list = field(default_factory=list)


stats = TestStats()


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


async def find_bitcoin_market():
    """Find current Bitcoin hourly market tokens."""
    slug = build_market_slug()
    print(f"Finding market: {slug}")

    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
        url = f"{GAMMA_API}/events/slug/{slug}"
        async with session.get(url) as resp:
            if resp.status != 200:
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
            return None, None, None

        market = markets[0]
        clob_ids = market.get("clobTokenIds")
        if isinstance(clob_ids, str):
            clob_ids = json.loads(clob_ids)

        return clob_ids[0], clob_ids[1], market.get("conditionId", "")


def check_snapshot(cache: PolymarketCache, feed: PolymarketMarketFeed) -> dict:
    """Check current snapshot and validate correctness."""
    snapshot, seq = cache.get_latest()
    if not snapshot:
        return None

    stats.snapshots += 1

    yes = snapshot.yes_top
    no = snapshot.no_top

    # Check sizes are populated
    if yes.best_bid_sz > 0:
        stats.snapshots_with_yes_bid_sz += 1
    if yes.best_ask_sz > 0:
        stats.snapshots_with_yes_ask_sz += 1

    # Validate NO derivation: NO bid = 100 - YES ask
    expected_no_bid = 100 - yes.best_ask_px
    expected_no_ask = 100 - yes.best_bid_px

    if no.best_bid_px == expected_no_bid and no.best_ask_px == expected_no_ask:
        stats.no_derivation_correct += 1
    else:
        stats.no_derivation_errors.append({
            "yes": (yes.best_bid_px, yes.best_ask_px),
            "no_actual": (no.best_bid_px, no.best_ask_px),
            "no_expected": (expected_no_bid, expected_no_ask),
        })

    # Track book depth
    book_depth = (len(feed._yes_bids), len(feed._yes_asks))
    stats.book_depths.append(book_depth)

    return {
        "seq": seq,
        "yes_bid": f"{yes.best_bid_px}c x {yes.best_bid_sz:,}",
        "yes_ask": f"{yes.best_ask_px}c x {yes.best_ask_sz:,}",
        "no_bid": f"{no.best_bid_px}c x {no.best_bid_sz:,}",
        "no_ask": f"{no.best_ask_px}c x {no.best_ask_sz:,}",
        "spread": yes.best_ask_px - yes.best_bid_px,
        "book_depth": book_depth,
        "age_ms": cache.get_age_ms(),
    }


def print_snapshot(data: dict, elapsed: float):
    """Print a single snapshot in readable format."""
    print(f"\n{'='*60}")
    print(f"Snapshot @ {elapsed:.1f}s  (seq={data['seq']}, age={data['age_ms']}ms)")
    print(f"{'='*60}")
    print(f"  YES:  {data['yes_bid']:>15}  |  {data['yes_ask']:<15}  spread={data['spread']}c")
    print(f"  NO:   {data['no_bid']:>15}  |  {data['no_ask']:<15}  (derived)")
    print(f"  Book: {data['book_depth'][0]} bids, {data['book_depth'][1]} asks")


def print_summary():
    """Print test summary."""
    print(f"\n{'='*60}")
    print("TEST SUMMARY")
    print(f"{'='*60}")

    n = stats.snapshots
    if n == 0:
        print("  No snapshots collected!")
        return

    # Size population rate
    bid_sz_pct = stats.snapshots_with_yes_bid_sz / n * 100
    ask_sz_pct = stats.snapshots_with_yes_ask_sz / n * 100

    print(f"\n  Snapshots checked: {n}")

    # Sizes
    print(f"\n  BBO SIZES:")
    status = "PASS" if bid_sz_pct > 90 else "WARN" if bid_sz_pct > 50 else "FAIL"
    print(f"    [{status}] YES bid size > 0:  {stats.snapshots_with_yes_bid_sz}/{n} ({bid_sz_pct:.1f}%)")
    status = "PASS" if ask_sz_pct > 90 else "WARN" if ask_sz_pct > 50 else "FAIL"
    print(f"    [{status}] YES ask size > 0:  {stats.snapshots_with_yes_ask_sz}/{n} ({ask_sz_pct:.1f}%)")

    # NO derivation
    deriv_pct = stats.no_derivation_correct / n * 100
    status = "PASS" if deriv_pct == 100 else "FAIL"
    print(f"\n  NO DERIVATION (100 - YES):")
    print(f"    [{status}] Correct: {stats.no_derivation_correct}/{n} ({deriv_pct:.1f}%)")
    if stats.no_derivation_errors:
        print(f"    Errors: {stats.no_derivation_errors[:3]}")

    # Book depth
    if stats.book_depths:
        avg_bids = sum(d[0] for d in stats.book_depths) / len(stats.book_depths)
        avg_asks = sum(d[1] for d in stats.book_depths) / len(stats.book_depths)
        print(f"\n  BOOK DEPTH:")
        print(f"    Avg: {avg_bids:.1f} bids, {avg_asks:.1f} asks")

    # Overall
    all_pass = bid_sz_pct > 80 and ask_sz_pct > 80 and deriv_pct == 100
    print(f"\n  {'='*40}")
    print(f"  OVERALL: {'PASS' if all_pass else 'FAIL'}")
    print(f"  {'='*40}")


async def run_test(duration: int = 30, snapshot_interval: int = 5):
    """Run the feed test."""
    # Find market
    yes_token, no_token, market_id = await find_bitcoin_market()
    if not yes_token:
        print("Failed to find Bitcoin market")
        return

    print(f"YES token: {yes_token[:20]}...")
    print(f"NO token:  {no_token[:20]}...")

    # Create cache and feed
    cache = PolymarketCache()
    feed = PolymarketMarketFeed(cache)
    feed.set_tokens(yes_token, no_token, market_id)

    # Start feed
    print(f"\nStarting feed for {duration}s...")
    feed.start()

    # Wait for connection
    await asyncio.sleep(2)

    start = time.monotonic()
    last_snapshot = start
    next_snapshot = snapshot_interval

    try:
        while True:
            elapsed = time.monotonic() - start
            if elapsed >= duration:
                break

            # Periodic snapshot display
            if elapsed >= next_snapshot:
                data = check_snapshot(cache, feed)
                if data:
                    print_snapshot(data, elapsed)
                next_snapshot += snapshot_interval

            # Also collect stats more frequently (every 100ms)
            if time.monotonic() - last_snapshot > 0.1:
                check_snapshot(cache, feed)
                last_snapshot = time.monotonic()

            await asyncio.sleep(0.05)

    except KeyboardInterrupt:
        print("\nInterrupted")
    finally:
        feed.stop(timeout=2)

    print_summary()


def main():
    parser = argparse.ArgumentParser(description="Test PolymarketMarketFeed")
    parser.add_argument("-d", "--duration", type=int, default=30,
                        help="Duration in seconds (default: 30)")
    parser.add_argument("-i", "--interval", type=int, default=5,
                        help="Snapshot display interval (default: 5s)")
    args = parser.parse_args()

    def signal_handler(signum, frame):
        print("\nStopping...")
        print_summary()
        sys.exit(0)

    signal.signal(signal.SIGINT, signal_handler)
    asyncio.run(run_test(args.duration, args.interval))


if __name__ == "__main__":
    main()
