from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import JSON, Boolean, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def now() -> str:
    return datetime.now(UTC).isoformat()


def uid() -> str:
    return str(uuid4())


class Base(DeclarativeBase):
    pass


class Record:
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    created_at: Mapped[str] = mapped_column(String(40), default=now)
    updated_at: Mapped[str] = mapped_column(String(40), default=now, onupdate=now)


class Source(Record, Base):
    __tablename__ = "sources"
    name: Mapped[str] = mapped_column(String(200))
    kind: Mapped[str] = mapped_column(String(20))
    url: Mapped[str] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    last_collected_at: Mapped[str | None] = mapped_column(String(40))
    last_error: Mapped[str | None] = mapped_column(Text)


class Topic(Record, Base):
    __tablename__ = "topics"
    source_id: Mapped[str] = mapped_column(ForeignKey("sources.id"))
    title: Mapped[str] = mapped_column(String(1000))
    url: Mapped[str] = mapped_column(Text)
    fingerprint: Mapped[str] = mapped_column(String(64), unique=True)
    summary: Mapped[str] = mapped_column(Text, default="")
    published_at: Mapped[str | None] = mapped_column(String(40))
    score: Mapped[float] = mapped_column(Float, default=0)
    relevance_score: Mapped[float] = mapped_column(Float, default=0)
    selection_reason: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(20), default="NEW", index=True)


class Template(Record, Base):
    __tablename__ = "templates"
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text, default="")
    prompt: Mapped[str] = mapped_column(Text)
    style: Mapped[str] = mapped_column(String(20), default="brief")
    version: Mapped[int] = mapped_column(Integer, default=1)
    history: Mapped[list] = mapped_column(JSON, default=list)


class Content(Record, Base):
    __tablename__ = "contents"
    topic_id: Mapped[str] = mapped_column(ForeignKey("topics.id"), unique=True)
    template_id: Mapped[str] = mapped_column(ForeignKey("templates.id"))
    title: Mapped[str] = mapped_column(String(1000))
    preset_id: Mapped[str] = mapped_column(String(100), default="tech_brief_video_v1")
    brief: Mapped[dict] = mapped_column(JSON, default=dict)
    version: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(30), default="DRAFT", index=True)
    script: Mapped[dict] = mapped_column(JSON, default=dict)
    scenes: Mapped[list] = mapped_column(JSON, default=list)
    media_version: Mapped[int | None] = mapped_column(Integer)
    video_asset_id: Mapped[str | None] = mapped_column(String(36))
    cover_asset_id: Mapped[str | None] = mapped_column(String(36))
    subtitle_asset_id: Mapped[str | None] = mapped_column(String(36))


