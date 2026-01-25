#!/usr/bin/env python3
"""
Binance-Polymarket Latency Visualizer.

Streams both Binance BTC price and Polymarket Bitcoin hourly market simultaneously,
recording all updates with LOCAL receive timestamps. Generates an interactive Plotly
visualization to analyze how quickly Polymarket reacts to Binance price movements.

Key insight: By using YOUR receive timestamps (not server timestamps), you can
measure your potential latency edge - how long after you see a Binance move
does Polymarket's market reflect it?

Usage:
    source .venv/bin/activate && python latency_visualizer.py [-d SECONDS] [-o OUTPUT.html]
"""

import asyncio
import json
import signal
import sys
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

import aiohttp
import websockets

# Constants
BINANCE_WS_URL = "wss://stream.binance.com:9443/stream?streams=btcusdt@bookTicker/btcusdt@trade"
PM_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
GAMMA_API = "https://gamma-api.polymarket.com"
ET = ZoneInfo("America/New_York")
MONTH_NAMES = ["january", "february", "march", "april", "may", "june",
               "july", "august", "september", "october", "november", "december"]


@dataclass
class BinanceTick:
    """Single Binance BBO update."""
    local_ts_ms: int  # YOUR receive timestamp
    bid_px: float
    ask_px: float
    bid_sz: float
    ask_sz: float
    mid_px: float


@dataclass
class BinanceTrade:
    """Single Binance trade."""
    local_ts_ms: int
    price: float
    size: float
    is_sell: bool


@dataclass
class PolymarketTick:
    """Single Polymarket BBO update."""
    local_ts_ms: int  # YOUR receive timestamp
    yes_bid: int      # cents (0-100)
    yes_ask: int      # cents (0-100)
    yes_bid_sz: int   # size at bid
    yes_ask_sz: int   # size at ask
    event_type: str   # book, price_change, best_bid_ask


@dataclass
class PolymarketTrade:
    """Single Polymarket trade."""
    local_ts_ms: int
    price: int        # cents (0-100)
    size: float       # trade size
    side: str         # 'BUY' = lifted ask, 'SELL' = hit bid


@dataclass
class StreamData:
    """Collected stream data for visualization."""
    binance_ticks: list[BinanceTick] = field(default_factory=list)
    binance_trades: list[BinanceTrade] = field(default_factory=list)
    polymarket_ticks: list[PolymarketTick] = field(default_factory=list)
    polymarket_trades: list[PolymarketTrade] = field(default_factory=list)

    # Reference price from market question
    reference_price: Optional[float] = None
    market_question: str = ""

    # Stats
    binance_count: int = 0
    polymarket_count: int = 0
    polymarket_trade_count: int = 0
    start_time_ms: int = 0


data = StreamData()
stop_event = asyncio.Event()


def build_market_slug() -> str:
    """Build current hour's market slug."""
    now = datetime.now(ET).replace(minute=0, second=0, microsecond=0)
    month = MONTH_NAMES[now.month - 1]
    day = now.day
    hour_24 = now.hour

    if hour_24 == 0:
        hour_12, period = 12, "am"
    elif hour_24 < 12:
        hour_12, period = hour_24, "am"
    elif hour_24 == 12:
        hour_12, period = 12, "pm"
    else:
        hour_12, period = hour_24 - 12, "pm"

    return f"bitcoin-up-or-down-{month}-{day}-{hour_12}{period}-et"


