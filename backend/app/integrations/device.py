"""Guarded Android preparation and read-only metric collection.

This module never installs device software, accepts permissions, handles login or
CAPTCHA screens, chooses ambiguous media, or clicks a final publish control.  A
resume from an editor requires a short-lived, main-worker-persisted evidence
record plus exact caption and media-fingerprint evidence in the current UI.
Selectors vary by app version; unknown pages deliberately stop for the operator.
"""

from __future__ import annotations

import base64
import hashlib
import importlib.resources
import ipaddress
import logging
import os
import re
import secrets
import shlex
import shutil
import string
import subprocess
import sys
import tempfile
import threading
import time
import xml.etree.ElementTree as ET
from datetime import UTC, datetime, timedelta
from decimal import ROUND_DOWN, Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from ..config import AppConfig
from ..device_credentials import get_device_pin
from .errors import CanceledError, IntegrationError

try:  # The application dependency is optional during non-device development.
    import uiautomator2 as u2
except ImportError:  # pragma: no cover - covered through the setup guard
    u2 = None

_SERIAL_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_PACKAGE_VERSION_PATTERN = re.compile(r"\bversionName=([^\s]+)")
_U2_REMOTE_JAR = "/data/local/tmp/u2.jar"
_MAX_HIERARCHY_BYTES = 2_000_000
_EVIDENCE_MAX_AGE = timedelta(minutes=30)
_WAIT_AFTER_NAVIGATION = 1.2
_WAIT_AFTER_POWER = 1.5
_WAIT_AFTER_SYSTEM_KEY = 0.8
_WAIT_AFTER_PROXIMITY = 1.0
_WAIT_AFTER_PIN = 1.2
_SYSTEM_UI_PACKAGE = "com.android.systemui"
_PIN_ENTRY_ID = "com.android.systemui:id/pinEntry"
_PROXIMITY_TITLE_ID = "android.miui:id/screen_on_proximity_sensor_text_title"
log = logging.getLogger(__name__)

_PLATFORMS = {
    "douyin": {
        "package": "com.ss.android.ugc.aweme",
        "hosts": {"www.douyin.com", "v.douyin.com", "www.iesdouyin.com"},
        "account_prefixes": ("抖音号",),
        "self_profile_markers": ("编辑资料", "编辑个人资料", "编辑主页"),
        "profile_labels": ("我",),
        "create_labels": ("拍摄", "拍摄，按钮", "创作", "+"),
        "editor_markers": ("发布设置", "作品描述", "添加话题", "谁可以看", "保存本地", "发布"),
        "caption_placeholders": ("添加作品描述", "写标题并使用合适的话题，能让更多人看到"),
    },
    "xiaohongshu": {
        "package": "com.xingin.xhs",
        "hosts": {
            "www.xiaohongshu.com",
            "xhslink.com",
            "www.xhslink.com",
            "xhslink.cn",
            "www.xhslink.cn",
        },
        "account_prefixes": ("小红书号", "RED ID"),
        "self_profile_markers": ("编辑资料", "编辑个人资料", "编辑主页"),
        "profile_labels": ("我",),
        "create_labels": ("发布", "+", "创作"),
        "editor_markers": (
            "发布笔记",
            "添加标题",
            "填写正文",
            "添加作品描述",
            "公开可见",
            "合辑",
            "发布",
            "发笔记",
        ),
        "caption_placeholders": ("填写正文", "输入正文描述", "添加作品描述"),
    },
}

_SYSTEM_PICKER_PACKAGES = {
    "com.android.providers.media.module",
    "com.google.android.providers.media.module",
    "com.android.documentsui",
}
_PERMISSION_PACKAGES = {
    "com.android.permissioncontroller",
    "com.google.android.permissioncontroller",
}
_LOGIN_MARKERS = (
    "手机号登录",
    "验证码登录",
    "密码登录",
    "登录后",
    "登录/注册",
    "重新登录",
    "为避免账号无法登录",
)
_CAPTCHA_MARKERS = ("滑块验证", "请完成验证", "安全验证", "图形验证码")
_PERMISSION_MARKERS = ("允许", "仅在使用该应用时允许", "始终允许", "不允许")
_PICKER_MARKERS = ("最近项目", "所有照片", "相册", "选择照片", "选择视频")
_NEXT_LABELS = ("下一步", "完成", "确定")
_QR_LOCK = threading.Lock()
_QR_SESSION: dict[str, Any] | None = None


def _validate_serial(serial: str) -> str:
    value = str(serial or "").strip()
    if not _SERIAL_PATTERN.fullmatch(value):
        raise IntegrationError("INVALID_DEVICE_SERIAL", "设备序列号无效")
    return value


def _platform(platform: str) -> dict:
    value = str(platform or "").strip().lower()
    if value not in _PLATFORMS:
        raise IntegrationError("UNSUPPORTED_PLATFORM", "暂不支持该发布平台")
    return _PLATFORMS[value]


def _adb_binary() -> str | None:
    return shutil.which("adb")


