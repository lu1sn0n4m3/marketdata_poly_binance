#!/usr/bin/env python3
"""
Binance Trade Feed Latency Test

Measures WebSocket trade feed latency.
Compares Binance's exchange timestamp (when trade happened) vs our receive time.

Usage:
    uv run --with websockets test_binance_latency.py
    uv run --with websockets test_binance_latency.py --duration 60
    uv run --with websockets test_binance_latency.py --symbols BTCUSDT,ETHUSDT,SOLUSDT
"""

import asyncio
import json
import time
import argparse
import statistics
from typing import Dict, List
from dataclasses import dataclass, field


@dataclass
class LatencyStats:
    """Track latency statistics for a symbol/event type."""
    symbol: str
    event_type: str
    latencies: List[float] = field(default_factory=list)
    count: int = 0
    
    def add(self, latency_ms: float):
        """Add a latency measurement."""
        self.latencies.append(latency_ms)
        self.count += 1
    
    def get_stats(self) -> Dict:
        """Compute statistics."""
        if not self.latencies:
            return {}
        
        return {
            'count': self.count,
            'min_ms': min(self.latencies),
            'max_ms': max(self.latencies),
            'mean_ms': statistics.mean(self.latencies),
            'median_ms': statistics.median(self.latencies),
            'p95_ms': sorted(self.latencies)[int(len(self.latencies) * 0.95)] if len(self.latencies) > 20 else max(self.latencies),
            'p99_ms': sorted(self.latencies)[int(len(self.latencies) * 0.99)] if len(self.latencies) > 100 else max(self.latencies),
            'stdev_ms': statistics.stdev(self.latencies) if len(self.latencies) > 1 else 0,
        }


