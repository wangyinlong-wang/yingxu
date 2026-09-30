from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Job, ModelCall, Topic


def payload(client, **changes):
    return {
        "source_id": client.get("/api/v1/sources").json()["items"][0]["id"],
        "title": "测试更新公告",
        "url": "https://x.com/Example/status/123456789?s=20",
        "summary": "测试原帖正文。导入并不意味着完成事实核验。",
        **changes,
    }


def test_import_creates_topic_and_uses_existing_draft_flow(client, engine):
    data = payload(client)
    result = client.post("/api/v1/topics/import", json=data)
    assert result.status_code == 200, result.text
    topic = result.json()["topic"]
    assert result.json()["created"] is True
    assert topic["summary"] == data["summary"]
    assert topic["url"] == "https://x.com/example/status/123456789"
    assert topic["status"] == "NEW"
    assert topic["published_at"] is None
    assert "人工录入" in topic["selection_reason"]
    draft = client.post(f"/api/v1/topics/{topic['id']}/content", json={})
    assert draft.status_code == 201
    assert draft.json()["status"] == "DRAFT"
    assert draft.json()["script"]["facts"] == []
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(Job)) == 0
        assert session.scalar(select(func.count()).select_from(ModelCall)) == 0


def test_reimport_reuses_topic_and_draft_without_overwriting(client):
    data = payload(client)
    first = client.post("/api/v1/topics/import", json=data).json()["topic"]
    draft = client.post(f"/api/v1/topics/{first['id']}/content", json={}).json()
    repeat = client.post("/api/v1/topics/import", json={
        **data, "url": "https://twitter.com/EXAMPLE/status/123456789#details",
        "title": "不得覆盖旧标题", "summary": "不得覆盖已经使用的正文",
    }).json()
    assert repeat["created"] is False
    assert repeat["topic"]["title"] == first["title"]
    assert repeat["topic"]["summary"] == first["summary"]
    existing = client.post(f"/api/v1/topics/{first['id']}/content", json={}).json()
    assert existing["id"] == draft["id"]
    assert existing["version"] == draft["version"]


def test_concurrent_import_is_idempotent(client, engine):
    data = payload(client)
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _: client.post("/api/v1/topics/import", json=data), range(2)))
    assert all(response.status_code == 200 for response in responses)
    assert sorted(response.json()["created"] for response in responses) == [False, True]
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(Topic)) == 1


@pytest.mark.parametrize("changes", [
    {"url": "javascript:alert(1)"},
    {"url": "http://127.0.0.1/private"},
    {"url": "https://user:secret@example.com/article"},
    {"url": "https://x.com/Example"},
    {"title": "   "},
    {"summary": ""},
])
def test_invalid_import_is_rejected_without_creating_topic(client, engine, changes):
    response = client.post("/api/v1/topics/import", json=payload(client, **changes))
    assert response.status_code == 422
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(Topic)) == 0


def test_import_requires_existing_source(client):
    response = client.post("/api/v1/topics/import", json=payload(client, source_id="missing"))
    assert response.status_code == 404
