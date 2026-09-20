from __future__ import annotations

import copy

from conftest import fixture_asset
from sqlalchemy.orm import Session

from app.models import Asset, Content, Job, Setting
from app.worker import Worker
from app.workflow import TaskContext, produce_content


def _enable_image_model(engine, *, daily_calls: int = 20) -> None:
    with Session(engine) as session:
        setting = session.get(Setting, "app")
        values = copy.deepcopy(setting.value)
        values["models"]["image"].update(
            enabled=True,
            base_url="https://fixture.example/v1",
            model="fixture-image",
        )
        values["limits"].update(daily_calls=daily_calls, daily_videos=20)
        setting.value = values
        session.commit()


def _ready_content(engine, config, content_id: str, *, current_video: bool = False) -> dict:
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
                    "narration": f"镜头{index + 1}解释推理预填方法如何减少重复计算。",
                    "caption": f"推理预填{index + 1}",
                    "highlight_terms": [],
                    "image_prompt": f"原始视觉方向 {index + 1}",
                    "image_asset_id": image.id,
                    "audio_asset_ids": [audio.id],
                    "motion": "push_in",
                    "transition": "fade",
                }
            )
        content.scenes = scenes
        if current_video:
            content.video_asset_id = fixture_asset(session, config, content_id, "video").id
            content.cover_asset_id = fixture_asset(session, config, content_id, "cover").id
            content.subtitle_asset_id = fixture_asset(session, config, content_id, "subtitle").id
            content.media_version = content.version
            content.status = "REVIEW_REQUIRED"
        session.commit()
        return {
            "version": content.version,
            "scenes": copy.deepcopy(scenes),
            "video_asset_id": content.video_asset_id,
        }


def _compose_fixture(calls):
    def compose_video(*, scenes, output_dir, **_kwargs):
        calls.append(copy.deepcopy(scenes))
        output_dir.mkdir(parents=True, exist_ok=True)
        values = {
            "video_path": output_dir / "video.mp4",
            "cover_path": output_dir / "cover.png",
            "subtitle_path": output_dir / "subtitles.srt",
        }
        for path in values.values():
            path.write_bytes(f"redraw-render-{len(calls)}".encode())
        return {key: str(path) for key, path in values.items()}

    return compose_video


