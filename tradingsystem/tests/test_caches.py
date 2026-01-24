"""Tests for snapshot caches - PolymarketCache and BinanceCache."""

import pytest
import threading
import time
from tradingsystem.snapshot_store import LatestSnapshotStore
from tradingsystem.caches import PolymarketCache, BinanceCache
from tradingsystem.types import (
    MarketSnapshotMeta,
    PolymarketBookTop,
    PolymarketBookSnapshot,
    BinanceSnapshot,
    now_ms,
)


class TestLatestSnapshotStore:
    """Tests for the generic LatestSnapshotStore."""

    def test_initial_state(self):
        """Test initial state is empty."""
        store = LatestSnapshotStore[str]()
        snapshot, seq = store.read_latest()
        assert snapshot is None
        assert seq == 0
        assert store.has_data is False

    def test_publish_and_read(self):
        """Test basic publish and read."""
        store = LatestSnapshotStore[str]()
        seq = store.publish("test_snapshot")
        assert seq == 1
        snapshot, read_seq = store.read_latest()
        assert snapshot == "test_snapshot"
        assert read_seq == 1
        assert store.has_data is True

    def test_sequence_monotonicity(self):
        """Test that sequence numbers increase monotonically."""
        store = LatestSnapshotStore[int]()
        for i in range(10):
            seq = store.publish(i)
            assert seq == i + 1

    def test_latest_overwrites_previous(self):
        """Test that new snapshots overwrite old ones."""
        store = LatestSnapshotStore[str]()
        store.publish("first")
        store.publish("second")
        store.publish("third")
        snapshot, seq = store.read_latest()
        assert snapshot == "third"
        assert seq == 3

    def test_get_seq_without_read(self):
        """Test getting sequence without full read."""
        store = LatestSnapshotStore[str]()
        store.publish("data")
        store.publish("more_data")
        assert store.get_seq() == 2

    def test_concurrent_reads_writes(self):
        """Test thread safety with concurrent access."""
        store = LatestSnapshotStore[int]()
        errors = []
        read_values = []

        def writer():
            for i in range(100):
                store.publish(i)
                time.sleep(0.001)

        def reader():
            for _ in range(100):
                try:
                    snapshot, seq = store.read_latest()
                    if snapshot is not None:
                        read_values.append((snapshot, seq))
                except Exception as e:
                    errors.append(e)
                time.sleep(0.001)

        writer_thread = threading.Thread(target=writer)
        reader_threads = [threading.Thread(target=reader) for _ in range(3)]

        writer_thread.start()
        for t in reader_threads:
            t.start()

        writer_thread.join()
        for t in reader_threads:
            t.join()

        assert len(errors) == 0, f"Errors during concurrent access: {errors}"
        # All reads should have seen consistent data
        for snapshot, seq in read_values:
            assert snapshot is not None
            assert seq > 0


