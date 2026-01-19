#!/usr/bin/env python3
"""
Local test for Polymarket consumer changes.

Tests:
1. Both markets connect and emit data
2. Watchdog uses emit time (not just receive time)
3. Hour boundary transition (if you run near an hour boundary)

Usage:
    python test_polymarket_local.py              # Quick test (2 minutes)
    python test_polymarket_local.py --duration 300  # Run for 5 minutes
    python test_polymarket_local.py --until-hour    # Run until next hour boundary + 2 min
"""

import asyncio
import argparse
import logging
import sys
from datetime import datetime, timezone, timedelta
from collections import defaultdict
from time import time

# Add collector to path
sys.path.insert(0, "collector/src")

from collector.streams.polymarket import PolymarketConsumer, get_eastern_time, derive_market_url
from collector.time.boundaries import seconds_until_next_hour

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("test")

# Also show debug logs from polymarket module
logging.getLogger("collector.streams.polymarket").setLevel(logging.DEBUG)
logging.getLogger("collector.streams.base").setLevel(logging.DEBUG)


class TestStats:
    """Track statistics for testing."""
    
    def __init__(self):
        self.counts = defaultdict(lambda: defaultdict(int))  # market -> event_type -> count
        self.last_emit = defaultdict(float)  # market -> timestamp
        self.start_time = time()
    
    def record(self, row: dict):
        market = row.get("stream_id", "unknown")
        event_type = row.get("event_type", "unknown")
        self.counts[market][event_type] += 1
        self.last_emit[market] = time()
    
    def print_stats(self):
        elapsed = time() - self.start_time
        print(f"\n{'='*60}")
        print(f"Stats after {elapsed:.1f}s:")
        print(f"{'='*60}")
        
        for market, events in sorted(self.counts.items()):
            last = self.last_emit.get(market, 0)
            ago = time() - last if last else float('inf')
            status = "✓" if ago < 30 else "⚠️" if ago < 120 else "❌"
            
            print(f"\n{status} {market}:")
            print(f"   Last emit: {ago:.1f}s ago")
            for event_type, count in sorted(events.items()):
                print(f"   {event_type}: {count} events")
        
        print(f"\n{'='*60}\n")


async def run_test(duration_seconds: float, until_hour: bool = False):
    """Run the test."""
    stats = TestStats()
    shutdown = asyncio.Event()
    
    # Calculate actual duration
    if until_hour:
        secs = seconds_until_next_hour()
        duration_seconds = secs + 120  # Run 2 min past hour boundary
        logger.info(f"Running until {secs:.0f}s to hour boundary + 2min = {duration_seconds:.0f}s total")
    
    # Show current market info
    et_time = get_eastern_time()
    logger.info(f"Current Eastern Time: {et_time.strftime('%Y-%m-%d %H:%M:%S ET')}")
    logger.info(f"Seconds until next hour: {seconds_until_next_hour():.0f}s")
    
    for slug in ["bitcoin-up-or-down", "ethereum-up-or-down"]:
        url = derive_market_url(slug, et_time)
        logger.info(f"  {slug} -> {url}")
    
    def on_row(row: dict):
        stats.record(row)
        # Print occasional samples
        if sum(sum(e.values()) for e in stats.counts.values()) % 100 == 1:
            logger.debug(f"Sample: {row.get('stream_id')} {row.get('event_type')}")
    
    # Create consumers
    consumers = [
        PolymarketConsumer(market_slug="bitcoin-up-or-down", on_row=on_row),
        PolymarketConsumer(market_slug="ethereum-up-or-down", on_row=on_row),
    ]
    
    # Start consumers
    tasks = [
        asyncio.create_task(c.run(shutdown))
        for c in consumers
    ]
    
    # Print stats periodically
    async def print_periodic_stats():
        while not shutdown.is_set():
            await asyncio.sleep(30)
            if not shutdown.is_set():
                stats.print_stats()
                
                # Check for stale streams
                for market, last in stats.last_emit.items():
                    ago = time() - last
                    if ago > 120:
                        logger.warning(f"⚠️  {market} is stale ({ago:.0f}s since last emit)")
    
    stats_task = asyncio.create_task(print_periodic_stats())
    
    logger.info(f"\nRunning test for {duration_seconds:.0f} seconds...")
    logger.info("Press Ctrl+C to stop early\n")
    
    try:
        await asyncio.sleep(duration_seconds)
    except asyncio.CancelledError:
        pass
    finally:
        logger.info("Shutting down...")
        shutdown.set()
        stats_task.cancel()
        
        for c in consumers:
            c.stop()
        
        await asyncio.gather(*tasks, return_exceptions=True)
        
        # Final stats
        stats.print_stats()
        
        # Summary
        print("\n" + "="*60)
        print("TEST SUMMARY")
        print("="*60)
        
        all_ok = True
        for market in ["bitcoin-up-or-down", "ethereum-up-or-down"]:
            if market not in stats.counts:
                print(f"❌ {market}: NO DATA RECEIVED")
                all_ok = False
            else:
                total = sum(stats.counts[market].values())
                last_ago = time() - stats.last_emit[market]
                if last_ago > 120:
                    print(f"⚠️  {market}: {total} events but stale ({last_ago:.0f}s)")
                    all_ok = False
                else:
                    print(f"✓ {market}: {total} events, last {last_ago:.1f}s ago")
                    
                    # Check for book events (new feature)
                    if "book" in stats.counts[market]:
                        print(f"  ✓ L2 book data: {stats.counts[market]['book']} snapshots")
                    else:
                        print(f"  ⚠️  No L2 book data (might need more time)")
        
        print("="*60)
        
        if all_ok:
            print("\n✅ Test PASSED - both markets emitting data")
            return 0
        else:
            print("\n❌ Test FAILED - check issues above")
            return 1


def main():
    parser = argparse.ArgumentParser(description="Test Polymarket consumer locally")
    parser.add_argument(
        "--duration", "-d",
        type=int,
        default=120,
        help="Test duration in seconds (default: 120)"
    )
    parser.add_argument(
        "--until-hour",
        action="store_true",
        help="Run until 2 minutes past next hour boundary (tests market transition)"
    )
    args = parser.parse_args()
    
    try:
        exit_code = asyncio.run(run_test(args.duration, args.until_hour))
        sys.exit(exit_code)
    except KeyboardInterrupt:
        print("\nInterrupted by user")
        sys.exit(0)


if __name__ == "__main__":
    main()
