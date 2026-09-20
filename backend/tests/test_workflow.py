from __future__ import annotations

import base64
import copy
import io
import json
import subprocess
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from PIL import Image
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from test_api import prepared_record

from app.integrations.media import probe_media
from app.models import Asset, Content, Job, JobStep, Metric, Report, Setting, Source, Topic
from app.services import AppError, enqueue
from app.worker import Worker
from app.workflow import TaskContext, make_report


def enable_fixture_models(engine):
    with Session(engine) as session:
        setting = session.get(Setting, "app")
        values = copy.deepcopy(setting.value)
        values["video"].update(width=320, height=568, fps=24, target_seconds=15)
        values["limits"].update(daily_calls=20, daily_videos=3)
        for kind in values["models"]:
            values["models"][kind].update(
                enabled=True, base_url="https://fixture.example/v1", model=f"fixture-{kind}"
            )
        setting.value = values
        session.commit()


def test_real_codec_pipeline_with_explicit_fixture_model_transport(
    client, engine, config, content_id, tmp_path, monkeypatch
):
    """Real HTTP protocol parsing + database + worker + FFmpeg. Model responses/tone are fixtures, not live AI/voice acceptance."""
    enable_fixture_models(engine)
    audio = tmp_path / "fixture-tone.mp3"
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=10",
            "-ac",
            "1",
            "-ar",
            "24000",
            "-y",
            str(audio),
        ],
        check=True,
        timeout=20,
    )
    picture = io.BytesIO()
    Image.new("RGB", (320, 568), "#315bff").save(picture, "PNG")
    script = {
        "title": "本地合成测试",
        "angle": "用于回归验证",
        "selection_reason": "确定性测试夹具",
        "facts": [
            {
                "claim": "这是一条测试陈述",
                "source_url": "https://example.org/test-topic",
                "verified": True,
                "notes": "夹具",
            }
        ],
        "platforms": {
            platform: {
                "title": "测试标题",
                "description": "不是实际新闻或发布，AI辅助制作。",
                "tags": ["测试"],
            }
            for platform in ("douyin", "xiaohongshu")
        },
        "scenes": [
            {
                "narration": "用于测试的旁白。请保持完整镜头。",
                "caption": "中文字幕测试",
                "highlight_terms": ["测试"],
                "image_prompt": "A test fixture",
            }
        ],
    }
    observed = []
    original = httpx.Client

    def respond(request):
        observed.append(request.url.path)
        if request.url.path.endswith("chat/completions"):
            prompt = json.loads(request.content)["messages"][0]["content"]
            assert "小红书标题最多20个中文字符" in prompt
            assert "抖音标题最多30个中文字符" in prompt
            assert "描述与#话题合计不超过1000个字符" in prompt
            assert "不得包含违法犯罪" in prompt
            assert "AI辅助制作" in prompt
            return httpx.Response(
                200,
                json={
                    "choices": [{"message": {"content": json.dumps(script, ensure_ascii=False)}}],
                    "usage": {"total_tokens": 20},
                },
            )
        if request.url.path.endswith("images/generations"):
            return httpx.Response(
                200, json={"data": [{"b64_json": base64.b64encode(picture.getvalue()).decode()}]}
            )
        if request.url.path.endswith("audio/speech"):
            assert json.loads(request.content)["input"] == script["scenes"][0]["narration"]
            return httpx.Response(200, content=audio.read_bytes(), headers={"content-type": "audio/mpeg"})
        raise AssertionError("Unexpected fixture endpoint")

    monkeypatch.setattr(
        "app.providers.httpx.Client",
        lambda **kwargs: original(transport=httpx.MockTransport(respond), **kwargs),
    )
    monkeypatch.setattr(
        "app.integrations.sources.fetch_article_excerpt",
        lambda url: {
            "text": "这是仅用于测试的原文材料。" * 30,
            "url": url,
            "fetched_at": datetime.now(UTC).isoformat(),
        },
    )
    response = client.post(
        f"/api/v1/contents/{content_id}/generate",
        json={"stage": "all"},
        headers={"Idempotency-Key": "fixture-whole-pipeline"},
    )
    assert response.status_code == 202, response.text
    worker = Worker(config, engine=engine)
    assert worker.run_once()
    job = client.get(f"/api/v1/jobs/{response.json()['job_id']}").json()
    assert job["status"] == "SUCCEEDED", job
    content = client.get(f"/api/v1/contents/{content_id}").json()
    assert content["status"] == "REVIEW_REQUIRED"
    assert content["media_version"] == content["version"]
    assert content["latest_quality_review"]["video_asset_id"] == content["video_asset_id"]
    assert content["latest_quality_review"]["status"] == "REVISE"
    assert job["result"]["quality_review_id"] == content["latest_quality_review"]["id"]
    assert content["script"]["facts"][0]["verified"] is False
    assert content["scenes"][0]["motion"] == "auto"
    assert content["scenes"][0]["transition"] == "fade"
    assert len(observed) == 3
    with Session(engine) as session:
        video = session.get(Asset, content["video_asset_id"])
        info = probe_media(config.data_dir / video.relative_path)
        assert (info["width"], info["height"], info["video_codec"], info["audio_codec"]) == (
            320,
            568,
            "h264",
            "aac",
        )
        assert abs(info["duration"] - 10) < 0.1
        before_assets = session.scalar(select(func.count(Asset.id)))
    assert "用于测试的旁白" in client.get(f"/api/v1/assets/{content['subtitle_asset_id']}/file").text
    assert client.get(f"/api/v1/contents/{content_id}/export").headers["content-type"] == "application/zip"
    rerender = client.post(f"/api/v1/contents/{content_id}/generate", json={"stage": "render"})
    assert rerender.status_code == 202, rerender.text
    assert worker.run_once()
    current = client.get(f"/api/v1/contents/{content_id}").json()
    assert current["video_asset_id"] == content["video_asset_id"]
    assert current["media_version"] == current["version"] == content["version"] + 1
    assert len(observed) == 3  # Render reuse makes no additional model call.
    with Session(engine) as session:
        assert session.scalar(select(func.count(Asset.id))) == before_assets


