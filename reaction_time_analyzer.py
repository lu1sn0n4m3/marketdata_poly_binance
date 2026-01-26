#!/usr/bin/env python3
"""
Polymarket Reaction Time Analyzer.

1. Collects Binance + Polymarket bid-ask data for N minutes
2. Computes full-sample mean/std of Binance mid returns
3. Identifies "events" where return > K sigma
4. Measures time until Polymarket quote changed after each event

Usage:
    python reaction_time_analyzer.py -d 300 --sigma 2.0
"""

import asyncio
import json
import signal
import sys
import time
import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

import aiohttp
import websockets

# Constants
BINANCE_WS_URL = "wss://stream.binance.com:9443/stream?streams=btcusdt@bookTicker"
PM_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
GAMMA_API = "https://gamma-api.polymarket.com"
ET = ZoneInfo("America/New_York")
MONTH_NAMES = ["january", "february", "march", "april", "may", "june",
               "july", "august", "september", "october", "november", "december"]


@dataclass
class Tick:
    ts_ms: int
    bid: float
    ask: float
    mid: float


@dataclass
class State:
    binance_ticks: list[Tick] = field(default_factory=list)
    pm_ticks: list[Tick] = field(default_factory=list)
    yes_token: str = ""
    no_token: str = ""
    start_ms: int = 0


state = State()
stop_event = asyncio.Event()


def build_market_slug() -> str:
    now = datetime.now(ET).replace(minute=0, second=0, microsecond=0)
    month = MONTH_NAMES[now.month - 1]
    hour_24 = now.hour
    if hour_24 == 0:
        hour_12, period = 12, "am"
    elif hour_24 < 12:
        hour_12, period = hour_24, "am"
    elif hour_24 == 12:
        hour_12, period = 12, "pm"
    else:
        hour_12, period = hour_24 - 12, "pm"
    return f"bitcoin-up-or-down-{month}-{now.day}-{hour_12}{period}-et"


async def find_market() -> tuple[Optional[str], Optional[str]]:
    slug = build_market_slug()
    print(f"Looking for market: {slug}")

    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
        async with session.get(f"{GAMMA_API}/events/slug/{slug}") as resp:
            if resp.status != 200:
                return None, None
            event = await resp.json()

        markets = event.get("markets", [])
        if not markets and event.get("id"):
            async with session.get(f"{GAMMA_API}/markets?event_id={event['id']}") as resp:
                if resp.status == 200:
                    markets = await resp.json()

        if not markets:
            return None, None

        clob_ids = markets[0].get("clobTokenIds")
        if isinstance(clob_ids, str):
            clob_ids = json.loads(clob_ids)

        print(f"Found: {markets[0].get('question', '')[:60]}...")
        return (clob_ids[0], clob_ids[1]) if clob_ids and len(clob_ids) > 1 else (None, None)


async def stream_binance():
    print("Connecting to Binance...")
    try:
        async with websockets.connect(BINANCE_WS_URL) as ws:
            print("Binance connected")
            while not stop_event.is_set():
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=1.0)
                    ts_ms = int(time.time() * 1000)
                    d = json.loads(raw).get("data", {})
                    bid, ask = float(d["b"]), float(d["a"])
                    state.binance_ticks.append(Tick(ts_ms, bid, ask, (bid + ask) / 2))
                except asyncio.TimeoutError:
                    continue
                except websockets.ConnectionClosed:
                    break
    except Exception as e:
        print(f"Binance error: {e}")


