"""
Integration tests for WebSocket clients - tests real connections.

Run with: pytest tradingsystem/tests/test_ws_integration.py -v -s

Note: These tests connect to real WebSocket endpoints.
- Market WS: No auth required, tests with current Bitcoin hourly market
- User WS: Requires credentials from environment or .env file

Environment variables:
- PM_PRIVATE_KEY: Wallet private key (0x prefixed)
- PM_FUNDER: Funder address (0x prefixed)
- PM_SIGNATURE_TYPE: 1 for EOA, 2 for proxy (default: 1)
"""

import os
import sys
import time
import queue
import logging
import pytest
from pathlib import Path

# Add project root to path
project_root = Path(__file__).parent.parent.parent.parent
sys.path.insert(0, str(project_root))

import json

from tradingsystem.caches import PolymarketCache
from tradingsystem.feeds import PolymarketMarketFeed, PolymarketUserFeed
from tradingsystem.market_finder import build_market_slug, get_current_hour_et
from tradingsystem.gamma_client import GammaClient

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger(__name__)


def load_env():
    """Load environment variables from .env file if available."""
    try:
        from dotenv import load_dotenv
        env_path = project_root / "deploy" / "prod.env.template"
        if env_path.exists():
            load_dotenv(env_path)
            logger.info(f"Loaded env from {env_path}")
    except ImportError:
        pass


def get_polymarket_credentials():
    """Get Polymarket credentials, deriving API creds if needed."""
    private_key = os.getenv("PM_PRIVATE_KEY")
    funder = os.getenv("PM_FUNDER", "")
    sig_type = int(os.getenv("PM_SIGNATURE_TYPE", "1"))

    if not private_key:
        return None

    # Derive API credentials using py-clob-client
    try:
        from py_clob_client.client import ClobClient

        client = ClobClient(
            host="https://clob.polymarket.com",
            chain_id=137,  # Polygon mainnet
            key=private_key,
            funder=funder,
            signature_type=sig_type,
        )

        # Derive or get L2 credentials
        creds = client.derive_api_key()

        return {
            "api_key": creds.api_key,
            "api_secret": creds.api_secret,
            "passphrase": creds.passphrase,
        }
    except Exception as e:
        logger.error(f"Failed to derive credentials: {e}")
        return None


class TestMarketWsIntegration:
    """Integration tests for PM Market WebSocket."""

    @pytest.fixture(autouse=True)
    def setup(self):
        """Setup for each test."""
        load_env()

    @pytest.mark.integration
    def test_market_ws_connects_and_receives_data(self):
        """Test real connection to market WebSocket."""
        cache = PolymarketCache()
        client = PolymarketMarketFeed(pm_cache=cache)

        # Get current market info (slug uses start hour)
        start_time = get_current_hour_et()
        slug = build_market_slug(start_time)
        logger.info(f"Testing with market slug: {slug}")

        # Fetch market info from Gamma API
        import asyncio

        async def get_market():
            gamma = GammaClient()
            try:
                event = await gamma.get_event_by_slug(slug)
                if event and event.get("markets"):
                    market = event["markets"][0]
                    tokens = market.get("tokens", [])
                    clob_ids = market.get("clobTokenIds")

                    # Parse clobTokenIds if it's a JSON string
                    if isinstance(clob_ids, str):
                        clob_ids = json.loads(clob_ids)
                    if not clob_ids:
                        clob_ids = []

                    yes_token = None
                    no_token = None

                    for t in tokens:
                        outcome = (t.get("outcome") or "").lower()
                        if outcome in ("yes", "up"):
                            yes_token = t.get("token_id")
                        elif outcome in ("no", "down"):
                            no_token = t.get("token_id")

                    if not yes_token and len(clob_ids) >= 2:
                        yes_token = clob_ids[0]
                        no_token = clob_ids[1]

                    return {
                        "market_id": market.get("conditionId", ""),
                        "yes_token": yes_token,
                        "no_token": no_token,
                    }
            finally:
                await gamma.close()
            return None

        market_info = asyncio.run(get_market())

        if not market_info or not market_info["yes_token"]:
            pytest.skip(f"Could not find market for slug: {slug}")

        logger.info(f"Market: {market_info['market_id'][:30]}...")
        logger.info(f"YES token: {market_info['yes_token'][:30]}...")
        logger.info(f"NO token: {market_info['no_token'][:30]}...")

        # Configure and start client
        client.set_tokens(
            yes_token_id=market_info["yes_token"],
            no_token_id=market_info["no_token"],
            market_id=market_info["market_id"],
        )

        client.start()

        try:
            # Wait for data
            logger.info("Waiting for market data...")
            max_wait = 10  # seconds
            start = time.time()

            while time.time() - start < max_wait:
                if cache.has_data:
                    snapshot, seq = cache.get_latest()
                    logger.info(f"Received data! seq={seq}")
                    logger.info(f"  YES: bid={snapshot.yes_top.best_bid_px}c, ask={snapshot.yes_top.best_ask_px}c")
                    logger.info(f"  NO:  bid={snapshot.no_top.best_bid_px}c, ask={snapshot.no_top.best_ask_px}c")
                    logger.info(f"  Age: {cache.get_age_ms()}ms")

                    # Verify we got real data
                    assert snapshot.yes_top.best_bid_px > 0 or snapshot.yes_top.best_ask_px < 100
                    assert seq > 0
                    return  # Success

                time.sleep(0.5)

            pytest.fail("No market data received within timeout")

        finally:
            client.stop()


