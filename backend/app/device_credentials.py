"""Device credentials stored in the local macOS Keychain.

The Android PIN is intentionally available only through this small boundary. It
is never written to the project data directory or passed to another process.
"""

from __future__ import annotations

import ctypes
import hashlib
import re
import sys
from collections.abc import Iterator
from contextlib import contextmanager

from .config import ROOT

_SERIAL_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_PROJECT_SCOPE = hashlib.sha256(str(ROOT.resolve()).encode("utf-8")).hexdigest()[:24]
_SERVICE = f"com.yingxu.publisher.device-pin.v1.{_PROJECT_SCOPE}"

_ERR_SEC_SUCCESS = 0
_ERR_SEC_NOT_AVAILABLE = -25291
_ERR_SEC_AUTH_FAILED = -25293
_ERR_SEC_NO_SUCH_KEYCHAIN = -25294
_ERR_SEC_DUPLICATE_ITEM = -25299
_ERR_SEC_ITEM_NOT_FOUND = -25300
_ERR_SEC_INTERACTION_NOT_ALLOWED = -25308
_CF_STRING_ENCODING_UTF8 = 0x08000100


def _error(code: str, message: str):
    # Import lazily because app.integrations re-exports device functions from its
    # package initializer, while device.py uses this credential boundary.
    from .integrations.errors import IntegrationError

    return IntegrationError(code, message)


def _validate_serial(serial: str) -> str:
    value = str(serial or "").strip()
    if not _SERIAL_PATTERN.fullmatch(value):
        raise _error("INVALID_DEVICE_SERIAL", "设备序列号无效")
    return value


def _validate_pin(pin: str) -> str:
    if not isinstance(pin, str) or not 4 <= len(pin) <= 12 or not pin.isascii() or not pin.isdecimal():
        raise _error("INVALID_DEVICE_PIN", "手机解锁 PIN 必须为 4 至 12 位 ASCII 数字")
    return pin


def _keychain_error(status: int):
    if status == _ERR_SEC_INTERACTION_NOT_ALLOWED:
        return _error(
            "DEVICE_KEYCHAIN_LOCKED",
            "macOS 钥匙串当前已锁定或需要交互授权，后台任务不会弹窗读取手机 PIN",
        )
    if status == _ERR_SEC_AUTH_FAILED:
        return _error("DEVICE_KEYCHAIN_DENIED", "macOS 钥匙串拒绝访问手机 PIN")
    if status in {_ERR_SEC_NOT_AVAILABLE, _ERR_SEC_NO_SUCH_KEYCHAIN}:
        return _error("DEVICE_KEYCHAIN_UNAVAILABLE", "macOS 钥匙串当前不可用")
    return _error("DEVICE_KEYCHAIN_ERROR", "macOS 钥匙串操作失败")


