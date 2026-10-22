"""Redis storage of collected events.

Keys (all under the configured prefix, default `fxcal:`):

    event:<id>          JSON of one Event; expires `retention` after the event time
    events:by_time      sorted set of event ids, score = event time (epoch seconds)
    crawl:<key>         ISO time of the last crawl of a source; expires after `retention`
    usage:<reader>:<YYYY-MM>  quota a page reader spent that month (Firecrawl
                        credits); expires after ~2 months

Redis deletes each event on its own once it is `retention` (default 15 days)
past its release time. Index entries of expired events are pruned on every
write and skipped on read.
"""

import threading
from collections.abc import Callable, Iterable
from datetime import UTC, datetime, timedelta

import redis

from .models import Event


def _score(value: datetime) -> float:
    return value.timestamp()


def merge(old: Event | None, new: Event) -> Event:
    """A feed refresh never erases what a page crawl added (Actual, status,
    URL), and a page crawl's source wins."""
    if old is None:
        return new
    return new.model_copy(
        update={
            "actual": new.actual if new.actual is not None else old.actual,
            "forecast": new.forecast if new.forecast is not None else old.forecast,
            "previous": new.previous if new.previous is not None else old.previous,
            "actual_status": new.actual_status
            if new.actual_status is not None
            else old.actual_status,
            "url": new.url if new.url is not None else old.url,
            "source": "forexfactory" if old.source == "forexfactory" else new.source,
        }
    )


class EventStore:
    """Thread-safe for one process: writes are serialized by a lock."""

    def __init__(
        self,
        client: redis.Redis,
        *,
        retention: timedelta = timedelta(days=15),
        prefix: str = "fxcal:",
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ):
        self._redis = client
        self._retention = retention
        self._prefix = prefix
        self._now = now
        self._index = f"{prefix}events:by_time"
        self._lock = threading.Lock()

    def _event_key(self, event_id: str) -> str:
        return f"{self._prefix}event:{event_id}"

    def close(self) -> None:
        self._redis.close()

    def ping(self) -> None:
        self._redis.ping()

    def _cutoff(self) -> datetime:
        """Events released before this are past retention."""
        return self._now() - self._retention

    def _prune(self) -> None:
        self._redis.zremrangebyscore(self._index, "-inf", f"({_score(self._cutoff())}")

    def _load(self, ids: Iterable[str]) -> dict[str, Event]:
        ids = list(ids)
        if not ids:
            return {}
        raw = self._redis.mget([self._event_key(i) for i in ids])
        return {
            event_id: Event.model_validate_json(value)
            for event_id, value in zip(ids, raw)
            if value is not None
        }

    def _ids_between(self, start: datetime, end: datetime) -> list[str]:
        return self._redis.zrangebyscore(self._index, _score(start), f"({_score(end)}")

    def replace_window(
        self,
        start: datetime,
        end: datetime,
        events: Iterable[Event],
        *,
        only_source: str | None = None,
    ) -> int:
        """Upserts [events] and deletes events in [start, end) they no longer list.

        With [only_source], only that source's events without an Actual are
        deleted, so a partial source cannot erase richer data. Events already
        past retention are not stored.
        """
        new = {e.id: e for e in events}
        with self._lock:
            self._prune()
            in_window = self._ids_between(start, end)
            existing = self._load({*in_window, *new})
            stale = [
                event_id
                for event_id in in_window
                if event_id not in new
                and (
                    only_source is None
                    or event_id not in existing
                    or (
                        existing[event_id].source == only_source
                        and existing[event_id].actual is None
                    )
                )
            ]
            cutoff = self._cutoff()
            pipe = self._redis.pipeline(transaction=True)
            for event_id in stale:
                pipe.delete(self._event_key(event_id))
                pipe.zrem(self._index, event_id)
            stored = 0
            for event_id, event in new.items():
                if event.time_utc < cutoff:
                    continue
                merged = merge(existing.get(event_id), event)
                pipe.set(
                    self._event_key(event_id),
                    merged.model_dump_json(),
                    exat=int(_score(event.time_utc + self._retention)),
                )
                pipe.zadd(self._index, {event_id: _score(event.time_utc)})
                stored += 1
            pipe.execute()
        return stored

    def query(self, start: datetime, end: datetime) -> list[Event]:
        """Events with start <= time < end, ordered by time, currency, title."""
        ids = self._ids_between(start, end)
        loaded = self._load(ids)
        missing = [i for i in ids if i not in loaded]
        if missing:
            # Expired by Redis; drop their index entries.
            self._redis.zrem(self._index, *missing)
        return sorted(loaded.values(), key=lambda e: (e.time_utc, e.currency, e.title))

    def mark_crawled(self, key: str, at: datetime) -> None:
        self._redis.set(
            f"{self._prefix}crawl:{key}",
            at.astimezone(UTC).isoformat(),
            ex=int(self._retention.total_seconds()),
        )

    def crawled_at(self, key: str) -> datetime | None:
        value = self._redis.get(f"{self._prefix}crawl:{key}")
        return datetime.fromisoformat(value) if value else None

    def add_usage(self, reader: str, at: datetime, amount: int) -> None:
        key = f"{self._prefix}usage:{reader}:{at.astimezone(UTC):%Y-%m}"
        pipe = self._redis.pipeline()
        pipe.incrby(key, amount)
        pipe.expire(key, int(timedelta(days=62).total_seconds()))
        pipe.execute()

    def usage(self, reader: str, at: datetime) -> int:
        value = self._redis.get(f"{self._prefix}usage:{reader}:{at.astimezone(UTC):%Y-%m}")
        return int(value) if value else 0

    def count(self) -> int:
        with self._lock:
            self._prune()
            return self._redis.zcard(self._index)
