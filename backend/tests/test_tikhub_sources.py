from __future__ import annotations

import copy
import json
from datetime import UTC, datetime

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.integrations import sources
from app.integrations.errors import IntegrationError
from app.models import Job, ModelCall, Setting, Source, now
from app.providers import invoke_tikhub_source
from app.services import AppError, enqueue
from app.worker import Worker


def _configure_tikhub(config) -> None:
    config.tikhub_api_key = SecretStr("fixture-tikhub-key")


def _paid_source(client, name: str = "OpenAI") -> dict:
    response = client.post(
        "/api/v1/sources",
        json={"name": name, "kind": "x_tikhub", "url": f"https://x.com/{name}"},
    )
    assert response.status_code == 201, response.text
    return response.json()


def _queue_paid(client, engine, config, monkeypatch, *, key: str = "paid-source"):
    _configure_tikhub(config)
    source = _paid_source(client)
    response = client.post(
        f"/api/v1/sources/{source['id']}/collect",
        json={"expected_kind": "x_tikhub"},
        headers={"Idempotency-Key": key},
    )
    assert response.status_code == 202, response.text
    worker = Worker(config, engine=engine)
    job = worker.claim()
    assert job and job.id == response.json()["job_id"]
    return source, job, worker


def _mock_http(monkeypatch, handler) -> None:
    original = httpx.Client
    monkeypatch.setattr(
        "app.providers.httpx.Client",
        lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs),
    )


def _graphql_user(author: str) -> dict:
    return {"user_results": {"result": {"legacy": {"screen_name": author}}}}


def _graphql_tweet(
    identifier: str,
    text: str,
    created_at: str,
    author: str,
    *,
    note: str | None = None,
    retweet: dict | None = None,
    quote: dict | None = None,
) -> dict:
    legacy = {"full_text": text, "created_at": created_at, "favorite_count": 10}
    if retweet is not None:
        legacy["retweeted_status_result"] = {"result": retweet}
    if quote is not None:
        legacy["quoted_status_result"] = {"result": quote}
    result = {"rest_id": identifier, "legacy": legacy, "core": _graphql_user(author)}
    if note is not None:
        result["note_tweet"] = {"note_tweet_results": {"result": {"text": note}}}
    return result


def _graphql_data(results: list[dict], *, include_metadata: bool = True) -> dict:
    entries = [
        {
            "entryId": f"tweet-{index}",
            "content": {"itemContent": {"tweet_results": {"result": result}}},
        }
        for index, result in enumerate(results)
    ]
    entries.append({"entryId": "cursor-bottom", "content": {"entryType": "TimelineTimelineCursor"}})
    data = {"user": {"result": {"timeline_v2": {"timeline": {"instructions": [{"entries": entries}]}}}}}
    if include_metadata:
        data["metadata"] = {
            "tweet_results": {
                "result": _graphql_tweet(
                    "999",
                    "AI metadata must never become a post",
                    "Thu Sep 10 18:00:00 +0000 2026",
                    "MetadataUser",
                )
            }
        }
    return data


def test_source_conversion_is_limited_to_x_providers_and_urls_remain_canonical(client):
    public = client.post(
        "/api/v1/sources",
        json={"name": "OpenAI public", "kind": "x", "url": "https://twitter.com/OpenAI/"},
    ).json()
    converted = client.patch(
        f"/api/v1/sources/{public['id']}",
        json={"kind": "x_tikhub"},
    )
    assert converted.status_code == 200, converted.text
    assert converted.json()["kind"] == "x_tikhub"
    assert converted.json()["url"] == "https://x.com/OpenAI"

    restored = client.patch(
        f"/api/v1/sources/{public['id']}",
        json={"kind": "x", "url": "https://www.x.com/AnthropicAI/"},
    )
    assert restored.status_code == 200, restored.text
    assert (restored.json()["kind"], restored.json()["url"]) == (
        "x",
        "https://x.com/AnthropicAI",
    )

    rss = next(item for item in client.get("/api/v1/sources").json()["items"] if item["kind"] == "rss")
    forbidden = client.patch(f"/api/v1/sources/{rss['id']}", json={"kind": "x_tikhub"})
    assert forbidden.status_code == 409
    assert forbidden.json()["code"] == "SOURCE_KIND_CHANGE_FORBIDDEN"