class TestUserWsIntegration:
    """Integration tests for PM User WebSocket."""

    @pytest.fixture(autouse=True)
    def setup(self):
        """Setup for each test."""
        load_env()

    @pytest.mark.integration
    def test_user_ws_connects_with_auth(self):
        """Test authenticated connection to user WebSocket."""
        creds = get_polymarket_credentials()

        if not creds:
            pytest.skip("No Polymarket credentials available")

        logger.info("Got credentials, testing user WS connection...")

        event_queue = queue.Queue(maxsize=1000)
        reconnect_called = {"value": False}

        def on_reconnect():
            reconnect_called["value"] = True
            logger.info("Reconnect callback triggered")

        client = PolymarketUserFeed(
            event_queue=event_queue,
            api_key=creds["api_key"],
            api_secret=creds["api_secret"],
            passphrase=creds["passphrase"],
            on_reconnect=on_reconnect,
        )

        # Get a market to subscribe to (slug uses start hour)
        start_time = get_current_hour_et()
        slug = build_market_slug(start_time)

        import asyncio

        async def get_market_id():
            gamma = GammaClient()
            try:
                event = await gamma.get_event_by_slug(slug)
                if event and event.get("markets"):
                    return event["markets"][0].get("conditionId", "")
            finally:
                await gamma.close()
            return None

        market_id = asyncio.run(get_market_id())

        if not market_id:
            pytest.skip(f"Could not find market for slug: {slug}")

        client.set_markets([market_id])
        client.start()

        try:
            # Wait for connection
            logger.info("Waiting for user WS connection...")
            max_wait = 10
            start = time.time()

            while time.time() - start < max_wait:
                if client.connected:
                    logger.info("User WS connected successfully!")

                    # Stay connected a bit longer to verify stability
                    time.sleep(2)

                    if client.connected:
                        logger.info("Connection stable")
                        return  # Success

                time.sleep(0.5)

            # Connection may not happen if auth fails
            if not client.connected:
                pytest.fail("User WS did not connect within timeout")

        finally:
            client.stop()


class TestBinanceIntegration:
    """Integration test for Binance data fetching."""

    @pytest.mark.integration
    def test_binance_snapshot_polling(self):
        """Test fetching Binance snapshot (simulated with BinanceCache)."""
        from tradingsystem.caches import BinanceCache

        cache = BinanceCache()

        # Simulate a polled response (in real app, this comes from binance_pricer service)
        mock_response = {
            "symbol": "BTCUSDT",
            "bbo_bid": 100000.50,
            "bbo_ask": 100001.50,
            "last_trade_price": 100001.00,
            "open_price": 99500.00,
            "hour_start_ts_ms": int(time.time() * 1000) - 1800000,  # 30 min ago
            "hour_end_ts_ms": int(time.time() * 1000) + 1800000,  # 30 min from now
            "t_remaining_ms": 1800000,
            "features": {
                "return_1m": 0.005,
                "ewma_vol": 0.02,
            },
            "p_yes_fair": 0.55,
        }

        seq = cache.update_from_poll(mock_response)

        assert seq == 1
        assert cache.has_data

        snapshot, _ = cache.get_latest()
        assert snapshot.symbol == "BTCUSDT"
        assert snapshot.mid_px == pytest.approx(100001.0)
        assert snapshot.p_yes == 0.55
        assert snapshot.p_yes_cents == 55

        logger.info("Binance cache test passed")


if __name__ == "__main__":
    # Run integration tests directly
    pytest.main([__file__, "-v", "-s", "-m", "integration"])
