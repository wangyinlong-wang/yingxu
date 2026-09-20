from __future__ import annotations

import copy
import io
import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from PIL import Image
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Job, ModelCall, Setting
from app.providers import invoke_model
from app.services import AppError, enqueue, usage_summary


def configured(engine, daily_calls=10):
    with Session(engine) as session:
        record = session.get(Setting, "app")
        values = copy.deepcopy(record.value)
        for kind, model in values["models"].items():
            model.update(
                {"base_url": "https://model.example.test/v1", "model": f"fixture-{kind}", "enabled": True}
            )
        values["limits"]["daily_calls"] = daily_calls
        record.value = values
        job = enqueue(session, kind="GENERATE_REPORT", payload={})
        job.status = "RUNNING"
        job.lease_owner = "fixture-owner"
        job.lease_until = (datetime.now(UTC) + timedelta(minutes=5)).isoformat()
        session.commit()
        return job.id


def mock_provider(monkeypatch, handler):
    original = httpx.Client
    monkeypatch.setattr(
        "app.providers.httpx.Client",
        lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs),
    )


def completion(text="fixture response"):
    return {"choices": [{"message": {"content": text}}], "usage": {"total_tokens": 12}}


def test_success_is_cached_and_usage_counted_once(engine, config, monkeypatch):
    job_id = configured(engine)
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=completion())

    mock_provider(monkeypatch, handler)
    args = {
        "job_id": job_id,
        "owner": "fixture-owner",
        "kind": "text",
        "payload": {"messages": [{"role": "user", "content": "fixture"}]},
        "scope": "same-input",
    }
    first = invoke_model(engine, config, **args)
    assert invoke_model(engine, config, **args) == first
    assert len(requests) == 1
    assert requests[0].url.path == "/v1/chat/completions"
    with Session(engine) as session:
        record = session.scalar(select(ModelCall))
        assert record.status == "SUCCEEDED"
        assert usage_summary(session, config.timezone) == {"calls_today": 1, "tokens_today": 12, "cost": None}


def test_ark_watermark_false_replaces_reserved_bottom_band_without_resizing(
    engine, config, monkeypatch
):
    job_id = configured(engine)
    with Session(engine) as session:
        record = session.get(Setting, "app")
        values = copy.deepcopy(record.value)
        values["models"]["image"].update(
            {"protocol": "ark", "model": "fixture-seedream", "watermark": False, "size": "2K"}
        )
        record.value = values
        session.commit()
    picture = Image.new("RGB", (100, 200), (15, 25, 45))
    for y in range(152, 176):
        for x in range(100):
            picture.putpixel((x, y), (20, 80, 180))
    for y in range(176, 200):
        for x in range(100):
            picture.putpixel((x, y), (240, 240, 240))
    encoded = io.BytesIO()
    picture.save(encoded, "PNG")

    def handler(request):
        body = json.loads(request.content)
        assert body["watermark"] is False and body["size"] == "2K"
        return httpx.Response(200, json={"data": [{"url": "https://image.example.test/output.png"}]})

    mock_provider(monkeypatch, handler)
    monkeypatch.setattr(
        "app.integrations.net.safe_get",
        lambda *_args, **_kwargs: (encoded.getvalue(), "image/png", "https://image.example.test/output.png"),
    )
    result = invoke_model(
        engine,
        config,
        job_id=job_id,
        owner="fixture-owner",
        kind="image",
        payload={"prompt": "竖屏安全区测试"},
        scope="ark-safe-area",
    )
    assert result["image_processing"] == {
        "ark_clean_bottom_safe_area": True,
        "bottom_fraction": 0.12,
    }
    with Session(engine) as session:
        from app.models import Asset
        from app.services import asset_path

        asset = session.get(Asset, result["asset_id"])
        with Image.open(asset_path(config, asset)) as output:
            assert output.size == (100, 200)
            red, green, blue = output.getpixel((50, 199))
            assert blue > red * 2 and blue > green