async def find_bitcoin_market() -> tuple[Optional[str], Optional[str], Optional[str], Optional[float]]:
    """Find current Bitcoin hourly market tokens and reference price."""
    import re

    slug = build_market_slug()
    print(f"Looking for market: {slug}")

    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
        url = f"{GAMMA_API}/events/slug/{slug}"
        async with session.get(url) as resp:
            if resp.status != 200:
                print(f"Failed to fetch event: {resp.status}")
                return None, None, None, None
            event = await resp.json()

        markets = event.get("markets", [])
        if not markets:
            event_id = event.get("id")
            if event_id:
                url = f"{GAMMA_API}/markets?event_id={event_id}"
                async with session.get(url) as resp:
                    if resp.status == 200:
                        markets = await resp.json()

        if not markets:
            print("No markets found")
            return None, None, None, None

        market = markets[0]
        clob_ids = market.get("clobTokenIds")
        if isinstance(clob_ids, str):
            clob_ids = json.loads(clob_ids)

        yes_token = clob_ids[0] if clob_ids else None
        no_token = clob_ids[1] if clob_ids and len(clob_ids) > 1 else None
        question = market.get("question", "")

        # Extract reference price
        pattern = r"\$([0-9,]+(?:\.[0-9]+)?)"
        match = re.search(pattern, question)
        ref_price = None
        if match:
            try:
                ref_price = float(match.group(1).replace(",", ""))
            except ValueError:
                pass

        print(f"Market: {question[:80]}...")
        print(f"YES token: {yes_token}")
        print(f"NO token:  {no_token}")
        if ref_price:
            print(f"Reference price: ${ref_price:,.2f}")

        return yes_token, no_token, question, ref_price


async def stream_binance():
    """Stream Binance BTC data."""
    print(f"Connecting to Binance...")

    try:
        async with websockets.connect(BINANCE_WS_URL) as ws:
            print("Binance connected")

            while not stop_event.is_set():
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=1.0)
                    local_ts_ms = int(time.time() * 1000)

                    msg = json.loads(raw)
                    stream = msg.get("stream", "")
                    payload = msg.get("data", {})

                    if stream.endswith("bookTicker"):
                        bid_px = float(payload["b"])
                        ask_px = float(payload["a"])
                        bid_sz = float(payload["B"])
                        ask_sz = float(payload["A"])
                        mid_px = (bid_px + ask_px) / 2

                        tick = BinanceTick(
                            local_ts_ms=local_ts_ms,
                            bid_px=bid_px,
                            ask_px=ask_px,
                            bid_sz=bid_sz,
                            ask_sz=ask_sz,
                            mid_px=mid_px,
                        )
                        data.binance_ticks.append(tick)
                        data.binance_count += 1

                    elif stream.endswith("trade"):
                        price = float(payload["p"])
                        size = float(payload["q"])
                        is_sell = payload.get("m", False)

                        trade = BinanceTrade(
                            local_ts_ms=local_ts_ms,
                            price=price,
                            size=size,
                            is_sell=is_sell,
                        )
                        data.binance_trades.append(trade)

                except asyncio.TimeoutError:
                    continue
                except websockets.ConnectionClosed:
                    print("Binance connection closed")
                    break

    except Exception as e:
        print(f"Binance error: {e}")


