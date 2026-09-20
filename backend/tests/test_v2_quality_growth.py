from __future__ import annotations

import copy
import json
import subprocess

from conftest import fixture_asset
from PIL import Image
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.integrations.media import review_render_quality
from app.models import (
    Account,
    Approval,
    Asset,
    Content,
    ContentRevision,
    Job,
    JobStep,
    Metric,
    ModelCall,
    Publication,
    QualityReview,
    Setting,
    uid,
)
from app.services import content_snapshot, digest, enqueue, legacy_content_snapshot, save_revision
from app.worker import Worker


def enable_models(engine):
    with Session(engine) as session:
        setting = session.get(Setting, "app")
        values = copy.deepcopy(setting.value)
        values["limits"].update(daily_calls=100, daily_videos=10)
        for kind in values["models"]:
            values["models"][kind].update(
                enabled=True,
                base_url="https://fixture.example/v1",
                model=f"fixture-{kind}",
            )
        setting.value = values
        session.commit()


def pass_report(video_asset_id: str) -> dict:
    return {
        "version": "1.0",
        "status": "PASS",
        "video_asset_id": video_asset_id,
        "checks": {},
        "issues": [],
        "warnings": [],
        "recommended_action": "present_for_review",
    }


def compliance_checks() -> dict:
    return {
        "source_rights_confirmed": True,
        "no_unauthorized_material": True,
        "claims_not_exaggerated": True,
        "no_offplatform_diversion": True,
        "no_unverifiable_guarantees": True,
        "ai_disclosure_confirmed": True,
        "platform_rules_checked": True,
        "visuals_manually_reviewed": True,
    }


def test_presets_are_versioned_read_only_and_unknown_ids_are_rejected(client, content_id):
    response = client.get("/api/v1/presets")
    assert response.status_code == 200
    assert [item["id"] for item in response.json()["items"]] == [
        "tech_brief_video_v1",
        "data_explainer_video_v1",
        "avatar_commentary_video_v1",
    ]
    assert all(item["version"] == 1 for item in response.json()["items"])
    assert client.post("/api/v1/presets", json={}).status_code == 405
    rejected = client.patch(
        f"/api/v1/contents/{content_id}",
        json={"version": 1, "preset_id": "unknown-preset"},
    )
    assert rejected.status_code == 404
    assert rejected.json()["code"] == "PRESET_NOT_FOUND"


def test_all_from_empty_rejects_before_enqueue_when_image_model_is_unavailable(
    client, engine, content_id
):
    enable_models(engine)
    with Session(engine) as session:
        setting = session.get(Setting, "app")
        values = copy.deepcopy(setting.value)
        values["models"]["image"]["enabled"] = False
        setting.value = values
        session.commit()
    rejected = client.post(f"/api/v1/contents/{content_id}/generate", json={"stage": "all"})
    assert rejected.status_code == 409
    assert rejected.json()["code"] == "MODEL_NOT_CONFIGURED"
    with Session(engine) as session:
        assert session.scalar(select(func.count(Job.id))) == 0
        assert session.scalar(select(func.count(ModelCall.id))) == 0


