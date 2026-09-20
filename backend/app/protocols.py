"""Pure request/response adapters for supported model HTTP protocols.

The caller owns authentication, network I/O, byte limits, persistence and
unknown-outcome handling.  This module never reads credentials or URLs itself.
"""

from __future__ import annotations

import copy
import json
import re
from typing import Any
from urllib.parse import urlsplit

from .services import AppError

_PROTOCOLS = {"openai", "ark", "dashscope"}
_SUPPORTED = {
    "text": {"openai"},
    "image": {"openai", "ark"},
    "speech": {"openai", "dashscope"},
}
_DASHSCOPE_LANGUAGES = {
    "chinese": "zh",
    "english": "en",
    "french": "fr",
    "german": "de",
    "japanese": "ja",
    "korean": "ko",
    "russian": "ru",
    "portuguese": "pt",
    "thai": "th",
    "indonesian": "id",
    "vietnamese": "vi",
    "spanish": "es",
    "italian": "it",
    "malaysian": "ms",
    "filipino": "fil",
    "arabic": "ar",
}
_DASHSCOPE_LANGUAGE_CODES = set(_DASHSCOPE_LANGUAGES.values())
_AUDIO_FORMATS = {"mp3", "wav", "pcm", "opus"}


def _error(code: str, message: str) -> AppError:
    return AppError(code, message, 409)


