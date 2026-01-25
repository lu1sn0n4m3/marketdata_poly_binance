"""Tests for WebSocket clients - PM Market and PM User."""

import pytest
import queue
import time
import threading
import orjson

from tradingsystem.feeds import ExponentialBackoff, ThreadedWsClient
from tradingsystem.feeds import PolymarketMarketFeed, PolymarketUserFeed
from tradingsystem.caches import PolymarketCache
from tradingsystem.types import (
    Token,
    Side,
    OrderStatus,
    ExecutorEventType,
    OrderAckEvent,
    CancelAckEvent,
    FillEvent,
)


class TestExponentialBackoff:
    """Tests for ExponentialBackoff."""

    def test_initial_delay(self):
        """Test initial delay is min_seconds."""
        backoff = ExponentialBackoff(min_seconds=1.0, max_seconds=60.0)
        delay = backoff.next_delay()
        # With jitter, should be between 0.5 and 1.0
        assert 0.5 <= delay <= 1.0

    def test_exponential_growth(self):
        """Test delays grow exponentially."""
        backoff = ExponentialBackoff(min_seconds=1.0, max_seconds=60.0)
        delays = [backoff.next_delay() for _ in range(5)]
        # Each delay should be roughly double the previous (with jitter)
        # After 4 attempts: 1, 2, 4, 8, 16 (before jitter)
        assert delays[-1] > delays[0]

    def test_max_cap(self):
        """Test delays are capped at max_seconds."""
        backoff = ExponentialBackoff(min_seconds=1.0, max_seconds=5.0)
        # Run many iterations to hit cap
        for _ in range(10):
            delay = backoff.next_delay()
            # With jitter (50-100%), max should be 5.0
            assert delay <= 5.0

    def test_reset(self):
        """Test reset brings delay back to minimum."""
        backoff = ExponentialBackoff(min_seconds=1.0, max_seconds=60.0)
        # Build up delay
        for _ in range(5):
            backoff.next_delay()
        # Reset
        backoff.reset()
        delay = backoff.next_delay()
        assert 0.5 <= delay <= 1.0


