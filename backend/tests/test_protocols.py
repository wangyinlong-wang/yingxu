from __future__ import annotations

import copy
import json

import pytest

from app.protocols import build_request, decode_response
from app.services import AppError


def test_openai_text_builds_structured_non_streaming_request_without_mutation():
    config = {"model": "qwen3.7-plus", "json_mode": True}
    payload = {"_structured": True, "messages": [{"role": "user", "content": "测试"}]}
    original = copy.deepcopy(payload)

    endpoint, request, accept = build_request("text", config, payload)

    assert endpoint == "chat/completions"
    assert accept == "application/json"
    assert request == {
        "model": "qwen3.7-plus",
        "messages": [{"role": "user", "content": "测试"}],
        "response_format": {"type": "json_object"},
    }
    assert "enable_thinking" not in request
    assert payload == original


def test_text_rejects_non_openai_protocol():
    with pytest.raises(AppError) as caught:
        build_request(
            "text",
            {"protocol": "dashscope", "model": "qwen3.7-plus"},
            {"messages": [{"role": "user", "content": "测试"}]},
        )
    assert caught.value.code == "MODEL_PROTOCOL_UNSUPPORTED"


@pytest.mark.parametrize("size", ["2K", "4k", "1440x2560", "1024x1536"])
def test_ark_image_uses_single_image_url_response_without_corner_watermark_by_default(size):
    endpoint, request, accept = build_request(
        "image",
        {"protocol": "ark", "model": "doubao-seedream-5-0-260128", "size": size},
        {"prompt": "无文字竖版科技场景"},
    )

    assert endpoint == "images/generations"
    assert accept == "application/json"
    assert request == {
        "model": "doubao-seedream-5-0-260128",
        "prompt": "无文字竖版科技场景",
        "size": "4K" if size == "4k" else size,
        "sequential_image_generation": "disabled",
        "stream": False,
        "response_format": "url",
        "watermark": False,
    }
    assert "n" not in request


def test_ark_watermark_can_be_explicitly_enabled():
    _, request, _ = build_request(
        "image",
        {"protocol": "ark", "model": "doubao-seedream-5-0-260128", "size": "2K", "watermark": True},
        {"prompt": "科技场景"},
    )
    assert request["watermark"] is True


def test_qwen_audio_instruction_does_not_become_spoken_text():
    guidance = "像给朋友讲科技新闻，自然、有起伏。"
    _, request, _ = build_request(
        "speech",
        {
            "protocol": "dashscope",
            "model": "qwen-audio-3.0-tts-plus",
            "voice": "my-voice-id",
            "instruction": guidance,
        },
        {"input": "IBM发布新模型。"},
    )
    assert request["input"]["instruction"] == guidance
    assert request["input"]["text"] == "IBM发布新模型。"
    with pytest.raises(AppError) as error:
        build_request(
            "speech", {"model": "other-tts", "voice": "alloy", "instruction": guidance}, {"input": "测试"}
        )
    assert error.value.code == "MODEL_PROTOCOL_CONFIG_INVALID"


def test_ark_image_rejects_unsupported_size():
    with pytest.raises(AppError) as caught:
        build_request(
            "image",
            {"protocol": "ark", "model": "doubao-seedream-5-0-260128", "size": "200x300"},
            {"prompt": "测试"},
        )
    assert caught.value.code == "MODEL_PROTOCOL_CONFIG_INVALID"


def test_openai_image_keeps_standard_shape():
    assert build_request(
        "image",
        {"model": "image-model", "size": "1024x1536"},
        {"prompt": "测试"},
    ) == (
        "images/generations",
        {"model": "image-model", "prompt": "测试", "n": 1, "size": "1024x1536"},
        "application/json",
    )


def test_openai_speech_keeps_binary_response_contract():
    assert build_request(
        "speech",
        {"model": "tts-model", "voice": "alloy"},
        {"input": "一段中文配音"},
    ) == (
        "audio/speech",
        {"model": "tts-model", "input": "一段中文配音", "voice": "alloy", "response_format": "mp3"},
        "audio/mpeg",
    )


def test_dashscope_qwen_audio_uses_speech_synthesizer_and_language_hint():
    endpoint, request, accept = build_request(
        "speech",
        {
            "protocol": "dashscope",
            "model": "qwen-audio-3.0-tts-plus",
            "voice": "longanlingxin",
            "language_type": "Chinese",
        },
        {"input": "一段中文配音"},
    )

    assert endpoint == "services/audio/tts/SpeechSynthesizer"
    assert accept == "application/json"
    assert request == {
        "model": "qwen-audio-3.0-tts-plus",
        "input": {
            "text": "一段中文配音",
            "voice": "longanlingxin",
            "format": "mp3",
            "language_hints": ["zh"],
        },
    }


def test_decode_openai_text_with_usage():
    raw = json.dumps({"choices": [{"message": {"content": "结果"}}], "usage": {"total_tokens": 12}}).encode()
    assert decode_response("text", "openai", raw) == {"text": "结果", "usage": {"total_tokens": 12}}


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        (
            {"data": [{"url": "https://images.example.test/result.png"}], "usage": {"generated_images": 1}},
            {"image_url": "https://images.example.test/result.png", "usage": {"generated_images": 1}},
        ),
        ({"data": [{"b64_json": "aW1hZ2U="}]}, {"image_b64": "aW1hZ2U=", "usage": {}}),
    ],
)
def test_decode_image_url_or_base64(body, expected):
    assert decode_response("image", "ark", json.dumps(body).encode()) == expected


def test_decode_openai_speech_returns_opaque_bytes():
    raw = b"ID3 deterministic fixture"
    assert decode_response("speech", "openai", raw) == {
        "audio_bytes": raw,
        "audio_format": "mp3",
        "usage": {},
    }


def test_decode_dashscope_speech_url_and_usage():
    raw = json.dumps(
        {
            "output": {
                "finish_reason": "stop",
                "audio": {"url": "https://audio.example.test/result.wav"},
            },
            "usage": {"characters": 8},
        }
    ).encode()
    assert decode_response("speech", "dashscope", raw) == {
        "audio_url": "https://audio.example.test/result.wav",
        "audio_format": "wav",
        "usage": {"characters": 8},
    }


def test_decode_dashscope_speech_base64_defaults_to_requested_mp3():
    raw = json.dumps({"output": {"finish_reason": "stop", "audio": {"data": "YXVkaW8="}}}).encode()
    assert decode_response("speech", "dashscope", raw) == {
        "audio_b64": "YXVkaW8=",
        "audio_format": "mp3",
        "usage": {},
    }


@pytest.mark.parametrize(
    ("kind", "protocol", "raw"),
    [
        ("text", "openai", b"{}"),
        ("image", "ark", b'{"data": []}'),
        ("speech", "dashscope", b'{"output": {"audio": {}}}'),
        ("text", "openai", b"not-json"),
    ],
)
def test_decode_rejects_incomplete_or_invalid_responses(kind, protocol, raw):
    with pytest.raises(AppError) as caught:
        decode_response(kind, protocol, raw)
    assert caught.value.code == "MODEL_RESPONSE_INVALID"
