"""MiniMax H3 V2: create once, durably remember the task, resume using GET only."""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import re
import subprocess
import tempfile
import time
from pathlib import Path

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from .integrations.media import _binary, _local_input, probe_media
from .integrations.net import safe_get
from .model_credentials import resolve_model_key
from .models import Asset, Job, ModelCall, now, uid
from .providers import _active_attempt, _finish_event, _lease_guard, _record_failure
from .services import (
    AppError,
    acquire_write,
    active_model,
    assert_models,
    asset_path,
    checked_asset,
    digest,
    require,
    usage_summary,
)
from .storage import register_file

POLL_INTERVAL = 10
TASK_ID = re.compile(r"[A-Za-z0-9_-]{1,200}")
H3_AUDIO_MIME = {"audio/mpeg", "audio/wav", "audio/x-wav"}
CONVERTIBLE_AUDIO_MIME = {"audio/mp4", "audio/aac", "audio/flac", "audio/ogg"}


def _audio_reference(data: bytes, suffix: str, mime: str) -> tuple[bytes, str]:
    """Validate and, when needed, convert the verified bytes rather than a mutable asset path."""
    with tempfile.TemporaryDirectory(prefix="h3-reference-") as temporary:
        directory = Path(temporary)
        source = directory / f"source{suffix}"
        source.write_bytes(data)
        info = probe_media(source)
        if not info.get("has_audio") or not 2 <= info.get("duration", 0) <= 15:
            raise AppError("VIDEO_REFERENCE_INVALID", "声音参考时长必须为 2–15 秒", 422)
        if mime in H3_AUDIO_MIME:
            return data, "audio/wav" if mime == "audio/x-wav" else mime
        output = directory / "reference.wav"
        try:
            result = subprocess.run(
                [
                    _binary("ffmpeg"), "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
                    *_local_input(source), "-map", "0:a:0", "-vn", "-ac", "1", "-ar", "24000",
                    "-c:a", "pcm_s16le", str(output),
                ],
                capture_output=True, timeout=30, check=False,
            )
        except (OSError, subprocess.SubprocessError):
            raise AppError("VIDEO_REFERENCE_INVALID", "声音参考转为 WAV 失败，请更换录音文件", 422) from None
        if result.returncode != 0 or not output.is_file() or output.stat().st_size > 10_000_000:
            raise AppError("VIDEO_REFERENCE_INVALID", "声音参考转为 WAV 失败或超过 10 MB", 422)
        return output.read_bytes(), "audio/wav"


def resumable_video_call(call: ModelCall) -> bool:
    return (
        call.kind == "video"
        and isinstance(call.response, dict)
        and bool(TASK_ID.fullmatch(str(call.response.get("task_id", ""))))
    )


def _request(provider: dict, payload: dict) -> dict:
    duration = payload.get("duration")
    prompt = payload.get("prompt")
    if (
        provider.get("protocol") != "minimax_h3"
        or provider.get("model") != "MiniMax-H3"
        or not isinstance(prompt, str)
        or not 1 <= len(prompt.strip()) <= 7000
        or isinstance(duration, bool)
        or not isinstance(duration, int)
        or not 4 <= duration <= 15
        or payload.get("resolution") not in {"768P", "2K"}
        or payload.get("ratio") not in {"9:16", "16:9", "1:1", "4:3", "3:4", "21:9"}
    ):
        raise AppError(
            "VIDEO_INPUT_INVALID", "MiniMax H3 参数无效，请检查模型名、提示词、时长、分辨率和比例", 422
        )
    return {
        "model": "MiniMax-H3",
        "content": [{"type": "text", "text": prompt.strip()}],
        "duration": duration,
        "resolution": payload["resolution"],
        "ratio": payload["ratio"],
    }


def _json_response(response: httpx.Response) -> dict:
    chunks, size = [], 0
    for chunk in response.iter_bytes():
        size += len(chunk)
        if size > 1024 * 1024:
            raise ValueError("video response too large")
        chunks.append(chunk)
    value = json.loads(b"".join(chunks))
    if not isinstance(value, dict):
        raise TypeError("invalid video response")
    return value


def _reference_content(session, config, references, content_id):
    """Send only explicitly selected local assets; never persist their data URIs."""
    if references.get("audio") and not references.get("image"):
        raise AppError("VIDEO_REFERENCE_INVALID", "声音参考需要图片参考", 422)
    items = []
    for kind in ("image", "audio"):
        reference = references.get(kind)
        if not reference:
            continue
        asset = checked_asset(session, config, reference["asset_id"],
                              {"portrait", "image"} if kind == "image" else {"voice_reference", "audio"})
        if asset.content_id not in {None, content_id} or asset.sha256 != reference["sha256"]:
            raise AppError("VIDEO_REFERENCE_CHANGED", "参考素材身份发生变化，未提交生成", 409)
        allowed = {"image/jpeg", "image/png", "image/webp"} if kind == "image" else H3_AUDIO_MIME | CONVERTIBLE_AUDIO_MIME
        path = asset_path(config, asset)
        if asset.mime_type not in allowed or path.stat().st_size > 10_000_000:
            raise AppError("VIDEO_REFERENCE_INVALID", "图片参考需 JPEG/PNG/WebP，声音参考需 MP3/WAV，单个文件不超过 10 MB", 422)
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != reference["sha256"]:
            raise AppError("VIDEO_REFERENCE_CHANGED", "参考素材文件发生变化，未提交生成", 409)
        mime = asset.mime_type
        if kind == "audio":
            data, mime = _audio_reference(data, path.suffix, mime)
        item = {"type": kind + "_url", kind + "_url": {
            "url": f"data:{mime};base64," + base64.b64encode(data).decode("ascii")}}
        item["role"] = "reference_" + kind
        items.append(item)
    return items


