from __future__ import annotations

import copy
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.orm import Session

from app.models import Asset, Content, Job, Setting
from app.storage import register_file
from app.worker import Worker
from app.workflow import TaskContext, produce_content


def test_transcribe_video_clip_runs_locally_and_returns_transcript(client, engine, config, content_id, tmp_path, monkeypatch):
    status = {"transcribe": {"installed": True, "model": "whisper-tiny", "engine": "cpu"}, "revision": "test-local"}
    monkeypatch.setattr("app.api.local_status", lambda _: status)
    monkeypatch.setattr("app.integrations.local_ai.local_status", lambda _: status)
    with Session(engine) as session:
        file = tmp_path / "speech.mp4"
        file.write_bytes(b"fixture-video-speech")
        clip = register_file(session, config, file, kind="video_clip", content_id=content_id,
                             mime_type="video/mp4", filename="speech.mp4")
        session.commit()
        clip_id = clip.id
    calls = []
    def run_local(_config, operation, payload, **kwargs):
        calls.append(operation)
        assert payload["audio_path"].endswith(".mp4")
        return {"transcript": "这是视频原声。"}
    monkeypatch.setattr("app.integrations.local_ai.run_local", run_local)
    response = client.post("/api/v1/local-ai/transcribe", json={"asset_id": clip_id})
    assert response.status_code == 202, response.text
    assert Worker(config, engine=engine).run_once()
    job = client.get(f"/api/v1/jobs/{response.json()['job_id']}").json()
    assert job["status"] == "SUCCEEDED", job
    assert job["result"] == {"transcript": "这是视频原声。", "source_asset_id": clip_id, "local": True}
    assert calls == ["transcribe"]


@pytest.mark.parametrize("case", ["valid", "silent", "foreign", "mixed", "different_clip"])
def test_select_native_video_audio(client, engine, config, content_id, tmp_path, monkeypatch, case):
    prepare_video_scene(engine, content_id)
    with Session(engine) as session:
        file = tmp_path / "native.mp4"
        file.write_bytes(b"fixture-native")
        clip = register_file(session, config, file, kind="video_clip", content_id=content_id,
                             mime_type="video/mp4", filename="native.mp4")
        session.flush()
        clip_id = clip.id
        file.write_bytes(b"fixture-other")
        other = register_file(session, config, file, kind="video_clip",
                              content_id=None if case == "foreign" else content_id,
                              mime_type="video/mp4", filename="other.mp4")
        session.commit()
        other_id = other.id
    monkeypatch.setattr("app.integrations.media.probe_media", lambda _: {"has_audio": case != "silent", "duration": 2})
    endpoint = f"/api/v1/contents/{content_id}"
    original = client.get(endpoint).json()
    scene = original["scenes"][0]
    scene["video_asset_id"] = clip_id
    scene["audio_asset_ids"] = [other_id] if case in {"foreign", "different_clip"} else [clip_id]
    if case == "mixed":
        scene["audio_asset_ids"].append(other_id)
    saved = client.patch(endpoint, json={"version": original["version"], "scenes": [scene]})
    if case != "valid":
        assert saved.status_code == 422, saved.text
        assert saved.json()["code"] == ("MISSING_NATIVE_AUDIO" if case == "silent" else "INVALID_NATIVE_AUDIO")
        return
    assert saved.status_code == 200, saved.text
    assert saved.json()["scenes"][0]["audio_asset_ids"] == [clip_id]
    scene["video_asset_id"] = other_id
    changed = client.patch(endpoint, json={"version": saved.json()["version"], "scenes": [scene]})
    assert changed.status_code == 200, changed.text
    assert changed.json()["scenes"][0]["audio_asset_ids"] == []


def test_render_with_native_clip_does_not_queue_corner_avatar(
    client, engine, config, content_id, tmp_path, monkeypatch
):
    prepare_video_scene(engine, content_id)
    with Session(engine) as session:
        image_path = tmp_path / "image.png"
        image_path.write_bytes(b"fixture-image")
        clip_path = tmp_path / "clip.mp4"
        clip_path.write_bytes(b"fixture-native-audio")
        image = register_file(
            session, config, image_path, kind="image", content_id=content_id,
            mime_type="image/png", filename="image.png",
        )
        clip = register_file(
            session, config, clip_path, kind="video_clip", content_id=content_id,
            mime_type="video/mp4", filename="clip.mp4",
        )
        content = session.get(Content, content_id)
        scene = copy.deepcopy(content.scenes[0])
        scene.update(image_asset_id=image.id, video_asset_id=clip.id, audio_asset_ids=[clip.id])
        content.scenes = [scene]
        setting = session.get(Setting, "app")
        values = copy.deepcopy(setting.value)
        values["local_ai"]["avatar_enabled"] = True
        setting.value = values
        session.commit()
    monkeypatch.setattr("app.integrations.media.probe_media", lambda _: {"has_audio": True, "duration": 6})
    response = client.post(f"/api/v1/contents/{content_id}/generate", json={"stage": "render"})
    assert response.status_code == 202, response.text
    with Session(engine) as session:
        job = session.get(Job, response.json()["job_id"])
        assert job.payload["local_ai"]["avatar_enabled"] is False


