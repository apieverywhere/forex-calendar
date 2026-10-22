# ADR-002 — SQLite storage with content-derived ids

## Status
Accepted (2026-10-19). Storage superseded by ADR-003 (Redis); ids and merge rules still apply.

## Context
The export only covers the current week, so history exists only if the service keeps it. The
export has no event ids; day pages have `data-event-id`, which the export cannot match.

## Decision
- Store events in SQLite (stdlib `sqlite3`, one file, one process).
- `id = sha1("CURRENCY|title lower-cased|time_utc ISO")[:16]` for both sources.
- Upserts never replace a known value with null; a page crawl's source wins.

## Consequences
- Export rows and page rows of the same event merge, so Actual from a page survives export
  refreshes.
- A rescheduled event gets a new id; the old row is removed when its week (export) or day (page)
  is refreshed.
- Two events with the same currency, title and minute collapse into one (acceptable; observed
  only for verbatim duplicates in the export).
- Single-process only. Moving to several workers or hosts means moving storage to a server
  database and the refresh job to one place.
