from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import fakeredis
import httpx

from app.config import Settings
from app.service import CalendarService
from app.store import EventStore

START = datetime(2026, 10, 22, 19, 0, tzinfo=UTC)
HANOI = ZoneInfo("Asia/Ho_Chi_Minh")


async def test_a_rate_limited_feed_is_not_retried_before_the_refresh_interval():
    clock = [START]
    feed_calls: list[datetime] = []

    def handler(request: httpx.Request) -> httpx.Response:
        feed_calls.append(clock[0])
        return httpx.Response(429, text="<html>Too many requests</html>")

    service = CalendarService(
        Settings(refresh_minutes=30),
        EventStore(fakeredis.FakeRedis(decode_responses=True), now=lambda: clock[0]),
        httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        now=lambda: clock[0],
    )
    day = date(2026, 10, 23)

    # Every app request and every 2-minute tick used to retry the feed,
    # which kept the server's IP blocked.
    await service.events(day, day, HANOI)
    for minutes in (2, 10, 29):
        clock[0] = START + timedelta(minutes=minutes)
        await service.tick()
        await service.events(day, day, HANOI)
    assert feed_calls == [START]

    clock[0] = START + timedelta(minutes=30)
    await service.tick()
    assert feed_calls == [START, START + timedelta(minutes=30)]
    assert "HTTP 429" in service.status()["last_error"]
