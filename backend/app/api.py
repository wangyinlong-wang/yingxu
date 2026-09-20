from __future__ import annotations

import copy
import hashlib
import json
import re
import shutil
import subprocess
import sys
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated
from urllib.parse import urlsplit

from fastapi import APIRouter, Body, Depends, File, Form, Header, Query, Request, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from PIL import Image, UnidentifiedImageError
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import __version__
from .config import AppConfig
from .db import settings
from .integrations.local_ai import local_status
from .models import (
    Account,
    Approval,
    Asset,
    Benchmark,
    Content,
    DemandSignal,
    Experiment,
    Job,
    Metric,
    ModelCall,
    Publication,
    QualityReview,
    Report,
    Schedule,
    Setting,
    Source,
    Template,
    Topic,
    WorkerHeartbeat,
    now,
    uid,
)
from .presets import list_presets, preset_by_id
from .schemas import (
    AccountInput,
    AllSettings,
    BenchmarkCreate,
    BenchmarkPatch,
    BriefSelect,
    ContentCreate,
    ContentPatch,
    DemandSignalCreate,
    DemandSignalPatch,
    DeviceInspect,
    ExperimentCreate,
    ExperimentPatch,
    Generation,
    LocalAIPreview,
    LocalAITranscribe,
    MetricInput,
    ProductionBrief,
    PublicationCancel,
    PublicationConfirm,
    PublicationCreate,
    PublicationMediaSelectionConfirm,
    ResolveJob,
    Review,
    ScheduleInput,
    Script,
    SourceCollect,
    SourceCreate,
    SourcePatch,
    TemplateInput,
    canonical_x_profile_url,
)
from .services import (
    AppError,
    accepted,
    acquire_write,
    approved_snapshot_matches,
    assert_editable,
    assert_models,
    asset_dict,
    asset_path,
    checked_asset,
    content_snapshot,
    current_quality_review,
    day_bounds,
    digest,
    enqueue,
    enqueue_publication,
    job_dict,
    local_ai_profile_snapshot,
    local_voice_options,
    normalized_work,
    publication_dict,
    publication_snapshot_matches,
    publishable_script,
    require,
    row,
    save_revision,
    usage_summary,
    validate_local_ai_settings,
)

router = APIRouter(prefix="/api/v1")


def database(request: Request):
    with Session(request.app.state.engine, expire_on_commit=False) as session:
        session.info["config"] = request.app.state.config
        yield session


def environment(request: Request) -> AppConfig:
    return request.app.state.config


DB = Annotated[Session, Depends(database)]
Env = Annotated[AppConfig, Depends(environment)]
Key = Annotated[str | None, Header(alias="Idempotency-Key", max_length=180)]


def listed(items: list) -> dict:
    return {"items": items, "total": len(items)}


def current_content_status(content: Content) -> str:
    return (
        "REVIEW_REQUIRED" if content.video_asset_id and content.media_version == content.version else "DRAFT"
    )


def _normalized_scenes(scenes: list[dict]) -> list[dict]:
    normalized = copy.deepcopy(scenes)
    for scene in normalized:
        scene.setdefault("motion", "auto")
        scene.setdefault("transition", "fade")
        scene.setdefault("scene_type", "image")
        scene.setdefault("visual_data", {})
        scene.setdefault("hero_moment", False)
        scene.setdefault("shot_intent", "")
    return normalized


def content_dict(session: Session, content: Content) -> dict:
    result = row(content)
    result["scenes"] = _normalized_scenes(result["scenes"])
    review = current_quality_review(session, content)
    result["latest_quality_review"] = row(review) if review else None
    return result


@router.get("/health")
def health():
    return {"status": "ok", "version": __version__}


def diagnostics_data(session: Session, config: AppConfig) -> dict:
    ffmpeg = shutil.which("ffmpeg")
    version = None
    if ffmpeg:
        try:
            version = subprocess.run(
                [ffmpeg, "-version"], capture_output=True, text=True, timeout=3, check=True
            ).stdout.splitlines()[0]
        except (OSError, subprocess.SubprocessError):
            ffmpeg = None
    adb = shutil.which("adb")
    adb_version = None
    adb_supported = False
    if adb:
        try:
            adb_output = subprocess.run(
                [adb, "version"], capture_output=True, text=True, timeout=3, check=True
            ).stdout
            match = re.search(r"Android Debug Bridge version (\d+)\.(\d+)\.(\d+)", adb_output)
            adb_version = ".".join(match.groups()) if match else None
            adb_supported = bool(match and tuple(map(int, match.groups())) >= (1, 0, 41))
        except (OSError, subprocess.SubprocessError):
            adb = None
    current = settings(session, config)
    local = local_status(config)
    local_speech_ready = False
    if current["local_ai"]["voice_enabled"]:
        try:
            validate_local_ai_settings(
                session,
                config,
                current["local_ai"],
                require_voice=True,
                require_avatar=False,
                validate_references=False,
                status=local,
            )
            local_speech_ready = True
        except AppError:
            pass
    workers = {}
    for queue in ("content", "device"):
        heartbeat = session.get(WorkerHeartbeat, queue)
        online = bool(
            heartbeat
            and datetime.fromisoformat(heartbeat.last_seen) > datetime.now(UTC) - timedelta(seconds=30)
        )
        workers[queue] = {"online": online, "last_seen": heartbeat.last_seen if heartbeat else None}
    model_status = {
        kind: {
            "configured": bool(
                config.key(kind) and values["base_url"] and values["model"] and values["enabled"]
            )
        }
        for kind, values in current["models"].items()
    }
    if current["local_ai"]["voice_enabled"]:
        model_status["speech"]["configured"] = local_speech_ready
    model_status["speech"]["local"] = local_speech_ready
    warnings = []
    if not ffmpeg:
        warnings.append("未找到 FFmpeg，视频合成暂不可用")
    if not adb:
        warnings.append("未找到 ADB；真机操作需要先安装 Android Platform Tools 并由你完成 USB 调试授权")
    elif not adb_supported:
        warnings.append("ADB 版本过旧；真机发布要求 Android Debug Bridge 1.0.41 或更新版本")
    if not all(item["configured"] for item in model_status.values()):
        warnings.append("模型能力尚未全部配置；请在本地 .env 设置对应密钥，再在设置中填写并启用接口")
    if not workers["content"]["online"]:
        warnings.append("内容任务进程未连接，已提交的任务将保留在队列中")
    return {
        "python": sys.version.split()[0],
        "timezone": current["timezone"],
        "ffmpeg": {"available": bool(ffmpeg), "version": version},
        "ffprobe": {"available": bool(shutil.which("ffprobe"))},
        "adb": {"available": bool(adb), "version": adb_version, "supported": adb_supported},
        "models": model_status,
        "storage": {"free_bytes": shutil.disk_usage(config.data_dir).free},
        "workers": workers,
        "warnings": warnings,
    }


@router.get("/diagnostics")
def diagnostics(session: DB, config: Env):
    return diagnostics_data(session, config)


@router.get("/dashboard")
def dashboard(session: DB, config: Env):
    counts = {
        "topics": session.scalar(select(func.count(Topic.id))) or 0,
        "contents": session.scalar(select(func.count(Content.id))) or 0,
        "review_required": session.scalar(
            select(func.count(Content.id)).where(Content.status == "REVIEW_REQUIRED")
        )
        or 0,
        "published": session.scalar(
            select(func.count(Publication.id)).where(Publication.status == "PUBLISHED")
        )
        or 0,
        "failed_jobs": session.scalar(select(func.count(Job.id)).where(Job.status == "FAILED")) or 0,
    }
    return {
        "counts": counts,
        "recent_jobs": [
            job_dict(session, item, False)
            for item in session.scalars(select(Job).order_by(Job.created_at.desc()).limit(8))
        ],
        "recent_contents": [
            row(item)
            for item in session.scalars(select(Content).order_by(Content.updated_at.desc()).limit(8))
        ],
        "usage": usage_summary(session, settings(session, config)["timezone"]),
        "environment": diagnostics_data(session, config),
    }


@router.get("/sources")
def list_sources(session: DB):
    return listed([row(item) for item in session.scalars(select(Source).order_by(Source.created_at))])


@router.get("/presets")
def presets():
    return listed(list_presets())


@router.post("/sources", status_code=201)
def create_source(data: SourceCreate, session: DB):
    item = Source(**data.model_dump())
    session.add(item)
    session.commit()
    return row(item)


@router.patch("/sources/{identifier}")
def update_source(identifier: str, data: SourcePatch, session: DB):
    acquire_write(session)
    item = require(session, Source, identifier)
    changes = data.model_dump(exclude_none=True)
    if "kind" in changes and item.kind not in {"x", "x_tikhub"}:
        raise AppError("SOURCE_KIND_CHANGE_FORBIDDEN", "仅可在两个 X 数据提供方式之间切换", 409)
    effective_kind = changes.get("kind", item.kind)
    if effective_kind in {"x", "x_tikhub"} and ("url" in changes or "kind" in changes):
        try:
            changes["url"] = canonical_x_profile_url(changes.get("url", item.url))
        except ValueError:
            raise AppError("SOURCE_URL_INVALID", "X 来源必须是公开账号主页地址", 422) from None
    for key, value in changes.items():
        setattr(item, key, value)
    session.commit()
    return row(item)


@router.post("/sources/{identifier}/collect", status_code=202)
def collect_source_route(
    identifier: str,
    session: DB,
    config: Env,
    data: Annotated[SourceCollect | None, Body()] = None,
    key: Key = None,
):
    acquire_write(session)
    source = require(session, Source, identifier)
    if not source.enabled:
        raise AppError("SOURCE_DISABLED", "请先启用该信息源", 409)
    expected_kind = data.expected_kind if data else None
    if expected_kind is not None and expected_kind != source.kind:
        raise AppError("SOURCE_PROVIDER_CHANGED", "信息源提供方式已变化，请刷新来源页面后重试", 409)
    if source.kind == "x_tikhub" and expected_kind != "x_tikhub":
        raise AppError("SOURCE_PROVIDER_CHANGED", "请刷新来源页面并明确确认使用 TikHub 后再采集", 409)
    snapshot = {"id": source.id, "kind": source.kind, "url": source.url}
    payload = {
        "source_id": identifier,
        "source_snapshot": snapshot,
        "expected_kind": expected_kind,
    }
    if key:
        existing = session.scalar(select(Job).where(Job.idempotency_key == key))
        if existing:
            if existing.kind != "COLLECT_SOURCE" or existing.payload != payload:
                raise AppError("IDEMPOTENCY_CONFLICT", "同一操作标识已用于不同采集请求", 409)
            return accepted(existing)
    if source.kind == "x_tikhub":
        if not config.tikhub_api_key.get_secret_value():
            raise AppError("TIKHUB_NOT_CONFIGURED", "请先在本机配置 TikHub API Key", 409)
        current = settings(session, config)
        if usage_summary(session, current["timezone"])["calls_today"] >= current["limits"][
            "daily_calls"
        ]:
            raise AppError("DAILY_CALL_LIMIT", "今日模型与数据 API 调用额度已用完", 409)
    job = enqueue(session, kind="COLLECT_SOURCE", payload=payload, key=key)
    session.commit()
    return accepted(job)