@pytest.mark.parametrize("source_kind", ["x", "x_tikhub"])
def test_short_x_post_is_used_as_complete_script_evidence_without_article_fetch(
    client, engine, config, content_id, monkeypatch, source_kind
):
    enable_fixture_models(engine)
    with Session(engine) as session:
        content = session.get(Content, content_id)
        topic = session.get(Topic, content.topic_id)
        source = session.get(Source, topic.source_id)
        source.kind = source_kind
        source.url = "https://x.com/OpenAI"
        topic.url = "https://x.com/OpenAI/status/123456789"
        topic.summary = "Qwen 发布新的 LLM 推理方法。"
        session.commit()
    monkeypatch.setattr(
        "app.integrations.sources.fetch_article_excerpt",
        lambda *_args, **_kwargs: pytest.fail("X short post must not be fetched as an HTML article"),
    )
    script = {
        "title": "X 短帖测试",
        "angle": "只使用已采集正文",
        "selection_reason": "测试",
        "facts": [
            {
                "claim": "Qwen 发布新的 LLM 推理方法",
                "source_url": "https://x.com/OpenAI/status/123456789",
                "verified": False,
                "notes": "待人工核验",
            }
        ],
        "platforms": {
            platform: {
                "title": "短帖测试",
                "description": "测试，AI辅助制作。",
                "tags": ["测试"],
            }
            for platform in ("douyin", "xiaohongshu")
        },
        "scenes": [
            {
                "narration": "解释这条短帖。",
                "caption": "短帖",
                "highlight_terms": ["短帖"],
                "image_prompt": "展示推理流程",
                "motion": "none",
                "transition": "cut",
            }
        ],
    }
    calls = []
    monkeypatch.setattr(
        "app.workflow.invoke_model",
        lambda *_args, **kwargs: calls.append(kwargs) or {"text": json.dumps(script, ensure_ascii=False)},
    )
    submitted = client.post(f"/api/v1/contents/{content_id}/generate", json={"stage": "script"})
    assert submitted.status_code == 202, submitted.text
    assert Worker(config, engine=engine).run_once()
    assert len(calls) == 1
    request = json.loads(calls[0]["payload"]["messages"][1]["content"])
    assert request["evidence"]["summary"] == "Qwen 发布新的 LLM 推理方法。"


