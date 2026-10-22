# ADR-001 — Sources: weekly export first, day pages only through a reader service

## Status
Accepted (2026-10-19)

## Context
The reference project (ForexFactory-Unofficial-API) tries, in order: Jina Reader (HTML, then
Markdown), a direct request to forexfactory.com, and the FairEconomy weekly export. From this
machine, direct requests get Cloudflare's 403 challenge, anonymous Jina requests get 401, and the
export works.

## Decision
1. The weekly export is the default source, refreshed on a schedule and stored.
2. Day pages are read only through a reader service with an API key: Firecrawl first (1 credit
   per page, free 1,000 / month), Jina Reader as fallback (added 2026-10-19, removed the same day; Firecrawl is now the only
   reader).
3. No direct requests to forexfactory.com and no attempt to defeat its bot protection
   (cloudscraper, headless browsers that mimic users, replayed cookies).
4. No Markdown fallback parser: it guesses columns from text and can assign wrong values.

## Consequences
- Without a key: current week plus collected history, no Actual values.
- With a key: full data, at a few seconds per day page.
- If Firecrawl stops serving Forex Factory pages, the service degrades to the export; it does not
  fail.