def test_timeout_remains_unknown_and_is_never_reposted(engine, config, monkeypatch):
    job_id = configured(engine)
    requests = []

    def handler(request):
        requests.append(request)
        raise httpx.ReadTimeout("fixture timeout", request=request)

    mock_provider(monkeypatch, handler)
    args = {
        "job_id": job_id,
        "owner": "fixture-owner",
        "kind": "text",
        "payload": {"messages": [{"role": "user", "content": "fixture"}]},
        "scope": "unknown-request",
    }
    for _ in range(2):
        with pytest.raises(AppError) as failure:
            invoke_model(engine, config, **args)
        assert failure.value.code == "MODEL_OUTCOME_UNKNOWN"
    assert len(requests) == 1
    with Session(engine) as session:
        record = session.scalar(select(ModelCall))
        assert record.status == "UNKNOWN"
        assert len(record.events) == 1


@pytest.mark.parametrize(
    "status,body", [(500, {"error": "fixture server error"}), (200, {"wrong": "response"})]
)
def test_uncertain_server_or_response_outcome_is_held(engine, config, monkeypatch, status, body):
    job_id = configured(engine)
    mock_provider(monkeypatch, lambda request: httpx.Response(status, json=body))
    with pytest.raises(AppError) as failure:
        invoke_model(
            engine,
            config,
            job_id=job_id,
            owner="fixture-owner",
            kind="text",
            payload={"messages": [{"role": "user", "content": "fixture"}]},
            scope="bad-response",
        )
    assert failure.value.code == "MODEL_OUTCOME_UNKNOWN"
    with Session(engine) as session:
        assert session.scalar(select(ModelCall)).status == "UNKNOWN"


def test_daily_limit_blocks_before_network(engine, config, monkeypatch):
    job_id = configured(engine, daily_calls=1)
    count = 0

    def handler(request):
        nonlocal count
        count += 1
        return httpx.Response(200, json=completion())

    mock_provider(monkeypatch, handler)
    invoke_model(
        engine,
        config,
        job_id=job_id,
        owner="fixture-owner",
        kind="text",
        payload={"messages": [{"role": "user", "content": "fixture"}]},
        scope="first",
    )
    with pytest.raises(AppError) as failure:
        invoke_model(
            engine,
            config,
            job_id=job_id,
            owner="fixture-owner",
            kind="text",
            payload={"messages": [{"role": "user", "content": "fixture"}]},
            scope="second",
        )
    assert failure.value.code == "DAILY_CALL_LIMIT"
    assert count == 1


def test_definitive_rejection_can_be_explicitly_retried_and_attempts_keep_dates(engine, config, monkeypatch):
    job_id = configured(engine)
    results = iter([401, 200])
    mock_provider(monkeypatch, lambda request: httpx.Response(next(results), json=completion()))
    args = {
        "job_id": job_id,
        "owner": "fixture-owner",
        "kind": "text",
        "payload": {"messages": [{"role": "user", "content": "fixture"}]},
        "scope": "rejected-request",
    }
    with pytest.raises(AppError) as failure:
        invoke_model(engine, config, **args)
    assert failure.value.code == "MODEL_HTTP_401"
    with Session(engine) as session:
        record = session.scalar(select(ModelCall))
        events = copy.deepcopy(record.events)
        events[0]["started_at"] = (datetime.now(UTC) - timedelta(days=2)).isoformat()
        record.events = events
        session.commit()
    invoke_model(engine, config, **args)
    with Session(engine) as session:
        record = session.scalar(select(ModelCall))
        assert record.attempts == 2 and len(record.events) == 2
        assert usage_summary(session, config.timezone)["calls_today"] == 1


