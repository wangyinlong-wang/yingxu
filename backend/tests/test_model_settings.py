from __future__ import annotations

import copy
import hashlib
import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import manage
from app.integrations.errors import IntegrationError
from app.models import Asset, ModelCall, Setting, uid
from app.providers import invoke_model
from app.services import AppError, digest, enqueue

LOCAL_URL = "http://127.0.0.1:8000/v1"
THIRD_PARTY_URL = "https://api.example.test/v1"


def _legacy_record(engine, mutate=None):
    """Rewrite the stored settings to the pre-source shape, as an old local record would hold."""

    with Session(engine) as session:
        record = session.get(Setting, "app")
        values = copy.deepcopy(record.value)
        for model in values["models"].values():
            model.pop("source", None)
            model.pop("local", None)
        if mutate:
            mutate(values)
        record.value = values
        session.commit()


def _stored(engine):
    with Session(engine) as session:
        return copy.deepcopy(session.get(Setting, "app").value)


def _voice_reference(session, config):
    binary = b"fixture-reference-wav"
    identifier = uid()
    path = config.data_dir / "assets" / f"{identifier}.wav"
    path.write_bytes(binary)
    asset = Asset(
        id=identifier,
        kind="voice_reference",
        filename="fixture.wav",
        relative_path=str(path.relative_to(config.data_dir)),
        mime_type="audio/wav",
        size=len(binary),
        sha256=hashlib.sha256(binary).hexdigest(),
    )
    session.add(asset)
    session.flush()
    return asset


def _local_status(revision="fixture-revision"):
    component = {"installed": True, "model": "fixture-model", "engine": "mlx"}
    return {
        "tts": dict(component),
        "chatterbox": dict(component),
        "avatar": dict(component),
        "transcribe": dict(component),
        "busy": False,
        "revision": revision,
    }


def _enable_native_voice(client, engine, config, monkeypatch):
    monkeypatch.setattr(
        "app.integrations.local_ai.local_status", lambda _config: _local_status()
    )
    with Session(engine) as session:
        reference = _voice_reference(session, config)
        reference_id = reference.id
        session.commit()
    return {"reference_audio_asset_id": reference_id, "reference_text": "测试参考文字"}


def test_legacy_settings_resolve_source_defaults_in_memory_without_mutation(client, engine):
    _legacy_record(engine)
    response = client.get("/api/v1/settings")
    assert response.status_code == 200, response.text
    models = response.json()["models"]
    assert {kind: model["source"] for kind, model in models.items()} == {
        "text": "third_party",
        "image": "third_party",
        "speech": "third_party",
        "video": "third_party",
    }
    for model in models.values():
        assert model["local"]["enabled"] is False
        assert model["local"]["base_url"] == ""
        assert model["local"]["protocol"] == "openai"
    # GET stays read-only: the stored legacy JSON is not rewritten with new fields.
    stored = _stored(engine)
    assert all("source" not in model and "local" not in model for model in stored["models"].values())


def test_legacy_voice_enabled_still_selects_builtin_speech(client, engine):
    _legacy_record(engine, lambda values: values["local_ai"].update(voice_enabled=True))
    models = client.get("/api/v1/settings").json()["models"]
    assert models["speech"]["source"] == "builtin"
    assert models["text"]["source"] == "third_party"
    assert models["image"]["source"] == "third_party"


def test_get_settings_decorates_each_profile_with_credential_metadata_only(client, engine):
    response = client.get("/api/v1/settings")
    assert response.status_code == 200, response.text
    assert "fixture-not-a-real-api-key" not in response.text
    for model in response.json()["models"].values():
        assert model["key_configured"] is True
        assert model["key_source"] == "environment"
        assert model["secure_store_available"] is True
        assert model["key_base_url"] == model["base_url"]
        assert model["local"]["key_configured"] is False
        assert model["local"]["key_source"] == "none"
        assert model["local"]["key_base_url"] == model["local"]["base_url"]