def test_brief_has_three_concepts_gates_script_and_selected_concept_constrains_plan(
    client, engine, config, content_id, monkeypatch
):
    enable_models(engine)
    calls = []
    brief_output = {
        "target_audience": "AI 产品与研发人员",
        "content_goal": "解释测试选题的价值与边界",
        "concepts": [
            {
                "id": f"c{index}",
                "title": f"方案 {index}",
                "hook": f"钩子 {index}",
                "narrative_structure": structure,
                "visual_approach": "使用多种视觉",
                "core_message": "核心信息",
                "why_this_works": "用户能快速理解",
            }
            for index, structure in enumerate(
                ("problem_solution", "comparison", "data_narrative"), start=1
            )
        ],
    }
    script_output = {
        "title": "已选方案脚本",
        "angle": "测试角度",
        "selection_reason": "基于真实来源",
        "facts": [
            {
                "claim": "用于验证审核的测试陈述",
                "source_url": "https://example.org/test-topic",
                "verified": False,
                "notes": "待人工核实",
            }
        ],
        "platforms": {
            platform: {
                "title": "测试脚本",
                "description": "仅供测试，AI辅助制作。",
                "tags": ["测试"],
            }
            for platform in ("douyin", "xiaohongshu")
        },
        "scenes": [
            {
                "narration": "这是测试旁白。",
                "caption": "测试",
                "highlight_terms": ["测试"],
                "image_prompt": "竖屏测试画面",
                "motion": "none",
                "transition": "cut",
                "scene_type": "image",
                "visual_data": {},
                "hero_moment": True,
                "shot_intent": "建立主题",
            }
        ],
    }

    def model(*_args, **kwargs):
        calls.append(kwargs)
        payload = brief_output if kwargs["scope"].endswith(":brief") else script_output
        return {"text": json.dumps(payload, ensure_ascii=False)}

    monkeypatch.setattr("app.workflow.invoke_model", model)
    monkeypatch.setattr(
        "app.integrations.sources.fetch_article_excerpt",
        lambda url: {"text": "足够长的测试来源。" * 50, "url": url, "fetched_at": "2026-09-13T00:00:00+00:00"},
    )
    submitted = client.post(
        f"/api/v1/contents/{content_id}/generate",
        json={"stage": "brief"},
        headers={"Idempotency-Key": "brief-v2"},
    )
    assert submitted.status_code == 202, submitted.text
    assert Worker(config, engine=engine).run_once()
    content = client.get(f"/api/v1/contents/{content_id}").json()
    assert len(content["brief"]["concepts"]) == 3
    assert len({item["id"] for item in content["brief"]["concepts"]}) == 3
    assert all(item["estimated_cost_usd"] is None for item in content["brief"]["concepts"])
    assert all(
        item["estimated_calls"] == {"text": 1, "image": 7, "speech": 7}
        for item in content["brief"]["concepts"]
    )
    request = json.loads(calls[0]["payload"]["messages"][1]["content"])
    assert set(request) == {"schema_example", "topic", "template", "preset", "target_platform"}
    assert all(
        concept["estimated_calls"] == {"text": 1, "image": 7, "speech": 7}
        for concept in request["schema_example"]["concepts"]
    )
    blocked = client.post(f"/api/v1/contents/{content_id}/generate", json={"stage": "script"})
    assert blocked.status_code == 409
    assert blocked.json()["code"] == "BRIEF_SELECTION_REQUIRED"
    selected = client.post(
        f"/api/v1/contents/{content_id}/brief/select",
        json={"version": content["version"], "concept_id": "c2"},
    )
    assert selected.status_code == 200, selected.text
    assert selected.json()["version"] == content["version"] + 1
    assert selected.json()["brief"]["selected_concept_id"] == "c2"
    assert selected.json()["scenes"] == [] and selected.json()["video_asset_id"] is None
    scripted = client.post(f"/api/v1/contents/{content_id}/generate", json={"stage": "script"})
    assert scripted.status_code == 202, scripted.text
    assert Worker(config, engine=engine).run_once()
    script_request = json.loads(calls[-1]["payload"]["messages"][1]["content"])
    assert script_request["creative_constraint"]["id"] == "c2"
    assert script_request["evidence"]["url"] == "https://example.org/test-topic"


