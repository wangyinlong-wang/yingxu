from __future__ import annotations

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import Approval, Asset, Content, QualityReview


def alembic_config(database_url: str) -> Config:
    backend = Path(__file__).resolve().parents[1]
    config = Config(str(backend / "alembic.ini"))
    config.set_main_option("script_location", str(backend / "migrations"))
    config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
    return config


def test_0003_upgrades_old_rows_defaults_unique_constraint_and_downgrade_boundary(tmp_path):
    database = tmp_path / "old.sqlite3"
    url = f"sqlite:///{database}"
    config = alembic_config(url)
    command.upgrade(config, "0002")
    engine = create_engine(url)
    stamp = "2026-09-13T00:00:00+00:00"
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO sources "
                "(name, kind, url, enabled, last_collected_at, last_error, id, created_at, updated_at) "
                "VALUES ('old', 'rss', 'https://example.org/feed', 1, NULL, NULL, "
                "'source-old', :stamp, :stamp)"
            ),
            {"stamp": stamp},
        )
        connection.execute(
            text(
                "INSERT INTO templates "
                "(name, description, prompt, style, version, history, id, created_at, updated_at) "
                "VALUES ('old', '', 'prompt', 'brief', 1, '[]', 'template-old', :stamp, :stamp)"
            ),
            {"stamp": stamp},
        )
        connection.execute(
            text(
                "INSERT INTO topics "
                "(source_id, title, url, fingerprint, summary, published_at, score, relevance_score, "
                "selection_reason, status, id, created_at, updated_at) VALUES "
                "('source-old', 'old topic', 'https://example.org/topic', "
                "'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', '', NULL, 0, 0, '', "
                "'SELECTED', 'topic-old', :stamp, :stamp)"
            ),
            {"stamp": stamp},
        )
        connection.execute(
            text(
                "INSERT INTO contents "
                "(topic_id, template_id, title, version, status, script, scenes, media_version, "
                "video_asset_id, cover_asset_id, subtitle_asset_id, id, created_at, updated_at) VALUES "
                "('topic-old', 'template-old', 'old content', 1, 'DRAFT', '{}', '[]', NULL, NULL, "
                "NULL, NULL, 'content-old', :stamp, :stamp)"
            ),
            {"stamp": stamp},
        )
        connection.execute(
            text(
                "INSERT INTO approvals "
                "(content_id, version, decision, snapshot_hash, notes, id, created_at, updated_at) VALUES "
                "('content-old', 1, 'APPROVED', 'legacy-hash', '', 'approval-old', :stamp, :stamp)"
            ),
            {"stamp": stamp},
        )
    command.upgrade(config, "0003")
    with Session(engine) as session:
        content = session.get(Content, "content-old")
        approval = session.get(Approval, "approval-old")
        assert content.preset_id == "tech_brief_video_v1"
        assert content.brief == {}
        assert approval.compliance == {}
        asset = Asset(
            content_id=content.id,
            kind="video",
            filename="fixture.mp4",
            relative_path="assets/fixture.mp4",
            mime_type="video/mp4",
            size=1,
            sha256="b" * 64,
        )
        session.add(asset)
        session.flush()
        session.add(
            QualityReview(
                content_id=content.id,
                content_version=content.version,
                video_asset_id=asset.id,
                status="PASS",
                report={"status": "PASS"},
            )
        )
        session.commit()
        session.add(
            QualityReview(
                content_id=content.id,
                content_version=content.version,
                video_asset_id=asset.id,
                status="REVISE",
                report={"status": "REVISE"},
            )
        )
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()
    command.downgrade(config, "0002")
    columns = {column["name"] for column in inspect(engine).get_columns("contents")}
    assert "preset_id" not in columns and "brief" not in columns
    assert "compliance" not in {
        column["name"] for column in inspect(engine).get_columns("approvals")
    }
    assert not {
        "quality_reviews",
        "demand_signals",
        "benchmarks",
        "experiments",
    }.intersection(inspect(engine).get_table_names())
