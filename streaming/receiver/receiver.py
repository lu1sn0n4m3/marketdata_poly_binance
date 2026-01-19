#!/usr/bin/env python3
"""
Binance Data Receiver (Amsterdam VPS)

Receives Binance BBO data from Frankfurt streamer over TLS.
Designed for 24/7 operation with stats and monitoring.
"""

import asyncio
import ssl
import struct
import time
import statistics
import os
import logging
from typing import Callable

import msgpack

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

# Configuration from environment
PORT = int(os.environ.get("PORT", "9001"))
CERT = os.environ.get("CERT", "/certs/server.crt")
KEY = os.environ.get("KEY", "/certs/server.key")


class Stats:
    """Track latency and message statistics."""
    
    def __init__(self):
        self.latencies = []
        self.last_seq = 0
        self.gaps = 0
        self.total_msgs = 0
        self.start_time = time.time()
        self.last_print = time.time()
        self.last_msg = {}
    
    def add(self, latency_us: float, seq: int, msg: dict):
        self.total_msgs += 1
        self.latencies.append(latency_us)
        self.last_msg = msg
        
        # Check sequence gaps
        if self.last_seq > 0 and seq != self.last_seq + 1:
            gap = seq - self.last_seq - 1
            if gap > 0:
                self.gaps += gap
                logger.warning(f"Sequence gap: expected {self.last_seq + 1}, got {seq}")
        self.last_seq = seq
        
        # Keep only last 1000 for memory
        if len(self.latencies) > 1000:
            self.latencies = self.latencies[-1000:]
    
    def should_log(self) -> bool:
        return time.time() - self.last_print >= 10.0
    
    def log_stats(self):
        if not self.latencies:
            return
        
        elapsed = time.time() - self.start_time
        rate = self.total_msgs / elapsed if elapsed > 0 else 0
        
        p50 = statistics.median(self.latencies)
        sorted_lat = sorted(self.latencies)
        p95 = sorted_lat[int(len(sorted_lat) * 0.95)] if len(sorted_lat) > 20 else max(sorted_lat)
        p99 = sorted_lat[int(len(sorted_lat) * 0.99)] if len(sorted_lat) > 100 else max(sorted_lat)
        
        symbol = self.last_msg.get("symbol", "???")
        mid = self.last_msg.get("mid", 0)
        
        logger.info(
            f"{symbol} mid={mid:,.2f} | "
            f"p50={p50/1000:.1f}ms p95={p95/1000:.1f}ms p99={p99/1000:.1f}ms | "
            f"{rate:.1f} msg/s | total={self.total_msgs} gaps={self.gaps}"
        )
        
        self.last_print = time.time()
    
    def get_latest(self) -> dict:
        """Get latest message for trading executor."""
        return self.last_msg.copy()


# Global stats and callback for trading executor integration
_stats = Stats()
_message_callback: Callable[[dict], None] | None = None


def set_message_callback(callback: Callable[[dict], None]):
    """Set callback to receive messages in trading executor."""
    global _message_callback
    _message_callback = callback


def get_latest_quote() -> dict:
    """Get the latest quote (for trading executor)."""
    return _stats.get_latest()


async def handle_connection(r: asyncio.StreamReader, w: asyncio.StreamWriter):
    """Handle incoming connection from Frankfurt streamer."""
    peer = w.get_extra_info("peername")
    logger.info(f"Connection from {peer}")
    
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
                _stats.add(latency_us, seq, msg)
                
                # Invoke callback for trading executor
                if _message_callback:
                    try:
                        _message_callback(msg)
                    except Exception as e:
                        logger.error(f"Callback error: {e}")
                
                # Log stats periodically
                if _stats.should_log():
                    _stats.log_stats()
            
    except asyncio.IncompleteReadError:
        logger.info(f"Connection closed by {peer}")
    except Exception as e:
        logger.error(f"Error handling {peer}: {e}")
    finally:
        w.close()
        await w.wait_closed()
        
        # Log final stats
        logger.info(f"Session ended: {_stats.total_msgs} messages, {_stats.gaps} gaps")


async def run_server():
    """Run the TLS server."""
    # SSL context
    ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    ctx.load_cert_chain(CERT, KEY)
    
    srv = await asyncio.start_server(handle_connection, "0.0.0.0", PORT, ssl=ctx)
    logger.info(f"Listening on 0.0.0.0:{PORT}")
    
    async with srv:
        await srv.serve_forever()


if __name__ == "__main__":
    logger.info("Starting Binance Data Receiver (Amsterdam)")
    asyncio.run(run_server())
