from __future__ import annotations

import copy
import hashlib
import json
import math
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

from pydantic import ValidationError
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from .config import AppConfig
from .db import settings
from .models import (
    Account,
    Approval,
    Asset,
    Content,
    ContentRevision,
    Job,
    JobStep,
    ModelCall,
    Publication,
    QualityReview,
    uid,
)
from .schemas import Script, validate_publishable_script

CONTENT_POLICY_VERSION = "platform-copy-v1"


class AppError(Exception):
    def __init__(self, code: str, message: str, status: int = 400, details: dict | None = None):
        super().__init__(message)
        self.code, self.message, self.status, self.details = code, message, status, details


def require(session: Session, model, identifier: str):
    item = session.get(model, identifier)
    if item is None:
        raise AppError("NOT_FOUND", "记录不存在或已不可用", 404)
    return item


def digest(value) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def row(item) -> dict:
    return {column.name: copy.deepcopy(getattr(item, column.name)) for column in item.__table__.columns}


def asset_dict(item: Asset) -> dict:
    result = row(item)
    result.pop("relative_path", None)
    result["url"] = f"/api/v1/assets/{item.id}/file"
    return result


def content_snapshot(content: Content) -> dict:
    snapshot = {
        key: copy.deepcopy(getattr(content, key))
        for key in (
            "id",
            "title",
            "version",
            "topic_id",
            "template_id",
            "preset_id",
            "brief",
            "script",
            "scenes",
            "media_version",
            "video_asset_id",
            "cover_asset_id",
            "subtitle_asset_id",
        )
    }
    snapshot["content_policy_version"] = CONTENT_POLICY_VERSION
    return snapshot


def legacy_content_snapshot(content: Content) -> dict:
    snapshot = content_snapshot(content)
    snapshot.pop("preset_id", None)
    snapshot.pop("brief", None)
    return snapshot


def approved_snapshot_matches(content: Content, approval: Approval) -> bool:
    if approval.snapshot_hash == digest(content_snapshot(content)):
        return True
    return not approval.compliance and approval.snapshot_hash == digest(legacy_content_snapshot(content))


def publication_snapshot_matches(content: Content, snapshot: dict) -> bool:
    current = content_snapshot(content)
    if all(key in snapshot for key in current):
        return current == {key: snapshot.get(key) for key in current}
    legacy = legacy_content_snapshot(content)
    return not content.brief and content.preset_id == "tech_brief_video_v1" and legacy == {
        key: snapshot.get(key) for key in legacy
    }


def current_quality_review(session: Session, content: Content) -> QualityReview | None:
    if not content.video_asset_id or content.media_version != content.version:
        return None
    return session.scalar(
        select(QualityReview)
        .where(
            QualityReview.content_id == content.id,
            QualityReview.content_version == content.version,
            QualityReview.video_asset_id == content.video_asset_id,
        )
        .order_by(QualityReview.updated_at.desc())
    )


def publishable_script(value: Script | dict) -> Script:
    try:
        return validate_publishable_script(value)
    except (ValidationError, ValueError):
        raise AppError(
            "PLATFORM_TEXT_INVALID",
            "请补全抖音和小红书标题、描述，按平台字数限制修改，并保留AI辅助制作标识",
            409,
        ) from None


def save_revision(session: Session, content: Content) -> None:
    session.flush()
    existing = session.scalar(
        select(ContentRevision).where(
            ContentRevision.content_id == content.id, ContentRevision.version == content.version
        )
    )
    snapshot = content_snapshot(content)
    if existing:
        existing.snapshot = snapshot
    else:
        session.add(ContentRevision(content_id=content.id, version=content.version, snapshot=snapshot))


