from __future__ import annotations

import copy
import hashlib
import io
import json
import os
import sys
import time
import wave
from datetime import UTC, datetime
from pathlib import Path

import pytest
from PIL import Image
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.integrations.errors import CanceledError, IntegrationError
from app.integrations.local_ai import local_status, run_local
from app.models import Asset, Content, Job, JobStep, ModelCall, Setting, uid
from app.services import enqueue
from app.worker import Worker, recover_expired
from app.workflow import TaskContext


def _status(revision="fixture-revision", *, installed=True):
    return {
        "tts": {
            "installed": installed,
            "model": "mlx-community/Qwen3-TTS-12Hz-1.7B-Base-bf16",
            "engine": "mlx",
        },
        "chatterbox": {
            "installed": installed,
            "model": "mlx-community/chatterbox-multilingual-v3",
            "engine": "mlx",
        },
        "avatar": {"installed": installed, "model": "MuseTalk-1.5", "engine": "mps"},
        "transcribe": {"installed": installed, "model": "whisper-tiny", "engine": "cpu"},
        "busy": False,
        "revision": revision if installed else None,
    }


def _write_wav(path: Path, seconds: float = 4.0) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(8000)
        output.writeframes(b"\0\0" * int(8000 * seconds))


def _asset(session, config, kind: str, suffix: str, binary: bytes, *, content_id=None) -> Asset:
    identifier = uid()
    path = config.data_dir / "assets" / f"{identifier}{suffix}"
    path.write_bytes(binary)
    mime = {
        "voice_reference": "audio/wav",
        "portrait": "image/png",
        "image": "image/png",
        "audio": "audio/wav",
    }[kind]
    asset = Asset(
        id=identifier,
        content_id=content_id,
        kind=kind,
        filename=f"fixture{suffix}",
        relative_path=str(path.relative_to(config.data_dir)),
        mime_type=mime,
        size=len(binary),
        sha256=hashlib.sha256(binary).hexdigest(),
    )
    session.add(asset)
    session.flush()
    return asset


def _install_runtime(root: Path) -> None:
    for relative in (
        ".tools/local-ai/models/qwen3-tts/config.json",
        ".tools/local-ai/models/chatterbox/config.json",
        ".tools/local-ai/models/avatar/musetalkV15/unet.pth",
        ".tools/local-ai/models/avatar/whisper/config.json",
    ):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"fixture")
    for relative in (".tools/local-ai/tts/bin/python", ".tools/local-ai/avatar/bin/python"):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.symlink_to(sys.executable)
    manifest = {
        "schema_version": 1,
        "revision": "fixture-revision",
        "components": {
            "tts": {
                "installed": True,
                "model": "mlx-community/Qwen3-TTS-12Hz-1.7B-Base-bf16",
            },
            "chatterbox": {
                "installed": True,
                "model": "mlx-community/chatterbox-multilingual-v3",
            },
            "avatar": {"installed": True, "model": "MuseTalk-1.5"},
            "transcribe": {"installed": True, "model": "whisper-tiny"},
        },
    }
    path = root / ".tools/local-ai/manifest.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest), encoding="utf-8")


def test_local_status_requires_manifest_runtime_and_weight_markers(config, tmp_path, monkeypatch):
    root = tmp_path / "app"
    monkeypatch.setattr("app.integrations.local_ai.ROOT", root)
    lock = config.data_dir / "runs" / "local-ai.lock"
    assert not lock.exists()
    absent = local_status(config)
    assert not lock.exists()
    assert absent["revision"] is None
    assert {absent[name]["reason"] for name in ("tts", "avatar", "transcribe")} == {"manifest_missing"}
    _install_runtime(root)
    ready = local_status(config)
    assert ready == {**_status(), "busy": False}
    (root / ".tools/local-ai/models/avatar/whisper/config.json").unlink()
    missing = local_status(config)
    assert missing["transcribe"] == {
        "installed": False,
        "model": "whisper-tiny",
        "engine": "cpu",
        "reason": "model_missing",
    }
    assert missing["tts"]["installed"] is True