def test_paid_collect_requires_explicit_provider_key_and_freezes_nonsecret_snapshot(client, engine, config):
    source = _paid_source(client)
    stale = client.post(f"/api/v1/sources/{source['id']}/collect")
    assert stale.status_code == 409 and stale.json()["code"] == "SOURCE_PROVIDER_CHANGED"
    missing_key = client.post(
        f"/api/v1/sources/{source['id']}/collect",
        json={"expected_kind": "x_tikhub"},
    )
    assert missing_key.status_code == 409 and missing_key.json()["code"] == "TIKHUB_NOT_CONFIGURED"

    _configure_tikhub(config)
    headers = {"Idempotency-Key": "paid-binding"}
    first = client.post(
        f"/api/v1/sources/{source['id']}/collect",
        json={"expected_kind": "x_tikhub"},
        headers=headers,
    )
    replayed = client.post(
        f"/api/v1/sources/{source['id']}/collect",
        json={"expected_kind": "x_tikhub"},
        headers=headers,
    )
    assert first.status_code == 202 and replayed.json() == first.json()
    with Session(engine) as session:
        payload = session.get(Job, first.json()["job_id"]).payload
        assert payload == {
            "source_id": source["id"],
            "source_snapshot": {
                "id": source["id"],
                "kind": "x_tikhub",
                "url": "https://x.com/OpenAI",
            },
            "expected_kind": "x_tikhub",
        }
        assert "fixture-tikhub-key" not in json.dumps(payload)

    other = _paid_source(client, "AnthropicAI")
    conflict = client.post(
        f"/api/v1/sources/{other['id']}/collect",
        json={"expected_kind": "x_tikhub"},
        headers=headers,
    )
    assert conflict.status_code == 409 and conflict.json()["code"] == "IDEMPOTENCY_CONFLICT"


def test_free_source_collect_remains_body_optional(client):
    free = next(item for item in client.get("/api/v1/sources").json()["items"] if item["kind"] == "rss")
    response = client.post(f"/api/v1/sources/{free['id']}/collect")
    assert response.status_code == 202, response.text


def test_paid_collect_checks_shared_daily_call_budget_before_queue(client, engine, config):
    _configure_tikhub(config)
    source = _paid_source(client)
    with Session(engine) as session:
        setting = session.get(Setting, "app")
        values = copy.deepcopy(setting.value)
        values["limits"]["daily_calls"] = 1
        setting.value = values
        usage_job = enqueue(session, kind="GENERATE_REPORT", payload={})
        session.add(
            ModelCall(
                job_id=usage_job.id,
                request_key="f" * 64,
                kind="text",
                model="fixture-text",
                status="SUCCEEDED",
                events=[{"started_at": now(), "status": "SUCCEEDED", "usage": {}}],
            )
        )
        session.commit()
    response = client.post(
        f"/api/v1/sources/{source['id']}/collect",
        json={"expected_kind": "x_tikhub"},
    )
    assert response.status_code == 409 and response.json()["code"] == "DAILY_CALL_LIMIT"
    with Session(engine) as session:
        assert session.scalar(select(Job.id).where(Job.kind == "COLLECT_SOURCE")) is None


def test_tikhub_provider_persists_intent_and_reuses_success_without_second_get(
    client, engine, config, monkeypatch
):
    source, job, worker = _queue_paid(client, engine, config, monkeypatch)
    requests = []
    response_data = {"tweets": []}

    def handler(request):
        requests.append(request)
        assert request.method == "GET"
        assert request.url.host == "api.tikhub.io"
        assert dict(request.url.params) == {"screen_name": "OpenAI"}
        assert request.headers["Authorization"] == "Bearer fixture-tikhub-key"
        return httpx.Response(200, json={"code": 200, "data": response_data, "request_id": "req-1"})

    _mock_http(monkeypatch, handler)
    first = invoke_tikhub_source(
        engine,
        config,
        job_id=job.id,
        owner=worker.owner,
        source_id=source["id"],
        screen_name="OpenAI",
    )
    with pytest.raises(AppError) as drifted:
        invoke_tikhub_source(
            engine,
            config,
            job_id=job.id,
            owner=worker.owner,
            source_id=source["id"],
            screen_name="AnthropicAI",
        )
    assert drifted.value.code == "JOB_INPUT_CHANGED"
    assert len(requests) == 1
    config.tikhub_api_key = SecretStr("")
    second = invoke_tikhub_source(
        engine,
        config,
        job_id=job.id,
        owner=worker.owner,
        source_id=source["id"],
        screen_name="OpenAI",
    )
    assert first == second == {"data": response_data, "request_id": "req-1"}
    assert len(requests) == 1
    with Session(engine) as session:
        call = session.scalar(select(ModelCall))
        persisted_job = session.get(Job, job.id)
        assert (call.kind, call.model, call.status, call.attempts) == (
            "source",
            "tikhub:twitter-user-posts",
            "SUCCEEDED",
            1,
        )
        assert call.response == first and call.usage == {}
        assert "source_requests" in persisted_job.payload
        persisted = json.dumps({"call": call.response, "job": persisted_job.payload})
        assert "fixture-tikhub-key" not in persisted
        persisted_job.status = "SUCCEEDED"
        persisted_job.lease_owner = persisted_job.lease_until = None
        session.commit()

    _configure_tikhub(config)
    fresh = client.post(
        f"/api/v1/sources/{source['id']}/collect",
        json={"expected_kind": "x_tikhub"},
    )
    fresh_worker = Worker(config, engine=engine)
    fresh_job = fresh_worker.claim()
    assert fresh.status_code == 202 and fresh_job.id == fresh.json()["job_id"]
    invoke_tikhub_source(
        engine,
        config,
        job_id=fresh_job.id,
        owner=fresh_worker.owner,
        source_id=source["id"],
        screen_name="OpenAI",
    )
    assert len(requests) == 2
    with Session(engine) as session:
        assert len(list(session.scalars(select(ModelCall)))) == 2