def job_dict(session: Session, job: Job, detail: bool = True) -> dict:
    result = row(job)
    for name in ("lease_owner", "lease_until", "request_hash", "idempotency_key", "payload"):
        result.pop(name, None)
    steps = (
        list(
            session.scalars(
                select(JobStep).where(JobStep.job_id == job.id).order_by(JobStep.created_at)
            )
        )
        if detail
        else []
    )
    result["steps"] = [row(step) for step in steps]
    timeline = [
        {
            "at": step.started_at or step.created_at,
            "kind": "step",
            "name": step.name,
            "status": step.status,
            "detail": step.detail,
            "_order": step.id,
        }
        for step in steps
    ]
    if detail:
        for call in session.scalars(
            select(ModelCall).where(ModelCall.job_id == job.id).order_by(ModelCall.started_at)
        ):
            usage = {
                key: value
                for key, value in (call.usage or {}).items()
                if key
                in {
                    "prompt_tokens",
                    "completion_tokens",
                    "total_tokens",
                    "input_tokens",
                    "output_tokens",
                    "cached_tokens",
                    "image_count",
                    "audio_seconds",
                    "video_seconds",
                }
                and not isinstance(value, bool)
                and isinstance(value, (int, float))
                and math.isfinite(float(value))
                and value >= 0
            }
            timeline.append(
                {
                    "at": call.started_at,
                    "kind": "model_call",
                    "name": call.kind,
                    "status": call.status,
                    "usage": usage,
                    "_order": call.id,
                }
            )
    result["timeline"] = [
        {key: value for key, value in item.items() if key != "_order"}
        for item in sorted(
            timeline,
            key=lambda item: (item["at"] or "", item["kind"], item["name"], item["_order"]),
        )
    ]
    result["cost"] = {"estimated_usd": None, "actual_usd": None, "status": "UNKNOWN"}
    pending = list(session.scalars(select(ModelCall).where(
        ModelCall.job_id == job.id, ModelCall.status.in_(["STARTED", "UNKNOWN"])
    )))
    from .video_provider import resumable_video_call

    result["can_resume_video"] = bool(pending) and all(resumable_video_call(call) for call in pending)
    result["requires_reconciliation"] = job.error_code == "RESTORE_REVIEW_REQUIRED" or (
        bool(pending) and not result["can_resume_video"]
    )
    result["blocking_job_id"] = job.result.get("details", {}).get("original_job_id")
    return result


def publication_dict(publication: Publication) -> dict:
    result = row(publication)
    result["auto_publish"] = bool(publication.snapshot.get("auto_publish", False))
    result["channel"] = publication.snapshot.get("account", {}).get("channel", "android")
    result.pop("snapshot", None)
    result.pop("reserved_serial", None)
    return result


def enqueue_publication(
    session: Session,
    config: AppConfig,
    content: Content,
    account: Account,
    *,
    automatic: bool = False,
    key: str | None = None,
) -> tuple[Publication, Job]:
    """Caller owns a write transaction; queued automatic jobs reserve the phone at claim time."""
    if content.status != "APPROVED" or content.media_version != content.version:
        raise AppError("APPROVAL_REQUIRED", "请先审核通过当前版本成片，再启动发布", 409)
    publishable_script(content.script)
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
        raise AppError("APPROVAL_STALE", "成片或文案已变化，请重新审核", 409)
    duplicate = session.scalar(
        select(Publication.id).where(
            Publication.content_id == content.id,
            Publication.account_id == account.id,
            Publication.platform == account.platform,
            Publication.status != "CANCELED",
        )
    )
    if duplicate:
        raise AppError(
            "DUPLICATE_PUBLICATION",
            "此内容在该账号已有发布或待处理记录，请处理原记录",
            409,
            {"publication_id": duplicate},
        )
    if not automatic:
        if session.scalar(select(Publication.id).where(Publication.reserved_serial == account.device_serial)):
            raise AppError("DEVICE_RESERVED", "此设备正被另一条发布任务占用", 409)
        if session.scalar(select(Job.id).where(Job.queue == "device", Job.status == "RUNNING")):
            raise AppError("DEVICE_BUSY", "设备任务正在执行，请稍后再准备上传", 409)
    checked_asset(session, config, content.video_asset_id, {"video"})
    snapshot = content_snapshot(content)
    snapshot.update(account=row(account), auto_publish=automatic)
    publication = Publication(
        id=uid(),
        content_id=content.id,
        content_version=content.version,
        content_title=content.title,
        platform=account.platform,
        account_id=account.id,
        status="PREPARING",
        snapshot=snapshot,
        reserved_serial=None if automatic else account.device_serial,
    )
    session.add(publication)
    session.flush()
    job = enqueue(
        session,
        kind="PREPARE_PUBLICATION",
        queue="device",
        content_id=content.id,
        payload={"publication_id": publication.id},
        key=key,
    )
    publication.job_id = job.id
    return publication, job


