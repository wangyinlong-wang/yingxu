from __future__ import annotations

import copy
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Asset, Content, Job, ModelCall, Setting
from app.services import AppError, usage_summary
from app.video_provider import invoke_video
from app.worker import recover_expired


@pytest.fixture
def video_job(engine, content_id):
    with Session(engine) as session:
        setting = session.get(Setting, "app")
        values = copy.deepcopy(setting.value)
        values["models"]["video"].update(
            enabled=True,
            model="MiniMax-H3",
            base_url="https://video.example.test/api/minimax",
            timeout_seconds=20,
        )
        setting.value = values
        content = session.get(Content, content_id)
        job = Job(
            kind="GENERATE_CONTENT",
            content_id=content_id,
            status="RUNNING",
            queue="content",
            idempotency_key="video-test",
            request_hash="fixture",
            lease_owner="video-owner",
            lease_until=(datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
            payload={"stage": "video", "version": content.version},
        )
        session.add(job)
        session.commit()
        return job.id


def install_transport(monkeypatch, handler):
    original = httpx.Client
    monkeypatch.setattr(
        "app.video_provider.httpx.Client",
        lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs),
    )
    monkeypatch.setattr("app.video_provider.time.sleep", lambda _: None)
    monkeypatch.setattr(
        "app.video_provider.safe_get",
        lambda *_, **__: (b"fixture-mp4", "video/mp4", "https://cdn.example.test/video.mp4"),
    )
    monkeypatch.setattr("app.video_provider.probe_media", lambda _: {"has_video": True, "has_audio": True, "duration": 6.0})


def call(engine, config, video_job, content_id):
    return invoke_video(
        engine,
        config,
        job_id=video_job,
        owner="video-owner",
        content_id=content_id,
        scope=f"content:{content_id}:scene:1:video",
        payload={"prompt": "A small test robot", "duration": 6, "resolution": "768P", "ratio": "9:16"},
    )


def complete(request):
    if request.method == "POST":
        return httpx.Response(200, json={"task_id": "fixture_task_1"})
    return httpx.Response(
        200,
        json={
            "task": {
                "status": "succeeded",
                "content": {"url": "https://cdn.example.test/video.mp4?private=signature"},
            }
        },
    )


def test_create_poll_cache_and_budget_count_once(engine, config, content_id, video_job, monkeypatch):
    observed = []

    def respond(request):
        observed.append(request)
        return complete(request)

    install_transport(monkeypatch, respond)
    first = call(engine, config, video_job, content_id)
    assert call(engine, config, video_job, content_id) == first
    assert [req.method for req in observed] == ["POST", "GET"]
    assert observed[0].url.path == "/api/minimax/v2/video_generation"
    assert observed[1].url.path == "/api/minimax/v2/query/video_generation/fixture_task_1"
    with Session(engine) as session:
        record = session.scalar(select(ModelCall))
        assert record.status == "SUCCEEDED"
        assert record.response == first
        assert "signature" not in str(record.response)
        assert "fixture-not-a-real-api-key" not in str(record.events)
        assert session.get(Asset, first["asset_id"]).kind == "video_clip"
        assert usage_summary(session, config.timezone)["calls_today"] == 1


@pytest.mark.parametrize("status_code", [None, 408, 503])
def test_unknown_create_never_reposts(engine, config, content_id, video_job, monkeypatch, status_code):
    observed = []

    def respond(request):
        observed.append(request.method)
        if status_code:
            return httpx.Response(status_code)
        raise httpx.ReadTimeout("private provider details", request=request)

    install_transport(monkeypatch, respond)
    for _ in range(2):
        with pytest.raises(AppError) as caught:
            call(engine, config, video_job, content_id)
        assert caught.value.code == "MODEL_OUTCOME_UNKNOWN"
        assert "private" not in str(caught.value)
    assert observed == ["POST"]


def test_poll_network_failure_keeps_id_and_retry_only_gets(
    engine, config, content_id, video_job, monkeypatch
):
    observed = []

    def respond(request):
        observed.append(request.method)
        if len(observed) == 2:
            raise httpx.ReadTimeout("private query details", request=request)
        return complete(request)

    install_transport(monkeypatch, respond)
    with pytest.raises(AppError) as caught:
        call(engine, config, video_job, content_id)
    assert caught.value.code == "VIDEO_RESULT_PENDING"
    result = call(engine, config, video_job, content_id)
    assert result["task_id"] == "fixture_task_1"
    assert observed == ["POST", "GET", "GET"]
    with Session(engine) as session:
        assert usage_summary(session, config.timezone)["calls_today"] == 1


