#!/usr/bin/env python3
"""
Test script for isolated fill tracking via Polymarket User WebSocket.

This script connects to the Polymarket user WebSocket and logs all fill events,
tracking inventory based on fills to verify the parsing logic is correct.

Usage:
    export PM_PRIVATE_KEY=0x...
    export PM_FUNDER=0x...
    python -m tradingsystem.test_fill_tracking

Then manually place orders via CLI or Polymarket UI and watch the fills come in.
"""

import logging
import os
import queue
import signal
import sys
import time
from dataclasses import dataclass, field
from typing import Optional

import orjson

from .clients import PolymarketRestClient, GammaClient, BitcoinHourlyMarketFinder
from .feeds.websocket_base import ThreadedWsClient
from .types import Token, Side, now_ms
import asyncio

# Configure logging
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s | %(levelname)-5s | %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("FillTracker")


# Default WebSocket URL
PM_USER_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/user"


@dataclass
class Fill:
    """A single fill record."""
    fill_id: str  # Unique ID for deduplication
    trade_id: str
    order_id: str
    role: str  # "TAKER" or "MAKER"
    token: Token
    side: Side
    size: float
    price: float
    status: str  # MATCHED, MINED, CONFIRMED
    ts: int


@dataclass
class Inventory:
    """Simple inventory tracker."""
    yes: float = 0.0
    no: float = 0.0

    @property
    def net(self) -> float:
        return self.yes - self.no

    def update(self, token: Token, side: Side, size: float):
        if token == Token.YES:
            if side == Side.BUY:
                self.yes += size
            else:
                self.yes -= size
        else:
            if side == Side.BUY:
                self.no += size
            else:
                self.no -= size