class TestPolymarketCache:
    """Tests for PolymarketCache."""

    def test_initial_state(self):
        """Test initial state."""
        cache = PolymarketCache()
        assert cache.has_data is False
        assert cache.seq == 0
        snapshot, seq = cache.get_latest()
        assert snapshot is None

    def test_set_market(self):
        """Test setting market identity."""
        cache = PolymarketCache()
        cache.set_market("condition_123", "yes_token", "no_token")
        # Update to trigger snapshot creation
        cache.update_from_ws(
            yes_bbo=(50, 100, 52, 100),
            no_bbo=(48, 100, 50, 100),
        )
        snapshot, _ = cache.get_latest()
        assert snapshot.market_id == "condition_123"
        assert snapshot.yes_token_id == "yes_token"
        assert snapshot.no_token_id == "no_token"

    def test_update_from_ws(self):
        """Test updating from WebSocket data."""
        cache = PolymarketCache()
        seq = cache.update_from_ws(
            yes_bbo=(50, 100, 52, 150),
            no_bbo=(48, 80, 50, 120),
            market_id="test_market",
            yes_token_id="yes_123",
            no_token_id="no_456",
        )
        assert seq == 1
        snapshot, _ = cache.get_latest()
        assert snapshot.yes_top.best_bid_px == 50
        assert snapshot.yes_top.best_bid_sz == 100
        assert snapshot.yes_top.best_ask_px == 52
        assert snapshot.yes_top.best_ask_sz == 150
        assert snapshot.no_top.best_bid_px == 48
        assert snapshot.no_top.best_ask_px == 50

    def test_update_yes_book_only(self):
        """Test partial update for YES book only."""
        cache = PolymarketCache()
        cache.update_from_ws(
            yes_bbo=(50, 100, 52, 100),
            no_bbo=(48, 100, 50, 100),
        )
        cache.update_yes_book(55, 200, 57, 200)
        snapshot, _ = cache.get_latest()
        # YES updated
        assert snapshot.yes_top.best_bid_px == 55
        assert snapshot.yes_top.best_bid_sz == 200
        # NO preserved
        assert snapshot.no_top.best_bid_px == 48
        assert snapshot.no_top.best_ask_px == 50

    def test_update_no_book_only(self):
        """Test partial update for NO book only."""
        cache = PolymarketCache()
        cache.update_from_ws(
            yes_bbo=(50, 100, 52, 100),
            no_bbo=(48, 100, 50, 100),
        )
        cache.update_no_book(45, 300, 47, 300)
        snapshot, _ = cache.get_latest()
        # YES preserved
        assert snapshot.yes_top.best_bid_px == 50
        # NO updated
        assert snapshot.no_top.best_bid_px == 45
        assert snapshot.no_top.best_bid_sz == 300

    def test_default_bbo_when_none(self):
        """Test default BBO values when data is None."""
        cache = PolymarketCache()
        cache.update_from_ws(yes_bbo=None, no_bbo=None)
        snapshot, _ = cache.get_latest()
        # Defaults: no bid (0), ask at 100
        assert snapshot.yes_top.best_bid_px == 0
        assert snapshot.yes_top.best_ask_px == 100
        assert snapshot.no_top.best_bid_px == 0
        assert snapshot.no_top.best_ask_px == 100

    def test_age_and_staleness(self):
        """Test age calculation and staleness detection."""
        cache = PolymarketCache()
        cache.update_from_ws(yes_bbo=(50, 100, 52, 100), no_bbo=None)

        # Immediately after update, age should be small
        age = cache.get_age_ms()
        assert age < 100  # Should be very fresh

        assert cache.is_stale(threshold_ms=500) is False

    def test_staleness_with_no_data(self):
        """Test staleness returns True when no data."""
        cache = PolymarketCache()
        assert cache.is_stale() is True
        assert cache.get_age_ms() == 999999

    def test_mid_history(self):
        """Test mid price history tracking."""
        cache = PolymarketCache(history_size=5)
        for bid in range(40, 60, 2):
            cache.update_from_ws(
                yes_bbo=(bid, 100, bid + 2, 100),
                no_bbo=None,
            )

        history = cache.get_mid_history()
        assert len(history) == 5  # Limited by history_size
        # Check that mids are recorded (mid = (bid + ask) / 2)
        mids = [mid for _, mid in history]
        assert all(40 <= m <= 60 for m in mids)

    def test_recent_mid_change(self):
        """Test mid price change calculation."""
        cache = PolymarketCache()
        cache.update_from_ws(yes_bbo=(50, 100, 52, 100), no_bbo=None)
        time.sleep(0.01)
        cache.update_from_ws(yes_bbo=(54, 100, 56, 100), no_bbo=None)

        change = cache.get_recent_mid_change(lookback_ms=1000)
        # Mid went from 51 to 55
        assert change == 4

    def test_clear(self):
        """Test clearing cache."""
        cache = PolymarketCache()
        cache.update_from_ws(yes_bbo=(50, 100, 52, 100), no_bbo=None)
        assert cache.has_data is True

        cache.clear()
        assert cache.has_data is False
        assert cache.seq == 0


