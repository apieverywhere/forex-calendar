# Architecture

```text
client ──HTTP──> main.py (routes) ──> CalendarService ──> EventStore (Redis)
                                           │
                                           ├──> sources/faireconomy.py ──> weekly export
                                           └──> sources/forexfactory_page.py ──> Firecrawl ──> forexfactory.com
background task (lifespan) ──> CalendarService.refresh_feed() every REFRESH_MINUTES
```

## Modules

| Module | Owns |
|---|---|
| `main.py` | App factory `create_app(settings, transport)`, lifespan (store, httpx client, background job), routes, parameter validation, CSV |
| `config.py` | `Settings` (frozen dataclass), read from environment |
| `models.py` | `Event` (stored), `EventOut` (+ local `date`, `time`), `CalendarResponse`; `event_id`, `normalize_impact`, `clean` |
| `service.py` | `CalendarService`: refresh, crawl, freshness, filtering, status; `day_bounds`, `to_output` |
| `store.py` | `EventStore`: Redis keys, expiry, time index, merge, range query, crawl log; writes guarded by a lock |
| `sources/` | Upstream HTTP and parsing only; raise `SourceError` on any upstream problem |

## Request flow (`GET /api/v1/calendar`)

1. Resolve `tz`, `date` / `start` / `end` (keywords are relative to today in `tz`).
2. `day_bounds` turns the local day range into a UTC window `[start, end)`.
3. Refresh the export if the last refresh is older than `max(REFRESH_MINUTES, 15)` minutes (it is
   free); failures are logged and ignored. **Queries never read day pages**, so they never spend
   reader credits; pages are read only by the jobs below.
4. Query the store for the window, filter in memory, convert times to `tz`, page with
   `limit` / `offset`.

## Storage (ADR-003)

Redis, prefix `fxcal:`:

| Key | Type | Content | Expires |
|---|---|---|---|
| `event:<id>` | string | Event JSON | release time + `RETENTION_DAYS` (60) |
| `events:by_time` | sorted set | event ids, score = release time (epoch s) | entries pruned on every write and when their event is gone |
| `crawl:<key>` | string | last crawl, `faireconomy` or `page:YYYY-MM-DD` | after `RETENTION_DAYS` |

Range queries read ids from the sorted set, then `MGET` the events. Events already past retention
are never written, and rows a refresh leaves unchanged are not rewritten (hosted Redis such as
Upstash bills per command).

### Ids and merging (ADR-002)

`id = sha1("CURRENCY|title lower-cased|time_utc ISO")[:16]`, identical in both sources, so a page
crawl and an export refresh update the same row.

Merge rules (applied in Python before the write; ADR-002):
- `actual`, `actual_status`, `url`, `forecast`, `previous`: new value if not null, else keep.
- `source`: once `forexfactory`, stays `forexfactory`.

### Removing stale rows

- Export refresh: within the export's time span, delete rows from the export that are no longer
  listed **and have no Actual**. Handles rescheduled or removed events without erasing page data.
- Page crawl: the page is authoritative for its day; delete every row in that day's window that
  the page no longer lists.

A rescheduled event gets a new id (its time changed); the old row is removed by the rules above
once its week or day is refreshed.

## Jobs (`CalendarService.tick`)

`tick()` runs every 2 minutes, in process (`run_jobs_loop`, when `REFRESH_MINUTES` > 0) or from an
external cron through `POST /api/v1/jobs/tick`. Each step checks Redis first, so extra calls cost
nothing.

| Step | Reads | Day pages |
|---|---|---|
| Export refresh | the weekly export, when older than `max(REFRESH_MINUTES, 15)` min | 0 |
| Releases awaiting Actual | day page of every data release (has Forecast or Previous) within 60 min after its time and still without Actual | ~1–2 per release time |
| Finalize yesterday | once a day (`final:<date>` mark): yesterday's page, only if a release there still has no Actual | usually 0 |
| Backfill | once per day ever (`page:<date>` mark): each day from Monday `PAGE_HISTORY_WEEKS` weeks ago to yesterday never read | only after a first deploy or downtime |
| Next week | once a day in the page zone (`next-week:<date>` mark, set before the read so a failure waits a day): the `?week=next` page | 1 page (1 credit) a day |

Each reader's spend (Firecrawl credits) is added up per month
(`usage:<reader>:<YYYY-MM>`) and shown by `GET /api/v1/status` (`usage_this_month`). Next week comes from the `?week=next` page (one credit a day, about 30 a month).
Its rows have `source = forexfactory_week`; they are a schedule, so when the export rolls over to
that week its refresh may update or delete them like its own rows, and day pages override them.

The export refresh time is kept in Redis, so restarting the service does not download the export
again while it is fresh (repeat downloads get HTTP 429). A failed download is recorded too: jobs
and queries do not try again until `REFRESH_MINUTES` (at least 15) have passed, because retrying a
rate-limited export keeps it blocked. `POST /api/v1/refresh` still forces a download.
Queries download the export only when `REFRESH_MINUTES=0` (no in-process jobs); otherwise
the jobs keep it fresh and a request never waits on the export.

## Concurrency

One `httpx.AsyncClient` per app. Export refreshes are serialized by a lock; page crawls are
limited to 3 at a time. Store writes (read, merge, `MULTI` write) are serialized by a
`threading.Lock`, so run one writer process.

## Errors

Upstream failures never fail a read request: the client gets what is stored. The latest failure
is kept in `last_error` and shown by `GET /api/v1/status`. `POST /api/v1/refresh` reports
failures in its body (`feed_error`, `page_error`) with HTTP 200.