def test_run_local_strips_cloud_secrets_and_reaps_canceled_process(config, tmp_path, monkeypatch):
    root = tmp_path / "app"
    _install_runtime(root)
    scripts = root / "scripts"
    scripts.mkdir()
    runner = scripts / "local_ai_runtime.py"
    runner.write_text(
        """
import json, os, pathlib, sys, time
request = json.loads(sys.stdin.readline())
if os.environ.get('PUBLISHER_TEXT_API_KEY') or os.environ.get('PUBLISHER_TIKHUB_API_KEY'):
    print('LOCAL_AI_RESULT=' + json.dumps({'error': {'code': 'SECRET_LEAK', 'message': 'bad'}}))
    raise SystemExit(0)
output = pathlib.Path(request['output_path'])
if request['text'] == 'cancel-me':
    output.with_suffix('.pid').write_text(str(os.getpid()))
    time.sleep(60)
output.write_bytes(b'fixture-output')
print('ignored diagnostic line')
print('LOCAL_AI_RESULT=' + json.dumps({
    'output_path': str(output), 'duration': 1.25, 'engine': 'mlx',
    'elapsed_seconds': 0.1, 'model': 'mlx-community/Qwen3-TTS-12Hz-1.7B-Base-bf16'}))
""".strip()
        + "\n",
        encoding="utf-8",
    )
    sandbox = tmp_path / "fake-sandbox"
    sandbox.write_text('#!/bin/sh\nshift 2\nexec "$@"\n', encoding="utf-8")
    sandbox.chmod(0o700)
    monkeypatch.setattr("app.integrations.local_ai.ROOT", root)
    monkeypatch.setattr("app.integrations.local_ai._SANDBOX", sandbox)
    monkeypatch.setenv("PUBLISHER_TEXT_API_KEY", "must-not-reach-child")
    monkeypatch.setenv("PUBLISHER_TIKHUB_API_KEY", "must-not-reach-child")
    reference = config.data_dir / "assets" / "reference.wav"
    _write_wav(reference)
    output_dir = config.data_dir / "runs" / "job-a"
    output_dir.mkdir(parents=True)
    output = output_dir / "voice.wav"
    result = run_local(
        config,
        "tts",
        {
            "text": "测试预览",
            "reference_audio_path": str(reference),
            "reference_text": "参考文字",
            "output_path": str(output),
        },
        progress=lambda *_: None,
        canceled=lambda: False,
    )
    assert result["engine"] == "mlx" and output.read_bytes() == b"fixture-output"

    canceled_output = output_dir / "cancel.wav"
    started = time.monotonic()
    with pytest.raises(CanceledError):
        run_local(
            config,
            "tts",
            {
                "text": "cancel-me",
                "reference_audio_path": str(reference),
                "reference_text": "参考文字",
                "output_path": str(canceled_output),
            },
            progress=lambda *_: None,
            canceled=lambda: time.monotonic() - started > 0.3,
        )
    pid = int(canceled_output.with_suffix(".pid").read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_run_local_chatterbox_validates_strength_and_never_passes_reference_text(
    config, tmp_path, monkeypatch
):
    root = tmp_path / "app"
    _install_runtime(root)
    scripts = root / "scripts"
    scripts.mkdir()
    runner = scripts / "local_ai_runtime.py"
    runner.write_text(
        """
import json, pathlib, sys
request = json.loads(sys.stdin.readline())
output = pathlib.Path(request['output_path'])
output.with_suffix('.request.json').write_text(json.dumps(request))
if 'reference_text' in request:
    print('LOCAL_AI_RESULT=' + json.dumps({'error': {'code': 'REFERENCE_TEXT_LEAK', 'message': 'bad'}}))
    raise SystemExit(0)
output.write_bytes(b'fixture-chatterbox-output')
print('LOCAL_AI_RESULT=' + json.dumps({
    'output_path': str(output), 'duration': 1.5, 'engine': 'mlx',
    'elapsed_seconds': 0.2, 'model': 'mlx-community/chatterbox-multilingual-v3'}))
""".strip()
        + "\n",
        encoding="utf-8",
    )
    sandbox = tmp_path / "fake-sandbox"
    sandbox.write_text('#!/bin/sh\nshift 2\nexec "$@"\n', encoding="utf-8")
    sandbox.chmod(0o700)
    monkeypatch.setattr("app.integrations.local_ai.ROOT", root)
    monkeypatch.setattr("app.integrations.local_ai._SANDBOX", sandbox)
    reference = config.data_dir / "assets" / "chatterbox-reference.wav"
    _write_wav(reference)
    output_dir = config.data_dir / "runs" / "chatterbox"
    output_dir.mkdir(parents=True)
    output = output_dir / "voice.wav"

    result = run_local(
        config,
        "chatterbox",
        {
            "text": "带情感的本地声音",
            "reference_audio_path": str(reference),
            "reference_text": "不应传入",
            "emotion_strength": 0.8,
            "output_path": str(output),
        },
        progress=lambda *_: None,
        canceled=lambda: False,
    )
    request = json.loads(output.with_suffix(".request.json").read_text())
    assert result["model"] == "mlx-community/chatterbox-multilingual-v3"
    assert request["operation"] == "chatterbox" and request["emotion_strength"] == 0.8
    assert "reference_text" not in request

    for index, invalid in enumerate((True, "0.5", float("nan"), -0.1, 1.1)):
        with pytest.raises(IntegrationError) as caught:
            run_local(
                config,
                "chatterbox",
                {
                    "text": "无效情感强度",
                    "reference_audio_path": str(reference),
                    "emotion_strength": invalid,
                    "output_path": str(output_dir / f"invalid-{index}.wav"),
                },
                progress=lambda *_: None,
                canceled=lambda: False,
            )
        assert caught.value.code == "LOCAL_AI_INPUT_INVALID"


def test_reference_uploads_and_profile_queue_freeze(client, engine, monkeypatch):
    status = _status()
    monkeypatch.setattr("app.api.local_status", lambda _config: status)
    monkeypatch.setattr("app.integrations.local_ai.local_status", lambda _config: status)
    assert client.get("/api/v1/settings").json()["local_ai"] == {
        "voice_enabled": False,
        "avatar_enabled": False,
        "voice_model": "qwen3",
        "emotion_strength": 0.65,
        "reference_audio_asset_id": None,
        "reference_text": "",
        "portrait_asset_id": None,
    }
    assert client.get("/api/v1/local-ai/status").json() == status
    audio_path = Path(client.app.state.config.data_dir) / "runs" / "upload.wav"
    _write_wav(audio_path)
    voice = client.post(
        "/api/v1/assets",
        data={"kind": "voice_reference"},
        files={"file": ("voice.wav", audio_path.read_bytes(), "audio/wav")},
    )
    assert voice.status_code == 201, voice.text
    short_audio = io.BytesIO()
    short_path = Path(client.app.state.config.data_dir) / "runs" / "short.wav"
    _write_wav(short_path, 2)
    short_audio.write(short_path.read_bytes())
    rejected_audio = client.post(
        "/api/v1/assets",
        data={"kind": "voice_reference"},
        files={"file": ("short.wav", short_audio.getvalue(), "audio/wav")},
    )
    assert rejected_audio.status_code == 422
    assert rejected_audio.json()["code"] == "REFERENCE_AUDIO_DURATION_INVALID"
    portrait_bytes = io.BytesIO()
    Image.new("RGB", (256, 320), "#3957d8").save(portrait_bytes, "PNG")
    portrait = client.post(
        "/api/v1/assets",
        data={"kind": "portrait"},
        files={"file": ("portrait.png", portrait_bytes.getvalue(), "image/png")},
    )
    assert portrait.status_code == 201, portrait.text
    wrong_profile_kind = client.patch(
        "/api/v1/settings",
        json={"local_ai": {"reference_audio_asset_id": portrait.json()["id"]}},
    )
    assert wrong_profile_kind.status_code == 422
    assert wrong_profile_kind.json()["code"] == "ASSET_KIND_MISMATCH"
    small_portrait = io.BytesIO()
    Image.new("RGB", (255, 320), "#3957d8").save(small_portrait, "PNG")
    rejected_portrait = client.post(
        "/api/v1/assets",
        data={"kind": "portrait"},
        files={"file": ("small.png", small_portrait.getvalue(), "image/png")},
    )
    assert rejected_portrait.status_code == 422
    assert rejected_portrait.json()["code"] == "PORTRAIT_TOO_SMALL"
    saved = client.patch(
        "/api/v1/settings",
        json={
            "local_ai": {
                "reference_audio_asset_id": voice.json()["id"],
                "reference_text": "这是参考录音文字。",
                "portrait_asset_id": portrait.json()["id"],
            }
        },
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["local_ai"]["voice_enabled"] is False
    preview = client.post(
        "/api/v1/local-ai/preview",
        json={"kind": "voice", "text": "冻结这句话"},
        headers={"Idempotency-Key": "voice-preview-fixture"},
    )
    assert preview.status_code == 202, preview.text
    replayed = client.post(
        "/api/v1/local-ai/preview",
        json={"kind": "voice", "text": "冻结这句话"},
        headers={"Idempotency-Key": "voice-preview-fixture"},
    )
    assert replayed.json() == preview.json()
    conflict = client.post(
        "/api/v1/local-ai/preview",
        json={"kind": "voice", "text": "另一个请求"},
        headers={"Idempotency-Key": "voice-preview-fixture"},
    )
    assert conflict.status_code == 409
    other_text = client.post("/api/v1/local-ai/preview", json={"kind": "voice", "text": "不同的预览文字"})
    assert other_text.status_code == 202
    avatar_preview = client.post("/api/v1/local-ai/preview", json={"kind": "avatar"})
    assert avatar_preview.status_code == 202, avatar_preview.text
    transcription = client.post("/api/v1/local-ai/transcribe", json={"asset_id": voice.json()["id"]})
    assert transcription.status_code == 202, transcription.text
    with Session(engine) as session:
        job = session.get(Job, preview.json()["job_id"])
        assert job.queue == "content"
        assert job.payload["reference_audio"]["sha256"] == voice.json()["sha256"]
        assert job.payload["reference_text"] == "这是参考录音文字。"
        assert (
            session.get(Job, other_text.json()["job_id"]).payload["input_hash"] != job.payload["input_hash"]
        )
        avatar_job = session.get(Job, avatar_preview.json()["job_id"])
        assert avatar_job.payload["portrait"]["asset_id"] == portrait.json()["id"]
        assert avatar_job.payload["audio"]["asset_id"] == voice.json()["id"]
        transcribe = session.get(Job, transcription.json()["job_id"])
        assert transcribe.payload["source"] == {
            "asset_id": voice.json()["id"],
            "sha256": voice.json()["sha256"],
        }
    changed = client.patch("/api/v1/settings", json={"local_ai": {"reference_text": "之后的新文字"}})
    assert changed.status_code == 200
    with Session(engine) as session:
        assert session.get(Job, preview.json()["job_id"]).payload["reference_text"] == "这是参考录音文字。"
    enabled = client.patch(
        "/api/v1/settings",
        json={"local_ai": {"voice_enabled": True, "avatar_enabled": True}},
    )
    assert enabled.status_code == 200, enabled.text
    assert enabled.json()["models"]["speech"]["enabled"] is False


def test_local_preview_and_transcription_return_durable_results(client, engine, config, monkeypatch):
    status = _status()
    monkeypatch.setattr("app.api.local_status", lambda _config: status)
    monkeypatch.setattr("app.integrations.local_ai.local_status", lambda _config: status)
    source_path = config.data_dir / "runs" / "reference.wav"
    _write_wav(source_path)
    with Session(engine) as session:
        source = _asset(
            session,
            config,
            "voice_reference",
            ".wav",
            source_path.read_bytes(),
        )
        source_id = source.id
        setting = session.get(Setting, "app")
        values = copy.deepcopy(setting.value)
        values["local_ai"] = {
            "voice_enabled": False,
            "avatar_enabled": False,
            "reference_audio_asset_id": source_id,
            "reference_text": "一段参考文字",
            "portrait_asset_id": None,
        }
        setting.value = values
        session.commit()

    def local_runner(_config, operation, payload, **_callbacks):
        if operation == "transcribe":
            return {"transcript": "本地转写结果", "engine": "cpu", "elapsed_seconds": 0.2}
        assert operation == "tts"
        _write_wav(Path(payload["output_path"]), 1)
        return {
            "output_path": payload["output_path"],
            "duration": 1.0,
            "engine": "mlx",
            "elapsed_seconds": 0.3,
            "model": "Qwen3-TTS-1.7B-Base",
        }

    monkeypatch.setattr("app.integrations.local_ai.run_local", local_runner)
    transcribe = client.post("/api/v1/local-ai/transcribe", json={"asset_id": source_id})
    preview = client.post("/api/v1/local-ai/preview", json={"kind": "voice", "text": "预览文字"})
    assert transcribe.status_code == preview.status_code == 202
    worker = Worker(config, engine=engine)
    assert worker.run_once() and worker.run_once()
    transcription = client.get(f"/api/v1/jobs/{transcribe.json()['job_id']}").json()
    assert transcription["result"] == {
        "transcript": "本地转写结果",
        "source_asset_id": source_id,
        "local": True,
    }
    voice = client.get(f"/api/v1/jobs/{preview.json()['job_id']}").json()
    assert voice["status"] == "SUCCEEDED", json.dumps(voice, ensure_ascii=False)
    assert voice["result"]["kind"] == "audio"
    assert voice["result"]["local"] is True and voice["result"]["engine"] == "mlx"
    with Session(engine) as session:
        assert session.get(Asset, voice["result"]["asset_id"]).kind == "audio"
        assert session.scalar(select(func.count(ModelCall.id))) == 0


def test_chatterbox_preview_override_is_frozen_cached_and_does_not_change_default(
    client, engine, config, monkeypatch
):
    status = _status()
    monkeypatch.setattr("app.api.local_status", lambda _config: status)
    monkeypatch.setattr("app.integrations.local_ai.local_status", lambda _config: status)
    reference_path = config.data_dir / "runs" / "chatterbox-preview-reference.wav"
    _write_wav(reference_path)
    with Session(engine) as session:
        reference = _asset(
            session,
            config,
            "voice_reference",
            ".wav",
            reference_path.read_bytes(),
        )
        setting = session.get(Setting, "app")
        values = copy.deepcopy(setting.value)
        values["local_ai"].update(
            reference_audio_asset_id=reference.id,
            reference_text="Qwen 才需要的参考文字",
        )
        setting.value = values
        session.commit()

    calls = []

    def local_runner(_config, operation, payload, **_callbacks):
        assert operation == "chatterbox"
        assert "reference_text" not in payload
        calls.append(payload["emotion_strength"])
        _write_wav(Path(payload["output_path"]), 1)
        return {
            "output_path": payload["output_path"],
            "duration": 1.0,
            "engine": "mlx",
            "elapsed_seconds": 0.3,
            "model": "mlx-community/chatterbox-multilingual-v3",
        }

    monkeypatch.setattr("app.integrations.local_ai.run_local", local_runner)
    submitted = []
    for strength in (0.9, 0.4, 0.4):
        response = client.post(
            "/api/v1/local-ai/preview",
            json={
                "kind": "voice",
                "text": "比较同一句话的情感",
                "voice_model": "chatterbox",
                "emotion_strength": strength,
            },
        )
        assert response.status_code == 202, response.text
        submitted.append(response.json()["job_id"])
        with Session(engine) as session:
            payload = session.get(Job, submitted[-1]).payload
            assert payload["voice_model"] == "chatterbox"
            assert payload["emotion_strength"] == strength
            assert "reference_text" not in payload
        assert Worker(config, engine=engine).run_once()

    assert calls == [0.9, 0.4]
    results = [client.get(f"/api/v1/jobs/{job_id}").json()["result"] for job_id in submitted]
    assert [result["voice_model"] for result in results] == [
        "chatterbox",
        "chatterbox",
        "chatterbox",
    ]
    assert [result["emotion_strength"] for result in results] == [0.9, 0.4, 0.4]
    with Session(engine) as session:
        hashes = [session.get(Asset, result["asset_id"]).input_hash for result in results]
        current = session.get(Setting, "app").value["local_ai"]
    assert hashes[0] != hashes[1] == hashes[2]
    assert current.get("voice_model", "qwen3") == "qwen3"
    assert current.get("emotion_strength", 0.65) == 0.65

    invalid = client.post(
        "/api/v1/local-ai/preview",
        json={"kind": "avatar", "voice_model": "chatterbox"},
    )
    assert invalid.status_code == 422


def test_recovery_accepts_only_checkpointed_local_result(engine, config):
    with Session(engine) as session:
        job = enqueue(
            session,
            kind="LOCAL_AI_TRANSCRIBE",
            payload={"fixture": True},
        )
        job.status = "RUNNING"
        job.lease_owner = "expired-worker"
        job.lease_until = "2020-01-01T00:00:00+00:00"
        job.result = {"transcript": "已落库", "source_asset_id": "fixture", "local": True}
        session.commit()
        job_id = job.id
    with Session(engine) as session:
        assert recover_expired(session, "content", datetime.now(UTC)) == 1
        session.commit()
        assert session.get(Job, job_id).status == "SUCCEEDED"


def test_worker_requeues_pre_inference_local_lock_contention(engine, config, monkeypatch):
    with Session(engine) as session:
        job = enqueue(session, kind="LOCAL_AI_PREVIEW", payload={"fixture": True})
        session.commit()
        job_id = job.id

    def busy(ctx: TaskContext, _kind: str):
        with ctx.step("运行本地模型", 20):
            raise IntegrationError("LOCAL_AI_BUSY", "本地模型忙")

    monkeypatch.setattr("app.worker.dispatch", busy)
    assert Worker(config, engine=engine).run_once()
    with Session(engine) as session:
        job = session.get(Job, job_id)
        assert (job.status, job.current_step, job.error_code) == (
            "QUEUED",
            "等待本地模型空闲",
            None,
        )
        assert session.scalar(select(JobStep)).status == "QUEUED"


def test_local_profile_enablement_fails_closed_when_runtime_is_missing(client, engine, config, monkeypatch):
    with Session(engine) as session:
        reference_path = config.data_dir / "assets" / "reference.wav"
        _write_wav(reference_path)
        reference = _asset(
            session,
            config,
            "voice_reference",
            ".wav",
            reference_path.read_bytes(),
        )
        reference_id = reference.id
        session.commit()
    monkeypatch.setattr("app.integrations.local_ai.local_status", lambda _config: _status(installed=False))
    response = client.patch(
        "/api/v1/settings",
        json={
            "local_ai": {
                "voice_enabled": True,
                "reference_audio_asset_id": reference_id,
                "reference_text": "参考文字",
            }
        },
    )
    assert response.status_code == 409
    assert response.json()["code"] == "LOCAL_AI_TTS_NOT_INSTALLED"


def test_chatterbox_profile_uses_audio_without_transcript_and_strength_changes_cache(
    client, engine, config, content_id, monkeypatch
):
    status = _status("chatterbox-revision")
    monkeypatch.setattr("app.integrations.local_ai.local_status", lambda _config: status)
    calls = []

    def local_runner(_config, operation, payload, **_callbacks):
        assert operation == "chatterbox" and "reference_text" not in payload
        calls.append(payload["emotion_strength"])
        _write_wav(Path(payload["output_path"]), 1)
        return {
            "output_path": payload["output_path"],
            "duration": 1.0,
            "engine": "mlx",
            "elapsed_seconds": 0.1,
            "model": "mlx-community/chatterbox-multilingual-v3",
        }

    monkeypatch.setattr("app.integrations.local_ai.run_local", local_runner)
    reference_path = config.data_dir / "runs" / "chatterbox-profile-reference.wav"
    _write_wav(reference_path)
    with Session(engine) as session:
        content = session.get(Content, content_id)
        image = _asset(session, config, "image", ".png", b"image", content_id=content.id)
        reference = _asset(
            session,
            config,
            "voice_reference",
            ".wav",
            reference_path.read_bytes(),
        )
        content.scenes = [
            {
                "id": "scene-chatterbox",
                "order": 0,
                "narration": "这是一句有情感的本地旁白。",
                "caption": "情感旁白",
                "highlight_terms": [],
                "image_prompt": "fixture",
                "image_asset_id": image.id,
                "audio_asset_ids": [],
            }
        ]
        reference_id = reference.id
        session.commit()

    enabled = client.patch(
        "/api/v1/settings",
        json={
            "local_ai": {
                "voice_enabled": True,
                "voice_model": "chatterbox",
                "emotion_strength": 0.75,
                "reference_audio_asset_id": reference_id,
                "reference_text": "",
            }
        },
    )
    assert enabled.status_code == 200, enabled.text

    hashes = []
    for strength in (0.75, 0.25):
        if strength != 0.75:
            changed = client.patch(
                "/api/v1/settings",
                json={"local_ai": {"emotion_strength": strength}},
            )
            assert changed.status_code == 200, changed.text
            with Session(engine) as session:
                content = session.get(Content, content_id)
                scenes = copy.deepcopy(content.scenes)
                scenes[0]["audio_asset_ids"] = []
                content.scenes = scenes
                session.commit()
        submitted = client.post(f"/api/v1/contents/{content_id}/generate", json={"stage": "assets"})
        assert submitted.status_code == 202, submitted.text
        with Session(engine) as session:
            profile = session.get(Job, submitted.json()["job_id"]).payload["local_ai"]
            assert profile["voice_model"] == "chatterbox"
            assert "reference_text" not in profile
            assert profile["tts"]["model"] == "mlx-community/chatterbox-multilingual-v3"
        assert Worker(config, engine=engine).run_once()
        with Session(engine) as session:
            content = session.get(Content, content_id)
            hashes.append(session.get(Asset, content.scenes[0]["audio_asset_ids"][0]).input_hash)
    assert calls == [0.75, 0.25]
    assert len(set(hashes)) == 2

    qwen_without_text = client.patch(
        "/api/v1/settings",
        json={"local_ai": {"voice_model": "qwen3"}},
    )
    assert qwen_without_text.status_code == 409
    assert qwen_without_text.json()["code"] == "LOCAL_AI_REFERENCE_TEXT_REQUIRED"


def test_local_voice_uses_no_cloud_ledger_and_cache_changes_with_profile(
    client, engine, config, content_id, monkeypatch
):
    revision = {"value": "rev-a"}
    monkeypatch.setattr("app.integrations.local_ai.local_status", lambda _config: _status(revision["value"]))
    calls = []

    def local_runner(_config, operation, payload, **_callbacks):
        assert operation == "tts" and "instruction" not in payload
        calls.append((payload["reference_text"], revision["value"]))
        _write_wav(Path(payload["output_path"]), 1)
        return {
            "output_path": payload["output_path"],
            "duration": 1.0,
            "engine": "mlx",
            "elapsed_seconds": 0.1,
            "model": "mlx-community/Qwen3-TTS-12Hz-1.7B-Base-bf16",
        }

    monkeypatch.setattr("app.integrations.local_ai.run_local", local_runner)
    with Session(engine) as session:
        content = session.get(Content, content_id)
        image = _asset(session, config, "image", ".png", b"fixture-image", content_id=content.id)
        ref_a_path = config.data_dir / "runs" / "ref-a.wav"
        ref_b_path = config.data_dir / "runs" / "ref-b.wav"
        _write_wav(ref_a_path)
        _write_wav(ref_b_path, 5)
        ref_a = _asset(session, config, "voice_reference", ".wav", ref_a_path.read_bytes())
        ref_b = _asset(session, config, "voice_reference", ".wav", ref_b_path.read_bytes())
        content.scenes = [
            {
                "id": "scene-local",
                "order": 0,
                "narration": "完整镜头本地配音。",
                "caption": "本地配音",
                "highlight_terms": ["本地"],
                "image_prompt": "fixture",
                "image_asset_id": image.id,
                "audio_asset_ids": [],
            }
        ]
        setting = session.get(Setting, "app")
        values = copy.deepcopy(setting.value)
        values["local_ai"] = {
            "voice_enabled": True,
            "avatar_enabled": False,
            "reference_audio_asset_id": ref_a.id,
            "reference_text": "参考甲",
            "portrait_asset_id": None,
        }
        setting.value = values
        ref_a_id, ref_b_id = ref_a.id, ref_b.id
        session.commit()

    hashes = []
    for reference_id, reference_text, next_revision in (
        (ref_a_id, "参考甲", "rev-a"),
        (ref_b_id, "参考乙", "rev-a"),
        (ref_b_id, "参考乙", "rev-b"),
    ):
        revision["value"] = next_revision
        with Session(engine) as session:
            content = session.get(Content, content_id)
            scenes = copy.deepcopy(content.scenes)
            scenes[0]["audio_asset_ids"] = []
            content.scenes = scenes
            setting = session.get(Setting, "app")
            values = copy.deepcopy(setting.value)
            values["local_ai"].update(
                reference_audio_asset_id=reference_id,
                reference_text=reference_text,
            )
            setting.value = values
            session.commit()
        submitted = client.post(f"/api/v1/contents/{content_id}/generate", json={"stage": "assets"})
        assert submitted.status_code == 202, submitted.text
        assert Worker(config, engine=engine).run_once()
        job = client.get(f"/api/v1/jobs/{submitted.json()['job_id']}").json()
        assert job["status"] == "SUCCEEDED", job
        with Session(engine) as session:
            content = session.get(Content, content_id)
            audio = session.get(Asset, content.scenes[0]["audio_asset_ids"][0])
            hashes.append(audio.input_hash)
    assert len(set(hashes)) == 3
    assert len(calls) == 3
    with Session(engine) as session:
        assert session.scalar(select(func.count(ModelCall.id))) == 0


def test_explicit_local_voice_replaces_existing_audio_without_cloud_calls(
    client, engine, config, content_id, monkeypatch
):
    from app.integrations import media

    monkeypatch.setattr("app.integrations.local_ai.local_status", lambda _config: _status("rev-a"))
    local_calls = []
    compose_calls = []

    def local_runner(_config, operation, payload, **_callbacks):
        assert operation == "tts" and "instruction" not in payload
        local_calls.append(payload["reference_text"])
        _write_wav(Path(payload["output_path"]), 1)
        return {
            "output_path": payload["output_path"],
            "duration": 1.0,
            "engine": "mlx",
            "elapsed_seconds": 0.2,
            "model": "mlx-community/Qwen3-TTS-12Hz-1.7B-Base-bf16",
        }

    def compose_video(*, output_dir, **_kwargs):
        compose_calls.append(True)
        output_dir.mkdir(parents=True, exist_ok=True)
        values = {
            "video_path": output_dir / "base.mp4",
            "cover_path": output_dir / "cover.png",
            "subtitle_path": output_dir / "subtitles.srt",
        }
        for path in values.values():
            path.write_bytes(b"local-voice-render")
        return {key: str(path) for key, path in values.items()}

    monkeypatch.setattr("app.integrations.local_ai.run_local", local_runner)
    monkeypatch.setattr(media, "compose_video", compose_video)
    monkeypatch.setattr(
        "app.workflow.invoke_model",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("cloud model must not run")),
    )
    reference_path = config.data_dir / "runs" / "reference-local-action.wav"
    old_audio_path = config.data_dir / "runs" / "old-cloud-audio.wav"
    _write_wav(reference_path)
    _write_wav(old_audio_path, 1)
    with Session(engine) as session:
        content = session.get(Content, content_id)
        image = _asset(session, config, "image", ".png", b"image", content_id=content.id)
        old_audio = _asset(
            session,
            config,
            "audio",
            ".wav",
            old_audio_path.read_bytes(),
            content_id=content.id,
        )
        reference = _asset(
            session,
            config,
            "voice_reference",
            ".wav",
            reference_path.read_bytes(),
        )
        old_audio_id = old_audio.id
        old_audio_file = config.data_dir / old_audio.relative_path
        content.scenes = [
            {
                "id": "scene-explicit-local",
                "order": 0,
                "narration": "明确替换为本人声音。",
                "caption": "本人声音",
                "highlight_terms": [],
                "image_prompt": "fixture",
                "image_asset_id": image.id,
                "audio_asset_ids": [old_audio_id],
            }
        ]
        setting = session.get(Setting, "app")
        values = copy.deepcopy(setting.value)
        values["local_ai"] = {
            "voice_enabled": False,
            "avatar_enabled": False,
            "reference_audio_asset_id": reference.id,
            "reference_text": "当前参考文字",
            "portrait_asset_id": None,
        }
        setting.value = values
        session.commit()
    first = client.post(f"/api/v1/contents/{content_id}/generate", json={"stage": "local_voice"})
    assert first.status_code == 202, first.text
    queued = client.get(f"/api/v1/contents/{content_id}").json()
    assert queued["version"] == 2 and queued["scenes"][0]["audio_asset_ids"] == []
    with Session(engine) as session:
        assert session.get(Asset, old_audio_id) is not None and old_audio_file.is_file()
        assert session.get(Job, first.json()["job_id"]).payload["local_ai"]["voice_enabled"] is True
    assert Worker(config, engine=engine).run_once()
    first_result = client.get(f"/api/v1/contents/{content_id}").json()
    assert first_result["status"] == "REVIEW_REQUIRED"
    local_audio_id = first_result["scenes"][0]["audio_asset_ids"][0]
    assert local_audio_id != old_audio_id

    second = client.post(f"/api/v1/contents/{content_id}/generate", json={"stage": "local_voice"})
    assert second.status_code == 202, second.text
    assert Worker(config, engine=engine).run_once()
    second_result = client.get(f"/api/v1/contents/{content_id}").json()
    assert second_result["version"] == 3
    assert second_result["scenes"][0]["audio_asset_ids"] == [local_audio_id]
    assert local_calls == ["当前参考文字"]
    assert compose_calls == [True]
    with Session(engine) as session:
        assert session.scalar(select(func.count(ModelCall.id))) == 0


def test_regular_asset_generation_preserves_selected_audio(client, engine, config, content_id, monkeypatch):
    monkeypatch.setattr("app.integrations.local_ai.local_status", lambda _config: _status("rev-a"))
    monkeypatch.setattr(
        "app.integrations.local_ai.run_local",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("local voice must not replace audio")),
    )
    reference_path = config.data_dir / "runs" / "reference-preserve.wav"
    selected_path = config.data_dir / "runs" / "selected.wav"
    _write_wav(reference_path)
    _write_wav(selected_path, 1)
    with Session(engine) as session:
        content = session.get(Content, content_id)
        image = _asset(session, config, "image", ".png", b"image", content_id=content.id)
        selected = _asset(session, config, "audio", ".wav", selected_path.read_bytes(), content_id=content.id)
        reference = _asset(session, config, "voice_reference", ".wav", reference_path.read_bytes())
        content.scenes = [
            {
                "id": "scene-preserve",
                "order": 0,
                "narration": "保留人工选择的声音。",
                "caption": "保留声音",
                "highlight_terms": [],
                "image_prompt": "fixture",
                "image_asset_id": image.id,
                "audio_asset_ids": [selected.id],
            }
        ]
        setting = session.get(Setting, "app")
        values = copy.deepcopy(setting.value)
        values["local_ai"] = {
            "voice_enabled": True,
            "avatar_enabled": False,
            "reference_audio_asset_id": reference.id,
            "reference_text": "当前参考文字",
            "portrait_asset_id": None,
        }
        setting.value = values
        selected_id = selected.id
        session.commit()
    submitted = client.post(f"/api/v1/contents/{content_id}/generate", json={"stage": "assets"})
    assert submitted.status_code == 202, submitted.text
    assert Worker(config, engine=engine).run_once()
    result = client.get(f"/api/v1/contents/{content_id}").json()
    assert result["scenes"][0]["audio_asset_ids"] == [selected_id]


