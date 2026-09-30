from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.integrations import net, sources
from app.integrations.errors import IntegrationError
from app.models import Job, ModelCall, Schedule, Source, Topic, now
from app.schemas import canonical_x_search_url
from app.services import AppError, enqueue
from app.topic_ranking import heat_score, same_event, title_tokens, versions_compatible
from app.worker import Worker, tick_schedules
from app.workflow import TaskContext, collect_enabled_sources

INSTANT = datetime(2026, 9, 30, tzinfo=UTC)
SEARCH_URL = "https://x.com/search?q=from%3Axai%20OR%20from%3Agrok&f=live"


def _flat(identifier: str, text: str, author: str, *, days_ago: float = 1, **extra) -> dict:
    stamp = (INSTANT - timedelta(days=days_ago)).strftime("%a %b %d %H:%M:%S +0000 %Y")
    return {"tweet_id": identifier, "text": text, "created_at": stamp, "favorites": 42, **extra} | (
        {"author": {"screen_name": author}} if author else {}
    )


def _mock_tikhub(monkeypatch, handler) -> None:
    original = httpx.Client
    monkeypatch.setattr(
        "app.providers.httpx.Client",
        lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs),
    )


# --- relevance -------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "title",
    [
        "Grok 5 is rolling out to everyone",
        "ChatGPT now supports voice in Chrome",
        "xAI releases Grok Imagine",
        "Sora 3 is here",
        "Meta releases Llama 5",
        "Introducing GPT-6 Astra",
    ],
)
def test_relevance_recognizes_current_ai_products(title):
    score, reason = sources._relevance(title, "")
    assert score > 0 and "AI" in reason


def test_focus_keywords_boost_and_respect_word_boundaries():
    assert sources._relevance("She said hello", "", ["ai"])[0] == 0
    boosted, reason = sources._relevance("Codex Security Cloud upgrade", "", ["Codex Security"])
    assert boosted > sources._relevance("Codex Security Cloud upgrade", "")[0]
    assert "关注词 Codex Security" in reason
    assert sources._relevance("豆包大模型发布新版本", "", ["豆包"])[0] > 0
    assert sources.source_keywords(["  Grok ", "grok", "", 3, "Claude"]) == ["Grok", "Claude"]


# --- TikHub flattened responses --------------------------------------------------------------------


def test_tikhub_flat_user_posts_include_pinned_quote_and_age_bound():
    data = {
        "pinned": _flat("11", "Introducing GPT-6 Astra", "OpenAI"),
        "timeline": [
            _flat(
                "12", "Codex update ships today", "OpenAI", quoted=_flat("13", "Claude context", "claudeai")
            ),
            _flat("14", "Old AI post", "OpenAI", days_ago=40),
            _flat("15", "", "OpenAI"),
        ],
        "next_cursor": "c",
        "status": "ok",
    }
    result = sources._collect_x_tikhub(data, 30, instant=INSTANT)
    assert [item["url"] for item in result["items"]] == [
        "https://x.com/OpenAI/status/11",
        "https://x.com/OpenAI/status/12",
    ]
    assert "https://x.com/claudeai/status/13" in result["items"][1]["summary"]
    assert result["items"][0]["score"] == 42


def test_tikhub_search_keeps_query_matches_without_literal_text_match():
    data = {
        "timeline": [
            {**_flat("21", "Big day for the team", ""), "screen_name": "xai", "type": "tweet"},
            {**_flat("22", "Grok 5 benchmark results", ""), "user_info": {"screen_name": "grok"}},
        ],
        "status": "ok",
    }
    result = sources.collect_source({"kind": "x_tikhub_search", "url": SEARCH_URL}, tikhub_data=data)
    by_url = {item["url"]: item for item in result["items"]}
    assert by_url["https://x.com/xai/status/21"]["selection_reason"] == "匹配 X 搜索条件"
    assert by_url["https://x.com/grok/status/22"]["relevance_score"] > 45
    assert "搜索" in result["warning"]
    # Profile collection keeps its stricter AI filter.
    profile = sources._collect_x_tikhub(data, 30, instant=INSTANT)
    assert [item["url"] for item in profile["items"]] == ["https://x.com/grok/status/22"]


def test_tikhub_flat_empty_timeline_is_a_legitimate_empty_page():
    assert sources._collect_x_tikhub({"timeline": [], "status": "ok"}, 30)["items"] == []


# --- X search URLs ---------------------------------------------------------------------------------


def test_search_url_canonicalization_and_parsing():
    assert (
        canonical_x_search_url("https://twitter.com/search?q=Grok+OR+xAI&src=typed_query")
        == "https://x.com/search?q=Grok%20OR%20xAI&f=live"
    )
    assert sources.x_search_query("https://x.com/search?q=Grok%20OR%20xAI&f=top") == ("Grok OR xAI", "Top")
    assert sources.x_search_query(SEARCH_URL) == ("from:xai OR from:grok", "Latest")
    for bad in (
        "https://x.com/search?q=",
        "https://x.com/search?q=a&f=media",
        "https://example.com/search?q=a",
        "https://x.com/OpenAI",
        "https://x.com/search?q=a&extra=1",
    ):
        with pytest.raises(ValueError):
            canonical_x_search_url(bad)
    with pytest.raises(IntegrationError):
        sources.x_search_query("https://x.com/search?q=a&f=media")