def enqueue(
    session: Session,
    *,
    kind: str,
    payload: dict,
    key: str | None = None,
    queue: str = "content",
    content_id: str | None = None,
) -> Job:
    key = key or uid()
    request_hash = digest({"kind": kind, "payload": payload, "queue": queue, "content_id": content_id})
    existing = session.scalar(select(Job).where(Job.idempotency_key == key))
    if existing:
        if existing.request_hash != request_hash:
            raise AppError("IDEMPOTENCY_CONFLICT", "同一操作标识已用于不同请求，请刷新后重试", 409)
        return existing
    job = Job(
        kind=kind,
        payload=payload,
        queue=queue,
        content_id=content_id,
        idempotency_key=key,
        request_hash=request_hash,
    )
    session.add(job)
    session.flush()
    return job


def accepted(job: Job) -> dict:
    return {"job_id": job.id, "status": job.status}


def acquire_write(session: Session) -> None:
    """Call before the first query in operations that must atomically check-and-write."""
    session.execute(text("BEGIN IMMEDIATE"))


def active_publication(session: Session, content_id: str):
    return session.scalar(
        select(Publication).where(
            Publication.content_id == content_id,
            Publication.status.in_(["PREPARING", "WAITING_USER", "VERIFYING", "NEEDS_ATTENTION"]),
        )
    )


def assert_editable(session: Session, content: Content, version: int) -> None:
    if content.version != version:
        raise AppError(
            "VERSION_CONFLICT", "内容已更新，请重新加载后编辑", 409, {"current_version": content.version}
        )
    if active_publication(session, content.id):
        raise AppError("PUBLICATION_IN_PROGRESS", "该内容正在发布准备中，请先处理或取消发布任务", 409)
    if session.scalar(
        select(Job.id).where(
            Job.content_id == content.id,
            Job.kind == "GENERATE_CONTENT",
            Job.status.in_(["QUEUED", "RUNNING"]),
        )
    ):
        raise AppError("GENERATION_IN_PROGRESS", "生成任务尚未结束，请等待完成或先取消任务", 409)


def asset_path(config: AppConfig, asset: Asset) -> Path:
    root = config.data_dir.resolve()
    path = (root / asset.relative_path).resolve()
    if not path.is_relative_to(root / "assets"):
        raise AppError("INVALID_ASSET_PATH", "素材路径不合法", 400)
    if not path.is_file():
        raise AppError("ASSET_MISSING", "素材文件不存在，请重新上传或生成", 409)
    return path


def checked_asset(session: Session, config: AppConfig, asset_id: str, kinds: set[str]) -> Asset:
    asset = require(session, Asset, asset_id)
    if asset.kind not in kinds:
        raise AppError("ASSET_KIND_MISMATCH", "素材类型不符合此位置的要求", 422)
    asset_path(config, asset)
    return asset