def test_get_settings_stays_readable_when_secure_store_is_unavailable(client, fake_keychain):
    fake_keychain.error = IntegrationError("DEVICE_KEYCHAIN_LOCKED", "locked")
    response = client.get("/api/v1/settings")
    assert response.status_code == 200, response.text
    text = response.json()["models"]["text"]
    assert text["secure_store_available"] is False
    assert text["key_configured"] is True
    assert text["key_source"] == "environment"


def test_source_switch_preserves_both_profiles(client):
    response = client.patch(
        "/api/v1/settings",
        json={
            "models": {
                "text": {
                    "base_url": THIRD_PARTY_URL,
                    "model": "fixture-remote",
                    "local": {"base_url": LOCAL_URL, "model": "fixture-local"},
                }
            }
        },
    )
    assert response.status_code == 200, response.text
    switched = client.patch("/api/v1/settings", json={"models": {"text": {"source": "local"}}})
    assert switched.status_code == 200, switched.text
    text = switched.json()["models"]["text"]
    assert text["source"] == "local"
    assert (text["base_url"], text["model"]) == (THIRD_PARTY_URL, "fixture-remote")
    assert (text["local"]["base_url"], text["local"]["model"]) == (LOCAL_URL, "fixture-local")
    back = client.patch("/api/v1/settings", json={"models": {"text": {"source": "third_party"}}})
    assert back.status_code == 200, back.text
    text = back.json()["models"]["text"]
    assert text["source"] == "third_party"
    assert (text["base_url"], text["model"]) == (THIRD_PARTY_URL, "fixture-remote")
    assert (text["local"]["base_url"], text["local"]["model"]) == (LOCAL_URL, "fixture-local")


def test_contradictory_speech_source_and_voice_flag_are_rejected(client):
    for source, voice_enabled in (("builtin", False), ("local", True), ("third_party", True)):
        response = client.patch(
            "/api/v1/settings",
            json={
                "models": {"speech": {"source": source}},
                "local_ai": {"voice_enabled": voice_enabled},
            },
        )
        assert response.status_code == 422, response.text
        assert response.json()["code"] == "CONTRADICTORY_SPEECH_SOURCE"


def test_legacy_voice_only_patch_synchronizes_speech_source(client, engine, config, monkeypatch):
    reference = _enable_native_voice(client, engine, config, monkeypatch)
    response = client.patch(
        "/api/v1/settings",
        json={"local_ai": {"voice_enabled": True, **reference}},
    )
    assert response.status_code == 200, response.text
    assert response.json()["models"]["speech"]["source"] == "builtin"
    # Turning the native voice off again falls back to the third-party profile.
    response = client.patch("/api/v1/settings", json={"local_ai": {"voice_enabled": False}})
    assert response.status_code == 200, response.text
    assert response.json()["models"]["speech"]["source"] == "third_party"


def test_voice_toggle_off_preserves_explicit_non_builtin_source(client, engine, config, monkeypatch):
    _enable_native_voice(client, engine, config, monkeypatch)
    response = client.patch("/api/v1/settings", json={"models": {"speech": {"source": "local"}}})
    assert response.status_code == 200, response.text
    assert response.json()["local_ai"]["voice_enabled"] is False
    response = client.patch("/api/v1/settings", json={"local_ai": {"voice_enabled": False}})
    assert response.status_code == 200, response.text
    assert response.json()["models"]["speech"]["source"] == "local"


def test_source_only_patch_synchronizes_voice_flag(client, engine, config, monkeypatch):
    reference = _enable_native_voice(client, engine, config, monkeypatch)
    # Selecting builtin without reference material fails the existing native checks,
    # which proves voice_enabled was synchronized by the source-only patch.
    response = client.patch("/api/v1/settings", json={"models": {"speech": {"source": "builtin"}}})
    assert response.status_code == 409
    assert response.json()["code"] == "LOCAL_AI_REFERENCE_AUDIO_REQUIRED"
    response = client.patch(
        "/api/v1/settings",
        json={"models": {"speech": {"source": "builtin"}}, "local_ai": reference},
    )
    assert response.status_code == 200, response.text
    assert response.json()["local_ai"]["voice_enabled"] is True