# --- HTML listing pages ----------------------------------------------------------------------------

LISTING = b"""<!doctype html><html><body>
<nav><a href="/news">News</a><a href="/careers/job">Careers opening here</a></nav>
<a href="/news/claude-discovers-enzyme"><div><time>Sep 23, 2026</time><span>Science</span></div>
  <span> Claude discovers a novel enzyme system</span></a>
<article><time datetime="2026-09-18T10:00:00Z"></time>
  <a href="/news/accenture-evaluation"><h3>Partnering with Accenture on evaluations</h3></a></article>
<a href="/news/accenture-evaluation">Read</a>
<a href="https://other.example/news/elsewhere">Some other site article</a>
<a href="/news/short">Tiny</a>
<script>var x = '<a href="/news/scripted">Scripted link title</a>';</script>
</body></html>"""


def test_html_list_extracts_same_host_articles_with_titles_and_dates(monkeypatch):
    monkeypatch.setattr(
        sources, "safe_get", lambda url, **_kwargs: (LISTING, "text/html; charset=utf-8", url)
    )
    result = sources.collect_source({"kind": "html_list", "url": "https://www.anthropic.com/news"})
    items = result["items"]
    assert [item["url"] for item in items] == [
        "https://www.anthropic.com/news/claude-discovers-enzyme",
        "https://www.anthropic.com/news/accenture-evaluation",
    ]
    assert items[0]["title"] == "Claude discovers a novel enzyme system"
    assert items[0]["published_at"] == "2026-09-23T00:00:00+00:00"
    assert items[1]["title"] == "Partnering with Accenture on evaluations"
    assert items[1]["published_at"].startswith("2026-09-18T10:00:00")
    assert items[1]["selection_reason"] == "来自已配置的网页列表"


def test_html_list_reports_missing_articles_and_validates_prefix(monkeypatch):
    monkeypatch.setattr(sources, "safe_get", lambda url, **_kwargs: (LISTING, "text/html", url))
    with pytest.raises(IntegrationError) as caught:
        sources.collect_source(
            {
                "kind": "html_list",
                "url": "https://www.anthropic.com/news",
                "options": {"path_prefix": "/blog/"},
            }
        )
    assert caught.value.code == "LISTING_EMPTY"
    with pytest.raises(IntegrationError):
        sources.collect_source(
            {"kind": "html_list", "url": "https://www.anthropic.com/news", "options": {"path_prefix": "news"}}
        )
    monkeypatch.setattr(sources, "safe_get", lambda url, **_kwargs: (b'{"a":1}', "application/json", url))
    with pytest.raises(IntegrationError) as caught:
        sources.collect_source({"kind": "html_list", "url": "https://www.anthropic.com/news"})
    assert caught.value.code == "INVALID_LISTING"


# --- network resilience ----------------------------------------------------------------------------


def _http_error(status: int, retry_after: float | None = None) -> IntegrationError:
    error = IntegrationError("HTTP_ERROR", f"来源返回 HTTP {status}")
    error.status, error.retry_after = status, retry_after
    return error


def test_safe_get_retries_transient_status_with_bounded_backoff(monkeypatch):
    calls, sleeps = [], []
    outcomes = [_http_error(429, 30), _http_error(503), (b"ok", "text/plain", "https://example.org/")]

    def once(url, **_kwargs):
        calls.append(url)
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(net, "_safe_get_once", once)
    monkeypatch.setattr(net.time, "sleep", sleeps.append)
    assert net.safe_get("https://example.org/", retries=2)[0] == b"ok"
    assert len(calls) == 3 and sleeps == [10.0, 3.0]


def test_safe_get_does_not_retry_definitive_errors_or_by_default(monkeypatch):
    calls = []

    def once(url, **_kwargs):
        calls.append(url)
        raise _http_error(404) if len(calls) == 1 else _http_error(429)

    monkeypatch.setattr(net, "_safe_get_once", once)
    monkeypatch.setattr(net.time, "sleep", lambda _seconds: None)
    with pytest.raises(IntegrationError):
        net.safe_get("https://example.org/", retries=2)
    with pytest.raises(IntegrationError):
        net.safe_get("https://example.org/")
    assert len(calls) == 2
    with pytest.raises(IntegrationError):
        net.safe_get("https://example.org/", retries=9)


