from datetime import UTC, datetime, timedelta

import fakeredis
import httpx
import pytest

from app.holidays import HolidayService
from app.sources import SourceError
from app.store import EventStore

from .conftest import make_client

START = datetime(2026, 11, 2, 9, tzinfo=UTC)


class Clock:
    def __init__(self):
        self.value = START

    def __call__(self) -> datetime:
        return self.value


def make_service(upstream, redis_server, clock, api_key="fcal-test") -> HolidayService:
    store = EventStore(
        fakeredis.FakeRedis(server=redis_server, decode_responses=True),
        retention=timedelta(days=15),
        now=clock,
    )
    client = httpx.AsyncClient(transport=httpx.MockTransport(upstream.handler))
    return HolidayService(store, client, api_key, now=clock)


def test_endpoint_serves_the_rows_whalio_reads(upstream, redis_server):
    with make_client(upstream, redis_server, finance_calendar_api_key="fcal-test") as client:
        body = client.get("/api/v1/holidays").json()

    assert body["count"] == 2
    assert body["source"] == "https://www.financecalendar.com"
    assert body["holidays"][1] == {
        "date": "2026-11-27",
        "name": "Day After Thanksgiving",
        "status": "early_close",
        "close_time_et": "13:00",
    }


def test_endpoint_without_a_key_answers_503(upstream, redis_server):
    with make_client(upstream, redis_server) as client:
        response = client.get("/api/v1/holidays")

    assert response.status_code == 503
    assert upstream.holidays_calls == 0


async def test_provider_is_called_at_most_once_a_day(upstream, redis_server):
    clock = Clock()
    service = make_service(upstream, redis_server, clock)

    await service.holidays()
    clock.value += timedelta(hours=23)
    await service.holidays()
    assert upstream.holidays_calls == 1

    clock.value += timedelta(hours=1)
    await service.holidays()
    assert upstream.holidays_calls == 2


async def test_failing_provider_serves_the_saved_list_and_waits_before_retrying(
    upstream, redis_server
):
    clock = Clock()
    service = make_service(upstream, redis_server, clock)
    await service.holidays()
    upstream.holidays_status = 401
    upstream.holidays = []

    clock.value += timedelta(days=1)
    body = await service.holidays()
    clock.value += timedelta(minutes=30)
    await service.holidays()

    assert body["count"] == 2
    assert body["fetched_at"] == START.isoformat()
    assert upstream.holidays_calls == 2


async def test_failing_provider_with_nothing_saved_raises(upstream, redis_server):
    upstream.holidays_status = 500
    service = make_service(upstream, redis_server, Clock())

    with pytest.raises(SourceError):
        await service.holidays()