@pytest.mark.parametrize(
    "status,business_code,expected",
    [
        (401, None, "TIKHUB_HTTP_401"),
        (403, None, "TIKHUB_HTTP_403"),
        (429, None, "TIKHUB_HTTP_429"),
        (200, 429, "TIKHUB_BUSINESS_REJECTED"),
    ],
)
def test_tikhub_definitive_rejections_are_failed_without_error_body(
    client, engine, config, monkeypatch, status, business_code, expected
):
    source, job, worker = _queue_paid(client, engine, config, monkeypatch, key=f"reject-{status}")

    def handler(_request):
        payload = (
            {"code": business_code, "message": "secret upstream body", "data": None}
            if status == 200
            else {"message": "secret upstream body"}
        )
        return httpx.Response(status, json=payload)

    _mock_http(monkeypatch, handler)
    with pytest.raises(AppError) as caught:
        invoke_tikhub_source(
            engine,
            config,
            job_id=job.id,
            owner=worker.owner,
            source_id=source["id"],
            screen_name="OpenAI",
        )
    assert caught.value.code == expected
    with Session(engine) as session:
        call = session.scalar(select(ModelCall))
        persisted_job = session.get(Job, job.id)
        assert call.status == "FAILED"
        assert "secret upstream body" not in json.dumps(
            {"response": call.response, "events": call.events, "error": persisted_job.error_message}
        )


@pytest.mark.parametrize(
    "upstream,reason",
    [
        ("Insufficient balance for API key fixture-tikhub-key", "insufficient_balance"),
        ("Current IP is not in the whitelist, API key fixture-tikhub-key", "ip_restricted"),
        ("The subscription endpoint whitelist rejects fixture-tikhub-key", "unspecified"),
    ],
)
def test_tikhub_rejection_reason_is_classified_without_echoing_credentials(
    client, engine, config, monkeypatch, upstream, reason
):
    source, job, worker = _queue_paid(client, engine, config, monkeypatch, key="safe-rejection")
    _mock_http(monkeypatch, lambda _request: httpx.Response(403, json={"message": upstream}))
    with pytest.raises(AppError):
        invoke_tikhub_source(
            engine,
            config,
            job_id=job.id,
            owner=worker.owner,
            source_id=source["id"],
            screen_name="OpenAI",
        )
    with Session(engine) as session:
        call = session.scalar(select(ModelCall))
        persisted_job = session.get(Job, job.id)
        assert call.response == {"http_status": 403, "reason": reason}
        assert "fixture-tikhub-key" not in json.dumps(
            {"response": call.response, "message": persisted_job.error_message}
        )


def test_tikhub_unknown_result_is_durable_and_never_sent_twice(client, engine, config, monkeypatch):
    source, job, worker = _queue_paid(client, engine, config, monkeypatch)
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(503, json={"message": "unknown"})

    _mock_http(monkeypatch, handler)
    for _ in range(2):
        with pytest.raises(AppError) as caught:
            invoke_tikhub_source(
                engine,
                config,
                job_id=job.id,
                owner=worker.owner,
                source_id=source["id"],
                screen_name="OpenAI",
            )
        assert caught.value.code == "MODEL_OUTCOME_UNKNOWN"
    assert len(requests) == 1
    with Session(engine) as session:
        call = session.scalar(select(ModelCall))
        assert call.status == "UNKNOWN" and call.attempts == 1


