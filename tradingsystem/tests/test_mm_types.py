"""Tests for mm_types.py - Core data types."""

import pytest
from tradingsystem.mm_types import (
    # Enums
    Token,
    Side,
    OrderStatus,
    QuoteMode,
    # Snapshot types
    MarketSnapshotMeta,
    PMBookTop,
    PMBookSnapshot,
    BNSnapshot,
    # State types
    InventoryState,
    # Strategy types
    DesiredQuoteLeg,
    DesiredQuoteSet,
    # Order types
    RealOrderSpec,
    WorkingOrder,
    # Utilities
    now_ms,
    price_to_cents,
    cents_to_price,
)


class TestMarketSnapshotMeta:
    """Tests for MarketSnapshotMeta."""

    def test_age_ms(self):
        """Test age calculation."""
        meta = MarketSnapshotMeta(
            monotonic_ts=1000,
            wall_ts=None,
            feed_seq=1,
            source="PM",
        )
        assert meta.age_ms(1500) == 500
        assert meta.age_ms(1000) == 0
        assert meta.age_ms(2000) == 1000


class TestPMBookTop:
    """Tests for PMBookTop."""

    def test_mid_px(self):
        """Test mid price calculation."""
        top = PMBookTop(best_bid_px=50, best_bid_sz=100, best_ask_px=52, best_ask_sz=100)
        assert top.mid_px == 51

    def test_mid_px_no_quotes(self):
        """Test mid price with no valid quotes."""
        top = PMBookTop(best_bid_px=0, best_bid_sz=0, best_ask_px=100, best_ask_sz=0)
        assert top.mid_px == 50

    def test_spread(self):
        """Test spread calculation."""
        top = PMBookTop(best_bid_px=48, best_bid_sz=100, best_ask_px=52, best_ask_sz=100)
        assert top.spread == 4

    def test_has_bid_ask(self):
        """Test bid/ask presence detection."""
        top = PMBookTop(best_bid_px=50, best_bid_sz=100, best_ask_px=52, best_ask_sz=0)
        assert top.has_bid is True
        assert top.has_ask is False


class TestPMBookSnapshot:
    """Tests for PMBookSnapshot."""

    def test_yes_mid(self):
        """Test YES mid price."""
        meta = MarketSnapshotMeta(monotonic_ts=1000, wall_ts=None, feed_seq=1, source="PM")
        yes_top = PMBookTop(best_bid_px=55, best_bid_sz=100, best_ask_px=57, best_ask_sz=100)
        no_top = PMBookTop(best_bid_px=43, best_bid_sz=100, best_ask_px=45, best_ask_sz=100)

        snapshot = PMBookSnapshot(
            meta=meta,
            market_id="0x123",
            yes_token_id="yes_token",
            no_token_id="no_token",
            yes_top=yes_top,
            no_top=no_top,
        )

        assert snapshot.yes_mid == 56

    def test_synthetic_mid_from_yes(self):
        """Test synthetic mid uses YES when available."""
        meta = MarketSnapshotMeta(monotonic_ts=1000, wall_ts=None, feed_seq=1, source="PM")
        yes_top = PMBookTop(best_bid_px=55, best_bid_sz=100, best_ask_px=57, best_ask_sz=100)
        no_top = PMBookTop(best_bid_px=43, best_bid_sz=100, best_ask_px=45, best_ask_sz=100)

        snapshot = PMBookSnapshot(
            meta=meta,
            market_id="0x123",
            yes_token_id="yes_token",
            no_token_id="no_token",
            yes_top=yes_top,
            no_top=no_top,
        )

        assert snapshot.synthetic_mid == 56


class TestBNSnapshot:
    """Tests for BNSnapshot."""

    def test_mid_px(self):
        """Test mid price calculation."""
        meta = MarketSnapshotMeta(monotonic_ts=1000, wall_ts=None, feed_seq=1, source="BN")
        snapshot = BNSnapshot(
            meta=meta,
            symbol="BTCUSDT",
            best_bid_px=100000.0,
            best_ask_px=100010.0,
        )
        assert snapshot.mid_px == 100005.0

    def test_p_yes_cents(self):
        """Test probability to cents conversion."""
        meta = MarketSnapshotMeta(monotonic_ts=1000, wall_ts=None, feed_seq=1, source="BN")
        snapshot = BNSnapshot(
            meta=meta,
            symbol="BTCUSDT",
            best_bid_px=100000.0,
            best_ask_px=100010.0,
            p_yes=0.55,
        )
        assert snapshot.p_yes_cents == 55

    def test_p_yes_cents_clamping(self):
        """Test probability clamping to 1-99."""
        meta = MarketSnapshotMeta(monotonic_ts=1000, wall_ts=None, feed_seq=1, source="BN")
        snapshot = BNSnapshot(
            meta=meta,
            symbol="BTCUSDT",
            best_bid_px=100000.0,
            best_ask_px=100010.0,
            p_yes=0.001,  # Would be 0 cents
        )
        assert snapshot.p_yes_cents == 1

        snapshot.p_yes = 0.999  # Would be 99.9 cents
        assert snapshot.p_yes_cents == 99


