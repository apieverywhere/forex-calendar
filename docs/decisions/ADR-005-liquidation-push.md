# ADR-005 — "Large liquidation" pushes from OKX's WebSocket

## Status
Accepted (2026-10-26). Whalio spec `specs/011-liquidation-alerts/spec.md`, Whalio ADR-006.

## Context
Whalio users want a notification when a large wave of liquidations hits a pair they follow.
BingX, the app's price source, publishes no liquidations. OKX does, on a public WebSocket
channel (`liquidation-orders`, `instType: SWAP`) that covers every swap in one subscription.
That channel pushes events as they happen, so polling a REST endpoint from the tick would
miss most of them.

## Decision
- While `FCM_SERVICE_ACCOUNT` is set, the lifespan runs `LiquidationWatcher`: one socket to
  `wss://ws.okx.com:8443/ws/v5/public`, a `ping` every 25 s, and a reconnect after 1, 2, 4 …
  60 s when the socket closes, errors, or stays silent for 50 s.
- On each connect it reads `ctVal` for the 7 tracked swaps from `/api/v5/public/instruments`.
  Notional is `sz × ctVal × bkPx`, in USD(T).
- `Detector` keeps per instrument and side the items of the last 60 s. When their sum reaches the
  instrument's threshold (`liquidations.TRACKED`), one notification message per language goes to
  `liq_<Whalio instrument id>_<lang>` on channel `liquidation_alerts`. That side then stays quiet
  for 15 minutes.
- Window, quiet periods and the socket are in memory. Nothing goes to Redis, and missed
  minutes are not backfilled.
- Each minute with liquidations on a tracked swap is logged, so thresholds can be tuned.

## Consequences
- Needs an always-on process. On hosts that stop idle processes the socket dies with the
  process, and the cron tick does not restart it.
- A restart can send a second message within 15 minutes, since quiet periods are in memory.
  Accepted, because restarts are rare.
- `websockets` becomes a direct dependency. uvicorn[standard] already installed it.
- Adding a Whalio language means adding it to `liquidations._TEXT` as well as `push._TEXT`.