def _run_command(arguments: list[str], *, timeout: float = 10) -> str:
    try:
        result = subprocess.run(arguments, capture_output=True, check=False, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise IntegrationError("ADB_TIMEOUT", "Android 设备命令执行超时") from exc
    except OSError as exc:
        raise IntegrationError("ADB_FAILED", "无法启动 Android 设备命令") from exc
    if result.returncode != 0:
        raise IntegrationError("ADB_FAILED", "Android 设备命令执行失败")
    return result.stdout.decode("utf-8", errors="replace")


def _adb(serial: str, *arguments: str, timeout: float = 10) -> str:
    adb = _adb_binary()
    if not adb:
        raise IntegrationError("ADB_NOT_FOUND", "未安装 Android platform-tools")
    # adb shell concatenates arguments for a remote shell; a local argv array alone
    # does not protect URLs, SQL selections or file names from shell interpretation.
    if arguments and arguments[0] == "shell":
        arguments = ("shell", " ".join(shlex.quote(str(value)) for value in arguments[1:]))
    return _run_command([adb, "-s", _validate_serial(serial), *arguments], timeout=timeout)


def list_devices() -> list[dict]:
    """List adb transports without attempting any device-side setup."""

    adb = _adb_binary()
    if not adb:
        return []
    output = _run_command([adb, "devices", "-l"], timeout=8)
    devices: list[dict] = []
    for raw_line in output.splitlines()[1:]:
        line = raw_line.strip()
        if not line or line.startswith("*"):
            continue
        parts = line.split()
        if len(parts) < 2 or not _SERIAL_PATTERN.fullmatch(parts[0]):
            continue
        details = {key: value for key, value in (part.split(":", 1) for part in parts[2:] if ":" in part)}
        item = {"serial": parts[0], "state": parts[1]}
        if details.get("model"):
            item["model"] = details["model"]
        devices.append(item)
    return devices


def _mdns_services(adb: str) -> list[tuple[str, str, str, int]]:
    try:
        output = _run_command([adb, "mdns", "services"], timeout=5)
    except IntegrationError:
        return []
    services = []
    for line in output.splitlines()[1:]:
        parts = line.split()
        if len(parts) != 3:
            continue
        host, separator, raw_port = parts[2].rpartition(":")
        if not separator or not raw_port.isdecimal() or not 1 <= int(raw_port) <= 65535:
            continue
        try:
            address = ipaddress.IPv4Address(host)
        except ipaddress.AddressValueError:
            continue
        if address.is_private and not address.is_loopback and not address.is_link_local:
            services.append((parts[0], parts[1].rstrip("."), host, int(raw_port)))
    return services


def _wireless_services(adb: str, ip: str) -> list[tuple[str, int]]:
    return [
        (name, port)
        for name, kind, host, port in _mdns_services(adb)
        if kind == "_adb-tls-connect._tcp" and host == ip
    ]


def connect_wireless_device(ip: str, connection_port: int) -> dict:
    adb = _adb_binary()
    if not adb:
        raise IntegrationError("ADB_NOT_FOUND", "未安装 Android platform-tools")
    address = f"{ip}:{connection_port}"
    try:
        _run_command([adb, "connect", address], timeout=15)
    except IntegrationError as exc:
        raise IntegrationError("ADB_CONNECT_FAILED", "连接失败，请核对无线调试主页面的 IP 和连接端口") from exc
    connected = [item for item in list_devices() if item["state"] == "device"]
    exact = next((item for item in connected if item["serial"] == address), None)
    if exact:
        return {"serial": exact["serial"], "state": "device"}
    aliases = {
        f"{instance}._adb-tls-connect._tcp"
        for instance, port in _wireless_services(adb, ip)
        if port == connection_port
    }
    matched = next((item for item in connected if item["serial"] in aliases), None)
    if not matched:
        raise IntegrationError("ADB_CONNECT_FAILED", "连接后设备仍未就绪，请保持无线调试开启并核对连接端口")
    return {"serial": matched["serial"], "state": "device"}


def _pair_at(adb: str, address: str, secret: str) -> None:
    try:
        # The code must not appear in argv, process listings, logs or error responses.
        paired = subprocess.run(
            [adb, "pair", address],
            input=(secret + "\n").encode(),
            capture_output=True,
            check=False,
            timeout=20,
        )
    except subprocess.TimeoutExpired as exc:
        raise IntegrationError("ADB_PAIR_TIMEOUT", "无线配对超时，请检查手机配对弹窗和网络") from exc
    except OSError as exc:
        raise IntegrationError("ADB_FAILED", "无法启动 Android 设备命令") from exc
    if paired.returncode != 0:
        raise IntegrationError("ADB_PAIR_FAILED", "无线配对失败，请检查配对码、配对端口及手机弹窗是否仍打开")


def _after_pair(adb: str, ip: str, pairing_port: int, connection_port: int | None) -> dict:
    if not connection_port or connection_port == pairing_port:
        for attempt in range(3):
            ports = {port for _, port in _wireless_services(adb, ip) if port != pairing_port}
            if len(ports) == 1:
                connection_port = ports.pop()
                break
            if len(ports) > 1:
                break
            if attempt < 2:
                time.sleep(0.5)
    if connection_port and connection_port != pairing_port:
        try:
            return {"paired": True, "connected": True, **connect_wireless_device(ip, connection_port)}
        except IntegrationError:
            pass
    return {"paired": True, "connected": False, "serial": None}


def pair_wireless_device(ip: str, pairing_port: int, connection_port: int | None, pairing_code: str) -> dict:
    """Pair using one-time stdin input, then find and verify the ADB transport."""

    adb = _adb_binary()
    if not adb:
        raise IntegrationError("ADB_NOT_FOUND", "未安装 Android platform-tools")
    services = [item for item in _mdns_services(adb) if item[2] == ip]
    connect_ports = {port for _, kind, _, port in services if kind == "_adb-tls-connect._tcp"}
    if pairing_port in connect_ports:
        pair_ports = {port for _, kind, _, port in services if kind == "_adb-tls-pairing._tcp"}
        if len(pair_ports) != 1:
            raise IntegrationError("ADB_PAIR_PORT_IS_CONNECT", "填入的是连接端口；请打开手机配对弹窗并填写弹窗中的端口")
        connection_port = connection_port or pairing_port
        pairing_port = pair_ports.pop()
    _pair_at(adb, f"{ip}:{pairing_port}", pairing_code)
    return _after_pair(adb, ip, pairing_port, connection_port)


def _render_pairing_qr(payload: str) -> bytes:
    swift = shutil.which("swift")
    if sys.platform != "darwin" or not swift:
        raise IntegrationError("QR_UNAVAILABLE", "本机缺少二维码生成环境，请使用配对码方式")
    cache = Path(tempfile.gettempdir()) / "yingxu-qr-swift-cache"
    try:
        cache.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise IntegrationError("QR_UNAVAILABLE", "无法生成配对二维码，请使用配对码方式") from exc
    env = os.environ.copy()
    env.update({
        "TMPDIR": str(cache),
        "SWIFT_MODULE_CACHE_PATH": str(cache / "swift-modules"),
        "CLANG_MODULE_CACHE_PATH": str(cache / "clang-modules"),
    })
    try:
        result = subprocess.run(
            [swift, str(Path(__file__).with_name("adb_pair_qr.swift"))],
            input=payload.encode(), capture_output=True, check=False, timeout=90, env=env,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise IntegrationError("QR_UNAVAILABLE", "无法生成配对二维码，请使用配对码方式") from exc
    if result.returncode != 0 or not result.stdout.startswith(b"\x89PNG\r\n\x1a\n"):
        raise IntegrationError("QR_UNAVAILABLE", "无法生成配对二维码，请使用配对码方式")
    return result.stdout


def start_wireless_qr() -> dict:
    adb = _adb_binary()
    if not adb:
        raise IntegrationError("ADB_NOT_FOUND", "未安装 Android platform-tools")
    alphabet = string.ascii_letters + string.digits
    service = "studio-" + "".join(secrets.choice(alphabet) for _ in range(10))
    secret = "".join(secrets.choice(alphabet) for _ in range(10))
    payload = f"WIFI:T:ADB;S:{service};P:{secret};;"
    png = _render_pairing_qr(payload)
    token = secrets.token_urlsafe(24)
    global _QR_SESSION
    with _QR_LOCK:
        _QR_SESSION = {
            "token": token, "service": service, "secret": secret,
            "expires_at": time.monotonic() + 120, "busy": False, "result": None,
        }
    return {"session_id": token, "image": "data:image/png;base64," + base64.b64encode(png).decode(), "expires_in": 120}


def poll_wireless_qr(token: str) -> dict:
    global _QR_SESSION
    with _QR_LOCK:
        session = _QR_SESSION
        if not session or session["token"] != token:
            raise IntegrationError("QR_SESSION_NOT_FOUND", "二维码已失效，请重新生成")
        if time.monotonic() >= session["expires_at"]:
            _QR_SESSION = None
            return {"status": "expired"}
        if session["result"]:
            return session["result"]
        if session["busy"]:
            return {"status": "waiting"}
        session["busy"] = True
    result = {"status": "waiting"}
    try:
        adb = _adb_binary()
        if not adb:
            result = {"status": "failed", "message": "未安装 Android platform-tools"}
        else:
            pairing = next(
                (
                    (host, port)
                    for name, kind, host, port in _mdns_services(adb)
                    if name == session["service"] and kind == "_adb-tls-pairing._tcp"
                ),
                None,
            )
            if pairing:
                ip, port = pairing
                try:
                    _pair_at(adb, f"{ip}:{port}", session["secret"])
                    connected = _after_pair(adb, ip, port, None)
                    result = {"status": "connected" if connected["connected"] else "paired", "ip": ip,
                              "serial": connected["serial"]}
                except IntegrationError:
                    result = {"status": "failed", "message": "扫码后配对失败，请重新生成二维码"}
    finally:
        with _QR_LOCK:
            if _QR_SESSION is session:
                session["busy"] = False
                if result["status"] != "waiting":
                    session["result"] = result
                    session["secret"] = ""
                    session["expires_at"] = time.monotonic() + 30
    return result


def _require_connected(serial: str) -> dict:
    matched = next((item for item in list_devices() if item["serial"] == serial), None)
    if matched is None:
        raise IntegrationError("DEVICE_NOT_FOUND", "所选 Android 设备未连接")
    if matched["state"] != "device":
        raise IntegrationError("DEVICE_NOT_READY", f"所选 Android 设备状态不可用：{matched['state']}")
    return matched


def _package_version(serial: str, package: str) -> str | None:
    output = _adb(serial, "shell", "dumpsys", "package", package, timeout=15)
    match = _PACKAGE_VERSION_PATTERN.search(output)
    return match.group(1) if match else None


def _local_u2_jar() -> Path:
    if u2 is None:
        raise IntegrationError("DEVICE_CLIENT_MISSING", "本机未安装 uiautomator2")
    try:
        resource = importlib.resources.files("uiautomator2").joinpath("assets/u2.jar")
        path = Path(str(resource))
    except (ModuleNotFoundError, AttributeError, TypeError) as exc:
        raise IntegrationError("DEVICE_SETUP_REQUIRED", "本机 UI 自动化运行组件不可用") from exc
    if not path.is_file():
        raise IntegrationError("DEVICE_SETUP_REQUIRED", "本机 UI 自动化运行组件不可用")
    return path


def _file_md5(path: Path) -> str:
    # This is the checksum uiautomator2 itself uses as a compatibility fingerprint.
    digest = hashlib.md5()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _require_uiautomator_setup(serial: str) -> None:
    """Ensure u2.connect cannot auto-push a missing or mismatched device jar."""

    local_digest = _file_md5(_local_u2_jar())
    try:
        output = _adb(serial, "shell", "toybox", "md5sum", _U2_REMOTE_JAR, timeout=8)
    except IntegrationError:
        try:
            output = _adb(serial, "shell", "md5sum", _U2_REMOTE_JAR, timeout=8)
        except IntegrationError as exc:
            raise IntegrationError(
                "DEVICE_SETUP_REQUIRED",
                "设备尚未初始化 UI 自动化，请由用户明确完成初始化后再继续",
            ) from exc
    if local_digest not in output.lower():
        raise IntegrationError(
            "DEVICE_SETUP_REQUIRED",
            "设备上的 UI 自动化组件版本不匹配，请由用户明确更新后再继续",
        )


def _connect_device(serial: str):
    _require_uiautomator_setup(serial)
    try:
        # The exact jar checksum above makes the library's setup check read-only.
        return u2.connect(serial)
    except Exception as exc:
        raise IntegrationError("DEVICE_AUTOMATION_UNAVAILABLE", "设备上的 UI 自动化服务不可用") from exc


def _current_app(device: Any) -> tuple[str, str]:
    try:
        current = device.app_current()
    except Exception as exc:
        raise IntegrationError("DEVICE_READ_FAILED", "无法读取当前 Android 页面") from exc
    if not isinstance(current, dict):
        return "", ""
    return str(current.get("package") or ""), str(current.get("activity") or "")


def _hierarchy(device: Any) -> list[dict]:
    try:
        try:
            raw = device.dump_hierarchy(compressed=False, max_depth=50)
        except TypeError:
            raw = device.dump_hierarchy()
    except Exception as exc:
        raise IntegrationError("DEVICE_READ_FAILED", "无法读取 Android 页面结构") from exc
    if not isinstance(raw, str) or not raw.strip():
        raise IntegrationError("DEVICE_READ_FAILED", "Android 页面结构为空")
    if len(raw.encode("utf-8")) > _MAX_HIERARCHY_BYTES:
        raise IntegrationError("DEVICE_READ_FAILED", "Android 页面结构超过大小上限")
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        raise IntegrationError("DEVICE_READ_FAILED", "Android 页面结构无效") from exc
    nodes = []
    parents = {child: parent for parent in root.iter() for child in parent}
    for node in root.iter():
        attributes = node.attrib
        if attributes.get("visible-to-user") == "false":
            continue
        parent = parents.get(node)
        parent_attributes = parent.attrib if parent is not None else {}
        if parent is not None and parent_attributes.get("clickable") != "true":
            grandparent = parents.get(parent)
            if grandparent is not None and grandparent.get("clickable") == "true":
                parent_attributes = grandparent.attrib
        text = " ".join(str(attributes.get("text") or "").split())
        description = " ".join(str(attributes.get("content-desc") or "").split())
        resource_id = str(attributes.get("resource-id") or "")
        hint = " ".join(str(attributes.get("hint") or "").split())
        if text or description or resource_id or hint:
            nodes.append(
                {
                    "text": text,
                    "description": description,
                    "resource_id": resource_id,
                    "hint": hint,
                    "class_name": str(attributes.get("class") or ""),
                    "package_name": str(attributes.get("package") or ""),
                    "clickable": str(attributes.get("clickable") or "").lower() == "true",
                    "parent_clickable": parent_attributes.get("clickable") == "true",
                    "parent_resource_id": parent_attributes.get("resource-id", ""),
                }
            )
    return nodes


def _tokens(nodes: list[dict]) -> list[str]:
    values: list[str] = []
    for node in nodes:
        for key in ("text", "description", "hint"):
            value = node.get(key)
            if value:
                values.append(value)
    return values


def _contains_marker(tokens: list[str], markers: tuple[str, ...]) -> bool:
    return any(marker in token for token in tokens for marker in markers)


def _account_visible(tokens: list[str], platform_config: dict, account_identifier: str) -> bool:
    expected = " ".join(str(account_identifier or "").split()).strip()
    if not expected:
        return False
    for token in tokens:
        for prefix in platform_config["account_prefixes"]:
            match = re.fullmatch(rf"{re.escape(prefix)}\s*[：:]\s*(.+)", token, flags=re.IGNORECASE)
            if match and " ".join(match.group(1).split()) == expected:
                return True
    return False


def _discover_account(tokens: list[str], platform_config: dict) -> str | None:
    for token in tokens:
        for prefix in platform_config["account_prefixes"]:
            match = re.fullmatch(rf"{re.escape(prefix)}\s*[：:]\s*(.+)", token, flags=re.IGNORECASE)
            if match:
                value = " ".join(match.group(1).split()).strip()
                if value:
                    return value
    return None


def _classify_page(platform: str, package: str, nodes: list[dict]) -> str:
    config = _PLATFORMS[platform]
    tokens = _tokens(nodes)
    if (
        package in _PERMISSION_PACKAGES
        or _contains_marker(tokens, _PERMISSION_MARKERS)
        and "permission" in package
    ):
        return "permission"
    if _contains_marker(tokens, _CAPTCHA_MARKERS):
        return "captcha"
    if _contains_marker(tokens, _LOGIN_MARKERS):
        return "login"
    if package != config["package"] and package not in _SYSTEM_PICKER_PACKAGES:
        return "unknown"
    editor_hits = sum(1 for marker in config["editor_markers"] if any(marker in token for token in tokens))
    if editor_hits >= 2 and any(
        token in {"发布", "发布笔记", "发作品", "发笔记"} for token in tokens
    ):
        return "editor"
    if (
        package in _SYSTEM_PICKER_PACKAGES
        or sum(1 for marker in _PICKER_MARKERS if any(marker in token for token in tokens)) >= 2
        or "视频" in tokens
        and ("所有照片" in tokens or any(re.fullmatch(r"lp_[0-9a-f]{16}", token) for token in tokens))
    ):
        return "picker"
    if package != config["package"]:
        return "unknown"
    if _discover_account(tokens, config) and any(token in config["self_profile_markers"] for token in tokens):
        return "profile"
    if platform == "xiaohongshu" and {"从相册选择", "拍摄与直播"}.issubset(tokens):
        return "camera"
    if platform == "xiaohongshu" and {"全部", "视频", "照片"}.issubset(tokens):
        return "picker"
    if "相册" in tokens and "拍摄，按钮" in tokens:
        return "camera"
    if any(token in config["profile_labels"] for token in tokens) and any(
        "首页" in token for token in tokens
    ):
        return "home"
    return "unknown"


def _unique_node(nodes: list[dict], labels: tuple[str, ...], *, contains: bool = False) -> dict | None:
    matches = []
    for node in nodes:
        values = (node["text"], node["description"], node["hint"])
        if contains:
            matched = any(label and label in value for label in labels for value in values)
        else:
            matched = any(label and label == value for label in labels for value in values)
        if matched:
            matches.append(node)
    return matches[0] if len(matches) == 1 else None


def _selector(device: Any, node: dict, nodes: list[dict], *, require_clickable: bool = False):
    # Douyin 40.4 exposes navigation labels as non-clickable accessibility buttons.
    # Their bounded label forwards a tap to its parent; don't generalize to arbitrary text.
    semantic_button = node.get("description", "").endswith("，按钮")
    if (
        require_clickable
        and not node.get("clickable")
        and not semantic_button
        and not node.get("parent_clickable")
    ):
        return None
    if require_clickable and not node.get("clickable") and node.get("parent_clickable"):
        parent_id = node.get("parent_resource_id")
        if parent_id and sum(item.get("resource_id") == parent_id for item in nodes) == 1:
            return device(resourceId=parent_id)
    if node.get("resource_id") and sum(item.get("resource_id") == node["resource_id"] for item in nodes) == 1:
        return device(resourceId=node["resource_id"])
    if (
        node.get("resource_id")
        and node.get("clickable")
        and sum(
            item.get("resource_id") == node["resource_id"] and bool(item.get("clickable")) for item in nodes
        )
        == 1
    ):
        return device(resourceId=node["resource_id"], clickable=True)
    if node.get("text") and sum(item.get("text") == node["text"] for item in nodes) == 1:
        return device(text=node["text"])
    if node.get("description") and sum(item.get("description") == node["description"] for item in nodes) == 1:
        return device(description=node["description"])
    return None


def _click_selected(device: Any, selected, *, expected_package: str | None = None) -> None:
    """Use fresh, visible UI bounds and integer pixels; never blind coordinates."""
    try:
        if selected.count != 1:
            raise IntegrationError("DEVICE_CONTROL_AMBIGUOUS", "目标控件已变化或不唯一，已停止操作")
        control = selected.info
        bounds = control["visibleBounds"]
        left, top, right, bottom = (bounds[key] for key in ("left", "top", "right", "bottom"))
        screen = device.info
        current_package = screen.get("currentPackageName")
        if expected_package and not current_package:
            current_package, _activity = _current_app(device)
        if (
            screen.get("screenOn") is False
            or expected_package
            and (control.get("packageName") != expected_package or current_package != expected_package)
            or (control.get("packageName") and current_package and control["packageName"] != current_package)
        ):
            raise IntegrationError("DEVICE_PAGE_CHANGED", "手机已锁屏或切换应用，已停止操作")
        if not (0 <= left < right <= screen["displayWidth"] and 0 <= top < bottom <= screen["displayHeight"]):
            raise IntegrationError("DEVICE_CONTROL_HIDDEN", "控件不在可见屏幕范围内，已停止操作")
        accepted = device.jsonrpc.click(int((left + right) // 2), int((top + bottom) // 2))
        if accepted is not True:
            raise IntegrationError("DEVICE_ACTION_REJECTED", "手机未接受点击事件，请检查设备状态")
    except IntegrationError:
        raise
    except Exception as exc:
        if "INJECT_EVENTS" in str(exc):
            raise IntegrationError(
                "DEVICE_PERMISSION_REQUIRED", "手机未允许模拟操作，请检查 USB 调试（安全设置）"
            ) from None
        raise IntegrationError("DEVICE_ACTION_FAILED", "Android 控件操作失败") from exc


def _click_unique(device: Any, nodes: list[dict], labels: tuple[str, ...]) -> bool:
    candidates = [
        node for node in nodes if any(node.get(key) in labels for key in ("text", "description", "hint"))
    ]
    primary = [
        node for node in candidates if node.get("clickable") or node.get("description", "").endswith("，按钮")
    ]
    actionable = primary or [node for node in candidates if node.get("parent_clickable")]
    node = actionable[0] if len(actionable) == 1 else None
    selected = _selector(device, node, nodes, require_clickable=True) if node else None
    if selected is None:
        return False
    _click_selected(device, selected)
    return True


def _set_caption(
    device: Any,
    nodes: list[dict],
    placeholders: tuple[str, ...],
    caption: str,
    *,
    title: str | None = None,
    description: str | None = None,
) -> bool:
    fields = [node for node in nodes if "EditText" in node.get("class_name", "")]
    node = _unique_node(fields, placeholders, contains=True)
    selected = _selector(device, node, nodes) if node else None
    if selected is None:
        return False
    try:
        title_field = None
        if title is not None:
            title_node = _unique_node(fields, ("添加标题", "填写标题"), contains=True)
            title_field = _selector(device, title_node, nodes) if title_node else None
            if title_field is None or title_field.set_text(title) is False:
                return False
        if selected.set_text(description if description is not None else caption) is False:
            return False
        # uiautomator2 can invalidate a selector immediately after set_text on
        # XHS 9.46. Both callers re-read the hierarchy and compare exact text.
    except Exception as exc:
        if "INJECT_EVENTS" in str(exc):
            raise IntegrationError(
                "DEVICE_PERMISSION_REQUIRED", "手机系统未允许模拟输入，请由用户在开发者选项完成安全调试授权"
            ) from None
        raise IntegrationError("DEVICE_ACTION_FAILED", "无法安全填写发布文案") from exc
    return True


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _iso_now() -> str:
    return datetime.now(UTC).isoformat()


def _base_evidence(
    *,
    serial: str,
    platform: str,
    app_version: str,
    video_sha256: str,
    caption: str,
) -> dict:
    return {
        "serial": serial,
        "platform": platform,
        "app_version": app_version,
        "video_sha256": video_sha256,
        "caption": caption,
    }


def _mark_account_verified(evidence: dict, account_identifier: str, verified_at: str | None = None) -> None:
    evidence["verified_account_identifier"] = account_identifier
    evidence["verified_at"] = verified_at or _iso_now()


def _valid_previous_evidence(
    evidence: dict | None,
    *,
    serial: str,
    platform: str,
    app_version: str,
    account_identifier: str,
    video_sha256: str,
    caption: str,
) -> bool:
    if not isinstance(evidence, dict):
        return False
    expected = {
        "serial": serial,
        "platform": platform,
        "app_version": app_version,
        "verified_account_identifier": account_identifier,
        "video_sha256": video_sha256,
        "caption": caption,
    }
    if any(evidence.get(key) != value for key, value in expected.items()):
        return False
    try:
        verified_at = datetime.fromisoformat(str(evidence["verified_at"]))
        if verified_at.tzinfo is None:
            return False
        age = datetime.now(UTC) - verified_at.astimezone(UTC)
    except (KeyError, TypeError, ValueError, OverflowError):
        return False
    return timedelta(0) <= age <= _EVIDENCE_MAX_AGE


def _expected_remote_video_path(video_sha256: str) -> str:
    if not re.fullmatch(r"[0-9a-f]{64}", video_sha256):
        raise IntegrationError("MEDIA_EVIDENCE_INVALID", "视频校验值无效")
    folder = f"lp_{video_sha256[:16]}"
    return f"/sdcard/Movies/LocalPublisher/{folder}/{folder}.mp4"


def _verify_remote_video_hash(
    serial: str, remote_video_path: str, video_sha256: str
) -> None:
    expected_path = _expected_remote_video_path(video_sha256)
    if remote_video_path != expected_path:
        raise IntegrationError("MEDIA_PATH_MISMATCH", "手机素材路径与已冻结视频不一致")
    remote_hash = _adb(
        serial,
        "shell",
        "toybox",
        "sha256sum",
        expected_path,
        timeout=30,
    ).split()
    if not remote_hash or remote_hash[0].lower() != video_sha256:
        raise IntegrationError("MEDIA_HASH_MISMATCH", "手机素材与已审核成片不一致")


def _valid_operator_media_confirmation(
    evidence: dict | None,
    *,
    platform: str,
    app_version: str,
    video_sha256: str,
) -> bool:
    if not isinstance(evidence, dict):
        return False
    confirmation = evidence.get("operator_media_confirmation")
    if not isinstance(confirmation, dict):
        return False
    if (
        platform != "xiaohongshu"
        or confirmation.get("confirmed") is not True
        or not str(confirmation.get("notes") or "").strip()
        or confirmation.get("video_sha256") != video_sha256
        or confirmation.get("app_version") != app_version
    ):
        return False
    try:
        confirmed_at = datetime.fromisoformat(str(confirmation["confirmed_at"]))
        if confirmed_at.tzinfo is None:
            return False
        age = datetime.now(UTC) - confirmed_at.astimezone(UTC)
    except (KeyError, TypeError, ValueError, OverflowError):
        return False
    return timedelta(0) <= age <= _EVIDENCE_MAX_AGE


def verify_operator_media_selection(
    *,
    serial: str,
    platform: str,
    account_identifier: str,
    expected_app_version: str,
    video_sha256: str,
    caption: str,
    remote_video_path: str,
    previous_evidence: dict,
    app_config: AppConfig,
) -> dict:
    """Read-only proof for an operator-selected XHS 9.46 video at the final editor."""

    serial = _validate_serial(serial)
    if platform != "xiaohongshu":
        raise IntegrationError("MEDIA_CONFIRMATION_PLATFORM_INVALID", "仅小红书支持人工素材确认")
    if expected_app_version != "9.46.0":
        raise IntegrationError("APP_VERSION_UNSUPPORTED", "当前小红书版本不支持此恢复流程")
    config = _platform(platform)
    _require_connected(serial)
    app_version = _package_version(serial, config["package"])
    if app_version != expected_app_version:
        raise IntegrationError("APP_VERSION_CHANGED", "小红书版本已变化，需重新核验素材")
    if not _valid_previous_evidence(
        previous_evidence,
        serial=serial,
        platform=platform,
        app_version=app_version,
        account_identifier=account_identifier,
        video_sha256=video_sha256,
        caption=caption,
    ):
        raise IntegrationError("MEDIA_CONTINUITY_INVALID", "账号或素材连续性证据无效或已过期")
    if (
        previous_evidence.get("reason") != "MEDIA_SELECTION_REQUIRED"
        or previous_evidence.get("page") != "picker"
    ):
        raise IntegrationError("MEDIA_CONFIRMATION_STATE_INVALID", "原任务不在待人工选择素材状态")
    _verify_remote_video_hash(serial, remote_video_path, video_sha256)
    phone = _connect_device(serial)
    if not ensure_unlocked(serial, phone, app_config=app_config):
        raise IntegrationError("DEVICE_USER_ACTION_REQUIRED", "手机尚未解锁，请检查自动解锁设置")
    page, package, _nodes = _guarded_page(phone, platform)
    if page != "editor" or package != config["package"]:
        raise IntegrationError("MEDIA_EDITOR_REQUIRED", "当前前台不是已绑定的小红书最终编辑页")
    return {"video_sha256": video_sha256, "app_version": app_version, "page": "editor"}


def _media_visible(tokens: list[str], video_sha256: str) -> bool:
    fingerprint = video_sha256[:16]
    return any(video_sha256 in token or fingerprint in token for token in tokens)


def _caption_visible(
    tokens: list[str], caption: str, *, title: str | None = None, description: str | None = None
) -> bool:
    if title is not None and description is not None:
        return title in tokens and description in tokens
    clean = " ".join(caption.split())
    return bool(clean) and any(token == clean for token in tokens)


def _attention(reason: str, evidence: dict, *, page: str, detail: str) -> dict:
    return {
        "status": "NEEDS_ATTENTION",
        "evidence": {**evidence, "reason": reason, "page": page, "detail": detail},
    }


def _emit_progress(progress, step: str, percent: int) -> None:
    try:
        progress(step, max(0, min(100, int(percent))))
    except Exception as exc:
        raise IntegrationError("PROGRESS_CALLBACK_FAILED", "更新任务进度失败") from exc


def _is_canceled(canceled) -> bool:
    try:
        return bool(canceled())
    except Exception as exc:
        raise IntegrationError("CANCEL_CALLBACK_FAILED", "检查取消状态失败") from exc


def _transfer_media(serial: str, video_path: Path, cover_path: Path, digest: str) -> tuple[str, str]:
    video_folder = f"/sdcard/Movies/LocalPublisher/lp_{digest[:16]}"
    video_remote = f"{video_folder}/lp_{digest[:16]}.mp4"
    cover_suffix = cover_path.suffix.lower()
    if cover_suffix not in {".jpg", ".jpeg", ".png", ".webp"}:
        raise IntegrationError("INVALID_MEDIA", "封面格式不受支持")
    cover_remote = f"{video_folder}/lp_{digest[:16]}{cover_suffix}"
    _adb(serial, "shell", "mkdir", "-p", video_folder)
    _adb(serial, "push", str(video_path), video_remote, timeout=180)
    _adb(serial, "push", str(cover_path), cover_remote, timeout=60)
    remote_hash = _adb(serial, "shell", "toybox", "sha256sum", video_remote, timeout=30).split()
    if not remote_hash or remote_hash[0] != digest:
        raise IntegrationError("MEDIA_TRANSFER_MISMATCH", "手机中的视频与本地成片不一致，已停止准备")
    for remote_path in (video_remote, cover_remote):
        _adb(
            serial,
            "shell",
            "am",
            "broadcast",
            "-a",
            "android.intent.action.MEDIA_SCANNER_SCAN_FILE",
            "-d",
            f"file://{remote_path}",
        )
    return video_remote, cover_remote


def _unlock_failure_path(serial: str, app_config: AppConfig) -> Path:
    identity = hashlib.sha256(_validate_serial(serial).encode("utf-8")).hexdigest()[:24]
    return app_config.data_dir / "runs" / f"device-unlock-{identity}.failed"


def unlock_failure_latched(serial: str, app_config: AppConfig | None = None) -> bool:
    """Return whether a prior PIN attempt blocks automatic retries."""

    path = _unlock_failure_path(serial, app_config or AppConfig())
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise IntegrationError(
            "DEVICE_UNLOCK_STATE_UNAVAILABLE", "无法核验手机自动解锁安全状态，已停止操作"
        ) from exc
    # Any unexpected object still means blocked. This is deliberately fail closed.
    return True


def clear_unlock_failure(serial: str, app_config: AppConfig | None = None) -> None:
    """Clear the non-secret retry latch after explicit credential lifecycle action."""

    path = _unlock_failure_path(serial, app_config or AppConfig())
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        raise IntegrationError("DEVICE_UNLOCK_STATE_UNAVAILABLE", "无法重置手机自动解锁安全状态") from exc


def _credential_serial(transport_serial: str) -> str:
    """Resolve a stable identity for wireless ADB host:port or mDNS transports."""

    transport_serial = _validate_serial(transport_serial)
    if ":" not in transport_serial and not transport_serial.endswith("._adb-tls-connect._tcp"):
        return transport_serial
    try:
        physical = _adb(
            transport_serial, "shell", "getprop", "ro.serialno", timeout=12
        ).strip()
        return _validate_serial(physical) if physical else transport_serial
    except IntegrationError:
        return transport_serial


def _begin_unlock_attempt(serial: str, app_config: AppConfig) -> None:
    path = _unlock_failure_path(serial, app_config)
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(path, flags, 0o600)
    except FileExistsError:
        raise IntegrationError(
            "DEVICE_UNLOCK_BLOCKED",
            "手机 PIN 曾自动尝试但未确认成功；请重新保存 PIN 后再试",
        ) from None
    except OSError as exc:
        raise IntegrationError(
            "DEVICE_UNLOCK_STATE_UNAVAILABLE", "无法记录手机自动解锁安全状态，未输入 PIN"
        ) from exc
    try:
        payload = f"version=1\nattempt_started_at={datetime.now(UTC).isoformat()}\n".encode("ascii")
        os.write(descriptor, payload)
        os.fsync(descriptor)
        os.fchmod(descriptor, 0o600)
    except OSError as exc:
        # Keep even an incomplete marker: existence prevents an uncontrolled retry.
        raise IntegrationError(
            "DEVICE_UNLOCK_STATE_UNAVAILABLE", "无法确认手机自动解锁安全状态，已禁止自动重试"
        ) from exc
    finally:
        os.close(descriptor)


def _keyguard_showing(serial: str) -> bool:
    output = _adb(serial, "shell", "dumpsys", "window", "policy", timeout=12)
    values = re.findall(r"(?<![A-Za-z])showing\s*=\s*(true|false)\b", output, flags=re.IGNORECASE)
    if not values:
        values = re.findall(r"\bmShowingLockscreen\s*=\s*(true|false)\b", output, flags=re.IGNORECASE)
    normalized = {value.lower() for value in values}
    if len(normalized) != 1:
        raise IntegrationError("DEVICE_LOCK_STATE_UNKNOWN", "无法确认 Android 系统锁屏状态")
    return normalized == {"true"}


def _power_is_asleep(serial: str) -> bool:
    output = _adb(serial, "shell", "dumpsys", "power", timeout=12)
    return bool(
        re.search(r"\bmWakefulness\s*=\s*(?:Asleep|Dozing)\b", output, flags=re.IGNORECASE)
        or re.search(r"\bWakefulness\s*:\s*(?:Asleep|Dozing)\b", output, flags=re.IGNORECASE)
        or re.search(r"\bDisplay Power:\s*state=OFF\b", output, flags=re.IGNORECASE)
    )


def _system_ui_nodes(device: Any) -> list[dict]:
    # Android 16 may report the covered application's activity while keyguard
    # is visible. Verify actual system-window node packages instead.
    for attempt in range(3):
        nodes = _hierarchy(device)
        pin = [node for node in nodes if node.get("resource_id") == _PIN_ENTRY_ID]
        proximity = [
            node
            for node in nodes
            if node.get("resource_id") == _PROXIMITY_TITLE_ID and node.get("text") == "已进入防误触模式"
        ]
        if (len(pin) == 1 and pin[0].get("package_name") == _SYSTEM_UI_PACKAGE) or (
            len(proximity) == 1 and proximity[0].get("package_name") in {"android", _SYSTEM_UI_PACKAGE}
        ):
            return nodes
        if attempt < 2:
            time.sleep(_WAIT_AFTER_SYSTEM_KEY)
    raise IntegrationError("DEVICE_UNLOCK_SCREEN_UNKNOWN", "锁屏页面不是已核验的 Android 系统 PIN 页面")


def _pin_screen_nodes(device: Any, nodes: list[dict]) -> None:
    if device.info.get("currentPackageName") != _SYSTEM_UI_PACKAGE:
        raise IntegrationError("DEVICE_UNLOCK_SCREEN_UNKNOWN", "系统 PIN 页面未处于前台，未输入密码")
    entries = [node for node in nodes if node.get("resource_id") == _PIN_ENTRY_ID]
    if len(entries) != 1 or entries[0].get("text") != "":
        raise IntegrationError("DEVICE_UNLOCK_SCREEN_UNKNOWN", "Android 系统 PIN 输入框状态无法确认")
    for digit in "0123456789":
        resource_id = f"{_SYSTEM_UI_PACKAGE}:id/key{digit}"
        if (
            sum(
                node.get("resource_id") == resource_id and node.get("package_name") == _SYSTEM_UI_PACKAGE
                for node in nodes
            )
            != 1
        ):
            raise IntegrationError("DEVICE_UNLOCK_SCREEN_UNKNOWN", "Android 系统 PIN 数字键状态无法确认")


def _click_pin_digit(device: Any, nodes: list[dict], digit: str) -> None:
    resource_id = f"{_SYSTEM_UI_PACKAGE}:id/key{digit}"
    node = next(item for item in nodes if item.get("resource_id") == resource_id)
    selected = _selector(device, node, nodes)
    if selected is None:
        raise IntegrationError("DEVICE_UNLOCK_SCREEN_UNKNOWN", "Android 系统 PIN 数字键状态无法确认")
    _click_selected(device, selected, expected_package=_SYSTEM_UI_PACKAGE)


def ensure_unlocked(
    serial: str,
    device: Any,
    canceled=None,
    app_config: AppConfig | None = None,
) -> bool:
    """Attempt one exact system PIN unlock, or preserve the existing manual flow."""

    serial = _validate_serial(serial)
    if not _keyguard_showing(serial):
        return True
    app_config = app_config or AppConfig()
    credential_serial = _credential_serial(serial)
    if unlock_failure_latched(credential_serial, app_config):
        raise IntegrationError(
            "DEVICE_UNLOCK_BLOCKED",
            "手机 PIN 曾自动尝试但未确认成功；请重新保存 PIN 后再试",
        )

    if _power_is_asleep(serial):
        _adb(serial, "shell", "input", "keyevent", "KEYCODE_POWER")
        time.sleep(_WAIT_AFTER_POWER)
    _adb(serial, "shell", "input", "keyevent", "KEYCODE_MENU")
    time.sleep(_WAIT_AFTER_SYSTEM_KEY)

    pin = get_device_pin(credential_serial)
    if pin is None:
        return False
    nodes = _system_ui_nodes(device)
    proximity = [
        node
        for node in nodes
        if node.get("resource_id") == _PROXIMITY_TITLE_ID and node.get("text") == "已进入防误触模式"
    ]
    if len(proximity) > 1:
        raise IntegrationError("DEVICE_UNLOCK_SCREEN_UNKNOWN", "Android 防误触页面状态无法确认")
    if proximity:
        _adb(serial, "shell", "input", "keyevent", "KEYCODE_VOLUME_UP")
        time.sleep(_WAIT_AFTER_PROXIMITY)
        _adb(serial, "shell", "input", "keyevent", "KEYCODE_MENU")
        time.sleep(_WAIT_AFTER_SYSTEM_KEY)
        nodes = _system_ui_nodes(device)

    if not _keyguard_showing(serial):
        return True
    _pin_screen_nodes(device, nodes)
    if canceled is not None and _is_canceled(canceled):
        raise CanceledError()

    _begin_unlock_attempt(credential_serial, app_config)
    try:
        for digit in pin:
            if canceled is not None and _is_canceled(canceled):
                raise CanceledError()
            _click_pin_digit(device, nodes, digit)
        time.sleep(_WAIT_AFTER_PIN)
        if _keyguard_showing(serial):
            raise IntegrationError(
                "DEVICE_UNLOCK_FAILED",
                "手机未确认解锁；已禁止自动重试，请核验后重新保存 PIN",
            )
        clear_unlock_failure(credential_serial, app_config)
        return True
    finally:
        pin = ""


def _guarded_page(device: Any, platform: str) -> tuple[str, str, list[dict]]:
    package, _activity = _current_app(device)
    info = getattr(device, "info", None)
    if isinstance(info, dict):
        package = info.get("currentPackageName") or package
        if info.get("screenOn") is False:
            return "locked", package, []
    if package not in {_PLATFORMS[platform]["package"], *_SYSTEM_PICKER_PACKAGES, *_PERMISSION_PACKAGES}:
        return "unknown", package, []
    nodes = _hierarchy(device)
    return _classify_page(platform, package, nodes), package, nodes


def inspect_device(serial: str, platform: str) -> dict:
    """Inspect the foreground target app without navigating or installing setup."""

    serial = _validate_serial(serial)
    platform = str(platform or "").strip().lower()
    config = _platform(platform)
    _require_connected(serial)
    device = _connect_device(serial)
    ensure_unlocked(serial, device)
    app_version = _package_version(serial, config["package"])
    page, package, nodes = _guarded_page(device, platform)
    tokens = _tokens(nodes)
    account_identifier = _discover_account(tokens, config) if page == "profile" else None
    warnings: list[str] = []
    if not app_version:
        warnings.append("未能读取应用版本")
    if package != config["package"]:
        warnings.append("目标应用当前不在前台")
    if page in {"login", "permission", "captcha", "locked"}:
        warnings.append("当前页面需要用户处理，自动化不会操作")
        if page == "locked":
            warnings.append("请在手机端解锁并保持目标应用打开")
    elif page == "unknown":
        warnings.append("当前页面无法安全识别")
    if not account_identifier:
        warnings.append("当前页面未显示可核验的账号标识")
    return {
        "serial": serial,
        "platform": platform,
        "app_version": app_version,
        "account_identifier": account_identifier,
        "ready": bool(app_version and account_identifier and page == "profile"),
        "warnings": warnings,
    }


def _album_video_item(
    device: Any, nodes: list[dict], serial: str, remote: str, size: int, digest: str, canceled,
    *, platform: str = "douyin",
) -> tuple[dict | None, list[dict]]:
    """Isolate one hash-verified video in its own named album before selecting it."""
    folder = f"lp_{digest[:16]}"
    expected = f"/storage/emulated/0/Movies/LocalPublisher/{folder}/{folder}.mp4"
    if remote != expected.replace("/storage/emulated/0", "/sdcard"):
        return None, nodes
    inventory = _adb(
        serial,
        "shell",
        "content",
        "query",
        "--uri",
        "content://media/external/video/media",
        "--projection",
        "_id:_data:_size:mime_type",
        "--where",
        f"_data LIKE '/storage/emulated/0/Movies/LocalPublisher/{folder}/%'",
    ).strip()
    expected_row = rf"Row: 0 _id=\d+, _data={re.escape(expected)}, _size={size}, mime_type=video/mp4"
    if not re.fullmatch(expected_row, inventory):
        return None, nodes
    if _is_canceled(canceled):
        raise CanceledError()
    if platform == "xiaohongshu":
        if folder not in _tokens(nodes):
            headings = [node for node in nodes if node.get("text") == "全部"
                        and node.get("class_name") == "android.widget.TextView"
                        and node.get("parent_clickable")]
            if len(headings) != 1:
                return None, nodes
            _click_selected(device, device(text="全部", className="android.widget.TextView"))
            time.sleep(_WAIT_AFTER_NAVIGATION)
            _page, package, nodes = _guarded_page(device, platform)
            if package != _PLATFORMS[platform]["package"] or not _click_unique(device, nodes, (folder,)):
                return None, nodes
            time.sleep(_WAIT_AFTER_NAVIGATION)
            _page, package, nodes = _guarded_page(device, platform)
        if folder not in _tokens(nodes) or not _click_unique(device, nodes, ("视频",)):
            return None, nodes
        time.sleep(_WAIT_AFTER_NAVIGATION)
        page, package, nodes = _guarded_page(device, platform)
        if page != "picker" or package != _PLATFORMS[platform]["package"] or folder not in _tokens(nodes):
            return None, nodes
        items = [node for node in nodes if node.get("package_name") == package
                 and node.get("class_name") == "android.widget.ImageView"
                 and node.get("clickable") and not node.get("description")]
        durations = [node for node in nodes if node.get("package_name") == package
                     and node.get("class_name") == "android.widget.TextView"
                     and re.fullmatch(r"\d+:\d{2}", node.get("text", ""))]
        return (durations[0] if len(items) == len(durations) == 1 else None), nodes
    if folder not in _tokens(nodes):
        if not _click_unique(device, nodes, ("所有照片",)):
            return None, nodes
        time.sleep(_WAIT_AFTER_NAVIGATION)
        _page, package, nodes = _guarded_page(device, "douyin")
        if package != _PLATFORMS["douyin"]["package"] or not _click_unique(device, nodes, (folder,)):
            return None, nodes
        time.sleep(_WAIT_AFTER_NAVIGATION)
        _page, package, nodes = _guarded_page(device, "douyin")
    if folder not in _tokens(nodes) or not _click_unique(device, nodes, ("视频",)):
        return None, nodes
    time.sleep(_WAIT_AFTER_NAVIGATION)
    page, package, nodes = _guarded_page(device, "douyin")
    if page != "picker" or package != _PLATFORMS["douyin"]["package"] or folder not in _tokens(nodes):
        return None, nodes
    items = [
        node
        for node in nodes
        if node.get("resource_id") == "com.ss.android.ugc.aweme:id/root_view" and node.get("clickable")
    ]
    durations = [
        node
        for node in nodes
        if node.get("resource_id") in {
            "com.ss.android.ugc.aweme:id/gc9", "com.ss.android.ugc.aweme:id/gc="
        }
        and node.get("parent_resource_id") == "com.ss.android.ugc.aweme:id/root_view"
        and re.fullmatch(r"\d+:\d{2}", node.get("text", ""))
    ]
    # A one-item media inventory and a one-item video grid must agree. Never pick
    # "the most recent" entry or infer identity from duration/thumbnail alone.
    return (items[0] if len(items) == len(durations) == 1 else None), nodes


def prepare_upload(
    *,
    serial: str,
    platform: str,
    account_identifier: str,
    video_path: str,
    cover_path: str,
    caption: str,
    progress,
    canceled,
    previous_evidence: dict | None = None,
    title: str | None = None,
    description: str | None = None,
) -> dict:
    """Prepare an upload and stop before the platform's final publish action."""

    if not callable(progress) or not callable(canceled):
        raise IntegrationError("INVALID_CALLBACK", "缺少进度或取消回调")
    serial = _validate_serial(serial)
    platform = str(platform or "").strip().lower()
    config = _platform(platform)
    account_identifier = " ".join(str(account_identifier or "").split()).strip()
    clean_caption = " ".join(str(caption or "").replace("\x00", "").split()).strip()
    clean_title = " ".join(title.split()) if title is not None else None
    clean_description = " ".join(description.split()) if description is not None else None
    if (title is None) != (description is None) or (
        title is not None and f"{clean_title} {clean_description}".strip() != clean_caption
    ):
        raise IntegrationError("INVALID_CAPTION", "标题、正文与审核文案不一致，已停止准备")
    if not account_identifier or len(account_identifier) > 256:
        raise IntegrationError("INVALID_ACCOUNT", "账号标识不能为空且不能超过 256 个字符")
    if not clean_caption or len(clean_caption) > 5_000:
        raise IntegrationError("INVALID_CAPTION", "发布文案不能为空且不能超过 5,000 个字符")
    try:
        video = Path(video_path).expanduser()
        cover = Path(cover_path).expanduser()
    except TypeError as exc:
        raise IntegrationError("INVALID_MEDIA", "待发布素材路径无效") from exc
    if not video.is_file() or not cover.is_file():
        raise IntegrationError("MEDIA_NOT_FOUND", "找不到待发布视频或封面")
    video = video.resolve()
    cover = cover.resolve()
    if _is_canceled(canceled):
        raise CanceledError()

    _require_connected(serial)
    device = _connect_device(serial)
    ensure_unlocked(serial, device, canceled)
    app_version = _package_version(serial, config["package"])
    if not app_version:
        raise IntegrationError("APP_VERSION_UNKNOWN", "无法核验目标 App 版本")
    video_sha256 = _sha256(video)
    evidence = _base_evidence(
        serial=serial,
        platform=platform,
        app_version=app_version,
        video_sha256=video_sha256,
        caption=clean_caption,
    )
    prior_valid = _valid_previous_evidence(
        previous_evidence,
        serial=serial,
        platform=platform,
        app_version=app_version,
        account_identifier=account_identifier,
        video_sha256=video_sha256,
        caption=clean_caption,
    )
    operator_media_confirmed = _valid_operator_media_confirmation(
        previous_evidence,
        platform=platform,
        app_version=app_version,
        video_sha256=video_sha256,
    )
    if operator_media_confirmed:
        evidence["operator_media_confirmation"] = dict(
            previous_evidence["operator_media_confirmation"]
        )
    _emit_progress(progress, "检查设备与账号", 5)
    page, package, nodes = _guarded_page(device, platform)
    tokens = _tokens(nodes)
    if page in {"login", "permission", "captcha", "locked"}:
        return _attention(page.upper(), evidence, page=page, detail="请用户完成当前页面后再继续准备")

    # Older runs stopped at the chapter introduction before recording the selected
    # hash; that exact post-selection result and visible introduction prove the stage.
    resumed_selected_media = prior_valid and previous_evidence.get("page") == page and (
        previous_evidence.get("selected_video_sha256") == video_sha256
        or platform == "xiaohongshu" and app_version == "9.47.0"
        and previous_evidence.get("reason") == "UNKNOWN_PAGE"
        and previous_evidence.get("detail") == "素材选择后的下一步控件无法唯一识别"
        and "添加章节，在视频上划重点！" in tokens
    )
    if resumed_selected_media:
        evidence["selected_video_sha256"] = video_sha256
        _mark_account_verified(evidence, account_identifier, str(previous_evidence["verified_at"]))
        for _ in range(3):
            if page == "editor":
                break
            if _is_canceled(canceled):
                raise CanceledError()
            labels = (("我知道了",) if "添加章节，在视频上划重点！" in tokens else _NEXT_LABELS)
            if package != config["package"] or not _click_unique(device, nodes, labels):
                return _attention("UNKNOWN_PAGE", evidence, page=page, detail="已选素材的编辑流程需要核验")
            time.sleep(_WAIT_AFTER_NAVIGATION)
            page, package, nodes = _guarded_page(device, platform)
            tokens = _tokens(nodes)
            if page in {"login", "permission", "captcha", "locked"}:
                return _attention(page.upper(), evidence, page=page, detail="请用户完成当前页面后再继续准备")

    account_verified_now = page == "profile" and _account_visible(tokens, config, account_identifier)
    continuous_account_verification = False
    if not account_verified_now and page == "home":
        if not _click_unique(device, nodes, config["profile_labels"]):
            return _attention(
                "ACCOUNT_VERIFICATION_REQUIRED",
                evidence,
                page=page,
                detail="请手动进入账号主页以显示账号标识",
            )
        time.sleep(_WAIT_AFTER_NAVIGATION)
        page, package, nodes = _guarded_page(device, platform)
        tokens = _tokens(nodes)
        account_verified_now = page == "profile" and _account_visible(tokens, config, account_identifier)
    resumed_camera = prior_valid and page == "camera" and previous_evidence.get("page") == "camera"
    if account_verified_now or prior_valid and page in {"picker", "editor"} or resumed_camera:
        continuous_account_verification = True
    else:
        return _attention(
            "ACCOUNT_MISMATCH" if _discover_account(tokens, config) else "ACCOUNT_VERIFICATION_REQUIRED",
            evidence,
            page=page,
            detail="当前页面未显示完全一致的目标账号，且没有有效的连续性证据",
        )
    if account_verified_now:
        _mark_account_verified(evidence, account_identifier)
    elif prior_valid:
        _mark_account_verified(evidence, account_identifier, str(previous_evidence["verified_at"]))

    if page == "editor":
        if not prior_valid and not account_verified_now:
            return _attention(
                "ACCOUNT_VERIFICATION_REQUIRED",
                evidence,
                page=page,
                detail="编辑页未显示目标账号，恢复需要有效的先前核验记录",
            )
        if operator_media_confirmed:
            _verify_remote_video_hash(
                serial,
                str(previous_evidence.get("remote_video_path") or ""),
                video_sha256,
            )
        if not operator_media_confirmed and not resumed_selected_media and not _media_visible(tokens, video_sha256):
            return _attention(
                "MEDIA_EVIDENCE_MISSING",
                evidence,
                page=page,
                detail="编辑页未显示所选视频的唯一指纹，未执行任何发布操作",
            )
        if not _caption_visible(tokens, clean_caption, title=clean_title, description=clean_description):
            if not account_verified_now and not operator_media_confirmed and not resumed_selected_media:
                return _attention(
                    "CAPTION_ENTRY_REQUIRED",
                    evidence,
                    page=page,
                    detail="恢复到编辑页后只允许复核，请手动输入并保留完全一致的文案",
                )
            if not _set_caption(
                device,
                nodes,
                config["caption_placeholders"],
                clean_caption,
                title=clean_title,
                description=clean_description,
            ):
                return _attention(
                    "CAPTION_ENTRY_REQUIRED", evidence, page=page, detail="请手动输入并保留完全一致的文案"
                )
            time.sleep(_WAIT_AFTER_NAVIGATION)
            page, package, nodes = _guarded_page(device, platform)
            tokens = _tokens(nodes)
            if (
                page != "editor"
                or package != config["package"]
                or not _caption_visible(
                    tokens, clean_caption, title=clean_title, description=clean_description
                )
            ):
                return _attention(
                    "CAPTION_UNVERIFIED",
                    evidence,
                    page=page,
                    detail="输入后的文案无法从界面精确复核",
                )
        _emit_progress(progress, "等待用户最终确认", 100)
        return {
            "status": "WAITING_USER",
            "evidence": {**evidence, "page": "editor", "final_publish_clicked": False},
        }

    if _is_canceled(canceled):
        raise CanceledError()
    _emit_progress(progress, "传输素材", 20)
    video_remote, cover_remote = _transfer_media(serial, video, cover, video_sha256)
    evidence.update({"remote_video_path": video_remote, "remote_cover_path": cover_remote})

    if page == "profile" and continuous_account_verification:
        if not _click_unique(device, nodes, config["create_labels"]):
            return _attention(
                "CREATE_ENTRY_REQUIRED",
                evidence,
                page=page,
                detail="请手动打开发布入口；自动化未找到唯一的创作控件",
            )
        time.sleep(_WAIT_AFTER_NAVIGATION)
        page, package, nodes = _guarded_page(device, platform)
        tokens = _tokens(nodes)
    if (
        page == "camera"
        and (
            platform == "douyin"
            and app_version in {"40.4.0", "40.5.0"}
            and _click_unique(device, nodes, ("相册",))
            or platform == "xiaohongshu"
            and app_version in {"9.46.0", "9.47.0"}
            and _click_unique(device, nodes, ("从相册选择",))
        )
    ):
        time.sleep(_WAIT_AFTER_NAVIGATION)
        page, package, nodes = _guarded_page(device, platform)
        tokens = _tokens(nodes)
    if page in {"login", "permission", "captcha", "locked"}:
        return _attention(page.upper(), evidence, page=page, detail="请用户完成当前页面后再继续准备")
    if page != "picker":
        return _attention(
            "MEDIA_SELECTION_REQUIRED",
            evidence,
            page=page,
            detail="请用户选择已传输且文件名含唯一指纹的素材，然后继续准备",
        )

    fingerprint = video_sha256[:16]
    _emit_progress(progress, "定位相册并核验唯一视频", 40)
    if continuous_account_verification and (
        platform == "douyin" and app_version in {"40.4.0", "40.5.0"}
        or platform == "xiaohongshu" and app_version == "9.47.0"
    ):
        media_node, nodes = _album_video_item(
            device, nodes, serial, video_remote, video.stat().st_size, video_sha256, canceled,
            platform=platform,
        )
    else:
        media_node = _unique_node(nodes, (f"lp_{fingerprint}.mp4",), contains=True)
    if media_node is None:
        return _attention(
            "MEDIA_SELECTION_REQUIRED",
            evidence,
            page=page,
            detail="媒体选择器未显示唯一的视频指纹，请用户手动核对选择",
        )
    prior_picker_continuity = (
        account_verified_now
        or not prior_valid
        or previous_evidence.get("page") in {"camera", "picker"}
        and previous_evidence.get("remote_video_path") == video_remote
    )
    if not prior_picker_continuity:
        return _attention(
            "MEDIA_SELECTION_REQUIRED",
            evidence,
            page=page,
            detail="上一轮证据无法证明当前媒体选择器与已核验账号连续",
        )
    if platform == "xiaohongshu" and app_version == "9.47.0" and continuous_account_verification:
        # XHS obfuscates ids and exposes no description for the video thumbnail.
        # The single duration label lies inside the inventory-verified video tile.
        selected = device(className="android.widget.TextView", text=media_node["text"])
    else:
        selected = _selector(device, media_node, nodes, require_clickable=True)
    if selected is None:
        return _attention(
            "MEDIA_SELECTION_REQUIRED", evidence, page=page, detail="唯一媒体条目没有可用的资源选择器"
        )
    _click_selected(device, selected)
    evidence["selected_video_sha256"] = video_sha256
    _emit_progress(progress, "已选择唯一素材", 55)
    time.sleep(_WAIT_AFTER_NAVIGATION)

    media_selected_in_call = True
    for _transition in range(3):
        if _is_canceled(canceled):
            raise CanceledError()
        page, package, nodes = _guarded_page(device, platform)
        tokens = _tokens(nodes)
        if page in {"login", "permission", "captcha", "locked"}:
            return _attention(page.upper(), evidence, page=page, detail="请用户完成当前页面后再继续准备")
        if page == "editor":
            break
        labels = (("我知道了",) if platform == "xiaohongshu"
                  and "添加章节，在视频上划重点！" in tokens else _NEXT_LABELS)
        if not _click_unique(device, nodes, labels):
            return _attention(
                "UNKNOWN_PAGE",
                evidence,
                page=page,
                detail="素材选择后的下一步控件无法唯一识别",
            )
        time.sleep(_WAIT_AFTER_NAVIGATION)
    if page != "editor" or package != config["package"]:
        return _attention("UNKNOWN_PAGE", evidence, page=page, detail="未能安全到达最终编辑页")

    tokens = _tokens(nodes)
    media_proven = media_selected_in_call or _media_visible(tokens, video_sha256)
    if not media_proven:
        return _attention("MEDIA_EVIDENCE_MISSING", evidence, page=page, detail="无法复核所选视频")
    if not _caption_visible(tokens, clean_caption, title=clean_title, description=clean_description):
        if not _set_caption(
            device,
            nodes,
            config["caption_placeholders"],
            clean_caption,
            title=clean_title,
            description=clean_description,
        ):
            return _attention(
                "CAPTION_ENTRY_REQUIRED", evidence, page=page, detail="请手动输入并保留完全一致的文案"
            )
        time.sleep(_WAIT_AFTER_NAVIGATION)
        page, package, nodes = _guarded_page(device, platform)
        tokens = _tokens(nodes)
        if (
            page != "editor"
            or package != config["package"]
            or not _caption_visible(tokens, clean_caption, title=clean_title, description=clean_description)
        ):
            return _attention(
                "CAPTION_UNVERIFIED", evidence, page=page, detail="输入后的文案无法从界面精确复核"
            )

    _emit_progress(progress, "等待用户最终确认", 100)
    return {
        "status": "WAITING_USER",
        "evidence": {**evidence, "page": "editor", "final_publish_clicked": False},
    }


def _validate_work_url(platform: str, work_url: str) -> str:
    if not isinstance(work_url, str) or len(work_url) > 2_048:
        raise IntegrationError("INVALID_WORK_URL", "作品地址无效或过长")
    try:
        parsed = urlsplit(str(work_url or "").strip())
    except ValueError as exc:
        raise IntegrationError("INVALID_WORK_URL", "作品地址无效") from exc
    host = (parsed.hostname or "").lower().rstrip(".")
    allowed_hosts = _PLATFORMS[platform]["hosts"]
    if parsed.scheme != "https" or parsed.username is not None or parsed.password is not None:
        raise IntegrationError("INVALID_WORK_URL", "作品地址必须是平台 HTTPS 地址")
    if host not in allowed_hosts:
        raise IntegrationError("INVALID_WORK_URL", "作品地址不属于所选平台")
    return parsed.geturl()


def _work_identifier_visible(tokens: list[str], work_id: str) -> bool:
    if not work_id:
        return False
    for token in tokens:
        if token == work_id:
            return True
        for prefix in ("作品 ", "笔记 ", "视频 "):
            if token.startswith(prefix) and token[len(prefix) :].strip() == work_id:
                return True
        match = re.fullmatch(r"(?:作品|笔记|视频)?\s*(?:ID|id|Id)\s*[：:]?\s*(\S+)", token)
        if match and match.group(1) == work_id:
            return True
    return False


def _metric_number(raw: str) -> int | None:
    clean = raw.strip().replace(",", "").replace("，", "").rstrip("+")
    match = re.fullmatch(r"(\d+(?:\.\d+)?)\s*(万|w|W|k|K|千|亿)?", clean)
    if not match:
        return None
    multipliers = {
        None: 1,
        "万": 10_000,
        "w": 10_000,
        "W": 10_000,
        "k": 1_000,
        "K": 1_000,
        "千": 1_000,
        "亿": 100_000_000,
    }
    try:
        value = Decimal(match.group(1)) * multipliers[match.group(2)]
        return int(value.to_integral_value(rounding=ROUND_DOWN))
    except (InvalidOperation, ValueError, OverflowError):
        return None


def _parse_metrics(tokens: list[str]) -> tuple[dict[str, int | None], dict[str, str]]:
    labels = {
        "views": ("播放", "观看", "浏览", "阅读"),
        "likes": ("点赞", "获赞", "赞"),
        "comments": ("评论",),
        "favorites": ("收藏",),
        "shares": ("分享", "转发"),
    }
    values: dict[str, int | None] = {key: None for key in labels}
    raw_evidence: dict[str, str] = {}
    number_pattern = r"\d+(?:[.,]\d+)*(?:\.\d+)?\s*(?:万|[wWkK]|千|亿)?\+?"
    for key, key_labels in labels.items():
        candidates: list[str] = []
        for index, token in enumerate(tokens):
            for label in key_labels:
                combined = re.search(rf"{re.escape(label)}(?:量|次数)?\s*[：:]?\s*({number_pattern})", token)
                if combined:
                    candidates.append(combined.group(1))
                reverse = re.search(rf"({number_pattern})\s*{re.escape(label)}", token)
                if reverse:
                    candidates.append(reverse.group(1))
                if token in {label, f"{label}量", f"{label}数"} and index + 1 < len(tokens):
                    candidates.append(tokens[index + 1])
        parsed = [(candidate, _metric_number(candidate)) for candidate in candidates]
        parsed = [(raw, value) for raw, value in parsed if value is not None]
        unique_values = {value for _raw, value in parsed}
        if len(unique_values) == 1:
            raw, value = parsed[0]
            values[key] = value
            raw_evidence[key] = raw
    return values, raw_evidence


def _douyin_work_link(device: Any, nodes: list[dict], fallback_url: str) -> tuple[str, list[dict]]:
    from ..services import normalized_work

    if not _click_unique(device, nodes, ("更多，按钮",)):
        raise IntegrationError("WORK_LINK_UNAVAILABLE", "未找到作品链接入口，请在手机打开目标作品")
    time.sleep(_WAIT_AFTER_NAVIGATION)
    _page, package, nodes = _guarded_page(device, "douyin")
    if package != _PLATFORMS["douyin"]["package"]:
        raise IntegrationError("DEVICE_PAGE_CHANGED", "手机已切换应用，停止核验作品")
    marker = "LocalPublisher:verifying-work-link"
    try:
        device.clipboard = marker
        if not _click_unique(device, nodes, ("分享链接", "复制链接")):
            raise IntegrationError("WORK_LINK_UNAVAILABLE", "未找到唯一的复制链接控件")
        time.sleep(_WAIT_AFTER_NAVIGATION)
        links = re.findall(
            r"https://(?:v\.douyin\.com|www\.douyin\.com|www\.iesdouyin\.com)/[^\s]+", device.clipboard or ""
        )
        if len(links) != 1:
            raise IntegrationError("WORK_LINK_UNVERIFIED", "没有读到新的作品链接，未采集数字")
        _, work_id = normalized_work("douyin", links[0], None)
    except IntegrationError:
        raise
    except Exception as exc:
        raise IntegrationError("WORK_LINK_UNVERIFIED", "无法核验当前作品链接，未采集数字") from exc
    finally:
        try:
            if device.clipboard == marker:
                device.clipboard = fallback_url
        except Exception as cleanup_error:  # noqa: BLE001 - preserve the original RPC failure
            log.warning("Could not restore temporary clipboard marker: %s", type(cleanup_error).__name__)
    _page, package, nodes = _guarded_page(device, "douyin")
    if "分享给" in _tokens(nodes) and _click_unique(device, nodes, ("取消",)):
        time.sleep(_WAIT_AFTER_NAVIGATION)
        _page, package, nodes = _guarded_page(device, "douyin")
    if package != _PLATFORMS["douyin"]["package"]:
        raise IntegrationError("DEVICE_PAGE_CHANGED", "手机已切换应用，停止采集")
    return work_id, nodes


def _douyin_visible_counts(nodes: list[dict]) -> tuple[dict, dict]:
    # Only native counter widgets are evidence. A caption saying "播放100万"
    # must never become a metric. Word-only labels such as "赞" stay unknown.
    curated = []
    for node in nodes:
        label = None
        if node.get("resource_id") == "com.ss.android.ugc.aweme:id/gt=":
            label = "点赞"
        elif node.get("resource_id") == "com.ss.android.ugc.aweme:id/d=i":
            label = "收藏"
        elif node.get("parent_resource_id") == "com.ss.android.ugc.aweme:id/eye" and "TextView" in node.get(
            "class_name", ""
        ):
            label = "评论"
        if label and _metric_number(node.get("text", "")) is not None:
            curated.append(f"{label} {node['text']}")
    return _parse_metrics(curated)


def collect_metrics(
    *,
    serial: str,
    platform: str,
    account_identifier: str,
    work_url: str,
    work_id: str,
    content_title: str,
) -> dict:
    """Read visible metrics after exact account and publication verification."""

    serial = _validate_serial(serial)
    platform = str(platform or "").strip().lower()
    config = _platform(platform)
    expected_account = " ".join(str(account_identifier or "").split()).strip()
    expected_work_id = " ".join(str(work_id or "").split()).strip()
    expected_title = " ".join(str(content_title or "").split()).strip()
    if not expected_account or len(expected_account) > 256:
        raise IntegrationError("INVALID_ACCOUNT", "账号标识不能为空且不能超过 256 个字符")
    if len(expected_work_id) > 256 or len(expected_title) > 500:
        raise IntegrationError("INVALID_WORK", "作品 ID 或标题过长")
    if not expected_work_id and not expected_title:
        raise IntegrationError("INVALID_WORK", "请填写作品 ID 或标题")
    work_url = _validate_work_url(platform, work_url)
    _require_connected(serial)
    device = _connect_device(serial)
    ensure_unlocked(serial, device)
    page, package, nodes = _guarded_page(device, platform)
    tokens = _tokens(nodes)
    if page in {"login", "permission", "captcha", "locked"}:
        raise IntegrationError("DEVICE_USER_ACTION_REQUIRED", "当前设备页面需要用户处理")
    if page != "profile" or not _account_visible(tokens, config, expected_account):
        raise IntegrationError("ACCOUNT_MISMATCH", "当前 App 账号与目标账号不完全一致")

    calibrated = (
        platform == "douyin"
        and _package_version(serial, config["package"]) == "40.4.0"
        and bool(re.fullmatch(r"\d{5,30}", expected_work_id))
    )
    target_uri = f"snssdk1128://aweme/detail/{expected_work_id}" if calibrated else work_url
    if calibrated:
        handler = _adb(
            serial,
            "shell",
            "cmd",
            "package",
            "resolve-activity",
            "--brief",
            "-a",
            "android.intent.action.VIEW",
            "-d",
            target_uri,
            "-p",
            config["package"],
        )
        if f"{config['package']}/" not in handler:
            raise IntegrationError("WORK_LINK_UNSUPPORTED", "当前 App 未提供可用的作品打开入口")
    _adb(
        serial,
        "shell",
        "am",
        "start",
        "-a",
        "android.intent.action.VIEW",
        "-d",
        target_uri,
        "-p",
        config["package"],
        timeout=20,
    )
    time.sleep(_WAIT_AFTER_NAVIGATION)
    page, package, nodes = _guarded_page(device, platform)
    tokens = _tokens(nodes)
    if page in {"login", "permission", "captcha"} or package != config["package"]:
        raise IntegrationError("DEVICE_USER_ACTION_REQUIRED", "无法安全打开作品页面，请用户检查设备")
    if calibrated:
        observed_work_id, nodes = _douyin_work_link(device, nodes, work_url)
        work_verified = observed_work_id == expected_work_id
    else:
        work_verified = _work_identifier_visible(tokens, expected_work_id)
        if expected_title:
            work_verified = work_verified or any(expected_title == token for token in tokens)
    if not work_verified:
        raise IntegrationError("WORK_MISMATCH", "当前页面作品与目标作品不完全一致")

    values, raw_values = _douyin_visible_counts(nodes) if calibrated else _parse_metrics(tokens)
    return {
        **values,
        "raw": {
            "work_url": work_url,
            "work_id": expected_work_id,
            "content_title": expected_title,
            "observed_values": raw_values,
            "page": page,
            "verification": "device_share_link" if calibrated else "visible_work_identifier_or_title",
        },
    }
