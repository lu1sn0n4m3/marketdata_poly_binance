"""Tests for Application wiring."""

import os
import time
import queue
import pytest
from unittest.mock import Mock, MagicMock, patch

from tradingsystem.config import AppConfig
from tradingsystem.feeds import BinanceFeed, BinancePricerPoller, PricerEnrichment
from tradingsystem.caches import BinanceCache
from tradingsystem.types import MarketInfo


class TestAppConfig:
    """Tests for AppConfig."""

    def test_default_values(self):
        """Test default configuration values."""
        config = AppConfig()

        assert config.strategy_hz == 50
        assert config.base_spread_cents == 3
        assert config.max_position == 500
        assert config.gross_cap == 1000
        assert config.pm_stale_threshold_ms == 500
        assert config.bn_stale_threshold_ms == 1000

    def test_from_env(self):
        """Test loading from environment variables."""
        with patch.dict(os.environ, {
            "PM_PRIVATE_KEY": "0x1234",
            "STRATEGY_HZ": "50",
            "MAX_POSITION": "200",
        }):
            config = AppConfig.from_env()

            assert config.pm_private_key == "0x1234"
            assert config.strategy_hz == 50
            assert config.max_position == 200

    def test_validate_missing_private_key(self):
        """Test validation fails without private key."""
        config = AppConfig(pm_private_key="")
        errors = config.validate()

        assert len(errors) > 0
        assert any("PM_PRIVATE_KEY" in e for e in errors)

    def test_validate_invalid_strategy_hz(self):
        """Test validation fails with invalid strategy Hz."""
        config = AppConfig(pm_private_key="0x1234", strategy_hz=0)
        errors = config.validate()

        assert any("STRATEGY_HZ" in e for e in errors)

    def test_validate_success(self):
        """Test validation succeeds with valid config."""
        config = AppConfig(pm_private_key="0x1234")
        errors = config.validate()

        assert len(errors) == 0


class TestBinanceFeed:
    """Tests for BinanceFeed (WebSocket-based)."""

    @pytest.fixture
    def cache(self):
        """Create Binance cache."""
        return BinanceCache()

    def test_feed_updates_cache_via_handle_message(self, cache):
        """Test BinanceFeed parses WS messages and updates cache."""
        import orjson

        feed = BinanceFeed(cache=cache, symbol="BTCUSDT")

        # Simulate a bookTicker message
        msg = orjson.dumps({
            "stream": "btcusdt@bookTicker",
            "data": {
                "s": "BTCUSDT",
                "b": "100000.50",
                "B": "1.5",
                "a": "100001.00",
                "A": "2.0",
            },
        })
        feed._handle_message(msg)

        assert cache.has_data
        snapshot, _ = cache.get_latest()
        assert snapshot is not None
        assert snapshot.best_bid_px == 100000.50
        assert snapshot.best_ask_px == 100001.00

    def test_feed_tracks_trade_price(self, cache):
        """Test trade messages update last trade price."""
        import orjson

        feed = BinanceFeed(cache=cache, symbol="BTCUSDT")

        # Send a trade first
        trade_msg = orjson.dumps({
            "stream": "btcusdt@trade",
            "data": {"p": "99999.00", "q": "0.1", "m": True},
        })
        feed._handle_message(trade_msg)
        assert feed._last_trade_price == 99999.00

        # Then BBO publishes with the trade price
        bbo_msg = orjson.dumps({
            "stream": "btcusdt@bookTicker",
            "data": {"s": "BTCUSDT", "b": "100000.00", "B": "1.0", "a": "100001.00", "A": "1.0"},
        })
        feed._handle_message(bbo_msg)

        snapshot, _ = cache.get_latest()
        assert snapshot.last_trade_price == 99999.00

    def test_feed_merges_pricer_enrichment(self, cache):
        """Test BinanceFeed merges pricer enrichment into snapshots."""
        import orjson

        enrichment = PricerEnrichment()
        enrichment.update({
            "p_yes_fair": 0.55,
            "t_remaining_ms": 3600000,
            "open_price": 99500.0,
            "features": {"ewma_vol": 0.02},
        })

        feed = BinanceFeed(cache=cache, symbol="BTCUSDT", enrichment=enrichment)

        bbo_msg = orjson.dumps({
            "stream": "btcusdt@bookTicker",
            "data": {"s": "BTCUSDT", "b": "100000.00", "B": "1.0", "a": "100001.00", "A": "1.0"},
        })
        feed._handle_message(bbo_msg)

        snapshot, _ = cache.get_latest()
        assert snapshot.best_bid_px == 100000.00
        assert snapshot.p_yes == 0.55
        assert snapshot.t_remaining_ms == 3600000

    def test_feed_stats(self, cache):
        """Test feed stats tracking."""
        import orjson

        feed = BinanceFeed(cache=cache, symbol="BTCUSDT")

        # Send a few messages
        for _ in range(3):
            feed._handle_message(orjson.dumps({
                "stream": "btcusdt@bookTicker",
                "data": {"s": "BTCUSDT", "b": "100000.00", "B": "1.0", "a": "100001.00", "A": "1.0"},
            }))
        feed._handle_message(orjson.dumps({
            "stream": "btcusdt@trade",
            "data": {"p": "100000.50", "q": "0.1", "m": False},
        }))

        stats = feed.stats
        assert stats["bbo_count"] == 3
        assert stats["trade_count"] == 1
        assert stats["error_count"] == 0

    def test_feed_handles_bad_messages(self, cache):
        """Test feed handles malformed messages gracefully."""
        feed = BinanceFeed(cache=cache, symbol="BTCUSDT")

        # Bad JSON
        feed._handle_message(b"not json")
        assert feed.stats["error_count"] == 1

        # Missing data field
        import orjson
        feed._handle_message(orjson.dumps({"stream": "btcusdt@bookTicker"}))
        # Should not crash, just skip