def test_tikhub_connect_failure_is_definitive_before_any_http_response(client, engine, config, monkeypatch):
    source, job, worker = _queue_paid(client, engine, config, monkeypatch)

    def handler(request):
        raise httpx.ConnectError("fixture TLS connect failed", request=request)

    _mock_http(monkeypatch, handler)
    with pytest.raises(AppError) as caught:
        invoke_tikhub_source(
            engine,
            config,
            job_id=job.id,
            owner=worker.owner,
            source_id=source["id"],
            screen_name="OpenAI",
        )
    assert caught.value.code == "TIKHUB_CONNECT_FAILED"
    with Session(engine) as session:
        assert session.scalar(select(ModelCall)).status == "FAILED"


def test_cached_paid_response_can_be_reparsed_after_retry_without_network(
    client, engine, config, monkeypatch
):
    _configure_tikhub(config)
    source = _paid_source(client)
    queued = client.post(
        f"/api/v1/sources/{source['id']}/collect",
        json={"expected_kind": "x_tikhub"},
    )
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "code": 200,
                "data": {"metadata": {"tweet_results": {"result": {"rest_id": "123"}}}},
            },
        )

    _mock_http(monkeypatch, handler)
    worker = Worker(config, engine=engine)
    assert worker.run_once()
    first = client.get(f"/api/v1/jobs/{queued.json()['job_id']}").json()
    assert first["status"] == "FAILED" and first["error_code"] == "TIKHUB_RESPONSE_INVALID"
    with Session(engine) as session:
        assert session.scalar(select(ModelCall)).status == "SUCCEEDED"
    retried = client.post(f"/api/v1/jobs/{queued.json()['job_id']}/retry")
    assert retried.status_code == 202, retried.text
    assert worker.run_once()
    second = client.get(f"/api/v1/jobs/{queued.json()['job_id']}").json()
    assert second["status"] == "FAILED" and second["error_code"] == "TIKHUB_RESPONSE_INVALID"
    assert len(requests) == 1


def test_tikhub_graphql_parser_handles_visibility_note_retweet_quote_age_and_metadata():
    quote = _graphql_tweet(
        "201",
        "Claude quoted context",
        "Thu Sep 10 08:00:00 +0000 2026",
        "AnthropicAI",
    )
    normal = _graphql_tweet(
        "101",
        "truncated",
        "Thu Sep 10 12:00:00 +0000 2026",
        "OpenAI",
        note="LLM note preserves literal <think> tokens",
        quote=quote,
    )
    original = _graphql_tweet(
        "301",
        "DeepSeek publishes a full inference guide",
        "Wed Sep 09 12:00:00 +0000 2026",
        "deepseek_ai",
    )
    retweet = _graphql_tweet(
        "102",
        "RT truncated…",
        "Thu Sep 10 13:00:00 +0000 2026",
        "OpenAI",
        retweet=original,
    )
    old = _graphql_tweet(
        "401",
        "Gemini old pinned post",
        "Wed Jul 01 12:00:00 +0000 2026",
        "GoogleDeepMind",
    )
    placeholder = _graphql_tweet(
        "0",
        "Qwen placeholder",
        "Thu Sep 10 14:00:00 +0000 2026",
        "OpenAI",
    )
    data = _graphql_data([{"tweet": normal}, retweet, old, placeholder])
    result = sources._collect_x_tikhub(
        data,
        30,
        instant=datetime(2026, 9, 11, tzinfo=UTC),
    )
    assert result["complete"] is False
    assert "当前一页" in result["warning"] and "完整 30 天历史" in result["warning"]
    assert [item["url"] for item in result["items"]] == [
        "https://x.com/OpenAI/status/101",
        "https://x.com/deepseek_ai/status/301",
    ]
    assert "<think>" in result["items"][0]["summary"]
    assert "https://x.com/AnthropicAI/status/201" in result["items"][0]["summary"]
    assert "RT truncated" not in result["items"][1]["summary"]
    assert all("/status/999" not in item["url"] for item in result["items"])


