import os
from dataclasses import dataclass
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class Settings:
    redis_url: str = "redis://localhost:6379/0"
    # Events are deleted this many days after their release time. The app
    # shows at most last week, so 15 days covers it with a small margin.
    retention_days: int = 15
    redis_prefix: str = "fxcal:"
    # Requests only trigger page crawls back to the Monday this many weeks
    # ago; older days are served from storage only (saves reader credits).
    page_history_weeks: int = 1
    feed_url: str = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
    refresh_minutes: int = 30
    firecrawl_api_key: str | None = None
    firecrawl_base_url: str = "https://api.firecrawl.dev"
    # Readers for day pages, tried in this order; ones without a key are
    # skipped.
    page_fetchers: tuple[str, ...] = ("firecrawl",)
    # Bearer token for POST /api/v1/jobs/tick and /api/v1/refresh; unset
    # disables both endpoints (the in-process scheduler still runs).
    jobs_token: str | None = None
    # Firebase service account (JSON) for "Actual released" pushes; unset
    # disables them.
    fcm_service_account: str | None = None
    ff_page_timezone: ZoneInfo = ZoneInfo("America/New_York")
    default_timezone: ZoneInfo = ZoneInfo("Asia/Ho_Chi_Minh")
    user_agent: str = (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36"
    )

    @property
    def page_source_enabled(self) -> bool:
        return bool(self.active_page_fetchers)

    @property
    def active_page_fetchers(self) -> list[str]:
        keys = {"firecrawl": self.firecrawl_api_key}
        return [name for name in self.page_fetchers if keys.get(name)]

    @classmethod
    def from_env(cls) -> "Settings":
        env = os.environ
        defaults = cls()
        return cls(
            redis_url=env.get("REDIS_URL") or defaults.redis_url,
            retention_days=int(env.get("RETENTION_DAYS") or defaults.retention_days),
            redis_prefix=env.get("REDIS_PREFIX") or defaults.redis_prefix,
            page_history_weeks=int(
                env.get("PAGE_HISTORY_WEEKS") or defaults.page_history_weeks
            ),
            feed_url=env.get("FEED_URL") or defaults.feed_url,
            refresh_minutes=int(env.get("REFRESH_MINUTES") or defaults.refresh_minutes),
            firecrawl_api_key=env.get("FIRECRAWL_API_KEY") or None,
            firecrawl_base_url=env.get("FIRECRAWL_BASE_URL") or defaults.firecrawl_base_url,
            page_fetchers=tuple(
                name.strip()
                for name in (env.get("PAGE_FETCHERS") or ",".join(defaults.page_fetchers)).split(",")
                if name.strip()
            ),
            jobs_token=env.get("JOBS_TOKEN") or None,
            fcm_service_account=env.get("FCM_SERVICE_ACCOUNT") or None,
            ff_page_timezone=ZoneInfo(
                env.get("FF_PAGE_TIMEZONE") or defaults.ff_page_timezone.key
            ),
            default_timezone=ZoneInfo(
                env.get("DEFAULT_TIMEZONE") or defaults.default_timezone.key
            ),
        )