async def stream_polymarket():
    print("Connecting to Polymarket...")
    yes_bids, yes_asks = {}, {}

    def cents(s):
        return round(float(s) * 100) if s else 0

    def bbo():
        bid = max(yes_bids.keys()) if yes_bids else 0
        ask = min(yes_asks.keys()) if yes_asks else 100
        return bid, ask

    try:
        async with websockets.connect(PM_WS_URL) as ws:
            print("Polymarket connected")
            await ws.send(json.dumps({"type": "market", "assets_ids": [state.yes_token, state.no_token]}))

            while not stop_event.is_set():
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=1.0)
                    ts_ms = int(time.time() * 1000)

                    for item in (json.loads(raw) if isinstance(json.loads(raw), list) else [json.loads(raw)]):
                        if not isinstance(item, dict):
                            continue
                        evt = item.get("event_type", "")
                        aid = item.get("asset_id", "")

                        if aid and aid != state.yes_token:
                            continue

                        if evt == "book" and aid == state.yes_token:
                            yes_bids.clear()
                            yes_asks.clear()
                            for lv in item.get("bids", []):
                                px, sz = cents(lv.get("price")), int(float(lv.get("size", 0)))
                                if sz > 0: yes_bids[px] = sz
                            for lv in item.get("asks", []):
                                px, sz = cents(lv.get("price")), int(float(lv.get("size", 0)))
                                if sz > 0: yes_asks[px] = sz
                            bid, ask = bbo()
                            state.pm_ticks.append(Tick(ts_ms, bid, ask, (bid + ask) / 2))

                        elif evt == "price_change":
                            for ch in item.get("price_changes", []):
                                if ch.get("asset_id") != state.yes_token:
                                    continue
                                side, px, sz = ch.get("side"), cents(ch.get("price")), int(float(ch.get("size", 0)))
                                book = yes_bids if side == "BUY" else yes_asks
                                if sz > 0:
                                    book[px] = sz
                                elif px in book:
                                    del book[px]
                            bid, ask = bbo()
                            state.pm_ticks.append(Tick(ts_ms, bid, ask, (bid + ask) / 2))

                except asyncio.TimeoutError:
                    continue
                except websockets.ConnectionClosed:
                    break
    except Exception as e:
        print(f"Polymarket error: {e}")


def find_tick_at_time(ticks: list[Tick], target_ms: int) -> Optional[Tick]:
    """Binary search for tick at or before target time."""
    if not ticks or ticks[0].ts_ms > target_ms:
        return None
    left, right = 0, len(ticks) - 1
    while left < right:
        mid = (left + right + 1) // 2
        if ticks[mid].ts_ms <= target_ms:
            left = mid
        else:
            right = mid - 1
    return ticks[left]


def detect_tick_events(bn: list[Tick], sigma: float) -> tuple[list, dict]:
    """Original single-tick detection."""
    returns = []
    for i in range(1, len(bn)):
        ret_bps = ((bn[i].mid - bn[i-1].mid) / bn[i-1].mid) * 10000
        returns.append((bn[i].ts_ms, ret_bps, bn[i].mid))

    mean = sum(r[1] for r in returns) / len(returns)
    var = sum((r[1] - mean) ** 2 for r in returns) / len(returns)
    std = math.sqrt(var)

    events = [(ts, ret, mid, "tick") for ts, ret, mid in returns if abs(ret - mean) > sigma * std]

    stats = {
        "n_returns": len(returns),
        "mean": mean,
        "std": std,
        "threshold_bps": sigma * std,
        "mode_info": "single tick-to-tick"
    }
    return events, stats


def detect_rolling_events(bn: list[Tick], sigma: float, window_ticks: int) -> tuple[list, dict]:
    """Rolling N-tick window detection."""
    if len(bn) < window_ticks + 1:
        return [], {"error": "Not enough ticks"}

    # Compute N-tick returns
    returns = []
    for i in range(window_ticks, len(bn)):
        ret_bps = ((bn[i].mid - bn[i - window_ticks].mid) / bn[i - window_ticks].mid) * 10000
        returns.append((bn[i].ts_ms, ret_bps, bn[i].mid))

    mean = sum(r[1] for r in returns) / len(returns)
    var = sum((r[1] - mean) ** 2 for r in returns) / len(returns)
    std = math.sqrt(var) if var > 0 else 0.001

    events = [(ts, ret, mid, f"rolling_{window_ticks}") for ts, ret, mid in returns
              if abs(ret - mean) > sigma * std]

    stats = {
        "n_returns": len(returns),
        "mean": mean,
        "std": std,
        "threshold_bps": sigma * std,
        "mode_info": f"{window_ticks}-tick rolling window"
    }
    return events, stats