def checked_scene_audio(session: Session, config: AppConfig, content_id: str, scene: dict) -> list[Asset]:
    assets = [checked_asset(session, config, item, {"audio", "video_clip"}) for item in scene["audio_asset_ids"]]
    for asset in assets:
        if asset.kind != "video_clip":
            continue
        if asset.content_id != content_id or asset.id != scene.get("video_asset_id") or len(assets) != 1:
            raise AppError("INVALID_NATIVE_AUDIO", "视频原声只能单独使用当前内容所选视频的音轨", 422)
        from .integrations.media import probe_media

        metadata = probe_media(asset_path(config, asset))
        if not metadata.get("has_audio") or metadata.get("duration", 0) <= 0:
            raise AppError("MISSING_NATIVE_AUDIO", "所选视频没有可用原声音轨，请选择其他配音", 422)
    return assets


def local_voice_options(profile: dict) -> tuple[str, float, str]:
    voice_model = profile.get("voice_model", "qwen3")
    emotion_strength = profile.get("emotion_strength", 0.65)
    if voice_model not in {"qwen3", "chatterbox"}:
        raise AppError("LOCAL_AI_PROFILE_INVALID", "本地声音模型配置无效", 409)
    if (
        isinstance(emotion_strength, bool)
        or not isinstance(emotion_strength, (int, float))
        or not math.isfinite(float(emotion_strength))
        or not 0 <= float(emotion_strength) <= 1
    ):
        raise AppError("LOCAL_AI_PROFILE_INVALID", "本地声音情感强度配置无效", 409)
    component = "chatterbox" if voice_model == "chatterbox" else "tts"
    return voice_model, float(emotion_strength), component


def validate_local_ai_settings(
    session: Session,
    config: AppConfig,
    profile: dict,
    *,
    require_voice: bool | None = None,
    require_avatar: bool | None = None,
    validate_references: bool = True,
    status: dict | None = None,
) -> dict | None:
    """Validate profile references and enabled runtime capabilities without loading a model."""

    reference_audio = None
    portrait = None
    voice = bool(profile.get("voice_enabled")) if require_voice is None else require_voice
    avatar = bool(profile.get("avatar_enabled")) if require_avatar is None else require_avatar
    voice_model, _emotion_strength, voice_component = local_voice_options(profile)
    if profile.get("reference_audio_asset_id") and (validate_references or voice):
        reference_audio = checked_asset(
            session, config, profile["reference_audio_asset_id"], {"voice_reference"}
        )
    if profile.get("portrait_asset_id") and (validate_references or avatar):
        portrait = checked_asset(session, config, profile["portrait_asset_id"], {"portrait"})
    if not voice and not avatar:
        return status
    if voice and not reference_audio:
        raise AppError("LOCAL_AI_REFERENCE_AUDIO_REQUIRED", "启用本地配音前请上传参考音频", 409)
    if voice and voice_model == "qwen3" and not str(profile.get("reference_text") or "").strip():
        raise AppError("LOCAL_AI_REFERENCE_TEXT_REQUIRED", "启用本地配音前请填写参考音频文字", 409)
    if avatar and not portrait:
        raise AppError("LOCAL_AI_PORTRAIT_REQUIRED", "启用数字人前请上传本人肖像", 409)
    if status is None:
        from .integrations.local_ai import local_status

        status = local_status(config)
    if voice and not status[voice_component]["installed"]:
        code = (
            "LOCAL_AI_CHATTERBOX_NOT_INSTALLED"
            if voice_component == "chatterbox"
            else "LOCAL_AI_TTS_NOT_INSTALLED"
        )
        raise AppError(code, "本地配音模型尚未完整安装", 409)
    if avatar and not status["avatar"]["installed"]:
        raise AppError("LOCAL_AI_AVATAR_NOT_INSTALLED", "数字人口型模型尚未完整安装", 409)
    return status