@router.get("/topics")
def list_topics(session: DB, q: str = "", limit: int = Query(100, ge=1, le=500)):
    statement = select(Topic)
    if q:
        statement = statement.where(
            Topic.title.contains(q, autoescape=True) | Topic.summary.contains(q, autoescape=True)
        )
    items = session.scalars(
        statement.order_by(Topic.relevance_score.desc(), Topic.created_at.desc()).limit(limit)
    )
    return listed([row(item) for item in items])


@router.post("/topics/{identifier}/content", status_code=201)
def topic_to_content(identifier: str, data: ContentCreate, session: DB):
    acquire_write(session)
    topic = require(session, Topic, identifier)
    existing = session.scalar(select(Content).where(Content.topic_id == identifier))
    if existing:
        return row(existing)
    if not preset_by_id(data.preset_id):
        raise AppError("PRESET_NOT_FOUND", "所选生产预设不存在", 404)
    template = (
        require(session, Template, data.template_id)
        if data.template_id
        else session.scalar(select(Template).order_by(Template.created_at))
    )
    content = Content(
        topic_id=identifier,
        template_id=template.id,
        preset_id=data.preset_id,
        title=topic.title,
        script=Script().model_dump(),
    )
    session.add(content)
    topic.status = "SELECTED"
    save_revision(session, content)
    session.commit()
    return row(content)


@router.post("/topics/{identifier}/dismiss")
def dismiss_topic(identifier: str, session: DB):
    topic = require(session, Topic, identifier)
    topic.status = "DISMISSED"
    session.commit()
    return row(topic)


@router.get("/contents")
def list_contents(session: DB, q: str = "", status: str = ""):
    statement = select(Content)
    if q:
        statement = statement.where(Content.title.contains(q, autoescape=True))
    if status:
        statement = statement.where(Content.status == status)
    return listed(
        [row(item) for item in session.scalars(statement.order_by(Content.updated_at.desc()).limit(300))]
    )


@router.get("/contents/{identifier}")
def get_content(identifier: str, session: DB):
    return content_dict(session, require(session, Content, identifier))


@router.post("/contents/{identifier}/brief/select")
def select_brief(identifier: str, data: BriefSelect, session: DB):
    acquire_write(session)
    content = require(session, Content, identifier)
    assert_editable(session, content, data.version)
    if not content.brief:
        raise AppError("BRIEF_REQUIRED", "请先生成 Production Brief", 409)
    try:
        brief = ProductionBrief.model_validate(content.brief)
    except ValidationError:
        raise AppError("BRIEF_INVALID", "Production Brief 结构无效，请重新生成", 409) from None
    if data.concept_id not in {concept.id for concept in brief.concepts}:
        raise AppError("BRIEF_CONCEPT_NOT_FOUND", "所选创意方案不存在", 404)
    brief.selected_concept_id = data.concept_id
    brief.selected_at = now()
    content.brief = brief.model_dump()
    content.version += 1
    content.script = Script().model_dump()
    content.scenes = []
    content.media_version = None
    content.video_asset_id = content.cover_asset_id = content.subtitle_asset_id = None
    content.status = "DRAFT"
    save_revision(session, content)
    session.commit()
    return content_dict(session, content)


@router.get("/contents/{identifier}/quality-reviews")
def list_quality_reviews(identifier: str, session: DB):
    require(session, Content, identifier)
    items = session.scalars(
        select(QualityReview)
        .where(QualityReview.content_id == identifier)
        .order_by(QualityReview.created_at.desc(), QualityReview.id.desc())
    )
    return listed([row(item) for item in items])


@router.post("/contents/{identifier}/quality-review", status_code=202)
def retry_quality_review(identifier: str, session: DB, config: Env, key: Key = None):
    acquire_write(session)
    content = require(session, Content, identifier)
    if not content.video_asset_id or content.media_version != content.version:
        raise AppError("VIDEO_REQUIRED", "请先生成当前版本视频", 409)
    checked_asset(session, config, content.video_asset_id, {"video"})
    payload = {
        "content_id": content.id,
        "version": content.version,
        "video_asset_id": content.video_asset_id,
    }
    existing = session.scalar(
        select(Job).where(
            Job.kind == "QUALITY_REVIEW",
            Job.content_id == content.id,
            Job.status.in_(["QUEUED", "RUNNING"]),
        )
    )
    if existing and existing.payload == payload:
        return accepted(existing)
    job = enqueue(
        session,
        kind="QUALITY_REVIEW",
        content_id=content.id,
        payload=payload,
        key=key,
    )
    session.commit()
    return accepted(job)


@router.patch("/contents/{identifier}")
def edit_content(identifier: str, data: ContentPatch, session: DB, config: Env):
    acquire_write(session)
    content = require(session, Content, identifier)
    assert_editable(session, content, data.version)
    changes = {key: value for key, value in data.model_dump(exclude={"version"}).items() if value is not None}
    if "template_id" in changes:
        require(session, Template, changes["template_id"])
    if "preset_id" in changes and not preset_by_id(changes["preset_id"]):
        raise AppError("PRESET_NOT_FOUND", "所选生产预设不存在", 404)
    if "scenes" in changes:
        scenes = changes["scenes"]
        if len({scene["id"] for scene in scenes}) != len(scenes):
            raise AppError("DUPLICATE_SCENE", "分镜标识不能重复", 422)
        old = {scene["id"]: scene for scene in content.scenes}
        for index, scene in enumerate(scenes):
            scene["order"] = index
            previous = old.get(scene["id"], {})
            scene_input = data.scenes[index]
            for field, default in (("motion", "auto"), ("transition", "fade")):
                if field not in scene_input.model_fields_set and previous:
                    scene[field] = previous.get(field, default)
            if scene["image_prompt"] != previous.get("image_prompt") and scene[
                "image_asset_id"
            ] == previous.get("image_asset_id"):
                scene["image_asset_id"] = None
            if (
                scene["image_asset_id"] == previous.get("image_asset_id")
                and (
                    scene.get("scene_type", "image") != previous.get("scene_type", "image")
                    or scene.get("visual_data", {}) != previous.get("visual_data", {})
                )
            ):
                scene["image_asset_id"] = None
            if scene["narration"] != previous.get("narration") and scene["audio_asset_ids"] == previous.get(
                "audio_asset_ids", []
            ):
                scene["audio_asset_ids"] = []
            if scene["image_asset_id"]:
                checked_asset(session, config, scene["image_asset_id"], {"image"})
            for asset_id in scene["audio_asset_ids"]:
                checked_asset(session, config, asset_id, {"audio"})
    if all(
        _normalized_scenes(content.scenes) == _normalized_scenes(value)
        if key == "scenes"
        else getattr(content, key) == value
        for key, value in changes.items()
    ):
        return row(content)
    visual_changed = any(
        name in changes
        and (
            _normalized_scenes(content.scenes) != _normalized_scenes(changes[name])
            if name == "scenes"
            else getattr(content, name) != changes[name]
        )
        for name in ("title", "scenes", "template_id", "preset_id")
    )
    had_current_video = bool(content.video_asset_id and content.media_version == content.version)
    for name, value in changes.items():
        setattr(content, name, value)
    content.version += 1
    if visual_changed:
        content.media_version = None
        content.video_asset_id = content.cover_asset_id = content.subtitle_asset_id = None
    elif had_current_video:
        # Fact review and platform copy change the approved snapshot, but not pixels or narration.
        content.media_version = content.version
    content.status = current_content_status(content)
    save_revision(session, content)
    session.commit()
    return row(content)


