import json

import fakeredis
import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app

FEED = [
    {"title": "CPI m/m", "country": "USD", "date": "2026-10-14T08:30:00-04:00",
     "impact": "High", "forecast": "0.3%", "previous": "0.4%"},
    {"title": "GDP q/y", "country": "CNY", "date": "2026-10-18T22:00:00-04:00",
     "impact": "Low", "forecast": "4.4%", "previous": "4.3%"},
    {"title": "German ZEW Economic Sentiment", "country": "EUR",
     "date": "2026-10-13T05:00:00-04:00", "impact": "Medium", "forecast": "", "previous": "-31.7"},
    {"title": "Bank Holiday", "country": "JPY", "date": "2026-10-12T00:00:00-04:00",
     "impact": "Holiday", "forecast": "", "previous": ""},
    # The feed sometimes repeats an item verbatim.
    {"title": "Bank Holiday", "country": "JPY", "date": "2026-10-12T00:00:00-04:00",
     "impact": "Holiday", "forecast": "", "previous": ""},
]  # fmt: skip

JOBS_TOKEN = "test-jobs-token"
AUTH = {"Authorization": f"Bearer {JOBS_TOKEN}"}

EMPTY_PAGE = '<table><tr class="calendar__row calendar__row--day-breaker"><td>Day</td></tr></table>'

PAGE_HTML = """
<table class="calendar__table">
  <tr class="calendar__row calendar__row--day-breaker"><td colspan="8">Wed Oct 14</td></tr>
  <tr class="calendar__row" data-event-id="141001">
    <td class="calendar__cell calendar__time">8:30am</td>
    <td class="calendar__cell calendar__currency">USD</td>
    <td class="calendar__cell calendar__impact">
      <span title="High Impact Expected" class="icon icon--ff-impact-red"></span></td>
    <td class="calendar__cell calendar__event"><span class="calendar__event-title">CPI m/m</span></td>
    <td class="calendar__cell calendar__actual"><span class="better">0.2%</span></td>
    <td class="calendar__cell calendar__forecast">0.3%</td>
    <td class="calendar__cell calendar__previous">0.4%</td>
  </tr>
  <tr class="calendar__row" data-event-id="141002">
    <td class="calendar__cell calendar__time"></td>
    <td class="calendar__cell calendar__currency">USD</td>
    <td class="calendar__cell calendar__impact">
      <span title="Medium Impact Expected" class="icon icon--ff-impact-ora"></span></td>
    <td class="calendar__cell calendar__event"><span class="calendar__event-title">Core CPI m/m</span></td>
    <td class="calendar__cell calendar__actual worse">0.4%</td>
    <td class="calendar__cell calendar__forecast">0.3%</td>
    <td class="calendar__cell calendar__previous">0.3%</td>
  </tr>
  <tr class="calendar__row" data-event-id="141003">
    <td class="calendar__cell calendar__time">Tentative</td>
    <td class="calendar__cell calendar__currency">GBP</td>
    <td class="calendar__cell calendar__impact">
      <span title="Low Impact Expected" class="icon icon--ff-impact-yel"></span></td>
    <td class="calendar__cell calendar__event"><span class="calendar__event-title">BOE Gov Speaks</span></td>
    <td class="calendar__cell calendar__actual"></td>
    <td class="calendar__cell calendar__forecast"></td>
    <td class="calendar__cell calendar__previous"></td>
  </tr>
</table>
"""


class FakeUpstream:
    """Serves the feed and Firecrawl pages; counts requests."""

    def __init__(self):
        self.feed = FEED
        self.feed_status = 200
        self.page_html = PAGE_HTML
        self.feed_calls = 0
        self.firecrawl_calls: list[dict] = []
        # HTTP status Firecrawl answers with; 402 = out of credits.
        self.firecrawl_status = 200

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.host == "nfs.faireconomy.media":
            self.feed_calls += 1
            if self.feed_status != 200:
                return httpx.Response(self.feed_status, text="Request Denied")
            return httpx.Response(200, text=json.dumps(self.feed))
        if request.url.host == "api.firecrawl.dev":
            body = json.loads(request.content)
            self.firecrawl_calls.append(body)
            assert request.headers["Authorization"] == "Bearer fc-test"
            if self.firecrawl_status != 200:
                return httpx.Response(self.firecrawl_status, json={"error": "Insufficient credits"})
            html = self.page_html if body["url"].endswith("day=oct14.2026") else EMPTY_PAGE
            return httpx.Response(
                200,
                json={"success": True, "data": {"rawHtml": html, "metadata": {"creditsUsed": 1}}},
            )
        return httpx.Response(404)


@pytest.fixture
def upstream() -> FakeUpstream:
    return FakeUpstream()


@pytest.fixture
def redis_server() -> fakeredis.FakeServer:
    """One in-memory Redis per test, shared by every client the test makes."""
    return fakeredis.FakeServer()


def make_client(
    upstream: FakeUpstream, redis_server: fakeredis.FakeServer, **overrides
) -> TestClient:
    # Fixtures use fixed 2026 dates; date-based limits are off unless a test sets them.
    overrides.setdefault("page_history_weeks", 100_000)
    overrides.setdefault("retention_days", 100_000)
    overrides.setdefault("jobs_token", JOBS_TOKEN)
    settings = Settings(refresh_minutes=0, **overrides)
    app = create_app(
        settings,
        transport=httpx.MockTransport(upstream.handler),
        redis_client=fakeredis.FakeRedis(server=redis_server, decode_responses=True),
    )
    return TestClient(app)