class ContentRevision(Record, Base):
    __tablename__ = "content_revisions"
    __table_args__ = (UniqueConstraint("content_id", "version"),)
    content_id: Mapped[str] = mapped_column(ForeignKey("contents.id"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    snapshot: Mapped[dict] = mapped_column(JSON)


class Asset(Record, Base):
    __tablename__ = "assets"
    content_id: Mapped[str | None] = mapped_column(ForeignKey("contents.id"), index=True)
    kind: Mapped[str] = mapped_column(String(20))
    filename: Mapped[str] = mapped_column(String(255))
    relative_path: Mapped[str] = mapped_column(Text, unique=True)
    mime_type: Mapped[str] = mapped_column(String(100))
    size: Mapped[int] = mapped_column(Integer)
    sha256: Mapped[str] = mapped_column(String(64))
    input_hash: Mapped[str | None] = mapped_column(String(64), index=True)


class Job(Record, Base):
    __tablename__ = "jobs"
    kind: Mapped[str] = mapped_column(String(40))
    queue: Mapped[str] = mapped_column(String(20), default="content", index=True)
    content_id: Mapped[str | None] = mapped_column(ForeignKey("contents.id"), index=True)
    status: Mapped[str] = mapped_column(String(30), default="QUEUED", index=True)
    idempotency_key: Mapped[str] = mapped_column(String(200), unique=True)
    request_hash: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    result: Mapped[dict] = mapped_column(JSON, default=dict)
    progress: Mapped[int] = mapped_column(Integer, default=0)
    current_step: Mapped[str] = mapped_column(String(200), default="等待执行")
    error_code: Mapped[str | None] = mapped_column(String(100))
    error_message: Mapped[str | None] = mapped_column(Text)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    lease_owner: Mapped[str | None] = mapped_column(String(100))
    lease_until: Mapped[str | None] = mapped_column(String(40))


class JobStep(Record, Base):
    __tablename__ = "job_steps"
    __table_args__ = (UniqueConstraint("job_id", "name"),)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(30))
    detail: Mapped[str] = mapped_column(Text, default="")
    started_at: Mapped[str | None] = mapped_column(String(40))
    completed_at: Mapped[str | None] = mapped_column(String(40))


class Approval(Record, Base):
    __tablename__ = "approvals"
    content_id: Mapped[str] = mapped_column(ForeignKey("contents.id"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    decision: Mapped[str] = mapped_column(String(20))
    snapshot_hash: Mapped[str] = mapped_column(String(64))
    notes: Mapped[str] = mapped_column(Text, default="")
    compliance: Mapped[dict] = mapped_column(JSON, default=dict)


class QualityReview(Record, Base):
    __tablename__ = "quality_reviews"
    __table_args__ = (UniqueConstraint("content_id", "content_version", "video_asset_id"),)
    content_id: Mapped[str] = mapped_column(ForeignKey("contents.id"), index=True)
    content_version: Mapped[int] = mapped_column(Integer)
    video_asset_id: Mapped[str] = mapped_column(ForeignKey("assets.id"))
    status: Mapped[str] = mapped_column(String(20))
    report: Mapped[dict] = mapped_column(JSON)


class Account(Record, Base):
    __tablename__ = "accounts"
    platform: Mapped[str] = mapped_column(String(20), unique=True)
    display_name: Mapped[str] = mapped_column(String(200))
    account_identifier: Mapped[str] = mapped_column(String(200))
    device_serial: Mapped[str] = mapped_column(String(200))


class Publication(Record, Base):
    __tablename__ = "publications"
    content_id: Mapped[str] = mapped_column(ForeignKey("contents.id"), index=True)
    content_version: Mapped[int] = mapped_column(Integer)
    content_title: Mapped[str] = mapped_column(String(1000))
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id"))
    platform: Mapped[str] = mapped_column(String(20))
    status: Mapped[str] = mapped_column(String(30), index=True)
    job_id: Mapped[str | None] = mapped_column(ForeignKey("jobs.id"))
    snapshot: Mapped[dict] = mapped_column(JSON)
    # A reservation persists while waiting for the human final click, including across restarts.
    reserved_serial: Mapped[str | None] = mapped_column(String(200), unique=True)
    work_url: Mapped[str | None] = mapped_column(Text)
    work_id: Mapped[str | None] = mapped_column(String(200))
    evidence: Mapped[dict] = mapped_column(JSON, default=dict)


class Metric(Record, Base):
    __tablename__ = "metrics"
    publication_id: Mapped[str] = mapped_column(ForeignKey("publications.id"), index=True)
    platform: Mapped[str] = mapped_column(String(20))
    content_title: Mapped[str] = mapped_column(String(1000))
    views: Mapped[int | None] = mapped_column(Integer)
    likes: Mapped[int | None] = mapped_column(Integer)
    comments: Mapped[int | None] = mapped_column(Integer)
    favorites: Mapped[int | None] = mapped_column(Integer)
    shares: Mapped[int | None] = mapped_column(Integer)
    source: Mapped[str] = mapped_column(String(20))
    raw: Mapped[dict] = mapped_column(JSON, default=dict)
    observed_at: Mapped[str] = mapped_column(String(40), default=now, index=True)


class DemandSignal(Record, Base):
    __tablename__ = "demand_signals"
    title: Mapped[str] = mapped_column(String(1000))
    source_kind: Mapped[str] = mapped_column(String(20))
    source_url: Mapped[str | None] = mapped_column(Text)
    evidence: Mapped[str] = mapped_column(Text, default="")
    scores: Mapped[dict] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(20), default="CANDIDATE", index=True)


class Benchmark(Record, Base):
    __tablename__ = "benchmarks"
    platform: Mapped[str] = mapped_column(String(20))
    account_name: Mapped[str] = mapped_column(String(200))
    content_url: Mapped[str | None] = mapped_column(Text)
    title: Mapped[str] = mapped_column(String(1000))
    content_type: Mapped[str] = mapped_column(String(20))
    analysis: Mapped[dict] = mapped_column(JSON)


class Experiment(Record, Base):
    __tablename__ = "experiments"
    name: Mapped[str] = mapped_column(String(500))
    hypothesis: Mapped[str] = mapped_column(Text)
    variable: Mapped[str] = mapped_column(String(30))
    status: Mapped[str] = mapped_column(String(20), default="DRAFT", index=True)
    content_ids: Mapped[list] = mapped_column(JSON, default=list)
    publication_ids: Mapped[list] = mapped_column(JSON, default=list)
    observation_windows: Mapped[list] = mapped_column(JSON, default=list)
    conclusion: Mapped[str] = mapped_column(Text, default="")
    next_action: Mapped[str] = mapped_column(String(30), default="undecided")


class Report(Record, Base):
    __tablename__ = "reports"
    date: Mapped[str] = mapped_column(String(10))
    body: Mapped[str] = mapped_column(Text)


class Setting(Base):
    __tablename__ = "settings"
    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[dict] = mapped_column(JSON)


class Schedule(Record, Base):
    __tablename__ = "schedules"
    name: Mapped[str] = mapped_column(String(200))
    kind: Mapped[str] = mapped_column(String(20))
    time: Mapped[str] = mapped_column(String(5))
    timezone: Mapped[str] = mapped_column(String(100))
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    enabled_at: Mapped[str | None] = mapped_column(String(40))
    daily_limit: Mapped[int] = mapped_column(Integer, default=1)
    last_run_at: Mapped[str | None] = mapped_column(String(40))
    last_scheduled_date: Mapped[str | None] = mapped_column(String(10))
    next_run_at: Mapped[str | None] = mapped_column(String(40))


class ModelCall(Record, Base):
    __tablename__ = "model_calls"
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"), index=True)
    request_key: Mapped[str] = mapped_column(String(64), unique=True)
    kind: Mapped[str] = mapped_column(String(20))
    model: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(30))
    attempts: Mapped[int] = mapped_column(Integer, default=1)
    response: Mapped[dict] = mapped_column(JSON, default=dict)
    usage: Mapped[dict] = mapped_column(JSON, default=dict)
    events: Mapped[list] = mapped_column(JSON, default=list)
    error_code: Mapped[str | None] = mapped_column(String(100))
    started_at: Mapped[str] = mapped_column(String(40), default=now, index=True)


class WorkerHeartbeat(Base):
    __tablename__ = "worker_heartbeats"
    queue: Mapped[str] = mapped_column(String(20), primary_key=True)
    worker_id: Mapped[str] = mapped_column(String(100))
    last_seen: Mapped[str] = mapped_column(String(40))