def detect_time_events(bn: list[Tick], sigma: float, windows_ms: list[int]) -> tuple[list, dict]:
    """Time-based multi-scale detection."""
    all_events = []
    window_stats = {}

    for window_ms in windows_ms:
        # For each tick, find the tick ~window_ms ago and compute return
        returns = []
        for i, tick in enumerate(bn):
            target_ts = tick.ts_ms - window_ms
            past_tick = find_tick_at_time(bn[:i], target_ts)
            if past_tick and past_tick.mid > 0:
                # Check that we're actually looking back roughly the right amount of time
                actual_delta = tick.ts_ms - past_tick.ts_ms
                if actual_delta >= window_ms * 0.5:  # at least 50% of target window
                    ret_bps = ((tick.mid - past_tick.mid) / past_tick.mid) * 10000
                    returns.append((tick.ts_ms, ret_bps, tick.mid, actual_delta))

        if len(returns) < 10:
            continue

        # Compute stats for this window
        rets = [r[1] for r in returns]
        mean = sum(rets) / len(rets)
        var = sum((r - mean) ** 2 for r in rets) / len(rets)
        std = math.sqrt(var) if var > 0 else 0.001

        window_stats[window_ms] = {"mean": mean, "std": std, "n": len(returns)}

        # Find events for this window
        for ts, ret, mid, actual_delta in returns:
            z_score = abs(ret - mean) / std
            if z_score > sigma:
                all_events.append((ts, ret, mid, f"{window_ms}ms", z_score, actual_delta))

    # Deduplicate: if same timestamp appears in multiple windows, keep the highest z-score
    events_by_ts = {}
    for ev in all_events:
        ts = ev[0]
        if ts not in events_by_ts or ev[4] > events_by_ts[ts][4]:
            events_by_ts[ts] = ev

    # Sort by timestamp and convert to standard format
    sorted_events = sorted(events_by_ts.values(), key=lambda x: x[0])
    events = [(ts, ret, mid, window_label) for ts, ret, mid, window_label, _, _ in sorted_events]

    stats = {
        "windows": window_stats,
        "mode_info": f"time-based windows: {windows_ms}ms",
        "n_raw_events": len(all_events),
        "n_deduped": len(events)
    }
    return events, stats


