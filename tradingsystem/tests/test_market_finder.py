"""Tests for market_finder.py - Bitcoin hourly market discovery."""

import pytest
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from unittest.mock import AsyncMock, patch

from tradingsystem.clients.market_finder import (
    BitcoinHourlyMarketFinder,
    MarketScheduler,
    build_market_slug,
    get_current_hour_et,
    get_next_hour_et,
    get_target_end_time,
    parse_market_end_time,
    extract_reference_price,
    ET,
)
from tradingsystem.clients import GammaClient


class TestBuildMarketSlug:
    """Tests for build_market_slug function."""

    def test_slug_format_pm(self):
        """Test slug format for PM hours."""
        # 1pm ET on January 23
        target = datetime(2026, 1, 23, 13, 0, 0, tzinfo=ET)
        slug = build_market_slug(target)
        assert slug == "bitcoin-up-or-down-january-23-1pm-et"

    def test_slug_format_am(self):
        """Test slug format for AM hours."""
        # 9am ET on March 5
        target = datetime(2026, 3, 5, 9, 0, 0, tzinfo=ET)
        slug = build_market_slug(target)
        assert slug == "bitcoin-up-or-down-march-5-9am-et"

    def test_slug_format_noon(self):
        """Test slug format for 12pm (noon)."""
        # 12pm ET on February 14
        target = datetime(2026, 2, 14, 12, 0, 0, tzinfo=ET)
        slug = build_market_slug(target)
        assert slug == "bitcoin-up-or-down-february-14-12pm-et"

    def test_slug_format_midnight(self):
        """Test slug format for 12am (midnight)."""
        # 12am ET on December 31
        target = datetime(2026, 12, 31, 0, 0, 0, tzinfo=ET)
        slug = build_market_slug(target)
        assert slug == "bitcoin-up-or-down-december-31-12am-et"

    def test_slug_format_11pm(self):
        """Test slug format for 11pm."""
        # 11pm ET on July 4
        target = datetime(2026, 7, 4, 23, 0, 0, tzinfo=ET)
        slug = build_market_slug(target)
        assert slug == "bitcoin-up-or-down-july-4-11pm-et"

    def test_slug_format_11am(self):
        """Test slug format for 11am."""
        # 11am ET on November 15
        target = datetime(2026, 11, 15, 11, 0, 0, tzinfo=ET)
        slug = build_market_slug(target)
        assert slug == "bitcoin-up-or-down-november-15-11am-et"

    def test_slug_uses_current_hour_by_default(self):
        """Test that slug defaults to current hour (market start time)."""
        slug = build_market_slug()
        current_hour = get_current_hour_et()
        expected = build_market_slug(current_hour)
        assert slug == expected


class TestTimezoneFunctions:
    """Tests for timezone helper functions."""

    def test_get_current_hour_et(self):
        """Test current hour in ET."""
        current = get_current_hour_et()
        assert current.tzinfo is not None
        assert current.minute == 0
        assert current.second == 0
        assert current.microsecond == 0

    def test_get_next_hour_et(self):
        """Test next hour in ET."""
        current = get_current_hour_et()
        next_hour = get_next_hour_et()
        diff = next_hour - current
        assert diff.total_seconds() == 3600

    def test_parse_market_end_time_z_suffix(self):
        """Test parsing UTC time with Z suffix."""
        end_time = parse_market_end_time("2026-01-23T17:00:00Z")
        assert end_time.tzinfo is not None
        # Should be converted to ET
        assert end_time.tzinfo == ET or str(end_time.tzinfo) == "America/New_York"

    def test_parse_market_end_time_utc_offset(self):
        """Test parsing UTC time with +00:00 offset."""
        end_time = parse_market_end_time("2026-01-23T17:00:00+00:00")
        assert end_time.tzinfo is not None


class TestExtractReferencePriceFunction:
    """Tests for extract_reference_price function."""

    def test_extract_simple_price(self):
        """Test extracting simple price."""
        question = "Will BTC be above $100,000 at 12:00 PM ET?"
        assert extract_reference_price(question) == 100000.0

    def test_extract_price_with_decimals(self):
        """Test extracting price with decimals."""
        question = "Will Bitcoin be above $99,500.50 at 3:00 PM?"
        assert extract_reference_price(question) == 99500.5

    def test_extract_price_no_commas(self):
        """Test extracting price without commas."""
        question = "Will BTC be above $50000 at noon?"
        assert extract_reference_price(question) == 50000.0

    def test_no_price_in_question(self):
        """Test when no price is in question."""
        question = "Will it rain tomorrow?"
        assert extract_reference_price(question) is None


