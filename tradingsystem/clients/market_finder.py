"""
Bitcoin Hourly Market Finder for Polymarket.

Finds the current Bitcoin hourly market by constructing the slug directly
from the current time. Markets use Eastern Time and follow the pattern:
"bitcoin-up-or-down-{month}-{day}-{hour}{am/pm}-et"

Example: bitcoin-up-or-down-january-23-1pm-et
"""

import re
import json
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional, Callable, Awaitable
from zoneinfo import ZoneInfo

from .gamma_client import GammaClient, GammaAPIError
from ..types import MarketInfo

logger = logging.getLogger(__name__)

# Eastern Time zone
ET = ZoneInfo("America/New_York")

# Month names for slug construction
MONTH_NAMES = [
    "january", "february", "march", "april", "may", "june",
    "july", "august", "september", "october", "november", "december"
]


def get_current_hour_et() -> datetime:
    """Get current hour boundary in Eastern Time."""
    now = datetime.now(ET)
    return now.replace(minute=0, second=0, microsecond=0)


def get_next_hour_et() -> datetime:
    """Get next hour boundary in Eastern Time (market resolution time)."""
    current = get_current_hour_et()
    return current + timedelta(hours=1)


def get_target_end_time() -> datetime:
    """
    Get the end time of the current hourly market.

    The market for hour H ends at hour H+1.
    E.g., the 12pm market ends at 1pm.
    """
    return get_next_hour_et()


def build_market_slug(target_time: Optional[datetime] = None) -> str:
    """
    Build the market slug for the current (or specified) hour.

    The slug format is: bitcoin-up-or-down-{month}-{day}-{hour}{am/pm}-et

    The hour in the slug is the MARKET START hour. The "1pm" market
    starts at 1pm and resolves at 2pm.

    Examples:
    - bitcoin-up-or-down-january-23-1pm-et (starts 1pm, ends 2pm)
    - bitcoin-up-or-down-february-14-12pm-et (starts 12pm, ends 1pm)
    - bitcoin-up-or-down-march-5-9am-et (starts 9am, ends 10am)

    Args:
        target_time: The market START time (defaults to current hour ET)

    Returns:
        The market slug string
    """
    if target_time is None:
        target_time = get_current_hour_et()

    # Ensure we're working in ET
    if target_time.tzinfo is None:
        target_time = target_time.replace(tzinfo=ET)
    else:
        target_time = target_time.astimezone(ET)

    month = MONTH_NAMES[target_time.month - 1]
    day = target_time.day
    hour_24 = target_time.hour

    # Convert to 12-hour format
    if hour_24 == 0:
        hour_12 = 12
        period = "am"
    elif hour_24 < 12:
        hour_12 = hour_24
        period = "am"
    elif hour_24 == 12:
        hour_12 = 12
        period = "pm"
    else:
        hour_12 = hour_24 - 12
        period = "pm"

    slug = f"bitcoin-up-or-down-{month}-{day}-{hour_12}{period}-et"
    return slug


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


class BitcoinHourlyMarketFinder:
    """
    Finds the current Bitcoin hourly market on Polymarket.

    Strategy:
    1. Construct the slug directly from current time
    2. Fetch the event by slug from Gamma API
    3. Extract YES/NO token IDs from the market

    The slug format is: bitcoin-up-or-down-{month}-{day}-{hour}{am/pm}-et
    """

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

        Constructs the slug directly and fetches from Gamma API.
        The market slug uses the START hour (e.g., "1pm" market starts at 1pm, ends at 2pm).

        Returns:
            MarketInfo or None if not found
        """
        start_time = get_current_hour_et()
        end_time = get_next_hour_et()
        slug = build_market_slug(start_time)

        logger.info(f"Looking for market with slug: {slug}")
        logger.info(f"  Market window: {start_time.strftime('%H:%M')} - {end_time.strftime('%H:%M %Z')}")

        try:
            # Try to get the event by slug
            event = await self._gamma.get_event_by_slug(slug)

            if event is None:
                logger.warning(f"No event found for slug: {slug}")
                return None

            # The event contains markets - get the first (usually only one for binary)
            markets = event.get("markets", [])
            if not markets:
                # Try getting markets separately
                event_id = event.get("id")
                if event_id:
                    markets = await self._gamma.get_markets_for_event(str(event_id))

            if not markets:
                logger.warning(f"No markets found in event: {slug}")
                return None

            # Use the first market (binary events typically have one market)
            market = markets[0]
            return self._parse_market(market, event)

        except GammaAPIError as e:
            logger.error(f"Failed to fetch market for slug {slug}: {e}")
            return None

    async def find_market_for_time(self, target_time: datetime) -> Optional[MarketInfo]:
        """
        Find a Bitcoin market for a specific time.

        Args:
            target_time: The market resolution time

        Returns:
            MarketInfo or None if not found
        """
        slug = build_market_slug(target_time)
        logger.info(f"Looking for market with slug: {slug}")

        try:
            event = await self._gamma.get_event_by_slug(slug)

            if event is None:
                logger.warning(f"No event found for slug: {slug}")
                return None

            markets = event.get("markets", [])
            if not markets:
                event_id = event.get("id")
                if event_id:
                    markets = await self._gamma.get_markets_for_event(str(event_id))

            if not markets:
                logger.warning(f"No markets found in event: {slug}")
                return None

            market = markets[0]
            return self._parse_market(market, event)

        except GammaAPIError as e:
            logger.error(f"Failed to fetch market for slug {slug}: {e}")
            return None

    def _parse_market(self, market: dict, event: Optional[dict] = None) -> MarketInfo:
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
            # Try clobTokenIds format (may be JSON string or list)
            clob_ids = market.get("clobTokenIds")
            if clob_ids:
                # Parse if it's a JSON string
                if isinstance(clob_ids, str):
                    clob_ids = json.loads(clob_ids)
                if len(clob_ids) >= 2:
                    # First is YES, second is NO
                    yes_token = {"token_id": clob_ids[0]}
                    no_token = {"token_id": clob_ids[1]}
            if not yes_token or not no_token:
                raise ValueError(f"Market missing YES/NO tokens: {market.get('id')}")

        # Parse end time
        end_date_str = market.get("endDate", "")
        if not end_date_str and event:
            end_date_str = event.get("endDate", "")

        try:
            market_end = parse_market_end_time(end_date_str)
            end_time_utc_ms = int(market_end.astimezone(timezone.utc).timestamp() * 1000)
        except Exception:
            end_time_utc_ms = 0

        # Extract reference price from question
        question = market.get("question", "")
        if not question and event:
            question = event.get("title", "")
        reference_price = extract_reference_price(question)

        # Get slug
        slug = market.get("slug", "")
        if not slug and event:
            slug = event.get("slug", "")

        return MarketInfo(
            condition_id=market.get("conditionId") or market.get("condition_id") or "",
            question=question,
            slug=slug,
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
        self._gamma = gamma
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
            logger.info(f"  Slug: {market.slug}")
            logger.info(f"  Condition ID: {market.condition_id}")
            logger.info(f"  YES token: {market.yes_token_id}")
            logger.info(f"  NO token: {market.no_token_id}")
            if market.reference_price:
                logger.info(f"  Reference price: ${market.reference_price:,.2f}")
            logger.info(f"  Time remaining: {market.time_remaining_ms / 1000:.0f}s")

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