def test_doh_falls_back_to_second_resolver(monkeypatch):
    net._DNS_CACHE.clear()
    used = []

    def query(resolver, hostname, proxy):
        used.append(resolver[0])
        if resolver[0] == "dns.google":
            raise httpx.ConnectTimeout("handshake timed out")
        return {"93.184.216.34"}, 30

    monkeypatch.setattr(net, "_doh_query", query)
    assert net._resolve_addresses("fallback.example", 443, "http://127.0.0.1:7897") == {"93.184.216.34"}
    assert used == ["dns.google", "cloudflare-dns.com"]
    net._DNS_CACHE.clear()


# --- API ---------------------------------------------------------------------------------------------


def test_source_api_accepts_search_and_listing_sources(client):
    search = client.post(
        "/api/v1/sources",
        json={
            "name": "xAI",
            "kind": "x_tikhub_search",
            "url": "https://x.com/search?q=from:xai",
            "keywords": ["Grok", "grok"],
        },
    )
    assert search.status_code == 201, search.text
    assert search.json()["url"] == "https://x.com/search?q=from%3Axai&f=live"
    assert search.json()["keywords"] == ["Grok"]
    listing = client.post(
        "/api/v1/sources",
        json={
            "name": "A",
            "kind": "html_list",
            "url": "https://www.anthropic.com/news",
            "options": {"path_prefix": "/news/"},
        },
    )
    assert listing.status_code == 201 and listing.json()["options"] == {"path_prefix": "/news/"}
    bad = client.post(
        "/api/v1/sources",
        json={
            "name": "R",
            "kind": "rss",
            "url": "https://example.org/feed",
            "options": {"path_prefix": "/x/"},
        },
    )
    assert bad.status_code == 422
    patched = client.patch(f"/api/v1/sources/{search.json()['id']}", json={"keywords": ["Grok 5"]})
    assert patched.status_code == 200 and patched.json()["keywords"] == ["Grok 5"]
    converted = client.patch(f"/api/v1/sources/{search.json()['id']}", json={"kind": "x"})
    assert converted.status_code == 409


def test_paid_search_collect_requires_explicit_confirmation(client, config):
    config.tikhub_api_key = SecretStr("fixture-tikhub-key")
    source = client.post(
        "/api/v1/sources", json={"name": "s", "kind": "x_tikhub_search", "url": SEARCH_URL}
    ).json()
    unconfirmed = client.post(f"/api/v1/sources/{source['id']}/collect")
    assert unconfirmed.status_code == 409 and unconfirmed.json()["code"] == "SOURCE_PROVIDER_CHANGED"
    confirmed = client.post(
        f"/api/v1/sources/{source['id']}/collect", json={"expected_kind": "x_tikhub_search"}
    )
    assert confirmed.status_code == 202, confirmed.text


def test_source_presets_are_listed_and_applied_once(client):
    listed = client.get("/api/v1/source-presets").json()["items"]
    ids = [item["id"] for item in listed]
    assert "openai-news" in ids and "x-vendor-accounts" in ids
    assert all(not item["added"] for item in listed)
    first = client.post("/api/v1/source-presets/apply", json={"ids": ["openai-news", "anthropic-news"]})
    assert first.status_code == 201 and len(first.json()["items"]) == 2
    again = client.post("/api/v1/source-presets/apply", json={"ids": ["openai-news"]})
    assert again.json() == {"items": [], "skipped": 1}
    assert client.post("/api/v1/source-presets/apply", json={"ids": ["missing"]}).status_code == 404
    marked = {item["id"]: item["added"] for item in client.get("/api/v1/source-presets").json()["items"]}
    assert marked["openai-news"] and marked["anthropic-news"] and not marked["deepmind-blog"]
    # Every preset URL must be accepted by the same validation users get.
    for preset in listed:
        body = {"name": preset["name"], "kind": preset["kind"], "url": preset["url"]}
        if preset.get("options"):
            body["options"] = preset["options"]
        response = client.post("/api/v1/sources", json=body)
        assert response.status_code == 201, (preset["id"], response.text)
        assert response.json()["url"] == preset["url"]


# --- workflow ----------------------------------------------------------------------------------------


def _run_collect_job(client, engine, config, source_id, expected_kind=None):
    body = {"expected_kind": expected_kind} if expected_kind else None
    queued = client.post(f"/api/v1/sources/{source_id}/collect", json=body)
    assert queued.status_code == 202, queued.text
    assert Worker(config, engine=engine).run_once()
    return client.get(f"/api/v1/jobs/{queued.json()['job_id']}").json()


