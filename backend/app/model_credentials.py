"""Model API keys stored in the local macOS Keychain.

Keys live under a dedicated service namespace scoped by the resolved data
directory, capability, profile source and normalized Base URL; they never enter
settings JSON, SQLite, logs or request snapshots. The heavy Security-framework
adapter is shared with the device-PIN boundary; only error translation and the
model-key namespace live here.
"""

from __future__ import annotations

import hashlib
from urllib.parse import urlsplit

from .config import AppConfig
from .device_credentials import _backend
from .integrations.errors import IntegrationError

MAX_KEY_LENGTH = 256
_KINDS = {"text", "image", "speech", "video"}
_SOURCES = {"third_party", "local"}


def _error(code: str, message: str):
    from .services import AppError

    return AppError(code, message, 409)


def _store_error(exc: IntegrationError):
    mapping = {
        "DEVICE_KEYCHAIN_LOCKED": (
            "MODEL_KEY_STORE_LOCKED",
            "macOS 钥匙串当前已锁定或需要交互授权，无法访问模型密钥；请解锁后重试",
        ),
        "DEVICE_KEYCHAIN_DENIED": ("MODEL_KEY_STORE_DENIED", "macOS 钥匙串拒绝访问模型密钥"),
        "DEVICE_KEYCHAIN_UNAVAILABLE": (
            "MODEL_KEY_STORE_UNAVAILABLE",
            "macOS 钥匙串当前不可用，无法保存或读取模型密钥",
        ),
        "DEVICE_KEYCHAIN_UNSUPPORTED": (
            "MODEL_KEY_STORE_UNSUPPORTED",
            "模型密钥安全存储仅支持 macOS 钥匙串",
        ),
    }
    code, message = mapping.get(
        exc.code, ("MODEL_KEY_STORE_ERROR", "macOS 钥匙串操作失败，无法访问模型密钥")
    )
    return _error(code, message)


def _validate_target(kind: str, source: str, base_url: str) -> None:
    if kind not in _KINDS or source not in _SOURCES or not isinstance(base_url, str) or not base_url:
        raise _error("MODEL_KEY_TARGET_INVALID", "模型密钥的目标能力、来源或服务地址无效")


def validate_model_key(value: str) -> str:
    """Printable ASCII secrets only, bounded length, never echoed in errors."""

    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= MAX_KEY_LENGTH
        or any(not 0x21 <= ord(char) <= 0x7E for char in value)
    ):
        raise _error(
            "MODEL_KEY_INVALID",
            f"模型密钥需为 1 至 {MAX_KEY_LENGTH} 位可打印 ASCII 字符（不含空格与控制字符）",
        )
    return value


def _normalize_url(base_url: str) -> str:
    parsed = urlsplit(base_url.strip())
    host = (parsed.hostname or "").lower().rstrip(".")
    if ":" in host:
        # Keep IPv6 literals bracketed so host and port stay unambiguous:
        # [::1]:443 and [::1:443] are different targets.
        host = f"[{host}]"
    try:
        port = parsed.port
    except ValueError:
        # An invalid port must never collapse onto another credential target.
        port = -1
    default_port = {"http": 80, "https": 443}.get(parsed.scheme.lower())
    netloc = host if port in (None, default_port) else f"{host}:{port}"
    return f"{parsed.scheme.lower()}://{netloc}{parsed.path.rstrip('/')}"


def _service(config: AppConfig) -> str:
    scope = hashlib.sha256(str(config.data_dir.resolve()).encode("utf-8")).hexdigest()[:24]
    return f"com.yingxu.publisher.model-key.v1.{scope}"


def _account(kind: str, source: str, base_url: str) -> str:
    url_hash = hashlib.sha256(_normalize_url(base_url).encode("utf-8")).hexdigest()[:24]
    return f"{kind}:{source}:{url_hash}"


def set_model_key(config: AppConfig, kind: str, source: str, base_url: str, api_key: str) -> None:
    _validate_target(kind, source, base_url)
    validate_model_key(api_key)
    try:
        _backend().set(_service(config), _account(kind, source, base_url), api_key)
    except IntegrationError as exc:
        raise _store_error(exc) from None


