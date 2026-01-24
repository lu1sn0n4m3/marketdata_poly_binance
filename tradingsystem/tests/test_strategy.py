"""Tests for Strategy module."""

import time
import pytest

from tradingsystem.types import (
    Token,
    QuoteMode,
    InventoryState,
    PolymarketBookSnapshot,
    PolymarketBookTop,
    BinanceSnapshot,
    MarketSnapshotMeta,
    DesiredQuoteSet,
    now_ms,
)
from tradingsystem.strategy import (
    StrategyInput,
    StrategyConfig,
    Strategy,
    DefaultMMStrategy,
    IntentMailbox,
    StrategyRunner,
)


class TestStrategyInput:
    """Tests for StrategyInput."""

    def test_is_data_valid_true(self):
        """Test valid data check."""
        inp = StrategyInput(
            pm_book=_create_pm_book(),
            bn_snap=None,
            inventory=InventoryState(),
            fair_px_cents=50,
            t_remaining_ms=600_000,
        )
        assert inp.is_data_valid is True

    def test_is_data_valid_false_no_book(self):
        """Test invalid when no PM book."""
        inp = StrategyInput(
            pm_book=None,
            bn_snap=None,
            inventory=InventoryState(),
            fair_px_cents=50,
            t_remaining_ms=600_000,
        )
        assert inp.is_data_valid is False

    def test_is_data_valid_false_no_fair(self):
        """Test invalid when no fair price."""
        inp = StrategyInput(
            pm_book=_create_pm_book(),
            bn_snap=None,
            inventory=InventoryState(),
            fair_px_cents=None,
            t_remaining_ms=600_000,
        )
        assert inp.is_data_valid is False

    def test_near_expiry(self):
        """Test near expiry detection."""
        inp = StrategyInput(
            pm_book=_create_pm_book(),
            bn_snap=None,
            inventory=InventoryState(),
            fair_px_cents=50,
            t_remaining_ms=200_000,  # 3.3 minutes
        )
        assert inp.near_expiry is True
        assert inp.very_near_expiry is False

    def test_very_near_expiry(self):
        """Test very near expiry detection."""
        inp = StrategyInput(
            pm_book=_create_pm_book(),
            bn_snap=None,
            inventory=InventoryState(),
            fair_px_cents=50,
            t_remaining_ms=30_000,  # 30 seconds
        )
        assert inp.near_expiry is True
        assert inp.very_near_expiry is True


class TestDefaultMMStrategy:
    """Tests for DefaultMMStrategy."""

    @pytest.fixture
    def strategy(self):
        """Create default strategy."""
        return DefaultMMStrategy()

    @pytest.fixture
    def base_input(self):
        """Create base valid input."""
        return StrategyInput(
            pm_book=_create_pm_book(),
            bn_snap=None,
            inventory=InventoryState(),
            fair_px_cents=50,
            t_remaining_ms=600_000,
        )

    def test_compute_quotes_valid_data(self, strategy, base_input):
        """Test quote computation with valid data."""
        result = strategy.compute_quotes(base_input)

        assert result.mode == QuoteMode.NORMAL
        assert result.bid_yes.enabled is True
        assert result.ask_yes.enabled is True
        assert result.bid_yes.px_yes < result.ask_yes.px_yes

    def test_compute_quotes_no_data(self, strategy):
        """Test STOP mode when no data."""
        inp = StrategyInput(
            pm_book=None,
            bn_snap=None,
            inventory=InventoryState(),
            fair_px_cents=None,
            t_remaining_ms=600_000,
        )
        result = strategy.compute_quotes(inp)

        assert result.mode == QuoteMode.STOP
        assert result.bid_yes.enabled is False
        assert result.ask_yes.enabled is False
        assert "NO_DATA" in result.reason_flags

    def test_compute_quotes_expiry_imminent(self, strategy, base_input):
        """Test STOP mode near expiry."""
        base_input.t_remaining_ms = 30_000  # 30 seconds
        result = strategy.compute_quotes(base_input)

        assert result.mode == QuoteMode.STOP
        assert "EXPIRY_IMMINENT" in result.reason_flags

    def test_compute_quotes_spread(self, strategy, base_input):
        """Test spread calculation."""
        result = strategy.compute_quotes(base_input)

        spread = result.ask_yes.px_yes - result.bid_yes.px_yes
        assert spread >= strategy.config.min_spread_cents
        assert spread <= strategy.config.max_spread_cents

    def test_position_skew_long(self, strategy, base_input):
        """Test position skew when long YES."""
        base_input.inventory = InventoryState(I_yes=200, I_no=0)
        result = strategy.compute_quotes(base_input)

        # Skew should push prices down (bid lower, ask lower)
        # Compare to neutral
        base_input_neutral = StrategyInput(
            pm_book=_create_pm_book(),
            bn_snap=None,
            inventory=InventoryState(),
            fair_px_cents=50,
            t_remaining_ms=600_000,
        )
        result_neutral = strategy.compute_quotes(base_input_neutral)

        # With long position, mid should be lower
        mid_skewed = (result.bid_yes.px_yes + result.ask_yes.px_yes) / 2
        mid_neutral = (result_neutral.bid_yes.px_yes + result_neutral.ask_yes.px_yes) / 2
        assert mid_skewed <= mid_neutral

    def test_position_skew_short(self, strategy, base_input):
        """Test position skew when short YES."""
        base_input.inventory = InventoryState(I_yes=0, I_no=200)
        result = strategy.compute_quotes(base_input)

        # Skew should push prices up when net short
        base_input_neutral = StrategyInput(
            pm_book=_create_pm_book(),
            bn_snap=None,
            inventory=InventoryState(),
            fair_px_cents=50,
            t_remaining_ms=600_000,
        )
        result_neutral = strategy.compute_quotes(base_input_neutral)

        mid_skewed = (result.bid_yes.px_yes + result.ask_yes.px_yes) / 2
        mid_neutral = (result_neutral.bid_yes.px_yes + result_neutral.ask_yes.px_yes) / 2
        assert mid_skewed >= mid_neutral

    def test_max_position_sell_only(self, strategy, base_input):
        """Test one-sided quoting at max position."""
        config = StrategyConfig(max_position=100)
        strategy = DefaultMMStrategy(config)

        base_input.inventory = InventoryState(I_yes=100, I_no=0)
        result = strategy.compute_quotes(base_input)

        assert result.mode == QuoteMode.ONE_SIDED_SELL

    def test_max_position_buy_only(self, strategy, base_input):
        """Test one-sided quoting at max short position."""
        config = StrategyConfig(max_position=100)
        strategy = DefaultMMStrategy(config)

        base_input.inventory = InventoryState(I_yes=0, I_no=100)
        result = strategy.compute_quotes(base_input)

        assert result.mode == QuoteMode.ONE_SIDED_BUY

    def test_prices_bounded(self, strategy, base_input):
        """Test prices are within valid range."""
        for fair in [5, 50, 95]:
            base_input.fair_px_cents = fair
            result = strategy.compute_quotes(base_input)

            assert 1 <= result.bid_yes.px_yes <= 99
            assert 1 <= result.ask_yes.px_yes <= 99
            assert result.bid_yes.px_yes < result.ask_yes.px_yes


