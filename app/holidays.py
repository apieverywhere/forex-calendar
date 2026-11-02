"""NYSE holidays and early closes from financecalendar.com (ADR-006).

Since 2026-11-01 financecalendar.com needs an API key, and its Free tier
allows 50 requests a day / 500 a month. Whalio no longer calls it: this
service fetches the list at most once a day, keeps the last good copy in
Redis and serves that, also while the provider is failing.
"""

import asyncio
import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import httpx

from .sources import SourceError
from .store import EventStore

log = logging.getLogger(__name__)

HOLIDAYS_URL = "https://www.financecalendar.com/wp-json/fc/v1/holidays"
SOURCE_URL = "https://www.financecalendar.com"
# Crawl key of the last attempt, successful or not.
ATTEMPT_KEY = "holidays:nyse"
_FIELDS = ("date", "name", "status", "close_time_et")


async def fetch_holidays(client: httpx.AsyncClient, api_key: str) -> list[dict]:
    try:
        response = await client.get(
            HOLIDAYS_URL,
            params={"exchange": "nyse"},
            headers={"Accept": "application/json", "X-API-Key": api_key},
        )
    except httpx.HTTPError as error:
        raise SourceError(f"holidays unreachable: {error}") from error
    if response.status_code != 200:
        raise SourceError(f"holidays returned HTTP {response.status_code}")
    try:
        rows = response.json().get("holidays")
    except (ValueError, AttributeError) as error:
        raise SourceError("holidays did not return a JSON object") from error
    if not isinstance(rows, list):
        raise SourceError("holidays JSON has no list")
    # Only what Whalio reads; the provider's other fields are not passed on.
    return [
        {field: row.get(field) for field in _FIELDS}
        for row in rows
        if isinstance(row, dict) and row.get("date") and row.get("name")
    ]


class HolidayService:
    def __init__(
        self,
        store: EventStore,
        client: httpx.AsyncClient,
        api_key: str | None,
        *,
        refresh: timedelta = timedelta(days=1),
        retry: timedelta = timedelta(hours=1),
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ):
        self.store = store
        self.client = client
        self.api_key = api_key
        self.refresh = refresh
        # After a failure, wait this long before calling the provider again,
        # so an outage cannot use up the daily quota.
        self.retry = retry
        self.now = now
        self._lock = asyncio.Lock()

    async def holidays(self) -> dict:
        """The saved list, refreshed first when it is a day old.

        Raises SourceError when nothing has been saved yet and the provider
        cannot be read (or no key is configured)."""
        async with self._lock:
            saved = self.store.holidays()
            if self._due(saved):
                now = self.now()
                self.store.mark_crawled(ATTEMPT_KEY, now)
                try:
                    rows = await fetch_holidays(self.client, self.api_key)
                    saved = (rows, now)
                    self.store.save_holidays(rows, now)
                except SourceError as error:
                    log.warning("holidays: %s", error)
                    if saved is None:
                        raise
        if saved is None:
            raise SourceError(
                "no holiday list yet"
                if self.api_key
                else "holidays not configured (FINANCE_CALENDAR_API_KEY)"
            )
        rows, fetched_at = saved
        return {
            "exchange": "nyse",
            "count": len(rows),
            "holidays": rows,
            "fetched_at": fetched_at.isoformat(),
            "source": SOURCE_URL,
        }

    def _due(self, saved: tuple[list[dict], datetime] | None) -> bool:
        if not self.api_key:
            return False
        now = self.now()
        if saved is not None and now - saved[1] < self.refresh:
            return False
        attempted = self.store.crawled_at(ATTEMPT_KEY)
        return attempted is None or now - attempted >= self.retry
