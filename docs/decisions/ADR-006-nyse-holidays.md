# ADR-006 — NYSE holidays from financecalendar.com, cached for Whalio

## Status
Accepted (2026-11-02). Whalio ADR-006 (holiday provider), spec `specs/007-trading-sessions/spec.md`.

## Context
Whalio's Trading Sessions tab lists NYSE holidays and early closes. The app called
financecalendar.com's `/holidays?exchange=nyse` directly, without a key. On 2026-11-01
financecalendar.com started to require a verified-account API key on every request (`X-API-Key`
header; anonymous calls answer 401 `api_key_required`). The Free tier allows 50 requests a day
and 500 a month per account (one page says 1,000 a day; the changelog and reference say 50 / 500).
A key shipped in the app could be read from the APK, and every install would share one
account's quota.

## Decision
- The service holds the key (`FINANCE_CALENDAR_API_KEY`) and serves `GET /api/v1/holidays`.
- `HolidayService` (`app/holidays.py`) calls financecalendar.com on a request when the saved list
  is a day old or missing, at most once an hour after a failure, and saves the last good list in
  Redis (`holidays:nyse`, no expiry). A failing provider never removes the saved list.
- Only `date`, `name`, `status` and `close_time_et` are passed on.
- Whalio caches the answer on the device and asks again after 30 days (Whalio spec 007).

## Consequences
- At most about 30 provider calls a month in normal use (plus at most 24 a day during an outage),
  inside the Free tier.
- Attribution: financecalendar.com allows caching and redistribution with a visible link; the
  response carries `source` and Whalio keeps the link on its Sessions tab.
- Without the key the endpoint answers 503 and the app shows its holiday error.
