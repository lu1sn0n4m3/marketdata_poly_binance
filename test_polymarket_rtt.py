#!/usr/bin/env python3
"""
Polymarket Order RTT (Round-Trip Time) Tester

Tests latency for order operations on Polymarket.
Run on both local PC and VPS to compare performance.

Usage:
    # Set environment variables
    export POLY_PRIVATE_KEY="0x..."
    export POLY_FUNDER="0x..."
    
    # Run with a market URL or slug
    uv run --with py-clob-client test_polymarket_rtt.py --market "bitcoin-above-100000-on-january-31"
    uv run --with py-clob-client test_polymarket_rtt.py --market "https://polymarket.com/event/bitcoin-above-100000-on-january-31"
    
    # More options
    uv run --with py-clob-client test_polymarket_rtt.py --market "..." -n 10 --test-cancel
    uv run --with py-clob-client test_polymarket_rtt.py --market "..." --outcome NO --price 0.10

What this script does:
    1. Fetches market info from Polymarket API using your slug
    2. Extracts the token ID for YES or NO outcome
    3. Places limit orders at your specified price (won't fill if far from market)
    4. Measures the round-trip time for each API call
    5. Cancels the orders after measuring
    6. Reports statistics: min, max, mean, median, p95, p99, stdev

Enhanced stress test (--stress flag):
    - Subscribes to actual L2 feeds (like production)
    - Measures event loop lag, queue depth, book update processing time
    - Supports multiple markets for burst conditions
    - Tracks correlation between data handler load and order latency
    - Runs for 5-15 minutes to capture real tails and jitter
    - Reports p99 percentiles (not just p95)

Expected output:
    - Warmup: 1 order to establish connection
    - Test: N iterations of place order (+ optional cancel)
    - Stats: latency statistics in milliseconds
    - Typical RTT: 50-200ms depending on location
"""

import os
import sys
import time
import json
import argparse
import statistics
import urllib.request
import urllib.error
import asyncio
import threading
from typing import Dict, Any, Optional, List, Tuple
from dataclasses import dataclass, field


# ============================================================================
# Market Info Fetcher
# ============================================================================

def fetch_market_info(slug_or_url: str) -> Dict[str, Any]:
    """
    Fetch market info from Polymarket gamma API.
    
    Args:
        slug_or_url: Either a market slug or full Polymarket URL
        
    Returns:
        Dict with market info including tokens
    """
    # Extract slug from URL if needed
    slug = slug_or_url.strip().rstrip('/')
    
    # Handle full URLs
    if 'polymarket.com' in slug:
        # Extract slug from URL like https://polymarket.com/event/bitcoin-above-100000
        parts = slug.split('/')
        slug = parts[-1] if parts else slug
        # Remove query params
        if '?' in slug:
            slug = slug.split('?')[0]
    
    print(f"\n[MARKET] Fetching market info...")
    print(f"  Slug: {slug}")
    
    # Try gamma API - first try as event slug, then as market slug
    urls_to_try = [
        f"https://gamma-api.polymarket.com/events?slug={slug}",
        f"https://gamma-api.polymarket.com/markets?slug={slug}",
    ]
    
    data = None
    for url in urls_to_try:
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'polymarket-rtt-test'})
            with urllib.request.urlopen(req, timeout=10) as response:
                data = json.loads(response.read().decode())
                if data:
                    print(f"  Found via: {url.split('?')[0].split('/')[-1]}")
                    break
        except:
            continue
    
    if not data:
        raise ValueError(f"No market/event found with slug: {slug}")
    
    item = data[0] if isinstance(data, list) else data
    
    # If this is an event, get the first market from it
    markets = item.get('markets', [])
    if markets:
        print(f"  Event: {item.get('title', 'Unknown')[:50]}...")
        print(f"  Contains {len(markets)} market(s), using first one")
        market = markets[0]
    else:
        market = item
    
    question = market.get('question', market.get('title', 'Unknown'))
    print(f"  ✅ Market: {question[:60]}...")
    
    # Extract tokens
    tokens = market.get('tokens', [])
    if not tokens:
        # Try clobTokenIds - may be a JSON string or array
        clob_tokens = market.get('clobTokenIds', [])
        outcomes = market.get('outcomes', [])
        
        # Parse outcomes if it's a JSON string
        if isinstance(outcomes, str):
            try:
                outcomes = json.loads(outcomes)
            except:
                outcomes = ['Up', 'Down']  # Default for bitcoin up/down markets
        
        # Parse clobTokenIds if it's a JSON string
        if isinstance(clob_tokens, str):
            try:
                clob_tokens = json.loads(clob_tokens)
            except:
                clob_tokens = []
        
        if clob_tokens and isinstance(clob_tokens, list):
            tokens = [
                {'token_id': tid, 'outcome': outcomes[i] if i < len(outcomes) else f"Outcome {i}"}
                for i, tid in enumerate(clob_tokens)
            ]
    
    if not tokens:
        raise ValueError(f"No tokens found for market: {slug}")
    
    # Display available outcomes
    print(f"  Outcomes:")
    for i, token in enumerate(tokens):
        outcome = token.get('outcome', token.get('name', f'Outcome {i}'))
        token_id = token.get('token_id', token.get('tokenId', 'N/A'))
        price = token.get('price', 'N/A')
        print(f"    [{i}] {outcome}: {token_id[:40]}... (price: {price})")
    
    return {
        'slug': slug,
        'question': question,
        'tokens': tokens,
        'market': market
    }


def get_token_id(market_info: Dict[str, Any], outcome: str = "YES") -> str:
    """
    Get token ID for specified outcome.
    
    Args:
        market_info: Market info from fetch_market_info
        outcome: "YES", "NO", "UP", or "DOWN" (case insensitive)
        
    Returns:
        Token ID string
    """
    tokens = market_info.get('tokens', [])
    outcome_upper = outcome.upper()
    
    # Map YES/NO to UP/DOWN for bitcoin markets
    outcome_aliases = {
        'YES': ['YES', 'UP'],
        'NO': ['NO', 'DOWN'],
        'UP': ['UP', 'YES'],
        'DOWN': ['DOWN', 'NO'],
    }
    
    aliases = outcome_aliases.get(outcome_upper, [outcome_upper])
    
    for token in tokens:
        token_outcome = token.get('outcome', token.get('name', '')).upper()
        for alias in aliases:
            if token_outcome == alias or token_outcome.startswith(alias):
                return token.get('token_id', token.get('tokenId'))
    
    # Fallback: YES/UP = first token, NO/DOWN = second token
    if outcome_upper in ['YES', 'UP'] and len(tokens) > 0:
        return tokens[0].get('token_id', tokens[0].get('tokenId'))
    elif outcome_upper in ['NO', 'DOWN'] and len(tokens) > 1:
        return tokens[1].get('token_id', tokens[1].get('tokenId'))
    
    raise ValueError(f"Could not find token for outcome: {outcome}")


# ============================================================================
# PolymarketOrderExecutor
# ============================================================================