class TestBinancePricerPoller:
    """Tests for BinancePricerPoller (HTTP enrichment)."""

    def test_poller_starts_and_stops(self):
        """Test poller lifecycle."""
        enrichment = PricerEnrichment()
        poller = BinancePricerPoller(
            enrichment=enrichment,
            url="http://localhost:9999/nonexistent",
            poll_hz=10,
        )

        poller.start()
        assert poller.is_running

        time.sleep(0.2)
        poller.stop()
        assert not poller.is_running

    def test_poller_handles_connection_errors(self):
        """Test poller handles connection errors gracefully."""
        enrichment = PricerEnrichment()
        poller = BinancePricerPoller(
            enrichment=enrichment,
            url="http://localhost:9999/nonexistent",
            poll_hz=100,
            timeout_s=0.1,
        )

        poller.start()
        time.sleep(0.3)
        poller.stop()

        stats = poller.stats
        assert stats["error_count"] > 0
        assert stats["last_error"] == "connection_error"

    @patch("tradingsystem.feeds.binance_feed.requests.get")
    def test_poller_updates_enrichment_on_success(self, mock_get):
        """Test poller updates enrichment on successful response."""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "symbol": "BTCUSDT",
            "bbo_bid": 100000.0,
            "bbo_ask": 100001.0,
            "p_yes_fair": 0.55,
            "t_remaining_ms": 3600000,
        }
        mock_get.return_value = mock_response

        enrichment = PricerEnrichment()
        poller = BinancePricerPoller(
            enrichment=enrichment,
            url="http://localhost:8080/snapshot",
            poll_hz=10,
        )

        poller.start()
        time.sleep(0.3)
        poller.stop()

        data = enrichment.get()
        assert data.get("p_yes_fair") == 0.55

    def test_poller_stats(self):
        """Test poller stats tracking."""
        enrichment = PricerEnrichment()
        poller = BinancePricerPoller(
            enrichment=enrichment,
            url="http://localhost:9999/nonexistent",
            poll_hz=50,
        )

        poller.start()
        time.sleep(0.1)
        poller.stop()

        stats = poller.stats
        assert "poll_count" in stats
        assert "success_count" in stats
        assert "error_count" in stats
        assert "success_rate" in stats


