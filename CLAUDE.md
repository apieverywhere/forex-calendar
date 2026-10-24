# CLAUDE.md

## Project

**forex-calendar** is a small FastAPI service that collects the Forex Factory economic calendar,
stores it in Redis (each event expires 15 days after its release) and serves it as JSON or CSV. Its main client is the Whalio Flutter app
(`/Users/macos/Documents/whalio`), whose Economic Calendar tab needs events in GMT+7 with
Actual / Forecast / Previous.

Single developer, AI-assisted. Optimize for small, readable modules and few dependencies.

## Documentation

Read the relevant file before changing behavior:

- `docs/overview.md` — purpose, scope, non-goals
- `docs/data-sources.md` — the two upstream sources, their fields and limits
- `docs/architecture.md` — modules, data flow, merge and freshness rules
- `docs/api.md` — every endpoint, parameter and response field
- `docs/operations.md` — configuration, running, deployment, troubleshooting
- `docs/integration-whalio.md` — how the Whalio app should consume the API
- `docs/decisions/ADR-*.md` — decisions and their reasons

Keep `docs/` in sync when a change affects endpoints, fields, sources or refresh rules.

## Hard rules

- Never bypass forexfactory.com's Cloudflare bot check (no cloudscraper, undetected browsers,
  cookie replay or similar). See ADR-001.
- Never fabricate values. A field the source did not provide is `null`, not a guess.
- The weekly export blocks clients that poll it too often: keep automatic refreshes at
  15 minutes or more.
- Event ids are `sha1(currency|title|time_utc)[:16]` in both sources; changing that formula
  breaks merging and existing stored rows (ADR-002).
- Queries never read pages (readers cost credits); only the jobs in `CalendarService.tick` do.
  Keep the monthly spend within the Firecrawl budget in `docs/data-sources.md` (1,000 credits).
- Every event key must carry an expiry (release time + `RETENTION_DAYS`); never write an event
  without one (ADR-003).

## Stack

Python 3.11+, FastAPI, httpx (async, one shared client), BeautifulSoup, redis-py.
Do not add an ORM, task queue or second HTTP client without a concrete need.

## Layout

```text
app/
├── main.py                     routes, parameter parsing, CSV output
├── config.py                   Settings from environment
├── models.py                   Event / EventOut / CalendarResponse, id and impact helpers
├── service.py                  refresh policy, crawling, filtering
├── store.py                    Redis storage (keys, expiry, index)
├── push.py                     FCM "Actual released" notifications (ADR-004)
└── sources/
    ├── __init__.py             SourceError
    ├── faireconomy.py          weekly export
    ├── page_fetchers.py        readers for day pages (Firecrawl) with fallback
    └── forexfactory_page.py    day- and week-page parsing
tests/                          pytest; upstream faked with httpx.MockTransport
```

Upstream HTTP is only in `sources/`. Routes call `CalendarService`, never sources or the store.

## Validation

```bash
.venv/bin/python -m pytest
```

Tests must not reach the network or a real Redis: fake upstream in `tests/conftest.py`
(`FakeUpstream`) and `fakeredis` (`redis_server` fixture).
After changing the page parser, also check it against a real page
(`POST /api/v1/refresh?day=YYYY-MM-DD` with `FIRECRAWL_API_KEY` set).
