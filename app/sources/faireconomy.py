"""Forex Factory's public weekly export, served by FairEconomy.

`https://nfs.faireconomy.media/ff_calendar_thisweek.json` lists the current
week (Sunday to Saturday, US Eastern). Items look like::

    {"title": "CPI m/m", "country": "USD", "date": "2026-10-14T08:30:00-04:00",
     "impact": "High", "forecast": "0.3%", "previous": "0.4%"}

It has no Actual values and no event URLs.
"""

from datetime import UTC, datetime

import httpx

from ..models import Event, clean, event_id, normalize_impact
from . import SourceError


async def fetch_feed(client: httpx.AsyncClient, url: str) -> list[dict]:
    try:
        response = await client.get(url, headers={"Accept": "application/json"})
    except httpx.HTTPError as error:
        raise SourceError(f"feed unreachable: {error}") from error
    if response.status_code != 200:
        raise SourceError(f"feed returned HTTP {response.status_code}")
    try:
        data = response.json()
    except ValueError as error:
        # The feed answers rate-limited clients with an HTML page.
        raise SourceError("feed did not return JSON (rate limited?)") from error
    if not isinstance(data, list):
        raise SourceError("feed JSON is not a list")
    return data


def parse_feed(items: list[dict], now: datetime) -> list[Event]:
    events: dict[str, Event] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        title = clean(item.get("title"))
        raw_date = clean(item.get("date"))
        if not title or not raw_date:
            continue
        try:
            time_utc = datetime.fromisoformat(raw_date).astimezone(UTC)
        except ValueError:
            continue
        currency = (clean(item.get("country")) or "ALL").upper()
        event = Event(
            id=event_id(currency, title, time_utc),
            title=title,
            currency=currency,
            impact=normalize_impact(item.get("impact")),
            time_utc=time_utc,
            forecast=clean(item.get("forecast")),
            previous=clean(item.get("previous")),
            source="faireconomy",
            updated_at=now,
        )
        # The feed sometimes repeats an item verbatim.
        events[event.id] = event
    return list(events.values())
