import json
from datetime import UTC, datetime, timedelta

import fakeredis
import httpx

from app.config import Settings
from app.models import Event, event_id
from app.service import CalendarService
from app.store import EventStore

# 12:40 UTC on Oct 14: 10 minutes after US CPI (12:30 UTC).
NOW = datetime(2026, 10, 14, 12, 40, tzinfo=UTC)


def make_event(title: str, at: datetime, **fields) -> Event:
    return Event(
        id=event_id("USD", title, at),
        title=title,
        currency="USD",
        impact=fields.pop("impact", "High"),
        time_utc=at,
        source="faireconomy",
        updated_at=NOW,
        **fields,
    )


def make_service(firecrawl_key: str | None = "fc-test"):
    pages: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = json.loads(request.content)["url"]
        # Day pages only; the next-week job is covered in test_next_week.py.
        if "?day=" in url:
            pages.append(url)
        html = '<table><tr class="calendar__row"><td></td></tr></table>'
        return httpx.Response(200, json={"success": True, "data": {"rawHtml": html}})

    store = EventStore(fakeredis.FakeRedis(decode_responses=True), now=lambda: NOW)
    service = CalendarService(
        Settings(firecrawl_api_key=firecrawl_key),
        store,
        httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        now=lambda: NOW,
    )
    return service, store, pages


def store_events(store: EventStore, *events: Event) -> None:
    store.replace_window(NOW - timedelta(days=1), NOW + timedelta(days=1), events)


async def test_rereads_pages_of_fresh_releases_without_actual():
    service, store, pages = make_service()
    store_events(
        store,
        make_event("CPI m/m", NOW - timedelta(minutes=10), forecast="0.3%"),
        # Not data releases or not pending: never re-read.
        make_event("Fed Chair Speaks", NOW - timedelta(minutes=10)),
        make_event("PPI m/m", NOW - timedelta(minutes=5), forecast="0.2%", actual="0.4%"),
        make_event("Old release", NOW - timedelta(minutes=90), forecast="1%"),
        make_event("Later today", NOW + timedelta(hours=2), forecast="1%"),
    )

    assert [e.title for e in service.awaiting_actual()] == ["CPI m/m"]
    assert await service.refresh_released() == 1
    assert len(pages) == 1
    assert pages[0].endswith("forexfactory.com/calendar?day=oct14.2026")


async def test_a_rate_decision_shown_as_pending_is_reread():
    service, store, pages = make_service()
    store_events(
        store,
        make_event("Official Cash Rate", NOW - timedelta(minutes=1), forecast="3.00%", actual="PENDING"),
    )

    assert [e.title for e in service.awaiting_actual()] == ["Official Cash Rate"]
    assert await service.refresh_released() == 1


async def test_nothing_to_do_without_pending_releases_or_key():
    service, store, pages = make_service()
    store_events(store, make_event("CPI m/m", NOW - timedelta(minutes=10), actual="0.2%"))
    assert await service.refresh_released() == 0

    assert pages == []


async def test_tick_without_a_key_never_reads_pages():
    service, store, pages = make_service(firecrawl_key=None)
    store.mark_crawled("faireconomy", NOW)  # feed fresh: no download either
    store_events(store, make_event("CPI m/m", NOW - timedelta(minutes=10), forecast="1%"))

    assert await service.tick() == {}
    assert pages == []


async def test_finalize_rereads_yesterday_once_only_when_an_actual_is_missing():
    service, store, pages = make_service()
    store.mark_crawled("faireconomy", NOW)
    yesterday = NOW - timedelta(days=1)
    # Mark every viewable day as read, so backfill stays quiet.
    for i in range(30):
        store.mark_crawled(f"page:{(NOW - timedelta(days=i)).date().isoformat()}", NOW)
    store_events(store, make_event("Late release", yesterday, forecast="1%"))

    first = await service.tick()
    second = await service.tick()

    assert first["finalized_pages"] == 1
    assert second["finalized_pages"] == 0
    assert len(pages) == 1


async def test_finalize_reads_nothing_when_yesterday_is_complete():
    service, store, pages = make_service()
    store.mark_crawled("faireconomy", NOW)
    for i in range(30):
        store.mark_crawled(f"page:{(NOW - timedelta(days=i)).date().isoformat()}", NOW)
    store_events(store, make_event("CPI m/m", NOW - timedelta(days=1), forecast="1%", actual="2%"))

    assert (await service.tick())["finalized_pages"] == 0
    assert pages == []


async def test_backfill_reads_each_missing_history_day_once():
    service, store, pages = make_service()
    store.mark_crawled("faireconomy", NOW)
    store.mark_crawled("final:" + (NOW - timedelta(days=1)).date().isoformat(), NOW)

    first = await service.tick()
    second = await service.tick()

    # Wed Oct 14 (page zone): from Sun Oct 4 (Monday of last week, less a
    # day of margin) to yesterday, Oct 13.
    assert first["backfilled_pages"] == 10
    assert second["backfilled_pages"] == 0
    assert pages[0].endswith("day=oct4.2026") and pages[-1].endswith("day=oct13.2026")