class TestBinanceCache:
    """Tests for BinanceCache."""

    def test_initial_state(self):
        """Test initial state."""
        cache = BinanceCache()
        assert cache.has_data is False
        assert cache.seq == 0

    def test_update_from_poll(self):
        """Test updating from polled data."""
        cache = BinanceCache()
        raw = {
            "symbol": "BTCUSDT",
            "bbo_bid": 100000.0,
            "bbo_ask": 100010.0,
            "last_trade_price": 100005.0,
            "open_price": 99500.0,
            "hour_start_ts_ms": 1000000,
            "hour_end_ts_ms": 2000000,
            "t_remaining_ms": 500000,
            "features": {
                "return_1m": 0.005,
                "ewma_vol": 0.02,
            },
            "p_yes_fair": 0.55,
        }
        seq = cache.update_from_poll(raw)
        assert seq == 1

        snapshot, _ = cache.get_latest()
        assert snapshot.symbol == "BTCUSDT"
        assert snapshot.best_bid_px == 100000.0
        assert snapshot.best_ask_px == 100010.0
        assert snapshot.last_trade_price == 100005.0
        assert snapshot.open_price == 99500.0
        assert snapshot.t_remaining_ms == 500000
        assert snapshot.return_1s == 0.005
        assert snapshot.ewma_vol_1s == 0.02
        assert snapshot.p_yes == 0.55

    def test_update_direct(self):
        """Test direct update method."""
        cache = BinanceCache()
        seq = cache.update_direct(
            symbol="BTCUSDT",
            best_bid_px=100000.0,
            best_ask_px=100010.0,
            last_trade_price=100005.0,
            open_price=99500.0,
            p_yes=0.6,
            shock_z=1.5,
        )
        assert seq == 1

        snapshot, _ = cache.get_latest()
        assert snapshot.mid_px == 100005.0
        assert snapshot.p_yes == 0.6
        assert snapshot.shock_z == 1.5

    def test_mid_price(self):
        """Test mid price calculation."""
        cache = BinanceCache()
        cache.update_direct(
            symbol="BTCUSDT",
            best_bid_px=100000.0,
            best_ask_px=100020.0,
        )
        assert cache.get_mid_price() == 100010.0

    def test_p_yes_accessor(self):
        """Test p_yes accessor."""
        cache = BinanceCache()
        assert cache.get_p_yes() is None

        cache.update_direct(
            symbol="BTCUSDT",
            best_bid_px=100000.0,
            best_ask_px=100010.0,
            p_yes=0.65,
        )
        assert cache.get_p_yes() == 0.65

    def test_time_remaining(self):
        """Test time remaining accessor."""
        cache = BinanceCache()
        assert cache.get_time_remaining_ms() == 0

        cache.update_direct(
            symbol="BTCUSDT",
            best_bid_px=100000.0,
            best_ask_px=100010.0,
            t_remaining_ms=300000,
        )
        assert cache.get_time_remaining_ms() == 300000

    def test_age_and_staleness(self):
        """Test age calculation and staleness detection."""
        cache = BinanceCache()
        cache.update_direct(
            symbol="BTCUSDT",
            best_bid_px=100000.0,
            best_ask_px=100010.0,
        )

        age = cache.get_age_ms()
        assert age < 100

        # Default threshold is 1000ms
        assert cache.is_stale() is False

        # Sleep to ensure age > 0
        time.sleep(0.005)
        assert cache.is_stale(threshold_ms=1) is True

    def test_staleness_with_no_data(self):
        """Test staleness returns True when no data."""
        cache = BinanceCache()
        assert cache.is_stale() is True

    def test_clear(self):
        """Test clearing cache."""
        cache = BinanceCache()
        cache.update_direct(
            symbol="BTCUSDT",
            best_bid_px=100000.0,
            best_ask_px=100010.0,
        )
        assert cache.has_data is True
        assert cache.seq == 1

        cache.clear()
        assert cache.has_data is False
        assert cache.seq == 0

    def test_price_vs_open(self):
        """Test price vs open calculation."""
        cache = BinanceCache()
        cache.update_direct(
            symbol="BTCUSDT",
            best_bid_px=101000.0,
            best_ask_px=101000.0,
            open_price=100000.0,
        )
        snapshot, _ = cache.get_latest()
        assert snapshot.price_vs_open == 1.01
        assert snapshot.return_from_open == pytest.approx(0.01)

    def test_p_yes_cents(self):
        """Test p_yes to cents conversion."""
        cache = BinanceCache()
        cache.update_direct(
            symbol="BTCUSDT",
            best_bid_px=100000.0,
            best_ask_px=100010.0,
            p_yes=0.55,
        )
        snapshot, _ = cache.get_latest()
        assert snapshot.p_yes_cents == 55

    def test_p_yes_cents_clamping(self):
        """Test p_yes cents clamping to 1-99."""
        cache = BinanceCache()
        cache.update_direct(
            symbol="BTCUSDT",
            best_bid_px=100000.0,
            best_ask_px=100010.0,
            p_yes=0.001,
        )
        snapshot, _ = cache.get_latest()
        assert snapshot.p_yes_cents == 1

        cache.update_direct(
            symbol="BTCUSDT",
            best_bid_px=100000.0,
            best_ask_px=100010.0,
            p_yes=0.999,
        )
        snapshot, _ = cache.get_latest()
        assert snapshot.p_yes_cents == 99


class TestBinanceSnapshotProperties:
    """Tests for BinanceSnapshot computed properties."""

    def test_price_vs_open_none_when_no_open(self):
        """Test price_vs_open returns None when no open price."""
        cache = BinanceCache()
        cache.update_direct(
            symbol="BTCUSDT",
            best_bid_px=100000.0,
            best_ask_px=100010.0,
            open_price=None,
        )
        snapshot, _ = cache.get_latest()
        assert snapshot.price_vs_open is None
        assert snapshot.return_from_open is None

    def test_p_yes_cents_none_when_no_p_yes(self):
        """Test p_yes_cents returns None when no p_yes."""
        cache = BinanceCache()
        cache.update_direct(
            symbol="BTCUSDT",
            best_bid_px=100000.0,
            best_ask_px=100010.0,
        )
        snapshot, _ = cache.get_latest()
        assert snapshot.p_yes_cents is None