def analyze(sigma: float, mode: str = "tick", windows_ms: list[int] = None,
            rolling_ticks: int = 5):
    """Post-collection analysis."""
    if windows_ms is None:
        windows_ms = [100, 250, 500]

    print("\n" + "=" * 70)
    print("ANALYSIS")
    print("=" * 70)

    bn = state.binance_ticks
    pm = state.pm_ticks

    if len(bn) < 10 or len(pm) < 10:
        print("Not enough data")
        return

    duration_s = (bn[-1].ts_ms - state.start_ms) / 1000
    print(f"Duration: {duration_s:.1f}s")
    print(f"Binance ticks: {len(bn):,}")
    print(f"Polymarket ticks: {len(pm):,}")
    print(f"Detection mode: {mode}")

    # Detect events based on mode
    if mode == "tick":
        events, stats = detect_tick_events(bn, sigma)
    elif mode == "rolling":
        events, stats = detect_rolling_events(bn, sigma, rolling_ticks)
    else:  # time
        events, stats = detect_time_events(bn, sigma, windows_ms)

    # Print stats
    print(f"\nDetection: {stats.get('mode_info', mode)}")
    if "windows" in stats:
        for w, ws in stats["windows"].items():
            print(f"  {w}ms window: mean={ws['mean']:.3f}bps, std={ws['std']:.3f}bps, n={ws['n']:,}")
        print(f"  Raw events: {stats['n_raw_events']}, after dedup: {stats['n_deduped']}")
    else:
        print(f"  N: {stats.get('n_returns', 0):,}")
        print(f"  Mean: {stats.get('mean', 0):.3f} bps")
        print(f"  Std:  {stats.get('std', 0):.3f} bps")
        print(f"  Threshold: ±{stats.get('threshold_bps', 0):.3f} bps ({sigma}σ)")

    print(f"\nEvents (>{sigma}σ): {len(events)}")

    if not events:
        print("\nNo significant events found. Try lowering --sigma.")
        return

    # Count events by window type (for time mode)
    if mode == "time":
        window_counts = {}
        for _, _, _, window_label in events:
            window_counts[window_label] = window_counts.get(window_label, 0) + 1
        print("Events by window:")
        for w, cnt in sorted(window_counts.items()):
            print(f"  {w}: {cnt}")

    # For each event, find time until PM quote changed
    pm_sorted = sorted(pm, key=lambda t: t.ts_ms)
    reaction_times = []

    for ev in events:
        ev_ts, ev_ret, ev_mid = ev[0], ev[1], ev[2]
        window_label = ev[3] if len(ev) > 3 else "tick"

        # Find PM state just before event
        pm_before = None
        for tick in pm_sorted:
            if tick.ts_ms >= ev_ts:
                break
            pm_before = tick

        if pm_before is None:
            continue

        # Find first PM change after event
        for tick in pm_sorted:
            if tick.ts_ms <= ev_ts:
                continue
            if tick.bid != pm_before.bid or tick.ask != pm_before.ask:
                reaction_ms = tick.ts_ms - ev_ts
                direction = "UP" if ev_ret > 0 else "DOWN"
                pm_dir = "UP" if tick.mid > pm_before.mid else "DOWN" if tick.mid < pm_before.mid else "FLAT"
                reaction_times.append((reaction_ms, direction, pm_dir, ev_ret, ev_mid, window_label))
                break

    print(f"\nReactions measured: {len(reaction_times)} / {len(events)} events")

    if not reaction_times:
        print("No reactions detected.")
        return

    times = [r[0] for r in reaction_times]
    times_sorted = sorted(times)

    print(f"\nReaction Time Stats (ms):")
    print(f"  Min:    {min(times):,}")
    print(f"  Max:    {max(times):,}")
    print(f"  Mean:   {sum(times)/len(times):,.1f}")
    print(f"  Median: {times_sorted[len(times_sorted)//2]:,}")
    for p in [25, 75, 90, 95]:
        idx = min(int(len(times_sorted) * p / 100), len(times_sorted) - 1)
        print(f"  P{p}:    {times_sorted[idx]:,}")

    # Directional accuracy
    correct = sum(1 for r in reaction_times if r[1] == r[2])
    print(f"\nDirectional accuracy: {correct}/{len(reaction_times)} ({100*correct/len(reaction_times):.1f}%)")

    # Show some events
    print(f"\nSample events:")
    for i, item in enumerate(reaction_times[:15]):
        rt, bn_dir, pm_dir, ret_bps, mid = item[:5]
        window_label = item[5] if len(item) > 5 else ""
        window_str = f" [{window_label}]" if window_label else ""
        print(f"  BN {bn_dir:4} {ret_bps:+6.1f}bps @ ${mid:,.0f} → PM {pm_dir:4} in {rt:,}ms{window_str}")

    print("=" * 70)

    # Generate visualization
    visualize(events, reaction_times, sigma, mode)


