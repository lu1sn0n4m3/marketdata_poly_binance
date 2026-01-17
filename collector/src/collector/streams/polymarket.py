"""Polymarket websocket consumer with auto-discovery."""

import asyncio
import logging
import re
from time import time_ns
from datetime import datetime, timezone, timedelta
from typing import Dict, Optional, Callable, List

import orjson
import websockets
import aiohttp

from ..time.boundaries import get_utc_hour_for_timestamp

logger = logging.getLogger(__name__)

GAMMA_API = "https://gamma-api.polymarket.com/events"
WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"


def parse_polymarket_url(url: str) -> Optional[str]:
    """Extract full slug from Polymarket URL (like the example)."""
    match = re.search(r"/event/([^/?]+)", url)
    return match.group(1) if match else None


def get_eastern_time() -> datetime:
    """
    Get current Eastern Time (ET) from UTC+1 system time.
    
    Note: This assumes the system clock is UTC+1.
    ET is UTC-5 (EST) or UTC-4 (EDT).
    """
    # Get system time (assumed to be UTC+1)
    # We create a timezone-aware datetime representing UTC+1
    system_tz = timezone(timedelta(hours=1))
    system_now = datetime.now(system_tz)
    
    # Convert to UTC
    utc_now = system_now.astimezone(timezone.utc)
    
    # Convert to Eastern Time
    # ET is UTC-5 (EST) or UTC-4 (EDT)
    # For simplicity, we'll use UTC-5 (EST) - daylight saving handling can be added later
    et_tz = timezone(timedelta(hours=-5))
    et_now = utc_now.astimezone(et_tz)
    return et_now


def derive_market_url(market_slug: str, et_time: datetime) -> str:
    """
    Derive Polymarket URL from market slug and Eastern time.
    
    The URL time is the STARTING time of the market (current hour).
    Format: https://polymarket.com/event/{market-slug}-{month}-{day}-{hour}pm-et
    Example: bitcoin-up-or-down-january-17-1pm-et (for 1pm-2pm market)
    """
    month_names = ["january", "february", "march", "april", "may", "june",
                   "july", "august", "september", "october", "november", "december"]
    
    month = month_names[et_time.month - 1]
    day = et_time.day
    # Use current hour as the STARTING time of the market
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


