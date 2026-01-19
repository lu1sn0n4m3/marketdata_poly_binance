# Binance Data Streaming: Frankfurt → Amsterdam Deployment

This guide explains how to deploy the Binance data streaming infrastructure using Docker containers for 24/7 operation.

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                                                                             │
│  ┌──────────────┐         TLS/msgpack          ┌──────────────────────┐    │
│  │   FRANKFURT  │         ~3.5ms               │     AMSTERDAM        │    │
│  │   (sender)   │ ──────────────────────────>  │     (receiver)       │    │
│  │              │         Port 9001            │                      │    │
│  └──────┬───────┘                              └──────────┬───────────┘    │
│         │                                                 │                │
│         │ WebSocket                                       │ Local          │
│         │ ~127ms                                          │                │
│         ▼                                                 ▼                │
│  ┌──────────────┐                              ┌──────────────────────┐    │
│  │   BINANCE    │                              │  Trading Executor    │    │
│  │   (Tokyo)    │                              │  (Polymarket MM)     │    │
│  └──────────────┘                              └──────────────────────┘    │
│                                                                             │
└─────────────────────────────────────────────────────────────────────────────┘
```

**Latency Budget:**
- Binance → Frankfurt: ~127ms (Tokyo → Europe, unavoidable)
- Frankfurt → Amsterdam: ~3.5ms (tested)
- **Total signal latency: ~131ms**

## Repository Structure

Add the following files to your repository:

```
marketdata_poly_binance/
├── streaming/
│   ├── sender/
│   │   ├── Dockerfile
│   │   ├── sender.py
│   │   └── requirements.txt
│   ├── receiver/
│   │   ├── Dockerfile
│   │   ├── receiver.py
│   │   └── requirements.txt
│   ├── docker-compose.sender.yml      # Frankfurt
│   ├── docker-compose.receiver.yml    # Amsterdam
│   └── scripts/
│       └── generate-certs.sh
├── docs/
│   └── BINANCE_STREAMING_DEPLOYMENT.md  # This file
```

## Step 1: Create the Sender (Frankfurt)

### `streaming/sender/Dockerfile`

```dockerfile
FROM python:3.12-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application
COPY sender.py .

# Health check - verify process is running
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD pgrep -f "python sender.py" || exit 1

# Run sender
CMD ["python", "-u", "sender.py"]
```

### `streaming/sender/requirements.txt`

```
websockets>=12.0
msgpack>=1.0
```

### `streaming/sender/sender.py`

```python
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
```

### `streaming/docker-compose.sender.yml`

```yaml
# Deploy on Frankfurt VPS
version: "3.8"

services:
  binance-sender:
    build:
      context: ./sender
      dockerfile: Dockerfile
    container_name: binance-sender
    restart: always
    environment:
      - AMS_HOST=146.190.30.198          # Amsterdam receiver IP
      - AMS_PORT=9001
      - SYMBOLS=btcusdt,ethusdt          # Comma-separated symbols
      - TZ=UTC
    volumes:
      - ./certs:/certs:ro                 # Mount TLS certificates
    logging:
      driver: json-file
      options:
        max-size: "10m"
        max-file: "3"
    # Limit resources
    deploy:
      resources:
        limits:
          cpus: "0.5"
          memory: 256M
```

---

## Step 2: Create the Receiver (Amsterdam)

### `streaming/receiver/Dockerfile`

```dockerfile
FROM python:3.12-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application
COPY receiver.py .

# Expose port
EXPOSE 9001

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD pgrep -f "python receiver.py" || exit 1

# Run receiver
CMD ["python", "-u", "receiver.py"]
```

### `streaming/receiver/requirements.txt`

```
msgpack>=1.0
```

### `streaming/receiver/receiver.py`

```python
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
```

### `streaming/docker-compose.receiver.yml`

```yaml
# Deploy on Amsterdam VPS
version: "3.8"

services:
  binance-receiver:
    build:
      context: ./receiver
      dockerfile: Dockerfile
    container_name: binance-receiver
    restart: always
    ports:
      - "9001:9001"
    environment:
      - PORT=9001
      - TZ=UTC
    volumes:
      - ./certs:/certs:ro               # Mount TLS certificates
    logging:
      driver: json-file
      options:
        max-size: "10m"
        max-file: "3"
    deploy:
      resources:
        limits:
          cpus: "0.5"
          memory: 256M
```

---

## Step 3: Generate TLS Certificates

### `streaming/scripts/generate-certs.sh`

```bash
#!/bin/bash
#
# Generate TLS certificates for sender/receiver communication
# Run this once and distribute the certs to both VPSs
#

set -e

CERT_DIR="./certs"
mkdir -p "$CERT_DIR"

# Certificate validity (days)
DAYS=3650  # 10 years

# Amsterdam receiver IP (update this!)
AMS_IP="146.190.30.198"

echo "Generating TLS certificates for Amsterdam receiver ($AMS_IP)..."

# Generate private key
openssl genrsa -out "$CERT_DIR/server.key" 2048

# Generate self-signed certificate
openssl req -new -x509 \
    -key "$CERT_DIR/server.key" \
    -out "$CERT_DIR/server.crt" \
    -days "$DAYS" \
    -subj "/CN=$AMS_IP" \
    -addext "subjectAltName=IP:$AMS_IP"

# Copy server cert as CA cert for sender
cp "$CERT_DIR/server.crt" "$CERT_DIR/ams_server.crt"