class TestInventoryState:
    """Tests for InventoryState."""

    def test_net_exposure(self):
        """Test net exposure calculation."""
        inv = InventoryState(I_yes=100, I_no=30)
        assert inv.net_E == 70

        inv = InventoryState(I_yes=30, I_no=100)
        assert inv.net_E == -70

    def test_gross_exposure(self):
        """Test gross exposure calculation."""
        inv = InventoryState(I_yes=100, I_no=30)
        assert inv.gross_G == 130

    def test_update_from_fill_buy_yes(self):
        """Test inventory update from YES buy fill."""
        inv = InventoryState(I_yes=100, I_no=50)
        inv.update_from_fill(Token.YES, Side.BUY, 25, ts=1000)
        assert inv.I_yes == 125
        assert inv.I_no == 50
        assert inv.last_update_ts == 1000

    def test_update_from_fill_sell_yes(self):
        """Test inventory update from YES sell fill."""
        inv = InventoryState(I_yes=100, I_no=50)
        inv.update_from_fill(Token.YES, Side.SELL, 25, ts=1000)
        assert inv.I_yes == 75
        assert inv.I_no == 50

    def test_update_from_fill_buy_no(self):
        """Test inventory update from NO buy fill."""
        inv = InventoryState(I_yes=100, I_no=50)
        inv.update_from_fill(Token.NO, Side.BUY, 25, ts=1000)
        assert inv.I_yes == 100
        assert inv.I_no == 75

    def test_update_from_fill_sell_no(self):
        """Test inventory update from NO sell fill."""
        inv = InventoryState(I_yes=100, I_no=50)
        inv.update_from_fill(Token.NO, Side.SELL, 25, ts=1000)
        assert inv.I_yes == 100
        assert inv.I_no == 25


class TestDesiredQuoteSet:
    """Tests for DesiredQuoteSet."""

    def test_stop_factory(self):
        """Test STOP intent factory."""
        intent = DesiredQuoteSet.stop(ts=1000, reason="TEST_STOP")
        assert intent.mode == QuoteMode.STOP
        assert intent.bid_yes.enabled is False
        assert intent.ask_yes.enabled is False
        assert "TEST_STOP" in intent.reason_flags


class TestRealOrderSpec:
    """Tests for RealOrderSpec."""

    def test_matches_exact(self):
        """Test exact order matching."""
        spec1 = RealOrderSpec(token=Token.YES, side=Side.BUY, px=50, sz=100)
        spec2 = RealOrderSpec(token=Token.YES, side=Side.BUY, px=50, sz=100)
        assert spec1.matches(spec2) is True

    def test_matches_within_tolerance(self):
        """Test order matching within tolerance."""
        spec1 = RealOrderSpec(token=Token.YES, side=Side.BUY, px=50, sz=100)
        spec2 = RealOrderSpec(token=Token.YES, side=Side.BUY, px=50, sz=103)
        assert spec1.matches(spec2, size_tol=5) is True

    def test_matches_different_token(self):
        """Test order matching with different token."""
        spec1 = RealOrderSpec(token=Token.YES, side=Side.BUY, px=50, sz=100)
        spec2 = RealOrderSpec(token=Token.NO, side=Side.BUY, px=50, sz=100)
        assert spec1.matches(spec2) is False

    def test_matches_different_side(self):
        """Test order matching with different side."""
        spec1 = RealOrderSpec(token=Token.YES, side=Side.BUY, px=50, sz=100)
        spec2 = RealOrderSpec(token=Token.YES, side=Side.SELL, px=50, sz=100)
        assert spec1.matches(spec2) is False


class TestWorkingOrder:
    """Tests for WorkingOrder."""

    def test_remaining_sz(self):
        """Test remaining size calculation."""
        spec = RealOrderSpec(token=Token.YES, side=Side.BUY, px=50, sz=100)
        order = WorkingOrder(
            client_order_id="client_1",
            server_order_id="server_1",
            order_spec=spec,
            status=OrderStatus.WORKING,
            created_ts=1000,
            last_state_change_ts=1000,
            filled_sz=25,
        )
        assert order.remaining_sz == 75

    def test_is_terminal(self):
        """Test terminal state detection."""
        spec = RealOrderSpec(token=Token.YES, side=Side.BUY, px=50, sz=100)

        order = WorkingOrder(
            client_order_id="client_1",
            server_order_id="server_1",
            order_spec=spec,
            status=OrderStatus.WORKING,
            created_ts=1000,
            last_state_change_ts=1000,
        )
        assert order.is_terminal is False

        order.status = OrderStatus.FILLED
        assert order.is_terminal is True

        order.status = OrderStatus.CANCELED
        assert order.is_terminal is True


class TestUtilities:
    """Tests for utility functions."""

    def test_price_to_cents(self):
        """Test price string to cents conversion."""
        assert price_to_cents("0.55") == 55
        assert price_to_cents("0.01") == 1
        assert price_to_cents("0.99") == 99
        assert price_to_cents(None) == 0
        assert price_to_cents("") == 0

    def test_cents_to_price(self):
        """Test cents to decimal conversion."""
        assert cents_to_price(55) == 0.55
        assert cents_to_price(1) == 0.01
        assert cents_to_price(99) == 0.99

    def test_now_ms_returns_int(self):
        """Test now_ms returns an integer."""
        ts = now_ms()
        assert isinstance(ts, int)
        assert ts > 0