def _nonempty_string(value: Any, *, field: str, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise _error("MODEL_PROTOCOL_INVALID_REQUEST", f"模型请求字段 {field} 无效")
    return value.strip()


def _protocol(model_config: dict) -> str:
    value = str(model_config.get("protocol") or "openai").strip().lower()
    if value not in _PROTOCOLS:
        raise _error("MODEL_PROTOCOL_UNSUPPORTED", "模型协议不受支持")
    return value


def _ark_size(value: Any) -> str:
    size = _nonempty_string(value, field="size", maximum=30)
    if size.upper() in {"2K", "4K"}:
        return size.upper()
    match = re.fullmatch(r"(\d{3,4})x(\d{3,4})", size, flags=re.IGNORECASE)
    if not match:
        raise _error("MODEL_PROTOCOL_CONFIG_INVALID", "Seedream 图片尺寸必须是 2K、4K 或有效宽高")
    width, height = int(match.group(1)), int(match.group(2))
    pixels = width * height
    ratio = width / height
    if not 512 <= width <= 4096 or not 512 <= height <= 4096 or not 1 / 3 <= ratio <= 3:
        raise _error("MODEL_PROTOCOL_CONFIG_INVALID", "Seedream 图片宽高或比例超出支持范围")
    if not 1024 * 1024 <= pixels <= 4096 * 4096:
        raise _error("MODEL_PROTOCOL_CONFIG_INVALID", "Seedream 图片像素总量超出支持范围")
    return f"{width}x{height}"


def _language_hint(value: Any) -> str:
    language = str(value or "Chinese").strip().lower()
    if language in _DASHSCOPE_LANGUAGES:
        return _DASHSCOPE_LANGUAGES[language]
    if language in _DASHSCOPE_LANGUAGE_CODES:
        return language
    raise _error("MODEL_PROTOCOL_CONFIG_INVALID", "百炼语音 language_type 不受支持")


def _text_request(model_config: dict, payload: dict) -> tuple[str, dict, str]:
    messages = payload.get("messages")
    if not isinstance(messages, list) or not messages:
        raise _error("MODEL_PROTOCOL_INVALID_REQUEST", "文本模型请求缺少 messages")
    request = copy.deepcopy(payload)
    structured = bool(request.pop("_structured", False))
    if request.get("stream") is True:
        raise _error("MODEL_PROTOCOL_INVALID_REQUEST", "当前文本协议仅支持非流式响应")
    request["model"] = _nonempty_string(model_config.get("model"), field="model", maximum=200)
    if structured and bool(model_config.get("json_mode")):
        request["response_format"] = {"type": "json_object"}
    if model_config.get("max_completion_tokens") is not None:
        request["max_completion_tokens"] = model_config["max_completion_tokens"]
    if model_config.get("enable_thinking") is not None:
        request["enable_thinking"] = model_config["enable_thinking"]
    return "chat/completions", request, "application/json"


def _image_request(protocol: str, model_config: dict, payload: dict) -> tuple[str, dict, str]:
    prompt = _nonempty_string(payload.get("prompt"), field="prompt", maximum=12_000)
    model = _nonempty_string(model_config.get("model"), field="model", maximum=200)
    if protocol == "ark":
        request = {
            "model": model,
            "prompt": prompt,
            "size": _ark_size(model_config.get("size")),
            "sequential_image_generation": "disabled",
            "stream": False,
            "response_format": "url",
            "watermark": bool(model_config.get("watermark", False)),
        }
    else:
        request = copy.deepcopy(payload)
        request.update(
            {
                "model": model,
                "n": 1,
                "size": _nonempty_string(model_config.get("size"), field="size", maximum=30),
            }
        )
    return "images/generations", request, "application/json"


def _speech_text(payload: dict) -> str:
    value = payload.get("input")
    if isinstance(value, dict):
        value = value.get("text")
    if value is None:
        value = payload.get("text")
    return _nonempty_string(value, field="input", maximum=10_000)


def _speech_request(protocol: str, model_config: dict, payload: dict) -> tuple[str, dict, str]:
    text = _speech_text(payload)
    model = _nonempty_string(model_config.get("model"), field="model", maximum=200)
    voice = _nonempty_string(model_config.get("voice"), field="voice", maximum=200)
    instruction = str(model_config.get("instruction") or "").strip()
    if instruction and (protocol != "dashscope" or not model.startswith("qwen-audio-3.0-tts-")):
        raise _error("MODEL_PROTOCOL_CONFIG_INVALID", "当前朗读指导仅支持百炼 Qwen-Audio-3.0-TTS")
    if protocol == "dashscope":
        return (
            "services/audio/tts/SpeechSynthesizer",
            {
                "model": model,
                "input": {
                    "text": text,
                    "voice": voice,
                    "format": "mp3",
                    "language_hints": [_language_hint(model_config.get("language_type"))],
                    **({"instruction": instruction} if instruction else {}),
                },
            },
            "application/json",
        )
    return (
        "audio/speech",
        {"model": model, "input": text, "voice": voice, "response_format": "mp3"},
        "audio/mpeg",
    )


def build_request(kind: str, model_config: dict, payload: dict) -> tuple[str, dict, str]:
    """Build a relative endpoint, JSON body and Accept value without side effects."""

    if not isinstance(model_config, dict) or not isinstance(payload, dict):
        raise _error("MODEL_PROTOCOL_INVALID_REQUEST", "模型配置或请求数据无效")
    normalized_kind = str(kind or "").strip().lower()
    if normalized_kind not in _SUPPORTED:
        raise _error("MODEL_KIND_UNSUPPORTED", "模型能力类型不受支持")
    protocol = _protocol(model_config)
    if protocol not in _SUPPORTED[normalized_kind]:
        raise _error("MODEL_PROTOCOL_UNSUPPORTED", "该模型能力与所选协议不兼容")
    if normalized_kind == "text":
        return _text_request(model_config, payload)
    if normalized_kind == "image":
        return _image_request(protocol, model_config, payload)
    return _speech_request(protocol, model_config, payload)


def _json_object(raw: bytes) -> dict:
    if not isinstance(raw, bytes) or not raw:
        raise _error("MODEL_RESPONSE_INVALID", "模型响应为空")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _error("MODEL_RESPONSE_INVALID", "模型响应不是有效 JSON") from exc
    if not isinstance(value, dict):
        raise _error("MODEL_RESPONSE_INVALID", "模型响应结构无效")
    return value


def _usage(value: dict) -> dict:
    usage = value.get("usage")
    return copy.deepcopy(usage) if isinstance(usage, dict) else {}


def _safe_output_url(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip() or len(value) > 8_192:
        return None
    try:
        parsed = urlsplit(value.strip())
        _ = parsed.port
    except ValueError:
        return None
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        return None
    return value.strip()


def _decode_text(raw: bytes) -> dict:
    value = _json_object(raw)
    try:
        text = value["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise _error("MODEL_RESPONSE_INVALID", "文本模型响应缺少正文") from exc
    if not isinstance(text, str) or not text.strip():
        raise _error("MODEL_RESPONSE_INVALID", "文本模型响应正文为空")
    return {"text": text, "usage": _usage(value)}


def _decode_image(raw: bytes) -> dict:
    value = _json_object(raw)
    data = value.get("data")
    if not isinstance(data, list) or not data or not isinstance(data[0], dict):
        raise _error("MODEL_RESPONSE_INVALID", "图片模型响应缺少图片数据")
    first = data[0]
    result: dict[str, Any] = {"usage": _usage(value)}
    if isinstance(first.get("b64_json"), str) and first["b64_json"].strip():
        result["image_b64"] = first["b64_json"].strip()
    url = _safe_output_url(first.get("url"))
    if url:
        result["image_url"] = url
    if "image_b64" not in result and "image_url" not in result:
        raise _error("MODEL_RESPONSE_INVALID", "图片模型响应未包含可用图片")
    return result


def _audio_format(audio: dict, url: str | None) -> str:
    declared = audio.get("format")
    if isinstance(declared, str) and declared.lower() in _AUDIO_FORMATS:
        return declared.lower()
    if url:
        suffix = urlsplit(url).path.rsplit(".", 1)[-1].lower()
        if suffix in _AUDIO_FORMATS:
            return suffix
    # build_request always asks this non-streaming protocol for MP3.
    return "mp3"


def _decode_dashscope_speech(raw: bytes) -> dict:
    value = _json_object(raw)
    output = value.get("output")
    audio = output.get("audio") if isinstance(output, dict) else None
    if not isinstance(audio, dict):
        raise _error("MODEL_RESPONSE_INVALID", "百炼语音响应缺少 audio")
    if output.get("finish_reason") not in {None, "stop"}:
        raise _error("MODEL_RESPONSE_INVALID", "百炼语音合成尚未完成")
    url = _safe_output_url(audio.get("url"))
    audio_b64 = audio.get("data")
    result: dict[str, Any] = {
        "audio_format": _audio_format(audio, url),
        "usage": _usage(value),
    }
    if url:
        result["audio_url"] = url
    if isinstance(audio_b64, str) and audio_b64.strip():
        result["audio_b64"] = audio_b64.strip()
    if "audio_url" not in result and "audio_b64" not in result:
        raise _error("MODEL_RESPONSE_INVALID", "百炼语音响应未包含可用音频")
    return result


def decode_response(kind: str, protocol: str, raw: bytes) -> dict:
    """Decode one complete, successful HTTP response into the frozen shape."""

    normalized_kind = str(kind or "").strip().lower()
    normalized_protocol = str(protocol or "openai").strip().lower()
    if normalized_kind not in _SUPPORTED or normalized_protocol not in _PROTOCOLS:
        raise _error("MODEL_PROTOCOL_UNSUPPORTED", "模型能力或协议不受支持")
    if normalized_protocol not in _SUPPORTED[normalized_kind]:
        raise _error("MODEL_PROTOCOL_UNSUPPORTED", "该模型能力与所选协议不兼容")
    if normalized_kind == "text":
        return _decode_text(raw)
    if normalized_kind == "image":
        return _decode_image(raw)
    if normalized_protocol == "openai":
        if not isinstance(raw, bytes) or not raw:
            raise _error("MODEL_RESPONSE_INVALID", "语音模型响应为空")
        return {"audio_bytes": raw, "audio_format": "mp3", "usage": {}}
    return _decode_dashscope_speech(raw)
