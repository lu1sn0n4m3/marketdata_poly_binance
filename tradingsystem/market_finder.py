"""
Bitcoin Hourly Market Finder for Polymarket.

Finds the current Bitcoin hourly market based on time zone handling
(markets use Eastern Time) and market structure matching.
"""

import re
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional, Callable, Awaitable
from zoneinfo import ZoneInfo

from .gamma_client import GammaClient, GammaAPIError
from .mm_types import MarketInfo

logger = logging.getLogger(__name__)

# Eastern Time zone
ET = ZoneInfo("America/New_York")


def get_current_hour_et() -> datetime:
    """Get current hour boundary in Eastern Time."""
    now = datetime.now(ET)
    return now.replace(minute=0, second=0, microsecond=0)


def get_next_hour_et() -> datetime:
    """Get next hour boundary in Eastern Time."""
    current = get_current_hour_et()
    return current + timedelta(hours=1)


def get_target_end_time() -> datetime:
    """
    Get the end time of the current hourly market.

    The market for hour H ends at hour H+1.
    E.g., the 12pm market ends at 1pm.
    """
    return get_next_hour_et()


def parse_market_end_time(end_date_str: str) -> datetime:
    """
    Parse market end date from API response.

    API returns ISO format in UTC (e.g., "2026-01-23T17:00:00Z").
    Converts to Eastern Time for comparison.
    """
    # Handle both Z suffix and +00:00
    if end_date_str.endswith("Z"):
        end_date_str = end_date_str[:-1] + "+00:00"
    dt = datetime.fromisoformat(end_date_str)
    return dt.astimezone(ET)


def extract_reference_price(question: str) -> Optional[float]:
    """
    Extract BTC reference price from market question.

    Examples:
    - "Will BTC be above $100,000 at 12:00 PM ET?" -> 100000.0
    - "Will Bitcoin be above $99,500.50 at..." -> 99500.5
    """
    # Pattern matches $100,000 or $99,500.50 style prices
    pattern = r"\$([0-9,]+(?:\.[0-9]+)?)"
    match = re.search(pattern, question)
    if match:
        price_str = match.group(1).replace(",", "")
        try:
            return float(price_str)
        except ValueError:
            pass
    return None


def extract_hour_from_question(question: str) -> Optional[int]:
    """
    Extract the hour from a market question.

    Examples:
    - "... at 12:00 PM ET" -> 12
    - "... at 3:00 AM ET" -> 3
    """
    # Pattern matches times like "12:00 PM" or "3:00 AM"
    pattern = r"(\d{1,2}):00\s*(AM|PM|am|pm)"
    match = re.search(pattern, question)
    if match:
        hour = int(match.group(1))
        period = match.group(2).upper()
        if period == "PM" and hour != 12:
            hour += 12
        elif period == "AM" and hour == 12:
            hour = 0
        return hour
    return None


class BitcoinHourlyMarketFinder:
    """
    Finds the current Bitcoin hourly market on Polymarket.

    Strategy:
    1. Search for "bitcoin" markets
    2. Filter by end time matching current hour
    3. Extract YES/NO token IDs
    """

    # Keywords that identify Bitcoin hourly markets
    BITCOIN_KEYWORDS = ["btc", "bitcoin"]
    HOURLY_KEYWORDS = ["hourly", "hour", "pm et", "am et"]

    def __init__(self, gamma: GammaClient):
        """
        Initialize the market finder.

        Args:
            gamma: Gamma API client
        """
        self._gamma = gamma

    async def find_current_market(self) -> Optional[MarketInfo]:
        """
        Find the Bitcoin hourly market for the current hour.

        Tries multiple strategies:
        1. Search for "bitcoin hourly"
        2. Search for "btc"
        3. Browse active events

        Returns:
            MarketInfo or None if not found
        """
        target_end = get_target_end_time()
        target_hour = get_current_hour_et().hour
        logger.info(
            f"Searching for Bitcoin market ending at {target_end.strftime('%Y-%m-%d %H:%M %Z')} "
            f"(hour {target_hour})"
        )

        # Strategy 1: Search for "bitcoin hourly"
        try:
            markets = await self._gamma.search_markets("bitcoin hourly", limit=100)
            logger.debug(f"Search 'bitcoin hourly' returned {len(markets)} markets")
            for market in markets:
                if self._is_matching_market(market, target_end, target_hour):
                    return self._parse_market(market)
        except GammaAPIError as e:
            logger.warning(f"Search 'bitcoin hourly' failed: {e}")

        # Strategy 2: Search for "btc"
        try:
            markets = await self._gamma.search_markets("btc", limit=100)
            logger.debug(f"Search 'btc' returned {len(markets)} markets")
            for market in markets:
                if self._is_matching_market(market, target_end, target_hour):
                    return self._parse_market(market)
        except GammaAPIError as e:
            logger.warning(f"Search 'btc' failed: {e}")

        # Strategy 3: Browse active events
        try:
            events = await self._gamma.get_active_events(limit=200)
            logger.debug(f"Active events returned {len(events)} events")
            for event in events:
                title = (event.get("title") or "").lower()
                if any(kw in title for kw in self.BITCOIN_KEYWORDS):
                    # Get markets for this event
                    event_id = event.get("id")
                    if event_id:
                        markets = await self._gamma.get_markets_for_event(str(event_id))
                        for market in markets:
                            if self._is_matching_market(market, target_end, target_hour):
                                return self._parse_market(market)
        except GammaAPIError as e:
            logger.warning(f"Browse events failed: {e}")

        logger.warning(f"No Bitcoin market found for {target_end}")
        return None

    def _is_matching_market(
        self,
        market: dict,
        target_end: datetime,
        target_hour: int,
    ) -> bool:
        """
        Check if a market matches our criteria.

        Criteria:
        - Not closed
        - End time matches target (within tolerance)
        - Question contains bitcoin/btc keywords
        - Question contains hour indicator
        """
        # Must not be closed
        if market.get("closed", True):
            return False

        # Must be active
        if not market.get("active", True):
            return False

        # Check end time
        end_date_str = market.get("endDate")
        if not end_date_str:
            return False

        try:
            market_end = parse_market_end_time(end_date_str)
            # Allow 5 minute tolerance for end time matching
            diff_seconds = abs((market_end - target_end).total_seconds())
            if diff_seconds > 300:
                return False
        except Exception as e:
            logger.debug(f"Failed to parse end time: {e}")
            return False

        # Check question contains bitcoin keywords
        question = (market.get("question") or "").lower()
        if not any(kw in question for kw in self.BITCOIN_KEYWORDS):
            return False

        # Check question mentions the hour (optional but helpful)
        question_hour = extract_hour_from_question(market.get("question", ""))
        if question_hour is not None and question_hour != target_hour:
            return False

        return True

    def _parse_market(self, market: dict) -> MarketInfo:
        """
        Parse market data into MarketInfo.

        Extracts token IDs for YES and NO outcomes.
        """
        tokens = market.get("tokens") or []

        # Find YES and NO tokens
        yes_token = None
        no_token = None

        for token in tokens:
            outcome = (token.get("outcome") or "").lower()
            if outcome in ("yes", "up"):
                yes_token = token
            elif outcome in ("no", "down"):
                no_token = token

        if not yes_token or not no_token:
            # Try clobTokenIds format
            clob_ids = market.get("clobTokenIds")
            if clob_ids and len(clob_ids) >= 2:
                # Usually first is YES, second is NO
                yes_token = {"token_id": clob_ids[0]}
                no_token = {"token_id": clob_ids[1]}
            else:
                raise ValueError(f"Market missing YES/NO tokens: {market.get('id')}")

        # Parse end time
        end_date_str = market.get("endDate", "")
        try:
            market_end = parse_market_end_time(end_date_str)
            end_time_utc_ms = int(market_end.astimezone(timezone.utc).timestamp() * 1000)
        except Exception:
            end_time_utc_ms = 0

        # Extract reference price from question
        reference_price = extract_reference_price(market.get("question", ""))

        return MarketInfo(
            condition_id=market.get("conditionId") or market.get("condition_id") or "",
            question=market.get("question", ""),
            slug=market.get("slug", ""),
            yes_token_id=yes_token.get("token_id", ""),
            no_token_id=no_token.get("token_id", ""),
            end_time_utc_ms=end_time_utc_ms,
            reference_price=reference_price,
        )


