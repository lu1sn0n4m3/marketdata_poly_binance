"""Polymarket websocket consumer with auto-discovery."""

import asyncio
import logging
import re
from datetime import datetime, timezone, timedelta
from typing import Callable, Dict, List, Optional

import aiohttp
import orjson
import websockets

from .base import BaseConsumer

logger = logging.getLogger(__name__)

GAMMA_API = "https://gamma-api.polymarket.com/events"
WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"


def parse_polymarket_url(url: str) -> Optional[str]:
    """Extract full slug from Polymarket URL."""
    match = re.search(r"/event/([^/?]+)", url)
    return match.group(1) if match else None


def get_eastern_time() -> datetime:
    """
    Get current Eastern Time (ET) from UTC.
    
    Note: Uses UTC-5 (EST). Daylight saving handling can be added later.
    """
    utc_now = datetime.now(timezone.utc)
    et_tz = timezone(timedelta(hours=-5))
    return utc_now.astimezone(et_tz)


def derive_market_url(market_slug: str, et_time: datetime) -> str:
    """
    Derive Polymarket URL from market slug and Eastern time.
    
    Format: https://polymarket.com/event/{market-slug}-{month}-{day}-{hour}pm-et
    """
    month_names = [
        "january", "february", "march", "april", "may", "june",
        "july", "august", "september", "october", "november", "december",
    ]
    
    month = month_names[et_time.month - 1]
    day = et_time.day
    hour_12 = et_time.hour % 12
    if hour_12 == 0:
        hour_12 = 12
    am_pm = "pm" if et_time.hour >= 12 else "am"
    
    url_slug = f"{market_slug}-{month}-{day}-{hour_12}{am_pm}-et"
    return f"https://polymarket.com/event/{url_slug}"


async def fetch_token_ids(slug: str) -> List[dict]:
    """
    Fetch token IDs for a market from Gamma API.
    
    Returns:
        List of dicts with: title, outcomes, token_ids
    """
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(GAMMA_API, params={"slug": slug}, timeout=10) as resp:
                if resp.status != 200:
                    logger.warning(f"Gamma API returned {resp.status} for slug {slug}")
                    return []
                
                data = await resp.json()
                if not data:
                    return []
                
                event = data[0] if isinstance(data, list) else data
                result = []
                
                for m in event.get("markets", []):
                    if not m.get("active", True):
                        continue
                    
                    outcomes = m.get("outcomes", [])
                    if isinstance(outcomes, str):
                        outcomes = orjson.loads(outcomes)
                    
                    token_ids = m.get("clobTokenIds", [])
                    if isinstance(token_ids, str):
                        token_ids = orjson.loads(token_ids)
                    
                    if token_ids:
                        result.append({
                            "title": m.get("question", ""),
                            "outcomes": outcomes,
                            "token_ids": token_ids,
                        })
                
                return result
        except Exception as e:
            logger.warning(f"Error fetching token IDs for {slug}: {e}")
            return []