class TestIntentMailbox:
    """Tests for IntentMailbox."""

    def test_put_and_get(self):
        """Test basic put and get."""
        mailbox = IntentMailbox()

        intent = DesiredQuoteSet.stop(ts=now_ms())
        mailbox.put(intent)

        retrieved = mailbox.get()
        assert retrieved is intent

        # Second get should return None
        assert mailbox.get() is None

    def test_overwrites(self):
        """Test that new intent overwrites old."""
        mailbox = IntentMailbox()

        intent1 = DesiredQuoteSet.stop(ts=1, reason="FIRST")
        intent2 = DesiredQuoteSet.stop(ts=2, reason="SECOND")

        mailbox.put(intent1)
        mailbox.put(intent2)

        retrieved = mailbox.get()
        assert retrieved is intent2
        assert "SECOND" in retrieved.reason_flags

    def test_peek_does_not_clear(self):
        """Test that peek doesn't clear the mailbox."""
        mailbox = IntentMailbox()

        intent = DesiredQuoteSet.stop(ts=now_ms())
        mailbox.put(intent)

        peeked = mailbox.peek()
        assert peeked is intent

        # Get should still work
        retrieved = mailbox.get()
        assert retrieved is intent


class TestStrategyRunner:
    """Tests for StrategyRunner."""

    def test_runner_starts_and_stops(self):
        """Test runner can start and stop."""
        strategy = DefaultMMStrategy()
        mailbox = IntentMailbox()

        def get_input():
            return StrategyInput(
                pm_book=_create_pm_book(),
                bn_snap=None,
                inventory=InventoryState(),
                fair_px_cents=50,
                t_remaining_ms=600_000,
            )

        runner = StrategyRunner(
            strategy=strategy,
            mailbox=mailbox,
            get_input=get_input,
            tick_hz=100,
        )

        runner.start()
        assert runner.is_running

        time.sleep(0.1)  # Let it run a few ticks

        runner.stop()
        assert not runner.is_running

    def test_runner_produces_intents(self):
        """Test runner produces intents."""
        strategy = DefaultMMStrategy()
        mailbox = IntentMailbox()

        def get_input():
            return StrategyInput(
                pm_book=_create_pm_book(),
                bn_snap=None,
                inventory=InventoryState(),
                fair_px_cents=50,
                t_remaining_ms=600_000,
            )

        runner = StrategyRunner(
            strategy=strategy,
            mailbox=mailbox,
            get_input=get_input,
            tick_hz=100,
        )

        runner.start()
        time.sleep(0.1)
        runner.stop()

        # Should have produced at least one intent
        assert runner.stats["intents_produced"] >= 1

        # Should have an intent in mailbox
        intent = mailbox.get()
        assert intent is not None
        assert intent.mode == QuoteMode.NORMAL

    def test_runner_debounces(self):
        """Test that runner debounces similar intents."""
        strategy = DefaultMMStrategy()
        mailbox = IntentMailbox()

        call_count = 0

        def get_input():
            nonlocal call_count
            call_count += 1
            return StrategyInput(
                pm_book=_create_pm_book(),
                bn_snap=None,
                inventory=InventoryState(),
                fair_px_cents=50,  # Always same input
                t_remaining_ms=600_000,
            )

        runner = StrategyRunner(
            strategy=strategy,
            mailbox=mailbox,
            get_input=get_input,
            tick_hz=100,
        )

        runner.start()
        time.sleep(0.2)  # Run for 200ms
        runner.stop()

        # Should have many ticks but fewer intents due to debouncing
        assert runner.stats["ticks"] > runner.stats["intents_produced"]


# =============================================================================
# HELPERS
# =============================================================================


def _create_pm_book() -> PolymarketBookSnapshot:
    """Create a test PM book."""
    return PolymarketBookSnapshot(
        meta=MarketSnapshotMeta(
            monotonic_ts=now_ms(),
            wall_ts=None,
            feed_seq=1,
            source="PM",
        ),
        market_id="test_market",
        yes_token_id="yes_token_123",
        no_token_id="no_token_456",
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
