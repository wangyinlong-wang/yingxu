from __future__ import annotations

import copy
import json

import httpx
import pytest
from conftest import fixture_asset
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Asset, Content, Job, JobStep, ModelCall, Setting, now, uid
from app.services import AppError, enqueue
from app.worker import Worker
from app.workflow import TaskContext, plan_motion, produce_content, render


def _enable_text_model(engine, *, daily_calls: int = 20) -> None:
    with Session(engine) as session:
        setting = session.get(Setting, "app")
        values = copy.deepcopy(setting.value)
        values["models"]["text"].update(
            enabled=True,
            base_url="https://fixture.example/v1",
            model="fixture-text",
        )
        values["limits"].update(daily_calls=daily_calls, daily_videos=20)
        setting.value = values
        session.commit()


def _ready_scenes(engine, config, content_id: str) -> list[dict]:
    with Session(engine) as session:
        content = session.get(Content, content_id)
        scenes = []
        for index in range(2):
            image = fixture_asset(session, config, content_id, "image")
            audio = fixture_asset(session, config, content_id, "audio")
            scenes.append(
                {
                    "id": f"scene-{index + 1}",
                    "order": index,
                    "narration": f"第{index + 1}个镜头的完整旁白。",
                    "caption": f"镜头{index + 1}",
                    "highlight_terms": [],
                    "image_prompt": f"竖屏画面 {index + 1}",
                    "image_asset_id": image.id,
                    "audio_asset_ids": [audio.id],
                }
            )
        content.scenes = scenes
        session.commit()
        return copy.deepcopy(scenes)


def _queue_and_claim(client, engine, content_id: str, monkeypatch) -> tuple[str, TaskContext]:
    monkeypatch.setattr("app.api.shutil.which", lambda name: f"/fixture/{name}")
    submitted = client.post(
        f"/api/v1/contents/{content_id}/generate",
        json={"stage": "motion"},
        headers={"Idempotency-Key": "fixture-motion"},
    )
    assert submitted.status_code == 202, submitted.text
    repeated = client.post(
        f"/api/v1/contents/{content_id}/generate",
        json={"stage": "motion"},
        headers={"Idempotency-Key": "fixture-motion"},
    )
    assert repeated.json() == submitted.json()
    worker = Worker(client.app.state.config, engine=engine)
    job = worker.claim()
    assert job and job.id == submitted.json()["job_id"]
    return job.id, TaskContext(engine, client.app.state.config, job.id, worker.owner)


def test_legacy_patch_preserves_motion_and_explicit_edit_invalidates_video(
    client, engine, config, approved_content
):
    with Session(engine) as session:
        content = session.get(Content, approved_content["id"])
        image = fixture_asset(session, config, content.id, "image")
        audio = fixture_asset(session, config, content.id, "audio")
        content.scenes = [
            {
                "id": "scene-preserved",
                "order": 0,
                "narration": "保留已有图片和声音。",
                "caption": "原标题",
                "highlight_terms": [],
                "image_prompt": "原提示词",
                "image_asset_id": image.id,
                "audio_asset_ids": [audio.id],
                "motion": "pan_left",
                "transition": "cut",
            }
        ]
        image_id, audio_id = image.id, audio.id
        session.commit()

    current = client.get(f"/api/v1/contents/{approved_content['id']}").json()
    legacy_scene = copy.deepcopy(current["scenes"][0])
    legacy_scene.pop("motion")
    legacy_scene.pop("transition")
    legacy_scene["caption"] = "旧客户端改标题"
    changed = client.patch(
        f"/api/v1/contents/{approved_content['id']}",
        json={"version": current["version"], "scenes": [legacy_scene]},
    )
    assert changed.status_code == 200, changed.text
    value = changed.json()
    assert (value["scenes"][0]["motion"], value["scenes"][0]["transition"]) == (
        "pan_left",
        "cut",
    )
    assert value["video_asset_id"] is None and value["media_version"] is None
    assert value["scenes"][0]["image_asset_id"] == image_id
    assert value["scenes"][0]["audio_asset_ids"] == [audio_id]

    explicit = copy.deepcopy(value["scenes"])
    explicit[0]["motion"], explicit[0]["transition"] = "pull_out", "fade"
    changed_again = client.patch(
        f"/api/v1/contents/{approved_content['id']}",
        json={"version": value["version"], "scenes": explicit},
    )
    assert changed_again.status_code == 200, changed_again.text
    assert changed_again.json()["scenes"][0]["image_asset_id"] == image_id
    assert changed_again.json()["scenes"][0]["audio_asset_ids"] == [audio_id]


