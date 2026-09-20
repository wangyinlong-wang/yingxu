from __future__ import annotations

import copy
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, event, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from .config import AppConfig, default_settings
from .models import Setting, Source, Template


def make_engine(config: AppConfig) -> Engine:
    config.prepare_directories()
    engine = create_engine(config.database_url, connect_args={"check_same_thread": False, "timeout": 15})

    @event.listens_for(engine, "connect")
    def configure_sqlite(connection, _record):
        cursor = connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=15000")
        cursor.close()

    return engine


def migrate(config: AppConfig) -> None:
    config.prepare_directories()
    backend = Path(__file__).resolve().parent.parent
    cfg = Config(str(backend / "alembic.ini"))
    cfg.set_main_option("script_location", str(backend / "migrations"))
    cfg.set_main_option("sqlalchemy.url", config.database_url.replace("%", "%%"))
    command.upgrade(cfg, "head")


def seed(engine: Engine, config: AppConfig) -> None:
    with Session(engine) as session:
        if not session.get(Setting, "app"):
            session.add(Setting(key="app", value=default_settings(config.timezone)))
        if session.scalar(select(Source.id).limit(1)) is None:
            session.add_all(
                [
                    Source(
                        name="Hacker News",
                        kind="hackernews",
                        url="https://hacker-news.firebaseio.com/v0/topstories.json",
                    ),
                    Source(name="Hugging Face 博客", kind="rss", url="https://huggingface.co/blog/feed.xml"),
                ]
            )
        if session.scalar(select(Template.id).limit(1)) is None:
            session.add_all(
                [
                    Template(
                        name="资讯快报",
                        style="brief",
                        description="一分钟讲清技术新进展、价值与边界。",
                        prompt="用简洁、准确的中文介绍一个 AI 或技术新进展。先交代发生了什么，再解释价值和限制。避免夸张标题。所有可验证说法必须引用提供的来源。",
                    ),
                    Template(
                        name="技术解读",
                        style="explain",
                        description="从问题到原理，解释技术变化意味着什么。",
                        prompt="围绕一个技术问题，按背景、工作原理、适用场景、已知限制组织中文解读。用具体例子解释，区分事实与推断，引用提供的来源。",
                    ),
                ]
            )
        session.commit()


def settings(session: Session, config: AppConfig | None = None) -> dict:
    record = session.get(Setting, "app")
    defaults = default_settings(config.timezone if config else "UTC")
    if not record:
        return defaults
    current = copy.deepcopy(record.value)
    # New optional capability fields acquire their documented defaults when reading older local settings.
    for kind, values in defaults["models"].items():
        current.setdefault("models", {}).setdefault(kind, {})
        for key, value in values.items():
            current["models"][kind].setdefault(key, value)
    for key, value in defaults["video"].items():
        current.setdefault("video", {}).setdefault(key, value)
    for key, value in defaults["publishing"].items():
        current.setdefault("publishing", {}).setdefault(key, value)
    for key, value in defaults["local_ai"].items():
        current.setdefault("local_ai", {}).setdefault(key, value)
    return current