class TestPolymarketMarketFeed:
    """Tests for PolymarketMarketFeed message parsing."""

    @pytest.fixture
    def cache(self):
        """Create fresh PolymarketCache."""
        return PolymarketCache()

    @pytest.fixture
    def client(self, cache):
        """Create market WS client with mock cache."""
        client = PolymarketMarketFeed(pm_cache=cache)
        client.set_tokens("yes_token_123", "no_token_456", "market_abc")
        return client

    def test_set_tokens(self, client, cache):
        """Test token configuration."""
        assert client._yes_token_id == "yes_token_123"
        assert client._no_token_id == "no_token_456"
        assert client._market_id == "market_abc"

    def test_handle_book_yes(self, client, cache):
        """Test handling book snapshot for YES token."""
        # Polymarket order book sorting:
        # BIDS: sorted ascending (0.01, 0.02, ...), best bid (highest) is LAST
        # ASKS: sorted descending (0.99, 0.98, ...), best ask (lowest) is LAST
        msg = {
            "event_type": "book",
            "asset_id": "yes_token_123",
            "bids": [
                {"price": "0.48", "size": "50"},
                {"price": "0.50", "size": "100"},  # Best bid (highest, last)
            ],
            "asks": [
                {"price": "0.54", "size": "150"},  # Higher price first
                {"price": "0.52", "size": "200"},  # Best ask (lowest, last)
            ],
        }
        client._handle_message(orjson.dumps(msg))

        snapshot, _ = cache.get_latest()
        assert snapshot is not None
        assert snapshot.yes_top.best_bid_px == 50
        assert snapshot.yes_top.best_bid_sz == 100
        assert snapshot.yes_top.best_ask_px == 52
        assert snapshot.yes_top.best_ask_sz == 200

    def test_handle_book_no(self, client, cache):
        """Test handling book snapshot for NO token."""
        msg = {
            "event_type": "book",
            "asset_id": "no_token_456",
            "bids": [{"price": "0.45", "size": "80"}],
            "asks": [{"price": "0.48", "size": "120"}],
        }
        client._handle_message(orjson.dumps(msg))

        snapshot, _ = cache.get_latest()
        assert snapshot is not None
        assert snapshot.no_top.best_bid_px == 45
        assert snapshot.no_top.best_ask_px == 48

    def test_handle_price_change(self, client, cache):
        """Test handling price change event."""
        # First set initial book
        book_msg = {
            "event_type": "book",
            "asset_id": "yes_token_123",
            "bids": [{"price": "0.50", "size": "100"}],
            "asks": [{"price": "0.52", "size": "200"}],
        }
        client._handle_message(orjson.dumps(book_msg))

        # Then price change
        change_msg = {
            "event_type": "price_change",
            "price_changes": [{
                "asset_id": "yes_token_123",
                "best_bid": "0.51",
                "best_ask": "0.53",
            }],
        }
        client._handle_message(orjson.dumps(change_msg))

        snapshot, _ = cache.get_latest()
        assert snapshot.yes_top.best_bid_px == 51
        assert snapshot.yes_top.best_ask_px == 53

    def test_handle_best_bid_ask(self, client, cache):
        """Test handling best_bid_ask event."""
        msg = {
            "event_type": "best_bid_ask",
            "asset_id": "yes_token_123",
            "best_bid": "0.55",
            "best_ask": "0.57",
        }
        client._handle_message(orjson.dumps(msg))

        snapshot, _ = cache.get_latest()
        assert snapshot.yes_top.best_bid_px == 55
        assert snapshot.yes_top.best_ask_px == 57

    def test_handle_array_of_events(self, client, cache):
        """Test handling array of events."""
        msgs = [
            {
                "event_type": "best_bid_ask",
                "asset_id": "yes_token_123",
                "best_bid": "0.50",
                "best_ask": "0.52",
            },
            {
                "event_type": "best_bid_ask",
                "asset_id": "no_token_456",
                "best_bid": "0.48",
                "best_ask": "0.50",
            },
        ]
        client._handle_message(orjson.dumps(msgs))

        snapshot, seq = cache.get_latest()
        assert seq == 2  # Two updates
        assert snapshot.yes_top.best_bid_px == 50
        assert snapshot.no_top.best_bid_px == 48

    def test_ignore_unknown_asset(self, client, cache):
        """Test that unknown asset IDs are ignored."""
        msg = {
            "event_type": "best_bid_ask",
            "asset_id": "unknown_token",
            "best_bid": "0.50",
            "best_ask": "0.52",
        }
        client._handle_message(orjson.dumps(msg))

        # Cache should be updated but with default values since unknown token
        snapshot, _ = cache.get_latest()
        # Default YES values unchanged
        assert snapshot.yes_top.best_bid_px == 0
        assert snapshot.yes_top.best_ask_px == 100

    def test_price_str_to_cents(self):
        """Test price string to cents conversion."""
        assert PolymarketMarketFeed._price_str_to_cents("0.50") == 50
        assert PolymarketMarketFeed._price_str_to_cents("0.01") == 1
        assert PolymarketMarketFeed._price_str_to_cents("0.99") == 99
        assert PolymarketMarketFeed._price_str_to_cents(None) == 0
        assert PolymarketMarketFeed._price_str_to_cents("") == 0

    def test_parse_level(self):
        """Test price level parsing."""
        level = {"price": "0.55", "size": "150"}
        px, sz = PolymarketMarketFeed._parse_level(level)
        assert px == 55
        assert sz == 150