class PolymarketOrderExecutor:
    """Execute orders on Polymarket using py-clob-client library."""
    
    def __init__(self, private_key: str, funder: str = None, signature_type: int = 1):
        if not private_key:
            raise ValueError("Wallet private key is required")
        
        self.private_key = private_key.strip()
        if not self.private_key.startswith("0x"):
            self.private_key = "0x" + self.private_key
        
        self.funder = funder.strip() if funder else None
        self.signature_type = signature_type
        self.client = None
        self._init_client()
    
    def _init_client(self):
        """Initialize the py-clob-client."""
        try:
            from py_clob_client.client import ClobClient
            
            host = "https://clob.polymarket.com"
            chain_id = 137
            
            print(f"\n[CLIENT] Initializing Polymarket client...")
            print(f"  Host: {host}")
            print(f"  Chain ID: {chain_id}")
            if self.funder:
                print(f"  Funder: {self.funder[:20]}...")
            print(f"  Signature Type: {self.signature_type}")
            
            self.client = ClobClient(
                host=host,
                key=self.private_key,
                chain_id=chain_id,
                funder=self.funder,
                signature_type=self.signature_type
            )
            
            print(f"  ✅ Client initialized")
            
            try:
                self.client.set_api_creds(self.client.create_or_derive_api_creds())
                print(f"  ✅ API credentials derived")
            except Exception as e:
                print(f"  ⚠️  Could not derive API creds: {e}")
            
        except ImportError as e:
            raise ValueError(f"py-clob-client not installed: {e}")
        except Exception as e:
            raise ValueError(f"Failed to initialize client: {e}")
    
    def place_order(self, token_id: str, side: str, size: float, price: float,
                   quiet: bool = False) -> Dict[str, Any]:
        """Place an order on Polymarket."""
        if not self.client:
            return {"success": False, "error": "Client not initialized"}
        
        if not quiet:
            print(f"[ORDER] {side} {size} @ {price}")
        
        try:
            from py_clob_client.clob_types import OrderArgs, OrderType
            from py_clob_client.order_builder.constants import BUY, SELL
            
            order_side = BUY if side.upper() == "BUY" else SELL
            
            order_args = OrderArgs(
                token_id=token_id,
                price=float(price),
                size=float(size),
                side=order_side
            )
            
            signed_order = self.client.create_order(order_args)
            response = self.client.post_order(signed_order, OrderType.GTC)
            
            return {"success": True, "data": response}
            
        except Exception as e:
            return {"success": False, "error": str(e)}
    
    def cancel_order(self, order_id: str, quiet: bool = False) -> Dict[str, Any]:
        """Cancel an order by ID."""
        if not self.client:
            return {"success": False, "error": "Client not initialized"}
        
        if not quiet:
            print(f"[CANCEL] {order_id[:20]}...")
        
        try:
            response = self.client.cancel_orders([order_id])
            return {"success": True, "data": response}
        except Exception as e:
            return {"success": False, "error": str(e)}
    
    def cancel_all(self, quiet: bool = False) -> Dict[str, Any]:
        """Cancel all open orders."""
        if not self.client:
            return {"success": False, "error": "Client not initialized"}
        
        if not quiet:
            print("[CANCEL ALL]")
        
        try:
            response = self.client.cancel_all()
            return {"success": True, "data": response}
        except Exception:
            try:
                orders = self.client.get_orders()
                if orders:
                    order_ids = [o.get('id') or o.get('orderID') for o in orders if o]
                    order_ids = [oid for oid in order_ids if oid]
                    if order_ids:
                        return {"success": True, "data": self.client.cancel_orders(order_ids)}
                return {"success": True, "data": "No orders"}
            except Exception as e2:
                return {"success": False, "error": str(e2)}


# ============================================================================
# RTT Testing
# ============================================================================

@dataclass
class RTTResult:
    """Single RTT measurement result."""
    operation: str
    latency_ms: float
    success: bool
    error: Optional[str] = None
    order_id: Optional[str] = None


class RTTTester:
    """Test round-trip time for Polymarket operations."""
    
    def __init__(self, executor: PolymarketOrderExecutor, token_id: str, 
                 price: float = 0.50, size: float = 5):
        self.executor = executor
        self.results: List[RTTResult] = []
        self.token_id = token_id
        self.price = price
        self.size = size
        
        print(f"\n[CONFIG] Test parameters:")
        print(f"  Token: {token_id[:40]}...")
        print(f"  Price: {price} (order at this price)")
        print(f"  Size: {size} contracts")
    
    def measure_place_order(self) -> RTTResult:
        """Measure RTT for placing a limit order."""
        start = time.perf_counter()
        result = self.executor.place_order(
            token_id=self.token_id,
            side="BUY",
            size=self.size,
            price=self.price,
            quiet=True
        )
        end = time.perf_counter()
        
        latency_ms = (end - start) * 1000
        
        rtt = RTTResult(
            operation="place_order",
            latency_ms=latency_ms,
            success=result["success"],
            error=result.get("error")
        )
        
        if result["success"]:
            order_data = result.get("data", {})
            rtt.order_id = order_data.get("id") or order_data.get("orderID")
        
        self.results.append(rtt)
        return rtt
    
    def measure_cancel_order(self, order_id: str) -> RTTResult:
        """Measure RTT for cancelling an order."""
        start = time.perf_counter()
        result = self.executor.cancel_order(order_id, quiet=True)
        end = time.perf_counter()
        
        latency_ms = (end - start) * 1000
        
        rtt = RTTResult(
            operation="cancel_order",
            latency_ms=latency_ms,
            success=result["success"],
            error=result.get("error")
        )
        self.results.append(rtt)
        return rtt
    
    def run_test(self, iterations: int = 5, test_cancel: bool = False, 
                 warmup: int = 1) -> Dict[str, Any]:
        """Run the full RTT test suite."""
        
        print(f"\n{'='*60}")
        print("POLYMARKET RTT TEST")
        print(f"{'='*60}")
        print(f"  Iterations: {iterations}")
        print(f"  Test cancel: {test_cancel}")
        print(f"  Warmup: {warmup}")
        
        # Warmup
        if warmup > 0:
            print(f"\n[WARMUP] {warmup} iteration(s)...")
            for i in range(warmup):
                try:
                    rtt = self.measure_place_order()
                    status = "✓" if rtt.success else "✗"
                    print(f"  {i+1}: {rtt.latency_ms:.0f}ms {status}")
                    if rtt.success and rtt.order_id:
                        self.measure_cancel_order(rtt.order_id)
                except Exception as e:
                    print(f"  {i+1}: Failed - {e}")
            
            self.results.clear()
            time.sleep(0.2)
        
        # Test
        place_times = []
        cancel_times = []
        
        print(f"\n[TEST] {iterations} iteration(s)...")
        for i in range(iterations):
            try:
                rtt = self.measure_place_order()
                status = "✓" if rtt.success else "✗"
                
                if rtt.success:
                    place_times.append(rtt.latency_ms)
                    msg = f"  {i+1:2}/{iterations}: PLACE {rtt.latency_ms:6.1f}ms {status}"
                    
                    if test_cancel and rtt.order_id:
                        cancel_rtt = self.measure_cancel_order(rtt.order_id)
                        cancel_status = "✓" if cancel_rtt.success else "✗"
                        if cancel_rtt.success:
                            cancel_times.append(cancel_rtt.latency_ms)
                        msg += f"  CANCEL {cancel_rtt.latency_ms:6.1f}ms {cancel_status}"
                    
                    print(msg)
                else:
                    print(f"  {i+1:2}/{iterations}: FAILED - {rtt.error}")
                
                if i < iterations - 1:
                    time.sleep(0.05)
                    
            except Exception as e:
                print(f"  {i+1:2}/{iterations}: ERROR - {e}")
        
        # Cleanup
        print("\n[CLEANUP] Cancelling remaining orders...")
        self.executor.cancel_all(quiet=True)
        
        return self._compute_stats(place_times, cancel_times)
    
    def _compute_stats(self, place_times: List[float], 
                       cancel_times: List[float]) -> Dict[str, Any]:
        """Compute and print statistics."""
        stats = {}
        
        print(f"\n{'='*60}")
        print("RESULTS")
        print(f"{'='*60}")
        
        if place_times:
            stats['place_order'] = {
                'count': len(place_times),
                'min_ms': min(place_times),
                'max_ms': max(place_times),
                'mean_ms': statistics.mean(place_times),
                'median_ms': statistics.median(place_times),
                'stdev_ms': statistics.stdev(place_times) if len(place_times) > 1 else 0,
            }
            
            s = stats['place_order']
            print(f"\nPLACE ORDER (n={s['count']}):")
            print(f"  Min:    {s['min_ms']:8.1f} ms")
            print(f"  Max:    {s['max_ms']:8.1f} ms")
            print(f"  Mean:   {s['mean_ms']:8.1f} ms")
            print(f"  Median: {s['median_ms']:8.1f} ms")
            print(f"  StdDev: {s['stdev_ms']:8.1f} ms")
        else:
            print("\n❌ No successful place_order measurements")
        
        if cancel_times:
            stats['cancel_order'] = {
                'count': len(cancel_times),
                'min_ms': min(cancel_times),
                'max_ms': max(cancel_times),
                'mean_ms': statistics.mean(cancel_times),
                'median_ms': statistics.median(cancel_times),
                'stdev_ms': statistics.stdev(cancel_times) if len(cancel_times) > 1 else 0,
            }
            
            s = stats['cancel_order']
            print(f"\nCANCEL ORDER (n={s['count']}):")
            print(f"  Min:    {s['min_ms']:8.1f} ms")
            print(f"  Max:    {s['max_ms']:8.1f} ms")
            print(f"  Mean:   {s['mean_ms']:8.1f} ms")
            print(f"  Median: {s['median_ms']:8.1f} ms")
            print(f"  StdDev: {s['stdev_ms']:8.1f} ms")
        
        print(f"\n{'='*60}")
        
        if place_times:
            print(f"\n📊 PLACE ORDER median = {statistics.median(place_times):.0f}ms")
        if cancel_times:
            print(f"📊 CANCEL ORDER median = {statistics.median(cancel_times):.0f}ms")
        
        return stats


