from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from alembic import command
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session
from test_v2_migration import alembic_config

from app.integrations import browser
from app.integrations.browser_runtime import (
    BrowserError,
    matching_work,
    parse_metrics,
    verified_identity,
    verify_page,
    work_link,
)
from app.integrations.errors import IntegrationError
from app.models import Publication
from app.worker import Worker


def browser_account(client, platform="douyin"):
    response = client.post("/api/v1/accounts", json={"channel": "browser", "platform": platform,
        "display_name": "Browser fixture", "account_identifier": "fixture-account"})
    assert response.status_code == 201, response.text
    return response.json()


def queue(client, approved_content, binding):
    response = client.post("/api/v1/publications", json={"content_id": approved_content["id"],
        "content_version": approved_content["version"], "account_id": binding["id"]})
    assert response.status_code == 202, response.text
    return response.json()


def test_account_channel_contract_and_legacy_default(client):
    result = browser_account(client)
    assert result["channel"] == "browser" and result["device_serial"] == "browser:douyin"
    payload = {"platform": "xiaohongshu", "display_name": "legacy", "account_identifier": "user"}
    assert client.post("/api/v1/accounts", json=payload).status_code == 422
    assert client.post("/api/v1/accounts", json={**payload, "device_serial": "browser:douyin"}).status_code == 422
    legacy = client.post("/api/v1/accounts", json={**payload, "device_serial": "adb-123"}).json()
    assert legacy["channel"] == "android" and legacy["device_serial"] == "adb-123"


def test_status_get_is_read_only_and_inspection_checks_bound_account(client, monkeypatch):
    monkeypatch.setattr(browser, "_chrome", lambda *_args, **_kwargs: pytest.fail("GET started Chrome"))
    assert client.get("/api/v1/browser/status").status_code == 200
    assert client.post("/api/v1/browser/unknown/open", json={}).status_code == 422
    browser_account(client)
    calls = []
    monkeypatch.setattr(browser, "run_browser", lambda *args, **kwargs: calls.append((args, kwargs)) or {"ready": True})
    assert client.post("/api/v1/browser/douyin/inspect", json={}).status_code == 200
    assert calls[0][0][1:] == ("douyin", "inspect")
    assert calls[0][1]["account_identifier"] == "fixture-account"


@pytest.mark.parametrize("platform", ["douyin", "xiaohongshu"])
def test_manual_browser_route_stops_before_submit_and_preserves_reservation(
    client, config, engine, approved_content, monkeypatch, platform
):
    binding = browser_account(client, platform)
    queued = queue(client, approved_content, binding)
    actions = []

    def run(_config, _platform, action, **kwargs):
        actions.append(action)
        assert _platform == platform and kwargs["title"] == "测试标题"
        return {"status": "WAITING_USER", "evidence": {"verified_account_identifier": "fixture-account"}}

    monkeypatch.setattr(browser, "run_browser", run)
    monkeypatch.setattr("app.integrations.device.prepare_upload", lambda **_: pytest.fail("Android called"))
    assert Worker(config, "device", engine=engine).run_once()
    assert actions == ["prepare"]
    record = client.get("/api/v1/publications").json()["items"][0]
    assert record["channel"] == "browser" and record["status"] == "WAITING_USER"
    assert client.post(f"/api/v1/browser/{platform}/open", json={}).status_code == 409
    patch = {key: binding[key] for key in ("platform", "display_name", "account_identifier")}
    patch.update(channel="android", device_serial="adb-1")
    assert client.patch(f"/api/v1/accounts/{binding['id']}", json=patch).status_code == 409
    with Session(engine) as session:
        assert session.get(Publication, queued["publication_id"]).reserved_serial == f"browser:{platform}"


def test_browser_submit_timeout_is_durably_fenced_and_cannot_resume(
    client, config, engine, approved_content, monkeypatch
):
    browser_account(client)
    assert client.patch("/api/v1/settings", json={"publishing": {"auto_publish_after_approval": True}}).status_code == 200
    approved = client.post(f"/api/v1/contents/{approved_content['id']}/approve",
        json={"version": approved_content["version"], "compliance_confirmed": True}).json()
    job_id = approved["auto_publish_job_ids"][0]
    actions = []

    def run(_config, _platform, action, **kwargs):
        actions.append(action)
        if action == "prepare":
            return {"status": "WAITING_USER", "evidence": {"verified_account_identifier": "fixture-account"}}
        with Session(engine) as session:
            record = session.scalar(select(Publication).where(Publication.job_id == job_id))
            assert record.evidence["submit_started_at"] and record.status == "VERIFYING"
        raise IntegrationError("BROWSER_TIMEOUT", "模拟提交后响应丢失")

    monkeypatch.setattr(browser, "run_browser", run)
    worker = Worker(config, "device", engine=engine)
    assert worker.run_once()
    assert not worker.run_once()
    record = client.get("/api/v1/publications").json()["items"][0]
    assert record["status"] == "VERIFYING" and actions == ["prepare", "submit"]
    response = client.post(f"/api/v1/publications/{record['id']}/resume")
    assert response.status_code == 409
    assert client.post(f"/api/v1/jobs/{job_id}/retry").status_code == 409


