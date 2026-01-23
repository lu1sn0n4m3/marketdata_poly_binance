"""Gamma API client for market discovery."""

import logging
from typing import Optional
from datetime import datetime, timezone, timedelta

import aiohttp
import orjson

from .types import MarketInfo, TokenIds

logger = logging.getLogger(__name__)


class GammaClient:
    """
    Client for the Polymarket Gamma API.
    
    Used to discover the current hourly market and resolve token identifiers.
    """
    
    def __init__(
        self,
        base_url: str = "https://gamma-api.polymarket.com",
        timeout_seconds: float = 10.0,
    ):
        """
        Initialize the client.
        
        Args:
            base_url: Gamma API base URL
            timeout_seconds: Request timeout
        """
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self._session: Optional[aiohttp.ClientSession] = None
    
    async def _get_session(self) -> aiohttp.ClientSession:
        """Get or create HTTP session."""
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=self.timeout_seconds)
            self._session = aiohttp.ClientSession(timeout=timeout)
        return self._session
    
    async def close(self) -> None:
        """Close the HTTP session."""
        if self._session and not self._session.closed:
            await self._session.close()
    
    def _derive_hourly_slug(self, market_slug: str, now_ms: int) -> str:
        """
        Derive the hourly market slug from base slug and timestamp.
        
        For BTC hourly markets, the slug format is:
        bitcoin-up-or-down-{month}-{day}-{hour}{am/pm}-et
        
        Example: bitcoin-up-or-down-january-22-7am-et
        
        The time in the slug indicates when the market STARTS (in Eastern Time).
        
        Args:
            market_slug: Base market slug (e.g., "bitcoin-up-or-down")
            now_ms: Current timestamp in milliseconds
        
        Returns:
            Full hourly market slug
        """
        # Convert to Eastern Time
        utc_now = datetime.fromtimestamp(now_ms / 1000, tz=timezone.utc)
        
        # Handle Eastern Time with DST awareness
        # EST = UTC-5, EDT = UTC-4
        # For simplicity, we use a fixed offset. For production, consider pytz.
        # EDT is typically Mar-Nov, EST is Nov-Mar
        # For now, use -5 (EST) as default
        et_offset_hours = -5
        if utc_now.month >= 3 and utc_now.month < 11:
            # Rough EDT approximation (March-November)
            et_offset_hours = -4
        
        et_tz = timezone(timedelta(hours=et_offset_hours))
        et_time = utc_now.astimezone(et_tz)
        
        month_names = [
            "january", "february", "march", "april", "may", "june",
            "july", "august", "september", "october", "november", "december",
        ]
        
        month = month_names[et_time.month - 1]
        day = et_time.day
        hour_12 = et_time.hour % 12
        if hour_12 == 0:
            hour_12 = 12
        am_pm = "pm" if et_time.hour >= 12 else "am"
        
        # Format: bitcoin-up-or-down-january-22-7am-et
        return f"{market_slug}-{month}-{day}-{hour_12}{am_pm}-et"
    
    async def get_current_hour_market(
        self,
        market_slug: str,
        now_ms: int,
    ) -> Optional[MarketInfo]:
        """
        Get the current hourly market.
        
        Args:
            market_slug: Base market slug (e.g., "bitcoin-up-or-down")
            now_ms: Current timestamp in milliseconds
        
        Returns:
            MarketInfo or None if not found
        """
        hourly_slug = self._derive_hourly_slug(market_slug, now_ms)
        
        try:
            session = await self._get_session()
            url = f"{self.base_url}/events"
            params = {"slug": hourly_slug}
            
            async with session.get(url, params=params) as resp:
                if resp.status != 200:
                    logger.warning(f"Gamma API returned {resp.status} for {hourly_slug}")
                    return None
                
                data = await resp.json()
                if not data:
                    logger.warning(f"No data returned for {hourly_slug}")
                    return None
                
                event = data[0] if isinstance(data, list) else data
                markets = event.get("markets", [])
                
                if not markets:
                    logger.warning(f"No markets in event for {hourly_slug}")
                    return None
                
                # Find the active market
                for market in markets:
                    if not market.get("active", True):
                        continue

                    # Parse token IDs
                    token_ids_raw = market.get("clobTokenIds", [])
                    if isinstance(token_ids_raw, str):
                        token_ids_raw = orjson.loads(token_ids_raw)

                    if len(token_ids_raw) < 2:
                        continue

                    # Parse outcomes to correctly map token IDs
                    # The outcomes array corresponds to clobTokenIds array
                    # For "Bitcoin Up or Down": "Up" = YES, "Down" = NO
                    outcomes = market.get("outcomes", [])
                    if isinstance(outcomes, str):
                        outcomes = orjson.loads(outcomes)

                    # Default: [0]=YES, [1]=NO (but this is often wrong!)
                    yes_idx, no_idx = 0, 1

                    # Find correct indices based on outcome names
                    if len(outcomes) >= 2:
                        for i, outcome in enumerate(outcomes):
                            outcome_lower = str(outcome).lower()
                            if outcome_lower in ("up", "yes", "true"):
                                yes_idx = i
                            elif outcome_lower in ("down", "no", "false"):
                                no_idx = i

                    # Log what we found for debugging
                    print(f"[GAMMA] Outcomes: {outcomes}")
                    print(f"[GAMMA] YES idx={yes_idx} (token={token_ids_raw[yes_idx][:20]}...)")
                    print(f"[GAMMA] NO idx={no_idx} (token={token_ids_raw[no_idx][:20]}...)")

                    tokens = TokenIds(
                        yes_token_id=token_ids_raw[yes_idx],
                        no_token_id=token_ids_raw[no_idx],
                    )

                    return MarketInfo(
                        condition_id=market.get("conditionId", ""),
                        market_slug=hourly_slug,
                        question=market.get("question", ""),
                        tokens=tokens,
                        end_date_iso=market.get("endDateIso"),
                        active=market.get("active", True),
                    )
                
                return None
                
        except Exception as e:
            logger.error(f"Error fetching market {hourly_slug}: {e}")
            return None
    
    async def resolve_token_ids(self, market_info: MarketInfo) -> Optional[TokenIds]:
        """
        Resolve token IDs for a market.
        
        Args:
            market_info: MarketInfo with condition_id
        
        Returns:
            TokenIds or None if not found
        """
        # Token IDs are already in MarketInfo from get_current_hour_market
        return market_info.tokens if market_info else None
    
    async def healthcheck(self) -> bool:
        """
        Check if the Gamma API is reachable.
        
        Returns:
            True if healthy
        """
        try:
            session = await self._get_session()
            # Just check if we can reach the API
            async with session.get(f"{self.base_url}/events", params={"limit": 1}) as resp:
                return resp.status == 200
        except Exception as e:
            logger.warning(f"Gamma healthcheck failed: {e}")
            return False
