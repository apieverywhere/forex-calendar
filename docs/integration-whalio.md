# Using the API from Whalio

Status: in use since 2026-10-19 (Whalio ADR-004), against a local instance until the service is
deployed. Whalio code: `code/app/lib/features/calendar/data/forex_calendar_*.dart`.

## Why switch

| | financecalendar.com (now) | forex-calendar |
|---|---|---|
| Events per week | ~10–15 | ~80–100 |
| Currency | derived from the name | provided |
| Actual | yes | only with `FIRECRAWL_API_KEY` |
| Better / worse coloring | not possible (free text) | `actual_status`, with a Firecrawl key |
| Hosting | none | this service must be deployed |

## Request

The calendar screen loads one week (Monday–Sunday, GMT+7):

```
GET {BASE}/api/v1/calendar?start=2026-10-19&end=2026-10-25&tz=Asia/Ho_Chi_Minh
```

Day selection, impact and currency filters stay client-side, as now (no refetch).

## Field mapping to `EconomicEvent`

| Whalio field | API field |
|---|---|
| `id` | `id` |
| `name` | `title` |
| `day` | `date` |
| `time` | `time_utc`; null when `all_day` |
| `impact` | `impact` (`Holiday` → low, or a new level) |
| `currency` | `currency` (`ALL` needs an icon or a filter rule) |
| `forecast`, `previous`, `actual` | same names |
| `url` | `url` |
| — | `actual_status` enables the design's green / red Actual |

## Changes in the app

- `core/network/api_client.dart`: a Dio for `CALENDAR_BASE_URL` pointing at this service.
- `features/calendar/data/`: a new data source and repository; `currencyForEvent` and the
  market-hours filter are no longer needed.
- Attribution: show Forex Factory as the source instead of financecalendar.com.
- `Holiday` events: decide whether they appear (Forex Factory shows them, gray).

## Environments

| Where the app runs | Base URL |
|---|---|
| iOS Simulator | `http://localhost:8000` (needs an ATS exception for HTTP, or HTTPS) |
| Android emulator | `http://10.0.2.2:8000` (needs cleartext allowed for HTTP) |
| Devices | the deployed HTTPS URL |
