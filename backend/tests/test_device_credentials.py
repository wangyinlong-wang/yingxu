from __future__ import annotations

import sys

import pytest

from app import device_credentials
from app.integrations.errors import IntegrationError


class _FakeBackend:
    def __init__(self, *, secret: str | None = None, stored: bool = False):
        self.secret = secret
        self.stored = stored
        self.calls = []

    def set(self, service, account, secret):
        self.calls.append(("set", service, account, secret))

    def get(self, service, account):
        self.calls.append(("get", service, account))
        return self.secret

    def delete(self, service, account):
        self.calls.append(("delete", service, account))
        return self.stored

    def contains(self, service, account):
        self.calls.append(("contains", service, account))
        return self.stored


@pytest.mark.skipif(sys.platform != "darwin", reason="native macOS framework")
def test_native_security_framework_symbols_load_without_keychain_access():
    framework = device_credentials._SecurityFramework()
    assert framework.kSecClass
    assert framework.kSecAttrSynchronizable
    assert framework.kSecUseAuthenticationUIFail


def test_keychain_operations_are_project_and_serial_scoped(monkeypatch):
    backend = _FakeBackend(secret="246810", stored=True)
    monkeypatch.setattr(device_credentials, "_backend", lambda: backend)

    device_credentials.set_device_pin("ABC123", "246810")
    assert device_credentials.get_device_pin("ABC123") == "246810"
    assert device_credentials.device_pin_status("ABC123") == {"stored": True}
    assert device_credentials.delete_device_pin("ABC123") is True

    services = {call[1] for call in backend.calls}
    accounts = {call[2] for call in backend.calls}
    assert len(services) == 1
    assert next(iter(services)).startswith("com.yingxu.publisher.device-pin.v1.")
    assert accounts == {"ABC123"}
    assert backend.calls[2][0] == "contains"


@pytest.mark.parametrize("pin", ["123", "1" * 13, "12a456", "１２３４５６", "١٢٣٤٥٦", ""])
def test_pin_validation_rejects_non_ascii_or_wrong_length_without_keychain_access(monkeypatch, pin):
    monkeypatch.setattr(
        device_credentials, "_backend", lambda: pytest.fail("invalid input must not access Keychain")
    )
    with pytest.raises(IntegrationError) as caught:
        device_credentials.set_device_pin("ABC123", pin)
    assert caught.value.code == "INVALID_DEVICE_PIN"
    if pin:
        assert pin not in str(caught.value)


def test_invalid_stored_pin_is_rejected_without_exposing_value(monkeypatch):
    backend = _FakeBackend(secret="not-a-pin")
    monkeypatch.setattr(device_credentials, "_backend", lambda: backend)
    with pytest.raises(IntegrationError) as caught:
        device_credentials.get_device_pin("ABC123")
    assert caught.value.code == "INVALID_DEVICE_PIN"
    assert "not-a-pin" not in str(caught.value)


def test_unsupported_platform_fails_without_file_fallback(monkeypatch):
    monkeypatch.setattr(device_credentials.sys, "platform", "linux")
    with pytest.raises(IntegrationError) as caught:
        device_credentials.device_pin_status("ABC123")
    assert caught.value.code == "DEVICE_KEYCHAIN_UNSUPPORTED"


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (-25308, "DEVICE_KEYCHAIN_LOCKED"),
        (-25293, "DEVICE_KEYCHAIN_DENIED"),
        (-25291, "DEVICE_KEYCHAIN_UNAVAILABLE"),
        (-25294, "DEVICE_KEYCHAIN_UNAVAILABLE"),
        (-50, "DEVICE_KEYCHAIN_ERROR"),
    ],
)
def test_security_statuses_map_to_sanitized_errors(status, code):
    error = device_credentials._keychain_error(status)
    assert error.code == code
    assert str(status) not in str(error)
