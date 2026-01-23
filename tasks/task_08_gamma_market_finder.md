# Task 08: Gamma API Market Finder

**Priority:** High (Startup Requirement)
**Estimated Complexity:** Medium
**Dependencies:** None (can be done early)

---

## Objective

Implement a client to find the correct Bitcoin hourly market from the Gamma API. The bot must automatically discover the current hour's market on startup.

---

## Context

From user requirements:

> You are fetching the bitcoin market of https://polymarket.com/event/bitcoin-up-or-down-january-23-12pm-et with gamma api and need to find the correct one (its in eastern time) on each startup.

---

## Gamma API Reference

**Base URL:** `https://gamma-api.polymarket.com`

### Relevant Endpoints

**1. Search Markets:**
```
GET /search/search-markets-events-and-profiles?q=bitcoin
```

**2. Get Events (with filtering):**
```
GET /events?order=id&ascending=false&closed=false&limit=50
```

**3. Get Markets by Tag:**
```
GET /markets?tag_id=<tag_id>&closed=false
```

**4. Get Event by Slug:**
```
GET /events/slug/<slug>
```

### Market Data Structure

```json
{
  "id": "123456",
  "question": "Will BTC be above $100,000 at 12:00 PM ET on January 23?",
  "conditionId": "0x5f65...",
  "slug": "bitcoin-up-or-down-january-23-12pm-et",
  "outcomes": ["Yes", "No"],
  "outcomePrices": "[\"0.55\", \"0.45\"]",
  "tokens": [
    {
      "token_id": "12345...",
      "outcome": "Yes"
    },
    {
      "token_id": "67890...",
      "outcome": "No"
    }
  ],
  "endDate": "2026-01-23T17:00:00Z",
  "closed": false,
  "active": true
}
```

---

## Implementation Checklist

### 1. Market Identification Strategy

Bitcoin hourly markets follow a pattern:
- Question contains "BTC" or "Bitcoin"
- Question contains a specific hour (e.g., "12:00 PM ET")
- End date matches the target hour
- Market is not closed

**Strategy:**
1. Search for "bitcoin hourly" or similar
2. Filter by active/not closed
3. Parse end times to find the current hour's market
4. Extract token IDs for YES and NO

### 2. Time Zone Handling

**Critical:** Polymarket uses Eastern Time (ET) for hourly markets.

```python
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

def get_current_hour_et() -> datetime:
    """Get current hour in Eastern Time."""
    et = ZoneInfo("America/New_York")
    now = datetime.now(et)
    return now.replace(minute=0, second=0, microsecond=0)

def get_target_end_time() -> datetime:
    """Get the end time of the current hourly market."""
    current_hour = get_current_hour_et()
    # Market ends at the END of the hour
    return current_hour.replace(hour=current_hour.hour + 1)

def parse_market_end_time(end_date_str: str) -> datetime:
    """Parse market end date from API response."""
    # API returns ISO format in UTC
    dt = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
    # Convert to ET
    et = ZoneInfo("America/New_York")
    return dt.astimezone(et)
```

### 3. Gamma Client Class

```python
class GammaClient:
    """
    Client for Polymarket Gamma API.

    Used to discover hourly Bitcoin markets.
    """

    def __init__(self, base_url: str = "https://gamma-api.polymarket.com"):
        self._base_url = base_url
        self._session: Optional[aiohttp.ClientSession] = None

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def search_markets(self, query: str, limit: int = 50) -> list[dict]:
        """Search for markets by query string."""
        session = await self._ensure_session()
        url = f"{self._base_url}/search/search-markets-events-and-profiles"
        params = {"q": query, "limit": limit}

        async with session.get(url, params=params) as resp:
            if resp.status != 200:
                raise GammaAPIError(f"Search failed: {resp.status}")
            data = await resp.json()
            return data.get("markets", [])

    async def get_active_events(self, limit: int = 100) -> list[dict]:
        """Get active events, newest first."""
        session = await self._ensure_session()
        url = f"{self._base_url}/events"
        params = {
            "order": "id",
            "ascending": "false",
            "closed": "false",
            "limit": limit,
        }

        async with session.get(url, params=params) as resp:
            if resp.status != 200:
                raise GammaAPIError(f"Get events failed: {resp.status}")
            return await resp.json()

    async def get_event_by_slug(self, slug: str) -> Optional[dict]:
        """Get event by its slug."""
        session = await self._ensure_session()
        url = f"{self._base_url}/events/slug/{slug}"

        async with session.get(url) as resp:
            if resp.status == 404:
                return None
            if resp.status != 200:
                raise GammaAPIError(f"Get event failed: {resp.status}")
            return await resp.json()

    async def get_markets_for_event(self, event_id: str) -> list[dict]:
        """Get all markets for an event."""
        session = await self._ensure_session()
        url = f"{self._base_url}/markets"
        params = {"event_id": event_id, "closed": "false"}

        async with session.get(url, params=params) as resp:
            if resp.status != 200:
                raise GammaAPIError(f"Get markets failed: {resp.status}")
            return await resp.json()


class GammaAPIError(Exception):
    pass
```

### 4. Market Finder Class

