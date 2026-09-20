"""Fail-closed adapter for the isolated on-device voice and avatar runtimes."""

from __future__ import annotations

import fcntl
import json
import math
import os
import re
import signal
import subprocess
import threading
import time
from collections import deque
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ..config import ROOT, AppConfig
from .errors import CanceledError, IntegrationError

_MANIFEST = Path(".tools/local-ai/manifest.json")
_SANDBOX = Path("/usr/bin/sandbox-exec")
_SANDBOX_PROFILE = "(version 1)(allow default)(deny network*)"
_RUNNER = Path("scripts/local_ai_runtime.py")
_COMPONENTS = {
    "tts": {
        "model": "mlx-community/Qwen3-TTS-12Hz-1.7B-Base-bf16",
        "engine": "mlx",
        "python": Path(".tools/local-ai/tts/bin/python"),
        "marker": Path(".tools/local-ai/models/qwen3-tts/config.json"),
    },
    "chatterbox": {
        "model": "mlx-community/chatterbox-multilingual-v3",
        "engine": "mlx",
        "python": Path(".tools/local-ai/tts/bin/python"),
        "marker": Path(".tools/local-ai/models/chatterbox/config.json"),
    },
    "avatar": {
        "model": "MuseTalk-1.5",
        "engine": "mps",
        "python": Path(".tools/local-ai/avatar/bin/python"),
        "marker": Path(".tools/local-ai/models/avatar/musetalkV15/unet.pth"),
    },
    "transcribe": {
        "model": "whisper-tiny",
        "engine": "cpu",
        "python": Path(".tools/local-ai/avatar/bin/python"),
        "marker": Path(".tools/local-ai/models/avatar/whisper/config.json"),
    },
}
_TIMEOUTS = {
    "tts": 600.0,
    "chatterbox": 600.0,
    "avatar": 1800.0,
    "transcribe": 300.0,
    "probe": 60.0,
}
_OUTPUT_LIMITS = {
    "tts": 64 * 1024 * 1024,
    "chatterbox": 64 * 1024 * 1024,
    "avatar": 768 * 1024 * 1024,
}
_CAPTURE_LIMIT = 512 * 1024
_RESULT_PREFIX = b"LOCAL_AI_RESULT="


def _manifest() -> tuple[dict | None, str | None]:
    path = ROOT / _MANIFEST
    if not path.is_file():
        return None, "manifest_missing"
    try:
        if path.stat().st_size > 1024 * 1024:
            raise ValueError("manifest too large")
        value = json.loads(path.read_text(encoding="utf-8"))
        if (
            not isinstance(value, dict)
            or value.get("schema_version") != 1
            or not isinstance(value.get("components"), dict)
            or not isinstance(value.get("revision"), str)
            or not value["revision"].strip()
            or len(value["revision"]) > 200
        ):
            raise ValueError("invalid manifest")
        return value, None
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError, TypeError):
        return None, "manifest_invalid"


def _component_status(name: str, manifest: dict | None, manifest_reason: str | None) -> dict:
    expected = _COMPONENTS[name]
    result = {"installed": False, "model": expected["model"], "engine": expected["engine"]}
    if manifest_reason:
        result["reason"] = manifest_reason
        return result
    component = manifest["components"].get(name) if manifest else None
    if not isinstance(component, dict) or component.get("installed") is not True:
        result["reason"] = "component_not_installed"
        return result
    if component.get("model") != expected["model"]:
        result["reason"] = "model_mismatch"
        return result
    executable = ROOT / expected["python"]
    if not executable.is_file() or not os.access(executable, os.X_OK):
        result["reason"] = "runtime_missing"
        return result
    if not (ROOT / expected["marker"]).is_file():
        result["reason"] = "model_missing"
        return result
    result["installed"] = True
    return result


def _busy(config: AppConfig) -> bool:
    path = config.data_dir / "runs" / "local-ai.lock"
    if path.is_symlink():
        return True
    if not path.is_file():
        return False
    try:
        if not path.resolve().is_relative_to((config.data_dir / "runs").resolve()):
            return True
        with path.open("r") as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            finally:
                try:
                    fcntl.flock(handle, fcntl.LOCK_UN)
                except OSError:
                    pass
    except OSError:
        return True
    return False


