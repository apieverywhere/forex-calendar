from datetime import UTC, datetime, timedelta

from .conftest import AUTH, make_client


def test_calendar_returns_feed_events_in_the_requested_time_zone(upstream, redis_server):
    with make_client(upstream, redis_server) as client:
        body = client.get("/api/v1/calendar", params={"date": "2026-10-14"}).json()

    assert body["timezone"] == "Asia/Ho_Chi_Minh"
    # 08:30 New York on the 14th is 19:30 in Hà Nội; ZEW (05:00 NY) is 16:00.
    assert [(e["time"], e["title"]) for e in body["events"]] == [
        ("19:30", "CPI m/m"),
    ]
    other = client_get(upstream, redis_server, {"date": "2026-10-13"})
    assert [(e["time"], e["currency"]) for e in other["events"]] == [("16:00", "EUR")]


def client_get(upstream, redis_server, params):
    with make_client(upstream, redis_server) as client:
        return client.get("/api/v1/calendar", params=params).json()


def test_filters_and_csv(upstream, redis_server):
    with make_client(upstream, redis_server) as client:
        params = {"start": "2026-10-12", "end": "2026-10-19", "tz": "America/New_York"}
        everything = client.get("/api/v1/calendar", params=params).json()
        high = client.get("/api/v1/calendar", params={**params, "impact": "high"}).json()
        cny = client.get("/api/v1/calendar", params={**params, "currency": "cny"}).json()
        search = client.get("/api/v1/calendar", params={**params, "search": "zew"}).json()
        csv = client.get("/api/v1/calendar", params={**params, "format": "csv"})

    assert everything["total"] == 4
    assert [e["title"] for e in high["events"]] == ["CPI m/m"]
    assert [e["title"] for e in cny["events"]] == ["GDP q/y"]
    assert [e["currency"] for e in search["events"]] == ["EUR"]
    assert csv.headers["content-type"].startswith("text/csv")
    assert csv.text.splitlines()[0].startswith("date,time,time_utc,currency")
    assert len(csv.text.splitlines()) == 5


def test_feed_is_reused_while_fresh(upstream, redis_server):
    with make_client(upstream, redis_server) as client:
        client.get("/api/v1/calendar", params={"date": "2026-10-14"})
        client.get("/api/v1/calendar", params={"date": "2026-10-13"})

    assert upstream.feed_calls == 1


def test_stored_data_survives_a_failing_feed(upstream, redis_server):
    with make_client(upstream, redis_server) as client:
        client.post("/api/v1/refresh", headers=AUTH)
    upstream.feed_status = 429

    with make_client(upstream, redis_server) as client:
        refresh = client.post("/api/v1/refresh", headers=AUTH).json()
        body = client.get("/api/v1/calendar", params={"date": "2026-10-14"}).json()
        status = client.get("/api/v1/status").json()

    assert "feed_error" in refresh
    assert [e["title"] for e in body["events"]] == ["CPI m/m"]
    assert status["events_stored"] == 4
    assert "HTTP 429" in status["last_error"]


def test_page_source_adds_actuals_to_feed_rows(upstream, redis_server):
    with make_client(upstream, redis_server, firecrawl_api_key="fc-test") as client:
        refresh = client.post(
            "/api/v1/refresh", params={"day": "2026-10-14"}, headers=AUTH
        ).json()
        body = client.get(
            "/api/v1/calendar", params={"date": "2026-10-14", "tz": "America/New_York"}
        ).json()
        status = client.get("/api/v1/status").json()

    assert refresh["page_events"] == 3
    # Queries never read day pages; only the refresh did.
    assert len(upstream.firecrawl_calls) == 1
    cpi = next(e for e in body["events"] if e["title"] == "CPI m/m")
    assert (cpi["actual"], cpi["actual_status"], cpi["source"]) == ("0.2%", "better", "forexfactory")
    assert [e["title"] for e in body["events"]] == ["BOE Gov Speaks", "CPI m/m", "Core CPI m/m"]
    assert status["usage_this_month"] == {"firecrawl": 1}


def test_feed_refresh_keeps_actuals_from_pages(upstream, redis_server):
    with make_client(upstream, redis_server, firecrawl_api_key="fc-test") as client:
        client.post("/api/v1/refresh", params={"day": "2026-10-14"}, headers=AUTH)
        client.post("/api/v1/refresh", headers=AUTH)
        body = client.get(
            "/api/v1/calendar",
            params={"date": "2026-10-14", "tz": "America/New_York", "has_actual": True},
        ).json()

    assert [(e["title"], e["actual"]) for e in body["events"]] == [
        ("CPI m/m", "0.2%"),
        ("Core CPI m/m", "0.4%"),
    ]


def test_bad_input_is_rejected(upstream, redis_server):
    with make_client(upstream, redis_server) as client:
        assert client.get("/api/v1/calendar", params={"date": "14/10"}).status_code == 400
        assert client.get("/api/v1/calendar", params={"tz": "Mars/Base"}).status_code == 400
        assert (
            client.get(
                "/api/v1/calendar", params={"start": "2026-10-14", "end": "2026-10-01"}
            ).status_code
            == 400
        )


def test_queries_never_read_day_pages(upstream, redis_server):
    with make_client(upstream, redis_server, firecrawl_api_key="fc-test") as client:
        for params in (
            {"date": "2026-10-14"},
            {"start": "2026-10-05", "end": "2026-10-25"},
            {"date": (datetime.now(UTC).date() - timedelta(weeks=5)).isoformat()},
        ):
            client.get("/api/v1/calendar", params=params)
        client.get("/api/v1/week")

    assert upstream.firecrawl_calls == []


def test_job_endpoints_need_the_token(upstream, redis_server):
    with make_client(upstream, redis_server) as client:
        assert client.post("/api/v1/jobs/tick").status_code == 401
        assert client.post("/api/v1/refresh", headers={"Authorization": "Bearer x"}).status_code == 401
        assert client.post("/api/v1/jobs/tick", headers=AUTH).status_code == 200
    with make_client(upstream, redis_server, jobs_token=None) as client:
        assert client.post("/api/v1/jobs/tick", headers=AUTH).status_code == 404


def test_firecrawl_reads_pages_with_no_cache(upstream, redis_server):
    with make_client(upstream, redis_server, firecrawl_api_key="fc-test") as client:
        refresh = client.post("/api/v1/refresh", params={"day": "2026-10-14"}, headers=AUTH).json()
        status = client.get("/api/v1/status").json()

    assert refresh["page_events"] == 3
    (call,) = upstream.firecrawl_calls
    assert call["url"].endswith("day=oct14.2026")
    assert call["maxAge"] == 0
    assert status["page_fetchers"] == ["firecrawl"]
    assert status["usage_this_month"] == {"firecrawl": 1}

