#!/usr/bin/env python3
"""
Binance Streamer (Frankfurt VPS)

Connects to Binance WebSocket, extracts BBO data, and streams to Amsterdam.
Designed for 24/7 operation with automatic reconnection.
"""

import asyncio
import ssl
import struct
import time
import socket
import json
import os
import logging

import msgpack
import websockets

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

# Configuration from environment
AMS_HOST = os.environ.get("AMS_HOST", "146.190.30.198")
AMS_PORT = int(os.environ.get("AMS_PORT", "9001"))
CA_CERT = os.environ.get("CA_CERT", "/certs/ams_server.crt")
SYMBOLS = os.environ.get("SYMBOLS", "btcusdt").lower().split(",")

# Build WebSocket URL
streams = "/".join([f"{s.strip()}@bookTicker" for s in SYMBOLS])
BINANCE_WS = f"wss://stream.binance.com:9443/stream?streams={streams}"


def frame(obj: dict) -> bytes:
    """Pack object with length prefix for TLS stream."""
    b = msgpack.packb(obj, use_bin_type=True)
    return struct.pack("!I", len(b)) + b


class BinanceStreamer:
    def __init__(self):
        self.seq = 0
        self.ams_writer = None
        self.stats = {"sent": 0, "errors": 0, "last_send": 0}
    
    async def connect_amsterdam(self) -> bool:
        """Establish TLS connection to Amsterdam receiver."""
        try:
            ctx = ssl.create_default_context(ssl.Purpose.SERVER_AUTH, cafile=CA_CERT)
            ctx.check_hostname = False
            
            logger.info(f"Connecting to Amsterdam {AMS_HOST}:{AMS_PORT}...")
            _r, self.ams_writer = await asyncio.open_connection(AMS_HOST, AMS_PORT, ssl=ctx)
            
            # Set TCP_NODELAY for low latency
            s = self.ams_writer.get_extra_info("socket")
            if s:
                s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            
            logger.info("Connected to Amsterdam")
            return True
            
        except Exception as e:
            logger.error(f"Amsterdam connection failed: {e}")
            return False
    
    async def send_to_amsterdam(self, payload: dict) -> bool:
        """Send payload to Amsterdam, return success status."""
        if not self.ams_writer:
            return False
        
        try:
            self.ams_writer.write(frame(payload))
            await self.ams_writer.drain()
            self.stats["sent"] += 1
            self.stats["last_send"] = time.time()
            return True
        except Exception as e:
            logger.error(f"Send failed: {e}")
            self.stats["errors"] += 1
            return False
    
    async def process_binance_message(self, msg: str) -> dict | None:
        """Parse Binance message and return payload for Amsterdam."""
        try:
            data = json.loads(msg)
            
            # Handle combined stream format
            if "stream" in data and "data" in data:
                data = data["data"]
            
            symbol = data.get("s", "")
            best_bid = float(data.get("b", 0))
            best_ask = float(data.get("a", 0))
            binance_ts = data.get("E", 0)
            
            if best_bid == 0 or best_ask == 0:
                return None
            
            self.seq += 1
            
            return {
                "seq": self.seq,
                "ts": time.time_ns(),
                "symbol": symbol,
                "mid": (best_bid + best_ask) / 2,
                "bid": best_bid,
                "ask": best_ask,
                "binance_ts": binance_ts
            }
            
        except (json.JSONDecodeError, KeyError, ValueError) as e:
            logger.warning(f"Parse error: {e}")
            return None
    
    async def log_stats(self):
        """Periodic stats logging."""
        while True:
            await asyncio.sleep(60)
            logger.info(
                f"Stats: sent={self.stats['sent']}, errors={self.stats['errors']}, "
                f"seq={self.seq}"
            )
    
    async def run(self):
        """Main loop with automatic reconnection."""
        asyncio.create_task(self.log_stats())
        
        while True:
            try:
                # Connect to Amsterdam
                if not await self.connect_amsterdam():
                    await asyncio.sleep(5)
                    continue
                
                # Connect to Binance
                logger.info(f"Connecting to Binance: {BINANCE_WS}")
                
                async with websockets.connect(
                    BINANCE_WS,
                    ping_interval=20,
                    ping_timeout=10,
                    close_timeout=5
                ) as ws:
                    logger.info(f"Connected to Binance, streaming {SYMBOLS}")
                    
                    async for msg in ws:
                        payload = await self.process_binance_message(msg)
                        if payload:
                            if not await self.send_to_amsterdam(payload):
                                # Amsterdam connection lost, reconnect
                                break
                
            except websockets.exceptions.ConnectionClosed as e:
                logger.warning(f"Binance connection closed: {e}")
            except Exception as e:
                logger.error(f"Error: {e}")
            
            # Cleanup
            if self.ams_writer:
                try:
                    self.ams_writer.close()
                    await self.ams_writer.wait_closed()
                except:
                    pass
                self.ams_writer = None
            
            logger.info("Reconnecting in 2s...")
            await asyncio.sleep(2)


if __name__ == "__main__":
    logger.info("Starting Binance Streamer (Frankfurt)")
    logger.info(f"Symbols: {SYMBOLS}")
    logger.info(f"Amsterdam: {AMS_HOST}:{AMS_PORT}")
    
    streamer = BinanceStreamer()
    asyncio.run(streamer.run())
