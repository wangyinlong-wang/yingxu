from __future__ import annotations

import base64
import binascii
import copy
import io
import json
import re
from pathlib import Path

import httpx
from PIL import Image, ImageFilter
from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from .config import AppConfig
from .db import settings
from .model_credentials import resolve_model_key, resolve_tikhub_key
from .models import Job, ModelCall, now, uid
from .protocols import build_request, decode_response
from .services import AppError, acquire_write, active_model, assert_models, digest, require, usage_summary
from .storage import register_file


def _finish_event(record: ModelCall, status: str, usage: dict | None = None) -> None:
    events = copy.deepcopy(record.events)
    if events:
        events[-1].update({"status": status, "completed_at": now(), "usage": usage or {}})
    record.events = events
    record.status = status


def _lease_guard(session: Session, job_id: str, owner: str, *, allow_cancel: bool = False) -> Job:
    job = require(session, Job, job_id)
    if job.status != "RUNNING" or job.lease_owner != owner or not job.lease_until or job.lease_until <= now():
        raise AppError("TASK_LEASE_LOST", "执行权已失效，当前进程不会继续请求或覆盖结果", 409)
    if job.cancel_requested and not allow_cancel:
        raise AppError("TASK_CANCELED", "任务已取消", 409)
    return job


def _active_attempt(session: Session, record: ModelCall, job_id: str, owner: str, attempt_id: str) -> Job:
    job = _lease_guard(session, job_id, owner, allow_cancel=True)
    if (
        record.status != "STARTED"
        or record.job_id != job_id
        or not record.events
        or record.events[-1].get("attempt_id") != attempt_id
    ):
        raise AppError("TASK_LEASE_LOST", "请求状态已被接管，晚到结果不会覆盖核验状态", 409)
    return job


def _record_failure(
    engine: Engine,
    identifier: str,
    code: str,
    message: str,
    uncertain: bool,
    *,
    job_id: str,
    owner: str,
    attempt_id: str,
    response_details: dict | None = None,
) -> None:
    with Session(engine) as session:
        acquire_write(session)
        record = require(session, ModelCall, identifier)
        try:
            job = _active_attempt(session, record, job_id, owner, attempt_id)
        except AppError:
            return
        _finish_event(record, "UNKNOWN" if uncertain else "FAILED")
        record.error_code = code
        if response_details is not None:
            record.response = response_details
        # Recovery sees this marker atomically with a definitive rejection and requires explicit retry.
        job.error_code, job.error_message = code, message
        session.commit()


