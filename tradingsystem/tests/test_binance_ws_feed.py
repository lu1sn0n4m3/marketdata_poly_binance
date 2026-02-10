#!/usr/bin/env python3
"""
Test script: Validate Binance public WebSocket streams.

Connects to Binance combined stream (bookTicker + trade) for BTCUSDT
and prints messages to verify format, latency, and throughput.

Usage:
    source .venv/bin/activate
    python tradingsystem/tests/test_binance_ws_feed.py
"""

import json
import time
import threading
import sys
import os

# Ensure imports work
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import websocket

# Binance combined stream URL
BINANCE_WS_URL = "wss://stream.binance.com:9443/stream?streams=btcusdt@bookTicker/btcusdt@trade"

# Stats
stats = {
    "bbo_count": 0,
    "trade_count": 0,
    "first_msg_ts": None,
    "last_bbo": None,
    "last_trade": None,
    "errors": 0,
}


def on_open(ws):
    print(f"[CONNECTED] to {BINANCE_WS_URL[:60]}...")
    print("Waiting for messages...\n")


def on_message(ws, message):
    ts = time.time()

    if stats["first_msg_ts"] is None:
        stats["first_msg_ts"] = ts

    try:
        msg = json.loads(message)
    except json.JSONDecodeError as e:
        stats["errors"] += 1
        print(f"[ERROR] JSON parse: {e}")
        return

    stream = msg.get("stream", "")
    data = msg.get("data", {})

    if "bookTicker" in stream:
        stats["bbo_count"] += 1
        bid = float(data.get("b", 0))
        ask = float(data.get("a", 0))
        bid_sz = float(data.get("B", 0))
        ask_sz = float(data.get("A", 0))
        stats["last_bbo"] = {"bid": bid, "ask": ask, "bid_sz": bid_sz, "ask_sz": ask_sz}

        if stats["bbo_count"] <= 5 or stats["bbo_count"] % 100 == 0:
            spread = ask - bid
            print(
                f"  [BBO #{stats['bbo_count']:>5}] "
                f"bid={bid:>10.2f} ({bid_sz:.4f})  "
                f"ask={ask:>10.2f} ({ask_sz:.4f})  "
                f"spread={spread:.2f}"
            )

    elif "trade" in stream:
        stats["trade_count"] += 1
        price = float(data.get("p", 0))
        qty = float(data.get("q", 0))
        trade_ts_ms = data.get("T", 0)
        is_buyer_maker = data.get("m", False)
        side = "SELL" if is_buyer_maker else "BUY"

        # Compute exchange-to-local latency
        local_ms = int(ts * 1000)
        latency_ms = local_ms - trade_ts_ms if trade_ts_ms else 0

        stats["last_trade"] = {"price": price, "qty": qty, "side": side}

        if stats["trade_count"] <= 5 or stats["trade_count"] % 200 == 0:
            print(
                f"  [TRADE #{stats['trade_count']:>5}] "
                f"price={price:>10.2f}  qty={qty:.6f}  "
                f"side={side:>4}  latency={latency_ms}ms"
            )


def on_error(ws, error):
    stats["errors"] += 1
    print(f"[ERROR] {error}")


def on_close(ws, close_status_code, close_msg):
    print(f"\n[CLOSED] code={close_status_code} msg={close_msg}")


def run_test(duration_seconds: int = 15):
    """Run WebSocket test for specified duration."""
    print("=" * 70)
    print("  BINANCE WEBSOCKET STREAM TEST")
    print("=" * 70)
    print(f"\nURL: {BINANCE_WS_URL}")
    print(f"Duration: {duration_seconds}s")
    print(f"Streams: btcusdt@bookTicker, btcusdt@trade\n")

    ws = websocket.WebSocketApp(
        BINANCE_WS_URL,
        on_open=on_open,
        on_message=on_message,
        on_error=on_error,
        on_close=on_close,
    )

    # Run in thread so we can stop after duration
    ws_thread = threading.Thread(
        target=lambda: ws.run_forever(ping_interval=20, ping_timeout=10),
        daemon=True,
    )
    ws_thread.start()

    # Wait for duration
    try:
        time.sleep(duration_seconds)
    except KeyboardInterrupt:
        print("\n[INTERRUPTED]")

    ws.close()
    ws_thread.join(timeout=3.0)

    # Print summary
    elapsed = time.time() - (stats["first_msg_ts"] or time.time())
    print("\n" + "=" * 70)
    print("  RESULTS")
    print("=" * 70)
    print(f"\n  Duration:       {elapsed:.1f}s")
    print(f"  BBO updates:    {stats['bbo_count']} ({stats['bbo_count']/max(1,elapsed):.1f}/sec)")
    print(f"  Trade updates:  {stats['trade_count']} ({stats['trade_count']/max(1,elapsed):.1f}/sec)")
    print(f"  Errors:         {stats['errors']}")

    if stats["last_bbo"]:
        bbo = stats["last_bbo"]
        print(f"\n  Last BBO:  bid={bbo['bid']:.2f}  ask={bbo['ask']:.2f}")

    if stats["last_trade"]:
        trade = stats["last_trade"]
        print(f"  Last Trade: price={trade['price']:.2f}  side={trade['side']}")

    # Validate
    ok = stats["bbo_count"] > 0 and stats["trade_count"] > 0 and stats["errors"] == 0
    print(f"\n  {'PASS' if ok else 'FAIL'}")
    print("=" * 70)
    return ok


if __name__ == "__main__":
    duration = int(sys.argv[1]) if len(sys.argv) > 1 else 15
    success = run_test(duration)
    sys.exit(0 if success else 1)