@router.post("/contents/{identifier}/generate", status_code=202)
def generate_content(identifier: str, data: Generation, session: DB, config: Env, key: Key = None):
    acquire_write(session)
    content = require(session, Content, identifier)
    payload = {
        "content_id": identifier,
        "version": content.version,
        "stage": data.stage,
        "scene_id": data.scene_id,
    }
    if data.stage == "motion":
        payload["motion_input"] = {
            "title": content.title,
            "scenes": [
                {
                    key: copy.deepcopy(scene.get(key, ""))
                    for key in ("id", "caption", "narration", "image_prompt")
                }
                for scene in content.scenes
            ],
        }
    if key:
        existing = session.scalar(select(Job).where(Job.idempotency_key == key))
        if existing:
            if (
                existing.kind != "GENERATE_CONTENT"
                or existing.payload.get("content_id") != identifier
                or existing.payload.get("stage") != data.stage
                or existing.payload.get("scene_id") != data.scene_id
            ):
                raise AppError("IDEMPOTENCY_CONFLICT", "同一操作标识已用于不同请求", 409)
            return accepted(existing)
    assert_editable(session, content, content.version)
    if data.stage in {"script", "all"} and content.brief and not content.brief.get(
        "selected_concept_id"
    ):
        raise AppError("BRIEF_SELECTION_REQUIRED", "请先选择一个创意方案", 409)
    if data.stage != "render":
        restored = session.scalar(
            select(Job.id).where(Job.content_id == identifier, Job.error_code == "RESTORE_REVIEW_REQUIRED")
        )
        if restored:
            raise AppError(
                "RESTORE_REVIEW_REQUIRED", "此内容存在恢复后的待核验任务，请先在任务中心确认实际执行状态", 409
            )
        pending = session.scalar(
            select(ModelCall)
            .join(Job, Job.id == ModelCall.job_id)
            .where(Job.content_id == identifier, ModelCall.status.in_(["STARTED", "UNKNOWN"]))
        )
        if pending:
            raise AppError(
                "MODEL_OUTCOME_UNKNOWN",
                "此内容有待核验的模型调用，请先在任务中心处理原记录",
                409,
                {"original_job_id": pending.job_id},
            )
    if data.stage in {"brief", "script"}:
        assert_models(session, config, ("text",))
    elif data.stage == "all":
        required = set()
        if not content.scenes:
            required.add("text")
            required.update(["image", "speech"])
        else:
            if any(
                scene.get("scene_type", "image") == "image" and not scene.get("image_asset_id")
                for scene in content.scenes
            ):
                required.add("image")
            if any(not scene.get("audio_asset_ids") for scene in content.scenes):
                required.add("speech")
        if required:
            assert_models(session, config, tuple(required))
    elif data.stage == "assets":
        if not content.scenes:
            raise AppError("SCRIPT_REQUIRED", "请先生成或保存分镜", 409)
        required = tuple(
            kind
            for kind, missing in (
                ("image", any(not s.get("image_asset_id") for s in content.scenes)),
                ("speech", any(not s.get("audio_asset_ids") for s in content.scenes)),
            )
            if missing
            and (
                kind != "image"
                or any(
                    scene.get("scene_type", "image") == "image"
                    and not scene.get("image_asset_id")
                    for scene in content.scenes
                )
            )
        )
        if required:
            assert_models(session, config, required)
    elif data.stage == "local_voice":
        if not content.scenes:
            raise AppError("SCRIPT_REQUIRED", "请先生成或保存分镜", 409)
        if any(not scene.get("image_asset_id") for scene in content.scenes):
            raise AppError("IMAGES_REQUIRED", "每个分镜都需要图片，才能改用本人声音生成视频", 409)
        if any(not str(scene.get("narration") or "").strip() for scene in content.scenes):
            raise AppError("NARRATION_REQUIRED", "每个分镜都需要旁白，才能改用本人声音", 409)
        for scene in content.scenes:
            checked_asset(session, config, scene["image_asset_id"], {"image"})
    elif data.stage == "motion":
        if not content.scenes:
            raise AppError("SCRIPT_REQUIRED", "请先生成或保存分镜", 409)
        if any(not str(scene.get("narration") or "").strip() for scene in content.scenes):
            raise AppError("NARRATION_REQUIRED", "每个分镜都需要旁白，才能编排动画", 409)
        if any(
            not scene.get("image_asset_id") or not scene.get("audio_asset_ids")
            for scene in content.scenes
        ):
            raise AppError("ASSETS_REQUIRED", "每个分镜都需要图片和配音，才能编排动画并合成", 409)
        for scene in content.scenes:
            checked_asset(session, config, scene["image_asset_id"], {"image"})
            for asset_id in scene["audio_asset_ids"]:
                checked_asset(session, config, asset_id, {"audio"})
        current = assert_models(session, config, ("text",))
        if usage_summary(session, current["timezone"])["calls_today"] >= current["limits"][
            "daily_calls"
        ]:
            raise AppError("DAILY_CALL_LIMIT", "已达到今日模型调用上限，请调整额度或次日重试", 409)
    elif data.stage == "images":
        if not content.scenes:
            raise AppError("SCRIPT_REQUIRED", "请先生成或保存分镜", 409)
        scene_ids = [scene.get("id") for scene in content.scenes]
        if data.scene_id is not None and data.scene_id not in scene_ids:
            raise AppError("SCENE_NOT_FOUND", "找不到需要重绘的分镜", 404)
        targets = {data.scene_id} if data.scene_id is not None else set(scene_ids)
        if any(not str(scene.get("narration") or "").strip() for scene in content.scenes):
            raise AppError("NARRATION_REQUIRED", "每个分镜都需要旁白，才能重绘配图并合成", 409)
        if any(not scene.get("audio_asset_ids") for scene in content.scenes):
            raise AppError("AUDIO_REQUIRED", "每个分镜都需要配音，才能重绘配图并合成", 409)
        for scene in content.scenes:
            for asset_id in scene["audio_asset_ids"]:
                checked_asset(session, config, asset_id, {"audio"})
            if scene["id"] not in targets:
                if not scene.get("image_asset_id"):
                    raise AppError("IMAGES_REQUIRED", "未重绘的分镜必须已有图片", 409)
                checked_asset(session, config, scene["image_asset_id"], {"image"})
        image_targets = [
            scene
            for scene in content.scenes
            if scene["id"] in targets and scene.get("scene_type", "image") == "image"
        ]
        current = assert_models(session, config, ("image",)) if image_targets else settings(session, config)
        calls_today = usage_summary(session, current["timezone"])["calls_today"]
        if calls_today + len(image_targets) > current["limits"]["daily_calls"]:
            raise AppError("DAILY_CALL_LIMIT", "剩余模型调用额度不足以完成本次重绘", 409)
        payload["image_scene_ids"] = [scene_id for scene_id in scene_ids if scene_id in targets]
    if data.stage in {"all", "render", "local_voice", "motion", "images"}:
        current = settings(session, config)
        start, end = day_bounds(current["timezone"])
        existing_today = list(
            session.scalars(
                select(Job.content_id)
                .where(
                    Job.kind == "GENERATE_CONTENT",
                    Job.created_at >= start,
                    Job.created_at < end,
                    Job.status != "CANCELED",
                )
                .distinct()
            )
        )
        if identifier not in existing_today and len(existing_today) >= current["limits"]["daily_videos"]:
            raise AppError("DAILY_VIDEO_LIMIT", "今日内容额度已用完；可在设置中调整每日成片数", 409)
        if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
            raise AppError("FFMPEG_REQUIRED", "请先安装 FFmpeg 与 ffprobe", 409)
    if data.stage == "render" and (
        not content.scenes
        or any(not s.get("image_asset_id") or not s.get("audio_asset_ids") for s in content.scenes)
    ):
        raise AppError("ASSETS_REQUIRED", "每个分镜都需要图片和配音，才能合成视频", 409)
    if data.stage == "images":
        scenes = copy.deepcopy(content.scenes)
        targets = set(payload["image_scene_ids"])
        for scene in scenes:
            if scene["id"] in targets:
                scene["image_asset_id"] = None
        content.scenes = scenes
        content.version += 1
        content.media_version = None
        content.video_asset_id = content.cover_asset_id = content.subtitle_asset_id = None
        payload["version"] = content.version
        save_revision(session, content)
    elif data.stage == "brief" and (
        bool(content.brief)
        or content.script != Script().model_dump()
        or bool(content.scenes)
        or content.media_version is not None
        or bool(content.video_asset_id or content.cover_asset_id or content.subtitle_asset_id)
        or content.status in {"APPROVED", "REJECTED", "REVIEW_REQUIRED"}
        or bool(
            session.scalar(
                select(Asset.id).where(Asset.content_id == content.id, Asset.kind == "video").limit(1)
            )
        )
    ):
        save_revision(session, content)
        content.version += 1
        content.brief = {}
        content.media_version = None
        content.video_asset_id = content.cover_asset_id = content.subtitle_asset_id = None
        content.status = "DRAFT"
        payload["version"] = content.version
        save_revision(session, content)
    elif data.stage == "local_voice":
        scenes = copy.deepcopy(content.scenes)
        for scene in scenes:
            scene["audio_asset_ids"] = []
        content.scenes = scenes
        content.version += 1
        content.media_version = None
        content.video_asset_id = content.cover_asset_id = content.subtitle_asset_id = None
        payload["version"] = content.version
        save_revision(session, content)
    elif data.stage != "brief" and (content.video_asset_id or (data.stage == "script" and content.scenes)):
        content.version += 1
        content.media_version = None
        content.video_asset_id = content.cover_asset_id = content.subtitle_asset_id = None
        payload["version"] = content.version
        save_revision(session, content)
    needs_voice = data.stage == "local_voice" or (
        data.stage in {"all", "assets"}
        and (not content.scenes or any(not scene.get("audio_asset_ids") for scene in content.scenes))
    )
    payload["local_ai"] = local_ai_profile_snapshot(
        session,
        config,
        include_voice=needs_voice,
        include_avatar=data.stage in {"all", "render", "local_voice", "motion", "images"},
        force_voice=data.stage == "local_voice",
    )
    job = enqueue(session, kind="GENERATE_CONTENT", payload=payload, content_id=identifier, key=key)
    content.status = "GENERATING"
    session.commit()
    return accepted(job)


def review_content(identifier: str, data: Review, decision: str, session: Session, config: AppConfig):
    acquire_write(session)
    content = require(session, Content, identifier)
    assert_editable(session, content, data.version)
    if decision == "APPROVED":
        if (
            data.auto_publish_expected is not None
            and data.auto_publish_expected
            != settings(session, config)["publishing"]["auto_publish_after_approval"]
        ):
            raise AppError("PUBLISH_MODE_CHANGED", "发布方式已变化，请重新查看审核确认说明", 409)
        if content.brief:
            try:
                brief = ProductionBrief.model_validate(content.brief)
            except ValidationError:
                raise AppError(
                    "BRIEF_INVALID", "Production Brief 结构无效，请重新生成", 409
                ) from None
            if not brief.selected_concept_id:
                raise AppError("BRIEF_SELECTION_REQUIRED", "请先选择一个创意方案", 409)
        if not content.video_asset_id or content.media_version != content.version:
            raise AppError("CURRENT_VIDEO_REQUIRED", "请先生成当前版本的成片", 409)
        checked_asset(session, config, content.video_asset_id, {"video"})
        facts = content.script.get("facts", [])
        if not facts or any(not fact.get("verified") or not fact.get("source_url") for fact in facts):
            raise AppError("FACTS_REQUIRE_REVIEW", "请核对每条事实的来源，并标记为已核实", 409)
        for fact in facts:
            parsed = urlsplit(fact["source_url"])
            if (
                parsed.scheme not in {"http", "https"}
                or not parsed.hostname
                or parsed.username
                or parsed.password
            ):
                raise AppError("FACT_SOURCE_INVALID", "事实来源必须为有效的 HTTP(S) 链接", 422)
        publishable_script(content.script)
        if not data.compliance_confirmed:
            raise AppError(
                "COMPLIANCE_CONFIRMATION_REQUIRED",
                "请确认已逐项检查标题、描述、旁白、画面与素材的法律和两平台合规性",
                409,
            )
        checks = data.compliance_checks.model_dump() if data.compliance_checks else None
        if checks is not None and not all(checks.values()):
            raise AppError(
                "COMPLIANCE_CHECKS_REQUIRED",
                "请完成全部八项结构化合规确认",
                409,
            )
        quality = current_quality_review(session, content)
        if not quality:
            raise AppError("QUALITY_REVIEW_REQUIRED", "请先对当前成片执行质量检查", 409)
        if quality.status != "PASS":
            raise AppError(
                "QUALITY_REVIEW_NOT_PASSED",
                "当前成片质量检查未通过，不能审核通过",
                409,
                {"quality_review_id": quality.id, "status": quality.status},
            )
        compliance = {
            "rule_version": "structured-compliance-v1",
            "content_version": content.version,
            "checks": checks or {},
            "notes": data.notes,
            "legacy_boolean_only": checks is None,
        }
    else:
        compliance = {}
    content.status = decision
    session.add(
        Approval(
            content_id=identifier,
            version=content.version,
            decision=decision,
            snapshot_hash=digest(content_snapshot(content)),
            notes=data.notes,
            compliance=compliance,
        )
    )
    automatic_jobs = []
    if decision == "APPROVED" and settings(session, config)["publishing"]["auto_publish_after_approval"]:
        for account in session.scalars(select(Account).order_by(Account.platform)):
            if session.scalar(
                select(Publication.id).where(
                    Publication.content_id == identifier,
                    Publication.account_id == account.id,
                    Publication.status != "CANCELED",
                )
            ):
                continue
            _, job = enqueue_publication(session, config, content, account, automatic=True)
            automatic_jobs.append(job.id)
    session.commit()
    return {**content_dict(session, content), "auto_publish_job_ids": automatic_jobs}


