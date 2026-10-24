import asyncio
import logging
from collections.abc import Callable
from datetime import UTC, date, datetime, time, timedelta, tzinfo
from zoneinfo import ZoneInfo

import httpx

from .config import Settings
from .models import Event, EventOut
from .sources import SourceError
from .sources.faireconomy import fetch_feed, parse_feed
from .sources.forexfactory_page import (
    NEXT_WEEK_URL,
    THIS_WEEK_URL,
    page_url,
    parse_day,
    parse_week,
)
from .push import LANGUAGES, FcmSender, PushError
from .sources.page_fetchers import FirecrawlFetcher, PageFetcher

log = logging.getLogger(__name__)

FEED_KEY = "faireconomy"
# Time of the last failed feed download (e.g. HTTP 429).
FEED_FAILED_KEY = "faireconomy:failed"
# Time of the last successful read of the next-week / this-week page.
NEXT_WEEK_KEY = "next-week"
THIS_WEEK_KEY = "this-week"
# The background jobs run this often (in process, or via POST /api/v1/jobs/tick).
TICK_INTERVAL = timedelta(minutes=2)
# After a release, its day page is re-read until Actual appears, for at most
# RELEASE_WINDOW: every tick for the first RELEASE_FAST_WINDOW (Forex Factory
# usually posts within a minute), then every RELEASE_SLOW_INTERVAL. A release
# that never gets an Actual costs about 10 reads instead of 30.
RELEASE_WINDOW = timedelta(minutes=60)
RELEASE_FAST_WINDOW = timedelta(minutes=10)
RELEASE_SLOW_INTERVAL = timedelta(minutes=10)
# While the export fails (e.g. HTTP 429 on a shared cloud IP), this week is
# read from the ?week=this page instead: after the export has been failing for
# THIS_WEEK_AFTER, at most once per THIS_WEEK_INTERVAL (one credit each).
THIS_WEEK_AFTER = timedelta(hours=2)
THIS_WEEK_INTERVAL = timedelta(hours=6)
_PAGE_CONCURRENCY = 3
# "Actual released" pushes go out only for releases this recent, so a backfill
# or a late read of an older day sends nothing (Whalio spec 010).
PUSH_WINDOW = timedelta(hours=3)


def day_bounds(start: date, end: date, tz: tzinfo) -> tuple[datetime, datetime]:
    """UTC instants covering [start, end] as calendar days in [tz]."""
    return (
        datetime.combine(start, time(0), tz).astimezone(UTC),
        datetime.combine(end + timedelta(days=1), time(0), tz).astimezone(UTC),
    )


def to_output(event: Event, tz: ZoneInfo) -> EventOut:
    local = event.time_utc.astimezone(tz)
    return EventOut(
        **event.model_dump(),
        date=local.date(),
        time=(event.time_note or "All Day") if event.all_day else local.strftime("%H:%M"),
    )


def awaits_actual(event: Event) -> bool:
    """A data release without its Actual yet. Speeches and holidays have no
    Forecast / Previous and never get an Actual."""
    return (
        event.actual is None
        and not event.all_day
        and event.impact != "Holiday"
        and (event.forecast is not None or event.previous is not None)
    )