def test_cancel_after_create_preserves_task_and_can_resume(
    engine, config, content_id, video_job, monkeypatch
):
    observed = []

    def respond(request):
        observed.append(request.method)
        if request.method == "POST":
            with Session(engine) as session:
                session.get(Job, video_job).cancel_requested = True
                session.commit()
        return complete(request)

    install_transport(monkeypatch, respond)
    with pytest.raises(AppError) as caught:
        call(engine, config, video_job, content_id)
    assert caught.value.code == "TASK_CANCELED"
    assert observed == ["POST"]
    with Session(engine) as session:
        assert session.scalar(select(ModelCall)).response["task_id"] == "fixture_task_1"
        assert session.scalar(select(Asset)) is None
        session.get(Job, video_job).cancel_requested = False
        session.commit()
    call(engine, config, video_job, content_id)
    assert observed == ["POST", "GET"]


def test_late_create_response_retains_id_but_cannot_attach_asset(
    engine, config, content_id, video_job, monkeypatch
):
    observed = []

    def respond(request):
        observed.append(request.method)
        with Session(engine) as session:
            session.get(Job, video_job).lease_owner = "new-owner"
            session.commit()
        return complete(request)

    install_transport(monkeypatch, respond)
    with pytest.raises(AppError) as caught:
        call(engine, config, video_job, content_id)
    assert caught.value.code == "TASK_LEASE_LOST"
    with Session(engine) as session:
        assert session.scalar(select(ModelCall)).response["task_id"] == "fixture_task_1"
        assert session.scalar(select(Asset)) is None
    assert observed == ["POST"]


def test_terminal_failure_never_creates_replacement(engine, config, content_id, video_job, monkeypatch):
    observed = []

    def respond(request):
        observed.append(request.method)
        return (
            complete(request)
            if request.method == "POST"
            else httpx.Response(200, json={"task": {"status": "failed"}})
        )

    install_transport(monkeypatch, respond)
    for _ in range(2):
        with pytest.raises(AppError) as caught:
            call(engine, config, video_job, content_id)
        assert caught.value.code == "VIDEO_TASK_FAILED"
    assert observed == ["POST", "GET"]


def test_budget_blocks_new_create_before_network(engine, config, content_id, video_job, monkeypatch):
    with Session(engine) as session:
        record = session.get(Setting, "app")
        value = copy.deepcopy(record.value)
        value["limits"]["daily_calls"] = 0
        record.value = value
        session.commit()
    install_transport(monkeypatch, lambda _: pytest.fail("network must not run"))
    with pytest.raises(AppError) as caught:
        call(engine, config, video_job, content_id)
    assert caught.value.code == "DAILY_CALL_LIMIT"


def test_download_uses_public_url_guard_and_preserves_task(
    engine, config, content_id, video_job, monkeypatch
):
    from app.integrations.net import safe_get

    def respond(request):
        return (
            complete(request)
            if request.method == "POST"
            else httpx.Response(
                200, json={"task": {"status": "succeeded", "content": {"url": "http://127.0.0.1/private"}}}
            )
        )

    install_transport(monkeypatch, respond)
    monkeypatch.setattr("app.video_provider.safe_get", safe_get)
    with pytest.raises(AppError) as caught:
        call(engine, config, video_job, content_id)
    assert caught.value.code == "VIDEO_RESULT_PENDING"
    with Session(engine) as session:
        assert session.scalar(select(ModelCall)).response["task_id"] == "fixture_task_1"
        assert session.scalar(select(Asset)) is None


def test_changed_provider_on_resume_does_not_create_new_task(
    engine, config, content_id, video_job, monkeypatch
):
    def respond(request):
        return complete(request) if request.method == "POST" else httpx.Response(503)

    install_transport(monkeypatch, respond)
    with pytest.raises(AppError):
        call(engine, config, video_job, content_id)
    with Session(engine) as session:
        record = session.get(Setting, "app")
        value = copy.deepcopy(record.value)
        value["models"]["video"]["base_url"] = "https://another.example.test"
        record.value = value
        session.commit()
    with pytest.raises(AppError) as caught:
        call(engine, config, video_job, content_id)
    assert caught.value.code == "JOB_INPUT_CHANGED"


def test_worker_recovery_and_retry_keep_known_video_request(
    client, engine, config, content_id, video_job, monkeypatch
):
    def respond(request):
        return complete(request) if request.method == "POST" else httpx.Response(503)

    install_transport(monkeypatch, respond)
    with pytest.raises(AppError):
        call(engine, config, video_job, content_id)
    with Session(engine) as session:
        record = session.scalar(select(ModelCall))
        record.status = "STARTED"
        job = session.get(Job, video_job)
        job.lease_until = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
        session.commit()
        recover_expired(session, "content", datetime.now(UTC))
        session.commit()
        assert session.get(Job, video_job).status == "QUEUED"
        job.status = "WAITING_USER"
        job.error_code = "MODEL_OUTCOME_UNKNOWN"
        session.commit()
        frozen = copy.deepcopy(job.payload["model_requests"])
    response = client.post(f"/api/v1/jobs/{video_job}/retry")
    assert response.status_code == 202, response.text
    with Session(engine) as session:
        assert session.get(Job, video_job).payload["model_requests"] == frozen