def test_human_reconciliation_required_before_unknown_retry(client, engine, config, monkeypatch):
    job_id = configured(engine)
    mock_provider(monkeypatch, lambda request: (_ for _ in ()).throw(httpx.ReadTimeout("fixture timeout")))
    with pytest.raises(AppError):
        invoke_model(
            engine,
            config,
            job_id=job_id,
            owner="fixture-owner",
            kind="text",
            payload={"messages": [{"role": "user", "content": "fixture"}]},
            scope="reconcile",
        )
    with Session(engine) as session:
        session.get(Job, job_id).status = "WAITING_USER"
        session.commit()
    assert client.post(f"/api/v1/jobs/{job_id}/retry").json()["code"] == "MODEL_OUTCOME_UNKNOWN"
    assert (
        client.post(
            f"/api/v1/jobs/{job_id}/resolve",
            json={"resolution": "not_completed", "confirmed": False, "notes": "test"},
        ).status_code
        == 422
    )
    response = client.post(
        f"/api/v1/jobs/{job_id}/resolve",
        json={"resolution": "not_completed", "confirmed": True, "notes": "测试：已核实服务商未完成该请求"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "FAILED"
    assert client.post(f"/api/v1/jobs/{job_id}/retry").status_code == 202


def test_canceled_intent_never_reaches_transport(engine, config, monkeypatch):
    job_id = configured(engine)
    original = httpx.Client
    requests = []

    def client_factory(**kwargs):
        with Session(engine) as session:
            session.get(Job, job_id).cancel_requested = True
            session.commit()
        return original(
            transport=httpx.MockTransport(
                lambda request: requests.append(request) or httpx.Response(200, json=completion())
            ),
            **kwargs,
        )

    monkeypatch.setattr("app.providers.httpx.Client", client_factory)
    with pytest.raises(AppError) as failure:
        invoke_model(
            engine,
            config,
            job_id=job_id,
            owner="fixture-owner",
            kind="text",
            payload={"messages": [{"role": "user", "content": "fixture"}]},
            scope="cancel-before-send",
        )
    assert failure.value.code == "TASK_CANCELED"
    assert requests == []
    with Session(engine) as session:
        assert session.scalar(select(ModelCall)).events[-1]["status"] == "CANCELED_BEFORE_SEND"
        assert usage_summary(session, config.timezone)["calls_today"] == 0


def test_wrong_lease_cannot_create_paid_intent(engine, config, monkeypatch):
    job_id = configured(engine)
    mock_provider(monkeypatch, lambda request: (_ for _ in ()).throw(AssertionError("must not POST")))
    with pytest.raises(AppError) as failure:
        invoke_model(
            engine,
            config,
            job_id=job_id,
            owner="old-owner",
            kind="text",
            payload={"messages": [{"role": "user", "content": "fixture"}]},
            scope="wrong-lease",
        )
    assert failure.value.code == "TASK_LEASE_LOST"
    with Session(engine) as session:
        assert session.scalar(select(ModelCall)) is None


def test_late_response_cannot_overwrite_recovered_unknown(engine, config, monkeypatch):
    from app.worker import recover_expired

    job_id = configured(engine)

    def late_response(request):
        with Session(engine) as session:
            job = session.get(Job, job_id)
            job.lease_until = (datetime.now(UTC) - timedelta(seconds=60)).isoformat()
            session.commit()
        with Session(engine) as session:
            recover_expired(session, "content", datetime.now(UTC))
            session.commit()
        return httpx.Response(200, json=completion())

    mock_provider(monkeypatch, late_response)
    with pytest.raises(AppError) as failure:
        invoke_model(
            engine,
            config,
            job_id=job_id,
            owner="fixture-owner",
            kind="text",
            payload={"messages": [{"role": "user", "content": "fixture"}]},
            scope="late-response",
        )
    assert failure.value.code == "TASK_LEASE_LOST"
    with Session(engine) as session:
        assert session.scalar(select(ModelCall)).status == "UNKNOWN"
        assert session.get(Job, job_id).status == "WAITING_USER"


def test_restart_after_http_rejection_requires_explicit_retry(engine, config, monkeypatch):
    from app.worker import Worker

    job_id = configured(engine)
    calls = []
    mock_provider(
        monkeypatch, lambda request: calls.append(request) or httpx.Response(401, json={"error": "fixture"})
    )
    with pytest.raises(AppError):
        invoke_model(
            engine,
            config,
            job_id=job_id,
            owner="fixture-owner",
            kind="text",
            payload={"messages": [{"role": "user", "content": "fixture"}]},
            scope="reject-then-crash",
        )
    with Session(engine) as session:
        session.get(Job, job_id).lease_until = (datetime.now(UTC) - timedelta(minutes=2)).isoformat()
        session.commit()
    assert Worker(config, engine=engine).claim() is None
    with Session(engine) as session:
        assert session.get(Job, job_id).status == "FAILED"
    assert len(calls) == 1