def prepare_video_scene(engine, content_id):
    with Session(engine) as session:
        content = session.get(Content, content_id)
        content.scenes = [
            {
                "id": "scene-video",
                "order": 0,
                "narration": "技术演示。",
                "caption": "技术演示",
                "image_prompt": "A robot moves a box",
                "image_asset_id": None,
                "audio_asset_ids": [],
                "scene_type": "image",
            }
        ]
        settings = session.get(Setting, "app")
        value = copy.deepcopy(settings.value)
        value["models"]["video"].update(
            enabled=True, base_url="https://video.example.test/api/minimax", model="MiniMax-H3"
        )
        settings.value = value
        session.commit()


def test_video_model_settings_and_key_are_additive_and_private(client):
    response = client.get("/api/v1/settings")
    assert response.status_code == 200
    assert response.json()["models"]["video"]["enabled"] is False
    assert response.json()["video"]["renderer"] == "ffmpeg"
    assert (
        client.patch("/api/v1/settings", json={"models": {"video": {"protocol": "openai"}}}).status_code
        == 422
    )
    assert (
        client.patch("/api/v1/settings", json={"models": {"video": {"source": "local"}}}).status_code == 422
    )
    secret = "fixture-video-key"
    saved = client.post(
        "/api/v1/settings/model-keys/video",
        json={
            "source": "third_party",
            "base_url": "https://video.example.test",
            "action": "set",
            "api_key": secret,
        },
    )
    assert saved.status_code == 200
    assert saved.json()["key_source"] == "keychain"
    assert secret not in saved.text
    assert secret not in client.get("/api/v1/settings").text


def test_video_generation_requires_one_image_scene_and_freezes_options(client, engine, content_id):
    prepare_video_scene(engine, content_id)
    endpoint = f"/api/v1/contents/{content_id}/generate"
    assert client.post(endpoint, json={"stage": "video"}).status_code == 422
    assert client.post(endpoint, json={"stage": "video", "scene_id": "missing"}).status_code == 404
    response = client.post(endpoint, json={"stage": "video", "scene_id": "scene-video"})
    assert response.status_code == 202, response.text
    with Session(engine) as session:
        job = session.get(Job, response.json()["job_id"])
        assert job.payload["video_options"]["clip_duration"] == 6
        assert job.payload["video_options"]["clip_resolution"] == "768P"
        assert job.payload["scene_id"] == "scene-video"


def test_video_generation_does_not_route_cards_to_paid_model(client, engine, content_id):
    prepare_video_scene(engine, content_id)
    with Session(engine) as session:
        content = session.get(Content, content_id)
        scenes = copy.deepcopy(content.scenes)
        scenes[0]["scene_type"] = "stat_card"
        scenes[0]["visual_data"] = {"value": "12"}
        content.scenes = scenes
        session.commit()
    response = client.post(
        f"/api/v1/contents/{content_id}/generate", json={"stage": "video", "scene_id": "scene-video"}
    )
    assert response.status_code == 422
    assert response.json()["code"] == "VIDEO_SCENE_INVALID"


def test_video_confirmation_freezes_price_inputs_and_idempotency(client, engine, content_id):
    prepare_video_scene(engine, content_id)
    endpoint = f"/api/v1/contents/{content_id}/generate"
    confirmed = {"stage": "video", "scene_id": "scene-video", "clip_duration": 4, "clip_resolution": "768P"}
    # Another window changes the default after the user has read the fee dialog.
    with Session(engine) as session:
        settings = session.get(Setting, "app")
        value = copy.deepcopy(settings.value)
        value["video"].update(clip_duration=15, clip_resolution="2K")
        settings.value = value
        session.commit()
    headers = {"Idempotency-Key": "confirmed-video-test"}
    response = client.post(endpoint, json=confirmed, headers=headers)
    assert response.status_code == 202, response.text
    with Session(engine) as session:
        options = session.get(Job, response.json()["job_id"]).payload["video_options"]
        assert options["clip_duration"] == 4
        assert options["clip_resolution"] == "768P"
    assert client.post(endpoint, json=confirmed, headers=headers).json() == response.json()
    conflicting = client.post(endpoint, json={**confirmed, "clip_duration": 15}, headers=headers)
    assert conflicting.status_code == 409
    assert conflicting.json()["code"] == "IDEMPOTENCY_CONFLICT"
    assert client.post(endpoint, json={"stage": "video", "scene_id": "scene-video", "clip_duration": 4}).status_code == 422