def visualize(events: list, reaction_times: list, sigma: float, mode: str = "tick"):
    """Create Plotly visualization."""
    try:
        import plotly.graph_objects as go
        from plotly.subplots import make_subplots
    except ImportError:
        print("\nInstall plotly for visualization: pip install plotly")
        return

    bn = state.binance_ticks
    pm = state.pm_ticks
    start = state.start_ms

    # Time series
    bn_t = [(t.ts_ms - start) / 1000 for t in bn]
    bn_mid = [t.mid for t in bn]
    pm_t = [(t.ts_ms - start) / 1000 for t in pm]
    pm_mid = [t.mid for t in pm]

    # Event points - handle both old (ts, ret, mid) and new (ts, ret, mid, window_label) formats
    ev_times = [(ev[0] - start) / 1000 for ev in events]
    ev_mids = [ev[2] for ev in events]
    ev_rets = [ev[1] for ev in events]
    ev_labels = [ev[3] if len(ev) > 3 else "tick" for ev in events]

    # Color by window type for time mode, otherwise by direction
    if mode == "time":
        # Assign colors by window
        window_colors = {"100ms": "purple", "250ms": "blue", "500ms": "cyan"}
        ev_colors = [window_colors.get(label, "gray") for label in ev_labels]
        hover_texts = [f"{r:+.1f}bps [{label}]" for r, label in zip(ev_rets, ev_labels)]
    else:
        ev_colors = ['green' if ret > 0 else 'red' for ret in ev_rets]
        hover_texts = [f"{r:+.1f}bps" for r in ev_rets]

    # Reaction points on PM
    rx_pm_t = []
    rx_pm_mid = []
    rx_colors = []
    for item in reaction_times:
        rt_ms, bn_dir, pm_dir, ret_bps, ev_mid_price = item[:5]
        # Find the event timestamp
        for ev in events:
            e_ts, e_ret, e_mid = ev[0], ev[1], ev[2]
            if abs(e_ret - ret_bps) < 0.001 and abs(e_mid - ev_mid_price) < 0.01:
                response_ts = e_ts + rt_ms
                # Find PM mid at response time
                for tick in pm:
                    if tick.ts_ms >= response_ts:
                        rx_pm_t.append((response_ts - start) / 1000)
                        rx_pm_mid.append(tick.mid)
                        rx_colors.append('green' if pm_dir == "UP" else 'red' if pm_dir == "DOWN" else 'gray')
                        break
                break

    fig = make_subplots(
        rows=2, cols=1,
        shared_xaxes=True,
        vertical_spacing=0.08,
        subplot_titles=('Binance BTC Mid Price', 'Polymarket YES Mid (cents)'),
        row_heights=[0.5, 0.5]
    )

    # Binance mid price
    fig.add_trace(
        go.Scatter(x=bn_t, y=bn_mid, mode='lines', name='BN Mid',
                   line=dict(color='blue', width=1)),
        row=1, col=1
    )

    # Event markers on Binance
    fig.add_trace(
        go.Scatter(x=ev_times, y=ev_mids, mode='markers', name=f'Events (>{sigma}σ)',
                   marker=dict(color=ev_colors, size=10, symbol='triangle-up',
                               line=dict(width=1, color='black')),
                   text=hover_texts,
                   hovertemplate='%{text}<br>$%{y:,.0f}<br>t=%{x:.1f}s'),
        row=1, col=1
    )

    # Polymarket mid price
    fig.add_trace(
        go.Scatter(x=pm_t, y=pm_mid, mode='lines', name='PM Mid',
                   line=dict(color='orange', width=1)),
        row=2, col=1
    )

    # Reaction markers on Polymarket
    if rx_pm_t:
        fig.add_trace(
            go.Scatter(x=rx_pm_t, y=rx_pm_mid, mode='markers', name='PM Reactions',
                       marker=dict(color=rx_colors, size=10, symbol='star',
                                   line=dict(width=1, color='black')),
                       hovertemplate='%{y:.1f}c<br>t=%{x:.1f}s'),
            row=2, col=1
        )

    # Add vertical lines connecting events to reactions (limit to 50 for performance)
    for i, item in enumerate(reaction_times[:50]):
        rt_ms, bn_dir, pm_dir, ret_bps, ev_mid_price = item[:5]
        for ev in events:
            e_ts, e_ret, e_mid = ev[0], ev[1], ev[2]
            if abs(e_ret - ret_bps) < 0.001 and abs(e_mid - ev_mid_price) < 0.01:
                t_event = (e_ts - start) / 1000
                fig.add_vline(x=t_event, line_dash="dot", line_color="gray", opacity=0.3)
                break

    mode_str = {"tick": "single-tick", "rolling": "rolling-tick", "time": "time-window"}
    fig.update_layout(
        title=f"Reaction Time Analysis ({mode_str.get(mode, mode)} mode, {sigma}σ threshold, {len(events)} events)",
        height=700,
        showlegend=True,
        hovermode='x unified'
    )

    fig.update_xaxes(title_text="Time (seconds)", row=2, col=1)
    fig.update_yaxes(title_text="Price ($)", row=1, col=1)
    fig.update_yaxes(title_text="Probability (cents)", row=2, col=1)

    output_file = "reaction_analysis.html"
    fig.write_html(output_file)
    print(f"\nVisualization saved to: {output_file}")


