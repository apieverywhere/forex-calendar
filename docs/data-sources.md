# Data Sources

Checked 2026-10-19 from this machine.

| Source | URL | Result |
|---|---|---|
| forexfactory.com direct | `https://www.forexfactory.com/calendar?day=oct16.2026` | 403, Cloudflare "Just a moment…" page. Not used (ADR-001). |
| Weekly export, JSON | `https://nfs.faireconomy.media/ff_calendar_thisweek.json` | 200, ~86 events |
| Weekly export, XML | `https://nfs.faireconomy.media/ff_calendar_thisweek.xml` | 200, adds event URL, local times only. Not used. |
| Other export weeks | `ff_calendar_nextweek.json`, `ff_calendar_lastweek.json`, `ff_calendar_today.json` | 404 |
| Jina Reader (removed 2026-10-19) | `https://r.jina.ai/https://www.forexfactory.com/calendar?day=…` | 401 anonymous; 200 with a key (~9k tokens per page). No longer configured |
| forexfactory.com next week via Firecrawl (2026-10-22) | `https://www.forexfactory.com/calendar?week=next` | 200; 96 events Sun Oct 25 – Sat Oct 31 with Forecast / Previous; 1 credit |
| Firecrawl `/v1/scrape` | `rawHtml`, `maxAge: 0`, wait 3 s | 200; **1 credit per page** (free: 1,000 / month); rows are blank without the wait, an older copy without `maxAge: 0` |

## 1. Weekly export (`app/sources/faireconomy.py`)

Current week, Sunday to Saturday US Eastern. Responses are not cacheable (`no-store`). Clients
that download it too often get an HTML "Request Denied" page instead of JSON; the service treats
that as a `SourceError` and keeps serving stored data.

Item:

```json
{"title": "CPI m/m", "country": "CAD", "date": "2026-10-19T08:30:00-04:00",
 "impact": "High", "forecast": "0.2%", "previous": "-0.1%"}
```

| Field | Mapping |
|---|---|
| `title` | `title` |
| `country` | `currency`, upper-cased; `All` becomes `ALL` |
| `date` | `time_utc` (ISO with offset, converted to UTC) |
| `impact` | `High`, `Medium`, `Low`, `Holiday` |
| `forecast`, `previous` | as text; empty string becomes `null` |
| — | `actual`, `actual_status`, `url` are always `null` |

Notes:
- No all-day marker; holidays carry a clock time (often 00:00 or 21:00 ET).
- Items are sometimes repeated verbatim; duplicates collapse to one id.

## 2. Day pages via a reader service (`app/sources/page_fetchers.py`, parsed in `forexfactory_page.py`)

Readers are tried in `PAGE_FETCHERS` order (default `firecrawl`, the only reader); one without a key is
skipped, and any failure (e.g. out of credits) falls back to the next. A page with blank
`calendar__cell--blank` cells was returned before its script ran and counts as a failure.

Firecrawl request:

```
POST https://api.firecrawl.dev/v1/scrape
Authorization: Bearer <FIRECRAWL_API_KEY>
{"url": "https://www.forexfactory.com/calendar?day=oct14.2026", "formats": ["rawHtml"],
 "onlyMainContent": false, "maxAge": 0, "actions": [{"type": "wait", "milliseconds": 3000}]}
```

Firecrawl renders pages in UTC−5.

The day parameter is `<mon><day>.<year>` with a lower-case English month and no zero padding.
A response without `calendar__row` is treated as blocked or changed layout (`SourceError`).

Parsed markup (from github.com/xclunderrated/ForexFactory-Unofficial-API):

| Selector | Field |
|---|---|
| `tr.calendar__row` | one event; rows without currency or title are skipped |
| `tr[data-event-id]` | used in `url` as `#detail=<id>` |
| `.calendar__time` | `8:30am`, `All Day`, `Tentative`; **blank means same time as the row above** |
| `.calendar__currency` | `currency` |
| `.calendar__impact` and descendants, `title` and `class` | impact: `high`/`red` → High, `medium`/`ora` → Medium, `holiday`/`non`/`gra` → Holiday, else Low |
| `.calendar__event-title`, fallback `.calendar__event` | `title` |
| `.calendar__actual` text; `better` / `worse` in its or a child's class | `actual`, `actual_status` |
| `.calendar__forecast`, `.calendar__previous` | `forecast`, `previous` |

Times are in the zone Forex Factory picks for the visitor, which follows the reader's servers, not US
Eastern: on 2026-10-19 Firecrawl pages came back in UTC−5 and Jina pages (no longer used) in
**UTC−2:30** (header "5:21pm" at 19:51 UTC; CAD CPI at "10:00" = 12:30 UTC = 08:30 New York). The parser therefore reads the header clock
(`.calendar__header-time`), subtracts the UTC fetch time and rounds to 15 minutes;
`FF_PAGE_TIMEZONE` is only the fallback when the clock is missing. `All Day` and `Tentative` rows are stored at local midnight with
`all_day = true` and the label in `time_note`.

Right after a rate decision Forex Factory puts `PENDING` in the Actual cell until the number is
posted (seen 2026-10-28, NZD Official Cash Rate). It is stored as `actual = null`, so the release
keeps being re-read and no "Actual released" push goes out for it.

Each page takes about 2–9 s; at most 3 run at once. A request for a week reads 9 pages (the week plus
one day on each side, because the page zone is only known after a fetch).

## 3. Week pages (`?week=next`, `?week=this`, parsed by `parse_week`)

`?week=this` is read only as a fallback while the export fails (checked 2026-10-23: 86 events,
the same as the export, plus 56 Actuals).

Same reader, same row markup and time-zone rule as day pages. The page lists several days; each
starts with a day-breaker row whose text has no year:

```html
<tr class="calendar__row calendar__row--day-breaker"><td colspan="10">Sun <span>Oct 25</span></td></tr>
```

Rows take their day from the breaker above them; the year is the one that puts the date nearest
the fetch (a week can span New Year). A blank time cell repeats the time above within the same
day. Event links use the day-page URL (`?day=oct26.2026#detail=<id>`). Rows are stored with
`source = forexfactory_week`. Checked 2026-10-22: 96 events, page in UTC−5, Fed and ECB times
matched their official schedules.

## Credit budget (Firecrawl, 1 credit per page)

Estimated 2026-10-23 from a week with 43 release times (~186 a month); every past release that
week got its Actual within minutes.

| Job | Credits / month |
|---|---|
| Releases awaiting Actual (1–2 reads per release time) | 190–370 |
| Finalize yesterday | 0–30 |
| Backfill (first deploy, downtime) | 0–10 |
| Next week (1 a day) | ~31 |
| This week fallback (only while the export fails, ≤ 4 a day) | 0–120 |
| **Total** | **~220–560** of a 1,000 free tier |

A release that never gets an Actual costs about 10 reads (every tick for 10 min, then every
10 min until 60). Spend so far: `usage_this_month` in `GET /api/v1/status`.