class MarketScheduler:
    """
    Manages market transitions for hourly markets.

    Handles:
    - Initial market selection on startup
    - Automatic transition to new market on hour boundary
    - Cooldown period during transitions
    """

    def __init__(
        self,
        gamma: GammaClient,
        on_market_change: Optional[Callable[[MarketInfo], Awaitable[None]]] = None,
        transition_lead_time_ms: int = 30_000,  # 30 seconds before hour end
    ):
        """
        Initialize the market scheduler.

        Args:
            gamma: Gamma API client
            on_market_change: Callback when market changes
            transition_lead_time_ms: Stop trading this many ms before hour end
        """
        self._finder = BitcoinHourlyMarketFinder(gamma)
        self._on_market_change = on_market_change
        self._lead_time_ms = transition_lead_time_ms

        self._current_market: Optional[MarketInfo] = None
        self._last_check_hour: Optional[int] = None

    async def select_market_for_now(self) -> Optional[MarketInfo]:
        """
        Select market for current hour.

        Returns:
            MarketInfo or None if not found
        """
        market = await self._finder.find_current_market()
        if market:
            self._current_market = market
            self._last_check_hour = datetime.now(ET).hour

            logger.info(f"Selected market: {market.question}")
            logger.info(f"  Condition ID: {market.condition_id}")
            logger.info(f"  YES token: {market.yes_token_id}")
            logger.info(f"  NO token: {market.no_token_id}")
            if market.reference_price:
                logger.info(f"  Reference price: ${market.reference_price:,.2f}")

            if self._on_market_change:
                await self._on_market_change(market)

        return market

    async def check_for_transition(self, now_ms: int) -> Optional[MarketInfo]:
        """
        Check if we need to transition to a new market.

        Should be called periodically (e.g., every few seconds).

        Args:
            now_ms: Current timestamp in milliseconds

        Returns:
            New MarketInfo if transitioned, None otherwise
        """
        current_hour = datetime.now(ET).hour

        # Check if hour changed
        if current_hour != self._last_check_hour:
            logger.info(f"Hour changed to {current_hour}, selecting new market")
            return await self.select_market_for_now()

        return None

    def should_pause_for_transition(self, now_ms: int) -> bool:
        """
        Check if we should pause trading due to upcoming transition.

        Returns True if we're within the lead time before hour end.

        Args:
            now_ms: Current timestamp in milliseconds
        """
        if self._current_market is None:
            return True

        remaining = self._current_market.time_remaining_ms
        return remaining < self._lead_time_ms

    def time_until_pause_ms(self, now_ms: int) -> int:
        """
        Get time until we should pause for transition.

        Args:
            now_ms: Current timestamp in milliseconds

        Returns:
            Milliseconds until pause, or 0 if should pause now
        """
        if self._current_market is None:
            return 0

        remaining = self._current_market.time_remaining_ms
        time_until = remaining - self._lead_time_ms
        return max(0, time_until)

    @property
    def current_market(self) -> Optional[MarketInfo]:
        """Get current market."""
        return self._current_market

    @property
    def has_market(self) -> bool:
        """Check if we have a current market."""
        return self._current_market is not None
