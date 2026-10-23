"""Firecrawl credits (1 per page read): release re-reads and the this-week
fallback stay within a 1,000-credit month."""

import json
from datetime import UTC, datetime, timedelta

import fakeredis
import httpx

from app.config import Settings
from app.models import Event, event_id
from app.service import FEED_FAILED_KEY, FEED_KEY, CalendarService
from app.sources.forexfactory_page import THIS_WEEK_URL
from app.store import EventStore

# US CPI at 08:30 New York = 12:30 UTC on Oct 14.
RELEASE = datetime(2026, 10, 14, 12, 30, tzinfo=UTC)

# The day page still shows no Actual (Forex Factory never posts one).
DAY_PAGE = """
<table class="calendar__table">
<tr class="calendar__row" data-event-id="1">
  <td class="calendar__cell calendar__time">8:30am</td>
  <td class="calendar__cell calendar__currency">USD</td>
  <td class="calendar__cell calendar__impact"><span class="icon icon--ff-impact-red"></span></td>
  <td class="calendar__cell calendar__event"><span class="calendar__event-title">CPI m/m</span></td>
  <td class="calendar__cell calendar__actual"></td>
  <td class="calendar__cell calendar__forecast">0.3%</td>
  <td class="calendar__cell calendar__previous">0.4%</td>
</tr>
</table>"""

WEEK_PAGE = """
<table class="calendar__table">
<tr class="calendar__row calendar__row--day-breaker"><td colspan="10">Wed <span>Oct 14</span></td></tr>
<tr class="calendar__row" data-event-id="1">
  <td class="calendar__cell calendar__time">8:30am</td>
  <td class="calendar__cell calendar__currency">USD</td>
  <td class="calendar__cell calendar__impact"><span class="icon icon--ff-impact-red"></span></td>
  <td class="calendar__cell calendar__event"><span class="calendar__event-title">CPI m/m</span></td>
  <td class="calendar__cell calendar__actual">0.2%</td>
  <td class="calendar__cell calendar__forecast">0.3%</td>
  <td class="calendar__cell calendar__previous">0.4%</td>
</tr>
</table>"""


def make_service(start: datetime):
    clock = [start]
    reads: list[tuple[datetime, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = json.loads(request.content)["url"]
        reads.append((clock[0], url))
        return httpx.Response(
            200,
            json={"success": True, "data": {"rawHtml": WEEK_PAGE if "week=" in url else DAY_PAGE}},
        )

    store = EventStore(fakeredis.FakeRedis(decode_responses=True), now=lambda: clock[0])
    service = CalendarService(
        Settings(firecrawl_api_key="fc-test"),
        store,
        httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        now=lambda: clock[0],
    )
    return service, store, clock, reads


async def test_a_release_without_actual_costs_about_ten_reads_not_thirty():
    service, store, clock, reads = make_service(RELEASE)
    cpi = Event(
        id=event_id("USD", "CPI m/m", RELEASE),
        title="CPI m/m",
        currency="USD",
        impact="High",
        time_utc=RELEASE,
        forecast="0.3%",
        previous="0.4%",
        source="faireconomy",
        updated_at=RELEASE,
    )
    store.replace_window(RELEASE - timedelta(hours=1), RELEASE + timedelta(hours=1), [cpi])

    # One tick every 2 minutes for the whole release window and beyond.
    for minute in range(0, 72, 2):
        clock[0] = RELEASE + timedelta(minutes=minute)
        await service.refresh_released()

    minutes = [int((at - RELEASE).total_seconds() // 60) for at, _ in reads]
    # Every tick for 10 minutes, then every 10 minutes until 60.
    assert minutes == [2, 4, 6, 8, 10, 20, 30, 40, 50, 60]


async def test_this_week_is_read_only_while_the_export_keeps_failing():
    start = datetime(2026, 10, 14, 13, 0, tzinfo=UTC)
    service, store, clock, reads = make_service(start)

    def this_week_reads():
        return [at for at, url in reads if url == THIS_WEEK_URL]

    # The export works: never read.
    store.mark_crawled(FEED_KEY, start)
    assert await service.refresh_this_week() == 0

    # Failing, but for less than 2 hours since the last success.
    clock[0] = start + timedelta(hours=1)
    store.mark_crawled(FEED_FAILED_KEY, clock[0])
    assert await service.refresh_this_week() == 0

    # 2 hours without the export: read, then at most every 6 hours.
    for hours in (2, 3, 7, 8, 9):
        clock[0] = start + timedelta(hours=hours)
        store.mark_crawled(FEED_FAILED_KEY, clock[0])
        await service.refresh_this_week()
    assert this_week_reads() == [start + timedelta(hours=2), start + timedelta(hours=8)]

    # The page's Actual is stored.
    stored = store.query(RELEASE, RELEASE + timedelta(minutes=1))
    assert [(e.title, e.actual) for e in stored] == [("CPI m/m", "0.2%")]

    # The export recovers: no more page reads.
    clock[0] = start + timedelta(hours=20)
    store.mark_crawled(FEED_KEY, clock[0])
    assert await service.refresh_this_week() == 0
    assert len(this_week_reads()) == 2