class FillTracker:
    """
    Tracks fills with proper deduplication.

    NOTE: The User WebSocket sends trades where we are involved.
    - When trader_side=TAKER: top-level fields are ours
    - When trader_side=MAKER: we are ONE of potentially many makers
      Must filter maker_orders by owner to find our fills only.
    """

    def __init__(self, my_owner_id: str):
        self.my_owner_id = my_owner_id
        self.fills: dict[str, Fill] = {}  # fill_id -> Fill
        self.inventory = Inventory()

    def process_trade_event(self, data: dict) -> list[Fill]:
        """
        Process a trade event and extract fills for our account.

        The User WS sends trades where we are involved:
        - trader_side == "TAKER": we are the taker, use top-level fields
        - trader_side == "MAKER": we are ONE of the makers, filter by owner

        Returns list of new fills (not previously seen).
        """
        trade_id = data.get("id", "")
        status = data.get("status", "")
        trader_side = data.get("trader_side", "")
        ts = now_ms()

        new_fills = []

        if trader_side == "TAKER":
            # We are the taker - use top-level fields
            taker_order_id = data.get("taker_order_id", "")
            fill_id = f"TAKER:{trade_id}"

            if fill_id not in self.fills:
                token = self._parse_token(data.get("outcome"))
                side = self._parse_side(data.get("side", ""))
                size = float(data.get("size", 0))
                price = float(data.get("price", 0))

                fill = Fill(
                    fill_id=fill_id,
                    trade_id=trade_id,
                    order_id=taker_order_id,
                    role="TAKER",
                    token=token,
                    side=side,
                    size=size,
                    price=price,
                    status=status,
                    ts=ts,
                )
                self.fills[fill_id] = fill
                self.inventory.update(token, side, size)
                new_fills.append(fill)

                logger.info(
                    f"[NEW FILL] {fill.role} {fill.side.name} {fill.size:.2f}x{fill.token.name} "
                    f"@{fill.price*100:.1f}c | trade={trade_id[:16]}..."
                )
            else:
                self.fills[fill_id].status = status
                logger.debug(f"[STATUS UPDATE] {fill_id} -> {status}")

        elif trader_side == "MAKER":
            # We are ONE of the makers - filter to find only our orders
            # maker_orders contains ALL makers in this trade, not just us
            for maker in data.get("maker_orders", []):
                # Only process our orders (match by owner)
                if maker.get("owner") != self.my_owner_id:
                    continue
                maker_order_id = maker.get("order_id", "")
                fill_id = f"MAKER:{trade_id}:{maker_order_id}"

                if fill_id not in self.fills:
                    token = self._parse_token(maker.get("outcome"))
                    side = self._parse_side(maker.get("side", ""))
                    size = float(maker.get("matched_amount", 0))
                    price = float(maker.get("price", 0))

                    fill = Fill(
                        fill_id=fill_id,
                        trade_id=trade_id,
                        order_id=maker_order_id,
                        role="MAKER",
                        token=token,
                        side=side,
                        size=size,
                        price=price,
                        status=status,
                        ts=ts,
                    )
                    self.fills[fill_id] = fill
                    self.inventory.update(token, side, size)
                    new_fills.append(fill)

                    logger.info(
                        f"[NEW FILL] {fill.role} {fill.side.name} {fill.size:.2f}x{fill.token.name} "
                        f"@{fill.price*100:.1f}c | order={maker_order_id[:16]}..."
                    )
                else:
                    self.fills[fill_id].status = status
                    logger.debug(f"[STATUS UPDATE] {fill_id} -> {status}")
        else:
            # Log unexpected trader_side for debugging
            logger.warning(f"[TRADE] Unknown trader_side={trader_side} for trade={trade_id[:16]}...")

        return new_fills

    def get_summary(self) -> dict:
        """Get current state summary."""
        total_bought_yes = sum(f.size for f in self.fills.values()
                               if f.token == Token.YES and f.side == Side.BUY)
        total_sold_yes = sum(f.size for f in self.fills.values()
                             if f.token == Token.YES and f.side == Side.SELL)
        total_bought_no = sum(f.size for f in self.fills.values()
                              if f.token == Token.NO and f.side == Side.BUY)
        total_sold_no = sum(f.size for f in self.fills.values()
                            if f.token == Token.NO and f.side == Side.SELL)

        return {
            "fills_count": len(self.fills),
            "inventory": {
                "yes": self.inventory.yes,
                "no": self.inventory.no,
                "net": self.inventory.net,
            },
            "volume": {
                "bought_yes": total_bought_yes,
                "sold_yes": total_sold_yes,
                "bought_no": total_bought_no,
                "sold_no": total_sold_no,
            },
        }

    @staticmethod
    def _parse_token(outcome: Optional[str]) -> Token:
        if outcome and outcome.lower() in ("yes", "up"):
            return Token.YES
        return Token.NO

    @staticmethod
    def _parse_side(side_str: str) -> Side:
        return Side.BUY if side_str.upper() == "BUY" else Side.SELL


