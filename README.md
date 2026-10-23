# Forex Calendar API

FastAPI service that collects the [Forex Factory](https://www.forexfactory.com/) economic
calendar, stores it in Redis (each event is deleted 15 days after its release) and serves it
as JSON or CSV. Endpoints follow
[ForexFactory-Unofficial-API](https://github.com/xclunderrated/ForexFactory-Unofficial-API).

Full documentation is in [`docs/`](docs/):
[overview](docs/overview.md) · [API reference](docs/api.md) ·
[data sources](docs/data-sources.md) · [architecture](docs/architecture.md) ·
[operations](docs/operations.md) · [Whalio integration](docs/integration-whalio.md) ·
[decisions](docs/decisions/).

## Sources

| Source | What it gives | Needs |
|---|---|---|
| Weekly export `nfs.faireconomy.media/ff_calendar_thisweek.json` (published by Forex Factory) | Current week: time, currency, impact, forecast, previous. **No Actual.** | nothing |
| Day pages `forexfactory.com/calendar?day=oct14.2026`, read through [Firecrawl](https://firecrawl.dev) | Any date, **Actual** and better / worse status | `FIRECRAWL_API_KEY` |
| Week pages `forexfactory.com/calendar?week=next` / `?week=this`, read through Firecrawl | Next week before the export rolls over; this week while the export is rate-limited | `FIRECRAWL_API_KEY` |

forexfactory.com blocks direct requests with a Cloudflare bot check; this service does not try to
bypass it. Without a Firecrawl key only the weekly export is used. Every refresh is stored, so past
weeks stay available (for 15 days) after the export moves on. Forex Factory's terms restrict automated use of
its site; check them before running this commercially.

## Run

Needs a running Redis (`brew install redis && redis-server`, or Docker `redis:7`).

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
cp .env.example .env          # set FIRECRAWL_API_KEY here to enable day pages
.venv/bin/uvicorn app.main:app --env-file .env --host 0.0.0.0 --port 8000
```

Interactive docs: http://localhost:8000/docs

Tests: `.venv/bin/python -m pytest`

## Endpoints

| Method | Path | Description |
|---|---|---|
| GET | `/health` | Liveness |
| GET | `/api/v1/status` | Stored events, last refresh, last error |
| GET | `/api/v1/calendar` | Events with filters (below) |
| GET | `/api/v1/calendar/{day}` | One day: `YYYY-MM-DD`, `today`, `tomorrow`, `yesterday`; add `.csv` for CSV |
| GET | `/api/v1/week?offset_weeks=0` | Monday–Sunday in `tz` |
| GET | `/api/v1/currencies` | Currencies seen within ±7 days |
| POST | `/api/v1/jobs/tick` | Run due jobs (for an external cron); `Authorization: Bearer $JOBS_TOKEN` |
| POST | `/api/v1/refresh?day=YYYY-MM-DD` | Refresh the export now (and crawl that day page with a key); same token |

`/api/v1/calendar` query parameters:

| Parameter | Default | Example |
|---|---|---|
| `date` | — | `2026-10-14`, `today` (overrides start/end) |
| `start`, `end` | today | `start=2026-10-12&end=2026-10-18` (max 92 days) |
| `tz` | `DEFAULT_TIMEZONE` (Asia/Ho_Chi_Minh) | `America/New_York` |
| `currency` | all | `USD,EUR` |
| `impact` | all | `High,Medium` (also `Low`, `Holiday`) |
| `search` | — | `CPI` |
| `has_actual` | false | `true` |
| `limit`, `offset` | — / 0 | `limit=20` |
| `format` | json | `csv` |

Days and the `date` / `time` fields are in `tz`; `time_utc` is always UTC.

```json
{
  "start": "2026-10-19", "end": "2026-10-19", "timezone": "Asia/Ho_Chi_Minh",
  "total": 4, "count": 4,
  "events": [{
    "id": "46b55963e950a896", "title": "CPI m/m", "currency": "CAD", "impact": "High",
    "time_utc": "2026-10-19T12:30:00Z", "date": "2026-10-19", "time": "19:30",
    "all_day": false, "time_note": null,
    "actual": null, "forecast": "0.2%", "previous": "-0.1%", "actual_status": null,
    "url": null, "source": "faireconomy", "updated_at": "2026-10-18T18:11:02Z"
  }]
}
```

## How it refreshes

- The weekly export is refreshed every `REFRESH_MINUTES` (default 30) in the background (on a
  request only when `REFRESH_MINUTES=0`). After a failure (e.g. HTTP 429) it waits the same
  interval before retrying. The export blocks clients that poll it too often.
- Requests never read pages. With a Firecrawl key (1 credit per page), background jobs read:
  - day pages right after releases until Actual appears (every 2 min for 10 min, then every
    10 min, up to 60 min), once a day for releases that missed that window, and once for
    never-read days of the viewable history (last week by default);
  - the next-week page once a day;
  - the this-week page only while the export fails: after 2 h of failure, at most every 6 h.

  Expected spend: about 220–560 credits a month (`docs/data-sources.md`, Credit budget). See
  `docs/architecture.md`.
- Rows from both sources share an id (currency + title + UTC time), so a page crawl adds Actual
  to the export's row, and a later export refresh never removes it.

## Layout

```text
app/
├── main.py                     FastAPI app and routes
├── config.py                   settings from environment
├── models.py                   Event models, id and impact normalization
├── service.py                  refresh policy, crawling, filtering
├── store.py                    Redis storage
└── sources/
    ├── faireconomy.py          weekly export
    ├── page_fetchers.py        readers for day pages (Firecrawl) with fallback
    └── forexfactory_page.py    day- and week-page parsing
tests/
```
