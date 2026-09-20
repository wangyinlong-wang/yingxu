from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session
from test_api import account
from test_integrations_device import _FakeDevice, _hierarchy, _prepare_monkeypatch

from app.integrations import device, publishing
from app.integrations.errors import IntegrationError
from app.models import Job, Publication
from app.services import AppError, normalized_work
from app.worker import Worker


def _submit_arguments():
    return {
        "serial": "ABC123",
        "platform": "douyin",
        "account_identifier": "expected-user",
        "video_sha256": "a" * 64,
        "caption": "测试标题 审核正文",
        "title": "测试标题",
        "description": "审核正文",
        "evidence": {
            "serial": "ABC123",
            "platform": "douyin",
            "app_version": "40.4.0",
            "verified_account_identifier": "expected-user",
            "verified_at": datetime.now(UTC).isoformat(),
            "video_sha256": "a" * 64,
            "caption": "测试标题 审核正文",
            "page": "editor",
            "final_publish_clicked": False,
        },
        "canceled": lambda: False,
    }


def _final_editor(description="审核正文"):
    return _hierarchy("发布设置", "作品描述", "测试标题", description, "发作品", clickable={"发作品"})


@pytest.mark.parametrize("success", [True, False])
def test_final_click_is_fenced_once_and_missing_success_stays_unknown(monkeypatch, success):
    fake = _FakeDevice([_final_editor(), _hierarchy(*(["发布成功", "测试标题"] if success else ["首页"]))])
    _prepare_monkeypatch(monkeypatch, fake)
    monkeypatch.setattr(device, "_package_version", lambda *args: "40.4.0")
    events = []
    original_click = fake.jsonrpc.click

    def clicked(x, y):
        assert events == ["intent"]
        return original_click(x, y)

    fake.jsonrpc.click = clicked
    monkeypatch.setattr(
        publishing, "_copy_published_link", lambda *args: ("https://www.douyin.com/video/123456", "123456")
    )
    result = publishing.submit_publication(**_submit_arguments(), record_submit=events.append)
    assert events == ["intent", "clicked"]
    assert len(fake.actions) == 1
    assert result["status"] == ("PUBLISHED" if success else "VERIFYING")


def test_xhs_946_final_click_uses_observed_send_note_label(monkeypatch):
    editor = _hierarchy(
        "添加标题",
        "添加作品描述",
        "公开可见",
        "测试标题",
        "审核正文",
        "发笔记",
        clickable={"发笔记"},
    )
    declaration = _hierarchy(
        "声明内容来源",
        "含 AI 合成内容",
        "确认并发布",
        clickable={"含 AI 合成内容"},
    )
    confirmed_declaration = _hierarchy(
        "声明内容来源",
        "含 AI 合成内容",
        "确认并发布",
        clickable={"确认并发布"},
    )
    fake = _FakeDevice(
        [editor, declaration, confirmed_declaration, _hierarchy("笔记发布成功", "测试标题")],
        packages=["com.xingin.xhs"] * 4,
    )
    _prepare_monkeypatch(monkeypatch, fake)
    monkeypatch.setattr(device, "_package_version", lambda *_args: "9.46.0")
    monkeypatch.setattr(
        publishing,
        "_copy_published_link",
        lambda *_args: ("https://www.xiaohongshu.com/explore/1234567890abcdef", "1234567890abcdef"),
    )
    arguments = _submit_arguments()
    arguments.update(
        {
            "platform": "xiaohongshu",
            "evidence": {
                **arguments["evidence"],
                "platform": "xiaohongshu",
                "app_version": "9.46.0",
            },
        }
    )
    events = []

    result = publishing.submit_publication(**arguments, record_submit=events.append)

    assert events == ["intent", "clicked"]
    assert fake.actions == [
        ("click", {"resourceId": "id/5"}),
        ("click", {"resourceId": "id/1"}),
        ("click", {"resourceId": "id/2"}),
    ]
    assert result["status"] == "PUBLISHED"
    assert result["evidence"]["content_declaration"] == "ai_generated"