def local_ai_profile_snapshot(
    session: Session,
    config: AppConfig,
    *,
    include_voice: bool,
    include_avatar: bool,
    force_voice: bool = False,
    force_avatar: bool = False,
    include_portrait: bool = False,
) -> dict:
    """Freeze the effective local profile and artifact identities for a queued job."""

    profile = settings(session, config)["local_ai"]
    voice = include_voice and (bool(profile["voice_enabled"]) or force_voice)
    avatar = include_avatar and (bool(profile["avatar_enabled"]) or force_avatar)
    voice_model, emotion_strength, voice_component = local_voice_options(profile)
    status = validate_local_ai_settings(
        session,
        config,
        profile,
        require_voice=voice,
        require_avatar=avatar,
        validate_references=False,
    )
    reference_audio = (
        checked_asset(session, config, profile["reference_audio_asset_id"], {"voice_reference"})
        if voice
        else None
    )
    portrait_required = avatar or include_portrait
    if portrait_required and not profile.get("portrait_asset_id"):
        raise AppError("LOCAL_AI_PORTRAIT_REQUIRED", "生成数字人前请上传本人肖像", 409)
    portrait = (
        checked_asset(session, config, profile["portrait_asset_id"], {"portrait"})
        if portrait_required
        else None
    )
    snapshot = {
        "voice_enabled": voice,
        "avatar_enabled": avatar,
        "voice_model": voice_model,
        "emotion_strength": emotion_strength,
        "reference_audio": (
            {"asset_id": reference_audio.id, "sha256": reference_audio.sha256} if reference_audio else None
        ),
        "portrait": {"asset_id": portrait.id, "sha256": portrait.sha256} if portrait else None,
        "revision": status["revision"] if status and (voice or avatar) else None,
        "tts": (
            {
                "model": status[voice_component]["model"],
                "engine": status[voice_component]["engine"],
            }
            if voice and status
            else None
        ),
        "avatar": (
            {"model": status["avatar"]["model"], "engine": status["avatar"]["engine"]}
            if avatar and status
            else None
        ),
    }
    if voice_model == "qwen3":
        snapshot["reference_text"] = profile["reference_text"] if voice else ""
    return snapshot


def active_model(current: dict, kind: str) -> tuple[str, dict | None]:
    """Resolve the selected source and its profile for one capability without side effects.

    Returns ("third_party"|"local", profile dict) for HTTP profiles and ("builtin", None)
    for the native speech engine, which dispatches through local_ai instead of invoke_model.
    """

    model = current.get("models", {}).get(kind, {})
    source = model.get("source") or "third_party"
    if source not in {"third_party", "local", "builtin"} or (source == "builtin" and kind != "speech"):
        source = "third_party"
    if source == "builtin":
        return "builtin", None
    if source == "local":
        return "local", model.get("local") or {}
    return "third_party", model


def assert_models(session: Session, config: AppConfig, kinds: tuple[str, ...]) -> dict:
    from .model_credentials import resolve_model_key

    current = settings(session, config)
    missing = []
    for kind in kinds:
        source, profile = active_model(current, kind)
        if source == "builtin":
            ready = bool(
                validate_local_ai_settings(
                    session,
                    config,
                    current["local_ai"],
                    require_voice=True,
                    require_avatar=False,
                    validate_references=False,
                )
            )
        elif not (profile.get("enabled") and profile.get("base_url") and profile.get("model")):
            ready = False
        elif source == "local":
            ready = True
        else:
            ready = bool(resolve_model_key(config, kind, source, profile["base_url"])[0])
        if not ready:
            missing.append(kind)
    if missing:
        names = {"text": "文本", "image": "图片", "speech": "配音", "video": "视频"}
        raise AppError(
            "MODEL_NOT_CONFIGURED",
            "请先配置并启用模型能力：" + "、".join(names[k] for k in missing),
            409,
            {"capabilities": missing},
        )
    return current


def day_bounds(zone: str, instant: datetime | None = None) -> tuple[str, str]:
    local = (instant or datetime.now(UTC)).astimezone(ZoneInfo(zone))
    start = local.replace(hour=0, minute=0, second=0, microsecond=0)
    return start.astimezone(UTC).isoformat(), (start + timedelta(days=1)).astimezone(UTC).isoformat()