# ============================================================================
# WebSocket RTT Tester - measures order-to-book visibility
# ============================================================================

class WebSocketRTTTester:
    """
    Measure true end-to-end latency by timing how long until your order 
    appears in the WebSocket book feed.
    
    Flow:
    1. Connect to book WebSocket (same as polymarket.py uses)
    2. Place order at T1
    3. Wait for order to appear in book/price_change update at T2
    4. T2 - T1 = true round-trip latency
    """
    
    WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
    
    def __init__(self, executor: PolymarketOrderExecutor, token_id: str,
                 price: float = 0.02, size: float = 100):
        self.executor = executor
        self.token_id = token_id
        self.base_price = price
        self.size = size
        
        self._order_placed_time: Optional[float] = None
        self._order_seen_time: Optional[float] = None
        self._target_price: Optional[float] = None
        self._target_order_id: Optional[str] = None
        self._ws_connected = threading.Event()
        self._order_seen = threading.Event()
        self._stop_ws = threading.Event()
        self._ws = None
        self._loop = None
        self._message_count = 0
        self._last_event_types: List[str] = []
    
    async def _ws_listener(self):
        """WebSocket listener that watches for our order in book updates."""
        try:
            import websockets
        except ImportError:
            print("  ❌ websockets not installed")
            print("  Run: uv run --with py-clob-client,websockets ...")
            return
        
        try:
            async with websockets.connect(self.WS_URL) as ws:
                self._ws = ws
                
                # Subscribe using the CORRECT format from polymarket.py
                # {"type": "market", "assets_ids": [token_id]}
                sub_msg = json.dumps({
                    "type": "market",
                    "assets_ids": [self.token_id]
                })
                await ws.send(sub_msg)
                
                self._ws_connected.set()
                
                while not self._stop_ws.is_set():
                    try:
                        msg = await asyncio.wait_for(ws.recv(), timeout=0.5)
                        self._message_count += 1
                        
                        data = json.loads(msg)
                        
                        # Handle array of events
                        if isinstance(data, list):
                            for item in data:
                                if self._check_for_order(item):
                                    self._order_seen_time = time.perf_counter()
                                    self._order_seen.set()
                        else:
                            if self._check_for_order(data):
                                self._order_seen_time = time.perf_counter()
                                self._order_seen.set()
                            
                    except asyncio.TimeoutError:
                        continue
                    except json.JSONDecodeError:
                        continue
                    except Exception as e:
                        if not self._stop_ws.is_set():
                            print(f"  WS error: {e}")
                        break
                        
        except Exception as e:
            if not self._stop_ws.is_set():
                print(f"  WS connection error: {e}")
    
    def _check_for_order(self, data: dict) -> bool:
        """Check if our order appears in the book update."""
        if not self._target_price:
            return False
        
        event_type = data.get("event_type", "")
        asset_id = data.get("asset_id", "")
        
        # Track event types for debugging
        if event_type and event_type not in self._last_event_types[-5:]:
            self._last_event_types.append(event_type)
            self._last_event_types = self._last_event_types[-10:]
        
        # Only check events for our token
        if asset_id and asset_id != self.token_id:
            return False
        
        target_price_str = f"{self._target_price:.2f}"
        
        # Check "book" events - full book snapshot
        if event_type == "book":
            bids = data.get("bids", [])
            for bid in bids:
                if isinstance(bid, dict):
                    price = bid.get("price", "")
                    if f"{float(price):.2f}" == target_price_str:
                        return True
        
        # Check "price_change" events - incremental updates
        elif event_type == "price_change":
            changes = data.get("price_changes", [])
            for c in changes:
                if c.get("asset_id") == self.token_id:
                    price = c.get("price", "")
                    side = c.get("side", "").upper()
                    if side == "BUY" and price:
                        if f"{float(price):.2f}" == target_price_str:
                            return True
        
        # Check bids directly (some messages have bids at top level)
        bids = data.get("bids", [])
        for bid in bids:
            price = None
            if isinstance(bid, dict):
                price = bid.get("price")
            elif isinstance(bid, (list, tuple)) and len(bid) >= 1:
                price = bid[0]
            
            if price is not None:
                try:
                    if f"{float(price):.2f}" == target_price_str:
                        return True
                except (ValueError, TypeError):
                    pass
        
        return False
    
    def _run_ws_in_thread(self):
        """Run WebSocket listener in a separate thread."""
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._ws_listener())
        except Exception:
            pass
        finally:
            self._loop.close()
    
    def measure_ws_rtt(self, iteration: int = 0) -> Optional[RTTResult]:
        """
        Measure order-to-book visibility latency.
        
        Returns RTTResult with the time from order placement to seeing it in the book.
        """
        # Use a unique price for each iteration to avoid confusion
        # Start at a low price like 0.02 and increment
        self._target_price = round(self.base_price + (iteration * 0.01), 2)
        self._order_seen.clear()
        self._order_seen_time = None
        
        # Place the order and record time
        self._order_placed_time = time.perf_counter()
        result = self.executor.place_order(
            token_id=self.token_id,
            side="BUY",
            size=self.size,
            price=self._target_price,
            quiet=True
        )
        api_time = time.perf_counter()
        
        if not result["success"]:
            return RTTResult(
                operation="ws_rtt",
                latency_ms=0,
                success=False,
                error=result.get("error")
            )
        
        order_data = result.get("data", {})
        self._target_order_id = order_data.get("id") or order_data.get("orderID")
        
        api_latency = (api_time - self._order_placed_time) * 1000
        
        # Wait for order to appear in WebSocket (timeout 3s)
        seen = self._order_seen.wait(timeout=3.0)
        
        if seen and self._order_seen_time:
            ws_latency = (self._order_seen_time - self._order_placed_time) * 1000
            return RTTResult(
                operation="ws_rtt",
                latency_ms=ws_latency,
                success=True,
                order_id=self._target_order_id
            )
        else:
            # Didn't see order in book, return API latency only
            return RTTResult(
                operation="ws_rtt",
                latency_ms=api_latency,
                success=True,
                error="not in book",
                order_id=self._target_order_id
            )
    
    def run_test(self, iterations: int = 5, warmup: int = 1) -> Dict[str, Any]:
        """Run WebSocket RTT test."""
        print(f"\n{'='*60}")
        print("WEBSOCKET RTT TEST (Order-to-Book Visibility)")
        print(f"{'='*60}")
        print(f"  Token: {self.token_id[:40]}...")
        print(f"  Base price: {self.base_price}")
        print(f"  Size: {self.size}")
        print(f"  Iterations: {iterations}")
        
        # Start WebSocket listener in background
        print(f"\n[WS] Connecting to WebSocket...")
        ws_thread = threading.Thread(target=self._run_ws_in_thread, daemon=True)
        ws_thread.start()
        
        # Wait for connection
        if not self._ws_connected.wait(timeout=10):
            print("  ❌ WebSocket connection timeout")
            return {}
        print("  ✅ Connected and subscribed")
        
        # Wait a moment for initial book snapshot
        time.sleep(1.0)
        print(f"  Messages received: {self._message_count}")
        if self._last_event_types:
            print(f"  Event types seen: {', '.join(self._last_event_types)}")
        
        # Warmup
        if warmup > 0:
            print(f"\n[WARMUP] {warmup} iteration(s)...")
            for i in range(warmup):
                rtt = self.measure_ws_rtt(iteration=i)
                if rtt:
                    status = "✓" if rtt.success and not rtt.error else "~"
                    note = "" if not rtt.error else f" ({rtt.error})"
                    print(f"  {i+1}: {rtt.latency_ms:.0f}ms {status}{note}")
                    if rtt.order_id:
                        self.executor.cancel_order(rtt.order_id, quiet=True)
                time.sleep(0.3)
        
        # Test
        ws_times = []
        api_times = []
        seen_in_book = 0
        
        print(f"\n[TEST] {iterations} iteration(s)...")
        
        for i in range(iterations):
            rtt = self.measure_ws_rtt(iteration=warmup + i)
            if rtt and rtt.success:
                if rtt.error:  # API only
                    api_times.append(rtt.latency_ms)
                    print(f"  {i+1:2}/{iterations}: API {rtt.latency_ms:6.1f}ms")
                else:  # Seen in book
                    ws_times.append(rtt.latency_ms)
                    seen_in_book += 1
                    print(f"  {i+1:2}/{iterations}: WS  {rtt.latency_ms:6.1f}ms ✓ (seen in book)")
                
                if rtt.order_id:
                    self.executor.cancel_order(rtt.order_id, quiet=True)
            else:
                print(f"  {i+1:2}/{iterations}: FAILED - {rtt.error if rtt else 'unknown'}")
            
            time.sleep(0.15)
        
        # Stop WebSocket
        self._stop_ws.set()
        
        # Cleanup
        print("\n[CLEANUP]...")
        self.executor.cancel_all(quiet=True)
        
        print(f"\n  Total WS messages: {self._message_count}")
        print(f"  Orders seen in book: {seen_in_book}/{iterations}")
        
        # Stats
        return self._compute_stats(ws_times, api_times)
    
    def _compute_stats(self, ws_times: List[float], api_times: List[float]) -> Dict[str, Any]:
        """Compute and print statistics."""
        print(f"\n{'='*60}")
        print("RESULTS")
        print(f"{'='*60}")
        
        stats = {}
        
        # WebSocket RTT (order seen in book)
        if ws_times:
            stats['ws_rtt'] = {
                'count': len(ws_times),
                'min_ms': min(ws_times),
                'max_ms': max(ws_times),
                'mean_ms': statistics.mean(ws_times),
                'median_ms': statistics.median(ws_times),
                'stdev_ms': statistics.stdev(ws_times) if len(ws_times) > 1 else 0,
            }
            
            s = stats['ws_rtt']
            print(f"\nORDER-TO-BOOK RTT (n={s['count']}):")
            print(f"  Min:    {s['min_ms']:8.1f} ms")
            print(f"  Max:    {s['max_ms']:8.1f} ms")
            print(f"  Mean:   {s['mean_ms']:8.1f} ms")
            print(f"  Median: {s['median_ms']:8.1f} ms")
            print(f"  StdDev: {s['stdev_ms']:8.1f} ms")
        
        # API RTT (order not seen, fallback)
        if api_times:
            stats['api_rtt'] = {
                'count': len(api_times),
                'min_ms': min(api_times),
                'max_ms': max(api_times),
                'mean_ms': statistics.mean(api_times),
                'median_ms': statistics.median(api_times),
                'stdev_ms': statistics.stdev(api_times) if len(api_times) > 1 else 0,
            }
            
            s = stats['api_rtt']
            print(f"\nAPI-ONLY RTT (order not seen in book, n={s['count']}):")
            print(f"  Min:    {s['min_ms']:8.1f} ms")
            print(f"  Max:    {s['max_ms']:8.1f} ms")
            print(f"  Mean:   {s['mean_ms']:8.1f} ms")
            print(f"  Median: {s['median_ms']:8.1f} ms")
            print(f"  StdDev: {s['stdev_ms']:8.1f} ms")
        
        if not ws_times and not api_times:
            print("\n❌ No successful measurements")
            return {}
        
        print(f"\n{'='*60}")
        
        if ws_times:
            print(f"\n📊 ORDER-TO-BOOK median = {statistics.median(ws_times):.0f}ms")
        if api_times:
            print(f"📊 API-ONLY median = {statistics.median(api_times):.0f}ms")
        
        return stats


