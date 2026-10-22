import hashlib
from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel

Impact = Literal["High", "Medium", "Low", "Holiday"]
ActualStatus = Literal["better", "worse"]
# forexfactory: a day page; forexfactory_week: the next-week page (schedule only).
Source = Literal["faireconomy", "forexfactory", "forexfactory_week"]


class Event(BaseModel):
    id: str
    title: str
    currency: str
    impact: Impact
    time_utc: datetime
    all_day: bool = False
    # Forex Factory's label when there is no clock time: "All Day", "Tentative".
    time_note: str | None = None
    actual: str | None = None
    forecast: str | None = None
    previous: str | None = None
    actual_status: ActualStatus | None = None
    url: str | None = None
    source: Source
    updated_at: datetime


class EventOut(Event):
    date: date
    # "HH:MM" in the requested time zone, or the time note for all-day events.
    time: str


class CalendarResponse(BaseModel):
    start: date
    end: date
    timezone: str
    total: int
    count: int
    events: list[EventOut]


def event_id(currency: str, title: str, time_utc: datetime) -> str:
    """Stable id shared by both sources, so their rows merge."""
    key = f"{currency.upper()}|{title.strip().lower()}|{time_utc.isoformat()}"
    return hashlib.sha1(key.encode()).hexdigest()[:16]


def normalize_impact(value: str | None) -> Impact:
    text = (value or "").lower()
    if "high" in text or "red" in text:
        return "High"
    if "medium" in text or "ora" in text:
        return "Medium"
    if "holiday" in text or "non" in text or "gra" in text:
        return "Holiday"
    return "Low"


def clean(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None
