from datetime import UTC, datetime, timedelta

import fakeredis
import pytest

from app.models import Event, event_id
from app.store import EventStore

# Real time: fakeredis expires keys against the real clock.
NOW = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)


def make_event(title: str, at: datetime, **fields) -> Event:
    return Event(
        id=event_id("USD", title, at),
        title=title,
        currency="USD",
        impact="High",
        time_utc=at,
        source=fields.pop("source", "faireconomy"),
        updated_at=NOW,
        **fields,
    )


@pytest.fixture
def client() -> fakeredis.FakeRedis:
    return fakeredis.FakeRedis(decode_responses=True)


@pytest.fixture
def clock() -> list[datetime]:
    return [NOW]


@pytest.fixture
def store(client, clock) -> EventStore:
    return EventStore(client, retention=timedelta(days=60), now=lambda: clock[0])


def test_each_event_expires_60_days_after_its_release(store, client):
    release = NOW + timedelta(days=2)
    event = make_event("CPI m/m", release)

    store.replace_window(NOW, NOW + timedelta(days=7), [event])

    expires_at = client.pexpiretime(f"fxcal:event:{event.id}") / 1000
    assert expires_at == (release + timedelta(days=60)).timestamp()


def test_events_already_past_retention_are_not_stored(store):
    old = make_event("Old", NOW - timedelta(days=61))
    recent = make_event("Recent", NOW - timedelta(days=59))

    stored = store.replace_window(NOW - timedelta(days=70), NOW, [old, recent])

    assert stored == 1
    assert [e.title for e in store.query(NOW - timedelta(days=70), NOW)] == ["Recent"]


def test_index_is_pruned_when_events_age_out(store, client, clock):
    event = make_event("CPI m/m", NOW)
    store.replace_window(NOW, NOW + timedelta(hours=1), [event])
    assert store.count() == 1

    # 61 days later Redis has expired the key; the index entry is pruned.
    clock[0] = NOW + timedelta(days=61)
    client.delete(f"fxcal:event:{event.id}")

    assert store.count() == 0
    assert store.query(NOW - timedelta(days=1), NOW + timedelta(days=1)) == []


def test_query_skips_and_cleans_expired_index_entries(store, client):
    keep = make_event("Keep", NOW)
    gone = make_event("Gone", NOW + timedelta(minutes=5))
    store.replace_window(NOW, NOW + timedelta(hours=1), [keep, gone])
    client.delete(f"fxcal:event:{gone.id}")

    events = store.query(NOW, NOW + timedelta(hours=1))

    assert [e.title for e in events] == ["Keep"]
    assert client.zcard("fxcal:events:by_time") == 1


def test_feed_refresh_keeps_page_data_and_only_removes_its_own_rows(store):
    window = (NOW, NOW + timedelta(days=1))
    page_row = make_event(
        "CPI m/m",
        NOW + timedelta(hours=1),
        actual="0.2%",
        actual_status="better",
        url="https://example.com",
        source="forexfactory",
    )
    feed_only = make_event("Dropped later", NOW + timedelta(hours=2))
    store.replace_window(*window, [page_row, feed_only])

    # The feed lists CPI again without Actual, and no longer lists the other.
    feed_cpi = make_event("CPI m/m", NOW + timedelta(hours=1), forecast="0.3%")
    store.replace_window(*window, [feed_cpi], only_source="faireconomy")

    (cpi,) = store.query(*window)
    assert (cpi.actual, cpi.actual_status, cpi.forecast) == ("0.2%", "better", "0.3%")
    assert cpi.source == "forexfactory"


def test_crawl_marks_expire_with_retention(store, client):
    store.mark_crawled("faireconomy", NOW)

    assert store.crawled_at("faireconomy") == NOW
    assert 0 < client.ttl("fxcal:crawl:faireconomy") <= 60 * 24 * 3600


def test_unchanged_rows_are_not_rewritten(store, clock):
    window = (NOW, NOW + timedelta(days=1))
    at = NOW + timedelta(hours=1)
    store.replace_window(*window, [make_event("CPI m/m", at, forecast="0.3%")])

    clock[0] = NOW + timedelta(minutes=30)
    later = clock[0]
    same = make_event("CPI m/m", at, forecast="0.3%").model_copy(update={"updated_at": later})
    store.replace_window(*window, [same], only_source="faireconomy")
    (cpi,) = store.query(*window)
    assert cpi.updated_at == NOW

    revised = same.model_copy(update={"forecast": "0.4%"})
    store.replace_window(*window, [revised], only_source="faireconomy")
    (cpi,) = store.query(*window)
    assert (cpi.forecast, cpi.updated_at) == ("0.4%", later)