def local_status(config: AppConfig) -> dict:
    """Read installation markers without importing or starting an ML runtime."""

    manifest, reason = _manifest()
    revision = manifest.get("revision") if manifest else None
    return {
        "tts": _component_status("tts", manifest, reason),
        "chatterbox": _component_status("chatterbox", manifest, reason),
        "avatar": _component_status("avatar", manifest, reason),
        "transcribe": _component_status("transcribe", manifest, reason),
        "busy": _busy(config),
        "revision": revision,
    }


def _path(value: Any, *, roots: tuple[Path, ...], name: str, must_exist: bool) -> Path:
    if not isinstance(value, (str, os.PathLike)):
        raise IntegrationError("LOCAL_AI_INPUT_INVALID", f"{name}路径无效")
    path = Path(value).expanduser()
    try:
        resolved = path.resolve(strict=must_exist)
    except (OSError, RuntimeError) as exc:
        raise IntegrationError("LOCAL_AI_INPUT_INVALID", f"{name}路径无效") from exc
    allowed = tuple(root.resolve() for root in roots)
    if not any(resolved.is_relative_to(root) for root in allowed):
        raise IntegrationError("LOCAL_AI_PATH_DENIED", f"{name}不在允许的本地目录中")
    if must_exist and not resolved.is_file():
        raise IntegrationError("LOCAL_AI_INPUT_MISSING", f"找不到{name}")
    if not must_exist:
        parent = resolved.parent
        if not parent.is_dir() or not parent.resolve().is_relative_to(allowed[0]):
            raise IntegrationError("LOCAL_AI_OUTPUT_INVALID", "本地产物目录无效")
    return resolved


def _clean_environment() -> dict[str, str]:
    sensitive = ("API_KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL")
    environment = {
        key: value
        for key, value in os.environ.items()
        if not any(marker in key.upper() for marker in sensitive)
        and not key.upper().startswith(("AWS_", "AZURE_"))
    }
    environment.update(
        {
            "HF_HUB_OFFLINE": "1",
            "TRANSFORMERS_OFFLINE": "1",
            "HF_HUB_DISABLE_TELEMETRY": "1",
        }
    )
    return environment


class _BoundedCapture:
    def __init__(self, stream, limit: int, overflow: threading.Event):
        self.stream, self.limit, self.overflow = stream, limit, overflow
        self.parts: deque[bytes] = deque()
        self.size = 0
        self.thread = threading.Thread(target=self._drain, daemon=True)

    def _drain(self) -> None:
        try:
            while block := self.stream.read(8192):
                self.parts.append(block)
                self.size += len(block)
                if self.size > self.limit:
                    self.overflow.set()
                while self.size > self.limit and self.parts:
                    removed = self.parts.popleft()
                    self.size -= len(removed)
        finally:
            self.stream.close()

    def start(self) -> None:
        self.thread.start()

    def finish(self) -> bytes:
        self.thread.join(timeout=5)
        return b"".join(self.parts)


def _terminate(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        process.wait()
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=5)