def test_old_worker_cannot_overwrite_new_step_state(engine, config):
    with Session(engine) as session:
        job = enqueue(session, kind="GENERATE_REPORT", payload={})
        job.status, job.lease_owner = "RUNNING", "old-owner"
        job.lease_until = (datetime.now(UTC) + timedelta(minutes=1)).isoformat()
        session.commit()
        identifier = job.id
    ctx = TaskContext(engine, config, identifier, "old-owner")
    with pytest.raises(AppError), ctx.step("同名阶段", 10):
        with Session(engine) as session:
            session.get(Job, identifier).lease_owner = "new-owner"
            session.scalar(select(JobStep)).status = "SUCCEEDED"
            session.commit()
        raise AppError("LATE_FAILURE", "旧执行迟到的异常")
    with Session(engine) as session:
        assert session.scalar(select(JobStep)).status == "SUCCEEDED"


@pytest.mark.parametrize(
    "terms,expected",
    [
        (["更低API价格", "API价格", "成本"], ["API价格", "成本"]),
        (["9月14日", "50%费率"], []),
        (["API价格", "成本", "API价格"], ["API价格", "成本"]),
    ],
)
def test_generated_nonliteral_highlights_keep_script_and_report_visible_warning(
    client, engine, config, content_id, monkeypatch, terms, expected
):
    enable_fixture_models(engine)
    with Session(engine) as session:
        content = session.get(Content, content_id)
        topic = session.get(Topic, content.topic_id)
        topic.summary = "确定性回归材料。" * 50
        script = {**copy.deepcopy(content.script), "title": "高亮回归"}
        session.commit()
    script["facts"][0].update(source_url="https://unverified.example/claim", verified=True)
    original_scene = {
        "narration": "更低的API价格。九月十四日起，费率只有一半。",
        "caption": "成本回馈用户",
        "highlight_terms": terms,
        "image_prompt": "成本变化示意图",
        "motion": "none",
        "transition": "cut",
    }
    script["scenes"] = [original_scene]
    monkeypatch.setattr("app.workflow.invoke_model", lambda *args, **kwargs: {"text": json.dumps(script)})
    response = client.post(f"/api/v1/contents/{content_id}/generate", json={"stage": "script"})
    assert response.status_code == 202, response.text
    assert Worker(config, engine=engine).run_once()
    job = client.get(f"/api/v1/jobs/{response.json()['job_id']}").json()
    assert job["status"] == "SUCCEEDED", job
    current = client.get(f"/api/v1/contents/{content_id}").json()
    scene = current["scenes"][0]
    assert scene["highlight_terms"] == expected
    for name in ("narration", "caption", "image_prompt", "motion", "transition"):
        assert scene[name] == original_scene[name]
    assert current["script"]["facts"][0]["source_url"] == ""
    assert current["script"]["facts"][0]["verified"] is False
    detail = next(step["detail"] for step in job["steps"] if step["name"] == "生成文案与分镜")
    invalid = [term for term in terms if term not in expected]
    if invalid:
        assert job["result"]["highlight_warnings"] == [{"scene_id": "scene-1", "terms": invalid}]
        assert "已取消高亮并保留普通字幕" in detail
        assert all(term in detail for term in invalid)
    else:
        assert job["result"]["highlight_warnings"] == []
        assert detail == "已完成"
    # Manual edits still enforce exact matches; generated-output context is never supplied by the API.
    scene["highlight_terms"] = ["旁白中不存在"]
    manual = client.patch(
        f"/api/v1/contents/{content_id}", json={"version": current["version"], "scenes": [scene]}
    )
    assert manual.status_code == 422