def test_single_scene_redraw_freezes_target_reuses_audio_and_checkpoints(
    client, engine, config, content_id, monkeypatch
):
    _enable_image_model(engine)
    baseline = _ready_content(engine, config, content_id, current_video=True)
    monkeypatch.setattr("app.api.shutil.which", lambda name: f"/fixture/{name}")
    request = {"stage": "images", "scene_id": "scene-1"}
    response = client.post(
        f"/api/v1/contents/{content_id}/generate",
        json=request,
        headers={"Idempotency-Key": "redraw-one"},
    )
    assert response.status_code == 202, response.text
    replay = client.post(
        f"/api/v1/contents/{content_id}/generate",
        json=request,
        headers={"Idempotency-Key": "redraw-one"},
    )
    assert replay.json() == response.json()
    conflict = client.post(
        f"/api/v1/contents/{content_id}/generate",
        json={"stage": "images", "scene_id": "scene-2"},
        headers={"Idempotency-Key": "redraw-one"},
    )
    assert conflict.status_code == 409 and conflict.json()["code"] == "IDEMPOTENCY_CONFLICT"

    with Session(engine) as session:
        content = session.get(Content, content_id)
        job = session.get(Job, response.json()["job_id"])
        assert content.version == baseline["version"] + 1
        assert content.video_asset_id is None and content.media_version is None
        assert [scene["image_asset_id"] for scene in content.scenes] == [
            None,
            baseline["scenes"][1]["image_asset_id"],
        ]
        assert [scene["audio_asset_ids"] for scene in content.scenes] == [
            scene["audio_asset_ids"] for scene in baseline["scenes"]
        ]
        assert job.payload["scene_id"] == "scene-1"
        assert job.payload["image_scene_ids"] == ["scene-1"]
        assert job.payload["local_ai"]["voice_enabled"] is False

        replacement = fixture_asset(session, config, content_id, "image")
        replacement_id = replacement.id
        session.commit()

    image_calls = []
    render_calls = []

    def image_model(*_args, **kwargs):
        assert kwargs["kind"] == "image"
        image_calls.append(kwargs)
        return {"asset_id": replacement_id}

    monkeypatch.setattr("app.workflow.invoke_model", image_model)
    monkeypatch.setattr("app.integrations.media.compose_video", _compose_fixture(render_calls))
    worker = Worker(config, engine=engine)
    job = worker.claim()
    assert job and job.id == response.json()["job_id"]
    ctx = TaskContext(engine, config, job.id, worker.owner)
    assert produce_content(ctx)["status"] == "REVIEW_REQUIRED"
    assert produce_content(ctx)["status"] == "REVIEW_REQUIRED"
    assert len(image_calls) == len(render_calls) == 1

    call = image_calls[0]
    assert (
        call["scope"] == f"content:{content_id}:version:{ctx.payload['version']}:scene:scene-1:image-redraw"
    )
    prompt = call["payload"]["prompt"]
    assert "镜头1解释推理预填方法如何减少重复计算" in prompt
    assert "推理预填1" in prompt
    assert "原始视觉方向 1" in prompt
    assert "用于验证审核的测试陈述" not in prompt
    assert "通用机器人" in prompt and "不得发明数值" in prompt
    assert "原生 9:16 竖屏构图" in prompt and "第 1/2 个镜头" in prompt
    assert "现代科技新闻纪实视觉" in prompt
    assert "reference_text" not in prompt

    with Session(engine) as session:
        content = session.get(Content, content_id)
        assert content.scenes[0]["image_asset_id"] == replacement_id
        assert content.scenes[1]["image_asset_id"] == baseline["scenes"][1]["image_asset_id"]
        assert [scene["audio_asset_ids"] for scene in content.scenes] == [
            scene["audio_asset_ids"] for scene in baseline["scenes"]
        ]
        assert content.status == "REVIEW_REQUIRED"
        assert session.get(Asset, baseline["scenes"][0]["image_asset_id"]) is not None


def test_image_prompt_keeps_method_scene_separate_from_later_numeric_results():
    from app.workflow import _image_generation_prompt

    prompt = _image_generation_prompt(
        "模型实验",
        {
            "narration": "把老师推理的前1%，塞给其他模型。",
            "caption": "推理预填方法",
            "image_prompt": "流程示意",
        },
        [
            {"claim": "实验使用教师模型推理过程的前1%进行预填"},
            {"claim": "预填后的重合度由16.79%变为34.97%"},
            {"claim": "提升18.18个百分点"},
        ],
    )
    assert "教师模型推理过程的前1%" in prompt
    assert "34.97%" not in prompt and "18.18" not in prompt
    assert "严格不要绘制任何文字、字母、数字、百分号" in prompt
    assert "顶部标题和底部字幕保留干净安全区" in prompt


def test_redraw_all_calls_image_once_per_scene_and_never_calls_text_or_speech(
    client, engine, config, content_id, monkeypatch
):
    _enable_image_model(engine)
    baseline = _ready_content(engine, config, content_id)
    monkeypatch.setattr("app.api.shutil.which", lambda name: f"/fixture/{name}")
    response = client.post(f"/api/v1/contents/{content_id}/generate", json={"stage": "images"})
    assert response.status_code == 202, response.text
    with Session(engine) as session:
        replacements = [fixture_asset(session, config, content_id, "image").id for _ in range(2)]
        session.commit()
    calls = []

    def image_model(*_args, **kwargs):
        assert kwargs["kind"] == "image"
        calls.append(kwargs)
        return {"asset_id": replacements[len(calls) - 1]}

    monkeypatch.setattr("app.workflow.invoke_model", image_model)
    monkeypatch.setattr("app.integrations.media.compose_video", _compose_fixture([]))
    assert Worker(config, engine=engine).run_once()
    assert len(calls) == 2
    assert all(f":version:{baseline['version'] + 1}:" in call["scope"] for call in calls)
    with Session(engine) as session:
        content = session.get(Content, content_id)
        assert [scene["image_asset_id"] for scene in content.scenes] == replacements
        assert [scene["audio_asset_ids"] for scene in content.scenes] == [
            scene["audio_asset_ids"] for scene in baseline["scenes"]
        ]