async def run(duration: int):
    state.start_ms = int(time.time() * 1000)

    bn_task = asyncio.create_task(stream_binance())
    pm_task = asyncio.create_task(stream_polymarket())

    async def progress():
        start = time.monotonic()
        while not stop_event.is_set():
            await asyncio.sleep(10)
            elapsed = time.monotonic() - start
            print(f"  {elapsed:.0f}s: BN={len(state.binance_ticks):,}, PM={len(state.pm_ticks):,}")
            if elapsed >= duration:
                stop_event.set()

    prog_task = asyncio.create_task(progress())

    try:
        await asyncio.wait_for(asyncio.gather(bn_task, pm_task, prog_task, return_exceptions=True), timeout=duration + 5)
    except asyncio.TimeoutError:
        pass

    stop_event.set()
    for t in [bn_task, pm_task, prog_task]:
        t.cancel()
        try:
            await t
        except:
            pass


async def main(duration: int, sigma: float, mode: str = "time",
               windows_ms: list[int] = None, rolling_ticks: int = 5):
    if windows_ms is None:
        windows_ms = [100, 250, 500]

    signal.signal(signal.SIGINT, lambda *_: stop_event.set())
    signal.signal(signal.SIGTERM, lambda *_: stop_event.set())

    yes, no = await find_market()
    if not yes or not no:
        print("Failed to find market")
        sys.exit(1)

    state.yes_token, state.no_token = yes, no

    print(f"\nCollecting data for {duration}s...")
    print(f"Detection mode: {mode}")
    if mode == "time":
        print(f"Time windows: {windows_ms}ms")
    elif mode == "rolling":
        print(f"Rolling window: {rolling_ticks} ticks")
    print("-" * 70)

    await run(duration)

    print(f"\nCollection done. Binance: {len(state.binance_ticks):,}, PM: {len(state.pm_ticks):,}")
    analyze(sigma, mode, windows_ms, rolling_ticks)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("-d", "--duration", type=int, default=300, help="Seconds to collect (default: 300)")
    parser.add_argument("--sigma", type=float, default=2.0, help="Std dev threshold (default: 2.0)")
    parser.add_argument("--mode", choices=["tick", "rolling", "time"], default="time",
                        help="Detection mode: tick (single tick), rolling (N-tick window), time (time-based windows)")
    parser.add_argument("--windows", type=str, default="100,250,500",
                        help="Comma-separated window sizes in ms for time mode (default: 100,250,500)")
    parser.add_argument("--rolling-ticks", type=int, default=5,
                        help="Number of ticks for rolling mode (default: 5)")
    args = parser.parse_args()

    windows_ms = [int(w) for w in args.windows.split(",")]
    asyncio.run(main(args.duration, args.sigma, args.mode, windows_ms, args.rolling_ticks))