def test_search_collection_calls_search_endpoint_once(client, engine, config, monkeypatch):
    config.tikhub_api_key = SecretStr("fixture-tikhub-key")
    source = client.post(
        "/api/v1/sources", json={"name": "s", "kind": "x_tikhub_search", "url": SEARCH_URL}
    ).json()
    requests = []
    recent = (datetime.now(UTC) - timedelta(hours=2)).strftime("%a %b %d %H:%M:%S +0000 %Y")

    def handler(request):
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "code": 200,
                "data": {
                    "timeline": [
                        {
                            "tweet_id": "31",
                            "text": "Grok 5 is live",
                            "created_at": recent,
                            "screen_name": "xai",
                        }
                    ]
                },
            },
        )

    _mock_tikhub(monkeypatch, handler)
    job = _run_collect_job(client, engine, config, source["id"], "x_tikhub_search")
    assert job["status"] == "SUCCEEDED", job
    assert len(requests) == 1 and requests[0].url.path.endswith("/fetch_search_timeline")
    assert dict(requests[0].url.params) == {"keyword": "from:xai OR from:grok", "search_type": "Latest"}
    with Session(engine) as session:
        assert session.scalar(select(ModelCall)).model == "tikhub:twitter-search"
        assert session.scalar(select(Topic.url)) == "https://x.com/xai/status/31"


def test_same_event_from_second_source_merges_into_one_topic(client, engine, config, monkeypatch):
    first = client.post(
        "/api/v1/sources", json={"name": "A", "kind": "rss", "url": "https://a.example/feed"}
    ).json()
    second = client.post(
        "/api/v1/sources", json={"name": "B", "kind": "rss", "url": "https://b.example/feed"}
    ).json()
    feeds = {
        first["id"]: [
            ("OpenAI launches GPT-6 Astra reasoning model for developers", "https://a.example/1"),
            # A near-identical second post from the same source stays a separate story.
            ("OpenAI launches GPT-6 Astra reasoning model for developers worldwide", "https://a.example/4"),
        ],
        second["id"]: [
            ("GPT-6 Astra reasoning model launches for developers", "https://b.example/2"),
            ("Mistral ships a completely different OCR product", "https://b.example/3"),
        ],
    }

    def fake_collect(source, **_kwargs):
        items = [
            {"title": title, "url": url, "summary": title, "relevance_score": 60, "score": 5}
            for title, url in feeds[source["id"]]
        ]
        return {
            "items": items,
            "complete": True,
            "scanned": len(items),
            "candidates": len(items),
            "warning": "",
        }

    monkeypatch.setattr("app.integrations.sources.collect_source", fake_collect)
    assert _run_collect_job(client, engine, config, first["id"])["status"] == "SUCCEEDED"
    result = _run_collect_job(client, engine, config, second["id"])
    assert result["result"]["merged"] == 1 and result["result"]["added"] == 1
    with Session(engine) as session:
        topics = {
            topic.url: topic
            for topic in session.scalars(select(Topic).where(Topic.url.like("https://_.example/%")))
        }
    assert set(topics) == {"https://a.example/1", "https://a.example/4", "https://b.example/3"}
    clustered = [topic for topic in topics.values() if topic.cluster_size == 2]
    assert len(clustered) == 1 and clustered[0].related[0]["source_name"] == "B"
    assert clustered[0].related[0]["url"] == "https://b.example/2"
    listed = client.get("/api/v1/topics").json()["items"]
    assert listed[0]["url"] == clustered[0].url and listed[0]["heat_score"] > listed[-1]["heat_score"]


def test_source_health_tracks_failures_backoff_and_recovery(client, engine, config, monkeypatch):
    source = client.post(
        "/api/v1/sources", json={"name": "A", "kind": "rss", "url": "https://a.example/feed"}
    ).json()
    outcomes = [
        IntegrationError("HTTP_ERROR", "来源返回 HTTP 503"),
        IntegrationError("HTTP_ERROR", "来源返回 HTTP 503"),
    ]

    def fake_collect(_source, **_kwargs):
        if outcomes:
            raise outcomes.pop(0)
        return {"items": [], "complete": True, "scanned": 0, "candidates": 0, "warning": ""}

    monkeypatch.setattr("app.integrations.sources.collect_source", fake_collect)
    assert _run_collect_job(client, engine, config, source["id"])["status"] == "FAILED"
    assert _run_collect_job(client, engine, config, source["id"])["status"] == "FAILED"
    with Session(engine) as session:
        item = session.get(Source, source["id"])
        assert item.consecutive_failures == 2 and item.next_eligible_at > now()
    assert _run_collect_job(client, engine, config, source["id"])["status"] == "SUCCEEDED"
    with Session(engine) as session:
        item = session.get(Source, source["id"])
        assert item.consecutive_failures == 0 and item.next_eligible_at is None and item.last_success_at


def _batch_context(engine, config, *, kind="COLLECT_BATCH", payload=None):
    with Session(engine) as session:
        job = enqueue(session, kind=kind, payload=payload or {})
        session.commit()
        job_id = job.id
    worker = Worker(config, engine=engine)
    claimed = worker.claim()
    assert claimed and claimed.id == job_id
    return TaskContext(engine, config, job_id, worker.owner)


