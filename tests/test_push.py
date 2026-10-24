import json
from datetime import UTC, datetime, timedelta

import fakeredis
import httpx
import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from app.config import Settings
from app.models import Event, event_id
from app.push import LANGUAGES, FcmSender, PushError, message_text
from app.service import CalendarService
from app.store import EventStore

# 12:35 UTC on Oct 14: 5 minutes after US CPI (12:30 UTC).
NOW = datetime(2026, 10, 14, 12, 35, tzinfo=UTC)

_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
ACCOUNT = {
    "project_id": "whalio-test",
    "client_email": "push@whalio-test.iam.gserviceaccount.com",
    "private_key_id": "key-1",
    "private_key": _KEY.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode(),
}


def make_event(title: str, at: datetime, **fields) -> Event:
    return Event(
        id=event_id("USD", title, at),
        title=title,
        currency="USD",
        impact="High",
        time_utc=at,
        source="forexfactory",
        updated_at=NOW,
        **fields,
    )


class FakeFcm:
    """Token endpoint and FCM send endpoint; records messages."""

    def __init__(self):
        self.tokens = 0
        self.messages: list[dict] = []
        # Topics whose send answers HTTP 500.
        self.failing: set[str] = set()

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.host == "oauth2.googleapis.com":
            self.tokens += 1
            assertion = dict(httpx.QueryParams(request.content.decode()))["assertion"]
            claims = jwt.decode(
                assertion,
                _KEY.public_key(),
                algorithms=["RS256"],
                audience="https://oauth2.googleapis.com/token",
                # Signed with the tests' fixed clock.
                options={"verify_exp": False, "verify_iat": False},
            )
            assert claims["iss"] == ACCOUNT["client_email"]
            return httpx.Response(200, json={"access_token": "token-1", "expires_in": 3600})
        assert request.url.path == "/v1/projects/whalio-test/messages:send"
        assert request.headers["Authorization"] == "Bearer token-1"
        message = json.loads(request.content)["message"]
        if message["topic"] in self.failing:
            return httpx.Response(500)
        self.messages.append(message)
        return httpx.Response(200, json={"name": "projects/whalio-test/messages/1"})


def make_service(fcm: FakeFcm | None):
    handler = fcm.handler if fcm else (lambda request: httpx.Response(404))
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    store = EventStore(fakeredis.FakeRedis(decode_responses=True), now=lambda: NOW)
    push = FcmSender(ACCOUNT, client, clock=NOW.timestamp) if fcm else None
    service = CalendarService(Settings(), store, client, now=lambda: NOW, push=push)
    return service, store


def store_events(store: EventStore, *events: Event) -> None:
    store.replace_window(NOW - timedelta(days=1), NOW + timedelta(days=1), events)


def test_message_text_leaves_out_missing_values():
    event = make_event(
        "CPI m/m", NOW, actual="0.2%", forecast="0.3%", previous="0.4%", actual_status="better"
    )
    assert message_text(event, "vi") == (
        "USD · CPI m/m",
        "Thực tế 0.2% · Dự báo 0.3% · Trước 0.4% · Tốt hơn dự báo",
    )
    bare = make_event("CPI m/m", NOW, actual="0.2%", previous="0.4%")
    assert message_text(bare, "en") == ("USD · CPI m/m", "Actual 0.2% · Previous 0.4%")


async def test_sends_a_fresh_actual_once_per_language():
    fcm = FakeFcm()
    service, store = make_service(fcm)
    cpi = make_event("CPI m/m", NOW - timedelta(minutes=5), actual="0.2%", forecast="0.3%")
    store_events(
        store,
        cpi,
        # Still waiting, too old, or not released yet: nothing to send.
        make_event("PPI m/m", NOW - timedelta(minutes=5), forecast="0.2%"),
        make_event("Retail Sales", NOW - timedelta(hours=4), actual="1%"),
        make_event("Later", NOW + timedelta(hours=1), actual="1%"),
    )

    assert await service.push_actuals() == len(LANGUAGES)
    assert sorted(m["topic"] for m in fcm.messages) == sorted(
        f"actual_{cpi.id}_{lang}" for lang in LANGUAGES
    )
    vi = next(m for m in fcm.messages if m["topic"].endswith("_vi"))
    assert vi["notification"] == {"title": "USD · CPI m/m", "body": "Thực tế 0.2% · Dự báo 0.3%"}
    assert vi["android"]["notification"]["channel_id"] == "actual_alerts"
    assert fcm.tokens == 1

    # A revised Actual is not sent again.
    store_events(store, cpi.model_copy(update={"actual": "0.3%"}))
    assert await service.push_actuals() == 0


async def test_a_failed_send_is_retried_next_tick_for_that_language_only():
    fcm = FakeFcm()
    service, store = make_service(fcm)
    cpi = make_event("CPI m/m", NOW - timedelta(minutes=5), actual="0.2%")
    store_events(store, cpi)
    fcm.failing = {f"actual_{cpi.id}_ja"}

    assert await service.push_actuals() == len(LANGUAGES) - 1
    assert "push" in service.last_error

    fcm.failing = set()
    assert await service.push_actuals() == 1
    assert fcm.messages[-1]["topic"] == f"actual_{cpi.id}_ja"


async def test_nothing_is_sent_without_a_service_account():
    service, store = make_service(None)
    store_events(store, make_event("CPI m/m", NOW - timedelta(minutes=5), actual="0.2%"))

    assert await service.push_actuals() == 0
    assert "pushed" not in await service.tick()
    assert service.status()["push_enabled"] is False


async def test_token_failure_raises_push_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "invalid_grant"})

    sender = FcmSender(ACCOUNT, httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    event = make_event("CPI m/m", NOW, actual="0.2%")
    try:
        await sender.send(event, "vi")
    except PushError as error:
        assert "token" in str(error)
    else:
        raise AssertionError("expected PushError")
