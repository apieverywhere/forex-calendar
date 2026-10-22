import asyncio
import csv
import io
import logging
from contextlib import asynccontextmanager, suppress
from datetime import UTC, date, datetime, timedelta
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
import redis
from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response

from .config import Settings
from .models import CalendarResponse, EventOut
from .service import CalendarService
from .sources import SourceError
from .store import EventStore

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

_CSV_FIELDS = [
    "date", "time", "time_utc", "currency", "impact", "title",
    "actual", "forecast", "previous", "actual_status", "url",
]  # fmt: skip


def create_app(
    settings: Settings | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    redis_client: redis.Redis | None = None,
) -> FastAPI:
    settings = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        store = EventStore(
            redis_client
            or redis.Redis.from_url(settings.redis_url, decode_responses=True),
            retention=timedelta(days=settings.retention_days),
            prefix=settings.redis_prefix,
        )
        # Fail at startup, not on the first request, when Redis is down.
        store.ping()
        client = httpx.AsyncClient(
            transport=transport,
            timeout=20,
            follow_redirects=True,
            headers={"User-Agent": settings.user_agent},
        )
        service = CalendarService(settings, store, client)
        app.state.service = service
        # In-process scheduler. With REFRESH_MINUTES=0 an external cron calls
        # POST /api/v1/jobs/tick instead (hosts that stop idle processes).
        jobs = []
        if settings.refresh_minutes > 0:
            jobs.append(asyncio.create_task(service.run_jobs_loop()))
        yield
        for job in jobs:
            job.cancel()
            with suppress(asyncio.CancelledError):
                await job
        await client.aclose()
        store.close()

    app = FastAPI(
        title="Forex Calendar API",
        version="0.1.0",
        description=(
            "Economic calendar collected from Forex Factory: the public weekly "
            "export, plus day pages via Firecrawl when `FIRECRAWL_API_KEY` is set."
        ),
        lifespan=lifespan,
    )
    app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["GET", "POST"])

    def service() -> CalendarService:
        return app.state.service

    def zone(name: str | None) -> ZoneInfo:
        if not name:
            return settings.default_timezone
        try:
            return ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError):
            raise HTTPException(400, f"unknown time zone: {name}") from None

    def resolve_day(value: str, tz: ZoneInfo) -> date:
        today = datetime.now(UTC).astimezone(tz).date()
        keywords = {"today": 0, "tomorrow": 1, "yesterday": -1}
        if value.lower() in keywords:
            return today + timedelta(days=keywords[value.lower()])
        try:
            return date.fromisoformat(value)
        except ValueError:
            raise HTTPException(400, f"invalid date: {value} (use YYYY-MM-DD)") from None

    def split(value: str | None, *, upper: bool) -> set[str] | None:
        if not value:
            return None
        items = {v.strip() for v in value.split(",") if v.strip()}
        return {v.upper() if upper else v.lower() for v in items} or None

    async def calendar(
        start: date,
        end: date,
        tz: ZoneInfo,
        currency: str | None,
        impact: str | None,
        search: str | None,
        has_actual: bool,
        limit: int | None,
        offset: int,
        fmt: str,
    ):
        if end < start:
            raise HTTPException(400, "end is before start")
        if (end - start).days > 92:
            raise HTTPException(400, "range is limited to 92 days")
        events = await service().events(
            start,
            end,
            tz,
            currencies=split(currency, upper=True),
            impacts=split(impact, upper=False),
            search=search,
            has_actual=has_actual,
        )
        page = events[offset : offset + limit if limit else None]
        if fmt == "csv":
            return _csv(page, f"forex_calendar_{start}_{end}.csv")
        return CalendarResponse(
            start=start, end=end, timezone=tz.key, total=len(events), count=len(page), events=page
        )

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok", "time": datetime.now(UTC).isoformat()}

    @app.get("/api/v1/status")
    def status() -> dict:
        return service().status()

    @app.get("/api/v1/calendar", response_model=CalendarResponse)
    async def get_calendar(
        start: str | None = Query(None, description="First day, YYYY-MM-DD or today/tomorrow/yesterday"),
        end: str | None = Query(None, description="Last day (inclusive); defaults to start"),
        date_: str | None = Query(None, alias="date", description="Single day; overrides start/end"),
        tz: str | None = Query(None, description="IANA time zone for days and times, e.g. Asia/Ho_Chi_Minh"),
        currency: str | None = Query(None, description="Comma-separated, e.g. USD,EUR"),
        impact: str | None = Query(None, description="Comma-separated: High,Medium,Low,Holiday"),
        search: str | None = Query(None, description="Matches the title, or a currency code exactly"),
        has_actual: bool = Query(False, description="Only events whose Actual is published"),
        limit: int | None = Query(None, ge=1, le=1000),
        offset: int = Query(0, ge=0),
        format: Literal["json", "csv"] = "json",
    ):
        zone_ = zone(tz)
        first = resolve_day(date_ or start or "today", zone_)
        last = first if date_ else resolve_day(end, zone_) if end else first
        return await calendar(
            first, last, zone_, currency, impact, search, has_actual, limit, offset, format
        )

    @app.get("/api/v1/calendar/{day}", response_model=CalendarResponse)
    async def get_day(
        day: str,
        tz: str | None = None,
        currency: str | None = None,
        impact: str | None = None,
        format: Literal["json", "csv"] = "json",
    ):
        if day.endswith(".csv"):
            day, format = day[:-4], "csv"
        zone_ = zone(tz)
        resolved = resolve_day(day, zone_)
        return await calendar(
            resolved, resolved, zone_, currency, impact, None, False, None, 0, format
        )

    @app.get("/api/v1/week", response_model=CalendarResponse)
    async def get_week(
        tz: str | None = None,
        offset_weeks: int = Query(0, ge=-52, le=52, description="0 = this week, -1 = last week"),
        currency: str | None = None,
        impact: str | None = None,
    ):
        """Monday to Sunday in the requested time zone."""
        zone_ = zone(tz)
        today = datetime.now(UTC).astimezone(zone_).date()
        monday = today - timedelta(days=today.weekday()) + timedelta(weeks=offset_weeks)
        return await calendar(
            monday, monday + timedelta(days=6), zone_, currency, impact, None, False, None, 0, "json"
        )

    @app.get("/api/v1/currencies")
    async def currencies(tz: str | None = None) -> dict:
        zone_ = zone(tz)
        today = datetime.now(UTC).astimezone(zone_).date()
        events = await service().events(today - timedelta(days=7), today + timedelta(days=7), zone_)
        return {"currencies": sorted({e.currency for e in events})}

    def require_jobs_token(authorization: str | None) -> None:
        """Job endpoints spend reader credits, so they need JOBS_TOKEN; without
        one configured they do not exist."""
        if not settings.jobs_token:
            raise HTTPException(404, "Not Found")
        if authorization != f"Bearer {settings.jobs_token}":
            raise HTTPException(401, "invalid or missing bearer token")

    @app.post("/api/v1/jobs/tick")
    async def jobs_tick(authorization: str | None = Header(None)) -> dict:
        """Runs due jobs (feed refresh, Actual after releases, finalize
        yesterday, backfill). For external cron, e.g. every 2 minutes."""
        require_jobs_token(authorization)
        return await service().tick()

    @app.post("/api/v1/refresh")
    async def refresh(
        day: str | None = Query(None, description="Also crawl this day page (needs FIRECRAWL_API_KEY)"),
        authorization: str | None = Header(None),
    ) -> dict:
        require_jobs_token(authorization)
        result: dict = {}
        try:
            result["feed_events"] = await service().refresh_feed()
        except SourceError as error:
            result["feed_error"] = str(error)
        if day:
            resolved = resolve_day(day, settings.ff_page_timezone)
            try:
                result["page_events"] = await service().crawl_day(resolved)
            except SourceError as error:
                result["page_error"] = str(error)
        return result

    return app


def _csv(events: list[EventOut], filename: str) -> Response:
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=_CSV_FIELDS, extrasaction="ignore")
    writer.writeheader()
    for event in events:
        writer.writerow(event.model_dump(mode="json"))
    return Response(
        buffer.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


app = create_app()
