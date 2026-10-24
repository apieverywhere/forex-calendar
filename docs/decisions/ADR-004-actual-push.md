# ADR-004 — "Actual released" pushes through FCM topics

## Status
Accepted (2026-10-23). Whalio spec `specs/010-actual-alerts/spec.md`, Whalio ADR-002 point 2.

## Context
Whalio users want a notification when an event they follow gets its Actual, with the app closed.
The device cannot reliably poll at release time (Android background limits), but this service
already re-reads day pages right after releases.

## Decision
- After the page jobs, `tick` sends one FCM notification message per language to the topic
  `actual_<event id>_<lang>` for every release of the last 3 hours that has an Actual. A
  `crawl:push:<id>:<lang>` mark (expiring with the retention) makes each send happen once.
- The app subscribes to those topics itself; the service keeps no device tokens or user data.
- FCM HTTP v1 is called with the shared httpx client; the OAuth token is a service-account JWT
  signed with PyJWT (`pyjwt[crypto]`), so no Firebase or Google SDK is added.
- Text is built here in the app's 7 languages, from stored values only (missing Forecast /
  Previous / status are left out).

## Consequences
- 7 messages per release with an Actual (~200 a day), free on FCM; no extra reader credits.
- Notifications are only as fast as the jobs: on hosts that stop idle processes a cron must call
  the tick every 2 minutes.
- Adding an app language means adding it to `push.LANGUAGES` and `_TEXT`.
- A failed send is retried on the next tick while the release is within 3 hours.
