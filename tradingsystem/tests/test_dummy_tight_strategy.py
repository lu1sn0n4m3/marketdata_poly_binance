"""Tests for DummyTightStrategy."""

import pytest
from tradingsystem.strategy import DummyTightStrategy, StrategyInput
from tradingsystem.types import (
    PolymarketBookSnapshot,
    PolymarketBookTop,
    MarketSnapshotMeta,
    InventoryState,
    QuoteMode,
    now_ms,
)


@pytest.fixture
def mock_pm_book():
    """Create a mock PM book with realistic BBO."""
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


class TestDummyTightStrategy:
    """Tests for DummyTightStrategy."""

    def test_flat_position_quotes_both_sides(self, mock_pm_book):
        """Test flat position quotes at BBO."""
        strategy = DummyTightStrategy(size=5)

        inp = StrategyInput(
            pm_book=mock_pm_book,
            bn_snap=None,
            inventory=InventoryState(),  # Flat
            fair_px_cents=50,
            t_remaining_ms=3600000,
            pm_seq=1,
            bn_seq=0,
        )

        intent = strategy.compute_quotes(inp)

        # Should quote both sides
        assert intent.mode == QuoteMode.NORMAL
        assert intent.bid_yes.enabled is True
        assert intent.ask_yes.enabled is True

        # Bid at best_bid = 48
        assert intent.bid_yes.px_yes == 48
        assert intent.bid_yes.sz == 5

        # Ask at best_ask = 52
        assert intent.ask_yes.px_yes == 52
        assert intent.ask_yes.sz == 5

    def test_long_yes_only_asks(self, mock_pm_book):
        """Test long YES position only quotes ask."""
        strategy = DummyTightStrategy(size=5)

        inp = StrategyInput(
            pm_book=mock_pm_book,
            bn_snap=None,
            inventory=InventoryState(I_yes=5, I_no=0),  # Long 5 YES
            fair_px_cents=50,
            t_remaining_ms=3600000,
            pm_seq=1,
            bn_seq=0,
        )

        intent = strategy.compute_quotes(inp)

        # Should only quote ask
        assert intent.mode == QuoteMode.ONE_SIDED_SELL
        assert intent.bid_yes.enabled is False
        assert intent.ask_yes.enabled is True

        # Ask at best_ask = 52 (aggressive to exit)
        assert intent.ask_yes.px_yes == 52
        assert intent.ask_yes.sz == 5

    def test_short_yes_only_bids(self, mock_pm_book):
        """Test short YES (long NO) position only quotes bid."""
        strategy = DummyTightStrategy(size=5)

        inp = StrategyInput(
            pm_book=mock_pm_book,
            bn_snap=None,
            inventory=InventoryState(I_yes=0, I_no=5),  # Short 5 YES (long NO)
            fair_px_cents=50,
            t_remaining_ms=3600000,
            pm_seq=1,
            bn_seq=0,
        )

        intent = strategy.compute_quotes(inp)

        # Should only quote bid
        assert intent.mode == QuoteMode.ONE_SIDED_BUY
        assert intent.bid_yes.enabled is True
        assert intent.ask_yes.enabled is False

        # Bid at best_bid = 48 (aggressive to exit)
        assert intent.bid_yes.px_yes == 48
        assert intent.bid_yes.sz == 5

    def test_exit_order_uses_configured_size(self, mock_pm_book):
        """Test exit orders always use configured size (for Polymarket minimum)."""
        strategy = DummyTightStrategy(size=10)

        inp = StrategyInput(
            pm_book=mock_pm_book,
            bn_snap=None,
            inventory=InventoryState(I_yes=7, I_no=0),  # 7 YES (above min 5)
            fair_px_cents=50,
            t_remaining_ms=3600000,
            pm_seq=1,
            bn_seq=0,
        )

        intent = strategy.compute_quotes(inp)

        # Size should always be configured size (10), not position (7)
        # This ensures consistent order sizing
        assert intent.ask_yes.sz == 10

    def test_dust_position_long_quotes_as_flat(self, mock_pm_book):
        """Test dust long position (< min_size) quotes as if flat."""
        strategy = DummyTightStrategy(size=5)

        inp = StrategyInput(
            pm_book=mock_pm_book,
            bn_snap=None,
            inventory=InventoryState(I_yes=3, I_no=0),  # Dust: 3 YES < min 5
            fair_px_cents=50,
            t_remaining_ms=3600000,
            pm_seq=1,
            bn_seq=0,
        )

        intent = strategy.compute_quotes(inp)

        # Should quote both sides as if flat (dust is ignored)
        assert intent.mode == QuoteMode.NORMAL
        assert intent.bid_yes.enabled is True
        assert intent.ask_yes.enabled is True
        # Quotes at BBO (flat behavior)
        assert intent.bid_yes.px_yes == 48
        assert intent.ask_yes.px_yes == 52

    def test_dust_position_short_quotes_as_flat(self, mock_pm_book):
        """Test dust short position (< min_size) quotes as if flat."""
        strategy = DummyTightStrategy(size=5)

        inp = StrategyInput(
            pm_book=mock_pm_book,
            bn_snap=None,
            inventory=InventoryState(I_yes=0, I_no=2),  # Dust: short 2 YES < min 5
            fair_px_cents=50,
            t_remaining_ms=3600000,
            pm_seq=1,
            bn_seq=0,
        )

        intent = strategy.compute_quotes(inp)

        # Should quote both sides as if flat (dust is ignored)
        assert intent.mode == QuoteMode.NORMAL
        assert intent.bid_yes.enabled is True
        assert intent.ask_yes.enabled is True
        # Quotes at BBO (flat behavior)
        assert intent.bid_yes.px_yes == 48
        assert intent.ask_yes.px_yes == 52

    def test_stops_without_pm_data(self):
        """Test stops when no PM book data."""
        strategy = DummyTightStrategy()

        inp = StrategyInput(
            pm_book=None,
            bn_snap=None,
            inventory=InventoryState(),
            fair_px_cents=50,
            t_remaining_ms=3600000,
        )

        intent = strategy.compute_quotes(inp)

        assert intent.mode == QuoteMode.STOP
        assert "NO_PM_DATA" in intent.reason_flags

    def test_stops_with_invalid_bbo(self, mock_pm_book):
        """Test stops when BBO is invalid."""
        strategy = DummyTightStrategy()

        # Make BBO invalid (bid >= ask)
        mock_pm_book.yes_top.best_bid_px = 55
        mock_pm_book.yes_top.best_ask_px = 50

        inp = StrategyInput(
            pm_book=mock_pm_book,
            bn_snap=None,
            inventory=InventoryState(),
            fair_px_cents=50,
            t_remaining_ms=3600000,
        )

        intent = strategy.compute_quotes(inp)

        assert intent.mode == QuoteMode.STOP
        assert "INVALID_BBO" in intent.reason_flags

    def test_bid_price_clamped_at_1(self, mock_pm_book):
        """Test bid price is clamped to minimum 1."""
        strategy = DummyTightStrategy()

        # Best bid at 1, so bid-1 would be 0
        mock_pm_book.yes_top.best_bid_px = 1
        mock_pm_book.yes_top.best_ask_px = 5

        inp = StrategyInput(
            pm_book=mock_pm_book,
            bn_snap=None,
            inventory=InventoryState(),
            fair_px_cents=3,
            t_remaining_ms=3600000,
        )

        intent = strategy.compute_quotes(inp)

        # Bid should be clamped to 1
        assert intent.bid_yes.px_yes == 1

    def test_ask_price_clamped_at_99(self, mock_pm_book):
        """Test ask price is clamped to maximum 99."""
        strategy = DummyTightStrategy()

        # Best ask at 99, so ask+1 would be 100
        mock_pm_book.yes_top.best_bid_px = 95
        mock_pm_book.yes_top.best_ask_px = 99

        inp = StrategyInput(
            pm_book=mock_pm_book,
            bn_snap=None,
            inventory=InventoryState(),
            fair_px_cents=97,
            t_remaining_ms=3600000,
        )

        intent = strategy.compute_quotes(inp)

        # Ask should be clamped to 99
        assert intent.ask_yes.px_yes == 99