# ============================================================================
# Enhanced Market Making Stress Test with L2 Feed Monitoring
# ============================================================================

class EnhancedMarketMakingStressTest:
    """
    Enhanced market-making stress test with comprehensive latency monitoring.
    
    Features:
    - Subscribes to actual L2 feeds (like production)
    - Measures event loop lag / queue depth / time-to-apply-book-update
    - Supports multiple markets for burst conditions
    - Tracks correlation between data handler load and order latency
    - Runs for 5-15 minutes to capture real tails and jitter
    - Reports p99 percentiles (not just p95)
    """
    
    WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
    
    def __init__(self, executor: PolymarketOrderExecutor, token_ids: List[str],
                 base_price: float = 0.02, size: float = 10, max_open: int = 5):
        self.executor = executor
        self.token_ids = token_ids if isinstance(token_ids, list) else [token_ids]
        self.base_price = base_price
        self.size = size
        self.max_open = max_open
        
        # Order tracking
        self.open_orders: Dict[str, float] = {}  # order_id -> placed_time
        self.place_latencies: List[float] = []
        self.cancel_latencies: List[float] = []
        self.place_errors: List[str] = []
        self.cancel_errors: List[str] = []
        
        # Order latency with context (for correlation analysis)
        self.place_latencies_with_context: List[Dict[str, Any]] = []  # latency, queue_depth, loop_lag, etc.
        
        # WebSocket / L2 feed metrics
        self.ws_messages = 0
        self.ws_book_updates = 0
        self.ws_trades = 0
        self.ws_price_changes = 0
        
        # Event loop lag tracking
        # Time when message arrives vs when it's processed
        self.event_loop_lags: List[float] = []  # ms
        
        # Queue depth tracking (messages waiting to be processed)
        self.queue_depths: List[int] = []
        self.max_queue_depth = 0
        
        # Book update processing time
        # Time from receiving book update to applying it
        self.book_update_times: List[float] = []  # ms
        
        # Message queue for async processing
        self._message_queue: asyncio.Queue = None
        self._queue_depth_samples: List[Tuple[float, int]] = []  # (timestamp, depth)
        
        # Control
        self._stop: Optional[asyncio.Event] = None  # Will be initialized in run_test
        self._ws_connected: Optional[asyncio.Event] = None  # Will be initialized in run_test
        self._lock = threading.Lock()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
    
    async def _ws_listener(self):
        """WebSocket listener for L2 market data with latency tracking."""
        if not self._stop:
            return
        
        try:
            import websockets
        except ImportError:
            print("  ❌ websockets not installed")
            return
        
        try:
            async with websockets.connect(self.WS_URL) as ws:
                # Subscribe to all token IDs (multiple markets for burst conditions)
                sub_msg = json.dumps({
                    "type": "market",
                    "assets_ids": self.token_ids
                })
                await ws.send(sub_msg)
                if self._ws_connected:
                    self._ws_connected.set()
                print(f"  ✅ Subscribed to {len(self.token_ids)} token(s)")
                
                while not self._stop.is_set():
                    try:
                        # Record time when message arrives
                        recv_start = time.perf_counter()
                        msg = await asyncio.wait_for(ws.recv(), timeout=0.1)
                        recv_end = time.perf_counter()
                        
                        # Calculate event loop lag (time to receive)
                        # This measures how long we waited for the message
                        # If queue is backed up, this will be higher
                        loop_lag = (recv_end - recv_start) * 1000
                        
                        # Put message in queue for processing
                        await self._message_queue.put((msg, recv_end, loop_lag))
                        
                    except asyncio.TimeoutError:
                        continue
                    except Exception as e:
                        if not self._stop.is_set():
                            print(f"  WS error: {e}")
                        break
        except Exception as e:
            if not self._stop.is_set():
                print(f"  WS connection error: {e}")
    
    async def _message_processor(self):
        """Process messages from queue and measure processing latency."""
        while not self._stop.is_set():
            try:
                # Get message with timeout
                msg, recv_time, loop_lag = await asyncio.wait_for(
                    self._message_queue.get(), timeout=0.1
                )
                
                # Record queue depth before processing
                queue_depth = self._message_queue.qsize()
                process_start = time.perf_counter()
                
                # Track queue depth
                with self._lock:
                    self.queue_depths.append(queue_depth)
                    self.max_queue_depth = max(self.max_queue_depth, queue_depth)
                    self._queue_depth_samples.append((process_start, queue_depth))
                    # Keep only last 1000 samples
                    if len(self._queue_depth_samples) > 1000:
                        self._queue_depth_samples.pop(0)
                
                # Process message
                self._process_ws_message(msg, recv_time, process_start, loop_lag)
                
                # Mark task as done
                self._message_queue.task_done()
                
            except asyncio.TimeoutError:
                continue
            except Exception as e:
                if not self._stop.is_set():
                    print(f"  Message processor error: {e}")
    
    def _process_ws_message(self, msg: str, recv_time: float, process_start: float, loop_lag: float):
        """Process incoming WebSocket message and measure book update time."""
        try:
            data = json.loads(msg)
            
            # Calculate event loop lag (time from recv to processing start)
            event_loop_lag = (process_start - recv_time) * 1000
            with self._lock:
                self.event_loop_lags.append(event_loop_lag)
            
            # Handle array of events
            events = data if isinstance(data, list) else [data]
            
            for event in events:
                self.ws_messages += 1
                event_type = event.get("event_type", "")
                
                # Measure book update processing time
                if event_type == "book":
                    book_start = time.perf_counter()
                    self.ws_book_updates += 1
                    
                    # Simulate book update processing (like in production)
                    # In real code, this would update internal book state
                    token_id = event.get("asset_id", "")
                    bids = event.get("bids", [])
                    asks = event.get("asks", [])
                    
                    # Process book (simulate applying update)
                    if bids and asks:
                        # This is where you'd update your internal book state
                        # For now, we just measure the time
                        pass
                    
                    book_end = time.perf_counter()
                    book_update_time = (book_end - book_start) * 1000
                    
                    with self._lock:
                        self.book_update_times.append(book_update_time)
                        
                elif event_type == "last_trade_price":
                    self.ws_trades += 1
                elif event_type == "price_change":
                    self.ws_price_changes += 1
                    
        except Exception:
            pass
    
    def place_order(self, price: float, token_id: str) -> Optional[str]:
        """Place an order and track latency with context (queue depth, loop lag)."""
        # Capture context before placing order
        try:
            queue_depth = self._message_queue.qsize() if self._message_queue else 0
        except (AttributeError, RuntimeError):
            queue_depth = 0
        
        # Get recent loop lag (average of last 10)
        with self._lock:
            recent_loop_lags = self.event_loop_lags[-10:] if self.event_loop_lags else []
            avg_loop_lag = statistics.mean(recent_loop_lags) if recent_loop_lags else 0.0
        
        start = time.perf_counter()
        result = self.executor.place_order(
            token_id=token_id,
            side="BUY",
            size=self.size,
            price=price,
            quiet=True
        )
        latency = (time.perf_counter() - start) * 1000
        
        if result["success"]:
            order_data = result.get("data", {})
            order_id = order_data.get("id") or order_data.get("orderID")
            
            with self._lock:
                self.place_latencies.append(latency)
                self.place_latencies_with_context.append({
                    'latency_ms': latency,
                    'queue_depth': queue_depth,
                    'loop_lag_ms': avg_loop_lag,
                    'timestamp': time.perf_counter(),
                })
                if order_id:
                    self.open_orders[order_id] = time.perf_counter()
            
            return order_id
        else:
            error_msg = result.get("error", "unknown")
            # Ensure we have a meaningful error message
            if not error_msg or error_msg == "unknown":
                error_msg = f"Unknown error (result: {result})"
            else:
                error_msg = str(error_msg)
            
            with self._lock:
                self.place_errors.append(error_msg)
            # Print error in real-time (truncate if too long)
            error_display = error_msg[:200]
            if len(error_msg) > 200:
                error_display += "..."
            print(f"  ⚠️  PLACE ERROR: {error_display}")
            return None
    
    def cancel_order(self, order_id: str) -> bool:
        """Cancel an order and track latency."""
        start = time.perf_counter()
        result = self.executor.cancel_order(order_id, quiet=True)
        latency = (time.perf_counter() - start) * 1000
        
        with self._lock:
            if order_id in self.open_orders:
                del self.open_orders[order_id]
        
        if result["success"]:
            with self._lock:
                self.cancel_latencies.append(latency)
            return True
        else:
            error_msg = result.get("error", "unknown")
            # Ensure we have a meaningful error message
            if not error_msg or error_msg == "unknown":
                error_msg = f"Unknown error (result: {result})"
            else:
                error_msg = str(error_msg)
            
            with self._lock:
                self.cancel_errors.append(error_msg)
            # Print error in real-time (truncate if too long)
            error_display = error_msg[:200]
            if len(error_msg) > 200:
                error_display += "..."
            print(f"  ⚠️  CANCEL ERROR: {error_display}")
            return False
    
    async def _run_test_async(self, duration_seconds: float = 600, target_rate: float = 5) -> Dict[str, Any]:
        """
        Run enhanced market-making stress test with L2 feed monitoring.
        
        Args:
            duration_seconds: How long to run the test (default: 600 = 10 minutes)
            target_rate: Target orders per second (place+cancel cycles)
        """
        print(f"\n{'='*60}")
        print("ENHANCED MARKET MAKING STRESS TEST")
        print(f"{'='*60}")
        print(f"  Tokens: {len(self.token_ids)} market(s)")
        for i, tid in enumerate(self.token_ids[:3]):  # Show first 3
            print(f"    [{i+1}] {tid[:40]}...")
        if len(self.token_ids) > 3:
            print(f"    ... and {len(self.token_ids) - 3} more")
        order_value = self.base_price * self.size
        print(f"  Base price: {self.base_price}")
        print(f"  Size per order: {self.size}")
        print(f"  Order value: ${order_value:.2f} per order (price * size)")
        print(f"  Max concurrent orders: {self.max_open}")
        print(f"  Duration: {duration_seconds}s ({duration_seconds/60:.1f} minutes)")
        print(f"  Target rate: {target_rate} orders/sec")
        print(f"  ⚠️  Note: Strictly sequential (place -> cancel -> wait 0.3s -> next) to avoid balance issues")
        
        # Initialize async events and message queue
        self._stop = asyncio.Event()
        self._ws_connected = asyncio.Event()
        self._message_queue = asyncio.Queue()
        
        # Start WebSocket listener
        print(f"\n[WS] Connecting to L2 feed...")
        ws_task = asyncio.create_task(self._ws_listener())
        processor_task = asyncio.create_task(self._message_processor())
        
        # Wait for connection
        try:
            await asyncio.wait_for(self._ws_connected.wait(), timeout=10)
        except asyncio.TimeoutError:
            print("  ❌ WebSocket connection timeout")
            self._stop.set()
            return {}
        print("  ✅ Connected and subscribed")
        
        # Let WS stabilize
        await asyncio.sleep(2.0)
        initial_ws_msgs = self.ws_messages
        
        # Run test
        print(f"\n[TEST] Running for {duration_seconds}s...")
        print("  Format: [elapsed] [placed/cancelled] place_ms | cancel_ms | queue | loop_lag | open_orders | ws_msgs")
        
        start_time = time.perf_counter()
        cycle_count = 0
        price_offset = 0
        interval = 1.0 / target_rate
        last_print = start_time
        
        # Rotate through token IDs for burst conditions
        token_idx = 0
        
        while (time.perf_counter() - start_time) < duration_seconds:
            cycle_start = time.perf_counter()
            
            # Select token (rotate through all tokens)
            token_id = self.token_ids[token_idx % len(self.token_ids)]
            token_idx += 1
            
            # Calculate unique price for this order
            price = round(self.base_price + (price_offset * 0.01), 2)
            price_offset = (price_offset + 1) % 20  # Cycle through 20 price levels
            
            # STRICTLY SEQUENTIAL: place -> wait for cancel -> wait for balance release -> next
            # This ensures we don't hit balance limits
            
            # Place order (this runs in thread pool to not block event loop)
            loop = asyncio.get_event_loop()
            order_id = await loop.run_in_executor(
                None, 
                lambda: self.place_order(price, token_id)
            )
            
            if order_id:
                # Cancel the order and wait for it to complete
                cancel_success = await loop.run_in_executor(None, lambda: self.cancel_order(order_id))
                if cancel_success:
                    cycle_count += 1
                # IMPORTANT: Wait for Polymarket to release the balance after cancel
                # This is separate from the cancel API returning - balance release takes time
                await asyncio.sleep(0.3)
            else:
                # Order placement failed - likely balance issue
                # Wait longer to let balance recover from previous cancellations
                await asyncio.sleep(1.0)
            
            # Print progress every 5 seconds
            now = time.perf_counter()
            if now - last_print >= 5.0:
                elapsed = now - start_time
                with self._lock:
                    placed = len(self.place_latencies)
                    cancelled = len(self.cancel_latencies)
                    place_err = len(self.place_errors)
                    cancel_err = len(self.cancel_errors)
                    
                    if self.place_latencies:
                        last_place = self.place_latencies[-1]
                    else:
                        last_place = 0
                    
                    if self.cancel_latencies:
                        last_cancel = self.cancel_latencies[-1]
                    else:
                        last_cancel = 0
                    
                    queue_depth = self._message_queue.qsize()
                    recent_loop_lags = self.event_loop_lags[-10:] if self.event_loop_lags else []
                    avg_loop_lag = statistics.mean(recent_loop_lags) if recent_loop_lags else 0.0
                    open_count = len(self.open_orders)
                
                rate = cycle_count / elapsed if elapsed > 0 else 0
                
                # Get most recent error for display
                last_error = None
                if self.place_errors:
                    last_error = self.place_errors[-1]
                elif self.cancel_errors:
                    last_error = self.cancel_errors[-1]
                
                error_summary = ""
                if place_err + cancel_err > 0:
                    if last_error:
                        # Truncate error message for display
                        error_preview = last_error[:60].replace('\n', ' ')
                        if len(last_error) > 60:
                            error_preview += "..."
                        error_summary = f" | last_err:{error_preview}"
                    else:
                        error_summary = f" | err:{place_err+cancel_err}"
                
                print(f"  {elapsed:6.1f}s: [{placed:4}/{cancelled:4}] "
                      f"place:{last_place:5.0f}ms | cancel:{last_cancel:5.0f}ms | "
                      f"queue:{queue_depth:2} | loop_lag:{avg_loop_lag:5.1f}ms | "
                      f"open:{open_count}/{self.max_open} | ws:{self.ws_messages:5} | err:{place_err+cancel_err}{error_summary}")
                last_print = now
            
            # Pace to target rate
            cycle_time = time.perf_counter() - cycle_start
            sleep_time = interval - cycle_time
            if sleep_time > 0:
                await asyncio.sleep(sleep_time)
        
        # Stop
        self._stop.set()
        total_time = time.perf_counter() - start_time
        
        # Wait for tasks to finish
        await asyncio.sleep(1.0)
        ws_task.cancel()
        processor_task.cancel()
        try:
            await ws_task
        except asyncio.CancelledError:
            pass
        try:
            await processor_task
        except asyncio.CancelledError:
            pass
        
        # Cleanup any remaining orders
        print(f"\n[CLEANUP] Cancelling {len(self.open_orders)} remaining orders...")
        for order_id in list(self.open_orders.keys()):
            self.cancel_order(order_id)
        
        # Cancel all just to be safe
        self.executor.cancel_all(quiet=True)
        
        # Stats
        return self._compute_stats(total_time, initial_ws_msgs)
    
    def run_test(self, duration_seconds: float = 600, target_rate: float = 5) -> Dict[str, Any]:
        """Synchronous wrapper for async test."""
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            return self._loop.run_until_complete(
                self._run_test_async(duration_seconds, target_rate)
            )
        finally:
            self._loop.close()
    
    def _compute_stats(self, total_time: float, initial_ws_msgs: int) -> Dict[str, Any]:
        """Compute and display comprehensive statistics including p99."""
        print(f"\n{'='*60}")
        print("RESULTS")
        print(f"{'='*60}")
        
        stats = {}
        
        # Helper to compute percentiles
        def percentile(data: List[float], p: float) -> float:
            if not data:
                return 0.0
            sorted_data = sorted(data)
            idx = int(len(sorted_data) * p)
            idx = min(idx, len(sorted_data) - 1)
            return sorted_data[idx]
        
        # Throughput
        total_placed = len(self.place_latencies)
        total_cancelled = len(self.cancel_latencies)
        place_rate = total_placed / total_time if total_time > 0 else 0
        cancel_rate = total_cancelled / total_time if total_time > 0 else 0
        
        print(f"\nTHROUGHPUT:")
        print(f"  Duration:        {total_time:.1f}s ({total_time/60:.1f} minutes)")
        print(f"  Orders placed:   {total_placed} ({place_rate:.1f}/sec)")
        print(f"  Orders cancelled:{total_cancelled} ({cancel_rate:.1f}/sec)")
        print(f"  Place errors:    {len(self.place_errors)}")
        print(f"  Cancel errors:   {len(self.cancel_errors)}")
        
        stats['throughput'] = {
            'duration_s': total_time,
            'orders_placed': total_placed,
            'orders_cancelled': total_cancelled,
            'place_rate': place_rate,
            'cancel_rate': cancel_rate,
            'place_errors': len(self.place_errors),
            'cancel_errors': len(self.cancel_errors),
        }
        
        # Place latency (with p99)
        if self.place_latencies:
            sorted_latencies = sorted(self.place_latencies)
            stats['place_latency'] = {
                'min_ms': min(self.place_latencies),
                'max_ms': max(self.place_latencies),
                'mean_ms': statistics.mean(self.place_latencies),
                'median_ms': statistics.median(self.place_latencies),
                'p95_ms': percentile(self.place_latencies, 0.95),
                'p99_ms': percentile(self.place_latencies, 0.99),
                'stdev_ms': statistics.stdev(self.place_latencies) if len(self.place_latencies) > 1 else 0,
            }
            
            s = stats['place_latency']
            print(f"\nPLACE LATENCY (n={len(self.place_latencies)}):")
            print(f"  Min:    {s['min_ms']:8.1f} ms")
            print(f"  Max:    {s['max_ms']:8.1f} ms")
            print(f"  Mean:   {s['mean_ms']:8.1f} ms")
            print(f"  Median: {s['median_ms']:8.1f} ms")
            print(f"  P95:    {s['p95_ms']:8.1f} ms")
            print(f"  P99:    {s['p99_ms']:8.1f} ms ⭐")
            print(f"  StdDev: {s['stdev_ms']:8.1f} ms")
        
        # Cancel latency (with p99)
        if self.cancel_latencies:
            stats['cancel_latency'] = {
                'min_ms': min(self.cancel_latencies),
                'max_ms': max(self.cancel_latencies),
                'mean_ms': statistics.mean(self.cancel_latencies),
                'median_ms': statistics.median(self.cancel_latencies),
                'p95_ms': percentile(self.cancel_latencies, 0.95),
                'p99_ms': percentile(self.cancel_latencies, 0.99),
                'stdev_ms': statistics.stdev(self.cancel_latencies) if len(self.cancel_latencies) > 1 else 0,
            }
            
            s = stats['cancel_latency']
            print(f"\nCANCEL LATENCY (n={len(self.cancel_latencies)}):")
            print(f"  Min:    {s['min_ms']:8.1f} ms")
            print(f"  Max:    {s['max_ms']:8.1f} ms")
            print(f"  Mean:   {s['mean_ms']:8.1f} ms")
            print(f"  Median: {s['median_ms']:8.1f} ms")
            print(f"  P95:    {s['p95_ms']:8.1f} ms")
            print(f"  P99:    {s['p99_ms']:8.1f} ms ⭐")
            print(f"  StdDev: {s['stdev_ms']:8.1f} ms")
        
        # Event loop lag
        if self.event_loop_lags:
            stats['event_loop_lag'] = {
                'min_ms': min(self.event_loop_lags),
                'max_ms': max(self.event_loop_lags),
                'mean_ms': statistics.mean(self.event_loop_lags),
                'median_ms': statistics.median(self.event_loop_lags),
                'p95_ms': percentile(self.event_loop_lags, 0.95),
                'p99_ms': percentile(self.event_loop_lags, 0.99),
            }
            
            s = stats['event_loop_lag']
            print(f"\nEVENT LOOP LAG (n={len(self.event_loop_lags)}):")
            print(f"  Mean:   {s['mean_ms']:8.1f} ms")
            print(f"  Median: {s['median_ms']:8.1f} ms")
            print(f"  P95:    {s['p95_ms']:8.1f} ms")
            print(f"  P99:    {s['p99_ms']:8.1f} ms ⭐")
        
        # Queue depth
        if self.queue_depths:
            stats['queue_depth'] = {
                'mean': statistics.mean(self.queue_depths),
                'median': statistics.median(self.queue_depths),
                'max': self.max_queue_depth,
                'p95': percentile(self.queue_depths, 0.95),
                'p99': percentile(self.queue_depths, 0.99),
            }
            
            s = stats['queue_depth']
            print(f"\nQUEUE DEPTH (n={len(self.queue_depths)}):")
            print(f"  Mean:   {s['mean']:8.1f}")
            print(f"  Median: {s['median']:8.1f}")
            print(f"  Max:    {s['max']:8.0f}")
            print(f"  P95:    {s['p95']:8.1f}")
            print(f"  P99:    {s['p99']:8.1f} ⭐")
        
        # Book update processing time
        if self.book_update_times:
            stats['book_update_time'] = {
                'min_ms': min(self.book_update_times),
                'max_ms': max(self.book_update_times),
                'mean_ms': statistics.mean(self.book_update_times),
                'median_ms': statistics.median(self.book_update_times),
                'p95_ms': percentile(self.book_update_times, 0.95),
                'p99_ms': percentile(self.book_update_times, 0.99),
            }
            
            s = stats['book_update_time']
            print(f"\nBOOK UPDATE TIME (n={len(self.book_update_times)}):")
            print(f"  Mean:   {s['mean_ms']:8.1f} ms")
            print(f"  Median: {s['median_ms']:8.1f} ms")
            print(f"  P95:    {s['p95_ms']:8.1f} ms")
            print(f"  P99:    {s['p99_ms']:8.1f} ms ⭐")
        
        # WebSocket stats
        ws_during_test = self.ws_messages - initial_ws_msgs
        ws_rate = ws_during_test / total_time if total_time > 0 else 0
        
        print(f"\nWEBSOCKET MARKET DATA (L2 Feed):")
        print(f"  Messages during test: {ws_during_test} ({ws_rate:.1f}/sec)")
        print(f"  Book updates:   {self.ws_book_updates}")
        print(f"  Trades:         {self.ws_trades}")
        print(f"  Price changes:  {self.ws_price_changes}")
        
        stats['websocket'] = {
            'messages': ws_during_test,
            'rate': ws_rate,
            'book_updates': self.ws_book_updates,
            'trades': self.ws_trades,
            'price_changes': self.ws_price_changes,
        }
        
        # Correlation analysis: order latency vs data handler load
        if self.place_latencies_with_context and len(self.place_latencies_with_context) > 10:
            # Group orders by queue depth
            high_queue = [x['latency_ms'] for x in self.place_latencies_with_context if x['queue_depth'] > 5]
            low_queue = [x['latency_ms'] for x in self.place_latencies_with_context if x['queue_depth'] <= 5]
            
            if high_queue and low_queue:
                high_queue_median = statistics.median(high_queue)
                low_queue_median = statistics.median(low_queue)
                
                print(f"\nCORRELATION ANALYSIS:")
                print(f"  Orders with queue_depth > 5:  {len(high_queue)} orders, median latency: {high_queue_median:.1f}ms")
                print(f"  Orders with queue_depth <= 5: {len(low_queue)} orders, median latency: {low_queue_median:.1f}ms")
                if high_queue_median > low_queue_median:
                    diff = high_queue_median - low_queue_median
                    pct = (diff / low_queue_median) * 100
                    print(f"  ⚠️  High queue depth increases latency by {diff:.1f}ms ({pct:.1f}%)")
                
                stats['correlation'] = {
                    'high_queue_count': len(high_queue),
                    'high_queue_median_ms': high_queue_median,
                    'low_queue_count': len(low_queue),
                    'low_queue_median_ms': low_queue_median,
                }
        
        # Errors - show detailed breakdown
        if self.place_errors:
            print(f"\nPLACE ERRORS ({len(self.place_errors)} total):")
            
            # Show first error in full
            print(f"\n  First error encountered:")
            first_err = self.place_errors[0]
            if len(first_err) > 200:
                print(f"    {first_err[:200]}")
                print(f"    ...truncated ({len(first_err)} chars total)")
            else:
                print(f"    {first_err}")
            
            # Group errors by type
            error_counts = {}
            for err in self.place_errors:
                # Extract error type (first 150 chars as key)
                err_key = err[:150]
                error_counts[err_key] = error_counts.get(err_key, 0) + 1
            
            print(f"\n  Error breakdown:")
            for err_key, count in sorted(error_counts.items(), key=lambda x: -x[1])[:5]:
                print(f"    [{count:3}x] {err_key}")
            
            # Check if it's Cloudflare blocking
            if any("403" in str(err) and ("<!DOCTYPE" in str(err) or "cloudflare" in str(err).lower()) 
                   for err in self.place_errors):
                print(f"\n  ⚠️  CLOUDFLARE RATE LIMIT DETECTED")
                print(f"      This VPS IP is being rate-limited by Cloudflare.")
                print(f"      Try: lower --rate (e.g., --rate 1 or --rate 2)")
                print(f"      Or: wait 10-15 minutes and try again")
        
        if self.cancel_errors:
            print(f"\nCANCEL ERRORS ({len(self.cancel_errors)} total):")
            
            # Show first error in full
            print(f"\n  First error encountered:")
            first_err = self.cancel_errors[0]
            if len(first_err) > 200:
                print(f"    {first_err[:200]}")
                print(f"    ...truncated ({len(first_err)} chars total)")
            else:
                print(f"    {first_err}")
            
            # Group errors by type
            error_counts = {}
            for err in self.cancel_errors:
                err_key = err[:150]
                error_counts[err_key] = error_counts.get(err_key, 0) + 1
            
            print(f"\n  Error breakdown:")
            for err_key, count in sorted(error_counts.items(), key=lambda x: -x[1])[:5]:
                print(f"    [{count:3}x] {err_key}")
        
        print(f"\n{'='*60}")
        
        # Summary
        if self.place_latencies and self.cancel_latencies:
            print(f"\n📊 SUMMARY:")
            print(f"   Duration: {total_time/60:.1f} minutes")
            print(f"   Throughput: {place_rate:.1f} orders/sec")
            print(f"   Place:  {stats['place_latency']['median_ms']:.0f}ms median, {stats['place_latency']['p99_ms']:.0f}ms p99 ⭐")
            print(f"   Cancel: {stats['cancel_latency']['median_ms']:.0f}ms median, {stats['cancel_latency']['p99_ms']:.0f}ms p99 ⭐")
            if 'event_loop_lag' in stats:
                print(f"   Event loop lag: {stats['event_loop_lag']['p99_ms']:.1f}ms p99 ⭐")
            if 'queue_depth' in stats:
                print(f"   Queue depth: {stats['queue_depth']['p99']:.1f} p99 ⭐")
            print(f"   WS data: {ws_rate:.1f} msgs/sec")
            if len(self.place_errors) > 0 or len(self.cancel_errors) > 0:
                print(f"   ⚠️  Errors: {len(self.place_errors)} place, {len(self.cancel_errors)} cancel (see details above)")
        
        return stats


