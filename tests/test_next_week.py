import json
from datetime import UTC, date, datetime, timedelta

import fakeredis
import httpx

from app.config import Settings
from app.models import Event, event_id
from app.service import CalendarService
from app.sources.forexfactory_page import NEXT_WEEK_URL, breaker_date, parse_week
from app.store import EventStore

# The page header shows 3:22pm while it is 20:22 UTC: the page is in UTC-5.
FETCHED = datetime(2026, 10, 22, 20, 22, tzinfo=UTC)


def _row(time: str, currency: str, impact: str, title: str, forecast: str = "", eid: str = "1"):
    return f"""
<tr class="calendar__row" data-event-id="{eid}">
  <td class="calendar__cell calendar__time"><div><span>{time}</span></div></td>
  <td class="calendar__cell calendar__currency"><abbr><span>{currency}</span></abbr></td>
  <td class="calendar__cell calendar__impact"><span class="icon icon--ff-impact-{impact}"></span></td>
  <td class="calendar__cell calendar__event"><span class="calendar__event-title">{title}</span></td>
  <td class="calendar__cell calendar__actual"></td>
  <td class="calendar__cell calendar__forecast">{forecast}</td>
  <td class="calendar__cell calendar__previous"></td>
</tr>"""


# Shape of https://www.forexfactory.com/calendar?week=next on 2026-10-22.
WEEK_PAGE = f"""
<div class="calendar__header-time"><a href="/timezone">3:22pm</a></div>
<table class="calendar__table">
<tr class="calendar__row calendar__row--day-breaker"><td class="calendar__cell" colspan="10">Sun <span>Oct 25</span></td></tr>
{_row("All Day", "NZD", "gra", "Bank Holiday", eid="147447")}
{_row("4:40pm", "AUD", "yel", "RBA Assist Gov Jones Speaks", eid="155132")}
<tr class="calendar__row calendar__row--day-breaker calendar__row--alt"><td class="calendar__cell" colspan="10">Mon <span>Oct 26</span></td></tr>
{_row("4:00am", "EUR", "ora", "German ifo Business Climate", "90.0", eid="150001")}
{_row("", "EUR", "yel", "Belgian NBB Business Climate", eid="150002")}
</table>"""


def test_week_page_rows_take_their_day_from_the_breaker_rows():
    events, tz, days = parse_week(WEEK_PAGE, UTC, FETCHED)

    assert tz.utcoffset(None) == timedelta(hours=-5)
    assert days == [date(2026, 10, 25), date(2026, 10, 26)]
    by_title = {e.title: e for e in events}
    holiday = by_title["Bank Holiday"]
    assert (holiday.all_day, holiday.time_utc) == (True, datetime(2026, 10, 25, 5, tzinfo=UTC))
    assert by_title["RBA Assist Gov Jones Speaks"].time_utc == datetime(
        2026, 10, 25, 21, 40, tzinfo=UTC
    )
    ifo = by_title["German ifo Business Climate"]
    assert (ifo.time_utc, ifo.impact, ifo.forecast) == (
        datetime(2026, 10, 26, 9, tzinfo=UTC),
        "Medium",
        "90.0",
    )
    # A blank time cell repeats the row above, within the same day.
    assert by_title["Belgian NBB Business Climate"].time_utc == ifo.time_utc
    assert ifo.url == "https://www.forexfactory.com/calendar?day=oct26.2026#detail=150001"
    assert {e.source for e in events} == {"forexfactory_week"}


def test_breaker_dates_pick_the_year_nearest_the_fetch():
    assert breaker_date("Thu Jan 1", date(2026, 12, 30)) == date(2027, 1, 1)
    assert breaker_date("Thu Dec 31", date(2027, 1, 2)) == date(2026, 12, 31)
    assert breaker_date("Today", date(2026, 10, 22)) is None


def make_service(html: str = WEEK_PAGE):
    clock = [FETCHED]
    urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.firecrawl.dev":
            urls.append(json.loads(request.content)["url"])
            return httpx.Response(200, json={"success": True, "data": {"rawHtml": html}})
        # The export, still on this week.
        return httpx.Response(200, json=[])

    store = EventStore(fakeredis.FakeRedis(decode_responses=True), now=lambda: clock[0])
    service = CalendarService(
        Settings(firecrawl_api_key="fc-test", page_history_weeks=0),
        store,
        httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        now=lambda: clock[0],
    )
    return service, store, urls, clock


async def test_next_week_is_read_once_a_day_and_stored():
    service, store, urls, clock = make_service()

    await service.tick()
    clock[0] += timedelta(minutes=2)
    await service.tick()
    stored = store.query(datetime(2026, 10, 25, tzinfo=UTC), datetime(2026, 10, 27, tzinfo=UTC))

    assert [u for u in urls if u == NEXT_WEEK_URL] == [NEXT_WEEK_URL]
    assert len(stored) == 4
    assert service.status()["usage_this_month"] == {"firecrawl": len(urls)}

    clock[0] += timedelta(days=1)
    await service.tick()
    assert [u for u in urls if u == NEXT_WEEK_URL] == [NEXT_WEEK_URL] * 2


async def test_the_export_replaces_next_week_rows_once_it_rolls_over():
    service, store, _, _ = make_service()
    await service.crawl_next_week()
    ifo_time = datetime(2026, 10, 26, 9, tzinfo=UTC)

    # The export now covers that week: ifo kept, the speech was cancelled.
    feed_ifo = Event(
        id=event_id("EUR", "German ifo Business Climate", ifo_time),
        title="German ifo Business Climate",
        currency="EUR",
        impact="Medium",
        time_utc=ifo_time,
        forecast="90.1",
        source="faireconomy",
        updated_at=FETCHED,
    )
    store.replace_window(
        datetime(2026, 10, 25, 4, tzinfo=UTC),
        datetime(2026, 10, 27, 4, tzinfo=UTC),
        [feed_ifo],
        only_sources={"faireconomy", "forexfactory_week"},
    )
    titles = {
        e.title: e
        for e in store.query(datetime(2026, 10, 25, tzinfo=UTC), datetime(2026, 10, 27, tzinfo=UTC))
    }

    assert "RBA Assist Gov Jones Speaks" not in titles
    ifo = titles["German ifo Business Climate"]
    assert (ifo.forecast, ifo.source) == ("90.1", "faireconomy")
    # The page's event link survives the merge.
    assert ifo.url.endswith("#detail=150001")