def test_redraw_validates_stage_target_and_non_target_assets_before_mutation(
    client, engine, config, content_id, monkeypatch
):
    _enable_image_model(engine)
    baseline = _ready_content(engine, config, content_id)
    monkeypatch.setattr("app.api.shutil.which", lambda name: f"/fixture/{name}")

    wrong_stage = client.post(
        f"/api/v1/contents/{content_id}/generate",
        json={"stage": "render", "scene_id": "scene-1"},
    )
    assert wrong_stage.status_code == 422
    missing = client.post(
        f"/api/v1/contents/{content_id}/generate",
        json={"stage": "images", "scene_id": "missing"},
    )
    assert missing.status_code == 404 and missing.json()["code"] == "SCENE_NOT_FOUND"

    with Session(engine) as session:
        content = session.get(Content, content_id)
        scenes = copy.deepcopy(content.scenes)
        scenes[1]["image_asset_id"] = None
        content.scenes = scenes
        session.commit()
    missing_other = client.post(
        f"/api/v1/contents/{content_id}/generate",
        json={"stage": "images", "scene_id": "scene-1"},
    )
    assert missing_other.status_code == 409 and missing_other.json()["code"] == "IMAGES_REQUIRED"
    with Session(engine) as session:
        content = session.get(Content, content_id)
        assert content.version == baseline["version"]
        assert content.scenes[0]["image_asset_id"] == baseline["scenes"][0]["image_asset_id"]

        scenes = copy.deepcopy(content.scenes)
        scenes[1]["image_asset_id"] = baseline["scenes"][1]["image_asset_id"]
        scenes[0]["audio_asset_ids"] = []
        content.scenes = scenes
        session.commit()
    missing_audio = client.post(f"/api/v1/contents/{content_id}/generate", json={"stage": "images"})
    assert missing_audio.status_code == 409 and missing_audio.json()["code"] == "AUDIO_REQUIRED"


def test_redraw_checks_required_call_budget_and_media_tools_before_mutation(
    client, engine, config, content_id, monkeypatch
):
    _enable_image_model(engine, daily_calls=1)
    baseline = _ready_content(engine, config, content_id)
    monkeypatch.setattr("app.api.shutil.which", lambda name: f"/fixture/{name}")
    exhausted = client.post(f"/api/v1/contents/{content_id}/generate", json={"stage": "images"})
    assert exhausted.status_code == 409 and exhausted.json()["code"] == "DAILY_CALL_LIMIT"
    with Session(engine) as session:
        content = session.get(Content, content_id)
        assert content.version == baseline["version"]
        setting = session.get(Setting, "app")
        values = copy.deepcopy(setting.value)
        values["limits"]["daily_calls"] = 20
        setting.value = values
        session.commit()

    monkeypatch.setattr("app.api.shutil.which", lambda _name: None)
    unavailable = client.post(
        f"/api/v1/contents/{content_id}/generate",
        json={"stage": "images", "scene_id": "scene-1"},
    )
    assert unavailable.status_code == 409 and unavailable.json()["code"] == "FFMPEG_REQUIRED"
    with Session(engine) as session:
        content = session.get(Content, content_id)
        assert content.version == baseline["version"]
        assert content.scenes == baseline["scenes"]
