from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup

from app.models import Event
from app.sources.faireconomy import parse_feed
from app.sources.forexfactory_page import page_timezone, page_url, parse_day

from .conftest import FEED, PAGE_HTML

NOW = datetime(2026, 10, 14, 12, tzinfo=UTC)
NEW_YORK = ZoneInfo("America/New_York")


def test_feed_maps_items_to_utc_and_drops_duplicates():
    events = parse_feed(FEED, NOW)

    assert len(events) == 4
    cpi = next(e for e in events if e.title == "CPI m/m")
    assert cpi.time_utc == datetime(2026, 10, 14, 12, 30, tzinfo=UTC)
    assert cpi.currency == "USD"
    assert cpi.impact == "High"
    assert (cpi.forecast, cpi.previous, cpi.actual) == ("0.3%", "0.4%", None)
    zew = next(e for e in events if e.currency == "EUR")
    assert zew.forecast is None
    assert {e.impact for e in events} == {"High", "Low", "Medium", "Holiday"}


def test_feed_skips_malformed_items():
    assert parse_feed([{"title": "x"}, {"date": "bad", "title": "y"}, "junk"], NOW) == []


def test_feed_items_that_used_to_abort_the_refresh_are_skipped_or_cleaned():
    cpi = {"title": "CPI m/m", "country": "USD", "date": "2026-10-14T08:30:00-04:00"}
    events = parse_feed(
        [
            {**cpi, "impact": 1},
            {**cpi, "title": "Year one", "date": "0001-01-01T00:00:00+01:00"},
            {**cpi, "title": "Far future", "date": "9999-12-31T12:00:00+00:00"},
        ],
        NOW,
    )
    assert [(e.title, e.impact) for e in events] == [("CPI m/m", "Low")]


def test_page_url_uses_forexfactory_day_format():
    assert page_url(date(2026, 10, 4)) == "https://www.forexfactory.com/calendar?day=oct4.2026"


def test_page_rows_carry_actual_status_and_inherit_time():
    events, tz = parse_day(PAGE_HTML, date(2026, 10, 14), NEW_YORK, NOW)
    # No header clock in the sample: the fallback zone is used.
    assert tz is NEW_YORK

    assert [e.title for e in events] == ["CPI m/m", "Core CPI m/m", "BOE Gov Speaks"]
    cpi, core, boe = events
    assert cpi.time_utc == datetime(2026, 10, 14, 12, 30, tzinfo=UTC)
    assert (cpi.actual, cpi.actual_status, cpi.impact) == ("0.2%", "better", "High")
    assert cpi.url.endswith("day=oct14.2026#detail=141001")
    # A blank time cell repeats the time above.
    assert core.time_utc == cpi.time_utc
    assert (core.actual_status, core.impact) == ("worse", "Medium")
    assert boe.all_day and boe.time_note == "Tentative"
    assert boe.actual is None and boe.impact == "Low"


def test_pending_actual_is_no_actual():
    html = PAGE_HTML.replace(">0.2%<", ">PENDING<", 1)
    assert ">PENDING<" in html
    cpi = parse_day(html, date(2026, 10, 14), NEW_YORK, NOW)[0][0]
    assert cpi.actual is None
    # Rows stored with the placeholder read back without it.
    assert Event.model_validate_json(cpi.model_copy(update={"actual": "PENDING"}).model_dump_json()).actual is None


def test_page_and_feed_rows_share_ids():
    feed_cpi = next(e for e in parse_feed(FEED, NOW) if e.title == "CPI m/m")
    page_cpi = parse_day(PAGE_HTML, date(2026, 10, 14), NEW_YORK, NOW)[0][0]
    assert feed_cpi.id == page_cpi.id


def _with_clock(clock: str) -> str:
    return (
        '<div class="calendar__header-time"><a href="/timezone">' + clock + "</a></div>"
        + PAGE_HTML
    )


def test_page_zone_comes_from_the_header_clock():
    # Observed through Jina: "5:21pm" on the page at 19:51 UTC is UTC-2:30.
    fetched = datetime(2026, 10, 18, 19, 51, 40, tzinfo=UTC)
    soup = BeautifulSoup(_with_clock("5:21pm"), "html.parser")

    tz = page_timezone(soup, fetched)

    assert tz.utcoffset(None) == timedelta(hours=-2, minutes=-30)


def test_page_zone_handles_midnight_and_eastern_offsets():
    late = datetime(2026, 10, 18, 23, 50, tzinfo=UTC)
    tokyo = page_timezone(BeautifulSoup(_with_clock("8:50am"), "html.parser"), late)
    assert tokyo.utcoffset(None) == timedelta(hours=9)
    early = datetime(2026, 10, 19, 1, 5, tzinfo=UTC)
    eastern = page_timezone(BeautifulSoup(_with_clock("9:05pm"), "html.parser"), early)
    assert eastern.utcoffset(None) == timedelta(hours=-4)


def test_page_times_use_the_detected_zone():
    fetched = datetime(2026, 10, 18, 19, 51, tzinfo=UTC)
    events, _ = parse_day(_with_clock("5:21pm"), date(2026, 10, 19), NEW_YORK, fetched)

    # "8:30am" at UTC-2:30 is 11:00 UTC.
    assert events[0].time_utc == datetime(2026, 10, 19, 11, 0, tzinfo=UTC)


def test_unrendered_pages_are_rejected():
    import pytest

    from app.sources import SourceError
    from app.sources.page_fetchers import _rendered_calendar

    blank = '<tr class="calendar__row"><td class="calendar__cell calendar__cell--blank"></td></tr>'
    with pytest.raises(SourceError, match="not rendered"):
        _rendered_calendar(blank, "firecrawl", 1)
    with pytest.raises(SourceError, match="no calendar table") as rejected:
        _rendered_calendar("<html>Just a moment...</html>", "firecrawl", 1)
    # The reader was paid even though the page is unusable.
    assert rejected.value.cost == 1
