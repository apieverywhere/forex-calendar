# ADR-003 — Redis storage with 60-day retention

## Status
Accepted (2026-10-19). Supersedes the storage part of ADR-002 (SQLite); ADR-002's id and merge
rules still hold.

## Context
Events only need to be kept for a limited history. With SQLite, old rows stayed forever and
cleanup would need a job. Redis can expire each record on its own, and is a better fit if the
service later runs as several instances.

## Decision
- Store events in Redis (`REDIS_URL`), under the prefix `fxcal:`:
  - `event:<id>`: the event as JSON, `EXPIREAT` = release time + `RETENTION_DAYS` (default 60).
  - `events:by_time`: sorted set of ids scored by release time, for range queries.
  - `crawl:<key>`: last crawl time per source, expires after the same retention.
- Events already older than the retention are not written.
- Index entries of expired events are removed on every write (`ZREMRANGEBYSCORE`) and when a
  read finds the event key gone.
- Merging keeps ADR-002's rules (a feed refresh never erases Actual / status / URL from a page).
- The app fails at startup if Redis is unreachable.

## Consequences
- History is a rolling window after release time (60 days at first; 15 days since 2026-10-19,
  because the app shows at most last week); nothing to clean up by hand.
- Redis must run with persistence (RDB or AOF) in production, or a restart loses history.
- Writes read existing events first (merge), then write in one transaction (`MULTI`). Writes are
  serialized inside one process; several writer processes could race on the same event.
- `GET /api/v1/status` `events_stored` counts events within retention.