def test_video_stage_saves_clip_without_rendering_or_other_model_calls(
    client, engine, config, content_id, tmp_path, monkeypatch
):
    prepare_video_scene(engine, content_id)
    response = client.post(
        f"/api/v1/contents/{content_id}/generate", json={"stage": "video", "scene_id": "scene-video"}
    )
    assert response.status_code == 202
    job_id = response.json()["job_id"]
    with Session(engine) as session:
        job = session.get(Job, job_id)
        job.status = "RUNNING"
        job.lease_owner = "video-test"
        job.lease_until = (datetime.now(UTC) + timedelta(minutes=5)).isoformat()
        file = tmp_path / "clip.mp4"
        file.write_bytes(b"fixture-video")
        clip = register_file(
            session,
            config,
            file,
            kind="video_clip",
            content_id=content_id,
            mime_type="video/mp4",
            filename="clip.mp4",
        )
        session.commit()
        clip_id = clip.id
    observed = []

    def invoke(*args, **kwargs):
        observed.append(kwargs)
        return {"asset_id": clip_id, "task_id": "fixture-task"}

    monkeypatch.setattr("app.video_provider.invoke_video", invoke)
    monkeypatch.setattr(
        "app.workflow.render", lambda *_: (_ for _ in ()).throw(AssertionError("unexpected render"))
    )
    monkeypatch.setattr(
        "app.workflow.invoke_model",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("unexpected model")),
    )
    result = produce_content(TaskContext(engine, config, job_id, "video-test"))
    assert result["status"] == "DRAFT"
    assert len(observed) == 1
    assert observed[0]["payload"]["ratio"] == "9:16"
    with Session(engine) as session:
        content = session.get(Content, content_id)
        assert content.scenes[0]["video_asset_id"] == clip_id
        assert content.video_asset_id is None
        assert content.media_version is None


def test_legacy_scene_patch_retains_clip_and_visual_change_invalidates_it(
    client, engine, config, content_id, tmp_path
):
    prepare_video_scene(engine, content_id)
    with Session(engine) as session:
        file = tmp_path / "clip.mp4"
        file.write_bytes(b"fixture-clip")
        clip = register_file(
            session,
            config,
            file,
            kind="video_clip",
            content_id=content_id,
            mime_type="video/mp4",
            filename="clip.mp4",
        )
        session.flush()
        content = session.get(Content, content_id)
        scene = copy.deepcopy(content.scenes[0])
        scene["video_asset_id"] = clip.id
        content.scenes = [scene]
        session.commit()
        clip_id = clip.id
    original = client.get(f"/api/v1/contents/{content_id}").json()
    legacy_scene = copy.deepcopy(original["scenes"][0])
    legacy_scene.pop("video_asset_id")
    legacy_scene["caption"] = "说明画面"
    saved = client.patch(
        f"/api/v1/contents/{content_id}", json={"version": original["version"], "scenes": [legacy_scene]}
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["scenes"][0]["video_asset_id"] == clip_id
    legacy_scene["image_prompt"] = "A different scene"
    changed = client.patch(
        f"/api/v1/contents/{content_id}", json={"version": saved.json()["version"], "scenes": [legacy_scene]}
    )
    assert changed.status_code == 200
    assert changed.json()["scenes"][0]["video_asset_id"] is None


def test_scene_cannot_attach_another_contents_video(client, engine, config, content_id, tmp_path):
    prepare_video_scene(engine, content_id)
    with Session(engine) as session:
        file = tmp_path / "foreign.mp4"
        file.write_bytes(b"foreign-video")
        clip = register_file(
            session,
            config,
            file,
            kind="video_clip",
            content_id=None,
            mime_type="video/mp4",
            filename="foreign.mp4",
        )
        session.commit()
        clip_id = clip.id
    original = client.get(f"/api/v1/contents/{content_id}").json()
    scene = original["scenes"][0]
    scene["video_asset_id"] = clip_id
    response = client.patch(
        f"/api/v1/contents/{content_id}", json={"version": original["version"], "scenes": [scene]}
    )
    assert response.status_code == 422
    with Session(engine) as session:
        assert session.get(Asset, clip_id).content_id is None


def test_reference_selection_is_explicit_and_part_of_idempotency(client, engine, config, content_id, tmp_path):
    prepare_video_scene(engine, content_id)
    with Session(engine) as session:
        path = tmp_path / 'portrait.jpg'
        path.write_bytes(b'fixture-portrait')
        portrait = register_file(session, config, path, kind='portrait', filename='portrait.jpg', mime_type='image/jpeg', content_id=None)
        session.commit()
        asset_id, sha256 = portrait.id, portrait.sha256
    endpoint = f'/api/v1/contents/{content_id}/generate'
    assert client.post(endpoint, json={'stage': 'all', 'reference_image_asset_id': asset_id}).status_code == 422
    request = {'stage': 'video', 'scene_id': 'scene-video', 'reference_image_asset_id': asset_id}
    headers = {'Idempotency-Key': 'explicit-portrait-video'}
    response = client.post(endpoint, json=request, headers=headers)
    assert response.status_code == 202, response.text
    with Session(engine) as session:
        job = session.get(Job, response.json()['job_id'])
        assert job.payload['video_references'] == {'image': {'asset_id': asset_id, 'sha256': sha256}}
    assert client.post(endpoint, json=request, headers=headers).json() == response.json()
    assert client.post(endpoint, json={'stage': 'video', 'scene_id': 'scene-video'}, headers=headers).status_code == 409