async def stream_polymarket(yes_token: str, no_token: str):
    """Stream Polymarket market data."""
    print(f"Connecting to Polymarket...")

    # Internal book to track BBO with sizes
    yes_bids: dict[int, int] = {}  # price_cents -> size
    yes_asks: dict[int, int] = {}

    def price_to_cents(price_str: Optional[str]) -> int:
        if not price_str:
            return 0
        try:
            return round(float(price_str) * 100)
        except (ValueError, TypeError):
            return 0

    def get_bbo() -> tuple[int, int, int, int]:
        """Get current BBO from internal book."""
        bid_px = max(yes_bids.keys()) if yes_bids else 0
        bid_sz = yes_bids.get(bid_px, 0)
        ask_px = min(yes_asks.keys()) if yes_asks else 100
        ask_sz = yes_asks.get(ask_px, 0)
        return bid_px, bid_sz, ask_px, ask_sz

    try:
        async with websockets.connect(PM_WS_URL) as ws:
            print("Polymarket connected")

            # Subscribe
            sub_msg = {"type": "market", "assets_ids": [yes_token, no_token]}
            await ws.send(json.dumps(sub_msg))
            print("Polymarket subscribed")

            while not stop_event.is_set():
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=1.0)
                    local_ts_ms = int(time.time() * 1000)

                    msg = json.loads(raw)
                    items = msg if isinstance(msg, list) else [msg]

                    for item in items:
                        if not isinstance(item, dict):
                            continue

                        event_type = item.get("event_type", "")
                        asset_id = item.get("asset_id", "")

                        # Only process YES token
                        if asset_id and asset_id != yes_token:
                            continue

                        if event_type == "book":
                            # Full book snapshot - rebuild internal book
                            if asset_id == yes_token:
                                yes_bids.clear()
                                yes_asks.clear()

                                for level in item.get("bids", []):
                                    px = price_to_cents(level.get("price"))
                                    try:
                                        sz = int(float(level.get("size", 0)))
                                    except (ValueError, TypeError):
                                        sz = 0
                                    if sz > 0:
                                        yes_bids[px] = sz

                                for level in item.get("asks", []):
                                    px = price_to_cents(level.get("price"))
                                    try:
                                        sz = int(float(level.get("size", 0)))
                                    except (ValueError, TypeError):
                                        sz = 0
                                    if sz > 0:
                                        yes_asks[px] = sz

                                bid_px, bid_sz, ask_px, ask_sz = get_bbo()
                                tick = PolymarketTick(
                                    local_ts_ms=local_ts_ms,
                                    yes_bid=bid_px,
                                    yes_ask=ask_px,
                                    yes_bid_sz=bid_sz,
                                    yes_ask_sz=ask_sz,
                                    event_type="book",
                                )
                                data.polymarket_ticks.append(tick)
                                data.polymarket_count += 1

                        elif event_type == "price_change":
                            # Incremental update
                            for change in item.get("price_changes", []):
                                if change.get("asset_id") != yes_token:
                                    continue

                                side = change.get("side", "")
                                price_str = change.get("price")
                                size_str = change.get("size")

                                if price_str is not None and size_str is not None:
                                    px = price_to_cents(price_str)
                                    try:
                                        sz = int(float(size_str))
                                    except (ValueError, TypeError):
                                        sz = 0

                                    if side == "BUY":
                                        if sz > 0:
                                            yes_bids[px] = sz
                                        elif px in yes_bids:
                                            del yes_bids[px]
                                    elif side == "SELL":
                                        if sz > 0:
                                            yes_asks[px] = sz
                                        elif px in yes_asks:
                                            del yes_asks[px]

                                # Get new BBO
                                bid_px, bid_sz, ask_px, ask_sz = get_bbo()
                                tick = PolymarketTick(
                                    local_ts_ms=local_ts_ms,
                                    yes_bid=bid_px,
                                    yes_ask=ask_px,
                                    yes_bid_sz=bid_sz,
                                    yes_ask_sz=ask_sz,
                                    event_type="price_change",
                                )
                                data.polymarket_ticks.append(tick)
                                data.polymarket_count += 1

                        elif event_type == "best_bid_ask":
                            # Direct BBO update (prices only)
                            if asset_id == yes_token:
                                bid_px = price_to_cents(item.get("best_bid"))
                                ask_px = price_to_cents(item.get("best_ask"))
                                bid_sz = yes_bids.get(bid_px, 0)
                                ask_sz = yes_asks.get(ask_px, 0)

                                tick = PolymarketTick(
                                    local_ts_ms=local_ts_ms,
                                    yes_bid=bid_px,
                                    yes_ask=ask_px,
                                    yes_bid_sz=bid_sz,
                                    yes_ask_sz=ask_sz,
                                    event_type="best_bid_ask",
                                )
                                data.polymarket_ticks.append(tick)
                                data.polymarket_count += 1

                        elif event_type == "tick":
                            # Trade event - capture which side was hit
                            if asset_id == yes_token:
                                price_str = item.get("price")
                                size_str = item.get("size")
                                side = item.get("side", "")  # BUY = lifted ask, SELL = hit bid

                                if price_str and size_str:
                                    price_cents = price_to_cents(price_str)
                                    try:
                                        size = float(size_str)
                                    except (ValueError, TypeError):
                                        size = 0

                                    if size > 0:
                                        trade = PolymarketTrade(
                                            local_ts_ms=local_ts_ms,
                                            price=price_cents,
                                            size=size,
                                            side=side,
                                        )
                                        data.polymarket_trades.append(trade)
                                        data.polymarket_trade_count += 1

                except asyncio.TimeoutError:
                    continue
                except websockets.ConnectionClosed:
                    print("Polymarket connection closed")
                    break

    except Exception as e:
        print(f"Polymarket error: {e}")


