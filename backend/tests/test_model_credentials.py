from __future__ import annotations

import hashlib

import pytest

from app import model_credentials
from app.integrations.errors import IntegrationError
from app.services import AppError

THIRD_PARTY_URL = "https://api.example.test/v1"
LOCAL_URL = "http://127.0.0.1:8000/v1"


def test_namespace_is_scoped_by_data_dir_capability_source_and_url(config, fake_keychain):
    model_credentials.set_model_key(config, "text", "third_party", THIRD_PARTY_URL, "fixture-key-1")
    (service, account), secret = next(iter(fake_keychain.items.items()))
    assert secret == "fixture-key-1"
    expected_scope = hashlib.sha256(str(config.data_dir.resolve()).encode("utf-8")).hexdigest()[:24]
    assert service == f"com.yingxu.publisher.model-key.v1.{expected_scope}"
    expected_url = hashlib.sha256(THIRD_PARTY_URL.encode("utf-8")).hexdigest()[:24]
    assert account == f"text:third_party:{expected_url}"
    assert "fixture-key-1" not in service + account


def test_set_get_delete_roundtrip_and_url_binding(config, fake_keychain):
    model_credentials.set_model_key(config, "image", "local", LOCAL_URL, "fixture-local-key")
    assert model_credentials.get_model_key(config, "image", "local", LOCAL_URL) == "fixture-local-key"
    # The same capability/source with a different URL is a different credential slot.
    assert model_credentials.get_model_key(config, "image", "local", "http://127.0.0.1:9000/v1") is None
    assert model_credentials.get_model_key(config, "image", "third_party", LOCAL_URL) is None
    assert model_credentials.get_model_key(config, "text", "local", LOCAL_URL) is None
    status = model_credentials.model_key_status(config, "image", "local", LOCAL_URL)
    assert status == {
        "key_configured": True,
        "key_source": "keychain",
        "secure_store_available": True,
    }
    assert model_credentials.delete_model_key(config, "image", "local", LOCAL_URL) is True
    assert model_credentials.delete_model_key(config, "image", "local", LOCAL_URL) is False
    assert model_credentials.get_model_key(config, "image", "local", LOCAL_URL) is None


@pytest.mark.parametrize(
    "secret",
    ["", "has space", "newline\nsecret", "non-ascii-密钥", "x" * 257],
)
def test_key_validation_rejects_unsafe_values_before_store_access(config, fake_keychain, secret):
    with pytest.raises(AppError) as caught:
        model_credentials.set_model_key(config, "text", "third_party", THIRD_PARTY_URL, secret)
    assert caught.value.code == "MODEL_KEY_INVALID"
    assert fake_keychain.items == {}
    if secret:
        assert secret not in str(caught.value)


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("DEVICE_KEYCHAIN_LOCKED", "MODEL_KEY_STORE_LOCKED"),
        ("DEVICE_KEYCHAIN_DENIED", "MODEL_KEY_STORE_DENIED"),
        ("DEVICE_KEYCHAIN_UNAVAILABLE", "MODEL_KEY_STORE_UNAVAILABLE"),
        ("DEVICE_KEYCHAIN_UNSUPPORTED", "MODEL_KEY_STORE_UNSUPPORTED"),
        ("DEVICE_KEYCHAIN_ERROR", "MODEL_KEY_STORE_ERROR"),
    ],
)
def test_store_failures_are_translated_and_sanitized(config, fake_keychain, code, expected):
    fake_keychain.error = IntegrationError(code, "fixture internal detail -25291")
    with pytest.raises(AppError) as caught:
        model_credentials.set_model_key(config, "text", "third_party", THIRD_PARTY_URL, "fixture-key")
    assert caught.value.code == expected
    assert code not in str(caught.value) and "-25291" not in str(caught.value)


def test_status_reports_unavailable_store_without_reading_secret_data(config, fake_keychain):
    model_credentials.set_model_key(config, "text", "third_party", THIRD_PARTY_URL, "fixture-key")
    fake_keychain.error = IntegrationError("DEVICE_KEYCHAIN_LOCKED", "locked")
    status = model_credentials.model_key_status(config, "text", "third_party", THIRD_PARTY_URL)
    assert status["secure_store_available"] is False
    # Environment fallback keeps third-party settings readable when the store is unavailable.
    assert status["key_configured"] is True
    assert status["key_source"] == "environment"