def test_collect_batch_skips_paid_and_backoff_and_survives_failures(engine, config, monkeypatch):
    with Session(engine) as session:
        for source in list(session.scalars(select(Source))):
            session.delete(source)
        session.add_all(
            [
                Source(id="free-ok", name="ok", kind="rss", url="https://a.example/feed"),
                Source(id="free-bad", name="bad", kind="rss", url="https://b.example/feed"),
                Source(id="paid", name="paid", kind="x_tikhub_search", url=SEARCH_URL),
                Source(
                    id="cooling",
                    name="cool",
                    kind="rss",
                    url="https://c.example/feed",
                    next_eligible_at=(datetime.now(UTC) + timedelta(hours=1)).isoformat(),
                ),
            ]
        )
        session.commit()
    visited = []

    def fake(ctx, identifier, _percent):
        visited.append(identifier)
        if identifier == "free-bad":
            raise IntegrationError("HTTP_ERROR", "来源返回 HTTP 500")
        return {"source_id": identifier, "added": 2}

    monkeypatch.setattr("app.workflow.collect", fake)
    ctx = _batch_context(engine, config)
    result = collect_enabled_sources(ctx, include_paid=False, respect_backoff=True)
    assert sorted(visited) == ["free-bad", "free-ok"]
    outcomes = {item["source_id"]: item for item in result}
    assert outcomes["paid"]["skipped"] == "PAID_NOT_INCLUDED"
    assert outcomes["cooling"]["skipped"] == "BACKOFF"
    assert outcomes["free-bad"]["error_code"] == "HTTP_ERROR"


def test_pipeline_continues_after_definitive_paid_failure(engine, config, monkeypatch):
    with Session(engine) as session:
        session.add(Source(id="paid", name="paid", kind="x_tikhub", url="https://x.com/OpenAI"))
        session.commit()

    def fake(ctx, identifier, _percent):
        if identifier == "paid":
            raise AppError("TIKHUB_HTTP_403", "TikHub 返回 HTTP 403", 409)
        return {"source_id": identifier, "added": 0}

    monkeypatch.setattr("app.workflow.collect", fake)
    ctx = _batch_context(engine, config, kind="PIPELINE_BATCH", payload={"daily_limit": 1})
    result = collect_enabled_sources(ctx)
    assert {"source_id": "paid", "error_code": "TIKHUB_HTTP_403"} in result
    assert any(item.get("added") == 0 for item in result)


def test_collect_batch_job_runs_end_to_end(engine, config, monkeypatch):
    monkeypatch.setattr(
        "app.workflow.collect", lambda ctx, identifier, _percent: {"source_id": identifier, "added": 1}
    )
    with Session(engine) as session:
        job = enqueue(session, kind="COLLECT_BATCH", payload={"include_paid": False})
        session.commit()
        job_id = job.id
    assert Worker(config, engine=engine).run_once()
    with Session(engine) as session:
        job = session.get(Job, job_id)
        assert job.status == "SUCCEEDED" and job.result["added"] == 2


# --- schedules ---------------------------------------------------------------------------------------


def _collect_schedule(engine, *, enabled_at="2026-09-30T00:00:00+00:00", interval=60) -> str:
    with Session(engine) as session:
        item = Schedule(
            name="collect",
            kind="collect",
            time="00:00",
            timezone="UTC",
            enabled=True,
            enabled_at=enabled_at,
            interval_minutes=interval,
        )
        session.add(item)
        session.commit()
        return item.id


def test_collect_schedule_runs_on_interval_and_never_stacks(engine):
    identifier = _collect_schedule(engine)
    start = datetime(2026, 9, 30, 0, 5, tzinfo=UTC)
    assert tick_schedules(engine, start) == 1
    assert tick_schedules(engine, start + timedelta(minutes=30)) == 0
    # The first run is still queued, so the next due slot waits instead of stacking.
    assert tick_schedules(engine, start + timedelta(minutes=61)) == 0
    with Session(engine) as session:
        job = session.scalar(select(Job).where(Job.kind == "COLLECT_BATCH"))
        assert job.payload == {
            "schedule_id": identifier,
            "scheduled_at": "2026-09-30T00:05:00+00:00",
            "include_paid": False,
        }
        job.status = "SUCCEEDED"
        session.commit()
    # Missed slots coalesce into one run.
    assert tick_schedules(engine, start + timedelta(hours=5)) == 1
    with Session(engine) as session:
        assert len(list(session.scalars(select(Job).where(Job.kind == "COLLECT_BATCH")))) == 2
        assert session.get(Schedule, identifier).next_run_at == (start + timedelta(hours=6)).isoformat()