class BinanceLatencyTest:
    """
    Test Binance trade feed latency.
    
    Measures the time between:
    - Binance exchange timestamp (when trade executed)
    - Our receive timestamp (when we got the message)
    
    This reveals network + Binance processing + broadcast latency.
    Trade streams have reliable exchange timestamps, unlike BBO.
    """
    
    def __init__(self, symbols: List[str] = None):
        self.symbols = symbols or ["BTCUSDT", "ETHUSDT"]
        self.symbols_lower = [s.lower() for s in self.symbols]
        
        # Build WebSocket URL - only trade streams (they have exchange timestamps)
        streams = "/".join(
            f"{s}@trade" for s in self.symbols_lower
        )
        self.ws_url = f"wss://stream.binance.com:9443/stream?streams={streams}"
        
        # Stats tracking
        self.stats: Dict[str, LatencyStats] = {}
        self.total_messages = 0
        self.start_time = 0
        
    async def run_test(self, duration_seconds: float = 30):
        """Run the latency test."""
        print(f"\n{'='*60}")
        print("BINANCE LATENCY TEST")
        print(f"{'='*60}")
        print(f"  Symbols: {', '.join(self.symbols)}")
        print(f"  Duration: {duration_seconds}s")
        print(f"  WebSocket: {self.ws_url[:80]}...")
        
        try:
            import websockets
        except ImportError:
            print("\n❌ websockets not installed")
            print("   Run: uv run --with websockets test_binance_latency.py")
            return
        
        print(f"\n[CONNECT] Connecting to Binance WebSocket...")
        
        self.start_time = time.time()
        last_print = self.start_time
        
        try:
            async with websockets.connect(self.ws_url) as ws:
                print("  ✅ Connected")
                print(f"\n[TEST] Receiving trade data for {duration_seconds}s...")
                print("  Measuring: Binance exchange timestamp -> your receive time")
                
                while True:
                    # Check duration
                    elapsed = time.time() - self.start_time
                    if elapsed >= duration_seconds:
                        break
                    
                    # Receive message
                    try:
                        msg = await asyncio.wait_for(ws.recv(), timeout=1.0)
                        recv_ts_ms = int(time.time() * 1000)
                        
                        self._process_message(msg, recv_ts_ms)
                        
                        # Print progress every 5 seconds
                        now = time.time()
                        if now - last_print >= 5.0:
                            self._print_progress(elapsed)
                            last_print = now
                            
                    except asyncio.TimeoutError:
                        continue
                    
        except Exception as e:
            print(f"\n❌ WebSocket error: {e}")
            return
        
        # Final results
        self._print_results()
    
    def _process_message(self, msg: str, recv_ts_ms: int):
        """Process a WebSocket message and record latency."""
        try:
            data = json.loads(msg)
            
            stream_name = data.get("stream", "")
            payload = data.get("data", {})
            
            # Only process trade events
            if not stream_name.endswith("trade"):
                return
            
            # Get symbol
            symbol = payload.get("s", "UNKNOWN")
            
            # Get Binance exchange timestamp (field "E")
            exch_ts_ms = payload.get("E")
            if not exch_ts_ms:
                return  # Skip if no exchange timestamp
            
            # Calculate latency
            latency_ms = recv_ts_ms - exch_ts_ms
            
            # Track stats
            if symbol not in self.stats:
                self.stats[symbol] = LatencyStats(symbol=symbol, event_type="trade")
            
            self.stats[symbol].add(latency_ms)
            self.total_messages += 1
            
        except Exception as e:
            pass
    
    def _print_progress(self, elapsed: float):
        """Print progress update."""
        rate = self.total_messages / elapsed if elapsed > 0 else 0
        
        print(f"\n  {elapsed:5.1f}s: {self.total_messages} trades ({rate:.1f}/sec)")
        
        # Show latest latency for each symbol
        for symbol, stats in sorted(self.stats.items()):
            if stats.latencies:
                latest = stats.latencies[-1]
                median = statistics.median(stats.latencies)
                print(f"    {symbol:10} | {stats.count:5} trades | latest: {latest:5.1f}ms | median: {median:5.1f}ms")
    
    def _print_results(self):
        """Print final statistics."""
        total_time = time.time() - self.start_time
        
        print(f"\n{'='*60}")
        print("RESULTS")
        print(f"{'='*60}")
        
        print(f"\nOVERALL:")
        print(f"  Duration:        {total_time:.1f}s")
        print(f"  Total trades:    {self.total_messages}")
        print(f"  Trade rate:      {self.total_messages / total_time:.1f}/sec")
        
        # Stats per symbol
        for symbol, stats_obj in sorted(self.stats.items()):
            stats = stats_obj.get_stats()
            if not stats:
                continue
            
            print(f"\n{stats_obj.symbol} TRADES (n={stats['count']}):")
            print(f"  Min:    {stats['min_ms']:8.1f} ms")
            print(f"  Max:    {stats['max_ms']:8.1f} ms")
            print(f"  Mean:   {stats['mean_ms']:8.1f} ms")
            print(f"  Median: {stats['median_ms']:8.1f} ms")
            print(f"  P95:    {stats['p95_ms']:8.1f} ms")
            print(f"  P99:    {stats['p99_ms']:8.1f} ms")
            print(f"  StdDev: {stats['stdev_ms']:8.1f} ms")
        
        print(f"\n{'='*60}")
        
        # Summary for easy comparison
        print(f"\n📊 SUMMARY (Trade Feed Latency):")
        for symbol, stats_obj in sorted(self.stats.items()):
            stats = stats_obj.get_stats()
            if stats:
                print(f"   {stats_obj.symbol:10} {stats['median_ms']:5.1f}ms median, "
                      f"{stats['p95_ms']:5.1f}ms p95")


def main():
    parser = argparse.ArgumentParser(
        description="Test Binance trade feed latency",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Measures trade feed latency by comparing:
  - Exchange timestamp (when trade executed at Binance)
  - Receive timestamp (when we got the message)

Shows how stale your market data view is.

Examples:
  # Basic test (30 seconds)
  uv run --with websockets test_binance_latency.py
  
  # Longer test
  uv run --with websockets test_binance_latency.py --duration 60
  
  # Different symbols
  uv run --with websockets test_binance_latency.py --symbols BTCUSDT,ETHUSDT,SOLUSDT
        """
    )
    parser.add_argument(
        "--duration", "-d",
        type=float,
        default=30,
        help="Test duration in seconds (default: 30)"
    )
    parser.add_argument(
        "--symbols", "-s",
        type=str,
        default="BTCUSDT,ETHUSDT",
        help="Comma-separated list of symbols (default: BTCUSDT,ETHUSDT)"
    )
    
    args = parser.parse_args()
    
    # Parse symbols
    symbols = [s.strip().upper() for s in args.symbols.split(",")]
    
    try:
        tester = BinanceLatencyTest(symbols=symbols)
        asyncio.run(tester.run_test(duration_seconds=args.duration))
        
        print("\n✅ Test complete!")
        
    except KeyboardInterrupt:
        print("\n\nInterrupted by user")
    except Exception as e:
        print(f"\n❌ Error: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    main()