def create_visualization(output_file: str = "latency_analysis.html"):
    """Create interactive Plotly visualization."""
    try:
        import plotly.graph_objects as go
        from plotly.subplots import make_subplots
    except ImportError:
        print("ERROR: plotly not installed. Run: pip install plotly")
        return

    if not data.binance_ticks or not data.polymarket_ticks:
        print("Not enough data for visualization")
        return

    # Convert to relative timestamps (seconds from start)
    start_ts = data.start_time_ms

    # Binance BBO data
    bn_times = [(t.local_ts_ms - start_ts) / 1000 for t in data.binance_ticks]
    bn_bids = [t.bid_px for t in data.binance_ticks]
    bn_asks = [t.ask_px for t in data.binance_ticks]
    bn_mids = [t.mid_px for t in data.binance_ticks]
    bn_spreads = [t.ask_px - t.bid_px for t in data.binance_ticks]

    # Binance trades
    trade_times = [(t.local_ts_ms - start_ts) / 1000 for t in data.binance_trades]
    trade_prices = [t.price for t in data.binance_trades]
    trade_sizes = [t.size for t in data.binance_trades]
    trade_colors = ['red' if t.is_sell else 'green' for t in data.binance_trades]

    # Polymarket BBO data
    pm_times = [(t.local_ts_ms - start_ts) / 1000 for t in data.polymarket_ticks]
    pm_bids = [t.yes_bid for t in data.polymarket_ticks]
    pm_asks = [t.yes_ask for t in data.polymarket_ticks]
    pm_mids = [(t.yes_bid + t.yes_ask) / 2 for t in data.polymarket_ticks]
    pm_spreads = [t.yes_ask - t.yes_bid for t in data.polymarket_ticks]

    # Create figure with subplots
    fig = make_subplots(
        rows=4, cols=1,
        shared_xaxes=True,
        vertical_spacing=0.05,
        subplot_titles=(
            f'Binance BTC/USDT Price (Ref: ${data.reference_price:,.0f})' if data.reference_price else 'Binance BTC/USDT Price',
            'Binance Spread ($)',
            'Polymarket YES Probability (cents)',
            'Polymarket Spread (cents)'
        ),
        row_heights=[0.35, 0.15, 0.35, 0.15]
    )

    # Row 1: Binance price with bid/ask bands
    fig.add_trace(
        go.Scatter(x=bn_times, y=bn_asks, mode='lines', name='Binance Ask',
                   line=dict(color='rgba(255,0,0,0.3)', width=1),
                   hovertemplate='Ask: $%{y:,.2f}<br>Time: %{x:.2f}s'),
        row=1, col=1
    )
    fig.add_trace(
        go.Scatter(x=bn_times, y=bn_bids, mode='lines', name='Binance Bid',
                   line=dict(color='rgba(0,255,0,0.3)', width=1),
                   fill='tonexty', fillcolor='rgba(128,128,128,0.1)',
                   hovertemplate='Bid: $%{y:,.2f}<br>Time: %{x:.2f}s'),
        row=1, col=1
    )
    fig.add_trace(
        go.Scatter(x=bn_times, y=bn_mids, mode='lines', name='Binance Mid',
                   line=dict(color='blue', width=1.5),
                   hovertemplate='Mid: $%{y:,.2f}<br>Time: %{x:.2f}s'),
        row=1, col=1
    )

    # Add trades as scatter points
    if trade_times:
        fig.add_trace(
            go.Scatter(x=trade_times, y=trade_prices, mode='markers', name='Trades',
                       marker=dict(color=trade_colors, size=4, opacity=0.5),
                       hovertemplate='Trade: $%{y:,.2f}<br>Time: %{x:.2f}s'),
            row=1, col=1
        )

    # Add reference price line if available
    if data.reference_price:
        fig.add_hline(y=data.reference_price, line_dash="dash", line_color="orange",
                      annotation_text=f"Ref: ${data.reference_price:,.0f}", row=1, col=1)

    # Row 2: Binance spread
    fig.add_trace(
        go.Scatter(x=bn_times, y=bn_spreads, mode='lines', name='Binance Spread',
                   line=dict(color='purple', width=1),
                   hovertemplate='Spread: $%{y:.2f}<br>Time: %{x:.2f}s'),
        row=2, col=1
    )

    # Row 3: Polymarket probability with bid/ask
    fig.add_trace(
        go.Scatter(x=pm_times, y=pm_asks, mode='lines', name='PM Ask (YES)',
                   line=dict(color='rgba(255,0,0,0.5)', width=1),
                   hovertemplate='Ask: %{y}c<br>Time: %{x:.2f}s'),
        row=3, col=1
    )
    fig.add_trace(
        go.Scatter(x=pm_times, y=pm_bids, mode='lines', name='PM Bid (YES)',
                   line=dict(color='rgba(0,255,0,0.5)', width=1),
                   fill='tonexty', fillcolor='rgba(128,128,128,0.1)',
                   hovertemplate='Bid: %{y}c<br>Time: %{x:.2f}s'),
        row=3, col=1
    )
    fig.add_trace(
        go.Scatter(x=pm_times, y=pm_mids, mode='lines', name='PM Mid',
                   line=dict(color='blue', width=1.5),
                   hovertemplate='Mid: %{y:.1f}c<br>Time: %{x:.2f}s'),
        row=3, col=1
    )

    # Add Polymarket trades as scatter points
    # BUY = lifted ask (green, someone bought YES), SELL = hit bid (red, someone sold YES)
    if data.polymarket_trades:
        pm_trade_times = [(t.local_ts_ms - start_ts) / 1000 for t in data.polymarket_trades]
        pm_trade_prices = [t.price for t in data.polymarket_trades]
        pm_trade_sizes = [t.size for t in data.polymarket_trades]
        pm_trade_sides = [t.side for t in data.polymarket_trades]
        # Green = BUY (lifted ask, bullish), Red = SELL (hit bid, bearish)
        pm_trade_colors = ['green' if t.side == 'BUY' else 'red' for t in data.polymarket_trades]

        # Scale marker size by trade size (min 5, max 20)
        max_size = max(pm_trade_sizes) if pm_trade_sizes else 1
        pm_marker_sizes = [max(5, min(20, 5 + 15 * (s / max_size))) for s in pm_trade_sizes]

        fig.add_trace(
            go.Scatter(
                x=pm_trade_times,
                y=pm_trade_prices,
                mode='markers',
                name='PM Trades',
                marker=dict(
                    color=pm_trade_colors,
                    size=pm_marker_sizes,
                    opacity=0.7,
                    line=dict(width=1, color='black'),
                ),
                hovertemplate='<b>%{customdata[0]}</b><br>Price: %{y}c<br>Size: %{customdata[1]:,.0f}<br>Time: %{x:.2f}s<extra></extra>',
                customdata=list(zip(pm_trade_sides, pm_trade_sizes)),
            ),
            row=3, col=1
        )

    # Row 4: Polymarket spread
    fig.add_trace(
        go.Scatter(x=pm_times, y=pm_spreads, mode='lines', name='PM Spread',
                   line=dict(color='purple', width=1),
                   hovertemplate='Spread: %{y}c<br>Time: %{x:.2f}s'),
        row=4, col=1
    )

    # Update layout
    fig.update_layout(
        title=dict(
            text=f"Binance vs Polymarket Latency Analysis<br><sup>{data.market_question[:100]}...</sup>",
            x=0.5,
        ),
        height=900,
        showlegend=True,
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="center", x=0.5),
        hovermode='x unified',
    )

    # Update axes
    fig.update_xaxes(title_text="Time (seconds from start, YOUR receive timestamps)", row=4, col=1)
    fig.update_yaxes(title_text="Price ($)", row=1, col=1)
    fig.update_yaxes(title_text="Spread ($)", row=2, col=1)
    fig.update_yaxes(title_text="Probability (cents)", row=3, col=1)
    fig.update_yaxes(title_text="Spread (c)", row=4, col=1)

    # Enable range slider for zooming
    fig.update_xaxes(rangeslider=dict(visible=True, thickness=0.05), row=4, col=1)

    # Save to HTML
    fig.write_html(output_file, include_plotlyjs=True)
    print(f"\nVisualization saved to: {output_file}")
    print("Open this file in your browser to interact with the chart.")

    # Also print some stats
    print_stats()