def test_fact_patch_with_frontend_motion_defaults_keeps_legacy_scene_video(
    client, engine, config, rendered_content
):
    with Session(engine) as session:
        content = session.get(Content, rendered_content)
        image = fixture_asset(session, config, content.id, "image")
        audio = fixture_asset(session, config, content.id, "audio")
        content.scenes = [
            {
                "id": "legacy-scene",
                "order": 0,
                "narration": "旧分镜仍应复用当前成片。",
                "caption": "旧分镜",
                "highlight_terms": [],
                "image_prompt": "旧数据没有动画字段",
                "image_asset_id": image.id,
                "audio_asset_ids": [audio.id],
            }
        ]
        original_video_id = content.video_asset_id
        session.commit()

    current = client.get(f"/api/v1/contents/{rendered_content}").json()
    script = copy.deepcopy(current["script"])
    script["facts"][0]["verified"] = True
    scenes = copy.deepcopy(current["scenes"])
    scenes[0].update(motion="auto", transition="fade")
    response = client.patch(
        f"/api/v1/contents/{rendered_content}",
        json={"version": current["version"], "script": script, "scenes": scenes},
    )
    assert response.status_code == 200, response.text
    changed = response.json()
    assert changed["video_asset_id"] == original_video_id
    assert changed["media_version"] == changed["version"] == current["version"] + 1
    assert changed["status"] == "REVIEW_REQUIRED"
    assert changed["scenes"][0]["motion"] == "auto"
    assert changed["scenes"][0]["transition"] == "fade"


def test_motion_plan_uses_frozen_text_snapshot_checkpoints_and_changes_render_hash(
    client, engine, config, content_id, monkeypatch
):
    _enable_text_model(engine)
    original_scenes = _ready_scenes(engine, config, content_id)
    job_id, ctx = _queue_and_claim(client, engine, content_id, monkeypatch)
    model_calls = []
    compose_calls = []

    def model(*_args, **kwargs):
        model_calls.append(kwargs)
        return {
            "text": json.dumps(
                {
                    "scenes": [
                        {"id": "scene-2", "motion": "pull_out", "transition": "cut"},
                        {"id": "scene-1", "motion": "push_in", "transition": "fade"},
                    ]
                }
            )
        }

    def compose_video(*, scenes, output_dir, **_kwargs):
        compose_calls.append(copy.deepcopy(scenes))
        output_dir.mkdir(parents=True, exist_ok=True)
        output = {
            "video_path": output_dir / "video.mp4",
            "cover_path": output_dir / "cover.png",
            "subtitle_path": output_dir / "subtitles.srt",
        }
        for path in output.values():
            path.write_bytes(f"render-{len(compose_calls)}".encode())
        return {key: str(path) for key, path in output.items()}

    monkeypatch.setattr("app.workflow.invoke_model", model)
    monkeypatch.setattr("app.integrations.media.compose_video", compose_video)
    first = produce_content(ctx)
    assert first["status"] == "REVIEW_REQUIRED"
    assert len(model_calls) == len(compose_calls) == 1

    with Session(engine) as session:
        job = session.get(Job, job_id)
        content = session.get(Content, content_id)
        frozen = job.payload["motion_input"]
        assert frozen == {
            "title": content.title,
            "scenes": [
                {
                    key: scene[key]
                    for key in ("id", "caption", "narration", "image_prompt")
                }
                for scene in original_scenes
            ],
        }
        assert job.payload["local_ai"]["voice_enabled"] is False
        assert "reference_text" not in json.dumps(frozen)
        assert session.scalar(
            select(JobStep.id).where(
                JobStep.job_id == job_id,
                JobStep.name == "AI编排动画",
                JobStep.status == "SUCCEEDED",
            )
        )
        assert [scene["motion"] for scene in content.scenes] == ["push_in", "pull_out"]
        assert [scene["transition"] for scene in content.scenes] == ["fade", "cut"]
        assert [scene["image_asset_id"] for scene in content.scenes] == [
            scene["image_asset_id"] for scene in original_scenes
        ]
        assert [scene["audio_asset_ids"] for scene in content.scenes] == [
            scene["audio_asset_ids"] for scene in original_scenes
        ]
        first_hash = session.get(Asset, content.video_asset_id).input_hash

    model_request = model_calls[0]
    assert model_request["scope"] == f"content:{content_id}:version:{ctx.payload['version']}:motion"
    assert model_request["kind"] == "text"
    cloud_body = json.loads(model_request["payload"]["messages"][1]["content"])
    assert cloud_body["input"] == frozen
    assert "image_asset_id" not in json.dumps(cloud_body)
    assert [(scene["motion"], scene["transition"]) for scene in compose_calls[0]] == [
        ("push_in", "fade"),
        ("pull_out", "cut"),
    ]

    assert produce_content(ctx)["status"] == "REVIEW_REQUIRED"
    assert len(model_calls) == len(compose_calls) == 1

    with Session(engine) as session:
        content = session.get(Content, content_id)
        changed = copy.deepcopy(content.scenes)
        changed[0]["motion"] = "none"
        content.scenes = changed
        session.commit()
    render(ctx)
    with Session(engine) as session:
        content = session.get(Content, content_id)
        assert session.get(Asset, content.video_asset_id).input_hash != first_hash
    assert len(compose_calls) == 2