def invoke_tikhub_source(
    engine: Engine,
    config: AppConfig,
    *,
    job_id: str,
    owner: str,
    source_id: str,
    screen_name: str | None = None,
    search: tuple[str, str] | None = None,
) -> dict:
    """Perform one durable paid TikHub read; successful raw data is cached before parsing."""

    if (screen_name is None) == (search is None):
        raise AppError("INVALID_SOURCE", "TikHub 读取必须指定账号或搜索条件之一", 422)
    if search is None:
        endpoint = "https://api.tikhub.io/api/v1/twitter/web/fetch_user_post_tweet"
        operation = "twitter-user-posts"
        params = {"screen_name": screen_name}
    else:
        endpoint = "https://api.tikhub.io/api/v1/twitter/web/fetch_search_timeline"
        operation = "twitter-search"
        params = {"keyword": search[0], "search_type": search[1]}
    scope = f"job:{job_id}:source:{source_id}:{operation}"
    request_key = digest(
        {
            "scope": scope,
            "method": "GET",
            "url": endpoint,
            "params": params,
        }
    )
    slot = digest({"scope": scope, "kind": "source"})
    with Session(engine, expire_on_commit=False) as session:
        acquire_write(session)
        job = _lease_guard(session, job_id, owner)
        previous_key = job.payload.get("source_requests", {}).get(slot)
        if previous_key and previous_key != request_key:
            raise AppError(
                "JOB_INPUT_CHANGED",
                "恢复采集任务时 TikHub 请求输入发生变化，不会自动发起新的付费读取",
                409,
            )
        job.payload = {
            **job.payload,
            "source_requests": {**job.payload.get("source_requests", {}), slot: request_key},
        }
        existing = session.scalar(select(ModelCall).where(ModelCall.request_key == request_key))
        if existing and existing.status == "SUCCEEDED":
            session.commit()
            return copy.deepcopy(existing.response)
        if existing and existing.status in {"STARTED", "UNKNOWN"}:
            raise AppError(
                "MODEL_OUTCOME_UNKNOWN",
                "同一 TikHub 读取结果尚未确认，请核验原任务后处理；系统不会自动再次请求",
                409,
                {"original_job_id": existing.job_id},
            )
        if existing and existing.status == "ABANDONED":
            raise AppError(
                "MODEL_RESULT_ABANDONED",
                "该 TikHub 读取结果未明且已放弃重试，请新建明确的数据采集操作",
                409,
            )
        credential, _credential_source = resolve_tikhub_key(config)
        if not credential:
            raise AppError("TIKHUB_NOT_CONFIGURED", "请先在本机配置 TikHub API Key", 409)
        current = settings(session, config)
        if usage_summary(session, current["timezone"])["calls_today"] >= current["limits"]["daily_calls"]:
            raise AppError("DAILY_CALL_LIMIT", "今日模型与数据 API 调用额度已用完", 409)
        attempt_id = uid()
        event = {"started_at": now(), "status": "STARTED", "job_id": job_id, "attempt_id": attempt_id}
        if existing:
            record = existing
            record.job_id, record.status, record.error_code = job_id, "STARTED", None
            record.attempts += 1
            record.events = [*record.events, event]
        else:
            record = ModelCall(
                id=uid(),
                job_id=job_id,
                request_key=request_key,
                kind="source",
                model=f"tikhub:{operation}",
                status="STARTED",
                events=[event],
            )
            session.add(record)
        session.commit()
        record_id = record.id

    try:
        with httpx.Client(timeout=60, follow_redirects=False) as client:
            with Session(engine) as session:
                acquire_write(session)
                record = require(session, ModelCall, record_id)
                try:
                    _active_attempt(session, record, job_id, owner, attempt_id)
                    _lease_guard(session, job_id, owner)
                except AppError as error:
                    if error.code == "TASK_CANCELED" and record.status == "STARTED":
                        _finish_event(record, "FAILED")
                        record.error_code = "CANCELED_BEFORE_SEND"
                        events = copy.deepcopy(record.events)
                        events[-1]["status"] = "CANCELED_BEFORE_SEND"
                        record.events = events
                        session.commit()
                    raise
                events = copy.deepcopy(record.events)
                events[-1]["dispatch_at"] = now()
                record.events = events
                session.commit()
            with client.stream(
                "GET",
                endpoint,
                params=params,
                headers={"Authorization": f"Bearer {credential}", "Accept": "application/json"},
            ) as response:
                status = response.status_code
                if status >= 300:
                    uncertain = status >= 500
                    code = "MODEL_OUTCOME_UNKNOWN" if uncertain else f"TIKHUB_HTTP_{status}"
                    reason = "unspecified"
                    error_bytes = bytearray()
                    try:
                        for chunk in response.iter_bytes():
                            error_bytes.extend(chunk[: max(0, 8192 - len(error_bytes))])
                            if len(error_bytes) >= 8192:
                                break
                    except httpx.TransportError:
                        pass
                    # Classify known provider explanations without persisting or
                    # displaying arbitrary response text that might echo secrets.
                    explanation = error_bytes.decode("utf-8", errors="replace").casefold()
                    if (
                        any(word in explanation for word in ("insufficient", "not sufficient", "not enough"))
                        and any(word in explanation for word in ("balance", "credit"))
                    ) or any(word in explanation for word in ("余额不足", "额度不足")):
                        reason = "insufficient_balance"
                    elif re.search(r"(?<![a-z])ip(?:v[46])?(?![a-z])", explanation) and any(
                        word in explanation for word in ("whitelist", "allowlist", "not allowed", "白名单")
                    ):
                        reason = "ip_restricted"
                    elif ("api key" in explanation or "apikey" in explanation) and any(
                        word in explanation
                        for word in ("invalid", "incorrect", "expired", "无效", "过期", "不正确")
                    ):
                        reason = "invalid_key"
                    elif any(
                        word in explanation for word in ("maintenance", "temporarily unavailable", "维护中")
                    ):
                        reason = "endpoint_unavailable"
                    messages = {
                        401: "TikHub 鉴权失败，请检查本机 API Key",
                        403: "TikHub 返回 HTTP 403，当前读取被拒绝",
                        429: "TikHub 调用额度不足或当前限流，请核实账户额度后重试",
                    }
                    classified = {
                        "insufficient_balance": "TikHub 返回额度不足，请检查账户余额或 API Key 的调用额度",
                        "ip_restricted": "TikHub 拒绝了当前访问 IP，请检查 API Key 的 IP 白名单",
                        "invalid_key": "TikHub 返回密钥无效或过期，请检查本机配置的 API Key",
                        "endpoint_unavailable": "TikHub 提示该接口当前维护或暂不可用，请稍后核实服务状态",
                    }
                    message = (
                        "TikHub 服务返回错误，付费读取结果未明；请先核验服务商记录"
                        if uncertain
                        else classified.get(
                            reason, messages.get(status, f"TikHub 返回 HTTP {status}，本次读取未成功")
                        )
                    )
                    _record_failure(
                        engine,
                        record_id,
                        code,
                        message,
                        uncertain,
                        job_id=job_id,
                        owner=owner,
                        attempt_id=attempt_id,
                        response_details={"http_status": status, "reason": reason},
                    )
                    raise AppError(code, message, 409)
                chunks, size = [], 0
                for block in response.iter_bytes():
                    size += len(block)
                    if size > 4 * 1024 * 1024:
                        raise ValueError("TikHub response too large")
                    chunks.append(block)
                raw = b"".join(chunks)
        envelope = json.loads(raw)
        if not isinstance(envelope, dict) or "code" not in envelope:
            raise ValueError("TikHub response missing business status")
        business_code = envelope.get("code")
        if str(business_code) != "200":
            code = "TIKHUB_BUSINESS_REJECTED"
            messages = {
                "401": "TikHub 鉴权失败，请检查本机 API Key",
                "403": "TikHub 当前账户没有读取此数据的权限",
                "429": "TikHub 调用额度不足或当前限流，请核实账户额度后重试",
            }
            message = messages.get(str(business_code), "TikHub 数据接口拒绝了本次读取，请核实服务状态")
            _record_failure(
                engine,
                record_id,
                code,
                message,
                False,
                job_id=job_id,
                owner=owner,
                attempt_id=attempt_id,
            )
            raise AppError(code, message, 409)
        result = {"data": copy.deepcopy(envelope.get("data"))}
        request_id = envelope.get("request_id") or envelope.get("requestId")
        if isinstance(request_id, str) and 0 < len(request_id) <= 200:
            result["request_id"] = request_id
        with Session(engine) as session:
            acquire_write(session)
            record = require(session, ModelCall, record_id)
            _active_attempt(session, record, job_id, owner, attempt_id)
            record.response, record.usage, record.error_code = result, {}, None
            _finish_event(record, "SUCCEEDED")
            session.commit()
        return result
    except AppError:
        raise
    except (httpx.ConnectError, httpx.ConnectTimeout):
        _record_failure(
            engine,
            record_id,
            "TIKHUB_CONNECT_FAILED",
            "未建立到 TikHub 的连接，服务端未返回 HTTP 响应；系统不会自动重试",
            False,
            job_id=job_id,
            owner=owner,
            attempt_id=attempt_id,
        )
        raise AppError(
            "TIKHUB_CONNECT_FAILED",
            "未建立到 TikHub 的连接，服务端未返回 HTTP 响应；可稍后明确重试",
            409,
        ) from None
    except (httpx.TransportError, OSError, ValueError, TypeError, json.JSONDecodeError):
        _record_failure(
            engine,
            record_id,
            "MODEL_OUTCOME_UNKNOWN",
            "TikHub 请求已发送但未取得完整可验证结果；请核验服务商记录",
            True,
            job_id=job_id,
            owner=owner,
            attempt_id=attempt_id,
        )
        raise AppError(
            "MODEL_OUTCOME_UNKNOWN",
            "TikHub 读取结果未明；系统不会自动再次请求，请先核验服务商记录",
            409,
        ) from None
    except Exception:  # noqa: BLE001 - all unexpected post-dispatch outcomes remain unknown
        _record_failure(
            engine,
            record_id,
            "MODEL_OUTCOME_UNKNOWN",
            "TikHub 返回结果处理未完成，需要核验服务商记录",
            True,
            job_id=job_id,
            owner=owner,
            attempt_id=attempt_id,
        )
        raise AppError(
            "MODEL_OUTCOME_UNKNOWN", "TikHub 结果处理未完成；已保留记录且不会自动重发", 409
        ) from None