def test_collect_schedule_api_validation(client, engine, config):
    base = {"name": "c", "kind": "collect", "time": "00:00", "timezone": "UTC", "enabled": True}
    created = client.post("/api/v1/schedules", json=base)
    assert created.status_code == 201, created.text
    assert created.json()["interval_minutes"] == 180 and created.json()["include_paid"] is False
    paid = client.post("/api/v1/schedules", json={**base, "include_paid": True})
    assert paid.status_code == 409 and paid.json()["code"] == "TIKHUB_NOT_CONFIGURED"
    assert client.post("/api/v1/schedules", json={**base, "interval_minutes": 5}).status_code == 422
    with Session(engine) as session:
        for source in session.scalars(select(Source)):
            source.enabled = False
        session.commit()
    empty = client.post("/api/v1/schedules", json=base)
    assert empty.status_code == 409 and empty.json()["code"] == "SOURCE_REQUIRED"
    daily = client.post("/api/v1/schedules", json={**base, "kind": "metrics", "interval_minutes": 60})
    assert daily.status_code == 201 and daily.json()["interval_minutes"] is None


# --- ranking -----------------------------------------------------------------------------------------


def test_title_clustering_is_conservative():
    assert same_event(
        title_tokens("OpenAI launches GPT-6 Astra for developers"),
        title_tokens("GPT-6 Astra launches for developers today"),
    )
    assert not same_event(title_tokens("Introducing GPT-6"), title_tokens("Introducing GPT-6"))
    assert not same_event(
        title_tokens("Claude Opus 5.5 benchmarks leak"), title_tokens("Gemini 3 benchmarks leak online")
    )
    assert len(title_tokens("谷歌发布新模型")) >= 3


def test_heat_prefers_fresh_confirmed_topics():
    def topic(**values):
        base = {
            "relevance_score": 60,
            "score": 0,
            "cluster_size": 1,
            "published_at": None,
            "created_at": INSTANT.isoformat(),
        }
        return type("T", (), {**base, **values})()

    fresh = topic(published_at=(INSTANT - timedelta(hours=1)).isoformat())
    stale = topic(published_at=(INSTANT - timedelta(days=5)).isoformat())
    confirmed = topic(published_at=(INSTANT - timedelta(days=5)).isoformat(), cluster_size=3)
    assert heat_score(fresh, INSTANT) > heat_score(stale, INSTANT)
    assert heat_score(confirmed, INSTANT) > heat_score(stale, INSTANT)


def test_topics_sort_modes(client, engine):
    with Session(engine) as session:
        source_id = session.scalar(select(Source.id))
        old = (datetime.now(UTC) - timedelta(days=6)).isoformat()
        fresh = datetime.now(UTC).isoformat()
        session.add_all(
            [
                Topic(
                    source_id=source_id,
                    title="Old but relevant",
                    url="https://e.example/1",
                    fingerprint="1" * 64,
                    relevance_score=90,
                    published_at=old,
                ),
                Topic(
                    source_id=source_id,
                    title="Fresh story",
                    url="https://e.example/2",
                    fingerprint="2" * 64,
                    relevance_score=70,
                    published_at=fresh,
                ),
            ]
        )
        session.commit()
    heat = [item["title"] for item in client.get("/api/v1/topics").json()["items"]]
    relevance = [item["title"] for item in client.get("/api/v1/topics?sort=relevance").json()["items"]]
    assert heat == ["Fresh story", "Old but relevant"]
    assert relevance == ["Old but relevant", "Fresh story"]


# --- migration ---------------------------------------------------------------------------------------


def test_0005_defaults_old_rows_and_refuses_unsafe_downgrade(tmp_path):
    from alembic import command
    from sqlalchemy import create_engine, text

    from tests.test_v2_migration import alembic_config

    url = f"sqlite:///{tmp_path / 'old.sqlite3'}"
    config = alembic_config(url)
    command.upgrade(config, "0004")
    engine = create_engine(url)
    stamp = "2026-09-13T00:00:00+00:00"
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO sources (name, kind, url, enabled, id, created_at, updated_at) "
                "VALUES ('old', 'rss', 'https://example.org/feed', 1, 'source-old', :stamp, :stamp)"
            ),
            {"stamp": stamp},
        )
    command.upgrade(config, "0005")
    with engine.connect() as connection:
        values = connection.execute(
            text("SELECT keywords, options, consecutive_failures FROM sources WHERE id = 'source-old'")
        ).one()
    assert tuple(values) == ("[]", "{}", 0)
    command.downgrade(config, "0004")
    command.upgrade(config, "0005")
    with engine.begin() as connection:
        connection.execute(text("UPDATE sources SET kind = 'html_list' WHERE id = 'source-old'"))
    with pytest.raises(RuntimeError):
        command.downgrade(config, "0004")
    engine.dispose()


def test_html_list_ignores_dates_from_shared_containers(monkeypatch):
    page = b"""<html><body><div>
    <time datetime="2026-01-01T00:00:00Z"></time><a href="/blog/first-post-title">First post title here</a>
    <time datetime="2026-02-01T00:00:00Z"></time><a href="/blog/second-post-title">Second post title here</a>
    </div></body></html>"""
    monkeypatch.setattr(sources, "safe_get", lambda url, **_kwargs: (page, "text/html", url))
    items = sources.collect_source({"kind": "html_list", "url": "https://ai.example/blog/"})["items"]
    assert [item["published_at"] for item in items] == [None, None]