class TestUserFeed(ThreadedWsClient):
    """Simplified user feed for testing."""

    def __init__(
        self,
        fill_tracker: FillTracker,
        api_key: str,
        api_secret: str,
        passphrase: str,
        market_ids: list[str],
    ):
        super().__init__(
            ws_url=PM_USER_WS_URL,
            name="TestUserFeed",
            ping_interval=20,
            ping_timeout=60,
        )
        self._fill_tracker = fill_tracker
        self._api_key = api_key
        self._api_secret = api_secret
        self._passphrase = passphrase
        self._market_ids = market_ids
        self._debug_file = None

    def start(self) -> None:
        """Start feed and open debug log."""
        from datetime import datetime
        os.makedirs("debug", exist_ok=True)
        ts_str = datetime.now().strftime("%Y%m%d_%H%M%S")
        debug_path = f"debug/fill_test_{ts_str}.jsonl"
        self._debug_file = open(debug_path, "w")
        logger.info(f"Debug log: {debug_path}")
        super().start()

    def stop(self, timeout: float = 5.0) -> None:
        """Stop feed and close debug log."""
        super().stop(timeout)
        if self._debug_file:
            self._debug_file.close()
            self._debug_file = None

    def _subscribe(self) -> None:
        """Send subscription message."""
        msg = {
            "type": "USER",
            "auth": {
                "apikey": self._api_key,
                "secret": self._api_secret,
                "passphrase": self._passphrase,
            },
            "markets": self._market_ids,
        }
        logger.info(f"[SUBSCRIBE] Sending subscription for markets: {self._market_ids}")
        logger.info(f"[SUBSCRIBE] API key present: {bool(self._api_key)}, secret present: {bool(self._api_secret)}")
        raw_msg = orjson.dumps(msg)
        logger.info(f"[SUBSCRIBE] Raw message (first 200 chars): {raw_msg[:200]}")
        self._send(raw_msg)
        logger.info(f"[SUBSCRIBE] Subscription sent!")

    def _handle_message(self, data: bytes) -> None:
        """Handle incoming message."""
        try:
            msg = orjson.loads(data)

            # Save raw message to debug file
            if self._debug_file:
                from .types import now_ms
                debug_entry = {"ts": now_ms(), "raw": msg}
                self._debug_file.write(orjson.dumps(debug_entry).decode() + "\n")
                self._debug_file.flush()

            # LOG ALL RAW MESSAGES for debugging
            logger.info(f"[RAW MSG] {data[:500].decode('utf-8', errors='replace')}")

            # Handle array of events
            if isinstance(msg, list):
                for item in msg:
                    self._process_event(item)
            else:
                self._process_event(msg)

        except Exception as e:
            logger.error(f"Parse error: {e}")

    def _process_event(self, data: dict) -> None:
        """Process a single event."""
        event_type = data.get("event_type")

        if event_type == "order":
            # Log order events
            order_type = data.get("type", "")
            order_id = data.get("id", "")[:20]
            side = data.get("side", "")
            outcome = data.get("outcome", "")
            price = data.get("price", "")
            size = data.get("original_size", "")

            logger.info(
                f"[ORDER] {order_type}: {side} {size}x{outcome} @{price} "
                f"order={order_id}..."
            )

        elif event_type == "trade":
            status = data.get("status", "")
            trader_side = data.get("trader_side", "")

            # Log raw trade event at INFO for debugging
            logger.info(
                f"[TRADE] status={status} trader_side={trader_side} "
                f"outcome={data.get('outcome')} side={data.get('side')} "
                f"size={data.get('size')} price={data.get('price')}"
            )

            # Only process confirmed fills
            if status in ("CONFIRMED", "MINED", "MATCHED"):
                new_fills = self._fill_tracker.process_trade_event(data)

                if new_fills:
                    # Log updated inventory
                    inv = self._fill_tracker.inventory
                    logger.info(
                        f"[INVENTORY] YES={inv.yes:.2f} NO={inv.no:.2f} NET={inv.net:.2f}"
                    )

        else:
            # Log any other event types we might be missing
            logger.info(f"[OTHER] event_type={event_type} data_keys={list(data.keys())}")

    def _on_connect(self) -> None:
        logger.info("=" * 40)
        logger.info("[CONNECTED] Connected to Polymarket User WebSocket!")
        logger.info("=" * 40)

    def _on_disconnect(self) -> None:
        logger.warning("[DISCONNECTED] Disconnected from Polymarket User WebSocket")


def load_credentials_from_env_file(env_path: str) -> dict:
    """Load credentials from a .env file."""
    creds = {}
    if os.path.exists(env_path):
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" in line:
                    key, value = line.split("=", 1)
                    creds[key.strip()] = value.strip().strip('"').strip("'")
    return creds