def test_explicit_references_are_sent_once_and_not_persisted(engine, config, content_id, video_job, monkeypatch, tmp_path):
    import json

    from app.storage import register_file

    references = {}
    with Session(engine) as session:
        for kind, mime, asset_kind in (("image", "image/jpeg", "portrait"), ("audio", "audio/wav", "voice_reference")):
            path = tmp_path / kind
            path.write_bytes(("private-reference-" + kind).encode())
            asset = register_file(session, config, path, kind=asset_kind, filename=kind, mime_type=mime, content_id=None)
            session.flush()
            references[kind] = {"asset_id": asset.id, "sha256": asset.sha256}
        session.commit()
    observed = []
    install_transport(monkeypatch, lambda request: observed.append(request) or complete(request))
    payload = {"prompt": "Use Image 1 and Audio 1", "duration": 15, "resolution": "768P", "ratio": "9:16", "references": references}
    def generate():
        return invoke_video(engine, config, job_id=video_job, owner="video-owner", content_id=content_id,
                            scope="explicit-references", payload=payload)
    first = generate()
    assert generate() == first
    body = json.loads(observed[0].content)
    assert [item["type"] for item in body["content"]] == ["text", "image_url", "audio_url"]
    assert body["content"][1]["role"] == "reference_image"
    assert body["content"][1]["image_url"]["url"].startswith("data:image/jpeg;base64,")
    assert body["content"][2]["role"] == "reference_audio"
    assert body["content"][2]["audio_url"]["url"].startswith("data:audio/wav;base64,")
    assert [request.method for request in observed] == ["POST", "GET"]
    with Session(engine) as session:
        record = session.scalar(select(ModelCall))
        assert "data:" not in str(record.response) + str(record.events) + str(session.get(Job, video_job).payload)
        reference = session.get(Asset, references["image"]["asset_id"])
        from app.services import asset_path
        asset_path(config, reference).write_bytes(b"changed-after-approval")
    with pytest.raises(AppError, match="参考素材文件发生变化"):
        generate()
    assert len(observed) == 2


def test_m4a_voice_reference_is_converted_to_wav_before_video_request(
    engine, config, content_id, video_job, monkeypatch, tmp_path
):
    import base64
    import io
    import json
    import shutil
    import subprocess
    import wave

    from app.storage import register_file

    voice = tmp_path / "voice.m4a"
    subprocess.run(
        [shutil.which("ffmpeg"), "-v", "error", "-f", "lavfi", "-i",
         "sine=frequency=440:duration=3:sample_rate=48000", "-c:a", "aac", str(voice)],
        check=True,
    )
    with Session(engine) as session:
        portrait = tmp_path / "portrait.jpg"
        portrait.write_bytes(b"fixture-portrait")
        image_asset = register_file(
            session, config, portrait, kind="portrait", filename="portrait.jpg",
            mime_type="image/jpeg", content_id=None,
        )
        audio_asset = register_file(
            session, config, voice, kind="voice_reference", filename="voice.m4a",
            mime_type="audio/mp4", content_id=None,
        )
        session.flush()
        references = {
            "image": {"asset_id": image_asset.id, "sha256": image_asset.sha256},
            "audio": {"asset_id": audio_asset.id, "sha256": audio_asset.sha256},
        }
        session.commit()
    observed = []
    install_transport(monkeypatch, lambda request: observed.append(request) or complete(request))
    invoke_video(
        engine, config, job_id=video_job, owner="video-owner", content_id=content_id,
        scope="m4a-reference", payload={"prompt": "Person speaking", "duration": 6,
                                        "resolution": "768P", "ratio": "9:16", "references": references},
    )
    uri = json.loads(observed[0].content)["content"][2]["audio_url"]["url"]
    assert uri.startswith("data:audio/wav;base64,")
    with wave.open(io.BytesIO(base64.b64decode(uri.partition(",")[2]))) as converted:
        assert converted.getframerate() == 24000
        assert converted.getnchannels() == 1
        assert 2.9 <= converted.getnframes() / converted.getframerate() <= 3.1
    assert voice.is_file()
    with Session(engine) as session:
        record = session.scalar(select(ModelCall))
        assert "data:" not in str(record.response) + str(record.events) + str(session.get(Job, video_job).payload)
