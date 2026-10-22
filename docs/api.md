# API Reference

Base URL (local): `http://localhost:8000`. OpenAPI UI: `/docs`; schema: `/openapi.json`.
CORS allows any origin for `GET` and `POST`. No authentication.

## Conventions

- Days are calendar days in the request's `tz` (IANA name; default `DEFAULT_TIMEZONE`,
  `Asia/Ho_Chi_Minh`). Day keywords: `today`, `tomorrow`, `yesterday`.
- `time_utc` is always UTC; `date` and `time` are in `tz`.
- Errors: HTTP 400 with `{"detail": "<message>"}` for bad input; 422 for parameters FastAPI
  rejects by type (e.g. `limit=0`).

## Event object

| Field | Type | Notes |
|---|---|---|
| `id` | string | 16 hex chars, stable across sources |
| `title` | string | Forex Factory's event name, e.g. `CPI m/m` |
| `currency` | string | `USD`, `EUR`, … or `ALL` |
| `impact` | string | `High`, `Medium`, `Low`, `Holiday` |
| `time_utc` | datetime | UTC; local midnight for all-day events |
| `date` | date | in `tz` |
| `time` | string | `HH:MM` in `tz`, or `time_note` / `All Day` when `all_day` |
| `all_day` | bool | true for `All Day` / `Tentative` rows (day pages only) |
| `time_note` | string \| null | `All Day`, `Tentative` |
| `actual` | string \| null | null until released; day pages only |
| `forecast`, `previous` | string \| null | free text as published (`0.3%`, `-775M`) |
| `actual_status` | `better` \| `worse` \| null | Forex Factory's own coloring; day pages only |
| `url` | string \| null | day page with `#detail=<id>`; day pages only |
| `source` | `faireconomy` \| `forexfactory` \| `forexfactory_week` | `forexfactory` once a day page has updated the row; `forexfactory_week` for next week read from the week page, until the export or a day page updates it |
| `updated_at` | datetime | last time a source changed the row |

## GET /api/v1/calendar

| Parameter | Default | Description |
|---|---|---|
| `date` | — | Single day; overrides `start` / `end` |
| `start` | `today` | First day |
| `end` | `start` | Last day, inclusive; range at most 92 days |
| `tz` | `DEFAULT_TIMEZONE` | IANA zone |
| `currency` | all | Comma-separated, case-insensitive |
| `impact` | all | Comma-separated: `High,Medium,Low,Holiday` |
| `search` | — | Substring of the title, or an exact currency code |
| `has_actual` | `false` | Only events with an Actual |
| `limit` | all | 1–1000 |
| `offset` | 0 | |
| `format` | `json` | `json` or `csv` |

Response (`CalendarResponse`):

```json
{
  "start": "2026-10-19", "end": "2026-10-19", "timezone": "Asia/Ho_Chi_Minh",
  "total": 4, "count": 4,
  "events": [{
    "id": "46b55963e950a896", "title": "CPI m/m", "currency": "CAD", "impact": "High",
    "time_utc": "2026-10-19T12:30:00Z", "date": "2026-10-19", "time": "19:30",
    "all_day": false, "time_note": null, "actual": null, "forecast": "0.2%",
    "previous": "-0.1%", "actual_status": null, "url": null, "source": "faireconomy",
    "updated_at": "2026-10-18T18:11:02Z"
  }]
}
```

`total` counts all matches; `count` the ones in this page. Events are ordered by time, then
currency, then title.

CSV columns: `date,time,time_utc,currency,impact,title,actual,forecast,previous,actual_status,url`.

```bash
curl "localhost:8000/api/v1/calendar?start=today&end=2026-10-25&impact=High,Medium&currency=USD,EUR"
curl "localhost:8000/api/v1/calendar?date=2026-10-14&tz=America/New_York&format=csv" -o day.csv
```

## GET /api/v1/calendar/{day}

`day`: `YYYY-MM-DD` or a keyword; suffix `.csv` for CSV. Accepts `tz`, `currency`, `impact`,
`format`.

```bash
curl localhost:8000/api/v1/calendar/today
curl localhost:8000/api/v1/calendar/2026-10-14.csv
```

## GET /api/v1/week

Monday to Sunday containing today in `tz`. `offset_weeks` (−52…52) moves by whole weeks.
Accepts `tz`, `currency`, `impact`.

## GET /api/v1/currencies

`{"currencies": ["AUD", "CAD", ...]}` — currencies with events within ±7 days of today.

## GET /api/v1/status

```json
{"events_stored": 402, "feed_refreshed_at": "2026-10-18T19:56:04+00:00",
 "refresh_minutes": 30, "retention_days": 15, "page_fetchers": ["firecrawl"],
 "usage_this_month": {"firecrawl": 1}, "last_error": null}
```

## POST /api/v1/jobs/tick

Runs every job that is due (export refresh, Actual after releases, finalize yesterday, backfill;
see `docs/architecture.md`). Meant for an external cron every 2 minutes when the service does not
run its own scheduler. Needs `Authorization: Bearer <JOBS_TOKEN>`; 401 with a wrong token, 404 when
`JOBS_TOKEN` is not set.

```json
{"released_pages": 1, "finalized_pages": 0, "backfilled_pages": 0}
```

## POST /api/v1/refresh

Same token as `jobs/tick`. Refreshes the export now. With `day=YYYY-MM-DD` (page time zone) and a Firecrawl key, also crawls that
day page. Always HTTP 200:

```json
{"feed_events": 86, "page_events": 14}
{"feed_error": "feed returned HTTP 429", "page_error": "no page reader configured (FIRECRAWL_API_KEY)"}
```

Do not call it in a loop: the export blocks frequent downloads.

## GET /health

`{"status": "ok", "time": "..."}` — does not touch upstream or the database.
