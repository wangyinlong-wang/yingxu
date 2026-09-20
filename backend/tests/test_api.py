from __future__ import annotations

import copy
import io
from types import SimpleNamespace

import pytest
from conftest import fixture_asset
from PIL import Image
from sqlalchemy.orm import Session

from app.integrations.errors import IntegrationError
from app.models import Approval, Asset, Content, Job, Publication, QualityReview, now
from app.services import content_snapshot, digest


def account(client, platform="douyin", serial="test-android"):
    response = client.post(
        "/api/v1/accounts",
        json={
            "platform": platform,
            "display_name": "测试绑定",
            "account_identifier": "fixture-account",
            "device_serial": serial,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def prepared_record(client, engine, approved_content):
    binding = account(client)
    response = client.post(
        "/api/v1/publications",
        json={
            "content_id": approved_content["id"],
            "content_version": approved_content["version"],
            "account_id": binding["id"],
        },
    )
    assert response.status_code == 202, response.text
    result = response.json()
    with Session(engine) as session:
        publication = session.get(Publication, result["publication_id"])
        publication.status = "WAITING_USER"
        session.get(Job, result["job_id"]).status = "WAITING_USER"
        session.commit()
    return result


def xhs_media_selection_record(client, engine, approved_content):
    binding = account(client, platform="xiaohongshu", serial="xhs-device")
    response = client.post(
        "/api/v1/publications",
        json={
            "content_id": approved_content["id"],
            "content_version": approved_content["version"],
            "account_id": binding["id"],
        },
    )
    assert response.status_code == 202, response.text
    result = response.json()
    with Session(engine) as session:
        publication = session.get(Publication, result["publication_id"])
        video = session.get(Asset, publication.snapshot["video_asset_id"])
        text = publication.snapshot["script"]["platforms"]["xiaohongshu"]
        caption = " ".join(
            value
            for value in (
                text["title"],
                text["description"],
                " ".join("#" + tag.lstrip("#") for tag in text["tags"]),
            )
            if value
        )
        folder = f"lp_{video.sha256[:16]}"
        publication.status = "NEEDS_ATTENTION"
        publication.evidence = {
            "serial": binding["device_serial"],
            "platform": "xiaohongshu",
            "app_version": "9.46.0",
            "video_sha256": video.sha256,
            "caption": caption,
            "verified_account_identifier": binding["account_identifier"],
            "verified_at": now(),
            "remote_video_path": f"/sdcard/Movies/LocalPublisher/{folder}/{folder}.mp4",
            "remote_cover_path": f"/sdcard/Movies/LocalPublisher/{folder}/{folder}.png",
            "reason": "MEDIA_SELECTION_REQUIRED",
            "page": "picker",
        }
        session.get(Job, result["job_id"]).status = "WAITING_USER"
        session.commit()
    return result


def test_initial_state_is_honest_and_secrets_are_not_returned(client):
    dashboard = client.get("/api/v1/dashboard").json()
    assert dashboard["counts"]["contents"] == 0
    assert dashboard["counts"]["published"] == 0
    assert dashboard["usage"]["cost"] is None
    assert len(client.get("/api/v1/sources").json()["items"]) == 2
    response = client.get("/api/v1/settings")
    assert "fixture-not-a-real-api-key" not in response.text
    assert response.json()["models"]["text"]["key_configured"] is True
    assert response.json()["models"]["text"]["enabled"] is False
    assert response.json()["models"]["image"]["watermark"] is False
    assert response.json()["video"]["timing_mode"] == "natural"


def test_diagnostics_rejects_legacy_adb_version(client, monkeypatch):
    monkeypatch.setattr("app.api.shutil.which", lambda name: f"/fixture/{name}")

    def tool_version(arguments, **_kwargs):
        stdout = (
            "Android Debug Bridge version 1.0.39\nVersion 0.0.1-4500957"
            if arguments[-1] == "version"
            else "ffmpeg version fixture --enable-libass"
        )
        return SimpleNamespace(stdout=stdout)

    monkeypatch.setattr("app.api.subprocess.run", tool_version)
    value = client.get("/api/v1/diagnostics").json()
    assert value["adb"] == {"available": True, "version": "1.0.39", "supported": False}
    assert any("ADB 版本过旧" in warning for warning in value["warnings"])


def test_cross_origin_and_host_are_rejected(client):
    assert (
        client.post("/api/v1/sources", headers={"Origin": "https://evil.example"}, json={}).status_code == 403
    )
    assert client.get("/api/v1/health", headers={"Host": "evil.example"}).status_code == 400
    assert client.get("/api/v1/health", headers={"Origin": "null"}).status_code == 403
    assert client.get("/api/v1/health").headers["X-Content-Type-Options"] == "nosniff"


def test_invalid_settings_do_not_echo_secret_values(client):
    response = client.patch(
        "/api/v1/settings", json={"models": {"text": {"api_key": "never-echo-this-secret"}}}
    )
    assert response.status_code == 422
    assert "never-echo-this-secret" not in response.text
    assert (
        client.patch(
            "/api/v1/settings", json={"models": {"text": {"base_url": "https://user:secret@example.org/v1"}}}
        ).status_code
        == 422
    )
    assert client.patch("/api/v1/settings", json={"limits": {"daily_calls": 0}}).status_code == 422


def test_x_source_urls_are_canonical_and_patch_uses_source_kind(client):
    created = client.post(
        "/api/v1/sources",
        json={"name": "OpenAI X", "kind": "x", "url": "https://www.twitter.com/OpenAI/"},
    )
    assert created.status_code == 201, created.text
    assert created.json()["url"] == "https://x.com/OpenAI"

    patched = client.patch(
        f"/api/v1/sources/{created.json()['id']}",
        json={"url": "https://x.com/AnthropicAI/"},
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["url"] == "https://x.com/AnthropicAI"

    for invalid in (
        "https://x.com/OpenAI/status/123",
        "https://x.com/search?q=AI",
        "https://example.org/OpenAI",
        "https://x.com/account-name-too-long",
        "https://user:pass@x.com/OpenAI",
    ):
        rejected = client.patch(
            f"/api/v1/sources/{created.json()['id']}",
            json={"url": invalid},
        )
        assert rejected.status_code == 422


def test_content_patch_preserves_nested_null_and_conflicts(client, content_id):
    response = client.patch(
        f"/api/v1/contents/{content_id}",
        json={
            "version": 1,
            "scenes": [
                {
                    "id": "scene-a",
                    "order": 0,
                    "narration": "测试旁白。",
                    "caption": "标题",
                    "image_prompt": "测试图",
                    "image_asset_id": None,
                    "audio_asset_ids": [],
                }
            ],
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["version"] == 2
    assert response.json()["scenes"][0]["image_asset_id"] is None
    conflict = client.patch(f"/api/v1/contents/{content_id}", json={"version": 1, "title": "旧版本修改"})
    assert conflict.status_code == 409
    assert conflict.json()["code"] == "VERSION_CONFLICT"


def test_fact_review_preserves_video_and_invalidates_approval(client, rendered_content):
    current = client.get(f"/api/v1/contents/{rendered_content}").json()
    assert (
        client.post(
            f"/api/v1/contents/{rendered_content}/approve", json={"version": current["version"]}
        ).json()["code"]
        == "FACTS_REQUIRE_REVIEW"
    )
    script = copy.deepcopy(current["script"])
    script["facts"][0]["verified"] = True
    changed = client.patch(
        f"/api/v1/contents/{rendered_content}", json={"version": current["version"], "script": script}
    ).json()
    assert changed["video_asset_id"] == current["video_asset_id"]
    assert changed["version"] == changed["media_version"] == current["version"] + 1
    assert changed["status"] == "REVIEW_REQUIRED"
    missing_confirmation = client.post(
        f"/api/v1/contents/{rendered_content}/approve", json={"version": changed["version"]}
    )
    assert missing_confirmation.status_code == 409
    assert missing_confirmation.json()["code"] == "COMPLIANCE_CONFIRMATION_REQUIRED"
    missing_quality = client.post(
        f"/api/v1/contents/{rendered_content}/approve",
        json={"version": changed["version"], "compliance_confirmed": True},
    )
    assert missing_quality.status_code == 409
    assert missing_quality.json()["code"] == "QUALITY_REVIEW_REQUIRED"
    with Session(client.app.state.engine) as session:
        session.add(
            QualityReview(
                content_id=rendered_content,
                content_version=changed["version"],
                video_asset_id=changed["video_asset_id"],
                status="PASS",
                report={"version": "1.0", "status": "PASS"},
            )
        )
        session.commit()
    assert (
        client.post(
            f"/api/v1/contents/{rendered_content}/approve",
            json={"version": changed["version"], "compliance_confirmed": True},
        ).status_code
        == 200
    )
    script["platforms"]["douyin"]["title"] = "新平台标题"
    changed = client.patch(
        f"/api/v1/contents/{rendered_content}", json={"version": changed["version"], "script": script}
    ).json()
    assert changed["status"] == "REVIEW_REQUIRED"
    assert changed["video_asset_id"] == current["video_asset_id"]


def test_visual_edit_requires_new_render(client, approved_content):
    response = client.patch(
        f"/api/v1/contents/{approved_content['id']}",
        json={"version": approved_content["version"], "title": "改变封面的标题"},
    )
    assert response.status_code == 200, response.text
    value = response.json()
    assert value["status"] == "DRAFT" and value["video_asset_id"] is None and value["media_version"] is None


def test_highlight_edit_requires_new_render_but_reuses_scene_media(client, engine, config, approved_content):
    with Session(engine) as session:
        content = session.get(Content, approved_content["id"])
        content.scenes = [
            {
                "id": "scene-keywords",
                "order": 0,
                "narration": "重点是自然语速。",
                "caption": "自然语速",
                "image_prompt": "测试图",
                "image_asset_id": fixture_asset(session, config, content.id, "image").id,
                "audio_asset_ids": [fixture_asset(session, config, content.id, "audio").id],
            }
        ]
        session.commit()
    baseline = client.get(f"/api/v1/contents/{approved_content['id']}").json()
    scenes = copy.deepcopy(baseline["scenes"])
    scenes[0]["highlight_terms"] = [scenes[0]["narration"][:2]]
    response = client.patch(
        f"/api/v1/contents/{approved_content['id']}",
        json={
            "version": approved_content["version"],
            "scenes": scenes,
        },
    )
    assert response.status_code == 200, response.text
    value = response.json()
    assert value["status"] == "DRAFT" and value["video_asset_id"] is None
    assert value["scenes"][0]["image_asset_id"] == baseline["scenes"][0]["image_asset_id"]
    assert value["scenes"][0]["audio_asset_ids"] == baseline["scenes"][0]["audio_asset_ids"]
    scenes[0]["highlight_terms"] = ["绝不出现在这段旁白的关键词"]
    rejected = client.patch(
        f"/api/v1/contents/{approved_content['id']}", json={"version": value["version"], "scenes": scenes}
    )
    assert rejected.status_code == 422
    assert client.get(f"/api/v1/contents/{approved_content['id']}").json()["version"] == value["version"]


def test_publication_idempotency_reservation_and_manual_confirmation(client, engine, approved_content):
    binding = account(client)
    body = {
        "content_id": approved_content["id"],
        "content_version": approved_content["version"],
        "account_id": binding["id"],
    }
    key = {"Idempotency-Key": "test-publish-action"}
    first = client.post("/api/v1/publications", json=body, headers=key)
    assert first.status_code == 202, first.text
    second = client.post("/api/v1/publications", json=body, headers=key)
    assert second.json() == first.json()
    assert client.post("/api/v1/publications", json=body).json()["code"] == "DUPLICATE_PUBLICATION"
    value = first.json()
    pub_id = value["publication_id"]
    assert (
        client.post(
            f"/api/v1/publications/{pub_id}/confirm",
            json={"confirmed": True, "work_url": "https://www.douyin.com/video/1234567890123456789"},
        ).status_code
        == 409
    )
    assert (
        client.patch(
            f"/api/v1/contents/{approved_content['id']}",
            json={"version": approved_content["version"], "title": "不应覆盖上传中内容"},
        ).status_code
        == 409
    )
    with Session(engine) as session:
        publication = session.get(Publication, pub_id)
        publication.status = "WAITING_USER"
        session.get(Job, value["job_id"]).status = "WAITING_USER"
        session.commit()
    wrong = client.post(
        f"/api/v1/publications/{pub_id}/confirm",
        json={"confirmed": True, "work_url": "https://evil.example/video/1234567890123456789"},
    )
    assert wrong.status_code == 422
    confirmed = client.post(
        f"/api/v1/publications/{pub_id}/confirm",
        json={"confirmed": True, "work_url": "https://www.douyin.com/video/1234567890123456789"},
    )
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["status"] == "PUBLISHED"
    assert confirmed.json()["evidence"]["confirmation"]["source"] == "user"
    assert client.post(f"/api/v1/publications/{pub_id}/cancel", json={}).status_code == 409
    assert client.post("/api/v1/publications", json=body).status_code == 409
    with Session(engine) as session:
        assert session.get(Publication, pub_id).reserved_serial is None


def test_xhs_operator_media_confirmation_is_audited_and_identical_repeat_is_idempotent(
    client, engine, approved_content, monkeypatch
):
    prepared = xhs_media_selection_record(client, engine, approved_content)
    calls = []

    def verify(**kwargs):
        calls.append(kwargs)
        return {
            "video_sha256": kwargs["video_sha256"],
            "app_version": kwargs["expected_app_version"],
            "page": "editor",
        }

    monkeypatch.setattr("app.integrations.device.verify_operator_media_selection", verify)
    endpoint = f"/api/v1/publications/{prepared['publication_id']}/confirm-media-selection"
    assert client.post(endpoint, json={"confirmed": True}).status_code == 422
    body = {"confirmed": True, "notes": "已人工核验哈希、时长和视频预览"}
    confirmed = client.post(endpoint, json=body)
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["status"] == "NEEDS_ATTENTION"
    assert confirmed.json()["operator_media_confirmation"]["confirmed"] is True
    assert "fixture-account" not in confirmed.text
    assert len(calls) == 1
    repeated = client.post(endpoint, json=body)
    assert repeated.status_code == 200
    assert repeated.json() == confirmed.json()
    assert len(calls) == 1
    with Session(engine) as session:
        publication = session.get(Publication, prepared["publication_id"])
        publication.evidence = {
            **publication.evidence,
            "reason": "无法安全填写发布文案",
            "error_code": "DEVICE_ACTION_FAILED",
        }
        session.commit()
    repeated_after_device_error = client.post(endpoint, json=body)
    assert repeated_after_device_error.status_code == 200
    assert repeated_after_device_error.json() == confirmed.json()
    assert len(calls) == 1
    conflict = client.post(endpoint, json={"confirmed": True, "notes": "不同事实"})
    assert conflict.status_code == 409
    assert conflict.json()["code"] == "MEDIA_CONFIRMATION_CONFLICT"
    resumed = client.post(f"/api/v1/publications/{prepared['publication_id']}/resume")
    assert resumed.status_code == 202
    assert resumed.json()["job_id"] == prepared["job_id"]
    assert client.get("/api/v1/publications").json()["total"] == 1
    with Session(engine) as session:
        publication = session.get(Publication, prepared["publication_id"])
        audit = publication.evidence["operator_media_confirmation"]
        assert audit["notes"] == body["notes"]
        assert audit["video_sha256"] == publication.evidence["video_sha256"]
        assert audit["app_version"] == "9.46.0"
        assert not publication.evidence.get("submit_started_at")
        assert not publication.evidence.get("final_publish_clicked")
        assert publication.status == "PREPARING"
        assert session.get(Job, prepared["job_id"]).status == "QUEUED"


@pytest.mark.parametrize(
    ("change", "code"),
    [
        ("platform", "MEDIA_CONFIRMATION_PLATFORM_INVALID"),
        ("status", "PUBLICATION_NOT_READY"),
        ("reason", "MEDIA_CONFIRMATION_STATE_INVALID"),
        ("running", "JOB_RUNNING"),
        ("restore", "RESTORE_REVIEW_REQUIRED"),
        ("submitted", "PUBLICATION_OUTCOME_UNKNOWN"),
        ("approval", "APPROVAL_STALE"),
        ("account", "ACCOUNT_CHANGED"),
        ("material", "MEDIA_CONTINUITY_INVALID"),
    ],
)
def test_xhs_operator_media_confirmation_rejects_stale_or_wrong_local_state(
    client, engine, approved_content, monkeypatch, change, code
):
    prepared = xhs_media_selection_record(client, engine, approved_content)
    with Session(engine) as session:
        publication = session.get(Publication, prepared["publication_id"])
        job = session.get(Job, prepared["job_id"])
        if change == "platform":
            publication.platform = "douyin"
        elif change == "status":
            publication.status = "PREPARING"
        elif change == "reason":
            publication.evidence = {**publication.evidence, "reason": "CAPTION_ENTRY_REQUIRED"}
        elif change == "running":
            job.status = "RUNNING"
        elif change == "restore":
            job.error_code = "RESTORE_REVIEW_REQUIRED"
        elif change == "submitted":
            publication.evidence = {**publication.evidence, "submit_started_at": now()}
        elif change == "approval":
            session.get(Content, publication.content_id).status = "DRAFT"
        elif change == "account":
            publication.reserved_serial = "changed-device"
        else:
            publication.evidence = {**publication.evidence, "video_sha256": "0" * 64}
        session.commit()
        unchanged = copy.deepcopy(publication.evidence)
    monkeypatch.setattr(
        "app.integrations.device.verify_operator_media_selection",
        lambda **_kwargs: pytest.fail("local validation must stop before device inspection"),
    )
    response = client.post(
        f"/api/v1/publications/{prepared['publication_id']}/confirm-media-selection",
        json={"confirmed": True, "notes": "人工核验"},
    )
    assert response.status_code == 409
    assert response.json()["code"] == code
    with Session(engine) as session:
        assert session.get(Publication, prepared["publication_id"]).evidence == unchanged


def test_xhs_operator_media_confirmation_detects_concurrent_state_change(
    client, engine, approved_content, monkeypatch
):
    prepared = xhs_media_selection_record(client, engine, approved_content)

    def verify(**kwargs):
        with Session(engine) as session:
            job = session.get(Job, prepared["job_id"])
            job.current_step = "concurrent update"
            session.commit()
        return {
            "video_sha256": kwargs["video_sha256"],
            "app_version": kwargs["expected_app_version"],
            "page": "editor",
        }

    monkeypatch.setattr("app.integrations.device.verify_operator_media_selection", verify)
    response = client.post(
        f"/api/v1/publications/{prepared['publication_id']}/confirm-media-selection",
        json={"confirmed": True, "notes": "人工核验"},
    )
    assert response.status_code == 409
    assert response.json()["code"] == "PUBLICATION_STATE_CHANGED"
    with Session(engine) as session:
        evidence = session.get(Publication, prepared["publication_id"]).evidence
        assert "operator_media_confirmation" not in evidence


def test_xhs_operator_media_confirmation_unlock_failure_writes_nothing(
    client, engine, approved_content, monkeypatch
):
    prepared = xhs_media_selection_record(client, engine, approved_content)
    with Session(engine) as session:
        before = copy.deepcopy(session.get(Publication, prepared["publication_id"]).evidence)
    monkeypatch.setattr(
        "app.integrations.device.verify_operator_media_selection",
        lambda **_kwargs: (_ for _ in ()).throw(
            IntegrationError("DEVICE_USER_ACTION_REQUIRED", "手机尚未解锁")
        ),
    )

    response = client.post(
        f"/api/v1/publications/{prepared['publication_id']}/confirm-media-selection",
        json={"confirmed": True, "notes": "人工核验"},
    )

    assert response.status_code == 409
    assert response.json()["code"] == "DEVICE_USER_ACTION_REQUIRED"
    with Session(engine) as session:
        publication = session.get(Publication, prepared["publication_id"])
        assert publication.status == "NEEDS_ATTENTION"
        assert publication.evidence == before
        assert session.get(Job, prepared["job_id"]).status == "WAITING_USER"


def test_legacy_approval_without_content_policy_version_requires_review(client, engine, approved_content):
    binding = account(client)
    with Session(engine) as session:
        content = session.get(Content, approved_content["id"])
        legacy_snapshot = content_snapshot(content)
        legacy_snapshot.pop("content_policy_version")
        approval = session.query(Approval).filter_by(content_id=content.id).one()
        approval.snapshot_hash = digest(legacy_snapshot)
        session.commit()
    response = client.post(
        "/api/v1/publications",
        json={
            "content_id": approved_content["id"],
            "content_version": approved_content["version"],
            "account_id": binding["id"],
        },
    )
    assert response.status_code == 409
    assert response.json()["code"] == "APPROVAL_STALE"


def test_confirm_expands_share_link_and_drops_tracking_query(client, engine, approved_content, monkeypatch):
    publication = prepared_record(client, engine, approved_content)
    expanded = "https://www.iesdouyin.com/share/video/1234567890123456789/?did=tracking-only"

    def resolve(url, **kwargs):
        assert url == "https://v.douyin.com/fixture/"
        assert kwargs["stop_at_url"](expanded)
        assert not kwargs["stop_at_url"]("https://other.test/video/1234567890123456789")
        return b"", "", expanded

    monkeypatch.setattr("app.integrations.net.safe_get", resolve)
    response = client.post(
        f"/api/v1/publications/{publication['publication_id']}/confirm",
        json={"confirmed": True, "work_url": "https://v.douyin.com/fixture/"},
    )
    assert response.status_code == 200
    value = response.json()
    assert value["status"] == "PUBLISHED" and value["work_id"] == "1234567890123456789"
    assert value["work_url"] == expanded.split("?")[0]
    assert value["evidence"]["confirmation"]["submitted_work_url"] == "https://v.douyin.com/fixture/"


def test_detect_published_work_from_bound_phone_without_pasted_link(
    client, engine, config, approved_content, monkeypatch
):
    prepared = prepared_record(client, engine, approved_content)
    with Session(engine) as session:
        publication = session.get(Publication, prepared["publication_id"])
        publication.evidence = {"verified_account_identifier": "fixture-account", "page": "editor"}
        session.commit()

    detected_calls = []

    def detect(**kwargs):
        detected_calls.append(kwargs)
        return {
            "work_url": "https://www.douyin.com/video/1234567890123456789",
            "work_id": "1234567890123456789",
            "evidence": {
                "confirmation_source": "device_detection",
                "work_title_verified": True,
                "verified_account_identifier": kwargs["account_identifier"],
            },
        }

    monkeypatch.setattr("app.integrations.publishing.detect_published_work", detect)
    response = client.post(f"/api/v1/publications/{prepared['publication_id']}/detect")
    assert response.status_code == 200, response.text
    value = response.json()
    assert value["status"] == "PUBLISHED"
    assert value["work_id"] == "1234567890123456789"
    assert value["evidence"]["detection"]["confirmation_source"] == "device_detection"
    assert detected_calls[0]["title"] == "测试标题"
    assert client.post(f"/api/v1/publications/{prepared['publication_id']}/detect").json() == value
    with Session(engine) as session:
        publication = session.get(Publication, prepared["publication_id"])
        job = session.get(Job, prepared["job_id"])
        assert publication.reserved_serial is None
        assert job.status == "SUCCEEDED" and job.error_code is None


def test_detect_published_work_requires_prior_account_verification(client, engine, approved_content):
    prepared = prepared_record(client, engine, approved_content)
    response = client.post(f"/api/v1/publications/{prepared['publication_id']}/detect")
    assert response.status_code == 409
    assert response.json()["code"] == "ACCOUNT_VERIFICATION_REQUIRED"


def test_link_resolution_does_not_hold_write_lock_or_overwrite_concurrent_cancel(
    client, engine, approved_content, monkeypatch
):
    publication = prepared_record(client, engine, approved_content)

    def resolve(url, **kwargs):
        with Session(engine) as session:
            item = session.get(Publication, publication["publication_id"])
            item.status, item.reserved_serial = "CANCELED", None
            session.get(Job, item.job_id).status = "CANCELED"
            session.commit()
        return b"", "", "https://www.douyin.com/video/1234567890123456789"

    monkeypatch.setattr("app.integrations.net.safe_get", resolve)
    response = client.post(
        f"/api/v1/publications/{publication['publication_id']}/confirm",
        json={"confirmed": True, "work_url": "https://v.douyin.com/fixture/"},
    )
    assert response.status_code == 409
    with Session(engine) as session:
        assert session.get(Publication, publication["publication_id"]).status == "CANCELED"


def test_manual_metrics_keep_missing_values_null(client, engine, approved_content):
    pub = prepared_record(client, engine, approved_content)
    client.post(
        f"/api/v1/publications/{pub['publication_id']}/confirm",
        json={"confirmed": True, "work_url": "https://www.douyin.com/video/1234567890123456789"},
    )
    response = client.post(
        "/api/v1/metrics", json={"publication_id": pub["publication_id"], "likes": 0, "notes": "测试补录"}
    )
    assert response.status_code == 201, response.text
    assert response.json()["likes"] == 0
    assert response.json()["views"] is None
    assert response.json()["source"] == "manual"
    assert (
        client.post(
            "/api/v1/metrics", json={"publication_id": pub["publication_id"], "views": -1}
        ).status_code
        == 422
    )


def test_upload_validation_and_file_boundary(client, engine, config, content_id):
    invalid = client.post(
        "/api/v1/assets",
        data={"kind": "image", "content_id": content_id},
        files={"file": ("fake.png", b"<svg onload=alert(1)></svg>", "image/png")},
    )
    assert invalid.status_code == 422
    png = io.BytesIO()
    Image.new("RGB", (16, 16), "blue").save(png, "PNG")
    valid = client.post(
        "/api/v1/assets",
        data={"kind": "image", "content_id": content_id},
        files={"file": ("../test.png", png.getvalue(), "image/png")},
    )
    assert valid.status_code == 201, valid.text
    assert valid.json()["filename"] == "test.png"
    assert "relative_path" not in valid.json()
    assert client.get(valid.json()["url"]).status_code == 200
    with Session(engine) as session:
        asset = fixture_asset(session, config, content_id, "image", relative_path="../should-not-read.txt")
        identifier = asset.id
        session.commit()
    denied = client.get(f"/api/v1/assets/{identifier}/file")
    assert denied.status_code == 400
    assert denied.json()["code"] == "INVALID_ASSET_PATH"


def test_no_model_calls_are_enabled_by_default(client, content_id):
    response = client.post(f"/api/v1/contents/{content_id}/generate", json={"stage": "all"})
    assert response.status_code == 409
    assert response.json()["code"] == "MODEL_NOT_CONFIGURED"
    assert client.get("/api/v1/jobs").json()["items"] == []


def test_source_collection_key_replay_and_conflict(client):
    sources = client.get("/api/v1/sources").json()["items"]
    headers = {"Idempotency-Key": "collection-action"}
    first = client.post(f"/api/v1/sources/{sources[0]['id']}/collect", headers=headers)
    again = client.post(f"/api/v1/sources/{sources[0]['id']}/collect", headers=headers)
    assert first.status_code == 202 and first.json() == again.json()
    other = client.post(f"/api/v1/sources/{sources[1]['id']}/collect", headers=headers)
    assert other.status_code == 409


def test_schedule_does_not_enable_without_configured_models(client):
    body = {
        "name": "测试计划",
        "kind": "pipeline",
        "time": "09:00",
        "timezone": "America/New_York",
        "enabled": False,
        "daily_limit": 1,
    }
    assert client.post("/api/v1/schedules", json=body).status_code == 201
    body["enabled"] = True
    assert client.post("/api/v1/schedules", json=body).json()["code"] == "MODEL_NOT_CONFIGURED"