def _parse_result(raw: bytes) -> dict:
    line = next((line for line in reversed(raw.splitlines()) if line.startswith(_RESULT_PREFIX)), None)
    if line is None or len(line) > 128 * 1024:
        raise IntegrationError("LOCAL_AI_RESULT_INVALID", "本地模型没有返回有效结果")
    try:
        value = json.loads(line[len(_RESULT_PREFIX) :].decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise IntegrationError("LOCAL_AI_RESULT_INVALID", "本地模型返回结果格式无效") from exc
    if not isinstance(value, dict):
        raise IntegrationError("LOCAL_AI_RESULT_INVALID", "本地模型返回结果格式无效")
    error = value.get("error")
    if isinstance(error, dict):
        code = str(error.get("code") or "LOCAL_AI_FAILED")[:100]
        if not re.fullmatch(r"[A-Z0-9_]+", code):
            code = "LOCAL_AI_FAILED"
        message = " ".join(str(error.get("message") or "本地模型执行失败").split())[:500]
        raise IntegrationError(code, message or "本地模型执行失败")
    return value


def _validated_result(config: AppConfig, operation: str, result: dict, output: Path | None) -> dict:
    try:
        elapsed = float(result["elapsed_seconds"])
    except (KeyError, TypeError, ValueError) as exc:
        raise IntegrationError("LOCAL_AI_RESULT_INVALID", "本地模型返回结果缺少耗时") from exc
    if not math.isfinite(elapsed) or elapsed < 0:
        raise IntegrationError("LOCAL_AI_RESULT_INVALID", "本地模型返回了无效耗时")
    if operation == "transcribe":
        transcript = result.get("transcript")
        if not isinstance(transcript, str) or len(transcript) > 2000 or result.get("engine") != "cpu":
            raise IntegrationError("LOCAL_AI_RESULT_INVALID", "本地转写结果无效或过长")
        return {"transcript": transcript.strip(), "engine": "cpu", "elapsed_seconds": elapsed}
    if operation == "probe":
        return result
    returned = result.get("output_path")
    if not isinstance(returned, str) or output is None:
        raise IntegrationError("LOCAL_AI_RESULT_INVALID", "本地模型没有返回产物路径")
    actual = _path(returned, roots=(config.data_dir / "runs",), name="产物", must_exist=True)
    if (
        actual != output.resolve()
        or actual.stat().st_size == 0
        or actual.stat().st_size > _OUTPUT_LIMITS[operation]
    ):
        raise IntegrationError("LOCAL_AI_OUTPUT_INVALID", "本地模型产物路径或大小无效")
    duration = result.get("duration")
    try:
        duration_value = float(duration)
    except (TypeError, ValueError) as exc:
        raise IntegrationError("LOCAL_AI_RESULT_INVALID", "本地模型返回了无效媒体时长") from exc
    if not math.isfinite(duration_value) or duration_value <= 0:
        raise IntegrationError("LOCAL_AI_RESULT_INVALID", "本地模型返回了无效媒体时长")
    expected = _COMPONENTS[operation]
    if result.get("engine") != expected["engine"] or result.get("model") != expected["model"]:
        raise IntegrationError("LOCAL_AI_RESULT_INVALID", "本地模型返回的引擎身份不匹配")
    return {
        "output_path": str(actual),
        "duration": duration_value,
        "engine": expected["engine"],
        "elapsed_seconds": elapsed,
        "model": result["model"],
    }


def run_local(
    config: AppConfig,
    operation: str,
    payload: dict,
    *,
    progress: Callable[[str, int], None],
    canceled: Callable[[], bool],
) -> dict:
    """Execute one isolated local operation without inheriting cloud credentials or network access."""

    if operation not in _TIMEOUTS or not isinstance(payload, dict):
        raise IntegrationError("LOCAL_AI_OPERATION_INVALID", "不支持的本地模型操作")
    if not callable(progress) or not callable(canceled):
        raise IntegrationError("LOCAL_AI_CALLBACK_INVALID", "缺少任务进度或取消回调")
    component = str(payload.get("component")) if operation == "probe" else operation
    if component not in _COMPONENTS:
        raise IntegrationError("LOCAL_AI_COMPONENT_INVALID", "本地模型组件无效")
    status = local_status(config)[component]
    if not status["installed"]:
        raise IntegrationError("LOCAL_AI_NOT_INSTALLED", "本地模型尚未完整安装")
    if not _SANDBOX.is_file() or not os.access(_SANDBOX, os.X_OK):
        raise IntegrationError("LOCAL_AI_SANDBOX_REQUIRED", "系统网络隔离工具不可用，本地模型不会启动")
    runner = ROOT / _RUNNER
    runtime_dir = ROOT / ".tools" / "local-ai"
    if not runner.is_file() or not runtime_dir.is_dir():
        raise IntegrationError("LOCAL_AI_RUNNER_MISSING", "本地模型运行脚本不存在")

    assets = config.data_dir / "assets"
    runs = config.data_dir / "runs"
    request_payload = dict(payload)
    output = None
    if operation in {"tts", "chatterbox"}:
        text = request_payload.get("text")
        if not isinstance(text, str) or not text.strip() or len(text) > 500:
            raise IntegrationError("LOCAL_AI_INPUT_INVALID", "待合成文字无效")
        reference_audio_path = str(
            _path(
                request_payload.get("reference_audio_path"),
                roots=(assets,),
                name="参考音频",
                must_exist=True,
            )
        )
        output = _path(request_payload.get("output_path"), roots=(runs,), name="产物", must_exist=False)
        allowed_suffixes = {".wav"} if operation == "chatterbox" else {".wav", ".mp3"}
        if output.suffix.lower() not in allowed_suffixes:
            raise IntegrationError("LOCAL_AI_OUTPUT_INVALID", "配音产物格式无效")
        request_payload = {
            "text": text,
            "reference_audio_path": reference_audio_path,
            "output_path": str(output),
        }
        if operation == "tts":
            reference_text = payload.get("reference_text")
            if (
                not isinstance(reference_text, str)
                or not reference_text.strip()
                or len(reference_text) > 2000
            ):
                raise IntegrationError("LOCAL_AI_INPUT_INVALID", "参考音频文字无效")
            request_payload["reference_text"] = reference_text
        else:
            emotion_strength = payload.get("emotion_strength")
            if (
                isinstance(emotion_strength, bool)
                or not isinstance(emotion_strength, (int, float))
                or not math.isfinite(float(emotion_strength))
                or not 0 <= float(emotion_strength) <= 1
            ):
                raise IntegrationError("LOCAL_AI_INPUT_INVALID", "情感强度必须是 0 到 1 的有限数值")
            request_payload["emotion_strength"] = float(emotion_strength)
    elif operation == "avatar":
        image_path = str(
            _path(request_payload.get("image_path"), roots=(assets,), name="肖像", must_exist=True)
        )
        audio_path = str(
            _path(
                request_payload.get("audio_path"),
                roots=(assets, runs),
                name="驱动音频",
                must_exist=True,
            )
        )
        output = _path(request_payload.get("output_path"), roots=(runs,), name="产物", must_exist=False)
        if output.suffix.lower() != ".mp4":
            raise IntegrationError("LOCAL_AI_OUTPUT_INVALID", "口型视频产物格式无效")
        request_payload = {
            "image_path": image_path,
            "audio_path": audio_path,
            "output_path": str(output),
        }
    elif operation == "transcribe":
        request_payload = {
            "audio_path": str(
                _path(
                    request_payload.get("audio_path"),
                    roots=(assets,),
                    name="参考音频",
                    must_exist=True,
                )
            )
        }
    else:
        request_payload = {"component": component}

    request = {**request_payload, "operation": operation}
    encoded = json.dumps(request, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"
    if len(encoded) > 64 * 1024:
        raise IntegrationError("LOCAL_AI_INPUT_INVALID", "本地模型请求过大")
    command = [
        str(_SANDBOX),
        "-p",
        _SANDBOX_PROFILE,
        str(ROOT / _COMPONENTS[component]["python"]),
        str(runner),
    ]
    lock_path = runs / "local-ai.lock"
    if lock_path.is_symlink():
        raise IntegrationError("LOCAL_AI_PATH_DENIED", "本地模型锁文件路径无效")
    overflow = threading.Event()
    started = time.monotonic()
    with lock_path.open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise IntegrationError("LOCAL_AI_BUSY", "本地模型正处理另一项任务") from None
        if canceled():
            raise CanceledError()
        try:
            process = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=runtime_dir,
                env=_clean_environment(),
                start_new_session=True,
            )
        except OSError as exc:
            raise IntegrationError("LOCAL_AI_START_FAILED", "无法启动本地模型运行环境") from exc
        stdout = _BoundedCapture(process.stdout, _CAPTURE_LIMIT, overflow)
        stderr = _BoundedCapture(process.stderr, _CAPTURE_LIMIT, overflow)
        stdout.start()
        stderr.start()
        try:
            assert process.stdin is not None
            try:
                process.stdin.write(encoded)
                process.stdin.close()
            except OSError as exc:
                raise IntegrationError("LOCAL_AI_FAILED", "本地模型进程未能接收任务") from exc
            last_heartbeat = 0.0
            while process.poll() is None:
                elapsed = time.monotonic() - started
                if canceled():
                    raise CanceledError()
                if overflow.is_set():
                    raise IntegrationError("LOCAL_AI_OUTPUT_TOO_LARGE", "本地模型诊断输出超过限制")
                if elapsed > _TIMEOUTS[operation]:
                    raise IntegrationError("LOCAL_AI_TIMEOUT", "本地模型执行超时")
                if elapsed - last_heartbeat >= 1.0:
                    progress(f"本地{component}模型运行中", 50)
                    last_heartbeat = elapsed
                time.sleep(0.1)
        finally:
            if process.poll() is None:
                _terminate(process)
            else:
                process.wait()
        raw_stdout = stdout.finish()
        _ = stderr.finish()  # Drained for deadlock safety; never log request-adjacent diagnostics here.
        if overflow.is_set():
            raise IntegrationError("LOCAL_AI_OUTPUT_TOO_LARGE", "本地模型诊断输出超过限制")
        if process.returncode != 0 and _RESULT_PREFIX not in raw_stdout:
            raise IntegrationError("LOCAL_AI_FAILED", "本地模型运行失败，请检查本机安装状态")
        result = _validated_result(config, operation, _parse_result(raw_stdout), output)
        progress(f"本地{component}模型已完成", 100)
        return result