def invoke_model(
    engine: Engine,
    config: AppConfig,
    *,
    job_id: str,
    owner: str,
    kind: str,
    payload: dict,
    scope: str,
    content_id: str | None = None,
) -> dict:
    """Persist intent before POST, cache definitive success, never automatically re-POST unknown outcomes."""
    with Session(engine, expire_on_commit=False) as session:
        acquire_write(session)
        job = _lease_guard(session, job_id, owner)
        current = assert_models(session, config, (kind,))
        source, provider = active_model(current, kind)
        if provider is None:
            raise AppError(
                "MODEL_NOT_CONFIGURED", "内置本地声音引擎不通过模型 HTTP 接口调用", 409
            )
        endpoint, request, accept = build_request(kind, provider, payload)
        url = provider["base_url"].rstrip("/") + "/" + endpoint
        output_processing = (
            "ark-clean-bottom-safe-area-v2"
            if kind == "image"
            and provider.get("protocol") == "ark"
            and not bool(provider.get("watermark", False))
            else None
        )
        request_key = digest(
            {
                "scope": scope,
                "kind": kind,
                "url": url,
                "request": request,
                "output_processing": output_processing,
                # Only local profiles carry a discriminator, so legacy third-party
                # in-flight and cached request keys stay byte-identical.
                **({"source": "local"} if source == "local" else {}),
            }
        )
        slot = digest({"scope": scope, "kind": kind})
        previous_key = job.payload.get("model_requests", {}).get(slot)
        if previous_key and previous_key != request_key:
            raise AppError(
                "JOB_INPUT_CHANGED",
                "恢复任务的接口或输入发生变化；请检查配置后明确重试，不会自动重复调用",
                409,
            )
        job.payload = {
            **job.payload,
            "model_requests": {**job.payload.get("model_requests", {}), slot: request_key},
        }
        existing = session.scalar(select(ModelCall).where(ModelCall.request_key == request_key))
        if existing and existing.status == "SUCCEEDED":
            session.commit()
            return copy.deepcopy(existing.response)
        if existing and existing.status in {"STARTED", "UNKNOWN"}:
            raise AppError(
                "MODEL_OUTCOME_UNKNOWN",
                "同一请求已有结果未明的调用，请核验原任务的服务商记录后处理",
                409,
                {"original_job_id": existing.job_id},
            )
        if existing and existing.status == "ABANDONED":
            raise AppError(
                "MODEL_RESULT_ABANDONED",
                "该请求结果未明且已放弃重试；请导入已有产物，或明确修改输入后创建新任务",
                409,
            )
        usage = usage_summary(session, current["timezone"])
        if usage["calls_today"] >= current["limits"]["daily_calls"]:
            raise AppError(
                "DAILY_CALL_LIMIT", "已达到今日模型调用上限，任务已保留，可调整额度或次日重试", 409
            )
        # Resolve the credential before recording dispatch/STARTED so a Keychain failure
        # can never leave a false unknown paid call behind. Local keys are optional.
        credential, _key_source = resolve_model_key(config, kind, source, provider["base_url"])
        attempt_id = uid()
        event = {"started_at": now(), "status": "STARTED", "job_id": job_id, "attempt_id": attempt_id}
        if existing:
            record = existing
            record.job_id, record.status, record.error_code = job_id, "STARTED", None
            record.attempts += 1
            record.events = [*record.events, event]
        else:
            record = ModelCall(
                id=uid(),
                job_id=job_id,
                request_key=request_key,
                kind=kind,
                model=provider["model"],
                status="STARTED",
                events=[event],
            )
            session.add(record)
        session.commit()
        record_id = record.id

    # No network I/O while holding a SQLite transaction. httpx retries are disabled by default.
    try:
        limit = 2 * 1024 * 1024 if kind == "text" else 100 * 1024 * 1024
        with httpx.Client(timeout=provider["timeout_seconds"], follow_redirects=False) as client:
            # The final dispatch fence runs after client setup. Cancellation after this committed
            # dispatch point is an in-flight request; its definitive response may only be cached.
            with Session(engine) as session:
                acquire_write(session)
                record = require(session, ModelCall, record_id)
                try:
                    _active_attempt(session, record, job_id, owner, attempt_id)
                    _lease_guard(session, job_id, owner)
                except AppError as error:
                    if error.code == "TASK_CANCELED" and record.status == "STARTED":
                        _finish_event(record, "FAILED")
                        record.error_code = "CANCELED_BEFORE_SEND"
                        events = copy.deepcopy(record.events)
                        events[-1]["status"] = "CANCELED_BEFORE_SEND"
                        record.events = events
                        session.commit()
                    raise
                events = copy.deepcopy(record.events)
                events[-1]["dispatch_at"] = now()
                record.events = events
                session.commit()
            headers = {"Accept": accept}
            if credential:
                headers["Authorization"] = f"Bearer {credential}"
            with client.stream(
                "POST",
                url,
                json=request,
                headers=headers,
            ) as response:
                status = response.status_code
                if status >= 300:
                    uncertain = status >= 500
                    code = "MODEL_OUTCOME_UNKNOWN" if uncertain else f"MODEL_HTTP_{status}"
                    messages = {
                        401: "模型接口鉴权失败，请检查本地密钥",
                        403: "当前模型接口或账户没有调用权限",
                        404: "模型接口路径或模型名不存在，请检查 Base URL 和能力配置",
                        429: "模型服务限流或额度不足，请核实服务商状态后重试",
                    }
                    message = (
                        "模型服务返回错误，调用结果未明，请先核验服务商记录"
                        if uncertain
                        else messages.get(status, f"模型服务返回 HTTP {status}，请核实该接口支持的参数")
                    )
                    _record_failure(
                        engine,
                        record_id,
                        code,
                        message,
                        uncertain,
                        job_id=job_id,
                        owner=owner,
                        attempt_id=attempt_id,
                    )
                    raise AppError(code, message, 409)
                chunks, size = [], 0
                for block in response.iter_bytes():
                    size += len(block)
                    if size > limit:
                        raise ValueError("response too large")
                    chunks.append(block)
                raw = b"".join(chunks)
        decoded = decode_response(kind, provider.get("protocol", "openai"), raw)
        usage = decoded.get("usage", {})
        result = {}
        produced_file: Path | None = None
        image_processing: dict | None = None
        if kind == "text":
            result = {"text": decoded["text"], "usage": usage}
        elif kind == "image":
            if decoded.get("image_b64"):
                binary = base64.b64decode(decoded["image_b64"], validate=True)
            elif decoded.get("image_url"):
                from .integrations.net import safe_get

                binary, _, _ = safe_get(decoded["image_url"], max_bytes=40 * 1024 * 1024, timeout=30)
            else:
                raise ValueError("missing image output")
            if len(binary) > 40 * 1024 * 1024:
                raise ValueError("image output too large")
            produced_file = config.data_dir / "runs" / f"{record_id}-{attempt_id}.png"
            with Image.open(io.BytesIO(binary)) as image:
                if image.width * image.height > 40_000_000:
                    raise ValueError("image dimensions too large")
                prepared = image.convert("RGB")
                if provider.get("protocol") == "ark" and not bool(provider.get("watermark", False)):
                    # Ark may still return its bottom-right badge when watermark=false. The generation prompt
                    # reserves this bottom strip, so replace it from the adjacent safe-area texture instead of
                    # shipping provider text or cropping/stretching the requested 9:16 frame.
                    band = max(1, round(prepared.height * 0.12))
                    source_top = max(0, prepared.height - 2 * band)
                    replacement = prepared.crop(
                        (0, source_top, prepared.width, source_top + band)
                    ).transpose(Image.Transpose.FLIP_TOP_BOTTOM)
                    if replacement.height != band:
                        replacement = replacement.resize(
                            (prepared.width, band), Image.Resampling.LANCZOS
                        )
                    replacement = replacement.filter(
                        ImageFilter.GaussianBlur(radius=max(2, prepared.width // 250))
                    )
                    prepared.paste(replacement, (0, prepared.height - band))
                    image_processing = {
                        "ark_clean_bottom_safe_area": True,
                        "bottom_fraction": 0.12,
                    }
                prepared.save(produced_file, "PNG")
        else:
            if decoded.get("audio_url"):
                from .integrations.net import safe_get

                audio_bytes, _, _ = safe_get(decoded["audio_url"], max_bytes=20 * 1024 * 1024, timeout=60)
            elif decoded.get("audio_b64"):
                audio_bytes = base64.b64decode(decoded["audio_b64"], validate=True)
            else:
                audio_bytes = decoded["audio_bytes"]
            audio_format = decoded.get("audio_format", "mp3")
            if audio_format not in {"mp3", "wav", "opus"}:
                raise ValueError("unsupported unframed audio format")
            produced_file = config.data_dir / "runs" / f"{record_id}-{attempt_id}.{audio_format}"
            produced_file.write_bytes(audio_bytes)
            from .integrations.media import probe_media

            media = probe_media(produced_file)
            if not media.get("has_audio") or not media.get("duration") or float(media["duration"]) <= 0:
                raise ValueError("missing speech output")
        with Session(engine) as session:
            acquire_write(session)
            record = require(session, ModelCall, record_id)
            _active_attempt(session, record, job_id, owner, attempt_id)
            if produced_file:
                mime_type = (
                    "image/png"
                    if kind == "image"
                    else {".mp3": "audio/mpeg", ".wav": "audio/wav", ".opus": "audio/ogg"}[
                        produced_file.suffix
                    ]
                )
                asset = register_file(
                    session,
                    config,
                    produced_file,
                    kind="image" if kind == "image" else "audio",
                    content_id=content_id,
                    mime_type=mime_type,
                    filename=f"{kind}-{record_id[:8]}{produced_file.suffix}",
                    input_hash=request_key,
                )
                result = {
                    "asset_id": asset.id,
                    "usage": usage,
                    **({"image_processing": image_processing} if image_processing else {}),
                }
            record.response, record.usage, record.error_code = result, usage, None
            _finish_event(record, "SUCCEEDED", usage)
            session.commit()
        if produced_file:
            produced_file.unlink(missing_ok=True)
        return result
    except AppError as error:
        if error.code in {"MODEL_RESPONSE_INVALID", "OUTPUT_MISSING", "DISK_FULL"}:
            _record_failure(
                engine,
                record_id,
                "MODEL_OUTCOME_UNKNOWN",
                "已收到响应，但产物尚未完整保存，请先核验本次调用",
                True,
                job_id=job_id,
                owner=owner,
                attempt_id=attempt_id,
            )
            raise AppError(
                "MODEL_OUTCOME_UNKNOWN",
                "已收到模型响应，但未取得完整可用产物；不会自动重发，请核验服务商记录",
                409,
            ) from None
        raise
    except (httpx.TransportError, OSError, ValueError, KeyError, IndexError, TypeError, binascii.Error):
        _record_failure(
            engine,
            record_id,
            "MODEL_OUTCOME_UNKNOWN",
            "请求结果未取得完整验证，需要核验服务商记录",
            True,
            job_id=job_id,
            owner=owner,
            attempt_id=attempt_id,
        )
        raise AppError(
            "MODEL_OUTCOME_UNKNOWN",
            "请求已发送，但未取得可验证的完整结果。请核验服务商记录；系统不会自动再次调用",
            409,
        ) from None
    except Exception:  # noqa: BLE001 - retain unknown outcome for all unexpected post-request decoder failures
        # Includes unsafe/unreadable returned image URLs. A successful paid POST must not be repeated implicitly.
        _record_failure(
            engine,
            record_id,
            "MODEL_OUTCOME_UNKNOWN",
            "模型返回结果处理未完成，需要核验服务商记录",
            True,
            job_id=job_id,
            owner=owner,
            attempt_id=attempt_id,
        )
        raise AppError(
            "MODEL_OUTCOME_UNKNOWN", "模型结果处理未完成，调用结果需核验；已保留记录且不会自动重发", 409
        ) from None