class TestPolymarketUserFeed:
    """Tests for PolymarketUserFeed message parsing."""

    @pytest.fixture
    def event_queue(self):
        """Create event queue."""
        return queue.Queue(maxsize=1000)

    @pytest.fixture
    def reconnect_flag(self):
        """Create reconnect tracking."""
        return {"called": False}

    @pytest.fixture
    def client(self, event_queue, reconnect_flag):
        """Create user WS client."""
        def on_reconnect():
            reconnect_flag["called"] = True

        client = PolymarketUserFeed(
            event_queue=event_queue,
            api_key="test_key",
            api_secret="test_secret",
            passphrase="test_pass",
            maker_address="0xOurWalletAddress",  # Our wallet address for MAKER fill matching
            on_reconnect=on_reconnect,
        )
        client.set_markets(["market_123"])
        return client

    def test_set_auth(self, client):
        """Test auth configuration."""
        client.set_auth("new_key", "new_secret", "new_pass")
        assert client._api_key == "new_key"
        assert client._api_secret == "new_secret"
        assert client._passphrase == "new_pass"

    def test_handle_placement_event(self, client, event_queue):
        """Test handling order placement event."""
        msg = {
            "event_type": "order",
            "type": "PLACEMENT",
            "id": "order_123",
            "price": "0.50",
            "original_size": "100",
            "size_matched": "0",
            "outcome": "Yes",
            "side": "BUY",
        }
        client._handle_message(orjson.dumps(msg))

        assert not event_queue.empty()
        event = event_queue.get_nowait()
        assert isinstance(event, OrderAckEvent)
        assert event.client_order_id == "order_123"
        assert event.status == OrderStatus.WORKING
        assert event.side == Side.BUY
        assert event.price == 50
        assert event.size == 100
        assert event.token == Token.YES

    def test_handle_update_event_partial_fill(self, client, event_queue):
        """Test handling order update with partial fill."""
        msg = {
            "event_type": "order",
            "type": "UPDATE",
            "id": "order_123",
            "price": "0.50",
            "original_size": "100",
            "size_matched": "25",
            "outcome": "Yes",
            "side": "BUY",
        }
        client._handle_message(orjson.dumps(msg))

        event = event_queue.get_nowait()
        assert isinstance(event, OrderAckEvent)
        assert event.status == OrderStatus.PARTIALLY_FILLED

    def test_handle_update_event_full_fill(self, client, event_queue):
        """Test handling order update with full fill."""
        msg = {
            "event_type": "order",
            "type": "UPDATE",
            "id": "order_123",
            "price": "0.50",
            "original_size": "100",
            "size_matched": "100",
            "outcome": "No",
            "side": "SELL",
        }
        client._handle_message(orjson.dumps(msg))

        event = event_queue.get_nowait()
        assert isinstance(event, OrderAckEvent)
        assert event.status == OrderStatus.FILLED
        assert event.token == Token.NO
        assert event.side == Side.SELL

    def test_handle_cancellation_event(self, client, event_queue):
        """Test handling order cancellation event."""
        msg = {
            "event_type": "order",
            "type": "CANCELLATION",
            "id": "order_123",
        }
        client._handle_message(orjson.dumps(msg))

        event = event_queue.get_nowait()
        assert isinstance(event, CancelAckEvent)
        assert event.server_order_id == "order_123"
        assert event.success is True

    def test_handle_trade_event_taker(self, client, event_queue):
        """Test handling trade fill event as taker."""
        msg = {
            "event_type": "trade",
            "id": "trade_abc123",  # Unique trade ID for deduplication
            "status": "CONFIRMED",
            "trader_side": "TAKER",  # We are the taker
            "taker_order_id": "order_456",
            "price": "0.55",
            "size": "50",
            "side": "BUY",
            "outcome": "Yes",
            "timestamp": "1700000000000",
            "maker_orders": [],
        }
        client._handle_message(orjson.dumps(msg))

        event = event_queue.get_nowait()
        assert isinstance(event, FillEvent)
        assert event.server_order_id == "order_456"
        assert event.price == 55
        assert event.size == 50
        assert event.side == Side.BUY
        assert event.token == Token.YES
        assert event.trade_id == "trade_abc123"
        assert event.role == "TAKER"
        assert event.fill_id == "SETTLED:TAKER:trade_abc123"

    def test_handle_trade_event_maker(self, client, event_queue):
        """Test handling trade fill event as maker."""
        # When trader_side=MAKER, we are the maker, not the taker
        # Only our orders (matching maker_address) should generate fills
        msg = {
            "event_type": "trade",
            "id": "trade_xyz789",  # Unique trade ID for deduplication
            "status": "MINED",
            "trader_side": "MAKER",  # We are a maker in this trade
            "taker_order_id": "taker_123",  # Someone else's order
            "price": "0.55",
            "size": "50",
            "side": "BUY",
            "outcome": "Yes",
            "timestamp": "1700000000000",
            "maker_orders": [
                {
                    "order_id": "maker_456",
                    "owner": "some_uuid",
                    "maker_address": "0xOurWalletAddress",  # Our order (matches our wallet)
                    "price": "0.55",
                    "matched_amount": "50",
                    "outcome": "Yes",
                    "side": "SELL",  # Maker's original order side
                },
                {
                    "order_id": "other_maker_789",
                    "owner": "someone_else",
                    "maker_address": "0xSomeoneElseWallet",  # Not our order
                    "price": "0.55",
                    "matched_amount": "30",
                    "outcome": "Yes",
                    "side": "SELL",
                },
            ],
        }
        client._handle_message(orjson.dumps(msg))

        # Should only get ONE event: our maker fill (not taker, not other makers)
        events = []
        while not event_queue.empty():
            events.append(event_queue.get_nowait())

        assert len(events) == 1

        maker_event = events[0]
        assert maker_event.server_order_id == "maker_456"
        assert maker_event.side == Side.SELL  # Maker's side from the data
        assert maker_event.trade_id == "trade_xyz789"
        assert maker_event.role == "MAKER"
        assert maker_event.fill_id == "SETTLED:MAKER:trade_xyz789:maker_456"

    def test_ignore_unconfirmed_trade(self, client, event_queue):
        """Test that unconfirmed trades are ignored."""
        msg = {
            "event_type": "trade",
            "status": "PENDING",
            "taker_order_id": "order_123",
            "price": "0.55",
            "size": "50",
        }
        client._handle_message(orjson.dumps(msg))

        assert event_queue.empty()

    def test_enqueue_blocks_on_full_queue(self, event_queue):
        """Test that enqueue blocks when queue is full."""
        small_queue = queue.Queue(maxsize=1)
        client = PolymarketUserFeed(
            event_queue=small_queue,
            api_key="key",
            api_secret="secret",
            passphrase="pass",
        )
        client.set_markets(["market"])

        # Fill the queue
        msg1 = {
            "event_type": "order",
            "type": "PLACEMENT",
            "id": "order_1",
            "price": "0.50",
            "original_size": "100",
            "size_matched": "0",
            "outcome": "Yes",
            "side": "BUY",
        }
        client._handle_message(orjson.dumps(msg1))
        assert small_queue.full()

        # Second message should still be queued (blocking)
        # Run in thread to avoid blocking test
        def send_second():
            msg2 = {
                "event_type": "order",
                "type": "PLACEMENT",
                "id": "order_2",
                "price": "0.51",
                "original_size": "50",
                "size_matched": "0",
                "outcome": "No",
                "side": "SELL",
            }
            client._handle_message(orjson.dumps(msg2))

        thread = threading.Thread(target=send_second)
        thread.start()

        # Let it block briefly
        time.sleep(0.1)

        # Consume first to unblock
        small_queue.get_nowait()

        # Wait for second to complete
        thread.join(timeout=1.0)
        assert not thread.is_alive()

        # Second event should now be in queue
        event = small_queue.get_nowait()
        assert event.client_order_id == "order_2"

    def test_outcome_to_token(self):
        """Test outcome string to Token conversion."""
        assert PolymarketUserFeed._outcome_to_token("Yes") == Token.YES
        assert PolymarketUserFeed._outcome_to_token("yes") == Token.YES
        assert PolymarketUserFeed._outcome_to_token("Up") == Token.YES
        assert PolymarketUserFeed._outcome_to_token("No") == Token.NO
        assert PolymarketUserFeed._outcome_to_token("Down") == Token.NO
        assert PolymarketUserFeed._outcome_to_token(None) == Token.NO

    def test_parse_side(self):
        """Test side string parsing."""
        assert PolymarketUserFeed._parse_side("BUY") == Side.BUY
        assert PolymarketUserFeed._parse_side("buy") == Side.BUY
        assert PolymarketUserFeed._parse_side("SELL") == Side.SELL
        assert PolymarketUserFeed._parse_side("anything") == Side.SELL

    def test_on_disconnect_triggers_callback(self, client, reconnect_flag):
        """Test that disconnect triggers reconnect callback."""
        # Simulate first connect
        client._on_connect()
        assert client._first_connect is False

        # Simulate disconnect
        client._on_disconnect()
        assert reconnect_flag["called"] is True

    def test_first_connect_no_callback(self, client, reconnect_flag):
        """Test that first disconnect doesn't trigger callback."""
        # First disconnect (before any connect)
        client._on_disconnect()
        assert reconnect_flag["called"] is False


class TestSubscriptionMessages:
    """Tests for subscription message formatting."""

    def test_market_ws_subscription_format(self):
        """Test market WS subscription message format."""
        cache = PolymarketCache()
        client = PolymarketMarketFeed(pm_cache=cache)
        client.set_tokens("yes_abc", "no_xyz", "market_123")

        # Can't easily test _subscribe without connection, but we can verify config
        assert client._yes_token_id == "yes_abc"
        assert client._no_token_id == "no_xyz"

    def test_user_ws_subscription_format(self):
        """Test user WS subscription message format."""
        q = queue.Queue()
        client = PolymarketUserFeed(
            event_queue=q,
            api_key="key123",
            api_secret="secret456",
            passphrase="pass789",
        )
        client.set_markets(["market_a", "market_b"])

        assert client._api_key == "key123"
        assert client._api_secret == "secret456"
        assert client._passphrase == "pass789"
        assert client._market_ids == ["market_a", "market_b"]