def test_builtin_source_is_rejected_for_text_and_image(client):
    for kind in ("text", "image"):
        response = client.patch("/api/v1/settings", json={"models": {kind: {"source": "builtin"}}})
        assert response.status_code == 422, response.text
        assert response.json()["code"] == "INVALID_SETTINGS"


@pytest.mark.parametrize(
    "base_url",
    [
        "https://example.org/v1",
        "http://192.168.1.10:8000",
        "http://127.0.0.2:8000",
        "https://metadata.google.internal/v1",
        "http://169.254.169.254/latest",
        "http://127.0.0.1:8000/v1?token=x",
        "http://127.0.0.1:8000/v1#fragment",
        "http://user:secret@127.0.0.1:8000/v1",
    ],
)
def test_local_profile_url_is_loopback_only_without_query_or_credentials(client, base_url):
    response = client.patch(
        "/api/v1/settings", json={"models": {"text": {"local": {"base_url": base_url}}}}
    )
    assert response.status_code == 422, response.text
    assert "secret" not in response.text


@pytest.mark.parametrize(
    "base_url",
    ["http://127.0.0.1:8000/v1", "http://[::1]:9000", "https://localhost:8443/v1"],
)
def test_local_profile_accepts_loopback_http_and_https(client, base_url):
    response = client.patch(
        "/api/v1/settings",
        json={"models": {"text": {"source": "local", "local": {"base_url": base_url}}}},
    )
    assert response.status_code == 200, response.text
    assert response.json()["models"]["text"]["local"]["base_url"] == base_url


def test_remote_third_party_still_requires_https_except_loopback(client):
    response = client.patch(
        "/api/v1/settings", json={"models": {"text": {"base_url": "http://example.org/v1"}}}
    )
    assert response.status_code == 422
    response = client.patch(
        "/api/v1/settings", json={"models": {"text": {"base_url": "http://127.0.0.1:9000/v1"}}}
    )
    assert response.status_code == 200, response.text


def test_third_party_enabled_requires_an_effective_key(client, config, monkeypatch):
    monkeypatch.setattr(config, "text_api_key", SecretStr(""))
    response = client.patch(
        "/api/v1/settings",
        json={
            "models": {
                "text": {"enabled": True, "base_url": THIRD_PARTY_URL, "model": "fixture-remote"}
            }
        },
    )
    assert response.status_code == 409
    assert response.json()["code"] == "API_KEY_REQUIRED"


def test_keychain_key_satisfies_third_party_enablement(client, config, monkeypatch):
    monkeypatch.setattr(config, "text_api_key", SecretStr(""))
    response = client.post(
        "/api/v1/settings/model-keys/text",
        json={
            "source": "third_party",
            "base_url": THIRD_PARTY_URL,
            "action": "set",
            "api_key": "fixture-keychain-key",
        },
    )
    assert response.status_code == 200, response.text
    response = client.patch(
        "/api/v1/settings",
        json={
            "models": {
                "text": {"enabled": True, "base_url": THIRD_PARTY_URL, "model": "fixture-remote"}
            }
        },
    )
    assert response.status_code == 200, response.text