def test_xhs_current_cn_share_link_is_copied_and_resolved(monkeypatch):
    short_url = "https://xhslink.cn/o/2zFsm6IYhB2"
    clean_url = "https://www.xiaohongshu.com/explore/1234567890abcdef"
    expanded = clean_url + "?xsec_token=tracking-secret&share_id=tracking-only"
    fake = _FakeDevice([])

    def click(_phone, _nodes, labels):
        if "复制链接" in labels:
            fake.clipboard = f"审核标题 {short_url} 复制后打开小红书"
        return True

    monkeypatch.setattr(device, "_click_unique", click)
    monkeypatch.setattr(
        device,
        "_guarded_page",
        lambda *_args: ("unknown", "com.xingin.xhs", []),
    )
    monkeypatch.setattr(
        "app.integrations.net.safe_get",
        lambda url, **kwargs: (b"", "", expanded),
    )

    work_url, work_id = publishing._copy_published_link(fake, "xiaohongshu", [])

    assert work_url == clean_url
    assert work_id == "1234567890abcdef"
    assert normalized_work("xiaohongshu", short_url, None) == (clean_url, work_id)
    assert normalized_work("xiaohongshu", expanded, None) == (clean_url, work_id)


def test_profile_navigation_dismisses_only_the_known_copied_link_panel(monkeypatch, config):
    copied = '<hierarchy><node text="链接已复制成功，去粘贴分享：" resource-id="com.ss.android.ugc.aweme:id/z0a" /></hierarchy>'
    fake = _FakeDevice([copied, copied, _hierarchy("抖音号：expected-user", "编辑主页")])
    _prepare_monkeypatch(monkeypatch, fake)
    fake.press = lambda key: fake.actions.append(("key", key))
    publishing.open_account_profile(
        serial="ABC123",
        platform="douyin",
        account_identifier="expected-user",
        canceled=lambda: False,
        app_config=config,
    )
    assert fake.actions == [("key", "back")]


def test_detect_published_work_requires_reviewed_title_before_reading_link(monkeypatch, config):
    fake = _FakeDevice([_hierarchy("测试标题", "审核正文", "点赞 12")])
    _prepare_monkeypatch(monkeypatch, fake)
    monkeypatch.setattr(device, "_package_version", lambda *_args: "40.4.0")
    monkeypatch.setattr(
        publishing,
        "_copy_published_link",
        lambda *_args: ("https://www.douyin.com/video/1234567890123456789", "1234567890123456789"),
    )

    result = publishing.detect_published_work(
        serial="ABC123",
        platform="douyin",
        account_identifier="expected-user",
        title="测试标题",
        canceled=lambda: False,
        app_config=config,
    )
    assert result["work_id"] == "1234567890123456789"
    assert result["evidence"] == {
        "confirmation_source": "device_detection",
        "work_title_verified": True,
        "verified_account_identifier": "expected-user",
        "app_version": "40.4.0",
    }

    mismatch = _FakeDevice([_hierarchy("另一条作品", "点赞 12")])
    _prepare_monkeypatch(monkeypatch, mismatch)
    with pytest.raises(IntegrationError) as caught:
        publishing.detect_published_work(
            serial="ABC123",
            platform="douyin",
            account_identifier="expected-user",
            title="测试标题",
            canceled=lambda: False,
            app_config=config,
        )
    assert caught.value.code == "WORK_TITLE_MISMATCH"


def test_final_click_stops_before_intent_when_caption_changed(monkeypatch):
    fake = _FakeDevice([_final_editor("被改动的正文")])
    _prepare_monkeypatch(monkeypatch, fake)
    monkeypatch.setattr(device, "_package_version", lambda *args: "40.4.0")
    with pytest.raises(IntegrationError) as error:
        publishing.submit_publication(
            **_submit_arguments(), record_submit=lambda stage: pytest.fail("must not submit")
        )
    assert error.value.code == "PUBLISH_EDITOR_CHANGED"
    assert fake.actions == []