def test_versions_must_agree_for_a_merge():
    assert versions_compatible("GPT 6.1 Sol: near-Astra", "GPT-6.1 Sol: near-Astra intelligence")
    assert versions_compatible("Introducing Claude Sonnet", "Claude Sonnet 5.5 arrives")
    assert not versions_compatible("Introducing Gemini 3.8 Flash", "Introducing Gemini 3.6 Flash")


RELEASE_FEED = b"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom"><title>Release notes from codex</title>
<entry><id>tag:github.com,2008:Repository/1/0.159.1</id><updated>2026-09-29T10:00:00Z</updated>
<link rel="alternate" href="https://github.com/openai/codex/releases/tag/0.159.1"/><title>0.159.1</title>
<content type="html">New Features: GPT-6.1 Sol in the model catalog</content></entry>
<entry><id>tag:github.com,2008:Repository/1/v2.1.285</id><updated>2026-09-29T09:00:00Z</updated>
<link rel="alternate" href="https://github.com/openai/codex/releases/tag/0.161.0-alpha.3"/><title>0.161.0-alpha.3</title>
<content type="html">Bug fixes</content></entry>
<entry><id>tag:github.com,2008:Repository/1/named</id><updated>2026-09-29T08:00:00Z</updated>
<link rel="alternate" href="https://github.com/openai/codex/releases/tag/named"/><title>Codex 0.158 brings agents</title>
<content type="html">Agents</content></entry>
</feed>"""


def test_version_only_release_titles_name_the_product(monkeypatch):
    monkeypatch.setattr(
        sources, "safe_get", lambda url, **_kwargs: (RELEASE_FEED, "application/atom+xml", url)
    )
    named = sources.collect_source(
        {
            "kind": "rss",
            "url": "https://github.com/openai/codex/releases.atom",
            "name": "OpenAI Codex Releases",
        }
    )["items"]
    assert [item["title"] for item in named] == [
        "OpenAI Codex 0.159.1",
        "OpenAI Codex 0.161.0-alpha.3",
        "Codex 0.158 brings agents",
    ]
    unnamed = sources.collect_source({"kind": "rss", "url": "https://github.com/openai/codex/releases.atom"})[
        "items"
    ]
    assert unnamed[0]["title"] == "openai/codex 0.159.1"


@pytest.mark.parametrize(
    ("title", "label", "expected"),
    [
        ("v2.1.285", "Claude Code", "Claude Code v2.1.285"),
        ("Release 1.2.0", "Tool", "Tool Release 1.2.0"),
        ("Claude Code v2.1.285", "Claude Code", "Claude Code v2.1.285"),
        ("2026 roadmap", "Tool", "2026 roadmap"),
        ("0.159.1", "", "0.159.1"),
        ("rust-v0.160.0-alpha.5", "OpenAI Codex", "OpenAI Codex rust-v0.160.0-alpha.5"),
        ("GPT-6.1", "OpenAI News", "GPT-6.1"),
    ],
)
def test_contextual_title_only_touches_bare_versions(title, label, expected):
    assert sources.contextual_title(title, label) == expected
    assert sources.release_label("Claude Code Releases", "https://example.org/x") == "Claude Code"
    assert sources.release_label("OpenAI 更新日志", "https://example.org/x") == "OpenAI"


# --- TikHub credential --------------------------------------------------------------------------------


def test_tikhub_key_is_managed_in_keychain_without_echo(client, config, fake_keychain):
    assert client.get("/api/v1/settings/tikhub-key").json() == {
        "key_configured": False,
        "key_source": "none",
        "secure_store_available": True,
    }
    saved = client.post(
        "/api/v1/settings/tikhub-key", json={"action": "set", "api_key": "fixture-keychain-tikhub"}
    )
    assert saved.status_code == 200 and saved.json()["key_source"] == "keychain"
    assert "fixture-keychain-tikhub" not in saved.text
    assert "fixture-keychain-tikhub" not in client.get("/api/v1/settings").text
    assert client.post("/api/v1/settings/tikhub-key", json={"action": "set"}).status_code == 422
    assert (
        client.post("/api/v1/settings/tikhub-key", json={"action": "set", "api_key": "has space"}).status_code
        == 409
    )
    config.tikhub_api_key = SecretStr("fixture-env-tikhub")
    cleared = client.post("/api/v1/settings/tikhub-key", json={"action": "clear"}).json()
    assert cleared == {"key_configured": True, "key_source": "environment", "secure_store_available": True}


def test_keychain_tikhub_key_enables_paid_collection_and_wins_over_environment(
    client, engine, config, fake_keychain, monkeypatch
):
    source = client.post(
        "/api/v1/sources", json={"name": "s", "kind": "x_tikhub_search", "url": SEARCH_URL}
    ).json()
    blocked = client.post(
        f"/api/v1/sources/{source['id']}/collect", json={"expected_kind": "x_tikhub_search"}
    )
    assert blocked.status_code == 409 and blocked.json()["code"] == "TIKHUB_NOT_CONFIGURED"
    config.tikhub_api_key = SecretStr("fixture-env-tikhub")
    client.post("/api/v1/settings/tikhub-key", json={"action": "set", "api_key": "fixture-keychain-tikhub"})
    seen = []

    def handler(request):
        seen.append(request.headers["Authorization"])
        return httpx.Response(200, json={"code": 200, "data": {"timeline": []}})

    _mock_tikhub(monkeypatch, handler)
    job = _run_collect_job(client, engine, config, source["id"], "x_tikhub_search")
    assert job["status"] == "SUCCEEDED", job
    assert seen == ["Bearer fixture-keychain-tikhub"]


def test_locked_keychain_never_falls_back_to_environment_tikhub_key(config, fake_keychain):
    from app.model_credentials import resolve_tikhub_key

    config.tikhub_api_key = SecretStr("fixture-env-tikhub")
    assert resolve_tikhub_key(config) == ("fixture-env-tikhub", "environment")
    fake_keychain.error = IntegrationError("DEVICE_KEYCHAIN_LOCKED", "locked")
    with pytest.raises(AppError) as caught:
        resolve_tikhub_key(config)
    assert caught.value.code == "MODEL_KEY_STORE_LOCKED"
    fake_keychain.error = IntegrationError("DEVICE_KEYCHAIN_UNSUPPORTED", "unsupported")
    assert resolve_tikhub_key(config) == ("fixture-env-tikhub", "environment")


def test_topics_can_be_limited_to_recent_publish_days(client, engine):
    with Session(engine) as session:
        source_id = session.scalar(select(Source.id))
        stamp = lambda hours: (datetime.now(UTC) - timedelta(hours=hours)).isoformat()
        session.add_all(
            [
                Topic(
                    source_id=source_id,
                    title="Fresh",
                    url="https://d.example/1",
                    fingerprint="a" * 64,
                    published_at=stamp(5),
                ),
                Topic(
                    source_id=source_id,
                    title="Edge",
                    url="https://d.example/2",
                    fingerprint="b" * 64,
                    published_at=stamp(47),
                ),
                Topic(
                    source_id=source_id,
                    title="Old",
                    url="https://d.example/3",
                    fingerprint="c" * 64,
                    published_at=stamp(72),
                ),
                Topic(source_id=source_id, title="Undated", url="https://d.example/4", fingerprint="d" * 64),
            ]
        )
        session.commit()
    recent = {item["title"] for item in client.get("/api/v1/topics?days=2").json()["items"]}
    everything = {item["title"] for item in client.get("/api/v1/topics").json()["items"]}
    assert recent == {"Fresh", "Edge"}
    assert everything >= {"Fresh", "Edge", "Old", "Undated"}
    assert {
        item["title"] for item in client.get("/api/v1/topics?days=2&sort=relevance").json()["items"]
    } == recent
    assert client.get("/api/v1/topics?days=0").status_code == 422
    assert client.get("/api/v1/topics?days=31").status_code == 422


def test_pipeline_only_picks_topics_published_in_the_last_two_days(engine, config, monkeypatch):
    import copy as _copy

    from app.models import Content, Setting

    with Session(engine) as session:
        setting = session.get(Setting, "app")
        values = _copy.deepcopy(setting.value)
        for kind in values["models"]:
            values["models"][kind].update(
                enabled=True, base_url="https://fixture.example/v1", model=f"fixture-{kind}"
            )
        values["limits"]["daily_videos"] = 20
        setting.value = values
        source_id = session.scalar(select(Source.id))
        stamp = lambda hours: (datetime.now(UTC) - timedelta(hours=hours)).isoformat()
        session.add_all(
            [
                Topic(
                    source_id=source_id,
                    title="Fresh AI",
                    url="https://p.example/1",
                    fingerprint="1" * 64,
                    relevance_score=60,
                    published_at=stamp(10),
                ),
                Topic(
                    source_id=source_id,
                    title="Three days AI",
                    url="https://p.example/2",
                    fingerprint="2" * 64,
                    relevance_score=90,
                    published_at=stamp(72),
                ),
                Topic(
                    source_id=source_id,
                    title="Undated AI",
                    url="https://p.example/3",
                    fingerprint="3" * 64,
                    relevance_score=90,
                ),
            ]
        )
        job = enqueue(session, kind="PIPELINE_BATCH", payload={"daily_limit": 5})
        job_id = job.id
        session.commit()
    monkeypatch.setattr(
        "app.workflow.collect", lambda ctx, identifier, _percent: {"source_id": identifier, "added": 0}
    )
    assert Worker(config, engine=engine).run_once()
    with Session(engine) as session:
        assert session.get(Job, job_id).status == "SUCCEEDED"
        titles = set(session.scalars(select(Content.title)))
    assert titles == {"Fresh AI"}