def test_brief_on_published_version_creates_new_version_and_preserves_history(
    client, engine, rendered_content
):
    enable_models(engine)
    with Session(engine) as session:
        content = session.get(Content, rendered_content)
        content.status = "APPROVED"
        save_revision(session, content)
        old_snapshot = copy.deepcopy(content_snapshot(content))
        account = Account(
            platform="douyin",
            display_name="版本历史测试",
            account_identifier="version-history-fixture",
            device_serial="version-history-device",
        )
        session.add(account)
        session.flush()
        publication = Publication(
            content_id=content.id,
            content_version=content.version,
            content_title=content.title,
            account_id=account.id,
            platform="douyin",
            status="PUBLISHED",
            snapshot={**old_snapshot, "account": {"id": account.id}, "auto_publish": False},
        )
        session.add(publication)
        session.commit()
        old_version = content.version
        publication_id = publication.id
        asset_ids = set(session.scalars(select(Asset.id).where(Asset.content_id == content.id)))
        old_revision = copy.deepcopy(
            session.scalar(
                select(ContentRevision).where(
                    ContentRevision.content_id == content.id,
                    ContentRevision.version == old_version,
                )
            ).snapshot
        )
    submitted = client.post(f"/api/v1/contents/{rendered_content}/generate", json={"stage": "brief"})
    assert submitted.status_code == 202, submitted.text
    with Session(engine) as session:
        content = session.get(Content, rendered_content)
        job = session.get(Job, submitted.json()["job_id"])
        assert content.version == old_version + 1
        assert job.payload["version"] == content.version
        assert content.status == "GENERATING"
        assert content.brief == {}
        assert content.media_version is None and content.video_asset_id is None
        assert set(session.scalars(select(Asset.id).where(Asset.content_id == content.id))) == asset_ids
        assert session.get(Publication, publication_id).snapshot == {
            **old_snapshot,
            "account": {"id": session.get(Publication, publication_id).account_id},
            "auto_publish": False,
        }
        revision = session.scalar(
            select(ContentRevision).where(
                ContentRevision.content_id == content.id,
                ContentRevision.version == old_version,
            )
        )
        assert revision.snapshot == old_revision == old_snapshot


def test_all_scene_types_validate_and_unsafe_card_text_is_rejected(client, content_id):
    visuals = {
        "image": {},
        "text_card": {"headline": "纯文本"},
        "stat_card": {"headline": "数据", "value": "42%"},
        "quote_card": {"headline": "引语", "body": "纯文本引语"},
    }
    scenes = [
        {
            "id": f"scene-{scene_type}",
            "order": index,
            "narration": "安全旁白。",
            "caption": "安全标题",
            "image_prompt": "测试配图" if scene_type == "image" else "",
            "scene_type": scene_type,
            "visual_data": visuals[scene_type],
            "hero_moment": index == 0,
            "shot_intent": "测试镜头意图",
        }
        for index, scene_type in enumerate(("image", "text_card", "stat_card", "quote_card"))
    ]
    response = client.patch(
        f"/api/v1/contents/{content_id}", json={"version": 1, "scenes": scenes}
    )
    assert response.status_code == 200, response.text
    assert [scene["scene_type"] for scene in response.json()["scenes"]] == [
        "image",
        "text_card",
        "stat_card",
        "quote_card",
    ]
    unsafe = copy.deepcopy(response.json()["scenes"])
    unsafe[1]["visual_data"] = {"headline": "$(rm -rf /tmp/example)"}
    rejected = client.patch(
        f"/api/v1/contents/{content_id}",
        json={"version": response.json()["version"], "scenes": unsafe},
    )
    assert rejected.status_code == 422


def test_approval_cannot_bypass_an_unselected_brief(client, engine, rendered_content):
    with Session(engine) as session:
        content = session.get(Content, rendered_content)
        content.brief = {
            "version": "1.0",
            "target_audience": "测试受众",
            "content_goal": "测试目标",
            "target_platform": "both",
            "quality_floor": "presentable",
            "concepts": [
                {
                    "id": f"c{index}",
                    "title": f"方案 {index}",
                    "hook": f"钩子 {index}",
                    "narrative_structure": "problem_solution",
                    "visual_approach": "测试视觉",
                    "core_message": "测试信息",
                    "why_this_works": "测试理由",
                    "estimated_calls": {"text": 1, "image": 7, "speech": 7},
                    "estimated_cost_usd": None,
                }
                for index in range(1, 4)
            ],
            "selected_concept_id": None,
            "selected_at": None,
        }
        session.commit()
        version = content.version
    rejected = client.post(
        f"/api/v1/contents/{rendered_content}/approve",
        json={"version": version, "compliance_confirmed": True},
    )
    assert rejected.status_code == 409
    assert rejected.json()["code"] == "BRIEF_SELECTION_REQUIRED"