echo ""
echo "Certificates generated in $CERT_DIR/"
echo ""
echo "Files:"
echo "  server.key       - Private key (Amsterdam only, keep secret!)"
echo "  server.crt       - Server certificate (Amsterdam)"
echo "  ams_server.crt   - CA certificate (Frankfurt, for verification)"
echo ""
echo "Deployment:"
echo "  Amsterdam: Copy server.key and server.crt to /root/certs/"
echo "  Frankfurt: Copy ams_server.crt to /root/certs/"
echo ""
```

Run the script:

```bash
cd streaming/scripts
chmod +x generate-certs.sh
./generate-certs.sh
```

---

## Step 4: Deploy to VPSs

### Amsterdam VPS (Receiver - deploy first)

```bash
# SSH into Amsterdam VPS
ssh root@146.190.30.198

# Create directories
mkdir -p /opt/binance-receiver/certs

# Copy files (from your local machine)
# scp streaming/receiver/* root@146.190.30.198:/opt/binance-receiver/
# scp streaming/docker-compose.receiver.yml root@146.190.30.198:/opt/binance-receiver/docker-compose.yml
# scp streaming/certs/server.* root@146.190.30.198:/opt/binance-receiver/certs/

# On the VPS:
cd /opt/binance-receiver

# Build and start
docker compose up -d --build

# Check logs
docker compose logs -f
```

### Frankfurt VPS (Sender)

```bash
# SSH into Frankfurt VPS
ssh root@<frankfurt-ip>

# Create directories
mkdir -p /opt/binance-sender/certs

# Copy files (from your local machine)
# scp streaming/sender/* root@<frankfurt-ip>:/opt/binance-sender/
# scp streaming/docker-compose.sender.yml root@<frankfurt-ip>:/opt/binance-sender/docker-compose.yml
# scp streaming/certs/ams_server.crt root@<frankfurt-ip>:/opt/binance-sender/certs/

# On the VPS:
cd /opt/binance-sender

# Build and start
docker compose up -d --build

# Check logs
docker compose logs -f
```

---

## Step 5: 24/7 Operation

### Automatic Restart

The `restart: always` policy in docker-compose ensures containers restart after:
- Container crash
- Docker daemon restart
- VPS reboot

### Enable Docker on Boot

```bash
# On both VPSs
sudo systemctl enable docker
```

### Monitoring Commands

```bash
# Check container status
docker ps

# View logs (last 100 lines, follow)
docker compose logs -f --tail 100

# Check resource usage
docker stats

# Restart if needed
docker compose restart
```

### Health Monitoring with Cron

Create `/opt/binance-receiver/healthcheck.sh` (Amsterdam):

```bash
#!/bin/bash
# Check if receiver is running and receiving data

CONTAINER="binance-receiver"
LOG_FILE="/var/log/binance-healthcheck.log"

if ! docker ps --format '{{.Names}}' | grep -q "^${CONTAINER}$"; then
    echo "$(date) - Container not running, restarting..." >> "$LOG_FILE"
    cd /opt/binance-receiver && docker compose up -d
fi
```

Add to crontab:

```bash
# Check every 5 minutes
*/5 * * * * /opt/binance-receiver/healthcheck.sh
```

---

## Step 6: Firewall Configuration

### Amsterdam VPS

```bash
# Allow incoming on port 9001 from Frankfurt only
ufw allow from <frankfurt-ip> to any port 9001

# Or allow from any (if Frankfurt IP changes)
ufw allow 9001/tcp
```

### Frankfurt VPS

No incoming ports needed - only outgoing connections.

---

## Step 7: Integration with Trading Executor

The receiver exposes functions for integration with your Polymarket trading executor:

```python
# In your trading executor (Amsterdam)
from receiver import set_message_callback, get_latest_quote

def on_binance_update(msg: dict):
    """Called for every Binance update."""
    symbol = msg["symbol"]
    mid = msg["mid"]
    bid = msg["bid"]
    ask = msg["ask"]
    
    # Your trading logic here
    if should_update_quotes(mid):
        update_polymarket_orders(mid)

# Register callback
set_message_callback(on_binance_update)

# Or poll latest quote
quote = get_latest_quote()
```

---

## Troubleshooting

### Sender can't connect to Amsterdam

```bash
# From Frankfurt, test connectivity
nc -zv 146.190.30.198 9001

# Check if receiver is listening
# On Amsterdam:
ss -tlnp | grep 9001
```

### High latency spikes

```bash
# Check network path
mtr --report 146.190.30.198

# Check container resource usage
docker stats
```

### Sequence gaps

Indicates network packet loss. Check:
- VPS network health in DigitalOcean dashboard
- Container resource limits (increase memory if needed)

### Certificate errors

```bash
# Verify certificate matches IP
openssl x509 -in /certs/server.crt -noout -text | grep -A1 "Subject Alternative Name"

# Regenerate if Amsterdam IP changed
./generate-certs.sh
```

---

## Summary

| Component | Location | Purpose | Port |
|-----------|----------|---------|------|
| Sender | Frankfurt | Connect to Binance, stream to Amsterdam | Outbound only |
| Receiver | Amsterdam | Receive data, serve to trading executor | 9001 |

**Expected Performance:**
- Frankfurt → Amsterdam latency: ~3.5ms p50
- Message rate: ~60-80 msg/s (BTCUSDT)
- Zero message gaps under normal operation

**Total Binance → Polymarket signal path:**
- Binance → Frankfurt: ~127ms
- Frankfurt → Amsterdam: ~3.5ms
- **Total: ~131ms**