def test_motion_plan_rejects_missing_extra_duplicate_malicious_and_invalid_rows(
    client, engine, config, content_id, monkeypatch
):
    _enable_text_model(engine)
    original_scenes = _ready_scenes(engine, config, content_id)
    _, ctx = _queue_and_claim(client, engine, content_id, monkeypatch)
    invalid_plans = [
        {"scenes": [{"id": "scene-1", "motion": "none", "transition": "fade"}]},
        {
            "scenes": [
                {"id": "scene-1", "motion": "none", "transition": "fade"},
                {"id": "scene-2", "motion": "pan_left", "transition": "fade"},
                {"id": "scene-extra", "motion": "none", "transition": "cut"},
            ]
        },
        {
            "scenes": [
                {"id": "scene-1", "motion": "none", "transition": "fade"},
                {"id": "scene-1", "motion": "pan_left", "transition": "cut"},
            ]
        },
        {
            "scenes": [
                {
                    "id": "scene-1",
                    "motion": "none",
                    "transition": "fade",
                    "command": "run arbitrary code",
                },
                {"id": "scene-2", "motion": "pan_left", "transition": "cut"},
            ]
        },
        {
            "scenes": [
                {"id": "scene-1", "motion": "exec('bad')", "transition": "fade"},
                {"id": "scene-2", "motion": "pan_left", "transition": "cut"},
            ]
        },
    ]
    current = {"value": invalid_plans[0]}
    monkeypatch.setattr(
        "app.workflow.invoke_model",
        lambda *_args, **_kwargs: {"text": json.dumps(current["value"])},
    )
    for plan in invalid_plans:
        current["value"] = plan
        with pytest.raises(AppError) as caught:
            plan_motion(ctx)
        assert caught.value.code == "MODEL_OUTPUT_INVALID"
    with Session(engine) as session:
        assert session.get(Content, content_id).scenes == original_scenes


def test_motion_plan_rejects_tampered_frozen_input_before_model_call(
    client, engine, config, content_id, monkeypatch
):
    _enable_text_model(engine)
    _ready_scenes(engine, config, content_id)
    job_id, ctx = _queue_and_claim(client, engine, content_id, monkeypatch)
    with Session(engine) as session:
        job = session.get(Job, job_id)
        payload = copy.deepcopy(job.payload)
        payload["motion_input"]["reference_text"] = "must not reach model"
        job.payload = payload
        session.commit()
    ctx.payload["motion_input"]["reference_text"] = "must not reach model"
    monkeypatch.setattr(
        "app.workflow.invoke_model",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("model must not run")),
    )
    with pytest.raises(AppError) as caught:
        plan_motion(ctx)
    assert caught.value.code == "JOB_INPUT_CHANGED"


def test_unknown_motion_model_outcome_is_not_sent_again(
    client, engine, config, content_id, monkeypatch
):
    _enable_text_model(engine)
    _ready_scenes(engine, config, content_id)
    _, ctx = _queue_and_claim(client, engine, content_id, monkeypatch)
    requests = []
    original_client = httpx.Client

    def respond(request):
        requests.append(request.url.path)
        return httpx.Response(500, json={"error": "fixture unknown outcome"})

    monkeypatch.setattr(
        "app.providers.httpx.Client",
        lambda **kwargs: original_client(transport=httpx.MockTransport(respond), **kwargs),
    )
    for _ in range(2):
        with pytest.raises(AppError) as caught:
            plan_motion(ctx)
        assert caught.value.code == "MODEL_OUTCOME_UNKNOWN"
    assert requests == ["/v1/chat/completions"]
    with Session(engine) as session:
        calls = list(session.scalars(select(ModelCall)))
        assert len(calls) == 1 and calls[0].status == "UNKNOWN" and calls[0].attempts == 1


def test_motion_preflight_requires_sources_model_and_available_quota(
    client, engine, config, content_id, monkeypatch
):
    monkeypatch.setattr("app.api.shutil.which", lambda name: f"/fixture/{name}")
    empty = client.post(f"/api/v1/contents/{content_id}/generate", json={"stage": "motion"})
    assert empty.status_code == 409 and empty.json()["code"] == "SCRIPT_REQUIRED"

    _ready_scenes(engine, config, content_id)
    disabled = client.post(f"/api/v1/contents/{content_id}/generate", json={"stage": "motion"})
    assert disabled.status_code == 409 and disabled.json()["code"] == "MODEL_NOT_CONFIGURED"

    _enable_text_model(engine, daily_calls=1)
    with Session(engine) as session:
        job = enqueue(session, kind="GENERATE_REPORT", payload={})
        session.add(
            ModelCall(
                id=uid(),
                job_id=job.id,
                request_key="a" * 64,
                kind="text",
                model="fixture-text",
                status="SUCCEEDED",
                events=[{"started_at": now(), "status": "SUCCEEDED", "usage": {}}],
            )
        )
        session.commit()
    exhausted = client.post(f"/api/v1/contents/{content_id}/generate", json={"stage": "motion"})
    assert exhausted.status_code == 409 and exhausted.json()["code"] == "DAILY_CALL_LIMIT"
