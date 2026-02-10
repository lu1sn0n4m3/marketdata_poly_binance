#!/usr/bin/env python3
"""
Measure Polymarket WS feed latency.

1. Discovers current Bitcoin hourly market via Gamma API
2. Connects to market WS and subscribes
3. Dumps raw messages to inspect timestamp fields
4. Measures server-to-local latency if timestamps are present

Usage:
    source .venv/bin/activate
    python tradingsystem/tests/test_pm_latency.py [duration_seconds]
"""

import asyncio
import json
import sys
import threading
import time
import statistics

import orjson
import websocket
import aiohttp

# --- Step 1: Discover market via Gamma API ---

GAMMA_URL = "https://gamma-api.polymarket.com"
PM_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"

MONTH_NAMES = [
    "january", "february", "march", "april", "may", "june",
    "july", "august", "september", "october", "november", "december",
]


def build_slug() -> str:
    """Build market slug from current ET time."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    ET = ZoneInfo("America/New_York")
    now = datetime.now(ET).replace(minute=0, second=0, microsecond=0)

    month = MONTH_NAMES[now.month - 1]
    day = now.day
    h = now.hour

    if h == 0:
        hour_12, period = 12, "am"
    elif h < 12:
        hour_12, period = h, "am"
    elif h == 12:
        hour_12, period = 12, "pm"
    else:
        hour_12, period = h - 12, "pm"

    return f"bitcoin-up-or-down-{month}-{day}-{hour_12}{period}-et"


async def discover_market() -> dict:
    """Find current market via Gamma API, return {yes_token, no_token, condition_id, question}."""
    slug = build_slug()
    print(f"  Slug: {slug}")

    async with aiohttp.ClientSession() as session:
        url = f"{GAMMA_URL}/events/slug/{slug}"
        async with session.get(url) as resp:
            if resp.status != 200:
                raise RuntimeError(f"Gamma API returned {resp.status} for slug {slug}")
            event = await resp.json()

    markets = event.get("markets", [])
    if not markets:
        raise RuntimeError(f"No markets found for {slug}")

    market = markets[0]
    tokens = market.get("tokens") or []

    yes_token = no_token = None
    for t in tokens:
        outcome = (t.get("outcome") or "").lower()
        if outcome in ("yes", "up"):
            yes_token = t["token_id"]
        elif outcome in ("no", "down"):
            no_token = t["token_id"]

    if not yes_token or not no_token:
        clob_ids = market.get("clobTokenIds")
        if isinstance(clob_ids, str):
            clob_ids = json.loads(clob_ids)
        if clob_ids and len(clob_ids) >= 2:
            yes_token, no_token = clob_ids[0], clob_ids[1]

    if not yes_token or not no_token:
        raise RuntimeError("Could not find YES/NO token IDs")

    return {
        "yes_token": yes_token,
        "no_token": no_token,
        "condition_id": market.get("conditionId", ""),
        "question": market.get("question", event.get("title", "")),
    }


# --- Step 2: Connect to WS and measure ---

latencies_ms: list[float] = []
msg_count = 0
event_types: dict[str, int] = {}
raw_samples: list[str] = []  # First N raw messages for inspection
SAMPLE_LIMIT = 10


def on_open(ws):
    print(f"\n[CONNECTED] to {PM_WS_URL}")

    # Subscribe
    sub = {
        "type": "MARKET",
        "assets_ids": [market_info["yes_token"], market_info["no_token"]],
        "custom_feature_enabled": False,
    }
    ws.send(json.dumps(sub))
    print(f"[SUBSCRIBED] to {market_info['condition_id'][:30]}...")
    print(f"  Question: {market_info['question']}")
    print(f"\nDumping first {SAMPLE_LIMIT} raw messages to inspect fields...\n")


def on_message(ws, message):
    global msg_count
    local_ms = time.time() * 1000

    try:
        msg = orjson.loads(message)
    except Exception:
        return

    # Handle arrays
    events = msg if isinstance(msg, list) else [msg]

    for evt in events:
        msg_count += 1
        et = evt.get("event_type", "unknown")
        event_types[et] = event_types.get(et, 0) + 1

        # Dump raw samples
        if len(raw_samples) < SAMPLE_LIMIT:
            # Pretty-print with truncated large fields
            display = {}
            for k, v in evt.items():
                if k in ("bids", "asks") and isinstance(v, list):
                    display[k] = f"[{len(v)} levels]"
                elif k == "price_changes" and isinstance(v, list):
                    display[k] = v[:2]  # Show first 2
                else:
                    display[k] = v
            raw_samples.append(json.dumps(display, indent=2))
            print(f"--- Message #{msg_count} ({et}) ---")
            print(raw_samples[-1])
            print()

        # Try to find ANY timestamp field
        for ts_key in ("timestamp", "T", "t", "time", "event_time", "E",
                       "server_time", "ts", "created_at", "updated_at"):
            ts_val = evt.get(ts_key)
            if ts_val is not None:
                try:
                    server_ms = float(ts_val)
                    # If it looks like seconds, convert
                    if server_ms < 1e12:
                        server_ms *= 1000
                    latency = local_ms - server_ms
                    latencies_ms.append(latency)
                except (ValueError, TypeError):
                    pass
                break  # Use first found timestamp

        # Also check nested data
        for nested_key in ("price_changes",):
            nested = evt.get(nested_key, [])
            if isinstance(nested, list):
                for item in nested:
                    if isinstance(item, dict):
                        for ts_key in ("timestamp", "T", "t", "time", "ts"):
                            ts_val = item.get(ts_key)
                            if ts_val is not None:
                                try:
                                    server_ms = float(ts_val)
                                    if server_ms < 1e12:
                                        server_ms *= 1000
                                    latency = local_ms - server_ms
                                    latencies_ms.append(latency)
                                except (ValueError, TypeError):
                                    pass
                                break


def on_error(ws, error):
    print(f"[ERROR] {error}")


def on_close(ws, code, msg):
    print(f"\n[CLOSED] code={code} msg={msg}")


def run(duration: int):
    print("=" * 60)
    print("  POLYMARKET WS FEED LATENCY TEST")
    print("=" * 60)
    print(f"\n  Duration: {duration}s\n")

    # Discover market
    print("  Discovering current market...")
    global market_info
    market_info = asyncio.run(discover_market())

    ws = websocket.WebSocketApp(
        PM_WS_URL,
        on_open=on_open,
        on_message=on_message,
        on_error=on_error,
        on_close=on_close,
    )

    t = threading.Thread(
        target=lambda: ws.run_forever(ping_interval=20, ping_timeout=10),
        daemon=True,
    )
    t.start()

    try:
        time.sleep(duration)
    except KeyboardInterrupt:
        pass

    ws.close()
    t.join(timeout=3.0)

    # --- Report ---
    print("\n" + "=" * 60)
    print("  RESULTS")
    print("=" * 60)

    print(f"\n  Total messages: {msg_count}")
    print(f"  Duration: {duration}s")
    print(f"  Rate: {msg_count / max(1, duration):.1f} msg/sec")

    print(f"\n  Event types:")
    for et, count in sorted(event_types.items(), key=lambda x: -x[1]):
        print(f"    {et:20s}: {count}")

    if latencies_ms:
        s = sorted(latencies_ms)
        n = len(s)
        print(f"\n  Latency ({n} samples with server timestamps):")
        print(f"    Min:    {min(s):>8.1f} ms")
        print(f"    P5:     {s[int(n*0.05)]:>8.1f} ms")
        print(f"    Median: {statistics.median(s):>8.1f} ms")
        print(f"    Mean:   {statistics.mean(s):>8.1f} ms")
        print(f"    P95:    {s[int(n*0.95)]:>8.1f} ms")
        print(f"    P99:    {s[min(int(n*0.99), n-1)]:>8.1f} ms")
        print(f"    Max:    {max(s):>8.1f} ms")
        jitter = s[int(n * 0.95)] - s[int(n * 0.05)]
        print(f"    Jitter (P95-P5): {jitter:.1f} ms")
    else:
        print(f"\n  No server timestamps found in messages.")
        print(f"  Timestamp fields checked: timestamp, T, t, time, event_time, E, server_time, ts")

    print("\n" + "=" * 60)


market_info = {}

if __name__ == "__main__":
    duration = int(sys.argv[1]) if len(sys.argv) > 1 else 15
    run(duration)