def test_three_card_types_render_locally_without_image_model_calls(
    client, engine, config, content_id
):
    with Session(engine) as session:
        audio = fixture_asset(session, config, content_id, "audio")
        session.commit()
        audio_id = audio.id
    scenes = []
    for index, scene_type in enumerate(("text_card", "stat_card", "quote_card")):
        visual = {"headline": f"卡片 {index + 1}", "body": "用于本地绘制的纯文本"}
        if scene_type == "stat_card":
            visual["value"] = "42%"
        scenes.append(
            {
                "id": f"card-{index}",
                "order": index,
                "narration": f"卡片 {index + 1} 的测试旁白。",
                "caption": f"卡片 {index + 1}",
                "image_prompt": "",
                "image_asset_id": None,
                "audio_asset_ids": [audio_id],
                "scene_type": scene_type,
                "visual_data": visual,
                "hero_moment": index == 1,
                "shot_intent": "展示本地卡片",
            }
        )
    edited = client.patch(
        f"/api/v1/contents/{content_id}", json={"version": 1, "scenes": scenes}
    )
    assert edited.status_code == 200, edited.text
    submitted = client.post(f"/api/v1/contents/{content_id}/generate", json={"stage": "assets"})
    assert submitted.status_code == 202, submitted.text
    assert Worker(config, engine=engine).run_once()
    current = client.get(f"/api/v1/contents/{content_id}").json()
    with Session(engine) as session:
        assert session.scalar(select(func.count(ModelCall.id))) == 0
        for scene in current["scenes"]:
            asset = session.get(Asset, scene["image_asset_id"])
            with Image.open(config.data_dir / asset.relative_path) as image:
                assert image.size == (1080, 1920)


def _make_video(path, *, color="blue", size="320x568", audio=True, fade=False):
    command = [
        "ffmpeg",
        "-v",
        "error",
        "-f",
        "lavfi",
        "-i",
        f"color=c={color}:s={size}:r=24:d=2",
    ]
    if audio:
        command.extend(
            [
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=1000:sample_rate=48000:duration=2",
                "-af",
                "loudnorm=I=-16:TP=-1.5:LRA=11",
                "-c:a",
                "aac",
            ]
        )
    if fade:
        command.extend(["-vf", "fade=t=in:st=0:d=0.35,fade=t=out:st=1.65:d=0.35"])
    command.extend(["-c:v", "libx264", "-pix_fmt", "yuv420p", "-shortest", "-y", str(path)])
    subprocess.run(command, check=True, timeout=30)


def _quality(video, subtitle, *, scenes=None, facts=None, expected=None):
    scenes = scenes or [
        {
            "id": "s1",
            "scene_type": "image",
            "hero_moment": True,
            "shot_intent": "建立主题",
        },
        {
            "id": "s2",
            "scene_type": "text_card",
            "hero_moment": False,
            "shot_intent": "展示结论",
        },
    ]
    return review_render_quality(
        video_path=video,
        subtitle_path=subtitle,
        video_asset_id="fixture-video",
        scenes=scenes,
        facts=facts or [],
        settings=expected or {"width": 320, "height": 568, "fps": 24},
        quality_rules={
            "min_visual_types": 2,
            "max_consecutive_same_type": 2,
            "require_hero_scene": True,
        },
    )


