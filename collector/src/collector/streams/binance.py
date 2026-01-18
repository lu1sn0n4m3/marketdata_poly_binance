"""Binance websocket consumer."""

import logging
from collections import defaultdict
from typing import Callable, Dict

import orjson
import websockets

from .base import BaseConsumer

logger = logging.getLogger(__name__)

# Symbols to subscribe to
BINANCE_SYMBOLS = ["BTCUSDT", "ETHUSDT"]


class BinanceConsumer(BaseConsumer):
    """
    Consumes Binance websocket streams and emits normalized BBO and trade rows.
    
    Subscribes to:
    - {symbol}@bookTicker: Best bid/offer updates
    - {symbol}@trade: Trade events
    
    Emits rows with:
    - event_type: "bbo" or "trade"
    - Strict schema fields per event type
    """
    
    def __init__(
        self,
        symbols: list[str],
        on_row: Callable[[dict], None],
    ):
        super().__init__(on_row)
        
        self.symbols = [s.lower() for s in symbols]
        self.symbols_upper = [s.upper() for s in symbols]
        
        # Build websocket URL with all streams
        streams = "/".join(
            f"{s}@bookTicker/{s}@trade" for s in self.symbols
        )
        self._ws_url = f"wss://stream.binance.com:9443/stream?streams={streams}"
        
        # Sequence counters per (symbol, event_type)
        self._sequences: Dict[str, int] = defaultdict(int)
    
    def _get_ws_url(self) -> str:
        return self._ws_url
    
    async def _on_connect(self, ws: websockets.WebSocketClientProtocol) -> None:
        # Binance combined streams don't require subscription message
        # Just log what we're tracking
        logger.info(f"Binance: Tracking {len(self.symbols)} symbols: {self.symbols_upper}")
    
    def _get_name(self) -> str:
        return f"Binance({len(self.symbols)} symbols)"
    
    def _handle_message(self, raw: bytes, recv_ts_ms: int) -> None:
        """Parse and normalize Binance message."""
        data = orjson.loads(raw)
        
        stream_name: str = data["stream"]
        payload: dict = data["data"]
        
        symbol = payload["s"]
        is_trade = stream_name.endswith("trade")
        event_type = "trade" if is_trade else "bbo"
        
        # Increment sequence for this (symbol, event_type)
        seq_key = f"{symbol}_{event_type}"
        self._sequences[seq_key] += 1
        seq = self._sequences[seq_key]
        
        if is_trade:
            row = self._normalize_trade(payload, recv_ts_ms, symbol, seq)
        else:
            row = self._normalize_bbo(payload, recv_ts_ms, symbol, seq)
        
        self._emit_row(row)
    
    def _normalize_bbo(
        self,
        payload: dict,
        recv_ts_ms: int,
        symbol: str,
        seq: int,
    ) -> dict:
        """
        Normalize Binance bookTicker to BBO row.
        
        Schema: ts_event, ts_recv, seq, bid_px, bid_sz, ask_px, ask_sz, update_id
        """
        return {
            "venue": "binance",
            "stream_id": symbol,
            "event_type": "bbo",
            "ts_event": payload.get("E", recv_ts_ms),
            "ts_recv": recv_ts_ms,
            "seq": seq,
            "bid_px": float(payload["b"]),
            "bid_sz": float(payload["B"]),
            "ask_px": float(payload["a"]),
            "ask_sz": float(payload["A"]),
            "update_id": payload.get("u", 0),
        }
    
    def _normalize_trade(
        self,
        payload: dict,
        recv_ts_ms: int,
        symbol: str,
        seq: int,
    ) -> dict:
        """
        Normalize Binance trade to trade row.
        
        Schema: ts_event, ts_recv, seq, price, size, side, trade_id
        """
        # In Binance, "m" (is buyer maker) = True means sell aggressor
        side = "sell" if payload.get("m", False) else "buy"
        
        return {
            "venue": "binance",
            "stream_id": symbol,
            "event_type": "trade",
            "ts_event": payload.get("E", recv_ts_ms),
            "ts_recv": recv_ts_ms,
            "seq": seq,
            "price": float(payload["p"]),
            "size": float(payload["q"]),
            "side": side,
            "trade_id": payload.get("t", 0),
        }