def test_local_enabled_needs_no_key(client, config, monkeypatch):
    for kind in ("text", "image", "speech"):
        monkeypatch.setattr(config, f"{kind}_api_key", SecretStr(""))
    response = client.patch(
        "/api/v1/settings",
        json={
            "models": {
                "text": {
                    "source": "local",
                    "local": {"enabled": True, "base_url": LOCAL_URL, "model": "fixture-local"},
                }
            }
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["models"]["text"]["local"]["enabled"] is True


def test_model_key_endpoint_lifecycle_returns_metadata_only(client, fake_keychain):
    response = client.post(
        "/api/v1/settings/model-keys/text",
        json={
            "source": "third_party",
            "base_url": THIRD_PARTY_URL,
            "action": "set",
            "api_key": "sk-fixture-123",
        },
    )
    assert response.status_code == 200, response.text
    assert response.json() == {
        "key_configured": True,
        "key_source": "keychain",
        "secure_store_available": True,
    }
    assert "sk-fixture-123" not in response.text
    # The saved key decorates the profile whose Base URL matches; other URLs stay on fallback.
    client.patch("/api/v1/settings", json={"models": {"text": {"base_url": THIRD_PARTY_URL}}})
    decorated = client.get("/api/v1/settings").json()["models"]["text"]
    assert decorated["key_source"] == "keychain"
    assert "sk-fixture-123" not in client.get("/api/v1/settings").text
    client.patch("/api/v1/settings", json={"models": {"text": {"base_url": "https://other.example.test/v1"}}})
    assert client.get("/api/v1/settings").json()["models"]["text"]["key_source"] == "environment"
    # Clearing discloses that the environment key remains active for third-party profiles.
    response = client.post(
        "/api/v1/settings/model-keys/text",
        json={"source": "third_party", "base_url": THIRD_PARTY_URL, "action": "clear"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["key_source"] == "environment"
    assert response.json()["key_configured"] is True
    assert fake_keychain.items == {}


def test_model_key_endpoint_local_profile_has_no_environment_fallback(client):
    response = client.post(
        "/api/v1/settings/model-keys/speech",
        json={"source": "local", "base_url": LOCAL_URL, "action": "set", "api_key": "fixture-local"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["key_source"] == "keychain"
    response = client.post(
        "/api/v1/settings/model-keys/speech",
        json={"source": "local", "base_url": LOCAL_URL, "action": "clear"},
    )
    assert response.json() == {
        "key_configured": False,
        "key_source": "none",
        "secure_store_available": True,
    }


def test_model_key_endpoint_validates_target_and_secret(client, fake_keychain):
    assert (
        client.post(
            "/api/v1/settings/model-keys/embedding",
            json={"source": "third_party", "base_url": THIRD_PARTY_URL, "action": "set", "api_key": "x"},
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/api/v1/settings/model-keys/text",
            json={"source": "builtin", "base_url": THIRD_PARTY_URL, "action": "set", "api_key": "x"},
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/api/v1/settings/model-keys/text",
            json={"source": "third_party", "base_url": THIRD_PARTY_URL, "action": "set"},
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/api/v1/settings/model-keys/text",
            json={"source": "local", "base_url": THIRD_PARTY_URL, "action": "set", "api_key": "x"},
        ).status_code
        == 422
    )
    response = client.post(
        "/api/v1/settings/model-keys/text",
        json={
            "source": "third_party",
            "base_url": THIRD_PARTY_URL,
            "action": "set",
            "api_key": "bad key with spaces",
        },
    )
    assert response.status_code == 409
    assert response.json()["code"] == "MODEL_KEY_INVALID"
    assert "bad key with spaces" not in response.text
    assert fake_keychain.items == {}


def test_model_key_endpoint_surfaces_sanitized_store_errors(client, fake_keychain):
    fake_keychain.error = IntegrationError("DEVICE_KEYCHAIN_LOCKED", "locked -25308")
    response = client.post(
        "/api/v1/settings/model-keys/text",
        json={
            "source": "third_party",
            "base_url": THIRD_PARTY_URL,
            "action": "set",
            "api_key": "fixture-key",
        },
    )
    assert response.status_code == 409
    assert response.json()["code"] == "MODEL_KEY_STORE_LOCKED"
    assert "-25308" not in response.text and "fixture-key" not in response.text


def _configured_job(engine, models):
    with Session(engine) as session:
        record = session.get(Setting, "app")
        values = copy.deepcopy(record.value)
        for kind, update in models.items():
            values["models"][kind].update(update)
        record.value = values
        job = enqueue(session, kind="GENERATE_REPORT", payload={})
        job.status = "RUNNING"
        job.lease_owner = "fixture-owner"
        job.lease_until = (datetime.now(UTC) + timedelta(minutes=5)).isoformat()
        session.commit()
        return job.id


def _mock_transport(monkeypatch, handler):
    original = httpx.Client
    monkeypatch.setattr(
        "app.providers.httpx.Client",
        lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs),
    )


def _completion(text="fixture response"):
    return {"choices": [{"message": {"content": text}}], "usage": {"total_tokens": 3}}


def _invoke(job_id, engine, config, scope):
    return invoke_model(
        engine,
        config,
        job_id=job_id,
        owner="fixture-owner",
        kind="text",
        payload={"messages": [{"role": "user", "content": "fixture"}]},
        scope=scope,
    )


def test_local_dispatch_omits_authorization_despite_environment_key(engine, config, monkeypatch):
    job_id = _configured_job(
        engine,
        {
            "text": {
                "source": "local",
                "local": {"enabled": True, "base_url": LOCAL_URL, "model": "fixture-local"},
            }
        },
    )
    requests = []
    _mock_transport(
        monkeypatch,
        lambda request: requests.append(request) or httpx.Response(200, json=_completion()),
    )
    result = _invoke(job_id, engine, config, "local-no-key")
    assert result["text"] == "fixture response"
    assert len(requests) == 1
    assert requests[0].url.path == "/v1/chat/completions"
    assert "authorization" not in requests[0].headers
    with Session(engine) as session:
        record = session.scalar(select(ModelCall))
        assert record.status == "SUCCEEDED"
        assert "fixture-not-a-real-api-key" not in json.dumps(record.response or {})


def test_local_dispatch_uses_saved_local_key_only(engine, config, monkeypatch, fake_keychain):
    job_id = _configured_job(
        engine,
        {
            "text": {
                "source": "local",
                "local": {"enabled": True, "base_url": LOCAL_URL, "model": "fixture-local"},
            }
        },
    )
    from app.model_credentials import set_model_key

    set_model_key(config, "text", "local", LOCAL_URL, "fixture-local-key")
    requests = []
    _mock_transport(
        monkeypatch,
        lambda request: requests.append(request) or httpx.Response(200, json=_completion()),
    )
    _invoke(job_id, engine, config, "local-with-key")
    assert requests[0].headers["authorization"] == "Bearer fixture-local-key"


def test_third_party_dispatch_prefers_url_scoped_keychain_key(engine, config, monkeypatch):
    job_id = _configured_job(
        engine,
        {"text": {"enabled": True, "base_url": THIRD_PARTY_URL, "model": "fixture-remote"}},
    )
    from app.model_credentials import set_model_key

    set_model_key(config, "text", "third_party", THIRD_PARTY_URL, "fixture-keychain-key")
    requests = []
    _mock_transport(
        monkeypatch,
        lambda request: requests.append(request) or httpx.Response(200, json=_completion()),
    )
    _invoke(job_id, engine, config, "third-party-keychain")
    assert requests[0].headers["authorization"] == "Bearer fixture-keychain-key"


def test_local_keychain_failure_creates_no_dispatch_record(engine, config, fake_keychain):
    job_id = _configured_job(
        engine,
        {
            "text": {
                "source": "local",
                "local": {"enabled": True, "base_url": LOCAL_URL, "model": "fixture-local"},
            }
        },
    )
    fake_keychain.error = IntegrationError("DEVICE_KEYCHAIN_LOCKED", "locked")
    with pytest.raises(AppError) as caught:
        _invoke(job_id, engine, config, "local-locked")
    assert caught.value.code == "MODEL_KEY_STORE_LOCKED"
    with Session(engine) as session:
        assert session.scalar(select(ModelCall)) is None


def test_third_party_locked_store_never_falls_back_to_environment_account(
    engine, config, fake_keychain, monkeypatch
):
    # Saved override + locked store + environment key present: dispatch must stop with the
    # store error, never bill the environment account, and leave no STARTED ledger entry.
    from app.model_credentials import set_model_key

    set_model_key(config, "text", "third_party", THIRD_PARTY_URL, "fixture-keychain-key")
    job_id = _configured_job(
        engine, {"text": {"enabled": True, "base_url": THIRD_PARTY_URL, "model": "fixture-remote"}}
    )
    requests = []
    _mock_transport(
        monkeypatch,
        lambda request: requests.append(request) or httpx.Response(200, json=_completion()),
    )
    fake_keychain.error = IntegrationError("DEVICE_KEYCHAIN_LOCKED", "locked -25308")
    with pytest.raises(AppError) as caught:
        _invoke(job_id, engine, config, "third-party-locked")
    assert caught.value.code == "MODEL_KEY_STORE_LOCKED"
    assert "-25308" not in str(caught.value)
    assert requests == []
    with Session(engine) as session:
        assert session.scalar(select(ModelCall)) is None


def test_locked_store_enablement_surfaces_store_error_not_api_key_required(client, fake_keychain):
    fake_keychain.error = IntegrationError("DEVICE_KEYCHAIN_LOCKED", "locked")
    response = client.patch(
        "/api/v1/settings",
        json={
            "models": {
                "text": {"enabled": True, "base_url": THIRD_PARTY_URL, "model": "fixture-remote"}
            }
        },
    )
    assert response.status_code == 409
    assert response.json()["code"] == "MODEL_KEY_STORE_LOCKED"


def test_local_discriminator_keeps_third_party_request_keys_legacy(engine, config, monkeypatch):
    loopback_https = "https://localhost:9000/v1"
    job_id = _configured_job(
        engine, {"text": {"enabled": True, "base_url": loopback_https, "model": "fixture-model"}}
    )
    _mock_transport(monkeypatch, lambda request: httpx.Response(200, json=_completion()))
    _invoke(job_id, engine, config, "same-scope")
    from app.protocols import build_request

    request = build_request(
        "text",
        {"protocol": "openai", "model": "fixture-model"},
        {"messages": [{"role": "user", "content": "fixture"}]},
    )[1]
    legacy_shape = {
        "scope": "same-scope",
        "kind": "text",
        "url": f"{loopback_https}/chat/completions",
        "request": request,
        "output_processing": None,
    }
    legacy_key = digest(legacy_shape)
    with Session(engine) as session:
        third_party_key = session.scalar(select(ModelCall.request_key))
    assert third_party_key == legacy_key
    # Switching the same capability to a local profile on the same URL must not collide
    # with the legacy third-party cache namespace.
    job_id = _configured_job(
        engine,
        {
            "text": {
                "source": "local",
                "base_url": loopback_https,
                "model": "fixture-model",
                "local": {"enabled": True, "base_url": loopback_https, "model": "fixture-model"},
            }
        },
    )
    _invoke(job_id, engine, config, "same-scope")
    with Session(engine) as session:
        keys = [row[0] for row in session.execute(select(ModelCall.request_key)).all()]
    assert set(keys) == {legacy_key, digest({**legacy_shape, "source": "local"})}


def test_restore_disables_local_profiles_and_builtin_voice(client, engine, config):
    with Session(engine) as session:
        record = session.get(Setting, "app")
        values = copy.deepcopy(record.value)
        for kind, model in values["models"].items():
            model["enabled"] = True
            model["source"] = "builtin" if kind == "speech" else "local"
            model["local"] = {"enabled": True, "base_url": LOCAL_URL, "model": "fixture-local"}
        values["local_ai"]["voice_enabled"] = True
        values["publishing"]["auto_publish_after_approval"] = True
        record.value = values
        session.commit()
    snapshot = manage.backup(config)
    engine.dispose()
    manage.restore(config, snapshot)
    with Session(engine) as session:
        values = copy.deepcopy(session.get(Setting, "app").value)
    for model in values["models"].values():
        assert model["enabled"] is False
        assert model["local"]["enabled"] is False
        assert model["source"] != "builtin"
    assert values["local_ai"]["voice_enabled"] is False
    assert values["publishing"]["auto_publish_after_approval"] is False