class TestMMApplicationComponents:
    """Tests for MM Application component wiring."""

    def test_strategy_input_construction(self):
        """Test strategy input is built correctly from caches."""
        from tradingsystem.caches import PolymarketCache, BinanceCache
        from tradingsystem.types import InventoryState
        from tradingsystem.strategy import StrategyInput

        pm_cache = PolymarketCache()
        bn_cache = BinanceCache()

        # Empty caches
        pm_book, pm_seq = pm_cache.get_latest()
        bn_snap, bn_seq = bn_cache.get_latest()

        assert pm_book is None
        assert bn_snap is None

        # Update PM cache
        pm_cache.set_market("market_1", "yes_token", "no_token")
        pm_cache.update_from_ws(
            yes_bbo=(48, 100, 52, 100),
            no_bbo=(48, 100, 52, 100),
        )

        pm_book, pm_seq = pm_cache.get_latest()
        assert pm_book is not None
        assert pm_book.yes_mid == 50

        # Update BN cache
        bn_cache.update_from_poll({
            "symbol": "BTCUSDT",
            "bbo_bid": 100000.0,
            "bbo_ask": 100001.0,
            "p_yes_fair": 0.55,
            "t_remaining_ms": 3600000,
        })

        bn_snap, bn_seq = bn_cache.get_latest()
        assert bn_snap is not None
        assert bn_snap.p_yes == 0.55
        assert bn_snap.p_yes_cents == 55

    def test_event_queue_integration(self):
        """Test event queue between components."""
        from tradingsystem.types import FillEvent, ExecutorEventType, Token, Side, now_ms

        event_queue = queue.Queue(maxsize=100)

        # Simulate fill event from User WS
        fill = FillEvent(
            event_type=ExecutorEventType.FILL,
            ts_local_ms=now_ms(),
            server_order_id="order_123",
            token=Token.YES,
            side=Side.BUY,
            price=50,
            size=10,
            fee=0.0,
            ts_exchange=now_ms(),
            trade_id="trade_queue_test_1",
            role="MAKER",
        )
        event_queue.put(fill)

        # Verify it can be retrieved
        event = event_queue.get(timeout=1.0)
        assert isinstance(event, FillEvent)
        assert event.server_order_id == "order_123"
        assert event.size == 10
        assert event.trade_id == "trade_queue_test_1"
        assert event.fill_id == "SETTLED:MAKER:trade_queue_test_1:order_123"

    def test_intent_mailbox_integration(self):
        """Test intent mailbox coalescing."""
        from tradingsystem.strategy import IntentMailbox
        from tradingsystem.types import DesiredQuoteSet, now_ms

        mailbox = IntentMailbox()

        # Put multiple intents rapidly
        for i in range(10):
            intent = DesiredQuoteSet.stop(ts=now_ms() + i, reason=f"TEST_{i}")
            mailbox.put(intent)

        # Only latest should be retrieved
        intent = mailbox.get()
        assert intent is not None
        assert "TEST_9" in intent.reason_flags

        # Second get should return None
        assert mailbox.get() is None


class TestMarketInfo:
    """Tests for MarketInfo."""

    def test_time_remaining_ms(self):
        """Test time remaining calculation."""
        import time

        # Market ending in 10 seconds
        end_time = int(time.time() * 1000) + 10_000

        market = MarketInfo(
            condition_id="condition_123",
            question="Test?",
            slug="test-slug",
            yes_token_id="yes_token",
            no_token_id="no_token",
            end_time_utc_ms=end_time,
        )

        remaining = market.time_remaining_ms
        assert 9000 < remaining <= 10000

    def test_expired_market(self):
        """Test expired market returns 0."""
        import time

        # Market ended 10 seconds ago
        end_time = int(time.time() * 1000) - 10_000

        market = MarketInfo(
            condition_id="condition_123",
            question="Test?",
            slug="test-slug",
            yes_token_id="yes_token",
            no_token_id="no_token",
            end_time_utc_ms=end_time,
        )

        assert market.time_remaining_ms == 0