def invoke_video(
    engine, config, *, job_id: str, owner: str, payload: dict, scope: str, content_id=None
) -> dict:
    with Session(engine, expire_on_commit=False) as session:
        acquire_write(session)
        job = _lease_guard(session, job_id, owner)
        current = assert_models(session, config, ("video",))
        source, provider = active_model(current, "video")
        request = _request(provider, payload)
        request["content"].extend(_reference_content(session, config, payload.get("references", {}), content_id))
        base_url = provider["base_url"].rstrip("/")
        request_key = digest({"scope": scope, "url": base_url, "kind": "video", "request": request})
        slot = digest({"scope": scope, "kind": "video"})
        previous = job.payload.get("model_requests", {}).get(slot)
        if previous and previous != request_key:
            raise AppError(
                "JOB_INPUT_CHANGED", "已提交视频任务的接口或输入发生变化，请恢复原配置后继续查询", 409
            )
        job.payload = {
            **job.payload,
            "model_requests": {**job.payload.get("model_requests", {}), slot: request_key},
        }
        record = session.scalar(select(ModelCall).where(ModelCall.request_key == request_key))
        if record and record.status == "ABANDONED":
            raise AppError("MODEL_RESULT_ABANDONED", "该请求已放弃，不会再次提交付费生成", 409)
        if record and record.status == "SUCCEEDED":
            asset = session.get(Asset, record.response.get("asset_id"))
            if asset:
                try:
                    path = asset_path(config, asset)
                    valid = hashlib.sha256(path.read_bytes()).hexdigest() == asset.sha256
                except (AppError, OSError):
                    valid = False
                if valid:
                    session.commit()
                    return copy.deepcopy(record.response)
            # Re-download from the same paid task, never create a replacement.
        task_id = record.response.get("task_id") if record and resumable_video_call(record) else None
        if task_id and record.error_code == "VIDEO_TASK_FAILED":
            raise AppError(
                "VIDEO_TASK_FAILED", "服务商已终止此视频任务；请修改输入后新建任务，不会自动再次计费", 409
            )
        if record and record.job_id != job_id:
            other_job = session.get(Job, record.job_id)
            if other_job and other_job.status in {"QUEUED", "RUNNING"}:
                raise AppError("VIDEO_TASK_BUSY", "同一视频请求仍由其他任务执行，请等待原任务结束", 409)
        if record and not task_id and record.status in {"STARTED", "UNKNOWN"}:
            raise AppError(
                "MODEL_OUTCOME_UNKNOWN", "视频创建请求结果未明，请先核验服务商记录，不会自动重发", 409
            )
        credential, _ = resolve_model_key(config, "video", source, base_url)
        attempt_id = uid()
        if task_id:
            # Renew the ownership fence without adding a paid-call event.
            events = copy.deepcopy(record.events)
            events[-1].update(attempt_id=attempt_id, status="STARTED")
            record.events = events
            record.job_id, record.status, record.error_code = job_id, "STARTED", None
        else:
            if usage_summary(session, current["timezone"])["calls_today"] >= current["limits"]["daily_calls"]:
                raise AppError("DAILY_CALL_LIMIT", "已达到今日模型调用上限", 409)
            event = {"started_at": now(), "status": "STARTED", "job_id": job_id, "attempt_id": attempt_id}
            if record:
                record.events = [*record.events, event]
                record.attempts += 1
                record.job_id, record.status, record.error_code = job_id, "STARTED", None
            else:
                record = ModelCall(
                    id=uid(),
                    job_id=job_id,
                    request_key=request_key,
                    kind="video",
                    model=provider["model"],
                    status="STARTED",
                    events=[event],
                )
                session.add(record)
        record_id = record.id
        session.commit()

    def guard(*, dispatched=False):
        with Session(engine) as session:
            acquire_write(session)
            active = require(session, ModelCall, record_id)
            _active_attempt(session, active, job_id, owner, attempt_id)
            _lease_guard(session, job_id, owner)
            if dispatched:
                events = copy.deepcopy(active.events)
                events[-1]["dispatch_at"] = now()
                active.events = events
            session.commit()

    def fail(code, message, uncertain=False):
        _record_failure(
            engine, record_id, code, message, uncertain, job_id=job_id, owner=owner, attempt_id=attempt_id
        )
        return AppError(code, message, 409)

    headers = {"Authorization": f"Bearer {credential}", "Accept": "application/json"}
    produced_file = config.data_dir / "runs" / f"{record_id}-{attempt_id}.mp4"
    try:
        with httpx.Client(timeout=min(provider["timeout_seconds"], 120), follow_redirects=False) as client:
            if not task_id:
                guard(dispatched=True)
                with client.stream(
                    "POST", base_url + "/v2/video_generation", headers=headers, json=request
                ) as response:
                    if response.status_code >= 300:
                        uncertain = response.status_code == 408 or response.status_code >= 500
                        raise fail(
                            "MODEL_OUTCOME_UNKNOWN" if uncertain else f"VIDEO_HTTP_{response.status_code}",
                            "视频创建结果未明，请核验服务商记录"
                            if uncertain
                            else "视频服务拒绝请求，请检查密钥、权限、余额和模型参数",
                            uncertain,
                        )
                    envelope = _json_response(response)
                task_id = envelope.get("task_id")
                if not isinstance(task_id, str) or not TASK_ID.fullmatch(task_id):
                    task_id = None
                    if envelope.get("base_resp", {}).get("status_code", 0) != 0:
                        raise fail("VIDEO_CREATE_REJECTED", "视频服务拒绝创建任务，请检查账户与参数")
                    raise ValueError("missing task id")
                # Preserve the definitive id even if cancellation/lease loss races the POST response.
                # This is recovery metadata only: it never registers an asset or publishes content.
                with Session(engine) as session:
                    acquire_write(session)
                    active = require(session, ModelCall, record_id)
                    if (
                        active.events
                        and active.events[-1].get("attempt_id") == attempt_id
                        and active.status in {"STARTED", "UNKNOWN"}
                    ):
                        active.response = {"task_id": task_id}
                        session.commit()
                    else:
                        raise AppError("TASK_LEASE_LOST", "视频任务已被其他操作接管", 409)
            deadline = time.monotonic() + provider["timeout_seconds"]
            while True:
                guard()
                if time.monotonic() >= deadline:
                    raise fail(
                        "VIDEO_POLL_TIMEOUT", "视频仍在生成，任务 ID 已保存；可重试以继续查询，不会再次生成"
                    )
                with client.stream(
                    "GET", base_url + f"/v2/query/video_generation/{task_id}", headers=headers
                ) as response:
                    if response.status_code >= 300:
                        raise fail("VIDEO_QUERY_FAILED", "暂时无法查询视频，已保留任务 ID；重试只查询原任务")
                    envelope = _json_response(response)
                if envelope.get("base_resp", {}).get("status_code", 0) != 0:
                    raise fail("VIDEO_QUERY_FAILED", "视频查询被拒绝，已保留任务 ID；请检查账户配置")
                task = envelope.get("task") or {}
                status = str(task.get("status", "")).lower()
                if status in {"failed", "fail", "cancelled", "canceled", "expired", "error"}:
                    raise fail("VIDEO_TASK_FAILED", "服务商已终止此视频任务；不会自动再次创建付费任务")
                if status in {"success", "succeeded", "completed", "done"}:
                    url = (task.get("content") or {}).get("url")
                    if not isinstance(url, str) or not url:
                        raise ValueError("missing video URL")
                    break
                if status not in {"queued", "pending", "processing", "running", "preparing", "generating"}:
                    raise ValueError("unknown video task status")
                # Check the lease and cancellation during waits, without making extra provider calls.
                for _ in range(POLL_INTERVAL):
                    guard()
                    time.sleep(1)
            guard()
            binary, _, _ = safe_get(url, max_bytes=50_000_000, timeout=120)
            produced_file.parent.mkdir(parents=True, exist_ok=True)
            produced_file.write_bytes(binary)
            metadata = probe_media(produced_file)
            if not metadata.get("has_video") or metadata.get("duration", 0) <= 0:
                raise ValueError("invalid video output")
            with Session(engine) as session:
                acquire_write(session)
                active = require(session, ModelCall, record_id)
                _active_attempt(session, active, job_id, owner, attempt_id)
                _lease_guard(session, job_id, owner)
                asset = register_file(
                    session,
                    config,
                    produced_file,
                    kind="video_clip",
                    content_id=content_id,
                    mime_type="video/mp4",
                    filename=f"h3-{record_id[:8]}.mp4",
                    input_hash=request_key,
                )
                result = {"asset_id": asset.id, "task_id": task_id}
                active.response, active.error_code = result, None
                active.usage = {"video_seconds": request["duration"]}
                _finish_event(active, "SUCCEEDED", active.usage)
                session.commit()
                return result
    except AppError:
        raise
    except Exception:  # noqa: BLE001 - never expose provider bodies/URLs/keys, and never re-POST uncertain work
        if task_id:
            raise fail(
                "VIDEO_RESULT_PENDING", "视频任务已提交，查询或下载未完成；重试将继续处理原任务"
            ) from None
        raise fail(
            "MODEL_OUTCOME_UNKNOWN", "视频创建请求未取得可验证结果，请核验服务商记录，不会自动重发", True
        ) from None
    finally:
        produced_file.unlink(missing_ok=True)