class PolymarketConsumer(BaseConsumer):
    """
    Consumes Polymarket websocket streams with auto-discovery.
    
    Features:
    - Auto-discovers hourly markets based on Eastern Time
    - Pre-fetches next hour's token IDs before UTC hour boundary
    - Reconnects when market changes
    
    Emits rows with:
    - event_type: "bbo" or "trade"
    - Strict schema fields per event type
    """
    
    def __init__(
        self,
        market_slug: str,
        on_row: Callable[[dict], None],
    ):
        super().__init__(on_row)
        
        self.market_slug = market_slug
        
        # Token tracking
        self._token_ids: List[str] = []
        self._token_ids_set: set = set()
        self._labels: Dict[str, str] = {}
        
        # BBO state per token
        self._best: Dict[str, List[float]] = {}  # [bid_px, bid_sz, ask_px, ask_sz]
        
        # Sequence counters per (token_id, event_type)
        self._seq: Dict[str, int] = {}
        
        # Current market URL
        self._current_market_url: Optional[str] = None
        self._connection_market_url: Optional[str] = None
        
        # Discovery task
        self._discovery_task: Optional[asyncio.Task] = None
    
    def _get_ws_url(self) -> str:
        return WS_URL
    
    async def _on_connect(self, ws: websockets.WebSocketClientProtocol) -> None:
        """Subscribe to current market's tokens."""
        if self._token_ids:
            await ws.send(orjson.dumps({
                "type": "market",
                "assets_ids": self._token_ids,
            }))
            logger.info(
                f"Polymarket: Subscribed to {len(self._token_ids)} tokens "
                f"for {self.market_slug} (market: {self._current_market_url})"
            )
        self._connection_market_url = self._current_market_url
    
    def _get_name(self) -> str:
        return f"Polymarket({self.market_slug})"
    
    async def _discover_current_market(self) -> Optional[str]:
        """Discover the current market URL based on Eastern time."""
        et_time = get_eastern_time()
        return derive_market_url(self.market_slug, et_time)
    
    async def _update_token_ids(self, market_url: str) -> bool:
        """Fetch and update token IDs for a market."""
        slug = parse_polymarket_url(market_url)
        if not slug:
            logger.warning(f"Failed to parse slug from URL: {market_url}")
            return False
        
        logger.info(f"Polymarket: Fetching token IDs for slug: {slug}")
        markets = await fetch_token_ids(slug)
        
        if not markets:
            logger.warning(f"No markets found for slug: {slug}")
            return False
        
        # Use first market
        m = markets[0]
        new_token_ids = m["token_ids"]
        outcomes = m.get("outcomes", [])
        
        if set(new_token_ids) != self._token_ids_set:
            self._token_ids = new_token_ids
            self._token_ids_set = set(new_token_ids)
            self._labels = {
                tid: (outcomes[i] if i < len(outcomes) else f"T{i}")
                for i, tid in enumerate(new_token_ids)
            }
            self._best = {tid: [0.0, 0.0, 0.0, 0.0] for tid in new_token_ids}
            self._seq = {}  # Reset sequences for new tokens
            return True
        
        return False
    
    async def _hourly_discovery_task(self, shutdown_event: asyncio.Event) -> None:
        """Background task to discover and pre-fetch hourly markets."""
        from ..time.boundaries import get_next_hour_boundary_utc, seconds_until_next_hour
        
        while self._running:
            if shutdown_event.is_set():
                break
            
            try:
                # Wait until 1 minute before next UTC hour
                seconds_to_next = seconds_until_next_hour()
                wait_time = max(0, seconds_to_next - 60.0)
                
                if wait_time > 0:
                    await asyncio.sleep(wait_time)
                
                if shutdown_event.is_set() or not self._running:
                    break
                
                # Pre-fetch next hour's market
                next_utc_hour = get_next_hour_boundary_utc()
                et_tz = timezone(timedelta(hours=-5))
                next_et_time = next_utc_hour.astimezone(et_tz)
                next_market_url = derive_market_url(self.market_slug, next_et_time)
                
                if next_market_url != self._current_market_url:
                    logger.info(f"Polymarket: Pre-fetching tokens for next hour: {next_market_url}")
                    await self._update_token_ids(next_market_url)
                
                # Wait until hour boundary
                remaining = seconds_until_next_hour()
                if remaining > 0:
                    await asyncio.sleep(remaining)
                
                if shutdown_event.is_set() or not self._running:
                    break
                
                # Switch to new market
                current_et_time = get_eastern_time()
                actual_market_url = derive_market_url(self.market_slug, current_et_time)
                
                if actual_market_url != self._current_market_url:
                    logger.info(
                        f"Polymarket: Market switched: "
                        f"{self._current_market_url} -> {actual_market_url}"
                    )
                    self._current_market_url = actual_market_url
                    await self._update_token_ids(actual_market_url)
            
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(f"Polymarket: Discovery error: {e}")
                await asyncio.sleep(60)
    
    async def run(self, shutdown_event: Optional[asyncio.Event] = None) -> None:
        """Run with hourly market discovery."""
        # Initial discovery
        self._current_market_url = await self._discover_current_market()
        if self._current_market_url:
            await self._update_token_ids(self._current_market_url)
        
        if not self._token_ids:
            logger.error("Polymarket: Failed to get initial token IDs")
            return
        
        # Start discovery task
        if shutdown_event:
            self._discovery_task = asyncio.create_task(
                self._hourly_discovery_task(shutdown_event)
            )
        
        try:
            await super().run(shutdown_event)
        finally:
            if self._discovery_task:
                self._discovery_task.cancel()
                try:
                    await self._discovery_task
                except asyncio.CancelledError:
                    pass
    
    def _handle_message(self, raw: bytes, recv_ts_ms: int) -> None:
        """Parse and normalize Polymarket message."""
        data = orjson.loads(raw)
        
        if isinstance(data, list):
            for item in data:
                self._process_event(item, recv_ts_ms)
        else:
            self._process_event(data, recv_ts_ms)
    
    def _process_event(self, data: dict, recv_ts_ms: int) -> None:
        """Process a single Polymarket event."""
        event_type = data.get("event_type", "")
        ts = data.get("timestamp")
        exch_ts = int(ts) if ts else recv_ts_ms
        
        if event_type == "book":
            self._handle_book(data, recv_ts_ms, exch_ts)
        elif event_type == "price_change":
            self._handle_price_change(data, recv_ts_ms, exch_ts)
        elif event_type == "last_trade_price":
            self._handle_trade(data, recv_ts_ms, exch_ts)
        elif event_type == "best_bid_ask":
            self._handle_best_bid_ask(data, recv_ts_ms, exch_ts)
    
    def _get_seq(self, token_id: str, event_type: str) -> int:
        """Get and increment sequence number."""
        key = f"{token_id}_{event_type}"
        self._seq[key] = self._seq.get(key, 0) + 1
        return self._seq[key]
    
    def _truncate_token_id(self, token_id: str) -> str:
        """Truncate long token IDs for readability."""
        if len(token_id) > 20:
            return token_id[:20] + "..."
        return token_id
    
    def _emit_bbo(
        self,
        token_id: str,
        recv_ts_ms: int,
        exch_ts: int,
        bid_px: float,
        bid_sz: float,
        ask_px: float,
        ask_sz: float,
    ) -> None:
        """Emit BBO row if changed."""
        if token_id not in self._token_ids_set:
            return
        
        # Check for change
        old = self._best.get(token_id, [0.0, 0.0, 0.0, 0.0])
        if (bid_px == old[0] and bid_sz == old[1] and 
            ask_px == old[2] and ask_sz == old[3]):
            return
        
        self._best[token_id] = [bid_px, bid_sz, ask_px, ask_sz]
        
        row = {
            "venue": "polymarket",
            "stream_id": self.market_slug,
            "event_type": "bbo",
            "ts_event": exch_ts,
            "ts_recv": recv_ts_ms,
            "seq": self._get_seq(token_id, "bbo"),
            "bid_px": bid_px,
            "bid_sz": bid_sz,
            "ask_px": ask_px,
            "ask_sz": ask_sz,
            "token_id": self._truncate_token_id(token_id),
        }
        self._emit_row(row)
    
    def _emit_trade(
        self,
        token_id: str,
        recv_ts_ms: int,
        exch_ts: int,
        price: float,
        size: float,
        side: str,
    ) -> None:
        """Emit trade row."""
        if token_id not in self._token_ids_set:
            return
        
        row = {
            "venue": "polymarket",
            "stream_id": self.market_slug,
            "event_type": "trade",
            "ts_event": exch_ts,
            "ts_recv": recv_ts_ms,
            "seq": self._get_seq(token_id, "trade"),
            "price": price,
            "size": size,
            "side": side.lower() if side else "unknown",
            "token_id": self._truncate_token_id(token_id),
        }
        self._emit_row(row)
    
    def _emit_book(
        self,
        token_id: str,
        recv_ts_ms: int,
        exch_ts: int,
        bids: list,
        asks: list,
        book_hash: str,
    ) -> None:
        """Emit full order book snapshot."""
        if token_id not in self._token_ids_set:
            return
        
        # Polymarket sends best bid/ask LAST in arrays
        # We want best FIRST, so reverse them
        # bids: best = highest price (reverse to get descending)
        # asks: best = lowest price (reverse to get ascending)
        bid_prices = [float(b["price"]) for b in reversed(bids)]
        bid_sizes = [float(b["size"]) for b in reversed(bids)]
        ask_prices = [float(a["price"]) for a in reversed(asks)]
        ask_sizes = [float(a["size"]) for a in reversed(asks)]
        
        row = {
            "venue": "polymarket",
            "stream_id": self.market_slug,
            "event_type": "book",
            "ts_event": exch_ts,
            "ts_recv": recv_ts_ms,
            "seq": self._get_seq(token_id, "book"),
            "token_id": self._truncate_token_id(token_id),
            "bid_prices": bid_prices,
            "bid_sizes": bid_sizes,
            "ask_prices": ask_prices,
            "ask_sizes": ask_sizes,
            "book_hash": book_hash,
        }
        self._emit_row(row)
    
    def _handle_book(self, data: dict, recv_ts_ms: int, exch_ts: int) -> None:
        """Handle book snapshot - emit both full book and BBO."""
        token_id = data.get("asset_id", "")
        if token_id not in self._token_ids_set:
            return
        
        bids = data.get("bids", [])
        asks = data.get("asks", [])
        book_hash = data.get("hash", "")
        
        if bids and asks:
            # Emit full book snapshot
            self._emit_book(
                token_id, recv_ts_ms, exch_ts,
                bids, asks, book_hash,
            )
            
            # Also emit BBO update (best bid/ask are LAST in arrays)
            bb = bids[-1]
            ba = asks[-1]
            self._emit_bbo(
                token_id, recv_ts_ms, exch_ts,
                float(bb["price"]), float(bb["size"]),
                float(ba["price"]), float(ba["size"]),
            )
    
    def _handle_price_change(self, data: dict, recv_ts_ms: int, exch_ts: int) -> None:
        """Handle price change event."""
        changes = data.get("price_changes", [])
        if not changes:
            return
        
        by_asset = {}
        for c in changes:
            token_id = c.get("asset_id", "")
            if token_id not in self._token_ids_set:
                continue
            
            if token_id not in by_asset:
                by_asset[token_id] = {
                    "bb": c.get("best_bid"),
                    "ba": c.get("best_ask"),
                    "lvl": {},
                }
            
            p, s, side = c.get("price"), c.get("size"), c.get("side", "").upper()
            if p and s is not None:
                by_asset[token_id]["lvl"][(side, p)] = float(s)
        
        for token_id, info in by_asset.items():
            old = self._best.get(token_id, [0.0, 0.0, 0.0, 0.0])
            bb_px = float(info["bb"]) if info["bb"] else old[0]
            ba_px = float(info["ba"]) if info["ba"] else old[2]
            bb_sz = info["lvl"].get(("BUY", info["bb"]), old[1] if bb_px == old[0] else 0.0)
            ba_sz = info["lvl"].get(("SELL", info["ba"]), old[3] if ba_px == old[2] else 0.0)
            self._emit_bbo(token_id, recv_ts_ms, exch_ts, bb_px, bb_sz, ba_px, ba_sz)
    
    def _handle_trade(self, data: dict, recv_ts_ms: int, exch_ts: int) -> None:
        """Handle trade event."""
        token_id = data.get("asset_id", "")
        if token_id not in self._token_ids_set:
            return
        
        p, s = data.get("price"), data.get("size")
        if p is not None and s is not None:
            self._emit_trade(
                token_id, recv_ts_ms, exch_ts,
                float(p), float(s), data.get("side", ""),
            )
    
    def _handle_best_bid_ask(self, data: dict, recv_ts_ms: int, exch_ts: int) -> None:
        """Handle best bid/ask update."""
        token_id = data.get("asset_id", "")
        if token_id not in self._token_ids_set:
            return
        
        bb = data.get("best_bid")
        ba = data.get("best_ask")
        
        if bb is not None and ba is not None:
            old = self._best.get(token_id, [0.0, 0.0, 0.0, 0.0])
            self._emit_bbo(
                token_id, recv_ts_ms, exch_ts,
                float(bb), old[1], float(ba), old[3],
            )