# ============================================================================
# Legacy Market Making Stress Test (kept for backward compatibility)
# ============================================================================

class MarketMakingStressTest(EnhancedMarketMakingStressTest):
    """Legacy alias for backward compatibility."""
    def __init__(self, executor: PolymarketOrderExecutor, token_id: str,
                 base_price: float = 0.02, size: float = 10, max_open: int = 5):
        super().__init__(executor, [token_id], base_price, size, max_open)


# ============================================================================
# Main
# ============================================================================

def main():
    parser = argparse.ArgumentParser(
        description="Test Polymarket order RTT (Round-Trip Time)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Environment Variables:
  POLY_PRIVATE_KEY   Wallet private key (REQUIRED)
  POLY_FUNDER        Funder address (REQUIRED)

Examples:
  # Basic API RTT test
  uv run --with py-clob-client test_polymarket_rtt.py --market "..." 
  
  # WebSocket RTT test (order-to-book visibility)
  uv run --with py-clob-client,websockets test_polymarket_rtt.py --market "..." --ws-rtt -n 10
  
  # Enhanced market-making stress test (10 minutes, L2 feed monitoring, p99 metrics)
  uv run --with py-clob-client,websockets test_polymarket_rtt.py --market "..." --stress
  
  # Stress test with custom duration (15 minutes) and multiple markets for burst conditions
  uv run --with py-clob-client,websockets test_polymarket_rtt.py --market "..." --stress --duration 900 --markets "market2" "market3"
  
  # Stress test with higher rate
  uv run --with py-clob-client,websockets test_polymarket_rtt.py --market "..." --stress --duration 600 --rate 10
        """
    )
    parser.add_argument(
        "--market", "-m",
        required=True,
        help="Market slug or full Polymarket URL"
    )
    parser.add_argument(
        "--outcome",
        default="YES",
        help="Which outcome to trade: YES/NO or UP/DOWN (default: YES)"
    )
    parser.add_argument(
        "--iterations", "-n",
        type=int,
        default=5,
        help="Number of test iterations (default: 5)"
    )
    parser.add_argument(
        "--test-cancel",
        action="store_true",
        help="Also measure cancel order latency"
    )
    parser.add_argument(
        "--ws-rtt",
        action="store_true",
        help="Measure order-to-book visibility via WebSocket (true end-to-end latency)"
    )
    parser.add_argument(
        "--stress",
        action="store_true",
        help="Enhanced market-making stress test: rapid place/cancel cycles with L2 feed monitoring"
    )
    parser.add_argument(
        "--duration",
        type=float,
        default=600,
        help="Duration for stress test in seconds (default: 600 = 10 minutes, recommended: 5-15 minutes)"
    )
    parser.add_argument(
        "--markets",
        nargs="+",
        help="Additional market slugs/URLs to subscribe to (for burst conditions). First market is used for orders."
    )
    parser.add_argument(
        "--rate",
        type=float,
        default=1,
        help="Target orders/sec for stress test (default: 1, conservative to avoid balance issues)"
    )
    parser.add_argument(
        "--warmup",
        type=int,
        default=1,
        help="Warmup iterations (default: 1)"
    )
    parser.add_argument(
        "--price",
        type=float,
        default=0.01,
        help="Order price - use low price to avoid fills (default: 0.01)"
    )
    parser.add_argument(
        "--size",
        type=float,
        default=5,
        help="Order size in contracts (default: 5, at price 0.01 = $0.05 per order)"
    )
    parser.add_argument(
        "--signature-type",
        type=int,
        default=1,
        help="Signature type (default: 1)"
    )
    args = parser.parse_args()
    
    # Get credentials
    private_key = os.environ.get("POLY_PRIVATE_KEY")
    if not private_key:
        print("❌ POLY_PRIVATE_KEY not set")
        print("\nRun:")
        print('  export POLY_PRIVATE_KEY="0x..."')
        sys.exit(1)
    
    funder = os.environ.get("POLY_FUNDER")
    if not funder:
        print("❌ POLY_FUNDER not set")
        print("\nRun:")
        print('  export POLY_FUNDER="0x..."')
        sys.exit(1)
    
    try:
        # Fetch market info
        market_info = fetch_market_info(args.market)
        token_id = get_token_id(market_info, args.outcome)
        
        print(f"\n  Using {args.outcome.upper()} token: {token_id[:40]}...")
        
        # Initialize executor
        executor = PolymarketOrderExecutor(
            private_key=private_key,
            funder=funder,
            signature_type=args.signature_type
        )
        
        # Run test
        if args.stress:
            # Enhanced market-making stress test with L2 feed monitoring
            # Collect token IDs from all markets
            token_ids = [token_id]
            
            if args.markets:
                print(f"\n[MARKETS] Fetching additional markets for burst conditions...")
                for market_slug in args.markets:
                    try:
                        market_info = fetch_market_info(market_slug)
                        additional_token_id = get_token_id(market_info, args.outcome)
                        token_ids.append(additional_token_id)
                        print(f"  ✅ Added: {market_slug[:40]}... -> {additional_token_id[:40]}...")
                    except Exception as e:
                        print(f"  ⚠️  Failed to fetch {market_slug}: {e}")
            
            print(f"\n  Total markets: {len(token_ids)}")
            print(f"  Primary token (for orders): {token_ids[0][:40]}...")
            
            tester = EnhancedMarketMakingStressTest(
                executor=executor,
                token_ids=token_ids,
                base_price=args.price,
                size=args.size,
                max_open=5
            )
            stats = tester.run_test(
                duration_seconds=args.duration,
                target_rate=args.rate
            )
        elif args.ws_rtt:
            # WebSocket RTT test - measures order-to-book visibility
            tester = WebSocketRTTTester(
                executor=executor,
                token_id=token_id,
                price=args.price,
                size=args.size
            )
            stats = tester.run_test(
                iterations=args.iterations,
                warmup=args.warmup
            )
        else:
            # Standard API RTT test
            tester = RTTTester(
                executor=executor,
                token_id=token_id,
                price=args.price,
                size=args.size
            )
            stats = tester.run_test(
                iterations=args.iterations,
                test_cancel=args.test_cancel,
                warmup=args.warmup
            )
        
        print("\n✅ Test complete!")
        
    except KeyboardInterrupt:
        print("\n\nInterrupted")
        sys.exit(0)
    except Exception as e:
        print(f"\n❌ Error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