def test_tikhub_parser_accepts_legitimate_empty_page_and_rejects_unrelated_metadata():
    empty = sources.collect_source(
        {"kind": "x_tikhub", "url": "https://x.com/OpenAI"},
        tikhub_data={"tweets": []},
    )
    assert empty["items"] == [] and empty["complete"] is False
    legacy = sources._collect_x_tikhub(
        {
            "tweets": [
                {
                    "id_str": "123",
                    "full_text": "Qwen releases a model update",
                    "created_at": "Thu Sep 10 12:00:00 +0000 2026",
                    "favorite_count": 2,
                    "user": {"screen_name": "QwenLM"},
                }
            ]
        },
        30,
        instant=datetime(2026, 9, 11, tzinfo=UTC),
    )
    assert [item["url"] for item in legacy["items"]] == ["https://x.com/QwenLM/status/123"]
    with pytest.raises(IntegrationError) as caught:
        sources.collect_source(
            {"kind": "x_tikhub", "url": "https://x.com/OpenAI"},
            tikhub_data={
                "metadata": {
                    "tweet_results": {
                        "result": _graphql_tweet(
                            "777",
                            "AI metadata is not a timeline",
                            "Thu Sep 10 12:00:00 +0000 2026",
                            "MetadataUser",
                        )
                    }
                }
            },
        )
    assert caught.value.code == "TIKHUB_RESPONSE_INVALID"


def test_legacy_job_cannot_turn_current_paid_source_into_an_unconfirmed_paid_read(
    client, engine, config, monkeypatch
):
    source = _paid_source(client)
    with Session(engine) as session:
        job = enqueue(session, kind="COLLECT_SOURCE", payload={"source_id": source["id"]})
        session.commit()
        job_id = job.id
    monkeypatch.setattr(
        "app.workflow.invoke_tikhub_source",
        lambda *_args, **_kwargs: pytest.fail("legacy job must not call TikHub"),
    )
    assert Worker(config, engine=engine).run_once()
    result = client.get(f"/api/v1/jobs/{job_id}").json()
    assert result["status"] == "FAILED"
    assert result["error_code"] == "SOURCE_PROVIDER_CHANGED"
    with Session(engine) as session:
        assert session.scalar(select(ModelCall)) is None


def test_pipeline_freezes_enabled_source_inputs_before_collection(engine, config, monkeypatch):
    with Session(engine) as session:
        setting = session.get(Setting, "app")
        values = copy.deepcopy(setting.value)
        for kind in values["models"]:
            values["models"][kind].update(
                enabled=True,
                base_url="https://fixture.example/v1",
                model=f"fixture-{kind}",
            )
        values["limits"]["daily_videos"] = 20
        setting.value = values
        source = Source(name="Pipeline X", kind="x_tikhub", url="https://x.com/OpenAI")
        session.add(source)
        session.flush()
        job = enqueue(session, kind="PIPELINE_BATCH", payload={"daily_limit": 1})
        source_id, job_id = source.id, job.id
        session.commit()
    observed = []

    def collected(ctx, identifier, _percent):
        observed.append(copy.deepcopy(ctx.payload["source_inputs"][identifier]))
        return {"source_id": identifier, "collected": 0, "added": 0}

    monkeypatch.setattr("app.workflow.collect", collected)
    worker = Worker(config, engine=engine)
    assert worker.run_once()
    with Session(engine) as session:
        snapshot = session.get(Job, job_id).payload["source_inputs"][source_id]
    assert (
        next(item for item in observed if item["id"] == source_id)
        == snapshot
        == {
            "id": source_id,
            "kind": "x_tikhub",
            "url": "https://x.com/OpenAI",
        }
    )


def test_pipeline_does_not_swallow_paid_source_unknown_outcome(engine, config, monkeypatch):
    with Session(engine) as session:
        source = Source(name="Paid pipeline", kind="x_tikhub", url="https://x.com/OpenAI")
        session.add(source)
        session.flush()
        job = enqueue(session, kind="PIPELINE_BATCH", payload={"daily_limit": 1})
        job_id = job.id
        session.commit()

    def collect_or_fail(ctx, identifier, _percent):
        if ctx.payload["source_inputs"][identifier]["kind"] == "x_tikhub":
            raise AppError("MODEL_OUTCOME_UNKNOWN", "paid read outcome unknown")
        return {"source_id": identifier, "collected": 0, "added": 0}

    monkeypatch.setattr("app.workflow.collect", collect_or_fail)
    assert Worker(config, engine=engine).run_once()
    with Session(engine) as session:
        job = session.get(Job, job_id)
        assert job.status == "WAITING_USER"
        assert job.error_code == "MODEL_OUTCOME_UNKNOWN"
