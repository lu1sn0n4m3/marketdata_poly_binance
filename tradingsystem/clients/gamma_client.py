"""
Gamma API Client for Polymarket market discovery.

The Gamma API is used to discover markets, get token IDs, and find
the current Bitcoin hourly market.
"""

import logging
from typing import Optional
import aiohttp

logger = logging.getLogger(__name__)


class GammaAPIError(Exception):
    """Error from Gamma API."""
    pass


class GammaClient:
    """
    Client for Polymarket Gamma API.

    Used to discover hourly Bitcoin markets and get token information.
    """

    DEFAULT_BASE_URL = "https://gamma-api.polymarket.com"

    def __init__(self, base_url: str = DEFAULT_BASE_URL):
        """
        Initialize the Gamma client.

        Args:
            base_url: Gamma API base URL
        """
        self._base_url = base_url.rstrip("/")
        self._session: Optional[aiohttp.ClientSession] = None

    async def _ensure_session(self) -> aiohttp.ClientSession:
        """Ensure HTTP session is created."""
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=10)
            self._session = aiohttp.ClientSession(timeout=timeout)
        return self._session

    async def close(self) -> None:
        """Close the HTTP session."""
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

    async def search_markets(
        self,
        query: str,
        limit: int = 50,
    ) -> list[dict]:
        """
        Search for markets by query string.

        Args:
            query: Search query (e.g., "bitcoin hourly")
            limit: Maximum results to return

        Returns:
            List of market dicts
        """
        session = await self._ensure_session()
        url = f"{self._base_url}/search/search-markets-events-and-profiles"
        params = {"q": query, "limit": limit}

        try:
            async with session.get(url, params=params) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    raise GammaAPIError(f"Search failed: {resp.status} - {text}")
                data = await resp.json()
                return data.get("markets", [])
        except aiohttp.ClientError as e:
            raise GammaAPIError(f"Search request failed: {e}")

    async def get_active_events(
        self,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict]:
        """
        Get active (not closed) events, newest first.

        Args:
            limit: Maximum events to return
            offset: Pagination offset

        Returns:
            List of event dicts
        """
        session = await self._ensure_session()
        url = f"{self._base_url}/events"
        params = {
            "order": "id",
            "ascending": "false",
            "closed": "false",
            "limit": limit,
            "offset": offset,
        }

        try:
            async with session.get(url, params=params) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    raise GammaAPIError(f"Get events failed: {resp.status} - {text}")
                return await resp.json()
        except aiohttp.ClientError as e:
            raise GammaAPIError(f"Get events request failed: {e}")

    async def get_event_by_slug(self, slug: str) -> Optional[dict]:
        """
        Get event by its URL slug.

        Args:
            slug: Event slug (e.g., "bitcoin-up-or-down-january-23-12pm-et")

        Returns:
            Event dict or None if not found
        """
        session = await self._ensure_session()
        url = f"{self._base_url}/events/slug/{slug}"

        try:
            async with session.get(url) as resp:
                if resp.status == 404:
                    return None
                if resp.status != 200:
                    text = await resp.text()
                    raise GammaAPIError(f"Get event failed: {resp.status} - {text}")
                return await resp.json()
        except aiohttp.ClientError as e:
            raise GammaAPIError(f"Get event request failed: {e}")

    async def get_event_by_id(self, event_id: str) -> Optional[dict]:
        """
        Get event by its ID.

        Args:
            event_id: Event ID

        Returns:
            Event dict or None if not found
        """
        session = await self._ensure_session()
        url = f"{self._base_url}/events/{event_id}"

        try:
            async with session.get(url) as resp:
                if resp.status == 404:
                    return None
                if resp.status != 200:
                    text = await resp.text()
                    raise GammaAPIError(f"Get event failed: {resp.status} - {text}")
                return await resp.json()
        except aiohttp.ClientError as e:
            raise GammaAPIError(f"Get event request failed: {e}")

    async def get_markets_for_event(self, event_id: str) -> list[dict]:
        """
        Get all markets for an event.

        Args:
            event_id: Event ID

        Returns:
            List of market dicts
        """
        session = await self._ensure_session()
        url = f"{self._base_url}/markets"
        params = {"event_id": event_id}

        try:
            async with session.get(url, params=params) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    raise GammaAPIError(f"Get markets failed: {resp.status} - {text}")
                return await resp.json()
        except aiohttp.ClientError as e:
            raise GammaAPIError(f"Get markets request failed: {e}")

    async def get_market(self, condition_id: str) -> Optional[dict]:
        """
        Get a specific market by condition ID.

        Args:
            condition_id: Market condition ID (hex string)

        Returns:
            Market dict or None if not found
        """
        session = await self._ensure_session()
        url = f"{self._base_url}/markets/{condition_id}"

        try:
            async with session.get(url) as resp:
                if resp.status == 404:
                    return None
                if resp.status != 200:
                    text = await resp.text()
                    raise GammaAPIError(f"Get market failed: {resp.status} - {text}")
                return await resp.json()
        except aiohttp.ClientError as e:
            raise GammaAPIError(f"Get market request failed: {e}")

    async def get_active_markets(
        self,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict]:
        """
        Get active (not closed) markets.

        Args:
            limit: Maximum markets to return
            offset: Pagination offset

        Returns:
            List of market dicts
        """
        session = await self._ensure_session()
        url = f"{self._base_url}/markets"
        params = {
            "closed": "false",
            "limit": limit,
            "offset": offset,
        }

        try:
            async with session.get(url, params=params) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    raise GammaAPIError(f"Get markets failed: {resp.status} - {text}")
                return await resp.json()
        except aiohttp.ClientError as e:
            raise GammaAPIError(f"Get markets request failed: {e}")
