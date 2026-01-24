"""Tests for DummyStrategy."""

import pytest
from tradingsystem.strategy import DummyStrategy, StrategyInput
from tradingsystem.types import (
    PolymarketBookSnapshot,
    PolymarketBookTop,
    BinanceSnapshot,
    MarketSnapshotMeta,
    InventoryState,
    QuoteMode,
    now_ms,
)


@pytest.fixture
def mock_pm_book():
    """Create a mock PM book snapshot."""
    return PolymarketBookSnapshot(
        meta=MarketSnapshotMeta(
            monotonic_ts=now_ms(),
            wall_ts=None,
            feed_seq=1,
            source="PM",
        ),
        market_id="market_123",
        yes_token_id="yes_token",
        no_token_id="no_token",
        yes_top=PolymarketBookTop(
            best_bid_px=48,
            best_bid_sz=100,
            best_ask_px=52,
            best_ask_sz=100,
        ),
        no_top=PolymarketBookTop(
            best_bid_px=48,
            best_bid_sz=100,
            best_ask_px=52,
            best_ask_sz=100,
        ),
    )


@pytest.fixture
def mock_bn_snap():
    """Create a mock BN snapshot."""
    return BinanceSnapshot(
        meta=MarketSnapshotMeta(
            monotonic_ts=now_ms(),
            wall_ts=None,
            feed_seq=1,
            source="BN",
        ),
        symbol="BTCUSDT",
        best_bid_px=100000.0,
        best_ask_px=100001.0,
        p_yes=0.55,
        t_remaining_ms=3600000,
    )


class TestDummyStrategy:
    """Tests for DummyStrategy."""

    def test_always_returns_fixed_quotes(self, mock_pm_book, mock_bn_snap):
        """Test strategy always returns same fixed quotes."""
        strategy = DummyStrategy(size=5)

        inp = StrategyInput(
            pm_book=mock_pm_book,
            bn_snap=mock_bn_snap,
            inventory=InventoryState(),
            fair_px_cents=55,
            t_remaining_ms=3600000,
            pm_seq=1,
            bn_seq=1,
        )

        intent = strategy.compute_quotes(inp)

        # Always NORMAL mode
        assert intent.mode == QuoteMode.NORMAL

        # YES bid at 1 cent
        assert intent.bid_yes.enabled is True
        assert intent.bid_yes.px_yes == 1
        assert intent.bid_yes.sz == 5

        # YES ask at 99 cents (will materialize as NO bid at 1 cent)
        assert intent.ask_yes.enabled is True
        assert intent.ask_yes.px_yes == 99
        assert intent.ask_yes.sz == 5

        # Reason flags include DUMMY_TEST
        assert "DUMMY_TEST" in intent.reason_flags

    def test_configurable_size(self, mock_pm_book, mock_bn_snap):
        """Test size is configurable."""
        strategy = DummyStrategy(size=10)

        inp = StrategyInput(
            pm_book=mock_pm_book,
            bn_snap=mock_bn_snap,
            inventory=InventoryState(),
            fair_px_cents=55,
            t_remaining_ms=3600000,
        )

        intent = strategy.compute_quotes(inp)

        assert intent.bid_yes.sz == 10
        assert intent.ask_yes.sz == 10

    def test_quotes_unchanged_regardless_of_input(self, mock_pm_book, mock_bn_snap):
        """Test quotes don't change based on market conditions."""
        strategy = DummyStrategy()

        # Test with different fair prices
        for fair in [10, 50, 90]:
            inp = StrategyInput(
                pm_book=mock_pm_book,
                bn_snap=mock_bn_snap,
                inventory=InventoryState(),
                fair_px_cents=fair,
                t_remaining_ms=3600000,
            )

            intent = strategy.compute_quotes(inp)

            # Prices always the same
            assert intent.bid_yes.px_yes == 1
            assert intent.ask_yes.px_yes == 99

    def test_quotes_unchanged_regardless_of_inventory(self, mock_pm_book, mock_bn_snap):
        """Test quotes don't change based on inventory."""
        strategy = DummyStrategy()

        # Test with different inventory states
        for i_yes, i_no in [(0, 0), (100, 0), (0, 100), (50, 50)]:
            inventory = InventoryState(I_yes=i_yes, I_no=i_no)
            inp = StrategyInput(
                pm_book=mock_pm_book,
                bn_snap=mock_bn_snap,
                inventory=inventory,
                fair_px_cents=50,
                t_remaining_ms=3600000,
            )

            intent = strategy.compute_quotes(inp)

            # Always quotes both sides at fixed prices
            assert intent.bid_yes.enabled is True
            assert intent.ask_yes.enabled is True
            assert intent.bid_yes.px_yes == 1
            assert intent.ask_yes.px_yes == 99

    def test_quotes_even_without_data(self):
        """Test quotes even when market data is missing."""
        strategy = DummyStrategy()

        # No PM book, no BN snap
        inp = StrategyInput(
            pm_book=None,
            bn_snap=None,
            inventory=InventoryState(),
            fair_px_cents=None,
            t_remaining_ms=0,
        )

        intent = strategy.compute_quotes(inp)

        # Still quotes at fixed prices
        assert intent.mode == QuoteMode.NORMAL
        assert intent.bid_yes.enabled is True
        assert intent.ask_yes.enabled is True
        assert intent.bid_yes.px_yes == 1
        assert intent.ask_yes.px_yes == 99

    def test_quotes_near_expiry(self, mock_pm_book, mock_bn_snap):
        """Test quotes unchanged even near expiry."""
        strategy = DummyStrategy()

        # Very near expiry (30 seconds)
        inp = StrategyInput(
            pm_book=mock_pm_book,
            bn_snap=mock_bn_snap,
            inventory=InventoryState(),
            fair_px_cents=50,
            t_remaining_ms=30_000,
        )

        intent = strategy.compute_quotes(inp)

        # Still quotes (unlike DefaultMMStrategy which would stop)
        assert intent.mode == QuoteMode.NORMAL
        assert intent.bid_yes.enabled is True
        assert intent.ask_yes.enabled is True