def main():
    # Load credentials from deploy/prod.env
    env_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "deploy", "prod.env"
    )
    creds = load_credentials_from_env_file(env_path)

    private_key = creds.get("PM_PRIVATE_KEY", "") or os.environ.get("PM_PRIVATE_KEY", "")
    funder = creds.get("PM_FUNDER", "") or os.environ.get("PM_FUNDER", "")

    if not private_key:
        logger.error(f"PM_PRIVATE_KEY not found in {env_path} or environment")
        sys.exit(1)

    logger.info(f"Loaded credentials from {env_path}")

    # Auto-discover current Bitcoin hourly market
    logger.info("Finding current Bitcoin hourly market...")
    gamma = GammaClient()
    finder = BitcoinHourlyMarketFinder(gamma)
    market_info = asyncio.run(finder.find_current_market())

    if market_info:
        market_id = market_info.condition_id
        logger.info(f"Found market: {market_info.slug}")
    else:
        # Fallback to environment or default
        market_id = creds.get("PM_MARKET_ID") or os.environ.get(
            "PM_MARKET_ID",
            "0x626aeb637c0ed67f3a8d237efdc7e1ed2dadb52803fc2b42ce5ff6348484fcb3"
        )
        logger.warning(f"No active market found, using fallback: {market_id[:20]}...")

    logger.info("=" * 60)
    logger.info("FILL TRACKING TEST")
    logger.info("=" * 60)
    logger.info(f"Market: {market_id}")
    logger.info("=" * 60)
    logger.info("")
    logger.info("Initializing REST client to get API credentials...")

    # Initialize REST client to get API credentials
    rest_client = PolymarketRestClient(
        private_key=private_key,
        funder=funder,
    )
    api_key, api_secret, passphrase = rest_client.api_credentials

    logger.info(f"API key (owner_id): {api_key}")
    logger.info("")
    logger.info("Starting WebSocket connection...")
    logger.info("Place orders manually via CLI or Polymarket UI to test fill tracking")
    logger.info("")

    # Create fill tracker with owner_id for filtering maker orders
    fill_tracker = FillTracker(my_owner_id=api_key)

    # Create WebSocket feed
    feed = TestUserFeed(
        fill_tracker=fill_tracker,
        api_key=api_key,
        api_secret=api_secret,
        passphrase=passphrase,
        market_ids=[market_id],
    )

    # Signal handler
    stop_flag = [False]

    def signal_handler(signum, frame):
        logger.info("Signal received, stopping...")
        stop_flag[0] = True

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    try:
        logger.info("Starting WebSocket feed...")
        feed.start()
        logger.info("Feed started, waiting 3s for connection...")
        time.sleep(3)  # Give time to connect
        logger.info(f"Feed connected: {feed.connected}")
        logger.info("")
        logger.info("Waiting for fills... (Ctrl+C to stop)")
        logger.info("Execute trades on Polymarket NOW and watch for [RAW MSG] logs")
        logger.info("")

        # Print summary every 10 seconds
        last_summary_time = 0
        while not stop_flag[0]:
            time.sleep(1.0)

            now = time.time()
            if now - last_summary_time >= 10:
                summary = fill_tracker.get_summary()
                inv = summary["inventory"]
                vol = summary["volume"]
                logger.info("-" * 40)
                logger.info(f"SUMMARY: {summary['fills_count']} fills")
                logger.info(f"Inventory: YES={inv['yes']:.2f} NO={inv['no']:.2f} NET={inv['net']:.2f}")
                logger.info(
                    f"Volume: bought_yes={vol['bought_yes']:.2f} sold_yes={vol['sold_yes']:.2f} "
                    f"bought_no={vol['bought_no']:.2f} sold_no={vol['sold_no']:.2f}"
                )
                logger.info("-" * 40)
                last_summary_time = now

    except Exception as e:
        logger.exception(f"Error: {e}")
    finally:
        logger.info("")
        logger.info("=" * 60)
        logger.info("FINAL SUMMARY")
        logger.info("=" * 60)

        summary = fill_tracker.get_summary()
        inv = summary["inventory"]
        vol = summary["volume"]

        logger.info(f"Total Fills: {summary['fills_count']}")
        logger.info(f"Inventory: YES={inv['yes']:.2f} NO={inv['no']:.2f} NET={inv['net']:.2f}")
        logger.info(f"Volume Breakdown:")
        logger.info(f"  Bought YES: {vol['bought_yes']:.2f}")
        logger.info(f"  Sold YES:   {vol['sold_yes']:.2f}")
        logger.info(f"  Bought NO:  {vol['bought_no']:.2f}")
        logger.info(f"  Sold NO:    {vol['sold_no']:.2f}")

        logger.info("")
        logger.info("All fills:")
        for fill_id, fill in fill_tracker.fills.items():
            logger.info(
                f"  {fill.role:5} {fill.side.name:4} {fill.size:6.2f}x{fill.token.name:3} "
                f"@{fill.price*100:5.1f}c | {fill.status:9} | {fill_id[:30]}..."
            )

        logger.info("=" * 60)

        feed.stop()
        logger.info("Test complete.")


if __name__ == "__main__":
    main()