def get_model_key(config: AppConfig, kind: str, source: str, base_url: str) -> str | None:
    _validate_target(kind, source, base_url)
    try:
        return _backend().get(
            _service(config), _account(kind, source, base_url), max_length=MAX_KEY_LENGTH
        )
    except IntegrationError as exc:
        raise _store_error(exc) from None


def delete_model_key(config: AppConfig, kind: str, source: str, base_url: str) -> bool:
    _validate_target(kind, source, base_url)
    try:
        return _backend().delete(
            _service(config), _account(kind, source, base_url)
        )
    except IntegrationError as exc:
        raise _store_error(exc) from None


def secure_store_available(config: AppConfig) -> bool:
    """Probe store availability with a metadata-only query; never reads secret item data."""

    try:
        _backend().contains(_service(config), "availability-probe")
        return True
    except IntegrationError:
        return False


def model_key_status(config: AppConfig, kind: str, source: str, base_url: str) -> dict:
    """Existence and availability metadata only; never reads secret item data."""

    if kind not in _KINDS or source not in _SOURCES:
        raise _error("MODEL_KEY_TARGET_INVALID", "模型密钥的目标能力或来源无效")
    available = secure_store_available(config)
    stored = False
    if available and isinstance(base_url, str) and base_url:
        try:
            stored = bool(
                _backend().contains(
                    _service(config), _account(kind, source, base_url)
                )
            )
        except IntegrationError:
            available = False
    environment = bool(config.key(kind)) if source == "third_party" else False
    return {
        "key_configured": stored or environment,
        "key_source": "keychain" if stored else ("environment" if environment else "none"),
        "secure_store_available": available,
    }


def resolve_model_key(config: AppConfig, kind: str, source: str, base_url: str) -> tuple[str | None, str]:
    """Effective dispatch credential; environment fallback applies to third-party only.

    The environment key may substitute only when the secure store definitively has no
    saved override (empty lookup) or cannot exist on this platform (UNSUPPORTED). A
    locked, denied, unavailable or failing store raises instead: silently falling back
    to the environment account would bill a different account than the user configured.
    """

    from .services import AppError

    try:
        key = get_model_key(config, kind, source, base_url)
    except AppError as error:
        if error.code != "MODEL_KEY_STORE_UNSUPPORTED":
            raise
        # A platform without the macOS Keychain can never hold a saved override.
        key = None
    if key:
        return key, "keychain"
    if source == "local":
        return None, "none"
    environment = config.key(kind)
    return (environment, "environment") if environment else (None, "none")


# TikHub (X data collection) shares the same Keychain namespace under a fixed account.
_TIKHUB_ACCOUNT = "tikhub:api"


def set_tikhub_key(config: AppConfig, api_key: str) -> None:
    validate_model_key(api_key)
    try:
        _backend().set(_service(config), _TIKHUB_ACCOUNT, api_key)
    except IntegrationError as exc:
        raise _store_error(exc) from None


def delete_tikhub_key(config: AppConfig) -> bool:
    try:
        return _backend().delete(_service(config), _TIKHUB_ACCOUNT)
    except IntegrationError as exc:
        raise _store_error(exc) from None


def tikhub_key_status(config: AppConfig) -> dict:
    """Existence and availability metadata only; never reads secret item data."""

    available = secure_store_available(config)
    stored = False
    if available:
        try:
            stored = bool(_backend().contains(_service(config), _TIKHUB_ACCOUNT))
        except IntegrationError:
            available = False
    environment = bool(config.tikhub_api_key.get_secret_value())
    return {
        "key_configured": stored or environment,
        "key_source": "keychain" if stored else ("environment" if environment else "none"),
        "secure_store_available": available,
    }


def resolve_tikhub_key(config: AppConfig) -> tuple[str | None, str]:
    """Keychain first; the environment key substitutes only when no override can exist.

    Same rule as model keys: a locked or failing store raises instead of silently
    billing the environment account.
    """

    try:
        key = _backend().get(_service(config), _TIKHUB_ACCOUNT, max_length=MAX_KEY_LENGTH)
    except IntegrationError as exc:
        if exc.code != "DEVICE_KEYCHAIN_UNSUPPORTED":
            raise _store_error(exc) from None
        key = None
    if key:
        return key, "keychain"
    environment = config.tikhub_api_key.get_secret_value()
    return (environment, "environment") if environment else (None, "none")