def test_failed_intent_commit_never_clicks(monkeypatch):
    fake = _FakeDevice([_final_editor()])
    _prepare_monkeypatch(monkeypatch, fake)
    monkeypatch.setattr(device, "_package_version", lambda *args: "40.4.0")

    def fail_commit(stage):
        raise AppError("TASK_CANCELED", "测试持久化失败")

    with pytest.raises(AppError):
        publishing.submit_publication(**_submit_arguments(), record_submit=fail_commit)
    assert fake.actions == []


def _approve_automatic(client, approved_content, platforms=("douyin",)):
    for platform in platforms:
        account(client, platform=platform, serial="test-android")
    settings = client.patch("/api/v1/settings", json={"publishing": {"auto_publish_after_approval": True}})
    assert settings.status_code == 200, settings.text
    result = client.post(
        f"/api/v1/contents/{approved_content['id']}/approve",
        json={"version": approved_content["version"], "compliance_confirmed": True},
    )
    assert result.status_code == 200, result.text
    return result.json()["auto_publish_job_ids"]


def test_approved_platforms_share_one_phone_without_losing_queue_order(
    client, engine, config, approved_content
):
    jobs = _approve_automatic(client, approved_content, ("douyin", "xiaohongshu"))
    assert len(jobs) == 2
    worker = Worker(config, queue="device", engine=engine)
    first = worker.claim()
    assert first.id == jobs[0]
    with Session(engine) as session:
        publication = session.scalar(select(Publication).where(Publication.job_id == first.id))
        assert publication.reserved_serial == "test-android"
        publication.status = "VERIFYING"
        publication.evidence = {"submit_started_at": datetime.now(UTC).isoformat()}
        session.get(Job, first.id).status = "WAITING_USER"
        session.commit()
    assert worker.claim() is None
    with Session(engine) as session:
        publication = session.scalar(select(Publication).where(Publication.job_id == first.id))
        publication.status, publication.reserved_serial = "PUBLISHED", None
        session.get(Job, first.id).status = "SUCCEEDED"
        session.commit()
    assert worker.claim().id == jobs[1]


def test_queued_legacy_publication_stops_before_device_action(
    client, engine, config, approved_content, monkeypatch
):
    jobs = _approve_automatic(client, approved_content)
    with Session(engine) as session:
        publication = session.scalar(select(Publication).where(Publication.job_id == jobs[0]))
        legacy_snapshot = dict(publication.snapshot)
        legacy_snapshot.pop("content_policy_version")
        publication.snapshot = legacy_snapshot
        session.commit()
    monkeypatch.setattr(
        publishing,
        "open_account_profile",
        lambda **kwargs: pytest.fail("legacy approval must stop before opening the platform"),
    )
    monkeypatch.setattr(
        device,
        "prepare_upload",
        lambda **kwargs: pytest.fail("legacy approval must stop before device upload"),
    )
    assert Worker(config, queue="device", engine=engine).run_once()
    with Session(engine) as session:
        job = session.get(Job, jobs[0])
        publication = session.scalar(select(Publication).where(Publication.job_id == jobs[0]))
        assert job.status == "WAITING_USER" and job.error_code == "APPROVAL_STALE"
        assert publication.status == "NEEDS_ATTENTION"