def test_resolve_model_key_prefers_keychain_and_falls_back_to_environment_for_third_party(
    config, fake_keychain
):
    # Environment fallback applies only when the store definitively has no saved override.
    assert model_credentials.resolve_model_key(config, "text", "third_party", THIRD_PARTY_URL) == (
        "fixture-not-a-real-api-key",
        "environment",
    )
    model_credentials.set_model_key(config, "text", "third_party", THIRD_PARTY_URL, "fixture-keychain-key")
    assert model_credentials.resolve_model_key(config, "text", "third_party", THIRD_PARTY_URL) == (
        "fixture-keychain-key",
        "keychain",
    )


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("DEVICE_KEYCHAIN_LOCKED", "MODEL_KEY_STORE_LOCKED"),
        ("DEVICE_KEYCHAIN_DENIED", "MODEL_KEY_STORE_DENIED"),
        ("DEVICE_KEYCHAIN_UNAVAILABLE", "MODEL_KEY_STORE_UNAVAILABLE"),
        ("DEVICE_KEYCHAIN_ERROR", "MODEL_KEY_STORE_ERROR"),
    ],
)
def test_store_failure_never_falls_back_to_the_environment_account(config, fake_keychain, code, expected):
    # A saved override plus an unreachable store must not silently switch billing to the
    # environment account, even though an environment key is present.
    model_credentials.set_model_key(config, "text", "third_party", THIRD_PARTY_URL, "fixture-keychain-key")
    fake_keychain.error = IntegrationError(code, "fixture internal detail -25308")
    with pytest.raises(AppError) as caught:
        model_credentials.resolve_model_key(config, "text", "third_party", THIRD_PARTY_URL)
    assert caught.value.code == expected
    assert code not in str(caught.value) and "-25308" not in str(caught.value)
    assert "fixture-not-a-real-api-key" not in str(caught.value)


def test_locked_store_blocks_resolution_even_without_a_saved_override(config, fake_keychain):
    # A locked store cannot definitively report "no entry", so the environment key stays unused.
    fake_keychain.error = IntegrationError("DEVICE_KEYCHAIN_LOCKED", "locked")
    with pytest.raises(AppError) as caught:
        model_credentials.resolve_model_key(config, "text", "third_party", THIRD_PARTY_URL)
    assert caught.value.code == "MODEL_KEY_STORE_LOCKED"


def test_unsupported_platform_keeps_environment_fallback_and_local_absence(config, fake_keychain):
    # A platform without the macOS Keychain can never hold a saved override.
    fake_keychain.error = IntegrationError("DEVICE_KEYCHAIN_UNSUPPORTED", "unsupported")
    assert model_credentials.resolve_model_key(config, "text", "third_party", THIRD_PARTY_URL) == (
        "fixture-not-a-real-api-key",
        "environment",
    )
    assert model_credentials.resolve_model_key(config, "speech", "local", LOCAL_URL) == (None, "none")


def test_url_identity_keeps_bracketed_ipv6_host_and_port_distinct():
    assert model_credentials._account("text", "local", "https://[::1]:443/v1") != model_credentials._account(
        "text", "local", "https://[::1:443]/v1"
    )


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("https://api.example.test/v1", "HTTPS://API.Example.TEST/v1/"),
        ("https://api.example.test/v1", "https://api.example.test:443/v1"),
        ("http://127.0.0.1:8000/v1", "http://127.0.0.1:8000/v1/"),
    ],
)
def test_url_identity_normalizes_spelling_case_and_default_ports(left, right):
    assert model_credentials._account("text", "third_party", left) == model_credentials._account(
        "text", "third_party", right
    )


def test_url_identity_never_suppresses_invalid_ports_into_another_target():
    assert model_credentials._account(
        "text", "third_party", "https://api.example.test:99999/v1"
    ) != model_credentials._account("text", "third_party", "https://api.example.test/v1")


def test_resolve_model_key_never_sends_environment_key_to_local(config, fake_keychain):
    # The fixture config has environment keys for every capability; local must ignore them.
    assert model_credentials.resolve_model_key(config, "speech", "local", LOCAL_URL) == (None, "none")
    model_credentials.set_model_key(config, "speech", "local", LOCAL_URL, "fixture-local-key")
    assert model_credentials.resolve_model_key(config, "speech", "local", LOCAL_URL) == (
        "fixture-local-key",
        "keychain",
    )


def test_local_store_failure_blocks_credential_resolution(config, fake_keychain):
    fake_keychain.error = IntegrationError("DEVICE_KEYCHAIN_LOCKED", "locked")
    with pytest.raises(AppError) as caught:
        model_credentials.resolve_model_key(config, "speech", "local", LOCAL_URL)
    assert caught.value.code == "MODEL_KEY_STORE_LOCKED"


def test_invalid_targets_are_rejected_before_store_access(config, fake_keychain):
    for kind, source, url in (
        ("embedding", "third_party", THIRD_PARTY_URL),
        ("text", "builtin", THIRD_PARTY_URL),
        ("text", "third_party", ""),
    ):
        with pytest.raises(AppError) as caught:
            model_credentials.get_model_key(config, kind, source, url)
        assert caught.value.code == "MODEL_KEY_TARGET_INVALID"
    assert fake_keychain.items == {}
