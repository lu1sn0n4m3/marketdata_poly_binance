#!/usr/bin/env python3
"""
Binance Data Receiver (Amsterdam VPS)

Receives Binance BBO data from London streamer over TLS.
Measures latency and validates sequence ordering.

Usage:
    python receiver.py
    
    # Or with uv
    uv run --with msgpack receiver.py
"""

import asyncio
import ssl
import struct
import time
import statistics

try:
    import msgpack
except ImportError:
    print("Install msgpack: pip install msgpack")
    exit(1)

PORT = 9001
CERT = "server.crt"
KEY = "server.key"

# Stats tracking
class Stats:
    def __init__(self):
        self.latencies = []
        self.last_seq = 0
        self.gaps = 0
        self.total_msgs = 0
        self.start_time = time.time()
        self.last_print = time.time()
    
    def add(self, latency_us: float, seq: int):
        self.total_msgs += 1
        self.latencies.append(latency_us)
        
        # Check sequence
        if self.last_seq > 0 and seq != self.last_seq + 1:
            gap = seq - self.last_seq - 1
            if gap > 0:
                self.gaps += gap
        self.last_seq = seq
        
        # Keep only last 1000 for memory
        if len(self.latencies) > 1000:
            self.latencies = self.latencies[-1000:]
    
    def should_print(self) -> bool:
        return time.time() - self.last_print >= 1.0
    
    def print_stats(self, last_msg: dict):
        if not self.latencies:
            return
        
        elapsed = time.time() - self.start_time
        rate = self.total_msgs / elapsed if elapsed > 0 else 0
        
        p50 = statistics.median(self.latencies)
        p95 = sorted(self.latencies)[int(len(self.latencies) * 0.95)] if len(self.latencies) > 20 else max(self.latencies)
        p99 = sorted(self.latencies)[int(len(self.latencies) * 0.99)] if len(self.latencies) > 100 else max(self.latencies)
        
        symbol = last_msg.get("symbol", "???")
        mid = last_msg.get("mid", 0)
        bid = last_msg.get("bid", 0)
        ask = last_msg.get("ask", 0)
        
        print(f"[{elapsed:6.1f}s] {symbol} mid={mid:,.2f} bid={bid:,.2f} ask={ask:,.2f} | "
              f"p50={p50/1000:5.1f}ms p95={p95/1000:5.1f}ms p99={p99/1000:5.1f}ms | "
              f"{rate:.1f} msg/s | gaps={self.gaps}")
        
        self.last_print = time.time()


async def handle(r: asyncio.StreamReader, w: asyncio.StreamWriter):
    """Handle incoming connection from London streamer."""
    peer = w.get_extra_info("peername")
    print(f"[RECEIVER] Connection from {peer}")
    
    stats = Stats()
    
    try:
        while True:
            # Read length-prefixed message
            hdr = await r.readexactly(4)
            n = struct.unpack("!I", hdr)[0]
            payload = await r.readexactly(n)
            msg = msgpack.unpackb(payload, raw=False)
            
            # Calculate latency
            recv_ns = time.time_ns()
            sent_ns = msg.get("ts")
            seq = msg.get("seq", 0)
            
            if sent_ns is not None:
                latency_us = (recv_ns - sent_ns) / 1_000
                stats.add(latency_us, seq)
                
                # Print aggregated stats once per second
                if stats.should_print():
                    stats.print_stats(msg)
            
    except asyncio.IncompleteReadError:
        print(f"[RECEIVER] Connection closed by {peer}")
    except Exception as e:
        print(f"[RECEIVER] Error: {e}")
    finally:
        w.close()
        await w.wait_closed()
        
        # Print final stats
        if stats.latencies:
            print(f"\n[RECEIVER] Final stats after {stats.total_msgs} messages:")
            print(f"  Latency p50: {statistics.median(stats.latencies)/1000:.2f} ms")
            print(f"  Latency p95: {sorted(stats.latencies)[int(len(stats.latencies)*0.95)]/1000:.2f} ms")
            print(f"  Sequence gaps: {stats.gaps}")


async def main():
    # SSL context
    ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    ctx.load_cert_chain(CERT, KEY)
    
    srv = await asyncio.start_server(handle, "0.0.0.0", PORT, ssl=ctx)
    print(f"[RECEIVER] Listening on 0.0.0.0:{PORT}")
    print(f"[RECEIVER] Waiting for Binance data from London...")
    
    async with srv:
        await srv.serve_forever()


if __name__ == "__main__":
    print("[RECEIVER] Starting Binance data receiver")
    asyncio.run(main())