class PolymarketConsumer:
    """Consumes Polymarket websocket streams with auto-discovery."""
    
    def __init__(
        self,
        market_slug: str,
        on_row: Callable[[dict], None],
        on_market_change: Optional[Callable[[str, List[str]], None]] = None,
    ):
        self.market_slug = market_slug
        self.on_row = on_row
        self.on_market_change = on_market_change
        
        self._token_ids: List[str] = []
        self._token_ids_set: set = set()
        self._labels: Dict[str, str] = {}
        self._best: Dict[str, List[float]] = {}  # [bid_px, bid_sz, ask_px, ask_sz]
        self._seq: Dict[str, int] = {}
        
        self._running = False
        self._backoff_seconds = 1.0
        self._max_backoff = 60.0
        self._current_market_url: Optional[str] = None
    
    async def _discover_current_market(self) -> Optional[str]:
        """Discover the current market URL based on Eastern time."""
        et_time = get_eastern_time()
        market_url = derive_market_url(self.market_slug, et_time)
        return market_url
    
    async def _update_token_ids(self, market_url: str) -> bool:
        """Fetch and update token IDs for a market. Returns True if successful."""
        slug = parse_polymarket_url(market_url)
        if not slug:
            logger.warning(f"Failed to parse slug from URL: {market_url}")
            return False
        
        logger.info(f"Fetching token IDs for slug: {slug}")
        markets = await fetch_token_ids(slug)
        if not markets:
            logger.warning(f"No markets found for slug: {slug}")
            return False
        
        # Use first market
        m = markets[0]
        new_token_ids = m["token_ids"]
        outcomes = m.get("outcomes", [])
        
        if set(new_token_ids) != self._token_ids_set:
            # Token IDs changed, update
            self._token_ids = new_token_ids
            self._token_ids_set = set(new_token_ids)
            self._labels = {
                tid: (outcomes[i] if i < len(outcomes) else f"T{i}")
                for i, tid in enumerate(new_token_ids)
            }
            self._best = {tid: [0.0, 0.0, 0.0, 0.0] for tid in new_token_ids}
            self._seq = {
                f"{tid}_bbo": 0 for tid in new_token_ids
            } | {
                f"{tid}_trade": 0 for tid in new_token_ids
            }
            
            if self.on_market_change:
                self.on_market_change(market_url, new_token_ids)
            
            return True
        
        return False
    
    def _normalize_row(self, token_id: str, event_type: str, recv_ts_ms: int, **kwargs) -> dict:
        """Normalize Polymarket event to standard row format."""
        seq_key = f"{token_id}_{event_type}"
        self._seq[seq_key] = self._seq.get(seq_key, 0) + 1
        seq = self._seq[seq_key]
        
        row = {
            "ts_event": kwargs.get("exch_ts", recv_ts_ms),
            "ts_recv": recv_ts_ms,
            "venue": "polymarket",
            "stream_id": self.market_slug,  # Use market slug as stream_id
            "seq": seq,
            "event_type": event_type,
            "token_id": token_id[:20] + "..." if len(token_id) > 20 else token_id,
        }
        
        if event_type == "bbo":
            row.update({
                "bid_px": kwargs["bid_px"],
                "bid_sz": kwargs["bid_sz"],
                "ask_px": kwargs["ask_px"],
                "ask_sz": kwargs["ask_sz"],
            })
        elif event_type == "trade":
            row.update({
                "price": kwargs["price"],
                "size": kwargs["size"],
                "side": kwargs.get("side", "unknown"),
            })
        
        return row
    
    def _bbo(self, token_id: str, recv_ts_ms: int, bid_px: float, bid_sz: float, ask_px: float, ask_sz: float, exch_ts: Optional[int] = None):
        """Handle BBO update."""
        if token_id not in self._token_ids_set:
            return
        
        old = self._best.get(token_id, [0.0, 0.0, 0.0, 0.0])
        if bid_px == old[0] and bid_sz == old[1] and ask_px == old[2] and ask_sz == old[3]:
            return  # No change
        
        self._best[token_id] = [bid_px, bid_sz, ask_px, ask_sz]
        
        row = self._normalize_row(
            token_id, "bbo", recv_ts_ms,
            bid_px=bid_px, bid_sz=bid_sz, ask_px=ask_px, ask_sz=ask_sz,
            exch_ts=exch_ts or recv_ts_ms,
        )
        self.on_row(row)
    
    def _trade(self, token_id: str, recv_ts_ms: int, price: float, size: float, side: str, exch_ts: Optional[int] = None):
        """Handle trade event."""
        if token_id not in self._token_ids_set:
            return
        
        row = self._normalize_row(
            token_id, "trade", recv_ts_ms,
            price=price, size=size, side=side.lower() if side else "unknown",
            exch_ts=exch_ts or recv_ts_ms,
        )
        self.on_row(row)
    
    def _process(self, data: dict, recv_ts_ms: int):
        """Process a websocket message."""
        event_type = data.get("event_type", "")
        ts = data.get("timestamp")
        exch_ts = int(ts) if ts else None
        
        if event_type == "book":
            token_id = data.get("asset_id", "")
            if token_id not in self._token_ids_set:
                return
            bids = data.get("bids", [])
            asks = data.get("asks", [])
            if bids and asks:
                bb = bids[-1]
                ba = asks[-1]
                self._bbo(
                    token_id, recv_ts_ms,
                    float(bb["price"]), float(bb["size"]),
                    float(ba["price"]), float(ba["size"]),
                    exch_ts,
                )
        
        elif event_type == "price_change":
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
                self._bbo(token_id, recv_ts_ms, bb_px, bb_sz, ba_px, ba_sz, exch_ts)
        
        elif event_type == "last_trade_price":
            token_id = data.get("asset_id", "")
            if token_id not in self._token_ids_set:
                return
            p, s = data.get("price"), data.get("size")
            if p is not None and s is not None:
                self._trade(token_id, recv_ts_ms, float(p), float(s), data.get("side", ""), exch_ts)
        
        elif event_type == "best_bid_ask":
            token_id = data.get("asset_id", "")
            if token_id not in self._token_ids_set:
                return
            bb = data.get("best_bid")
            ba = data.get("best_ask")
            if bb is not None and ba is not None:
                old = self._best.get(token_id, [0.0, 0.0, 0.0, 0.0])
                self._bbo(token_id, recv_ts_ms, float(bb), old[1], float(ba), old[3], exch_ts)
    
    def _handle(self, raw: bytes, recv_ts_ms: int):
        """Handle incoming websocket message."""
        data = orjson.loads(raw)
        if isinstance(data, list):
            for item in data:
                self._process(item, recv_ts_ms)
        else:
            self._process(data, recv_ts_ms)
    
    async def run(self, shutdown_event: Optional[asyncio.Event] = None) -> None:
        """Run the consumer with auto-discovery and reconnection."""
        self._running = True
        
        # Initial market discovery
        current_market_url = await self._discover_current_market()
        if not current_market_url:
            logger.error("Failed to discover initial Polymarket market")
            return
        
        await self._update_token_ids(current_market_url)
        self._current_market_url = current_market_url
        
        # Hourly market discovery task
        async def _hourly_discovery():
            while self._running:
                if shutdown_event and shutdown_event.is_set():
                    break
                await asyncio.sleep(3600)  # Check every hour
                if shutdown_event and shutdown_event.is_set():
                    break
                
                new_market_url = await self._discover_current_market()
                if new_market_url != self._current_market_url:
                    logger.info(f"Polymarket market changed: {self._current_market_url} -> {new_market_url}")
                    self._current_market_url = new_market_url
                    await self._update_token_ids(new_market_url)
        
        discovery_task = asyncio.create_task(_hourly_discovery())
        
        try:
            while self._running:
                if shutdown_event and shutdown_event.is_set():
                    break
                
                if not self._token_ids:
                    logger.warning("No token IDs available, waiting...")
                    await asyncio.sleep(5)
                    await self._update_token_ids(self._current_market_url)
                    continue
                
                try:
                    async with websockets.connect(
                        WS_URL,
                        ping_interval=20,
                        ping_timeout=60,
                        max_size=2**20,
                        compression=None,
                    ) as ws:
                        await ws.send(orjson.dumps({"type": "market", "assets_ids": self._token_ids}))
                        logger.info(f"Polymarket connected: {len(self._token_ids)} tokens for {self.market_slug}")
                        self._backoff_seconds = 1.0
                        
                        async for msg in ws:
                            if shutdown_event and shutdown_event.is_set():
                                break
                            if not self._running:
                                break
                            try:
                                self._handle(msg, time_ns() // 1_000_000)
                            except Exception as e:
                                logger.warning(f"Error handling Polymarket message: {e}")
                                continue
                
                except asyncio.CancelledError:
                    break
                except Exception as e:
                    logger.warning(f"Polymarket connection error: {e}, reconnecting in {self._backoff_seconds:.1f}s")
                
                if shutdown_event and shutdown_event.is_set():
                    break
                if not self._running:
                    break
                
                await asyncio.sleep(self._backoff_seconds)
                self._backoff_seconds = min(self._backoff_seconds * 2, self._max_backoff)
        
        finally:
            discovery_task.cancel()
            try:
                await discovery_task
            except asyncio.CancelledError:
                pass
    
    def stop(self):
        """Stop the consumer."""
        self._running = False
