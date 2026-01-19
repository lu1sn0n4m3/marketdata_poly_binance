#!/usr/bin/env python3
"""
Binance Streamer (London VPS)

Connects to Binance WebSocket, extracts BBO data, and streams to Amsterdam.
Runs on London VPS where Binance is accessible.

Usage:
    python sender.py
    
    # Or with uv
    uv run --with websockets,msgpack sender.py
"""

import asyncio
import ssl
import struct
import time
import socket
import json

try:
    import msgpack
except ImportError:
    print("Install msgpack: pip install msgpack")
    exit(1)

try:
    import websockets
except ImportError:
    print("Install websockets: pip install websockets")
    exit(1)

# Amsterdam receiver config
AMS_HOST = "146.190.30.198"
AMS_PORT = 9001
CA_CERT = "/root/ams_server.crt"

# Binance WebSocket
BINANCE_WS = "wss://stream.binance.com:9443/ws/btcusdt@bookTicker"


def frame(obj):
    """Pack object with length prefix for TLS stream."""
    b = msgpack.packb(obj, use_bin_type=True)
    return struct.pack("!I", len(b)) + b


async def binance_to_amsterdam():
    """Main loop: Binance -> Amsterdam streaming."""
    
    # SSL context for Amsterdam connection
    ctx = ssl.create_default_context(ssl.Purpose.SERVER_AUTH, cafile=CA_CERT)
    ctx.check_hostname = False
    
    seq = 0
    ams_writer = None
    last_msg = None  # For drop-stale logic
    
    while True:
        try:
            # Connect to Amsterdam
            print(f"[SENDER] Connecting to Amsterdam {AMS_HOST}:{AMS_PORT}...")
            _r, ams_writer = await asyncio.open_connection(AMS_HOST, AMS_PORT, ssl=ctx)
            
            # Set TCP_NODELAY for low latency
            s = ams_writer.get_extra_info("socket")
            if s:
                s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            
            print(f"[SENDER] Connected to Amsterdam")
            
            # Connect to Binance
            print(f"[SENDER] Connecting to Binance...")
            async with websockets.connect(BINANCE_WS) as ws:
                print(f"[SENDER] Connected to Binance, streaming BTCUSDT...")
                
                async for msg in ws:
                    try:
                        data = json.loads(msg)
                        
                        # Extract BBO from Binance bookTicker
                        # Format: {"u":123,"s":"BTCUSDT","b":"104500.00","B":"1.5","a":"104501.00","A":"2.0","E":1234567890123}
                        symbol = data.get("s", "BTCUSDT")
                        best_bid = float(data.get("b", 0))
                        best_ask = float(data.get("a", 0))
                        binance_ts = data.get("E", 0)  # Event time in ms
                        
                        if best_bid == 0 or best_ask == 0:
                            continue
                        
                        mid = (best_bid + best_ask) / 2
                        seq += 1
                        
                        # Build payload per docs
                        payload = {
                            "seq": seq,
                            "ts": time.time_ns(),  # London send timestamp
                            "symbol": symbol,
                            "mid": mid,
                            "bid": best_bid,
                            "ask": best_ask,
                            "binance_ts": binance_ts
                        }
                        
                        # Send to Amsterdam (no buffering)
                        ams_writer.write(frame(payload))
                        await ams_writer.drain()
                        
                    except json.JSONDecodeError:
                        continue
                    except Exception as e:
                        print(f"[SENDER] Error processing message: {e}")
                        continue
                        
        except Exception as e:
            print(f"[SENDER] Error: {e!r} (retry in 1s)")
            if ams_writer:
                try:
                    ams_writer.close()
                    await ams_writer.wait_closed()
                except:
                    pass
            await asyncio.sleep(1)


if __name__ == "__main__":
    print("[SENDER] Starting Binance -> Amsterdam streamer")
    print(f"[SENDER] Binance: {BINANCE_WS}")
    print(f"[SENDER] Amsterdam: {AMS_HOST}:{AMS_PORT}")
    asyncio.run(binance_to_amsterdam())
