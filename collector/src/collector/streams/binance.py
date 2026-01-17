"""Binance websocket consumer."""

import asyncio
import logging
from time import time_ns
from typing import Dict, Optional, Callable
from collections import defaultdict

import orjson
import websockets

logger = logging.getLogger(__name__)

# Hardcoded symbols for now
BINANCE_SYMBOLS = ["BTCUSDT", "ETHUSDT"]


class BinanceConsumer:
    """Consumes Binance websocket streams and normalizes rows."""
    
    def __init__(
        self,
        symbols: list[str],
        on_row: Callable[[dict], None],
    ):
        self.symbols = [s.lower() for s in symbols]
        streams = "/".join(f"{s}@bookTicker/{s}@trade" for s in self.symbols)
        self.url = f"wss://stream.binance.com:9443/stream?streams={streams}"
        self.on_row = on_row
        
        # Sequence tracking per stream (local sequence counter)
        self._sequences: Dict[str, int] = defaultdict(int)
        
        self._running = False
        self._backoff_seconds = 1.0
        self._max_backoff = 60.0
    
    def _normalize_row(self, stream_name: str, payload: dict, recv_ts_ms: int) -> dict:
        """
        Normalize Binance message to standard row format.
        
        Returns:
            Normalized row dict with: ts_event, ts_recv, venue, stream_id, seq, ...
        """
        symbol = payload["s"]
        is_trade = stream_name.endswith("trade")
        
        # Determine stream_id (just the symbol)
        stream_id = symbol
        
        # Get sequence number (local counter, not exchange sequence)
        seq_key = f"{symbol}_{'trade' if is_trade else 'bbo'}"
        self._sequences[seq_key] += 1
        seq = self._sequences[seq_key]
        
        # Build normalized row
        row = {
            "ts_event": payload.get("E", recv_ts_ms),  # Exchange timestamp
            "ts_recv": recv_ts_ms,  # Server receive time
            "venue": "binance",
            "stream_id": stream_id,
            "seq": seq,
            "event_type": "trade" if is_trade else "bbo",
        }
        
        if is_trade:
            row.update({
                "price": float(payload["p"]),
                "size": float(payload["q"]),
                "side": "sell" if payload.get("m", False) else "buy",
                "trade_id": payload.get("t"),
            })
        else:
            row.update({
                "bid_px": float(payload["b"]),
                "bid_sz": float(payload["B"]),
                "ask_px": float(payload["a"]),
                "ask_sz": float(payload["A"]),
                "update_id": payload.get("u"),
            })
        
        return row
    
    def _handle_message(self, raw: bytes) -> None:
        """Handle incoming websocket message."""
        recv_ts_ms = time_ns() // 1_000_000
        data = orjson.loads(raw)
        
        stream_name: str = data["stream"]
        payload: dict = data["data"]
        
        row = self._normalize_row(stream_name, payload, recv_ts_ms)
        self.on_row(row)
    
    async def run(self, shutdown_event: Optional[asyncio.Event] = None) -> None:
        """Run the consumer with exponential backoff reconnection."""
        self._running = True
        
        while self._running:
            if shutdown_event and shutdown_event.is_set():
                break
            
            try:
                async with websockets.connect(
                    self.url,
                    ping_interval=20,
                    ping_timeout=60,
                    max_size=2**20,
                    compression=None,
                ) as ws:
                    logger.info(f"Binance connected: {len(self.symbols)} symbols")
                    self._backoff_seconds = 1.0  # Reset backoff on successful connection
                    
                    async for message in ws:
                        if shutdown_event and shutdown_event.is_set():
                            break
                        if not self._running:
                            break
                        try:
                            self._handle_message(message)
                        except Exception as e:
                            logger.warning(f"Error handling Binance message: {e}")
                            continue
            
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(f"Binance connection error: {e}, reconnecting in {self._backoff_seconds:.1f}s")
            
            if shutdown_event and shutdown_event.is_set():
                break
            if not self._running:
                break
            
            # Exponential backoff with jitter
            await asyncio.sleep(self._backoff_seconds)
            self._backoff_seconds = min(self._backoff_seconds * 2, self._max_backoff)
    
    def stop(self):
        """Stop the consumer."""
        self._running = False