def test_offline_quality_review_passes_normal_media_and_covers_failures(tmp_path):
    video = tmp_path / "normal.mp4"
    black = tmp_path / "black.mp4"
    silent = tmp_path / "silent.mp4"
    subtitle = tmp_path / "normal.srt"
    invalid_subtitle = tmp_path / "invalid.srt"
    _make_video(video)
    _make_video(black, color="black")
    _make_video(silent, audio=False)
    subtitle.write_text("1\n00:00:00,000 --> 00:00:02,000\n测试字幕\n", encoding="utf-8")
    invalid_subtitle.write_text(
        "1\n00:00:01,000 --> 00:00:03,000\n超出视频\n", encoding="utf-8"
    )

    passed = _quality(video, subtitle)
    assert passed["status"] == "PASS", passed
    assert passed["checks"]["visual"]["sample_count"] == 4
    assert passed["checks"]["audio"]["integrated_lufs"] is not None
    wrong_size = _quality(video, subtitle, expected={"width": 1080, "height": 1920, "fps": 24})
    assert wrong_size["status"] == "FAIL"
    assert any(issue["code"] == "MEDIA_SETTINGS_MISMATCH" for issue in wrong_size["issues"])
    black_result = _quality(black, subtitle)
    assert black_result["status"] == "FAIL"
    assert any(issue["code"] == "NEAR_BLACK_FRAME" for issue in black_result["issues"])
    silent_result = _quality(silent, subtitle)
    assert silent_result["status"] == "FAIL"
    assert any(issue["code"] == "MEDIA_STREAM_MISSING" for issue in silent_result["issues"])
    subtitle_result = _quality(video, invalid_subtitle)
    assert subtitle_result["status"] == "FAIL"
    assert any(issue["code"] == "SUBTITLE_EXCEEDS_VIDEO" for issue in subtitle_result["issues"])
    repeated = [
        {"id": f"repeat-{index}", "scene_type": "image", "hero_moment": index == 0, "shot_intent": "重复"}
        for index in range(3)
    ]
    repeated_result = _quality(video, subtitle, scenes=repeated)
    assert repeated_result["status"] == "REVISE"
    assert any(issue["code"] == "SCENE_TYPE_REPETITION" for issue in repeated_result["issues"])
    sourced_cards = [
        {
            "id": "stat",
            "scene_type": "stat_card",
            "visual_data": {"value": "42%", "source_url": "https://example.org/fact"},
            "hero_moment": True,
            "shot_intent": "展示数据",
        },
        {
            "id": "text",
            "scene_type": "text_card",
            "visual_data": {"headline": "结论"},
            "hero_moment": False,
            "shot_intent": "收束内容",
        },
    ]
    unmatched = _quality(video, subtitle, scenes=sourced_cards)
    assert unmatched["status"] == "REVISE"
    assert any(issue["code"] == "FACT_VISUAL_UNMATCHED" for issue in unmatched["issues"])
    matched = _quality(
        video,
        subtitle,
        scenes=sourced_cards,
        facts=[
            {
                "claim": "样本数据为 42%",
                "source_url": "https://example.org/fact",
                "verified": True,
            }
        ],
    )
    assert matched["status"] == "PASS", matched
    faded = tmp_path / "faded-short.mp4"
    _make_video(faded, fade=True)
    faded_result = _quality(faded, subtitle)
    assert faded_result["status"] == "PASS", faded_result
    assert not any(issue["code"] == "NEAR_BLACK_FRAME" for issue in faded_result["issues"])