@router.post("/contents/{identifier}/approve")
def approve_content(identifier: str, data: Review, session: DB, config: Env):
    return review_content(identifier, data, "APPROVED", session, config)


@router.post("/contents/{identifier}/reject")
def reject_content(identifier: str, data: Review, session: DB, config: Env):
    return review_content(identifier, data, "REJECTED", session, config)


@router.get("/assets")
def list_assets(session: DB, content_id: str | None = None):
    statement = select(Asset)
    if content_id:
        statement = statement.where(Asset.content_id == content_id)
    return listed(
        [asset_dict(item) for item in session.scalars(statement.order_by(Asset.created_at.desc()).limit(500))]
    )


@router.get("/assets/{identifier}/file")
def download_asset(identifier: str, session: DB, config: Env):
    asset = require(session, Asset, identifier)
    return FileResponse(
        asset_path(config, asset),
        media_type=asset.mime_type,
        filename=asset.filename,
        content_disposition_type="inline",
        headers={"X-Content-Type-Options": "nosniff"},
    )


@router.post("/assets", status_code=201)
async def upload_asset(
    session: DB,
    config: Env,
    file: Annotated[UploadFile, File()],
    kind: Annotated[str, Form()],
    content_id: Annotated[str | None, Form()] = None,
):
    if kind not in {"image", "audio", "bgm", "logo", "voice_reference", "portrait"}:
        raise AppError("ASSET_KIND_INVALID", "素材类型不受支持", 422)
    if content_id:
        require(session, Content, content_id)
    suffix = Path(file.filename or "").suffix.lower()
    allowed = {
        "image": {".png", ".jpg", ".jpeg", ".webp"},
        "logo": {".png", ".jpg", ".jpeg", ".webp"},
        "audio": {".mp3", ".wav", ".aac", ".m4a", ".flac", ".ogg"},
        "bgm": {".mp3", ".wav", ".aac", ".m4a", ".flac", ".ogg"},
        "voice_reference": {".mp3", ".wav", ".aac", ".m4a", ".flac", ".ogg"},
        "portrait": {".png", ".jpg", ".jpeg", ".webp"},
    }
    if suffix not in allowed[kind]:
        raise AppError(
            "ASSET_FORMAT_INVALID", "文件格式不支持；图片请用 PNG/JPG/WebP，音频请用 MP3/WAV/AAC 等", 422
        )
    identifier = uid()
    target = config.data_dir / "assets" / f"{identifier}{suffix}"
    maximum = {
        "image": 30 * 1024 * 1024,
        "logo": 30 * 1024 * 1024,
        "portrait": 20 * 1024 * 1024,
        "voice_reference": 30 * 1024 * 1024,
        "audio": 150 * 1024 * 1024,
        "bgm": 150 * 1024 * 1024,
    }[kind]
    size = 0
    hasher = hashlib.sha256()
    try:
        with target.open("xb") as output:
            while block := await file.read(1024 * 1024):
                size += len(block)
                if size > maximum:
                    raise AppError("ASSET_TOO_LARGE", f"文件超过 {maximum // 1024 // 1024} MB 限制", 413)
                output.write(block)
                hasher.update(block)
        if size == 0:
            raise AppError("EMPTY_ASSET", "文件为空", 422)
        if kind in {"image", "logo", "portrait"}:
            try:
                with Image.open(target) as image:
                    image_format = image.format
                    expected_format = {
                        ".png": "PNG",
                        ".jpg": "JPEG",
                        ".jpeg": "JPEG",
                        ".webp": "WEBP",
                    }[suffix]
                    if image_format != expected_format:
                        raise AppError("IMAGE_FORMAT_MISMATCH", "图片扩展名与实际格式不一致", 422)
                    if image.width * image.height > 40_000_000:
                        raise AppError("IMAGE_TOO_LARGE", "图片像素数超过限制", 422)
                    if kind == "portrait" and min(image.width, image.height) < 256:
                        raise AppError("PORTRAIT_TOO_SMALL", "本人肖像的宽和高均需至少 256 像素", 422)
                    image.verify()
                mime = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp"}[image_format]
            except (UnidentifiedImageError, OSError, Image.DecompressionBombError):
                raise AppError("INVALID_IMAGE", "文件不是有效的图片", 422) from None
        else:
            from .integrations.errors import IntegrationError
            from .integrations.media import probe_media

            try:
                info = probe_media(target)
            except IntegrationError:
                raise AppError("INVALID_AUDIO", "文件不是有效的音频", 422) from None
            if not info.get("has_audio") or not info.get("duration") or float(info["duration"]) <= 0:
                raise AppError("INVALID_AUDIO", "文件不是有效的音频", 422)
            if kind == "voice_reference" and not 3 <= float(info["duration"]) <= 30:
                raise AppError("REFERENCE_AUDIO_DURATION_INVALID", "参考音频时长需在 3 到 30 秒之间", 422)
            mime = {
                ".mp3": "audio/mpeg",
                ".wav": "audio/wav",
                ".aac": "audio/aac",
                ".m4a": "audio/mp4",
                ".flac": "audio/flac",
                ".ogg": "audio/ogg",
            }[suffix]
        asset = Asset(
            id=identifier,
            content_id=content_id,
            kind=kind,
            filename=Path(file.filename or f"素材{suffix}").name[:200],
            relative_path=str(target.relative_to(config.data_dir)),
            mime_type=mime,
            size=size,
            sha256=hasher.hexdigest(),
        )
        session.add(asset)
        session.commit()
        return asset_dict(asset)
    except BaseException:
        target.unlink(missing_ok=True)
        raise
    finally:
        await file.close()


@router.get("/local-ai/status")
def get_local_ai_status(config: Env):
    return local_status(config)


def _local_job_replay(session: Session, key: str | None, kind: str, request: dict) -> Job | None:
    if not key:
        return None
    existing = session.scalar(select(Job).where(Job.idempotency_key == key))
    if not existing:
        return None
    if existing.kind != kind or existing.payload.get("request") != request:
        raise AppError("IDEMPOTENCY_CONFLICT", "同一操作标识已用于不同请求", 409)
    return existing


def _asset_identity(asset: Asset) -> dict:
    return {"asset_id": asset.id, "sha256": asset.sha256}


@router.post("/local-ai/transcribe", status_code=202)
def transcribe_local_audio(
    data: LocalAITranscribe,
    session: DB,
    config: Env,
    key: Key = None,
):
    request = data.model_dump()
    if replayed := _local_job_replay(session, key, "LOCAL_AI_TRANSCRIBE", request):
        return accepted(replayed)
    source = checked_asset(session, config, data.asset_id, {"voice_reference"})
    status = local_status(config)
    if not status["transcribe"]["installed"]:
        raise AppError("LOCAL_AI_TRANSCRIBE_NOT_INSTALLED", "本地转写模型尚未完整安装", 409)
    payload = {
        "request": request,
        "source": _asset_identity(source),
        "revision": status["revision"],
        "transcribe": {
            "model": status["transcribe"]["model"],
            "engine": status["transcribe"]["engine"],
        },
    }
    session.rollback()
    acquire_write(session)
    if replayed := _local_job_replay(session, key, "LOCAL_AI_TRANSCRIBE", request):
        session.commit()
        return accepted(replayed)
    current = checked_asset(session, config, data.asset_id, {"voice_reference"})
    if current.sha256 != payload["source"]["sha256"]:
        raise AppError("ASSET_CHANGED", "参考音频已变化，请重新提交", 409)
    job = enqueue(session, kind="LOCAL_AI_TRANSCRIBE", payload=payload, key=key)
    session.commit()
    return accepted(job)


@router.post("/local-ai/preview", status_code=202)
def preview_local_ai(
    data: LocalAIPreview,
    session: DB,
    config: Env,
    key: Key = None,
):
    from .integrations.media import probe_media

    request = data.model_dump()
    if replayed := _local_job_replay(session, key, "LOCAL_AI_PREVIEW", request):
        return accepted(replayed)
    current = settings(session, config)
    profile = current["local_ai"]
    status = local_status(config)
    if data.kind == "voice":
        profile = copy.deepcopy(profile)
        if data.voice_model is not None:
            profile["voice_model"] = data.voice_model
        if data.emotion_strength is not None:
            profile["emotion_strength"] = data.emotion_strength
        voice_model, emotion_strength, voice_component = local_voice_options(profile)
        validate_local_ai_settings(
            session,
            config,
            profile,
            require_voice=True,
            require_avatar=False,
            validate_references=False,
            status=status,
        )
        reference = checked_asset(session, config, profile["reference_audio_asset_id"], {"voice_reference"})
        payload = {
            "request": request,
            "operation": "voice",
            "reference_audio": _asset_identity(reference),
            "voice_model": voice_model,
            "emotion_strength": emotion_strength,
            "revision": status["revision"],
            "component": {
                "model": status[voice_component]["model"],
                "engine": status[voice_component]["engine"],
            },
        }
        if voice_model == "qwen3":
            payload["reference_text"] = profile["reference_text"]
    else:
        validate_local_ai_settings(
            session,
            config,
            profile,
            require_voice=False,
            require_avatar=True,
            validate_references=False,
            status=status,
        )
        portrait = checked_asset(session, config, profile["portrait_asset_id"], {"portrait"})
        audio_id = data.audio_asset_id or profile.get("reference_audio_asset_id")
        if not audio_id:
            raise AppError("LOCAL_AI_PREVIEW_AUDIO_REQUIRED", "数字人预览需要一段本地音频", 409)
        audio = checked_asset(session, config, audio_id, {"audio", "voice_reference"})
        media = probe_media(asset_path(config, audio))
        if not media.get("has_audio") or not 0 < float(media.get("duration") or 0) <= 30:
            raise AppError("LOCAL_AI_PREVIEW_AUDIO_INVALID", "数字人预览音频需有效且不超过 30 秒", 422)
        payload = {
            "request": request,
            "operation": "avatar",
            "portrait": _asset_identity(portrait),
            "audio": _asset_identity(audio),
            "revision": status["revision"],
            "component": {
                "model": status["avatar"]["model"],
                "engine": status["avatar"]["engine"],
            },
        }
    payload["input_hash"] = digest(payload)
    session.rollback()
    acquire_write(session)
    if replayed := _local_job_replay(session, key, "LOCAL_AI_PREVIEW", request):
        session.commit()
        return accepted(replayed)
    for field, kinds in (
        ("reference_audio", {"voice_reference"}),
        ("portrait", {"portrait"}),
        ("audio", {"audio", "voice_reference"}),
    ):
        identity = payload.get(field)
        if identity:
            asset = checked_asset(session, config, identity["asset_id"], kinds)
            if asset.sha256 != identity["sha256"]:
                raise AppError("ASSET_CHANGED", "本地预览素材已变化，请重新提交", 409)
    job = enqueue(session, kind="LOCAL_AI_PREVIEW", payload=payload, key=key)
    session.commit()
    return accepted(job)


