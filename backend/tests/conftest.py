from __future__ import annotations

import hashlib

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import AppConfig
from app.main import create_app
from app.models import Asset, Content, QualityReview, Source, Template, Topic, uid
from app.schemas import Script


@pytest.fixture
def config(tmp_path):
    return AppConfig(
        data_dir=tmp_path / "workspace",
        timezone="America/New_York",
        _env_file=None,
        text_api_key=SecretStr("fixture-not-a-real-api-key"),
        image_api_key=SecretStr("fixture-not-a-real-api-key"),
        speech_api_key=SecretStr("fixture-not-a-real-api-key"),
    )


@pytest.fixture
def app(config):
    return create_app(config)


@pytest.fixture
def client(app):
    with TestClient(app, base_url="http://127.0.0.1:8765", raise_server_exceptions=False) as client:
        yield client


@pytest.fixture
def engine(app, client):
    return app.state.engine


@pytest.fixture
def content_id(engine):
    with Session(engine) as session:
        source = session.scalar(select(Source))
        template = session.scalar(select(Template))
        topic = Topic(
            id=uid(),
            source_id=source.id,
            title="测试用技术选题（非新闻）",
            url="https://example.org/test-topic",
            fingerprint=hashlib.sha256(b"fixture-topic").hexdigest(),
            summary="这是一份确定性测试材料，不是实际新闻或运营数据。",
            relevance_score=80,
        )
        session.add(topic)
        session.flush()
        script = Script(
            angle="测试角度",
            selection_reason="测试原因",
            facts=[
                {
                    "claim": "用于验证审核的测试陈述",
                    "source_url": topic.url,
                    "verified": False,
                    "notes": "单元测试夹具",
                }
            ],
            platforms={
                "douyin": {
                    "title": "测试标题",
                    "description": "非实际发布内容，AI辅助制作。",
                    "tags": ["测试"],
                },
                "xiaohongshu": {
                    "title": "测试标题",
                    "description": "非实际发布内容，AI辅助制作。",
                    "tags": ["测试"],
                },
            },
        )
        content = Content(
            id=uid(),
            topic_id=topic.id,
            template_id=template.id,
            title=topic.title,
            script=script.model_dump(),
            scenes=[],
        )
        session.add(content)
        session.commit()
        return content.id


def fixture_asset(session: Session, config: AppConfig, content_id: str, kind: str, *, relative_path=None):
    """Business-state fixture only; not a codec, model, voice or published-output acceptance sample."""
    identifier = uid()
    extension = {"video": ".mp4", "cover": ".png", "subtitle": ".srt", "image": ".png", "audio": ".mp3"}[kind]
    path = config.data_dir / "assets" / (identifier + extension)
    path.write_bytes(b"business-state-fixture-only")
    asset = Asset(
        id=identifier,
        content_id=content_id,
        kind=kind,
        filename="fixture" + extension,
        relative_path=relative_path or str(path.relative_to(config.data_dir)),
        mime_type={
            "video": "video/mp4",
            "cover": "image/png",
            "subtitle": "application/x-subrip",
            "image": "image/png",
            "audio": "audio/mpeg",
        }[kind],
        size=path.stat().st_size,
        sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
    )
    session.add(asset)
    session.flush()
    return asset


@pytest.fixture
def rendered_content(engine, config, content_id):
    with Session(engine) as session:
        content = session.get(Content, content_id)
        for kind, field in (
            ("video", "video_asset_id"),
            ("cover", "cover_asset_id"),
            ("subtitle", "subtitle_asset_id"),
        ):
            setattr(content, field, fixture_asset(session, config, content_id, kind).id)
        content.media_version, content.status = content.version, "REVIEW_REQUIRED"
        session.commit()
    return content_id


@pytest.fixture
def approved_content(client, rendered_content):
    current = client.get(f"/api/v1/contents/{rendered_content}").json()
    current["script"]["facts"][0]["verified"] = True
    response = client.patch(
        f"/api/v1/contents/{rendered_content}",
        json={"version": current["version"], "script": current["script"]},
    )
    assert response.status_code == 200, response.text
    with Session(client.app.state.engine) as session:
        content = session.get(Content, rendered_content)
        session.add(
            QualityReview(
                content_id=content.id,
                content_version=content.version,
                video_asset_id=content.video_asset_id,
                status="PASS",
                report={
                    "version": "1.0",
                    "status": "PASS",
                    "video_asset_id": content.video_asset_id,
                    "checks": {},
                    "issues": [],
                    "warnings": [],
                    "recommended_action": "present_for_review",
                },
            )
        )
        session.commit()
    response = client.post(
        f"/api/v1/contents/{rendered_content}/approve",
        json={
            "version": response.json()["version"],
            "notes": "测试夹具审核",
            "compliance_confirmed": True,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()