class TestBitcoinHourlyMarketFinder:
    """Tests for BitcoinHourlyMarketFinder."""

    @pytest.fixture
    def mock_gamma(self):
        """Create mock Gamma client."""
        return AsyncMock(spec=GammaClient)

    @pytest.fixture
    def finder(self, mock_gamma):
        """Create market finder with mock client."""
        return BitcoinHourlyMarketFinder(mock_gamma)

    @pytest.mark.asyncio
    async def test_find_current_market_success(self, finder, mock_gamma):
        """Test finding market successfully."""
        # Build the expected slug using current hour (market start time)
        start_time = get_current_hour_et()
        end_time = get_next_hour_et()
        expected_slug = build_market_slug(start_time)

        mock_event = {
            "id": "123",
            "slug": expected_slug,
            "title": "Bitcoin Up or Down",
            "markets": [{
                "conditionId": "0xabc123",
                "question": "Will BTC be above $100,000?",
                "slug": expected_slug,
                "endDate": end_time.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "clobTokenIds": ["yes_token_123", "no_token_456"],
            }],
        }

        mock_gamma.get_event_by_slug.return_value = mock_event

        result = await finder.find_current_market()

        assert result is not None
        assert result.condition_id == "0xabc123"
        assert result.yes_token_id == "yes_token_123"
        assert result.no_token_id == "no_token_456"
        mock_gamma.get_event_by_slug.assert_called_once_with(expected_slug)

    @pytest.mark.asyncio
    async def test_find_current_market_not_found(self, finder, mock_gamma):
        """Test when market is not found."""
        mock_gamma.get_event_by_slug.return_value = None

        result = await finder.find_current_market()
        assert result is None

    @pytest.mark.asyncio
    async def test_find_market_with_tokens_array(self, finder, mock_gamma):
        """Test parsing market with tokens array format."""
        start_time = get_current_hour_et()
        end_time = get_next_hour_et()
        expected_slug = build_market_slug(start_time)

        mock_event = {
            "id": "123",
            "slug": expected_slug,
            "markets": [{
                "conditionId": "0xdef456",
                "question": "Will BTC be above $105,000?",
                "endDate": end_time.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "tokens": [
                    {"token_id": "yes_from_tokens", "outcome": "Yes"},
                    {"token_id": "no_from_tokens", "outcome": "No"},
                ],
            }],
        }

        mock_gamma.get_event_by_slug.return_value = mock_event

        result = await finder.find_current_market()

        assert result is not None
        assert result.yes_token_id == "yes_from_tokens"
        assert result.no_token_id == "no_from_tokens"
        assert result.reference_price == 105000.0


class TestMarketScheduler:
    """Tests for MarketScheduler."""

    @pytest.fixture
    def mock_gamma(self):
        """Create mock Gamma client."""
        return AsyncMock(spec=GammaClient)

    @pytest.fixture
    def scheduler(self, mock_gamma):
        """Create scheduler with mock client."""
        return MarketScheduler(
            gamma=mock_gamma,
            transition_lead_time_ms=30_000,
        )

    @pytest.mark.asyncio
    async def test_select_market_for_now(self, scheduler, mock_gamma):
        """Test selecting market for current hour."""
        start_time = get_current_hour_et()
        end_time = get_next_hour_et()
        expected_slug = build_market_slug(start_time)

        mock_event = {
            "id": "123",
            "slug": expected_slug,
            "markets": [{
                "conditionId": "0xabc123",
                "question": "Will BTC be above $100,000?",
                "endDate": end_time.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "clobTokenIds": ["yes_token", "no_token"],
            }],
        }

        mock_gamma.get_event_by_slug.return_value = mock_event

        result = await scheduler.select_market_for_now()

        assert result is not None
        assert scheduler.current_market == result
        assert scheduler.has_market is True

    def test_should_pause_for_transition_no_market(self, scheduler):
        """Test pause check when no market selected."""
        assert scheduler.should_pause_for_transition(0) is True

    def test_has_market_initially_false(self, scheduler):
        """Test has_market is False initially."""
        assert scheduler.has_market is False
