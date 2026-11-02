# ADR-007 — "Price alert" pushes from BingX 24h tickers

## Status
Accepted (2026-11-02). Whalio spec `specs/012-price-alerts/spec.md`.

## Context
Whalio users want a notification when a pair they watch has moved 5% or more over 24 hours,
with its current price, also while the app is closed. Polling from the phone is unreliable on
Android (Whalio ADR-002), and the service already sends FCM topic messages (ADR-004, ADR-005).
The product owner chose an hourly check, a fixed ±5% threshold on BingX's 24h change, and one
alert per crossing.

## Decision
- With `FCM_SERVICE_ACCOUNT` set, `CalendarService.tick` runs `PriceAlerts.run_if_due` at most
  once an hour (`crawl:price_alerts` mark, set before the read).
- One call to `GET /openApi/swap/v2/quote/ticker` returns every BingX swap; `ETH-BTC` has no swap
  and is read from `GET /openApi/spot/v1/ticker/24hr`. The instrument → BingX symbol table
  (`price_alerts.PAIRS`) mirrors the app's `bingxSymbol`.
- |`priceChangePercent`| ≥ 5 gives a side (`up` / `down`). The side is stored in
  `price:<instrument>` (3-day expiry) and a message per language goes to
  `price_<instrument>_<lang>` (Android channel `price_alerts`) when it differs from the stored
  one; under 5% the mark is deleted.
- Price and percentage are BingX's strings; nothing is rounded or guessed.

## Consequences
- Two BingX requests an hour; no key and far below its limits.
- A pair that stays past 5% is alerted once; it alerts again after falling back under 5%, or on
  the other side. The first run after a deploy alerts pairs already past 5%.
- A failed send is not retried; the side is stored before sending.
- Adding a Whalio instrument means adding it to `PAIRS`.