def print_stats():
    """Print summary statistics."""
    if not data.binance_ticks or not data.polymarket_ticks:
        return

    duration_s = (data.binance_ticks[-1].local_ts_ms - data.start_time_ms) / 1000

    print("\n" + "=" * 70)
    print("STREAM STATISTICS")
    print("=" * 70)
    print(f"  Duration: {duration_s:.1f}s")
    print(f"  Binance updates: {data.binance_count:,} ({data.binance_count/duration_s:.1f}/s)")
    print(f"  Polymarket updates: {data.polymarket_count:,} ({data.polymarket_count/duration_s:.1f}/s)")
    print(f"  Polymarket trades: {data.polymarket_trade_count:,} ({data.polymarket_trade_count/duration_s:.2f}/s)")

    # Binance stats
    bn_spreads = [t.ask_px - t.bid_px for t in data.binance_ticks]
    print(f"\n  BINANCE BTC/USDT:")
    print(f"    Avg spread: ${sum(bn_spreads)/len(bn_spreads):.2f}")
    print(f"    Min/Max spread: ${min(bn_spreads):.2f} / ${max(bn_spreads):.2f}")
    print(f"    Price range: ${min(t.mid_px for t in data.binance_ticks):,.2f} - ${max(t.mid_px for t in data.binance_ticks):,.2f}")

    # Polymarket stats
    pm_spreads = [t.yes_ask - t.yes_bid for t in data.polymarket_ticks]
    print(f"\n  POLYMARKET YES TOKEN:")
    print(f"    Avg spread: {sum(pm_spreads)/len(pm_spreads):.1f} cents")
    print(f"    Min/Max spread: {min(pm_spreads)} / {max(pm_spreads)} cents")
    print(f"    Prob range: {min(t.yes_bid for t in data.polymarket_ticks)}c - {max(t.yes_ask for t in data.polymarket_ticks)}c")

    # Polymarket trade stats
    if data.polymarket_trades:
        buy_trades = [t for t in data.polymarket_trades if t.side == 'BUY']
        sell_trades = [t for t in data.polymarket_trades if t.side == 'SELL']
        buy_volume = sum(t.size for t in buy_trades)
        sell_volume = sum(t.size for t in sell_trades)
        print(f"\n  POLYMARKET TRADES:")
        print(f"    Total: {len(data.polymarket_trades)} trades")
        print(f"    BUY (lifted ask):  {len(buy_trades)} trades, {buy_volume:,.0f} volume")
        print(f"    SELL (hit bid):    {len(sell_trades)} trades, {sell_volume:,.0f} volume")
        if buy_volume + sell_volume > 0:
            buy_pct = 100 * buy_volume / (buy_volume + sell_volume)
            print(f"    Volume imbalance:  {buy_pct:.1f}% BUY / {100-buy_pct:.1f}% SELL")

    # Update frequency analysis
    if len(data.binance_ticks) > 1:
        bn_gaps = []
        for i in range(1, len(data.binance_ticks)):
            gap = data.binance_ticks[i].local_ts_ms - data.binance_ticks[i-1].local_ts_ms
            bn_gaps.append(gap)
        print(f"\n  BINANCE UPDATE GAPS:")
        print(f"    Avg: {sum(bn_gaps)/len(bn_gaps):.1f}ms")
        print(f"    Max: {max(bn_gaps)}ms")

    if len(data.polymarket_ticks) > 1:
        pm_gaps = []
        for i in range(1, len(data.polymarket_ticks)):
            gap = data.polymarket_ticks[i].local_ts_ms - data.polymarket_ticks[i-1].local_ts_ms
            pm_gaps.append(gap)
        print(f"\n  POLYMARKET UPDATE GAPS:")
        print(f"    Avg: {sum(pm_gaps)/len(pm_gaps):.1f}ms")
        print(f"    Max: {max(pm_gaps)}ms")

    if data.reference_price:
        # Calculate implied probability from Binance price
        print(f"\n  REFERENCE PRICE: ${data.reference_price:,.2f}")
        last_bn = data.binance_ticks[-1]
        diff = last_bn.mid_px - data.reference_price
        print(f"    Current BTC: ${last_bn.mid_px:,.2f} ({'+' if diff >= 0 else ''}{diff:,.2f} from ref)")

    print("\n" + "=" * 70)
    print("\nTIP: Look for moments where Binance price moves sharply.")
    print("     See how long until Polymarket probability adjusts.")
    print("     The delay is your potential latency edge!")
    print("\n     PM Trade colors: GREEN = BUY (lifted ask, bullish)")
    print("                      RED = SELL (hit bid, bearish)")
    print("     Watch which side gets picked off after Binance moves!")
    print("=" * 70)