class _SecurityFramework:
    """Small ctypes adapter around the modern SecItem API."""

    def __init__(self) -> None:
        try:
            self.security = ctypes.CDLL("/System/Library/Frameworks/Security.framework/Security")
            self.core = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
            self._configure_functions()
            self._load_constants()
        except (AttributeError, OSError, ValueError) as exc:
            raise _error(
                "DEVICE_KEYCHAIN_UNAVAILABLE", "无法加载 macOS 钥匙串安全组件"
            ) from exc

    def _configure_functions(self) -> None:
        self.security.SecItemCopyMatching.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
        self.security.SecItemCopyMatching.restype = ctypes.c_int32
        self.security.SecItemAdd.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
        self.security.SecItemAdd.restype = ctypes.c_int32
        self.security.SecItemUpdate.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        self.security.SecItemUpdate.restype = ctypes.c_int32
        self.security.SecItemDelete.argtypes = [ctypes.c_void_p]
        self.security.SecItemDelete.restype = ctypes.c_int32

        self.core.CFStringCreateWithBytes.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_long,
            ctypes.c_uint32,
            ctypes.c_ubyte,
        ]
        self.core.CFStringCreateWithBytes.restype = ctypes.c_void_p
        self.core.CFDataCreate.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_long]
        self.core.CFDataCreate.restype = ctypes.c_void_p
        self.core.CFDataGetLength.argtypes = [ctypes.c_void_p]
        self.core.CFDataGetLength.restype = ctypes.c_long
        self.core.CFDataGetBytePtr.argtypes = [ctypes.c_void_p]
        self.core.CFDataGetBytePtr.restype = ctypes.c_void_p
        self.core.CFDictionaryCreate.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.c_long,
            ctypes.c_void_p,
            ctypes.c_void_p,
        ]
        self.core.CFDictionaryCreate.restype = ctypes.c_void_p
        self.core.CFRelease.argtypes = [ctypes.c_void_p]
        self.core.CFRelease.restype = None

    def _load_constants(self) -> None:
        names = (
            "kSecClass",
            "kSecClassGenericPassword",
            "kSecAttrService",
            "kSecAttrAccount",
            "kSecAttrSynchronizable",
            "kSecValueData",
            "kSecReturnData",
            "kSecUseAuthenticationUI",
            "kSecUseAuthenticationUIFail",
        )
        for name in names:
            setattr(self, name, ctypes.c_void_p.in_dll(self.security, name).value)
        self.kCFBooleanFalse = ctypes.c_void_p.in_dll(self.core, "kCFBooleanFalse").value
        self.kCFBooleanTrue = ctypes.c_void_p.in_dll(self.core, "kCFBooleanTrue").value

    def _string(self, value: str) -> int:
        encoded = value.encode("utf-8")
        buffer = ctypes.create_string_buffer(encoded)
        reference = self.core.CFStringCreateWithBytes(
            None,
            ctypes.cast(buffer, ctypes.c_void_p),
            len(encoded),
            _CF_STRING_ENCODING_UTF8,
            False,
        )
        if not reference:
            raise _error("DEVICE_KEYCHAIN_ERROR", "无法准备 macOS 钥匙串项目")
        return reference

    def _data(self, value: bytes) -> int:
        buffer = ctypes.create_string_buffer(value)
        reference = self.core.CFDataCreate(None, ctypes.cast(buffer, ctypes.c_void_p), len(value))
        if not reference:
            raise _error("DEVICE_KEYCHAIN_ERROR", "无法准备 macOS 钥匙串数据")
        return reference

    @contextmanager
    def _dictionary(self, entries: list[tuple[int, int]]) -> Iterator[int]:
        keys = (ctypes.c_void_p * len(entries))(*(key for key, _value in entries))
        values = (ctypes.c_void_p * len(entries))(*(_value for _key, _value in entries))
        reference = self.core.CFDictionaryCreate(None, keys, values, len(entries), None, None)
        if not reference:
            raise _error("DEVICE_KEYCHAIN_ERROR", "无法准备 macOS 钥匙串查询")
        try:
            yield reference
        finally:
            self.core.CFRelease(reference)

    @contextmanager
    def _query(self, service: str, account: str, *, return_data: bool = False) -> Iterator[int]:
        service_ref = self._string(service)
        account_ref = self._string(account)
        entries = [
            (self.kSecClass, self.kSecClassGenericPassword),
            (self.kSecAttrService, service_ref),
            (self.kSecAttrAccount, account_ref),
            (self.kSecAttrSynchronizable, self.kCFBooleanFalse),
            (self.kSecUseAuthenticationUI, self.kSecUseAuthenticationUIFail),
        ]
        if return_data:
            entries.append((self.kSecReturnData, self.kCFBooleanTrue))
        try:
            with self._dictionary(entries) as query:
                yield query
        finally:
            self.core.CFRelease(account_ref)
            self.core.CFRelease(service_ref)

    def contains(self, service: str, account: str) -> bool:
        with self._query(service, account) as query:
            status = self.security.SecItemCopyMatching(query, None)
        if status == _ERR_SEC_ITEM_NOT_FOUND:
            return False
        if status != _ERR_SEC_SUCCESS:
            raise _keychain_error(status)
        return True

    def get(self, service: str, account: str, *, max_length: int = 12) -> str | None:
        result = ctypes.c_void_p()
        with self._query(service, account, return_data=True) as query:
            status = self.security.SecItemCopyMatching(query, ctypes.byref(result))
        if status == _ERR_SEC_ITEM_NOT_FOUND:
            return None
        if status != _ERR_SEC_SUCCESS:
            raise _keychain_error(status)
        if not result.value:
            raise _error("DEVICE_KEYCHAIN_ERROR", "macOS 钥匙串返回的数据无效")
        try:
            length = self.core.CFDataGetLength(result.value)
            pointer = self.core.CFDataGetBytePtr(result.value)
            if length < 0 or length > max_length or (length and not pointer):
                raise _error("DEVICE_KEYCHAIN_ERROR", "macOS 钥匙串返回的数据无效")
            try:
                return ctypes.string_at(pointer, length).decode("ascii")
            except UnicodeDecodeError as exc:
                raise _error(
                    "INVALID_DEVICE_PIN", "钥匙串中的手机 PIN 格式无效，请重新保存"
                ) from exc
        finally:
            self.core.CFRelease(result.value)

    def set(self, service: str, account: str, secret: str) -> None:
        secret_ref = self._data(secret.encode("ascii"))
        try:
            with self._query(service, account) as query, self._dictionary(
                [(self.kSecValueData, secret_ref)]
            ) as changes:
                status = self.security.SecItemUpdate(query, changes)
            if status == _ERR_SEC_ITEM_NOT_FOUND:
                self._add(service, account, secret_ref)
                return
            if status != _ERR_SEC_SUCCESS:
                raise _keychain_error(status)
        finally:
            self.core.CFRelease(secret_ref)

    def _add(self, service: str, account: str, secret_ref: int) -> None:
        service_ref = self._string(service)
        account_ref = self._string(account)
        try:
            entries = [
                (self.kSecClass, self.kSecClassGenericPassword),
                (self.kSecAttrService, service_ref),
                (self.kSecAttrAccount, account_ref),
                (self.kSecAttrSynchronizable, self.kCFBooleanFalse),
                (self.kSecUseAuthenticationUI, self.kSecUseAuthenticationUIFail),
                (self.kSecValueData, secret_ref),
            ]
            with self._dictionary(entries) as attributes:
                status = self.security.SecItemAdd(attributes, None)
            if status == _ERR_SEC_DUPLICATE_ITEM:
                with self._query(service, account) as query, self._dictionary(
                    [(self.kSecValueData, secret_ref)]
                ) as changes:
                    status = self.security.SecItemUpdate(query, changes)
            if status != _ERR_SEC_SUCCESS:
                raise _keychain_error(status)
        finally:
            self.core.CFRelease(account_ref)
            self.core.CFRelease(service_ref)

    def delete(self, service: str, account: str) -> bool:
        with self._query(service, account) as query:
            status = self.security.SecItemDelete(query)
        if status == _ERR_SEC_ITEM_NOT_FOUND:
            return False
        if status != _ERR_SEC_SUCCESS:
            raise _keychain_error(status)
        return True


def _backend() -> _SecurityFramework:
    if sys.platform != "darwin":
        raise _error("DEVICE_KEYCHAIN_UNSUPPORTED", "手机 PIN 安全存储仅支持 macOS 钥匙串")
    return _SecurityFramework()


def set_device_pin(serial: str, pin: str) -> None:
    """Create or replace one project's PIN for exactly one Android serial."""

    serial = _validate_serial(serial)
    pin = _validate_pin(pin)
    _backend().set(_SERVICE, serial, pin)


def get_device_pin(serial: str) -> str | None:
    """Read one PIN without allowing authentication UI from worker processes."""

    serial = _validate_serial(serial)
    pin = _backend().get(_SERVICE, serial)
    return _validate_pin(pin) if pin is not None else None


def delete_device_pin(serial: str) -> bool:
    """Delete one serial-bound PIN, returning whether an item existed."""

    serial = _validate_serial(serial)
    return _backend().delete(_SERVICE, serial)


def device_pin_status(serial: str) -> dict[str, bool]:
    """Return existence only; this path never requests secret item data."""

    serial = _validate_serial(serial)
    return {"stored": _backend().contains(_SERVICE, serial)}
