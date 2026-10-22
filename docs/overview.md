# Overview

## Purpose
Give the Whalio app (and any other client) one stable API for the Forex Factory economic
calendar: events with time, currency, impact and Actual / Forecast / Previous, in the client's
time zone, filterable, as JSON or CSV.

Forex Factory has no official API. Its site blocks automated requests, and its public weekly
export covers only the current week. This service collects what is legitimately available, keeps
it, and hides the upstream details from clients.

## Scope

| In | Out |
|---|---|
| Collect the weekly export on a schedule | Bypassing Cloudflare or other access controls |
| Optional day pages via Firecrawl (with an API key) | Event detail pages, history charts, news |
| Store collected events in Redis for 15 days after release | User accounts, API keys for clients |
| Query by day range and time zone; filter by currency, impact, text, released | Push notifications (Whalio ADR-002 owns those) |
| JSON and CSV output | A web dashboard |

## Coverage at a glance

| Without `FIRECRAWL_API_KEY` | With `FIRECRAWL_API_KEY` |
|---|---|
| Current week from the export, plus past weeks collected while the service ran (kept 15 days) | Current and last week, Actual within minutes of release |
| Forecast, Previous | Forecast, Previous, **Actual**, better / worse status, link to the day page |
| About 80–100 events a week, all currencies Forex Factory lists | Same events |

## Terms
Forex Factory's terms restrict automated use of the site. The weekly export is published by
Forex Factory for public download. Reading day pages through a third-party reader is the
operator's decision; review the terms before commercial use.

## Status
Version 0.1.0. Both sources verified against live data on 2026-10-19: the export, and day pages
through a reader with a key (e.g. Oct 12–18: 109 events, 64 with Actual; times match the export).