```python
@dataclass
class FoundMarket:
    """Result of market search."""
    condition_id: str
    question: str
    slug: str
    yes_token_id: str
    no_token_id: str
    end_time_utc: datetime
    reference_price: Optional[float]  # Strike price from question


class BitcoinHourlyMarketFinder:
    """
    Finds the current Bitcoin hourly market.

    Strategy:
    1. Search for bitcoin hourly markets
    2. Filter by end time matching current hour
    3. Extract YES/NO token IDs
    """

    def __init__(self, gamma: GammaClient):
        self._gamma = gamma

    async def find_current_market(self) -> Optional[FoundMarket]:
        """
        Find the Bitcoin hourly market for the current hour.

        Returns None if no matching market found.
        """
        target_end = get_target_end_time()
        logger.info(f"Searching for Bitcoin market ending at {target_end}")

        # Strategy 1: Search for "bitcoin"
        markets = await self._gamma.search_markets("bitcoin hourly")

        # Filter and match
        for market in markets:
            if self._is_matching_market(market, target_end):
                return self._parse_market(market)

        # Strategy 2: Get active events and filter
        events = await self._gamma.get_active_events()
        for event in events:
            title = event.get("title", "").lower()
            if "bitcoin" in title or "btc" in title:
                event_markets = await self._gamma.get_markets_for_event(event["id"])
                for market in event_markets:
                    if self._is_matching_market(market, target_end):
                        return self._parse_market(market)

        logger.warning(f"No Bitcoin market found for {target_end}")
        return None

    def _is_matching_market(self, market: dict, target_end: datetime) -> bool:
        """Check if market matches our criteria."""
        # Must not be closed
        if market.get("closed", True):
            return False

        # Check end time
        end_date_str = market.get("endDate")
        if not end_date_str:
            return False

        try:
            market_end = parse_market_end_time(end_date_str)
            # Allow 5 minute tolerance for end time matching
            diff = abs((market_end - target_end).total_seconds())
            if diff > 300:
                return False
        except Exception:
            return False

        # Check question contains relevant keywords
        question = market.get("question", "").lower()
        if "btc" not in question and "bitcoin" not in question:
            return False

        return True

    def _parse_market(self, market: dict) -> FoundMarket:
        """Parse market data into FoundMarket."""
        tokens = market.get("tokens", [])
        yes_token = next((t for t in tokens if t.get("outcome", "").lower() == "yes"), None)
        no_token = next((t for t in tokens if t.get("outcome", "").lower() == "no"), None)

        if not yes_token or not no_token:
            raise ValueError(f"Market missing YES/NO tokens: {market.get('id')}")

        # Try to extract reference price from question
        reference_price = self._extract_reference_price(market.get("question", ""))

        return FoundMarket(
            condition_id=market.get("conditionId", ""),
            question=market.get("question", ""),
            slug=market.get("slug", ""),
            yes_token_id=yes_token.get("token_id", ""),
            no_token_id=no_token.get("token_id", ""),
            end_time_utc=parse_market_end_time(market.get("endDate", "")),
            reference_price=reference_price,
        )

    def _extract_reference_price(self, question: str) -> Optional[float]:
        """Extract BTC reference price from question."""
        import re
        # Pattern: $100,000 or $99,500.50
        pattern = r"\$([0-9,]+(?:\.[0-9]+)?)"
        match = re.search(pattern, question)
        if match:
            price_str = match.group(1).replace(",", "")
            try:
                return float(price_str)
            except ValueError:
                pass
        return None
```

### 5. Market Scheduler (Hour Transitions)

```python
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
        on_market_change: Callable[[FoundMarket], Awaitable[None]],
        transition_cooldown_ms: int = 30_000,  # 30 seconds before hour end
    ):
        self._finder = BitcoinHourlyMarketFinder(gamma)
        self._on_market_change = on_market_change
        self._cooldown_ms = transition_cooldown_ms

        self._current_market: Optional[FoundMarket] = None
        self._last_check_hour: Optional[int] = None

    async def select_market_for_now(self) -> Optional[FoundMarket]:
        """Select market for current hour."""
        market = await self._finder.find_current_market()
        if market:
            self._current_market = market
            self._last_check_hour = datetime.now(ZoneInfo("America/New_York")).hour
            await self._on_market_change(market)
        return market

    async def check_for_transition(self, now_ms: int) -> None:
        """Check if we need to transition to a new market."""
        current_hour = datetime.now(ZoneInfo("America/New_York")).hour

        # Check if hour changed
        if current_hour != self._last_check_hour:
            logger.info(f"Hour changed to {current_hour}, selecting new market")
            await self.select_market_for_now()

    def should_pause_for_transition(self, now_ms: int) -> bool:
        """Check if we should pause trading due to upcoming transition."""
        if self._current_market is None:
            return True

        # Check time until market end
        now = datetime.now(timezone.utc)
        end = self._current_market.end_time_utc
        remaining_ms = int((end - now).total_seconds() * 1000)

        return remaining_ms < self._cooldown_ms

    @property
    def current_market(self) -> Optional[FoundMarket]:
        return self._current_market
```

---

## File Locations

- Create: `services/polymarket_trader/src/polymarket_trader/gamma_client.py`
- Create: `services/polymarket_trader/src/polymarket_trader/market_finder.py`
- Create: `services/polymarket_trader/src/polymarket_trader/market_scheduler.py`

---

## Acceptance Criteria

- [ ] Correctly identifies current Bitcoin hourly market
- [ ] Handles Eastern Time zone correctly
- [ ] Extracts YES and NO token IDs
- [ ] Handles market transitions at hour boundaries
- [ ] Provides cooldown period before hour end
- [ ] Graceful handling when no market found
- [ ] Extracts reference price from question (for display)

---

## Testing

Create `tests/test_market_finder.py`:
- Test time zone conversion (ET to UTC)
- Test market matching logic
- Test token ID extraction
- Test reference price parsing
- Test hour transition detection
- Mock Gamma API responses for testing
