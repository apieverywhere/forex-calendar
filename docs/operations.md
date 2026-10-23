# Operations

## Configuration

Environment variables (see `.env.example`); `uvicorn --env-file .env` loads them.

| Variable | Default | Meaning |
|---|---|---|
| `REDIS_URL` | `redis://localhost:6379/0` | Redis holding events |
| `RETENTION_DAYS` | `15` | Events are deleted this many days after their release (the app shows at most last week) |
| `JOBS_TOKEN` | empty | Bearer token for `POST /api/v1/jobs/tick` and `/api/v1/refresh`; empty disables both |
| `REDIS_PREFIX` | `fxcal:` | Key prefix, to share a Redis with other apps |
| `PAGE_HISTORY_WEEKS` | `1` | Backfill reads day pages only back to the Monday this many weeks ago; older days come from storage only |
| `FEED_URL` | `https://nfs.faireconomy.media/ff_calendar_thisweek.json` | Weekly export |
| `REFRESH_MINUTES` | `30` | Background refresh interval; `0` disables the job. Effective minimum 15 |
| `PAGE_FETCHERS` | `firecrawl` | Day-page readers in fallback order (only `firecrawl` exists) |
| `FIRECRAWL_API_KEY` | empty | Firecrawl reader (1 credit per page) |
| `FF_PAGE_TIMEZONE` | `America/New_York` | Fallback zone for day pages; normally detected from the page's header clock |
| `DEFAULT_TIMEZONE` | `Asia/Ho_Chi_Minh` | Default `tz` of responses |

## Run locally

Start Redis first. The app refuses to start when Redis is unreachable. On the development Mac it
runs in Docker, with AOF persistence in a named volume and restarting with Docker Desktop:

```bash
docker run -d --name whalio-redis --restart unless-stopped -p 6379:6379 \
  -v whalio-redis-data:/data redis:7 redis-server --appendonly yes
docker exec whalio-redis redis-cli ping      # PONG
```

Docker Desktop must be running (`open -a Docker`). `brew install redis` also works but builds
from source on this Intel Mac and takes very long.

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
cp .env.example .env
.venv/bin/uvicorn app.main:app --env-file .env --host 0.0.0.0 --port 8000
```

`--host 0.0.0.0` lets the iOS Simulator and devices on the same network reach it. The Android
emulator reaches the host at `http://10.0.2.2:8000`.

Run with a single worker: the background refresh job lives in the process, and several workers
would each poll the export and race on merges.

## Tests

```bash
.venv/bin/python -m pytest
```

No network access and no real Redis; upstream and Redis (`fakeredis`) are faked in
`tests/conftest.py`.

## Deploying

Any host that runs a long-lived Python process with a persistent disk:

```bash
pip install .
uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 1
```

- Use a Redis with persistence (AOF or RDB snapshots), e.g. a managed Redis; otherwise past
  weeks are lost when Redis restarts. Upstash works over its Redis protocol, no extra library:
  `REDIS_URL=rediss://default:<token>@<name>.upstash.io:6379` (`rediss` = TLS; the password is
  the database token).
- Put it behind HTTPS (reverse proxy or the platform's TLS) before the Whalio app uses it on
  devices; iOS blocks plain HTTP by default.
- Keep `FIRECRAWL_API_KEY` in the platform's secret store, not in the image.
- **Always-on host** (VPS, Fly.io, Railway): keep `REFRESH_MINUTES` > 0; the service runs its jobs
  every 2 minutes. No cron needed.
- **Host that stops idle processes** (serverless, free tiers): set `REFRESH_MINUTES=0` and a
  `JOBS_TOKEN`, and let a scheduler call the tick every 2 minutes, e.g. cron-job.org, GitHub
  Actions or the platform's cron:

  ```bash
  curl -fsS -X POST -H "Authorization: Bearer $JOBS_TOKEN" https://<host>/api/v1/jobs/tick
  ```
- Reader use: about 1–2 pages per release time on weekdays (~330–400 pages / month), plus a
  one-time backfill of up to ~2 weeks after the first deploy. That fits Firecrawl's free 1,000
  credits / month. Watch `usage_this_month` in
  `GET /api/v1/status`.

## Monitoring

`GET /api/v1/status`: `feed_refreshed_at` older than about two intervals, or a `last_error`,
means upstream trouble. Logs print `feed refreshed: N events` on each refresh.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `feed did not return JSON (rate limited?)` | Export blocked this IP for polling too often | Raise `REFRESH_MINUTES`; avoid calling `/refresh` repeatedly; wait it out |
| `firecrawl HTTP 401` / `402` | Invalid key / out of credits | Set a valid `FIRECRAWL_API_KEY`; wait for the monthly reset |
| `page has no calendar table` | Blocked page or Forex Factory changed its markup | Fetch the page through Firecrawl by hand and compare with `docs/data-sources.md` |
| Day-page times off by hours | Header clock missing or a stale cached page | Check that Firecrawl requests send `maxAge: 0`; compare one event with the export |
| Feed HTTP 429 after restarts | Export downloaded too often | Wait; restarts no longer re-download while the stored refresh is fresh, and after a failure the service waits `REFRESH_MINUTES` before retrying |
| Feed HTTP 429 on a cloud host (`feed_refreshed_at` old or null) | The host's shared outgoing IP is blocked by the export, by other tenants' traffic too (seen on Render Singapore, 2026-10-23) | With `FIRECRAWL_API_KEY` the service reads this week from the `?week=this` page after 2 h, every 6 h at most, until the export recovers. Otherwise move to a host with its own IP |
| Firecrawl credits used up fast | Backfill after a deploy or downtime | Each page costs 1 credit; past days are re-read at most every 24 h |
| Empty response for a past week | Service was not running that week and no Firecrawl key | Add a key, or accept the gap |
| Startup fails with `ConnectionError` | Redis not running or wrong `REDIS_URL` | Start Redis; check `redis-cli -u $REDIS_URL ping` |
| `Failed host lookup` for an upstream | Local DNS (the Whalio machine's router returned only IPv6 for some hosts) | Check `dscacheutil -q host -a name <host>`; change DNS |

## Data

Inspect with `redis-cli`:

```bash
redis-cli ZCARD fxcal:events:by_time                       # events within retention
redis-cli ZRANGE fxcal:events:by_time -5 -1                # latest ids
redis-cli GET fxcal:event:<id>                             # one event (JSON)
redis-cli TTL fxcal:event:<id>                             # seconds until deletion
```

`redis-cli --scan --pattern 'fxcal:*' | xargs redis-cli DEL` resets everything; the next
refresh refills the current week.