def test_quality_gate_and_structured_and_legacy_compliance(
    client, engine, rendered_content
):
    with Session(engine) as session:
        content = session.get(Content, rendered_content)
        script = copy.deepcopy(content.script)
        script["facts"][0]["verified"] = True
        content.script = script
        session.add(
            QualityReview(
                content_id=content.id,
                content_version=content.version,
                video_asset_id=content.video_asset_id,
                status="REVISE",
                report={"version": "1.0", "status": "REVISE"},
            )
        )
        session.commit()
        version = content.version
    rejected = client.post(
        f"/api/v1/contents/{rendered_content}/approve",
        json={
            "version": version,
            "compliance_confirmed": True,
            "compliance_checks": compliance_checks(),
        },
    )
    assert rejected.status_code == 409
    assert rejected.json()["code"] == "QUALITY_REVIEW_NOT_PASSED"
    with Session(engine) as session:
        review = session.scalar(select(QualityReview))
        review.status = "PASS"
        review.report = pass_report(review.video_asset_id)
        session.commit()
    approved = client.post(
        f"/api/v1/contents/{rendered_content}/approve",
        json={
            "version": version,
            "notes": "八项已复核",
            "compliance_confirmed": True,
            "compliance_checks": compliance_checks(),
        },
    )
    assert approved.status_code == 200, approved.text
    assert approved.json()["latest_quality_review"]["status"] == "PASS"
    with Session(engine) as session:
        approval = session.scalar(select(Approval).order_by(Approval.created_at.desc()))
        assert approval.compliance["legacy_boolean_only"] is False
        assert all(approval.compliance["checks"].values())
        content = session.get(Content, rendered_content)
        content.status = "REVIEW_REQUIRED"
        session.commit()
    legacy = client.post(
        f"/api/v1/contents/{rendered_content}/approve",
        json={"version": version, "compliance_confirmed": True},
    )
    assert legacy.status_code == 200, legacy.text
    with Session(engine) as session:
        approval = session.scalar(select(Approval).order_by(Approval.created_at.desc()))
        assert approval.compliance["legacy_boolean_only"] is True
        assert approval.compliance["checks"] == {}


def test_historical_approval_without_v2_quality_report_remains_publishable(
    client, engine, rendered_content
):
    with Session(engine) as session:
        content = session.get(Content, rendered_content)
        content.status = "APPROVED"
        session.add(
            Approval(
                content_id=content.id,
                version=content.version,
                decision="APPROVED",
                snapshot_hash=digest(legacy_content_snapshot(content)),
                notes="V1 historical approval",
                compliance={},
            )
        )
        session.commit()
        version = content.version
    account = client.post(
        "/api/v1/accounts",
        json={
            "platform": "douyin",
            "display_name": "历史审批测试",
            "account_identifier": "historical-fixture",
            "device_serial": "historical-device",
        },
    )
    assert account.status_code == 201, account.text
    submitted = client.post(
        "/api/v1/publications",
        json={
            "content_id": rendered_content,
            "content_version": version,
            "account_id": account.json()["id"],
        },
    )
    assert submitted.status_code == 202, submitted.text