def test_browser_detection_and_metrics_share_existing_analytics(
    client, config, engine, approved_content, monkeypatch
):
    queued = queue(client, approved_content, browser_account(client))

    def run(_config, _platform, action, **kwargs):
        if action == "prepare":
            return {"status": "WAITING_USER", "evidence": {"verified_account_identifier": "fixture-account"}}
        if action == "detect":
            return {"work_url": "https://www.douyin.com/video/123456789", "work_id": "123456789"}
        assert action == "metrics" and kwargs["work_id"] == "123456789"
        return {"views": None, "likes": 0, "comments": 2, "favorites": None, "shares": None,
                "raw": {"channel": "browser", "work_id": "123456789"}}

    monkeypatch.setattr(browser, "run_browser", run)
    worker = Worker(config, "device", engine=engine)
    assert worker.run_once()
    detected = client.post(f"/api/v1/publications/{queued['publication_id']}/detect")
    assert detected.status_code == 200 and detected.json()["status"] == "PUBLISHED"
    assert client.post(f"/api/v1/publications/{queued['publication_id']}/collect-metrics").status_code == 202
    assert worker.run_once()
    rows = client.get("/api/v1/metrics").json()["items"]
    assert len(rows) == 1 and rows[0]["source"] == "browser"
    assert rows[0]["likes"] == 0 and rows[0]["views"] is None and rows[0]["comments"] == 2
    assert client.get("/api/v1/analytics").status_code == 200


def test_metrics_parse_missing_zero_units_and_ambiguous_values():
    values = parse_metrics([{"label": "点赞", "value": "0"}, {"label": "播放量", "value": "1.2万"},
                           {"label": "评论", "value": "12"}, {"label": "评论", "value": "13"},
                           {"label": "标题", "value": "发布10000点赞"}, {"label": "分享", "value": "—"}])
    assert values["likes"] == 0 and values["views"] == 12000
    assert values["comments"] is None and values["shares"] is None and values["favorites"] is None


def test_rendered_xhs_icons_are_matched_by_shape_not_position():
    fixture = json.loads((Path(__file__).parent / "fixtures/xhs_metric_icons.json").read_text())
    pairs = [{"icon": row["icon"], "value": str(index + 1)} for index, row in enumerate(fixture)]
    values = parse_metrics(list(reversed(pairs)))
    assert [values[key] for key in ("views", "comments", "likes", "favorites", "shares")] == [1, 2, 3, 4, 5]
    pairs[0]["icon"] = "changed-icon"
    assert parse_metrics(pairs)["views"] is None


def test_creator_detail_link_and_same_context_identity_fallback():
    url = "https://creator.douyin.com/creator-micro/work-management/work-detail/123456789?enter_from=content"
    state = {"url": url, "identity_regions": [], "challenge": False}
    request = {"platform": "douyin", "account_identifier": "fixture-account",
               "_identity_regions": ["抖音号：fixture-account"]}
    assert verify_page(request, state) == "fixture-account"
    assert work_link("douyin", url) == ("https://www.douyin.com/video/123456789", "123456789")
    with pytest.raises(BrowserError):
        verify_page(request, {**state, "identity_regions": ["抖音号：changed-account"]})
    with pytest.raises(BrowserError):
        verify_page(request, {**state, "challenge": True})
    assert verified_identity("xiaohongshu", {
        "url": "https://creator.xiaohongshu.com/", "identity_regions": ["小红书账号：fixture-xhs"]
    }, "fixture-xhs") == "fixture-xhs"


def test_account_and_work_require_exact_identity():
    state = {"url": "https://creator.douyin.com/creator-micro/home",
             "identity_regions": ["抖音号：fixture-account"], "challenge": False}
    assert verified_identity("douyin", state, "fixture-account") == "fixture-account"
    for patch in ({"url": "https://creator.douyin.com.evil.invalid/"}, {"challenge": True},
                  {"identity_regions": ["抖音号：wrong"]}, {"identity_regions": ["fixture-account"]}):
        with pytest.raises(BrowserError):
            verified_identity("douyin", {**state, **patch}, "fixture-account")
    work = {"texts": ["测试标题"], "links": ["https://www.douyin.com/video/123456789?tracking=secret"], "pairs": []}
    found = matching_work("douyin", {"works": [work]}, "测试标题", "123456789")
    assert found["work_url"] == "https://www.douyin.com/video/123456789"
    for title, identity in (("另一个标题", "123456789"), ("测试标题", "123456788")):
        with pytest.raises(BrowserError):
            matching_work("douyin", {"works": [work]}, title, identity)
    duplicate = copy.deepcopy(work)
    duplicate["links"] = ["https://www.douyin.com/video/987654321"]
    with pytest.raises(BrowserError):
        matching_work("douyin", {"works": [work, duplicate]}, "测试标题")
    assert work_link("douyin", "https://evil.invalid/video/123456789") is None
    assert work_link("xiaohongshu", "https://www.xiaohongshu.com/explore/1234567890abcdef?token=secret") == (
        "https://www.xiaohongshu.com/explore/1234567890abcdef", "1234567890abcdef")


def test_migration_keeps_android_rows_and_blocks_unsafe_downgrade(tmp_path):
    url = f"sqlite:///{tmp_path / 'migration.sqlite3'}"
    cfg = alembic_config(url)
    command.upgrade(cfg, "0003")
    engine = create_engine(url)
    with engine.begin() as connection:
        connection.execute(text("INSERT INTO accounts (id, platform, display_name, account_identifier, device_serial,"
                                " created_at, updated_at) VALUES ('old','douyin','old','identity','adb','now','now')"))
    command.upgrade(cfg, "0004")
    with engine.begin() as connection:
        assert connection.execute(text("SELECT channel FROM accounts WHERE id='old'")).scalar() == "android"
        connection.execute(text("UPDATE accounts SET channel='browser', device_serial='browser:douyin'"))
    with pytest.raises(RuntimeError, match="Browser accounts"):
        command.downgrade(cfg, "0003")
    with engine.begin() as connection:
        connection.execute(text("UPDATE accounts SET channel='android', device_serial='adb'"))
    command.downgrade(cfg, "0003")
    with engine.connect() as connection:
        assert connection.execute(text("SELECT device_serial FROM accounts WHERE id='old'")).scalar() == "adb"
    engine.dispose()