async def run_streams(yes_token: str, no_token: str, duration_s: int):
    """Run both streams concurrently."""
    data.start_time_ms = int(time.time() * 1000)

    # Create tasks for both streams
    binance_task = asyncio.create_task(stream_binance())
    polymarket_task = asyncio.create_task(stream_polymarket(yes_token, no_token))

    # Progress reporting
    async def progress_reporter():
        start = time.monotonic()
        while not stop_event.is_set():
            await asyncio.sleep(5.0)
            elapsed = time.monotonic() - start
            print(f"  {elapsed:.0f}s: Binance={data.binance_count:,}, PM_quotes={data.polymarket_count:,}, PM_trades={data.polymarket_trade_count:,}")
            if elapsed >= duration_s:
                stop_event.set()
                break

    progress_task = asyncio.create_task(progress_reporter())

    # Wait for completion or timeout
    try:
        await asyncio.wait_for(
            asyncio.gather(binance_task, polymarket_task, progress_task, return_exceptions=True),
            timeout=duration_s + 5
        )
    except asyncio.TimeoutError:
        pass

    stop_event.set()

    # Cancel tasks
    for task in [binance_task, polymarket_task, progress_task]:
        if not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass


def signal_handler(signum, frame):
    """Handle Ctrl+C gracefully."""
    print("\n\nInterrupted! Generating visualization...")
    stop_event.set()


async def main(duration: int = 60, output_file: str = "latency_analysis.html"):
    """Main entry point."""
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    # Find the current Bitcoin hourly market
    yes_token, no_token, question, ref_price = await find_bitcoin_market()
    if not yes_token or not no_token:
        print("Failed to find Bitcoin market")
        sys.exit(1)

    data.market_question = question or ""
    data.reference_price = ref_price

    print(f"\nStarting dual stream for {duration}s...")
    print("-" * 70)

    await run_streams(yes_token, no_token, duration)

    print(f"\nCollected {data.binance_count:,} Binance updates, {data.polymarket_count:,} PM quotes, {data.polymarket_trade_count:,} PM trades")

    # Create visualization
    create_visualization(output_file)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Binance-Polymarket Latency Visualizer")
    parser.add_argument("-d", "--duration", type=int, default=60,
                        help="Duration in seconds (default: 60)")
    parser.add_argument("-o", "--output", type=str, default="latency_analysis.html",
                        help="Output HTML file (default: latency_analysis.html)")
    args = parser.parse_args()

    asyncio.run(main(args.duration, args.output))