@pytest.mark.parametrize(
    "changes",
    [
        {"highlight_terms": "错误的字符串结构"},
        {"highlight_terms": ["长" * 41]},
        {"highlight_terms": ["字"] * 9},
        {"highlight_terms": ["不存在"], "motion": "execute_command"},
    ],
)
def test_highlight_fallback_does_not_accept_invalid_structure(
    client, engine, config, content_id, monkeypatch, changes
):
    enable_fixture_models(engine)
    with Session(engine) as session:
        content = session.get(Content, content_id)
        session.get(Topic, content.topic_id).summary = "确定性回归材料。" * 50
        script = {**copy.deepcopy(content.script), "title": "结构回归"}
        session.commit()
    script["scenes"] = [{"narration": "完整的旁白。", "image_prompt": "流程示意", **changes}]
    monkeypatch.setattr("app.workflow.invoke_model", lambda *args, **kwargs: {"text": json.dumps(script)})
    response = client.post(f"/api/v1/contents/{content_id}/generate", json={"stage": "script"})
    assert response.status_code == 202, response.text
    assert Worker(config, engine=engine).run_once()
    job = client.get(f"/api/v1/jobs/{response.json()['job_id']}").json()
    assert job["status"] == "FAILED"
    assert job["error_code"] == "MODEL_OUTPUT_INVALID"
    assert client.get(f"/api/v1/contents/{content_id}").json()["scenes"] == []


def test_report_checkpoint_survives_crash_without_duplicate_row(
    client, engine, config, approved_content, monkeypatch
):
    enable_fixture_models(engine)
    publication = prepared_record(client, engine, approved_content)
    client.post(
        f"/api/v1/publications/{publication['publication_id']}/confirm",
        json={"confirmed": True, "work_url": "https://www.douyin.com/video/1234567890123456789"},
    )
    client.post("/api/v1/metrics", json={"publication_id": publication["publication_id"], "likes": 10})
    response = client.post("/api/v1/reports")
    assert response.status_code == 202, response.text
    worker = Worker(config, engine=engine)
    claimed = worker.claim()
    calls = []
    monkeypatch.setattr(
        "app.workflow.invoke_model",
        lambda *args, **kwargs: calls.append(True) or {"text": "测试报告，有一条夹具指标"},
    )
    ctx = TaskContext(engine, config, claimed.id, worker.owner)
    first = make_report(ctx)
    assert make_report(ctx)["report_id"] == first["report_id"]
    assert calls == [True]
    with Session(engine) as session:
        job = session.get(Job, claimed.id)
        job.lease_until = (datetime.now(UTC) - timedelta(seconds=60)).isoformat()
        session.commit()
    assert worker.claim() is None
    with Session(engine) as session:
        assert session.get(Job, claimed.id).status == "SUCCEEDED"
        assert session.scalar(select(func.count(Report.id))) == 1


def test_metric_checkpoint_prevents_repeated_device_read(
    client, engine, config, approved_content, monkeypatch
):
    from app.workflow import read_metrics

    publication = prepared_record(client, engine, approved_content)
    client.post(
        f"/api/v1/publications/{publication['publication_id']}/confirm",
        json={"confirmed": True, "work_url": "https://www.douyin.com/video/1234567890123456789"},
    )
    submitted = client.post(f"/api/v1/publications/{publication['publication_id']}/collect-metrics")
    assert submitted.status_code == 202
    worker = Worker(config, queue="device", engine=engine)
    job = worker.claim()
    reads = []
    monkeypatch.setattr(
        "app.integrations.device.collect_metrics",
        lambda **kwargs: (
            reads.append(kwargs)
            or {
                "views": None,
                "likes": 0,
                "comments": None,
                "favorites": None,
                "shares": None,
                "raw": {"fixture": True},
            }
        ),
    )
    ctx = TaskContext(engine, config, job.id, worker.owner)
    result = read_metrics(ctx)
    assert read_metrics(ctx)["metric_id"] == result["metric_id"]
    with Session(engine) as session:
        assert session.scalar(select(func.count(Metric.id))) == 1
        metric = session.scalar(select(Metric))
        assert metric.views is None and metric.likes == 0
        assert metric.content_title == approved_content["title"]
        session.get(Job, job.id).lease_until = (datetime.now(UTC) - timedelta(seconds=60)).isoformat()
        session.commit()
    assert worker.claim() is None
    assert len(reads) == 1
    assert reads[0]["content_title"] == approved_content["script"]["platforms"]["douyin"]["title"]