@pytest.mark.parametrize("cancel_after_click", [False, True])
def test_auto_publication_persists_intent_and_success_in_real_worker(
    client, engine, config, approved_content, monkeypatch, cancel_after_click
):
    jobs = _approve_automatic(client, approved_content)
    monkeypatch.setattr(publishing, "open_account_profile", lambda **kwargs: None)
    monkeypatch.setattr(
        device, "prepare_upload", lambda **kwargs: {"status": "WAITING_USER", "evidence": {"page": "editor"}}
    )

    def submit(**kwargs):
        kwargs["record_submit"]("intent")
        with Session(engine) as session:
            p = session.scalar(select(Publication).where(Publication.job_id == jobs[0]))
            assert p.status == "VERIFYING" and p.evidence["submit_started_at"]
        kwargs["record_submit"]("clicked")
        if cancel_after_click:
            assert client.post(f"/api/v1/jobs/{jobs[0]}/cancel").status_code == 200
        return {
            "status": "PUBLISHED",
            "work_url": "https://www.douyin.com/video/123456",
            "work_id": "123456",
            "evidence": {"confirmation_source": "device"},
        }

    monkeypatch.setattr(publishing, "submit_publication", submit)
    assert Worker(config, queue="device", engine=engine).run_once()
    with Session(engine) as session:
        job = session.get(Job, jobs[0])
        p = session.scalar(select(Publication).where(Publication.job_id == jobs[0]))
        assert job.status == "SUCCEEDED" and job.result["published_work_id"] == "123456"
        assert p.status == "PUBLISHED" and p.reserved_serial is None
        assert p.evidence["submit_clicked_at"] and p.evidence["final_publish_clicked"] is True


def test_unknown_submit_cannot_resume_or_cancel_without_explicit_nonpublication_confirmation(
    client, engine, approved_content
):
    jobs = _approve_automatic(client, approved_content)
    with Session(engine) as session:
        p = session.scalar(select(Publication).where(Publication.job_id == jobs[0]))
        p.status, p.reserved_serial = "VERIFYING", "test-android"
        p.evidence = {"submit_started_at": datetime.now(UTC).isoformat()}
        session.get(Job, jobs[0]).status = "WAITING_USER"
        identifier = p.id
        session.commit()
    assert (
        client.post(f"/api/v1/publications/{identifier}/resume").json()["code"]
        == "PUBLICATION_OUTCOME_UNKNOWN"
    )
    assert (
        client.post(f"/api/v1/publications/{identifier}/cancel", json={}).json()["code"]
        == "PUBLICATION_OUTCOME_UNKNOWN"
    )
    canceled = client.post(
        f"/api/v1/publications/{identifier}/cancel",
        json={"confirmed_not_published": True, "notes": "已在平台核验未发布"},
    )
    assert canceled.status_code == 200 and canceled.json()["status"] == "CANCELED"


@pytest.mark.parametrize("published", [False, True])
def test_process_recovery_never_replays_a_committed_submit(
    client, engine, config, approved_content, published
):
    jobs = _approve_automatic(client, approved_content)
    with Session(engine) as session:
        p = session.scalar(select(Publication).where(Publication.job_id == jobs[0]))
        p.status = "PUBLISHED" if published else "VERIFYING"
        p.evidence = {"submit_started_at": datetime.now(UTC).isoformat()}
        p.reserved_serial = None if published else "test-android"
        job = session.get(Job, jobs[0])
        job.status, job.lease_owner = "RUNNING", "old-worker"
        job.lease_until = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
        session.commit()
    assert Worker(config, queue="device", engine=engine).claim() is None
    with Session(engine) as session:
        assert session.get(Job, jobs[0]).status == ("SUCCEEDED" if published else "WAITING_USER")
        p = session.scalar(select(Publication).where(Publication.job_id == jobs[0]))
        assert p.status == ("PUBLISHED" if published else "VERIFYING")


def test_review_rejects_a_changed_publish_mode_before_enqueuing(client, engine, approved_content):
    account(client)
    assert (
        client.patch(
            "/api/v1/settings", json={"publishing": {"auto_publish_after_approval": True}}
        ).status_code
        == 200
    )
    response = client.post(
        f"/api/v1/contents/{approved_content['id']}/approve",
        json={
            "version": approved_content["version"],
            "auto_publish_expected": False,
        },
    )
    assert response.status_code == 409 and response.json()["code"] == "PUBLISH_MODE_CHANGED"
    with Session(engine) as session:
        assert session.scalar(select(Publication.id)) is None
