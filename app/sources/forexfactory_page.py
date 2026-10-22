"""Parsing of forexfactory.com day pages (fetched by `page_fetchers`).

Parsing follows the calendar table's markup, as used by
github.com/xclunderrated/ForexFactory-Unofficial-API:

    tr.calendar__row
      td.calendar__time        "8:30am" | "All Day" | "Tentative" | "" (same as above)
      td.calendar__currency    "USD"
      td.calendar__impact      span[title="High Impact Expected"], class *impact-red*
      td.calendar__event       span.calendar__event-title
      td.calendar__actual      text, class/child "better" | "worse"
      td.calendar__forecast
      td.calendar__previous

Times on the page are in the zone Forex Factory picks for the visitor, which
depends on where the reader's servers are (observed: Jina UTC-2:30,
Firecrawl UTC-5; not US Eastern). The
page header shows the current time in that zone (`.calendar__header-time`), so
the parser derives the offset from it and the fetch time; `FF_PAGE_TIMEZONE`
is only the fallback when the header is missing.
"""

import re
from datetime import UTC, date, datetime, time, timedelta, timezone, tzinfo

from bs4 import BeautifulSoup, Tag

from ..models import ActualStatus, Event, clean, event_id, normalize_impact

_MONTHS = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]
_CLOCK = re.compile(r"^(\d{1,2}):(\d{2})\s*(am|pm)$", re.IGNORECASE)


def page_url(day: date) -> str:
    return f"https://www.forexfactory.com/calendar?day={_MONTHS[day.month - 1]}{day.day}.{day.year}"


def _text(row: Tag, selector: str) -> str | None:
    cell = row.select_one(selector)
    return clean(cell.get_text(" ", strip=True)) if cell else None


def _impact(row: Tag) -> str:
    cell = row.select_one(".calendar__impact")
    if cell is None:
        return "Low"
    hints = []
    for element in [cell, *cell.find_all(True)]:
        hints.append(element.get("title") or "")
        hints.append(" ".join(element.get("class") or []))
    return normalize_impact(" ".join(hints))


def _actual_status(row: Tag) -> ActualStatus | None:
    cell = row.select_one(".calendar__actual")
    if cell is None:
        return None
    classes = " ".join(
        " ".join(e.get("class") or []) for e in [cell, *cell.find_all(True)]
    )
    if "better" in classes:
        return "better"
    if "worse" in classes:
        return "worse"
    return None


def _time_utc(day: date, label: str | None, tz: tzinfo) -> tuple[datetime, bool, str | None]:
    """Release time, whether it is all-day, and the label when not a clock time."""
    match = _CLOCK.match(label or "")
    if match:
        hour = int(match[1]) % 12 + (12 if match[3].lower() == "pm" else 0)
        local = datetime.combine(day, time(hour, int(match[2])), tz)
        return local.astimezone(UTC), False, None
    midnight = datetime.combine(day, time(0), tz)
    return midnight.astimezone(UTC), True, label or "All Day"


def page_timezone(soup: BeautifulSoup, fetched_at: datetime) -> tzinfo | None:
    """Zone of the page's times: header clock minus the UTC fetch time,
    rounded to 15 minutes. None when the header clock is missing."""
    header = soup.select_one(".calendar__header-time")
    match = _CLOCK.match(header.get_text(strip=True)) if header else None
    if not match:
        return None
    local = (int(match[1]) % 12 + (12 if match[3].lower() == "pm" else 0)) * 60 + int(match[2])
    utc = fetched_at.astimezone(UTC)
    diff = local - (utc.hour * 60 + utc.minute)
    # Real offsets lie within UTC-12 .. UTC+14.
    if diff < -12 * 60:
        diff += 24 * 60
    elif diff > 14 * 60:
        diff -= 24 * 60
    return timezone(timedelta(minutes=round(diff / 15) * 15))


def parse_day(
    html: str, day: date, fallback_tz: tzinfo, now: datetime
) -> tuple[list[Event], tzinfo]:
    """Events of one day page, and the time zone the page was rendered in."""
    soup = BeautifulSoup(html, "html.parser")
    tz = page_timezone(soup, now) or fallback_tz
    events: dict[str, Event] = {}
    current_time: str | None = None
    for row in soup.select("tr.calendar__row"):
        # A blank time cell means "same time as the row above".
        label = _text(row, ".calendar__time")
        if label:
            current_time = label
        currency = _text(row, ".calendar__currency")
        title = _text(row, ".calendar__event-title") or _text(row, ".calendar__event")
        if not currency or not title:
            continue
        time_utc, all_day, note = _time_utc(day, current_time, tz)
        detail_id = row.get("data-event-id") or row.get("data-eventid")
        event = Event(
            id=event_id(currency, title, time_utc),
            title=title,
            currency=currency.upper(),
            impact=_impact(row),
            time_utc=time_utc,
            all_day=all_day,
            time_note=note,
            actual=_text(row, ".calendar__actual"),
            forecast=_text(row, ".calendar__forecast"),
            previous=_text(row, ".calendar__previous"),
            actual_status=_actual_status(row),
            url=f"{page_url(day)}#detail={detail_id}" if detail_id else page_url(day),
            source="forexfactory",
            updated_at=now,
        )
        events[event.id] = event
    return list(events.values()), tz