def test_strategy_crud_public_urls_associations_and_null_metric_deltas(
    client, engine, content_id
):
    scores = {
        "demand": 5,
        "production": 4,
        "delivery": 3,
        "presentation": 2,
        "compliance": 1,
        "iteration": 0,
    }
    invalid = client.post(
        "/api/v1/strategy/demand-signals",
        json={
            "title": "私网链接",
            "source_kind": "manual",
            "source_url": "http://127.0.0.1/private",
            "evidence": "测试",
            "scores": scores,
        },
    )
    assert invalid.status_code == 422
    demand = client.post(
        "/api/v1/strategy/demand-signals",
        json={
            "title": "公开需求信号",
            "source_kind": "forum",
            "source_url": "https://example.org/demand",
            "evidence": "人工录入证据",
            "scores": scores,
        },
    )
    assert demand.status_code == 201, demand.text
    patched = client.patch(
        f"/api/v1/strategy/demand-signals/{demand.json()['id']}",
        json={"status": "VALIDATED", "source_url": None},
    )
    assert patched.status_code == 200 and patched.json()["source_url"] is None
    benchmark = client.post(
        "/api/v1/strategy/benchmarks",
        json={
            "platform": "douyin",
            "account_name": "公开账号",
            "content_url": "https://www.douyin.com/video/1234567890123456789",
            "title": "对标内容",
            "content_type": "video",
            "analysis": {"hook": "开场明确", "prohibited_copy": "禁止照搬"},
        },
    )
    assert benchmark.status_code == 201, benchmark.text
    with Session(engine) as session:
        account = Account(
            platform="douyin",
            display_name="策略测试",
            account_identifier="strategy-fixture",
            device_serial="strategy-device",
        )
        session.add(account)
        session.flush()
        content = session.get(Content, content_id)
        publication = Publication(
            content_id=content.id,
            content_version=content.version,
            content_title=content.title,
            account_id=account.id,
            platform="douyin",
            status="PUBLISHED",
            snapshot={},
        )
        session.add(publication)
        session.flush()
        session.add_all(
            [
                Metric(
                    publication_id=publication.id,
                    platform="douyin",
                    content_title=content.title,
                    views=None,
                    likes=0,
                    comments=None,
                    favorites=None,
                    shares=None,
                    source="manual",
                    raw={"secret_provider_body": "must-not-leak"},
                    observed_at="2026-09-13T00:00:00+00:00",
                ),
                Metric(
                    publication_id=publication.id,
                    platform="douyin",
                    content_title=content.title,
                    views=10,
                    likes=5,
                    comments=None,
                    favorites=None,
                    shares=None,
                    source="manual",
                    raw={},
                    observed_at="2026-09-14T00:00:00+00:00",
                ),
            ]
        )
        session.commit()
        publication_id = publication.id
    missing = client.post(
        "/api/v1/strategy/experiments",
        json={
            "name": "无效关联",
            "hypothesis": "测试",
            "variable": "title",
            "content_ids": ["missing-content"],
        },
    )
    assert missing.status_code == 422
    assert missing.json()["code"] == "EXPERIMENT_CONTENT_NOT_FOUND"
    experiment = client.post(
        "/api/v1/strategy/experiments",
        json={
            "name": "标题实验",
            "hypothesis": "新标题会改善真实指标",
            "variable": "title",
            "status": "RUNNING",
            "content_ids": [content_id],
            "publication_ids": [publication_id],
            "observation_windows": ["2h", "24h", "发布后一周"],
        },
    )
    assert experiment.status_code == 201, experiment.text
    metric = experiment.json()["publication_metrics"][0]
    assert metric["latest_metric"]["views"] == 10
    assert "raw" not in metric["latest_metric"]
    assert metric["delta"]["views"] is None
    assert metric["delta"]["likes"] == 5
    completed = client.patch(
        f"/api/v1/strategy/experiments/{experiment.json()['id']}",
        json={"status": "COMPLETED", "conclusion": "已完成", "next_action": "continue"},
    )
    assert completed.status_code == 200
    assert completed.json()["status"] == "COMPLETED"


def test_job_timeline_and_cost_exclude_provider_bodies_and_urls(client, engine):
    with Session(engine) as session:
        job = enqueue(session, kind="GENERATE_CONTENT", payload={"secret": "hidden"})
        job.status = "SUCCEEDED"
        step = JobStep(
            job_id=job.id,
            name="render",
            status="SUCCEEDED",
            detail="已完成",
            started_at="2026-09-13T00:00:00+00:00",
            completed_at="2026-09-13T00:00:01+00:00",
        )
        call = ModelCall(
            job_id=job.id,
            request_key=uid(),
            kind="image",
            model="fixture-image",
            status="SUCCEEDED",
            response={"provider_body": "secret", "signed_url": "https://example.org/?signature=secret"},
            usage={"total_tokens": 12, "provider_payload": "secret", "signed_url": "secret"},
            events=[],
            started_at="2026-09-13T00:00:00+00:00",
        )
        session.add_all([step, call])
        session.commit()
        job_id = job.id
    response = client.get(f"/api/v1/jobs/{job_id}")
    assert response.status_code == 200
    value = response.json()
    assert value["cost"] == {"estimated_usd": None, "actual_usd": None, "status": "UNKNOWN"}
    assert [item["kind"] for item in value["timeline"]] == ["model_call", "step"]
    model_event = value["timeline"][0]
    assert model_event["usage"] == {"total_tokens": 12}
    assert "provider_body" not in response.text and "signature=secret" not in response.text