class CalendarService:
    """Collects the calendar and answers queries.

    Day pages are read only by the jobs in [tick], never by queries:
    right after releases, once a day for releases that missed that window,
    and once for days in the viewable history that were never read.
    """

    def __init__(
        self,
        settings: Settings,
        store,
        client: httpx.AsyncClient,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        push: FcmSender | None = None,
    ):
        self.settings = settings
        self.store = store
        self.client = client
        self.now = now
        # None when FCM_SERVICE_ACCOUNT is unset: no notifications.
        self.push = push
        self.last_error: str | None = None
        self._feed_lock = asyncio.Lock()
        self._tick_lock = asyncio.Lock()
        self._page_semaphore = asyncio.Semaphore(_PAGE_CONCURRENCY)
        # Zone of the last page read; pages are rendered in the reader's zone.
        self._page_tz: tzinfo = settings.ff_page_timezone
        self.fetchers: list[PageFetcher] = []
        for name in settings.active_page_fetchers:
            if name == "firecrawl":
                self.fetchers.append(
                    FirecrawlFetcher(settings.firecrawl_api_key or "", settings.firecrawl_base_url)
                )

    # --- sources ------------------------------------------------------------

    async def refresh_feed(self) -> int:
        """Downloads the weekly feed and stores it. Returns the event count."""
        async with self._feed_lock:
            now = self.now()
            try:
                items = await fetch_feed(self.client, self.settings.feed_url)
            except SourceError as error:
                self.last_error = f"{now.isoformat()} feed: {error}"
                self.store.mark_crawled(FEED_FAILED_KEY, now)
                raise
            events = parse_feed(items, now)
            if events:
                start = min(e.time_utc for e in events)
                end = max(e.time_utc for e in events) + timedelta(seconds=1)
                # The export replaces its own rows and the next-week schedule.
                self.store.replace_window(
                    start, end, events, only_sources={"faireconomy", "forexfactory_week"}
                )
            self.store.mark_crawled(FEED_KEY, now)
            log.info("feed refreshed: %d events", len(events))
            return len(events)

    async def _fetch_page(self, url: str, label: str, now: datetime) -> str:
        """HTML of a forexfactory.com page through the first reader that
        succeeds; its cost is added to that reader's usage."""
        if not self.fetchers:
            raise SourceError("no page reader configured (FIRECRAWL_API_KEY)")
        errors = []
        for fetcher in self.fetchers:
            try:
                page = await fetcher.fetch(self.client, url)
                break
            except SourceError as error:
                # e.g. out of credits: fall back to the next reader.
                errors.append(str(error))
                log.warning("page %s: %s failed, trying next reader", label, fetcher.name)
        else:
            self.last_error = f"{now.isoformat()} page {label}: {'; '.join(errors)}"
            raise SourceError("; ".join(errors))
        self.store.add_usage(fetcher.name, now, page.cost)
        return page.html

    async def crawl_day(self, day: date) -> int:
        """Reads one forexfactory.com day page (page time zone) and stores it."""
        async with self._page_semaphore:
            now = self.now()
            html = await self._fetch_page(page_url(day), str(day), now)
            events, page_tz = parse_day(html, day, self._page_tz, now)
            self._page_tz = page_tz
            start, end = day_bounds(day, day, page_tz)
            # The page is authoritative for its day.
            self.store.replace_window(start, end, events)
            self.store.mark_crawled(f"page:{day.isoformat()}", now)
            return len(events)

    async def crawl_next_week(self) -> int:
        """Reads the next-week page (one reader credit) and stores its
        schedule; the export and day pages replace these rows later."""
        return await self._crawl_week(NEXT_WEEK_URL, NEXT_WEEK_KEY, "next week")

    async def crawl_this_week(self) -> int:
        """Reads the this-week page (one reader credit): the export's data,
        plus Actuals, when the export itself is unavailable."""
        return await self._crawl_week(THIS_WEEK_URL, THIS_WEEK_KEY, "this week")

    async def _crawl_week(self, url: str, key: str, label: str) -> int:
        async with self._page_semaphore:
            now = self.now()
            html = await self._fetch_page(url, label, now)
            events, page_tz, days = parse_week(html, self._page_tz, now)
            if not days:
                self.last_error = f"{now.isoformat()} page {label}: no days on the page"
                raise SourceError(f"{label} page lists no days")
            self._page_tz = page_tz
            start, end = day_bounds(min(days), max(days), page_tz)
            # The page is authoritative for the days it lists.
            self.store.replace_window(start, end, events)
            self.store.mark_crawled(key, now)
            log.info("%s: %d events", label, len(events))
            return len(events)

    async def _crawl_days(self, days: list[date], reason: str) -> int:
        results = await asyncio.gather(*(self.crawl_day(d) for d in days), return_exceptions=True)
        for day, result in zip(days, results):
            if isinstance(result, Exception):
                log.warning("%s: page %s failed: %s", reason, day, result)
        if days:
            log.info("%s: read %d page(s)", reason, len(days))
        return len(days)

    def _feed_due(self) -> bool:
        """Whether automatic refreshes may download the feed: the last
        success and the last failure are both older than the refresh interval.
        Retrying a rate-limited feed sooner keeps it blocked (HTTP 429)."""
        interval = timedelta(minutes=max(self.settings.refresh_minutes, 15))
        now = self.now()
        return all(
            at is None or now - at >= interval
            for at in self.store.crawled_many([FEED_KEY, FEED_FAILED_KEY])
        )

    def _page_today(self) -> date:
        return self.now().astimezone(self._page_tz).date()

    def earliest_page_day(self) -> date:
        """Oldest day kept complete: Monday `page_history_weeks` weeks back
        (page zone), less one day of margin for the zone difference."""
        today = self._page_today()
        monday = today - timedelta(days=today.weekday())
        return monday - timedelta(weeks=self.settings.page_history_weeks, days=1)

    # --- jobs ---------------------------------------------------------------

    def awaiting_actual(self) -> list[Event]:
        """Data releases from the last RELEASE_WINDOW still without Actual."""
        now = self.now()
        return [e for e in self.store.query(now - RELEASE_WINDOW, now) if awaits_actual(e)]

    async def refresh_released(self) -> int:
        """Re-reads the day pages of releases awaiting Actual: every tick while
        the newest pending release on that page is in its first
        RELEASE_FAST_WINDOW, then every RELEASE_SLOW_INTERVAL."""
        now = self.now()
        youngest: dict[date, timedelta] = {}
        for event in self.awaiting_actual():
            day = event.time_utc.astimezone(self._page_tz).date()
            age = now - event.time_utc
            youngest[day] = min(age, youngest.get(day, age))
        days = sorted(youngest)
        read_at = self.store.crawled_many([f"page:{day.isoformat()}" for day in days])
        due = [
            day
            for day, last in zip(days, read_at)
            if youngest[day] <= RELEASE_FAST_WINDOW
            or last is None
            or now - last >= RELEASE_SLOW_INTERVAL
        ]
        return await self._crawl_days(due, "releases awaiting Actual")

    async def finalize_yesterday(self) -> int:
        """Once a day: re-reads yesterday's page if a release there still has
        no Actual (it was posted after RELEASE_WINDOW). Usually reads nothing."""
        yesterday = self._page_today() - timedelta(days=1)
        key = f"final:{yesterday.isoformat()}"
        if self.store.crawled_at(key) is not None:
            return 0
        start, end = day_bounds(yesterday, yesterday, self._page_tz)
        missing = any(awaits_actual(e) for e in self.store.query(start, end))
        read = await self._crawl_days([yesterday] if missing else [], "finalize yesterday")
        self.store.mark_crawled(key, self.now())
        return read

    async def refresh_next_week(self) -> int:
        """Once a day (page zone): reads the next-week page, so the app shows
        next week before the export rolls over. A failure waits for the next
        day rather than spending a credit every tick."""
        key = f"next-week:{self._page_today().isoformat()}"
        if self.store.crawled_at(key) is not None:
            return 0
        self.store.mark_crawled(key, self.now())
        try:
            return await self.crawl_next_week()
        except SourceError as error:
            log.warning("next week: %s", error)
            return 0

    async def refresh_this_week(self) -> int:
        """Reads the this-week page while the export is failing: once it has
        failed for THIS_WEEK_AFTER since its last success, at most every
        THIS_WEEK_INTERVAL. Never runs while the export works."""
        now = self.now()
        succeeded, failed, read = self.store.crawled_many(
            [FEED_KEY, FEED_FAILED_KEY, THIS_WEEK_KEY + ":tried"]
        )
        failing = failed is not None and (succeeded is None or failed > succeeded)
        if not failing:
            return 0
        if succeeded is not None and now - succeeded < THIS_WEEK_AFTER:
            return 0
        if read is not None and now - read < THIS_WEEK_INTERVAL:
            return 0
        self.store.mark_crawled(THIS_WEEK_KEY + ":tried", now)
        try:
            return await self.crawl_this_week()
        except SourceError as error:
            log.warning("this week: %s", error)
            return 0

    async def backfill(self) -> int:
        """Reads, once, every day of the viewable history up to yesterday that
        was never read (e.g. right after the first deploy)."""
        first, yesterday = self.earliest_page_day(), self._page_today() - timedelta(days=1)
        history = [first + timedelta(days=i) for i in range((yesterday - first).days + 1)]
        marks = self.store.crawled_many([f"page:{day.isoformat()}" for day in history])
        days = [day for day, mark in zip(history, marks) if mark is None]
        return await self._crawl_days(days, "backfill")

    async def push_actuals(self) -> int:
        """Sends "Actual released" for releases of the last PUSH_WINDOW that
        have their Actual, once per event and language (marked in Redis, so a
        revised Actual is not sent again). A failed send is retried next tick."""
        if self.push is None:
            return 0
        now = self.now()
        released = [e for e in self.store.query(now - PUSH_WINDOW, now) if e.actual is not None]
        messages = [(event, lang) for event in released for lang in LANGUAGES]
        marks = self.store.crawled_many([f"push:{e.id}:{lang}" for e, lang in messages])
        sent = 0
        for (event, lang), mark in zip(messages, marks):
            if mark is not None:
                continue
            try:
                await self.push.send(event, lang)
            except PushError as error:
                self.last_error = f"{now.isoformat()} push: {error}"
                log.warning("push %s %s: %s", event.id, lang, error)
                continue
            self.store.mark_crawled(f"push:{event.id}:{lang}", now)
            sent += 1
        if sent:
            log.info("pushed %d Actual notification(s)", sent)
        return sent

    async def tick(self) -> dict:
        """Runs every job that is due. Safe to call often: each job skips work
        already done (state lives in Redis)."""
        async with self._tick_lock:
            report: dict = {}
            if self._feed_due():
                try:
                    report["feed_events"] = await self.refresh_feed()
                except SourceError as error:
                    report["feed_error"] = str(error)
            if self.fetchers:
                report["released_pages"] = await self.refresh_released()
                report["finalized_pages"] = await self.finalize_yesterday()
                report["backfilled_pages"] = await self.backfill()
                report["next_week_events"] = await self.refresh_next_week()
                report["this_week_events"] = await self.refresh_this_week()
            if self.push is not None:
                report["pushed"] = await self.push_actuals()
            return report

    async def run_jobs_loop(self) -> None:
        """In-process scheduler: [tick] every TICK_INTERVAL."""
        while True:
            try:
                await self.tick()
            except Exception:  # noqa: BLE001 - the loop must survive anything
                log.exception("tick failed")
            await asyncio.sleep(TICK_INTERVAL.total_seconds())

    # --- queries ------------------------------------------------------------

    async def events(
        self,
        start: date,
        end: date,
        tz: ZoneInfo,
        *,
        currencies: set[str] | None = None,
        impacts: set[str] | None = None,
        search: str | None = None,
        has_actual: bool = False,
    ) -> list[EventOut]:
        """Stored events only; never reads a day page (no reader credits)."""
        start_utc, end_utc = day_bounds(start, end, tz)
        # The feed is free; refreshing it here keeps a service without jobs
        # (REFRESH_MINUTES=0 and no cron) usable for the current week. With the
        # in-process scheduler the jobs refresh it, so requests never wait on
        # a feed download (up to the 20 s client timeout).
        if self.settings.refresh_minutes == 0 and self._feed_due():
            try:
                await self.refresh_feed()
            except SourceError:
                log.warning("feed refresh failed; serving stored data")
        needle = (search or "").strip().lower()
        return [
            to_output(e, tz)
            for e in self.store.query(start_utc, end_utc)
            if (not currencies or e.currency in currencies)
            and (not impacts or e.impact.lower() in impacts)
            and (not needle or needle in e.title.lower() or needle == e.currency.lower())
            and (not has_actual or e.actual is not None)
        ]

    def status(self) -> dict:
        feed = self.store.crawled_at(FEED_KEY)
        now = self.now()
        return {
            "events_stored": self.store.count(),
            "feed_refreshed_at": feed.isoformat() if feed else None,
            "refresh_minutes": self.settings.refresh_minutes,
            "retention_days": self.settings.retention_days,
            "page_fetchers": [f.name for f in self.fetchers],
            "push_enabled": self.push is not None,
            # Firecrawl credits spent this month (UTC).
            "usage_this_month": {f.name: self.store.usage(f.name, now) for f in self.fetchers},
            "last_error": self.last_error,
        }