@router.get("/contents/{identifier}/export")
def export_content(identifier: str, session: DB, config: Env):
    content = require(session, Content, identifier)
    if not content.video_asset_id or content.media_version != content.version:
        raise AppError("CURRENT_VIDEO_REQUIRED", "请先生成当前版本成片", 409)
    # Spool large ZIPs to disk; don't hold an entire video in API process memory.
    import tempfile

    temporary = tempfile.SpooledTemporaryFile(max_size=4 * 1024 * 1024)  # noqa: SIM115 - response iterator owns closure
    try:
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_STORED) as archive:
            for field, filename in (
                ("video_asset_id", "video.mp4"),
                ("cover_asset_id", "cover.png"),
                ("subtitle_asset_id", "subtitles.srt"),
            ):
                asset_id = getattr(content, field)
                if asset_id:
                    archive.write(asset_path(config, require(session, Asset, asset_id)), filename)
            for platform, text in content.script.get("platforms", {}).items():
                archive.writestr(
                    f"{platform}.txt",
                    f"{text.get('title', '')}\n\n{text.get('description', '')}\n\n"
                    + " ".join("#" + tag.lstrip("#") for tag in text.get("tags", [])),
                )
            archive.writestr(
                "manifest.json",
                json.dumps(
                    {
                        "content_id": identifier,
                        "version": content.version,
                        "title": content.title,
                        "exported_at": now(),
                        "facts": content.script.get("facts", []),
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
            )
        temporary.seek(0)
    except BaseException:
        temporary.close()
        raise

    def chunks():
        try:
            while block := temporary.read(1024 * 1024):
                yield block
        finally:
            temporary.close()

    return StreamingResponse(
        chunks(),
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="content-{identifier}-v{content.version}.zip"'
        },
    )


@router.get("/jobs")
def list_jobs(session: DB, status: str = "", limit: int = Query(100, ge=1, le=500)):
    statement = select(Job)
    if status:
        statement = statement.where(Job.status == status)
    return listed(
        [
            job_dict(session, item)
            for item in session.scalars(statement.order_by(Job.created_at.desc()).limit(limit))
        ]
    )


@router.get("/jobs/{identifier}")
def get_job(identifier: str, session: DB):
    return job_dict(session, require(session, Job, identifier))


@router.post("/jobs/{identifier}/cancel")
def cancel_job(identifier: str, session: DB):
    acquire_write(session)
    job = require(session, Job, identifier)
    if job.status in {"SUCCEEDED", "CANCELED"}:
        return job_dict(session, job)
    job.cancel_requested = True
    if job.status != "RUNNING":
        job.status = "CANCELED"
        if job.content_id and job.kind == "GENERATE_CONTENT":
            content = require(session, Content, job.content_id)
            content.status = current_content_status(content)
    if job.kind == "PREPARE_PUBLICATION":
        publication = require(session, Publication, job.payload["publication_id"])
        if publication.status != "PUBLISHED":
            publication.status = "NEEDS_ATTENTION"
            publication.evidence = {
                **publication.evidence,
                "reason": "准备任务已请求停止，请在手机核验状态后处理或取消发布记录",
            }
    session.commit()
    return job_dict(session, job)


@router.post("/jobs/{identifier}/retry", status_code=202)
def retry_job(identifier: str, session: DB):
    acquire_write(session)
    job = require(session, Job, identifier)
    if session.scalar(
        select(ModelCall.id).where(ModelCall.job_id == identifier, ModelCall.status == "ABANDONED")
    ):
        raise AppError(
            "MODEL_RESULT_ABANDONED",
            "该任务包含已放弃的未知调用，不能直接重发；可在工作台导入产物或修改输入后创建新任务",
            409,
        )
    if job.error_code == "RESTORE_REVIEW_REQUIRED":
        raise AppError("RESTORE_REVIEW_REQUIRED", "请先核验备份恢复前后的实际执行结果，不能直接重试", 409)
    if job.kind == "PREPARE_PUBLICATION":
        raise AppError("USE_PUBLICATION_RESUME", "请在发布中心核验手机状态后选择继续准备", 409)
    if session.scalar(
        select(ModelCall.id).where(
            ModelCall.job_id == identifier, ModelCall.status.in_(["STARTED", "UNKNOWN"])
        )
    ):
        raise AppError(
            "MODEL_OUTCOME_UNKNOWN", "本次模型调用结果未确认，请先核验服务商记录，不能直接重复调用", 409
        )
    if job.status not in {"FAILED", "CANCELED"}:
        raise AppError("JOB_NOT_RETRYABLE", "仅失败或已取消的任务可以重试", 409)
    if job.content_id and job.kind == "GENERATE_CONTENT":
        content = require(session, Content, job.content_id)
        assert_editable(session, content, job.payload["version"])
        content.status = "GENERATING"
    if job.error_code == "MODEL_OUTPUT_INVALID":
        for call in session.scalars(
            select(ModelCall).where(ModelCall.job_id == identifier, ModelCall.kind == "text")
        ):
            if call.status == "SUCCEEDED":
                # Explicit retry of invalid structured output authorizes a fresh request, with its own usage event.
                call.status, call.error_code = "FAILED", "INVALID_OUTPUT_RETRY_REQUESTED"
    job.status, job.cancel_requested, job.error_code, job.error_message = "QUEUED", False, None, None
    job.payload = {key: value for key, value in job.payload.items() if key != "model_requests"}
    job.lease_owner = job.lease_until = None
    job.current_step = "等待重试（复用已完成步骤）"
    session.commit()
    return accepted(job)


@router.post("/jobs/{identifier}/resolve")
def resolve_job(identifier: str, data: ResolveJob, session: DB):
    acquire_write(session)
    job = require(session, Job, identifier)
    if job.status == "RUNNING":
        raise AppError("JOB_RUNNING", "正在执行的任务不能核销，请先等待结果或停止任务", 409)
    calls = list(
        session.scalars(
            select(ModelCall).where(
                ModelCall.job_id == identifier, ModelCall.status.in_(["STARTED", "UNKNOWN"])
            )
        )
    )
    restored = job.error_code == "RESTORE_REVIEW_REQUIRED"
    if not calls and not restored:
        raise AppError("NO_UNKNOWN_CALL", "该任务没有待核验的模型调用", 409)
    if data.resolution == "not_completed":
        for call in calls:
            call.status, call.error_code = "FAILED", "USER_VERIFIED_NOT_COMPLETED"
        job.status = "FAILED"
        job.error_code, job.error_message = (
            "USER_VERIFIED_NOT_COMPLETED",
            "用户已核验服务商未完成请求，可按需手动重试",
        )
    else:
        job.status, job.cancel_requested = "CANCELED", True
        for call in calls:
            call.status, call.error_code = "ABANDONED", "USER_ABANDONED_UNCERTAIN_RESULT"
        if restored:
            job.error_code, job.error_message = "RESTORED_TASK_CANCELED", "用户已核验并选择取消恢复后的任务"
    job.result = {
        **job.result,
        "reconciliation": {"resolution": data.resolution, "notes": data.notes, "at": now()},
    }
    if job.content_id:
        content = require(session, Content, job.content_id)
        content.status = current_content_status(content)
    session.commit()
    return job_dict(session, job)


@router.get("/accounts")
def list_accounts(session: DB):
    return listed([row(item) for item in session.scalars(select(Account).order_by(Account.platform))])


@router.post("/accounts", status_code=201)
def add_account(data: AccountInput, session: DB):
    acquire_write(session)
    if session.scalar(select(Account.id).where(Account.platform == data.platform)):
        raise AppError("PLATFORM_ACCOUNT_EXISTS", "首版每个平台仅绑定一个账号，请编辑现有绑定", 409)
    account = Account(**data.model_dump())
    session.add(account)
    session.commit()
    return row(account)


@router.patch("/accounts/{identifier}")
def edit_account(identifier: str, data: AccountInput, session: DB):
    acquire_write(session)
    account = require(session, Account, identifier)
    if session.scalar(
        select(Publication.id).where(Publication.account_id == identifier, Publication.status != "CANCELED")
    ):
        # Metrics and publication evidence are account-specific and must remain attributable.
        if data.account_identifier != account.account_identifier or data.platform != account.platform:
            raise AppError("ACCOUNT_HAS_PUBLICATIONS", "该账号已有发布记录，不能替换平台或账号身份", 409)
        if session.scalar(
            select(Publication.id).where(
                Publication.account_id == identifier,
                Publication.status.in_(["PREPARING", "WAITING_USER", "VERIFYING", "NEEDS_ATTENTION"]),
            )
        ):
            raise AppError("DEVICE_RESERVED", "该账号还有待处理的设备任务，暂不能更换绑定", 409)
    for key, value in data.model_dump().items():
        setattr(account, key, value)
    session.commit()
    return row(account)


@router.get("/devices")
def devices():
    from .integrations.device import list_devices

    return listed(list_devices())


@router.post("/devices/{serial}/inspect")
def inspect_device_route(serial: str, data: DeviceInspect, session: DB, config: Env):
    if session.scalar(select(Publication.id).where(Publication.reserved_serial == serial)):
        raise AppError("DEVICE_RESERVED", "该设备正等待发布确认，请先在发布中心处理", 409)
    if session.scalar(select(Job.id).where(Job.queue == "device", Job.status == "RUNNING")):
        raise AppError("DEVICE_BUSY", "设备任务正在执行，请稍后检查", 409)
    from .integrations.device import inspect_device
    from .services import device_lock

    with device_lock(config):
        return inspect_device(serial, data.platform)


@router.get("/publications")
def list_publications(session: DB):
    return listed(
        [
            publication_dict(item)
            for item in session.scalars(
                select(Publication).order_by(Publication.created_at.desc()).limit(500)
            )
        ]
    )


def _media_selection_confirmation_context(
    session: Session, config: AppConfig, publication: Publication
) -> dict:
    if publication.platform != "xiaohongshu":
        raise AppError("MEDIA_CONFIRMATION_PLATFORM_INVALID", "仅小红书支持人工素材确认", 409)
    if publication.status not in {"WAITING_USER", "NEEDS_ATTENTION"}:
        raise AppError("PUBLICATION_NOT_READY", "发布记录当前不能确认人工素材", 409)
    if publication.evidence.get("submit_started_at") or publication.evidence.get(
        "final_publish_clicked"
    ):
        raise AppError("PUBLICATION_OUTCOME_UNKNOWN", "已进入发布提交阶段，不能重新确认素材", 409)
    job = require(session, Job, publication.job_id)
    if (
        job.kind != "PREPARE_PUBLICATION"
        or job.payload.get("publication_id") != publication.id
        or job.status in {"QUEUED", "RUNNING"}
    ):
        raise AppError("JOB_RUNNING", "原发布准备任务尚未停稳或关联无效", 409)
    if job.error_code == "RESTORE_REVIEW_REQUIRED":
        raise AppError("RESTORE_REVIEW_REQUIRED", "请先核验备份恢复后的平台实际状态", 409)
    evidence = copy.deepcopy(publication.evidence)
    has_confirmation = isinstance(evidence.get("operator_media_confirmation"), dict)
    if not has_confirmation and (
        evidence.get("reason") != "MEDIA_SELECTION_REQUIRED" or evidence.get("page") != "picker"
    ):
        raise AppError("MEDIA_CONFIRMATION_STATE_INVALID", "原任务不在待人工选择素材状态", 409)
    content = require(session, Content, publication.content_id)
    if (
        content.status != "APPROVED"
        or content.version != publication.content_version
        or not publication_snapshot_matches(content, publication.snapshot)
    ):
        raise AppError("APPROVAL_STALE", "内容版本或审核快照已变化", 409)
    approval = session.scalar(
        select(Approval)
        .where(
            Approval.content_id == content.id,
            Approval.version == content.version,
            Approval.decision == "APPROVED",
        )
        .order_by(Approval.created_at.desc())
    )
    if not approval or not approved_snapshot_matches(content, approval):
        raise AppError("APPROVAL_STALE", "当前内容没有可用的审核记录", 409)
    frozen_account = copy.deepcopy(publication.snapshot.get("account") or {})
    account = require(session, Account, publication.account_id)
    if (
        frozen_account.get("id") != account.id
        or frozen_account.get("platform") != "xiaohongshu"
        or account.platform != "xiaohongshu"
        or frozen_account.get("account_identifier") != account.account_identifier
        or frozen_account.get("device_serial") != account.device_serial
        or publication.reserved_serial != account.device_serial
    ):
        raise AppError("ACCOUNT_CHANGED", "已绑定账号或设备与原审核快照不一致", 409)
    video_asset_id = publication.snapshot.get("video_asset_id")
    if not isinstance(video_asset_id, str):
        raise AppError("MEDIA_EVIDENCE_INVALID", "发布快照缺少已审核视频", 409)
    video = checked_asset(session, config, video_asset_id, {"video"})
    if video.content_id != content.id:
        raise AppError("MEDIA_EVIDENCE_INVALID", "已审核视频与当前内容不一致", 409)
    approved_script = publishable_script(publication.snapshot.get("script") or {})
    platform_text = approved_script.platforms.xiaohongshu.model_dump()
    caption = " ".join(
        value
        for value in (
            platform_text["title"],
            platform_text.get("description", ""),
            " ".join("#" + tag.lstrip("#") for tag in platform_text.get("tags", [])),
        )
        if value
    )
    expected_remote = (
        f"/sdcard/Movies/LocalPublisher/lp_{video.sha256[:16]}/lp_{video.sha256[:16]}.mp4"
    )
    expected_evidence = {
        "serial": account.device_serial,
        "platform": "xiaohongshu",
        "app_version": "9.46.0",
        "verified_account_identifier": account.account_identifier,
        "video_sha256": video.sha256,
        "caption": caption,
        "remote_video_path": expected_remote,
    }
    if any(evidence.get(key) != value for key, value in expected_evidence.items()):
        raise AppError("MEDIA_CONTINUITY_INVALID", "账号或素材连续性证据与已冻结快照不一致", 409)
    state = digest(
        {
            "publication": row(publication),
            "job": row(job),
            "content": content_snapshot(content),
            "approval": row(approval),
            "account": row(account),
            "video": row(video),
        }
    )
    return {
        "state": state,
        "evidence": evidence,
        "serial": account.device_serial,
        "account_identifier": account.account_identifier,
        "app_version": expected_evidence["app_version"],
        "video_sha256": video.sha256,
        "caption": caption,
        "remote_video_path": expected_remote,
    }


@router.post("/publications", status_code=202)
def prepare_publication(data: PublicationCreate, session: DB, config: Env, key: Key = None):
    acquire_write(session)
    if key:
        previous = session.scalar(select(Job).where(Job.idempotency_key == key))
        if previous:
            publication = session.scalar(select(Publication).where(Publication.job_id == previous.id))
            if (
                not publication
                or publication.content_id != data.content_id
                or publication.content_version != data.content_version
                or publication.account_id != data.account_id
            ):
                raise AppError("IDEMPOTENCY_CONFLICT", "操作标识已经用于其他发布准备", 409)
            return {**accepted(previous), "publication_id": publication.id}
    content = require(session, Content, data.content_id)
    account = require(session, Account, data.account_id)
    if content.version != data.content_version:
        raise AppError("APPROVAL_REQUIRED", "请先审核通过当前版本成片，再启动上传准备", 409)
    publication, job = enqueue_publication(session, config, content, account, key=key)
    session.commit()
    return {**accepted(job), "publication_id": publication.id}


@router.post("/publications/{identifier}/confirm-media-selection")
def confirm_media_selection(
    identifier: str,
    data: PublicationMediaSelectionConfirm,
    session: DB,
    config: Env,
):
    acquire_write(session)
    publication = require(session, Publication, identifier)
    context = _media_selection_confirmation_context(session, config, publication)
    existing = context["evidence"].get("operator_media_confirmation")
    if existing is not None:
        if (
            isinstance(existing, dict)
            and existing.get("confirmed") is True
            and existing.get("notes") == data.notes
            and existing.get("video_sha256") == context["video_sha256"]
            and existing.get("app_version") == context["app_version"]
            and isinstance(existing.get("confirmed_at"), str)
        ):
            return {
                "id": publication.id,
                "status": publication.status,
                "operator_media_confirmation": {
                    "confirmed": True,
                    "confirmed_at": existing["confirmed_at"],
                },
            }
        raise AppError(
            "MEDIA_CONFIRMATION_CONFLICT",
            "已有人工素材确认与本次事实不一致",
            409,
        )
    expected_state = context["state"]
    previous_evidence = context["evidence"]
    session.rollback()

    from .integrations.device import verify_operator_media_selection
    from .services import device_lock

    with device_lock(config):
        verified = verify_operator_media_selection(
            serial=context["serial"],
            platform="xiaohongshu",
            account_identifier=context["account_identifier"],
            expected_app_version=context["app_version"],
            video_sha256=context["video_sha256"],
            caption=context["caption"],
            remote_video_path=context["remote_video_path"],
            previous_evidence=previous_evidence,
            app_config=config,
        )
    if verified != {
        "video_sha256": context["video_sha256"],
        "app_version": context["app_version"],
        "page": "editor",
    }:
        raise AppError("MEDIA_CONFIRMATION_INVALID", "设备素材核验结果不完整", 409)

    acquire_write(session)
    publication = require(session, Publication, identifier)
    current = _media_selection_confirmation_context(session, config, publication)
    if current["state"] != expected_state:
        raise AppError("PUBLICATION_STATE_CHANGED", "核验期间发布记录已变化", 409)
    confirmed_at = now()
    confirmation = {
        "confirmed": True,
        "notes": data.notes,
        "video_sha256": current["video_sha256"],
        "app_version": current["app_version"],
        "confirmed_at": confirmed_at,
    }
    publication.evidence = {
        **publication.evidence,
        "operator_media_confirmation": confirmation,
    }
    session.commit()
    return {
        "id": publication.id,
        "status": publication.status,
        "operator_media_confirmation": {"confirmed": True, "confirmed_at": confirmed_at},
    }


@router.post("/publications/{identifier}/confirm")
def confirm_publication(identifier: str, data: PublicationConfirm, session: DB):
    publication = require(session, Publication, identifier)
    platform = publication.platform
    session.rollback()
    work_url, work_id = normalized_work(platform, data.work_url, data.work_id)
    acquire_write(session)
    publication = require(session, Publication, identifier)
    if publication.platform != platform:
        raise AppError("PUBLICATION_STATE_CHANGED", "发布记录的平台发生变化，请重新核验", 409)
    if publication.status == "PUBLISHED":
        if publication.work_id != work_id:
            raise AppError("PUBLICATION_ALREADY_CONFIRMED", "已确认发布记录不能改为另一作品", 409)
        return publication_dict(publication)
    job = require(session, Job, publication.job_id)
    if job.status in {"QUEUED", "RUNNING"} or publication.status not in {
        "WAITING_USER",
        "NEEDS_ATTENTION",
        "VERIFYING",
    }:
        raise AppError("PUBLICATION_NOT_READY", "请先完成或停止准备任务，再核实手机实际发布结果", 409)
    if session.scalar(
        select(Publication.id).where(
            Publication.platform == publication.platform,
            Publication.work_id == work_id,
            Publication.status == "PUBLISHED",
            Publication.id != identifier,
        )
    ):
        raise AppError("WORK_ALREADY_ASSOCIATED", "该作品已关联其他发布记录", 409)
    publication.work_url, publication.work_id, publication.status = work_url, work_id, "PUBLISHED"
    publication.reserved_serial = None
    publication.evidence = {
        **publication.evidence,
        "confirmation": {
            "source": "user",
            "confirmed": True,
            "submitted_work_url": data.work_url,
            "work_url": work_url,
            "work_id": work_id,
            "notes": data.notes,
            "at": now(),
        },
    }
    job.status, job.progress, job.current_step = "SUCCEEDED", 100, "用户已确认实际发布并关联作品"
    job.error_code = job.error_message = None
    session.commit()
    return publication_dict(publication)


@router.post("/publications/{identifier}/detect")
def detect_publication(identifier: str, session: DB, config: Env):
    """Record a published work from the bound phone without requiring a pasted share link."""
    publication = require(session, Publication, identifier)
    if publication.status == "PUBLISHED":
        return publication_dict(publication)
    if publication.status not in {"WAITING_USER", "NEEDS_ATTENTION", "VERIFYING"}:
        raise AppError("PUBLICATION_NOT_READY", "发布记录当前不能从手机识别作品", 409)
    job = require(session, Job, publication.job_id)
    if job.status in {"QUEUED", "RUNNING"}:
        raise AppError("JOB_RUNNING", "发布任务仍在执行，请等待后再识别作品", 409)
    account = copy.deepcopy(publication.snapshot.get("account") or {})
    if (
        account.get("device_serial") is None
        or account.get("account_identifier") is None
        or publication.evidence.get("verified_account_identifier") != account.get("account_identifier")
    ):
        raise AppError(
            "ACCOUNT_VERIFICATION_REQUIRED",
            "原发布准备未完成账号核验；请连接绑定手机并先继续准备",
            409,
        )
    expected_state = digest(
        {
            "status": publication.status,
            "evidence": publication.evidence,
            "content_title": publication.content_title,
            "account": account,
        }
    )
    platform = publication.platform
    frozen_script = publishable_script(publication.snapshot.get("script") or {})
    title = getattr(frozen_script.platforms, platform).title
    session.rollback()

    from .integrations.publishing import detect_published_work
    from .services import device_lock

    with device_lock(config):
        detected = detect_published_work(
            serial=account["device_serial"],
            platform=platform,
            account_identifier=account["account_identifier"],
            title=title,
            canceled=lambda: False,
            app_config=config,
        )
    work_url, work_id = normalized_work(platform, detected["work_url"], detected["work_id"])

    acquire_write(session)
    publication = require(session, Publication, identifier)
    if publication.status == "PUBLISHED":
        if publication.work_id == work_id:
            return publication_dict(publication)
        raise AppError("PUBLICATION_ALREADY_CONFIRMED", "发布记录已关联另一作品", 409)
    actual_state = digest(
        {
            "status": publication.status,
            "evidence": publication.evidence,
            "content_title": publication.content_title,
            "account": publication.snapshot.get("account") or {},
        }
    )
    if actual_state != expected_state:
        raise AppError("PUBLICATION_STATE_CHANGED", "识别期间发布记录已变化，请刷新后核对", 409)
    if session.scalar(
        select(Publication.id).where(
            Publication.platform == platform,
            Publication.work_id == work_id,
            Publication.status == "PUBLISHED",
            Publication.id != identifier,
        )
    ):
        raise AppError("WORK_ALREADY_ASSOCIATED", "该作品已关联其他发布记录", 409)
    job = require(session, Job, publication.job_id)
    publication.work_url, publication.work_id, publication.status = work_url, work_id, "PUBLISHED"
    publication.reserved_serial = None
    publication.evidence = {
        **publication.evidence,
        "detection": {
            **detected.get("evidence", {}),
            "work_url": work_url,
            "work_id": work_id,
            "at": now(),
        },
    }
    job.status, job.progress, job.current_step = "SUCCEEDED", 100, "已从绑定手机识别并关联实际作品"
    job.error_code = job.error_message = None
    session.commit()
    return publication_dict(publication)


@router.post("/publications/{identifier}/cancel")
def cancel_publication(identifier: str, data: PublicationCancel, session: DB):
    acquire_write(session)
    publication = require(session, Publication, identifier)
    if publication.status == "PUBLISHED":
        raise AppError("PUBLISHED_IMMUTABLE", "已发布记录不能通过取消操作删除或解除防重", 409)
    job = require(session, Job, publication.job_id)
    if publication.evidence.get("submit_started_at") and (
        not data.confirmed_not_published or not data.notes or job.status == "RUNNING"
    ):
        raise AppError(
            "PUBLICATION_OUTCOME_UNKNOWN", "已进入发布提交阶段，请先核验平台没有发布，再明确确认取消", 409
        )
    publication.status = "CANCELED"
    publication.evidence = {
        **publication.evidence,
        "cancellation": {
            "notes": data.notes,
            "confirmed_not_published": data.confirmed_not_published,
            "at": now(),
            "warning": "取消仅释放本地流程；请自行处理手机中的草稿或已上传内容",
        },
    }
    job.cancel_requested = True
    if job.status != "RUNNING":
        job.status = "CANCELED"
        publication.reserved_serial = None
    session.commit()
    return publication_dict(publication)


@router.post("/publications/{identifier}/resume", status_code=202)
def resume_publication(identifier: str, session: DB):
    acquire_write(session)
    publication = require(session, Publication, identifier)
    if publication.evidence.get("submit_started_at"):
        raise AppError(
            "PUBLICATION_OUTCOME_UNKNOWN", "已记录发布提交，不能再次上传或点击发布；请核验作品链接", 409
        )
    if publication.status not in {"WAITING_USER", "NEEDS_ATTENTION"}:
        raise AppError("PUBLICATION_NOT_RESUMABLE", "此记录当前不能继续准备", 409)
    job = require(session, Job, publication.job_id)
    if job.error_code == "RESTORE_REVIEW_REQUIRED":
        raise AppError("RESTORE_REVIEW_REQUIRED", "请先在任务中心核验备份恢复后的平台实际状态", 409)
    if job.status in {"QUEUED", "RUNNING"}:
        raise AppError("JOB_RUNNING", "准备任务仍在执行，请等待", 409)
    publication.status = "PREPARING"
    job.status, job.cancel_requested, job.error_code, job.error_message = "QUEUED", False, None, None
    job.current_step = "等待核验手机现场后继续准备"
    session.commit()
    return accepted(job)


@router.post("/publications/{identifier}/collect-metrics", status_code=202)
def collect_metrics_route(identifier: str, session: DB, key: Key = None):
    acquire_write(session)
    publication = require(session, Publication, identifier)
    if publication.status != "PUBLISHED":
        raise AppError("PUBLISHED_WORK_REQUIRED", "请先确认发布并关联实际作品", 409)
    account = require(session, Account, publication.account_id)
    if session.scalar(select(Publication.id).where(Publication.reserved_serial == account.device_serial)):
        raise AppError("DEVICE_RESERVED", "手机正等待发布确认，暂不能采集数据", 409)
    job = enqueue(
        session,
        kind="COLLECT_METRICS",
        queue="device",
        content_id=publication.content_id,
        payload={"publication_id": identifier},
        key=key,
    )
    session.commit()
    return accepted(job)


@router.get("/metrics")
def list_metrics(session: DB, publication_id: str | None = None):
    statement = select(Metric)
    if publication_id:
        statement = statement.where(Metric.publication_id == publication_id)
    return listed(
        [row(item) for item in session.scalars(statement.order_by(Metric.observed_at.desc()).limit(1000))]
    )


@router.get("/strategy/demand-signals")
def list_demand_signals(session: DB, status: str = ""):
    statement = select(DemandSignal)
    if status:
        statement = statement.where(DemandSignal.status == status)
    return listed(
        [
            row(item)
            for item in session.scalars(
                statement.order_by(DemandSignal.updated_at.desc(), DemandSignal.id.desc()).limit(1000)
            )
        ]
    )


@router.post("/strategy/demand-signals", status_code=201)
def create_demand_signal(data: DemandSignalCreate, session: DB):
    item = DemandSignal(**data.model_dump())
    session.add(item)
    session.commit()
    return row(item)


@router.patch("/strategy/demand-signals/{identifier}")
def update_demand_signal(identifier: str, data: DemandSignalPatch, session: DB):
    acquire_write(session)
    item = require(session, DemandSignal, identifier)
    changes = data.model_dump(exclude_unset=True)
    for key, value in changes.items():
        if value is None and key != "source_url":
            raise AppError("INVALID_REQUEST", "非可空字段不能设为 null", 422)
        setattr(item, key, value)
    session.commit()
    return row(item)


@router.get("/strategy/benchmarks")
def list_benchmarks(session: DB, platform: str = ""):
    statement = select(Benchmark)
    if platform:
        statement = statement.where(Benchmark.platform == platform)
    return listed(
        [
            row(item)
            for item in session.scalars(
                statement.order_by(Benchmark.updated_at.desc(), Benchmark.id.desc()).limit(1000)
            )
        ]
    )


@router.post("/strategy/benchmarks", status_code=201)
def create_benchmark(data: BenchmarkCreate, session: DB):
    item = Benchmark(**data.model_dump())
    session.add(item)
    session.commit()
    return row(item)


@router.patch("/strategy/benchmarks/{identifier}")
def update_benchmark(identifier: str, data: BenchmarkPatch, session: DB):
    acquire_write(session)
    item = require(session, Benchmark, identifier)
    changes = data.model_dump(exclude_unset=True)
    for key, value in changes.items():
        if value is None and key != "content_url":
            raise AppError("INVALID_REQUEST", "非可空字段不能设为 null", 422)
        setattr(item, key, value)
    session.commit()
    return row(item)


def _validate_experiment_links(session: Session, content_ids: list[str], publication_ids: list[str]) -> None:
    missing_contents = [identifier for identifier in content_ids if not session.get(Content, identifier)]
    if missing_contents:
        raise AppError(
            "EXPERIMENT_CONTENT_NOT_FOUND",
            "实验关联的内容不存在",
            422,
            {"ids": missing_contents},
        )
    missing_publications = [
        identifier for identifier in publication_ids if not session.get(Publication, identifier)
    ]
    if missing_publications:
        raise AppError(
            "EXPERIMENT_PUBLICATION_NOT_FOUND",
            "实验关联的发布记录不存在",
            422,
            {"ids": missing_publications},
        )


def _experiment_dict(session: Session, experiment: Experiment) -> dict:
    result = row(experiment)
    publication_metrics = []
    metric_fields = ("views", "likes", "comments", "favorites", "shares")
    for publication_id in experiment.publication_ids:
        metrics = list(
            session.scalars(
                select(Metric)
                .where(Metric.publication_id == publication_id)
                .order_by(Metric.observed_at, Metric.created_at, Metric.id)
            )
        )
        latest = row(metrics[-1]) if metrics else None
        if latest:
            latest.pop("raw", None)
        earliest = metrics[0] if len(metrics) > 1 else None
        delta = {
            field: (
                getattr(metrics[-1], field) - getattr(earliest, field)
                if earliest is not None
                and getattr(metrics[-1], field) is not None
                and getattr(earliest, field) is not None
                else None
            )
            for field in metric_fields
        }
        publication_metrics.append(
            {"publication_id": publication_id, "latest_metric": latest, "delta": delta}
        )
    result["publication_metrics"] = publication_metrics
    return result


@router.get("/strategy/experiments")
def list_experiments(session: DB, status: str = ""):
    statement = select(Experiment)
    if status:
        statement = statement.where(Experiment.status == status)
    return listed(
        [
            _experiment_dict(session, item)
            for item in session.scalars(
                statement.order_by(Experiment.updated_at.desc(), Experiment.id.desc()).limit(1000)
            )
        ]
    )


@router.post("/strategy/experiments", status_code=201)
def create_experiment(data: ExperimentCreate, session: DB):
    acquire_write(session)
    values = data.model_dump()
    _validate_experiment_links(session, values["content_ids"], values["publication_ids"])
    item = Experiment(**values)
    session.add(item)
    session.commit()
    return _experiment_dict(session, item)


@router.patch("/strategy/experiments/{identifier}")
def update_experiment(identifier: str, data: ExperimentPatch, session: DB):
    acquire_write(session)
    item = require(session, Experiment, identifier)
    changes = data.model_dump(exclude_unset=True)
    if any(value is None for value in changes.values()):
        raise AppError("INVALID_REQUEST", "实验字段不能设为 null", 422)
    _validate_experiment_links(
        session,
        changes.get("content_ids", item.content_ids),
        changes.get("publication_ids", item.publication_ids),
    )
    for key, value in changes.items():
        setattr(item, key, value)
    session.commit()
    return _experiment_dict(session, item)


@router.post("/metrics", status_code=201)
def manual_metric(data: MetricInput, session: DB):
    publication = require(session, Publication, data.publication_id)
    if publication.status != "PUBLISHED":
        raise AppError("PUBLISHED_WORK_REQUIRED", "仅能为已关联的实际作品补录数据", 409)
    values = data.model_dump(exclude={"notes", "observed_at"})
    if all(values[name] is None for name in ("views", "likes", "comments", "favorites", "shares")):
        raise AppError("METRIC_REQUIRED", "请至少填写一项实际指标，未填写项目将保持为空", 422)
    observed_at = now()
    if data.observed_at:
        try:
            parsed = datetime.fromisoformat(data.observed_at)
            if parsed.tzinfo is None or parsed > datetime.now(UTC) + timedelta(minutes=5):
                raise ValueError()
            observed_at = parsed.astimezone(UTC).isoformat()
        except ValueError:
            raise AppError("INVALID_OBSERVED_TIME", "采集时间必须包含时区，且不能晚于当前时间", 422) from None
    metric = Metric(
        **values,
        platform=publication.platform,
        content_title=publication.content_title,
        source="manual",
        raw={"notes": data.notes, "work_id": publication.work_id},
        observed_at=observed_at,
    )
    session.add(metric)
    session.commit()
    return row(metric)


@router.get("/analytics")
def analytics(session: DB):
    return {
        "publications": [
            publication_dict(item)
            for item in session.scalars(
                select(Publication)
                .where(Publication.status == "PUBLISHED")
                .order_by(Publication.created_at.desc())
            )
        ],
        "metrics": [
            row(item) for item in session.scalars(select(Metric).order_by(Metric.observed_at).limit(5000))
        ],
        "reports": [
            row(item) for item in session.scalars(select(Report).order_by(Report.created_at.desc()).limit(30))
        ],
    }


@router.post("/reports", status_code=202)
def generate_report(session: DB, config: Env, key: Key = None):
    acquire_write(session)
    assert_models(session, config, ("text",))
    if not session.scalar(select(Metric.id).limit(1)):
        raise AppError("METRICS_REQUIRED", "暂无实际运营数据，请先采集或补录指标", 409)
    job = enqueue(session, kind="GENERATE_REPORT", payload={}, key=key)
    session.commit()
    return accepted(job)


@router.get("/templates")
def templates(session: DB):
    return listed(
        [
            {key: value for key, value in row(item).items() if key != "history"}
            for item in session.scalars(select(Template).order_by(Template.created_at))
        ]
    )


@router.get("/templates/{identifier}/versions")
def template_versions(identifier: str, session: DB):
    template = require(session, Template, identifier)
    current = {key: value for key, value in row(template).items() if key != "history"}
    return listed([current, *reversed(template.history)])


@router.post("/templates", status_code=201)
def create_template(data: TemplateInput, session: DB):
    template = Template(**data.model_dump())
    session.add(template)
    session.commit()
    return row(template)


@router.patch("/templates/{identifier}")
def edit_template(identifier: str, session: DB, data: Annotated[dict, Body()]):
    acquire_write(session)
    template = require(session, Template, identifier)
    old = {key: getattr(template, key) for key in TemplateInput.model_fields}
    try:
        validated = TemplateInput.model_validate({**old, **data})
    except ValidationError:
        raise AppError("INVALID_TEMPLATE", "模板字段不正确，请检查名称、风格和提示词", 422) from None
    if any(getattr(template, key) != value for key, value in validated.model_dump().items()):
        template.history = [
            *template.history,
            {**old, "id": template.id, "version": template.version, "updated_at": template.updated_at},
        ]
        for key, value in validated.model_dump().items():
            setattr(template, key, value)
        template.version += 1
    session.commit()
    return row(template)


def public_settings(session: Session, config: AppConfig):
    current = settings(session, config)
    for kind, values in current["models"].items():
        values["key_configured"] = bool(config.key(kind))
    return current


@router.get("/settings")
def get_settings(session: DB, config: Env):
    return public_settings(session, config)


def merge_settings(original: dict, patch: dict) -> dict:
    result = copy.deepcopy(original)
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = merge_settings(result[key], value)
        else:
            result[key] = value
    return result


@router.patch("/settings")
def update_settings(session: DB, config: Env, data: Annotated[dict, Body()]):
    acquire_write(session)
    combined = merge_settings(settings(session, config), data)
    try:
        validated = AllSettings.model_validate(combined).model_dump()
    except ValidationError as error:
        raise AppError(
            "INVALID_SETTINGS",
            "设置格式不正确",
            422,
            {
                "errors": [
                    {"field": ".".join(map(str, e["loc"])), "message": e["msg"]} for e in error.errors()
                ]
            },
        ) from None
    for kind, model in validated["models"].items():
        if model["enabled"] and not config.key(kind):
            raise AppError(
                "API_KEY_REQUIRED", f"请先在本地 .env 配置 PUBLISHER_{kind.upper()}_API_KEY 并重启服务", 409
            )
    validate_local_ai_settings(session, config, validated["local_ai"])
    if validated["publishing"]["auto_publish_after_approval"] and not session.scalar(select(Account.id)):
        raise AppError("PUBLISH_ACCOUNT_REQUIRED", "开启审核后自动发布前，请至少绑定一个平台账号", 409)
    for field, kinds in (("bgm_asset_id", {"bgm", "audio"}), ("logo_asset_id", {"logo", "image"})):
        if validated["video"].get(field):
            checked_asset(session, config, validated["video"][field], kinds)
    if validated["video"]["font_path"]:
        font = Path(validated["video"]["font_path"])
        if (
            not font.is_absolute()
            or not font.is_file()
            or font.suffix.lower() not in {".ttf", ".ttc", ".otf"}
        ):
            raise AppError("INVALID_FONT", "字体路径需指向本机存在的 TTF/TTC/OTF 文件", 422)
    record = require(session, Setting, "app")
    record.value = validated
    session.commit()
    return public_settings(session, config)


@router.get("/schedules")
def schedules(session: DB):
    return listed([row(item) for item in session.scalars(select(Schedule).order_by(Schedule.created_at))])


def validate_schedule_enabled(data: ScheduleInput, session: Session, config: AppConfig):
    if data.enabled and data.kind == "pipeline":
        current = assert_models(session, config, ("text", "image", "speech"))
        if current["local_ai"]["avatar_enabled"]:
            validate_local_ai_settings(
                session,
                config,
                current["local_ai"],
                require_voice=False,
                require_avatar=True,
                validate_references=False,
            )
        if data.daily_limit > current["limits"]["daily_videos"]:
            raise AppError("SCHEDULE_LIMIT_EXCEEDED", "计划每日数量不能超过系统每日成片上限", 422)
        if not session.scalar(select(Source.id).where(Source.enabled.is_(True))):
            raise AppError("SOURCE_REQUIRED", "请先启用至少一个信息源", 409)


@router.post("/schedules", status_code=201)
def create_schedule(data: ScheduleInput, session: DB, config: Env):
    acquire_write(session)
    validate_schedule_enabled(data, session, config)
    schedule = Schedule(**data.model_dump(), enabled_at=now() if data.enabled else None)
    session.add(schedule)
    session.commit()
    return row(schedule)


@router.patch("/schedules/{identifier}")
def edit_schedule(identifier: str, session: DB, config: Env, data: Annotated[dict, Body()]):
    acquire_write(session)
    schedule = require(session, Schedule, identifier)
    try:
        validated = ScheduleInput.model_validate(
            {**{key: getattr(schedule, key) for key in ScheduleInput.model_fields}, **data}
        )
    except ValidationError:
        raise AppError("INVALID_SCHEDULE", "计划字段不正确，请检查时间、时区和每日数量", 422) from None
    validate_schedule_enabled(validated, session, config)
    if validated.enabled and (
        not schedule.enabled or schedule.time != validated.time or schedule.timezone != validated.timezone
    ):
        schedule.enabled_at = now()
    for key, value in validated.model_dump().items():
        setattr(schedule, key, value)
    schedule.next_run_at = None
    session.commit()
    return row(schedule)