def usage_summary(session: Session, zone: str) -> dict:
    start, end = day_bounds(zone)
    calls = session.scalars(select(ModelCall).where(ModelCall.updated_at >= start))
    events = [
        event
        for call in calls
        for event in call.events
        if start <= event["started_at"] < end and event.get("status") != "CANCELED_BEFORE_SEND"
    ]
    return {
        "calls_today": len(events),
        "tokens_today": sum(int(event.get("usage", {}).get("total_tokens", 0) or 0) for event in events),
        "cost": None,
    }


@contextmanager
def device_lock(config: AppConfig):
    """Serialize UI inspection and device worker actions across processes on this host."""
    import fcntl

    with (config.data_dir / "runs" / "device-ui.lock").open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise AppError("DEVICE_BUSY", "设备正在执行另一项操作，请稍后再试", 409) from None
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def normalized_work(
    platform: str, url: str, explicit_id: str | None, *, expand_short: bool = True
) -> tuple[str, str]:
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.username or parsed.password or parsed.fragment:
        raise AppError("INVALID_WORK_URL", "请输入平台 HTTPS 作品链接", 422)
    host = (parsed.hostname or "").lower()
    domains = {
        "douyin": {"www.douyin.com", "douyin.com", "v.douyin.com", "www.iesdouyin.com"},
        "xiaohongshu": {
            "www.xiaohongshu.com",
            "xiaohongshu.com",
            "xhslink.com",
            "www.xhslink.com",
            "xhslink.cn",
            "www.xhslink.cn",
        },
    }
    if host not in domains[platform] or parsed.port not in {None, 443}:
        raise AppError("INVALID_WORK_URL", "作品链接与目标平台不一致", 422)
    import re

    if platform == "douyin":
        match = re.search(r"/(?:video|note|share/video)/(\d+)", parsed.path)
        inferred = match.group(1) if match else parse_qs(parsed.query).get("modal_id", [None])[0]
        valid = r"\d{5,30}"
    else:
        match = re.search(r"/(?:explore|discovery/item)/([a-fA-F0-9]{16,32})", parsed.path)
        inferred = match.group(1) if match else None
        valid = r"[a-fA-F0-9]{16,32}"
    if (
        not inferred
        and not explicit_id
        and expand_short
        and host
        in {
            "v.douyin.com",
            "xhslink.com",
            "www.xhslink.com",
            "xhslink.cn",
            "www.xhslink.cn",
        }
    ):
        from .integrations.errors import IntegrationError
        from .integrations.net import safe_get

        def identifies_work(candidate: str) -> bool:
            try:
                normalized_work(platform, candidate, None, expand_short=False)
                return True
            except (AppError, ValueError):
                return False

        try:
            _, _, expanded = safe_get(url, max_bytes=1_000_000, timeout=20, stop_at_url=identifies_work)
        except IntegrationError:
            raise AppError(
                "WORK_LINK_UNRESOLVED", "分享短链接暂时无法展开，请稍后重试或填写完整作品链接和作品 ID", 422
            ) from None
        resolved, work_id = normalized_work(platform, expanded, None, expand_short=False)
        target = urlsplit(resolved)
        trackless_work_path = (
            platform == "douyin"
            and re.search(r"/(?:video|note|share/video)/\d+", target.path)
            or platform == "xiaohongshu"
            and re.search(r"/(?:explore|discovery/item)/[a-fA-F0-9]{16,32}", target.path)
        )
        if trackless_work_path:
            resolved = urlunsplit((target.scheme, target.netloc, target.path, "", ""))
        return resolved, work_id
    if inferred and explicit_id and inferred != explicit_id:
        raise AppError("WORK_ID_MISMATCH", "作品 ID 与链接不一致", 422)
    work_id = explicit_id or inferred
    if not work_id or not re.fullmatch(valid, work_id):
        raise AppError(
            "WORK_ID_REQUIRED", "无法从短链接确认作品 ID，请使用完整作品链接或补充真实作品 ID", 422
        )
    if platform == "xiaohongshu" and inferred:
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", "")), work_id
    return url, work_id
