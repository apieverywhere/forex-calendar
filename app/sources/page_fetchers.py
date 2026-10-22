"""Services that read forexfactory.com day pages on our behalf.

forexfactory.com sits behind a Cloudflare bot check, so day pages are
requested through a reader service that returns the rendered HTML. Each
fetcher returns the page and what the call cost in its own unit; the service
tries them in the order of `PAGE_FETCHERS` and falls back to the next one on
any failure (including an exhausted quota).

Checked 2026-10-19:
- Firecrawl: 1 credit per page (free plan: 1,000 credits / month). The page
  is rendered by JavaScript, so it waits 3 s; without `maxAge: 0` an older
  cached copy may come back. Rendered in UTC-5.
"""

from dataclasses import dataclass
from typing import Protocol

import httpx

from . import SourceError


@dataclass(frozen=True)
class FetchedPage:
    html: str
    # Units of the fetcher's quota spent: Firecrawl credits.
    cost: int


class PageFetcher(Protocol):
    name: str

    async def fetch(self, client: httpx.AsyncClient, url: str) -> FetchedPage: ...


def _rendered_calendar(html: str, provider: str) -> str:
    if "calendar__row" not in html:
        raise SourceError(f"{provider}: page has no calendar table (blocked or layout changed)")
    # Rows exist before the page's script fills them; blank cells mean the
    # reader returned the page too early.
    if "calendar__cell--blank" in html:
        raise SourceError(f"{provider}: calendar rows were not rendered")
    return html


class FirecrawlFetcher:
    name = "firecrawl"

    def __init__(self, api_key: str, base_url: str = "https://api.firecrawl.dev"):
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")

    async def fetch(self, client: httpx.AsyncClient, url: str) -> FetchedPage:
        try:
            response = await client.post(
                f"{self._base_url}/v1/scrape",
                headers={"Authorization": f"Bearer {self._api_key}"},
                json={
                    "url": url,
                    "formats": ["rawHtml"],
                    "onlyMainContent": False,
                    # Never a cached copy: Actual values and the header clock
                    # (used for the page's time zone) must be current.
                    "maxAge": 0,
                    "actions": [{"type": "wait", "milliseconds": 3000}],
                },
                timeout=90,
            )
        except httpx.HTTPError as error:
            raise SourceError(f"firecrawl unreachable: {error}") from error
        if response.status_code != 200:
            # 402: out of credits; 429: rate limited.
            raise SourceError(f"firecrawl HTTP {response.status_code}: {response.text[:200]}")
        body = response.json()
        if not body.get("success"):
            raise SourceError(f"firecrawl: {body.get('error')}")
        data = body.get("data") or {}
        html = _rendered_calendar(data.get("rawHtml") or "", self.name)
        credits = (data.get("metadata") or {}).get("creditsUsed")
        return FetchedPage(html, credits if isinstance(credits, int) else 1)