def test_avatar_failure_never_registers_base_video_as_final(engine, config, content_id, monkeypatch):
    from app.integrations import media

    monkeypatch.setattr("app.integrations.local_ai.local_status", lambda _config: _status("rev-a"))

    def compose_video(*, output_dir, **_kwargs):
        output_dir.mkdir(parents=True, exist_ok=True)
        values = {
            "video_path": output_dir / "base.mp4",
            "cover_path": output_dir / "cover.png",
            "subtitle_path": output_dir / "subtitles.srt",
        }
        for path in values.values():
            path.write_bytes(b"fixture-output")
        return {key: str(path) for key, path in values.items()}

    def local_runner(_config, operation, payload, **_callbacks):
        assert operation == "avatar"
        Path(payload["output_path"]).write_bytes(b"fixture-avatar")
        return {
            "output_path": payload["output_path"],
            "duration": 4.0,
            "engine": "mps",
            "elapsed_seconds": 0.1,
            "model": "MuseTalk-1.5",
        }

    monkeypatch.setattr(media, "compose_video", compose_video)
    monkeypatch.setattr(
        media,
        "probe_media",
        lambda _path: {"duration": 4.0, "has_video": True, "has_audio": True},
    )
    monkeypatch.setattr(
        media,
        "overlay_avatar",
        lambda **_kwargs: (_ for _ in ()).throw(IntegrationError("OVERLAY_FAILED", "叠加失败")),
        raising=False,
    )
    monkeypatch.setattr("app.integrations.local_ai.run_local", local_runner)
    with Session(engine) as session:
        content = session.get(Content, content_id)
        image = _asset(session, config, "image", ".png", b"image", content_id=content.id)
        audio_path = config.data_dir / "runs" / "scene.wav"
        _write_wav(audio_path)
        audio = _asset(session, config, "audio", ".wav", audio_path.read_bytes(), content_id=content.id)
        portrait = _asset(session, config, "portrait", ".png", b"portrait")
        content.scenes = [
            {
                "id": "scene-avatar",
                "order": 0,
                "narration": "数字人镜头。",
                "caption": "数字人",
                "highlight_terms": [],
                "image_prompt": "fixture",
                "image_asset_id": image.id,
                "audio_asset_ids": [audio.id],
            }
        ]
        content.status = "GENERATING"
        job = enqueue(
            session,
            kind="GENERATE_CONTENT",
            content_id=content.id,
            payload={
                "content_id": content.id,
                "version": content.version,
                "stage": "render",
                "local_ai": {
                    "voice_enabled": False,
                    "avatar_enabled": True,
                    "reference_audio": None,
                    "reference_text": "",
                    "portrait": {"asset_id": portrait.id, "sha256": portrait.sha256},
                    "revision": "rev-a",
                    "tts": None,
                    "avatar": {"model": "MuseTalk-1.5", "engine": "mps"},
                },
            },
        )
        session.commit()
        job_id = job.id
    assert Worker(config, engine=engine).run_once()
    with Session(engine) as session:
        content = session.get(Content, content_id)
        assert content.video_asset_id is None and content.media_version is None
        assert session.get(Job, job_id).status == "FAILED"
        assert session.scalar(select(func.count(Asset.id)).where(Asset.kind == "avatar")) == 1
        assert session.scalar(select(func.count(Asset.id)).where(Asset.kind == "video")) == 0

    def overlay_success(*, output_path, **_kwargs):
        output_path.write_bytes(b"final-with-avatar")
        return {"video_path": str(output_path), "duration": 4.0}

    monkeypatch.setattr(media, "overlay_avatar", overlay_success)
    with Session(engine) as session:
        job = session.get(Job, job_id)
        job.status, job.lease_owner, job.lease_until = "QUEUED", None, None
        session.get(Content, content_id).status = "GENERATING"
        session.commit()
    assert Worker(config, engine=engine).run_once()
    with Session(engine) as session:
        content = session.get(Content, content_id)
        job = session.get(Job, job_id)
        video = session.get(Asset, content.video_asset_id)
        assert job.status == "SUCCEEDED" and content.media_version == content.version
        assert (config.data_dir / video.relative_path).read_bytes() == b"final-with-avatar"
        assert session.scalar(select(func.count(Asset.id)).where(Asset.kind == "avatar")) == 1
