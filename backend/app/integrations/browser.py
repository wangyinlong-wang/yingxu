"""Local browser-use subprocess boundary; the backend keeps all durable job state."""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

import httpx

from ..config import AppConfig
from .errors import CanceledError, IntegrationError

VERSION = "0.13.10"
PLATFORMS = {"douyin", "xiaohongshu"}
RUNTIME = Path(__file__).with_name("browser_runtime.py")


def runtime_status(config: AppConfig) -> dict:
    version = None
    # Reading package metadata does not import browser-use or start a Chrome process.
    root = config.browser_use_python.parent.parent / "lib"
    for metadata in root.glob("python*/site-packages/browser_use-*.dist-info/METADATA"):
        for line in metadata.read_text().splitlines():
            if line.startswith("Version: "):
                version = line.removeprefix("Version: ")
                break
    installed = config.browser_use_python.is_file() and version == VERSION
    chrome = config.chrome_path.is_file() and os.access(config.chrome_path, os.X_OK)
    return {
        "installed": installed, "version": version, "chrome_available": chrome,
        "ready": installed and chrome,
        "message": "本地 browser-use 与 Chrome 已就绪" if installed and chrome else (
            "请运行 ./scripts/setup_browser_use.sh 安装锁定版本" if not installed
            else "未找到 Google Chrome，请安装或配置 PUBLISHER_CHROME_PATH"
        ),
    }


def _endpoint(profile: Path) -> str | None:
    try:
        lines = (profile / "DevToolsActivePort").read_text().splitlines()
        port = int(lines[0])
        if not 0 < port < 65536 or not lines[1].startswith("/devtools/browser/"):
            return None
        with httpx.Client(trust_env=False, timeout=1) as client:
            response = client.get(f"http://127.0.0.1:{port}/json/version")
            response.raise_for_status()
            if response.json().get("webSocketDebuggerUrl") != f"ws://127.0.0.1:{port}{lines[1]}":
                return None
        return f"http://127.0.0.1:{port}"
    except (OSError, ValueError, IndexError, httpx.HTTPError):
        return None


def _chrome(config: AppConfig, platform: str, *, launch: bool) -> tuple[Path, str]:
    if platform not in PLATFORMS:
        raise IntegrationError("BROWSER_PLATFORM_INVALID", "不支持的浏览器平台")
    state = runtime_status(config)
    if not state["ready"]:
        raise IntegrationError("BROWSER_NOT_INSTALLED", state["message"])
    profile = (config.data_dir / "browser" / platform).resolve()
    endpoint = _endpoint(profile)
    if not endpoint and launch:
        profile.mkdir(parents=True, exist_ok=True, mode=0o700)
        profile.chmod(0o700)
        subprocess.Popen(
            [str(config.chrome_path), f"--user-data-dir={profile}", "--remote-debugging-port=0",
             "--remote-debugging-address=127.0.0.1", "--enable-automation", "--no-first-run",
             "--no-default-browser-check", "--disable-background-networking", "--disable-sync", "about:blank"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and not endpoint:
            time.sleep(0.25)
            endpoint = _endpoint(profile)
    if not endpoint:
        raise IntegrationError("BROWSER_NOT_RUNNING", "请在发布中心打开对应平台的 Chrome 并完成登录")
    return profile, endpoint


def run_browser(config: AppConfig, platform: str, action: str, *, canceled=None, **payload) -> dict:
    if canceled and canceled():
        raise CanceledError()
    profile, endpoint = _chrome(config, platform, launch=action in {"open", "prepare"})
    request = {**payload, "action": action, "platform": platform, "profile": str(profile), "endpoint": endpoint}
    env = {key: os.environ[key] for key in ("PATH", "HOME", "TMPDIR", "LANG", "SYSTEMROOT") if key in os.environ}
    env.update(ANONYMIZED_TELEMETRY="false", BROWSER_USE_CLOUD_SYNC="false",
               BROWSER_USE_LOGGING_LEVEL="error", CDP_LOGGING_LEVEL="ERROR", NO_PROXY="*")
    # No model keys, browser cookies, or runtime diagnostics enter the job result/log.
    process = subprocess.Popen(
        [str(config.browser_use_python), str(RUNTIME)], cwd=profile, env=env,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
    )
    deadline = time.monotonic() + (240 if action == "prepare" else 80)
    first = True
    try:
        while True:
            try:
                output, _ = process.communicate(json.dumps(request) if first else None, timeout=0.3)
                break
            except subprocess.TimeoutExpired:
                first = False
                if canceled and canceled():
                    raise CanceledError()
                if time.monotonic() >= deadline:
                    raise IntegrationError("BROWSER_TIMEOUT", "浏览器操作超时，请检查 Chrome 现场；不会自动重试")
        records = [line.removeprefix("BROWSER_RESULT=") for line in output.splitlines()
                   if line.startswith("BROWSER_RESULT=")]
        if len(records) != 1:
            raise IntegrationError("BROWSER_RUNTIME_FAILED", "browser-use 运行异常，请检查安装与 Chrome 现场")
        result = json.loads(records[0])
        if result.get("error"):
            raise IntegrationError(result["error"]["code"], result["error"]["message"])
        if process.returncode:
            raise IntegrationError("BROWSER_RUNTIME_FAILED", "browser-use 运行未正常结束，请检查 Chrome 现场")
        return result
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate()


def prepare_upload(*, app_config, platform, serial, progress, canceled, **kwargs):
    progress("核验 Chrome 账号并上传审核成片", 15)
    return run_browser(app_config, platform, "prepare", canceled=canceled, **kwargs)


def submit_publication(*, app_config, platform, serial, record_submit, canceled, **kwargs):
    if canceled():
        raise CanceledError()
    # Persist BEFORE crossing the process boundary, including startup errors/timeouts.
    # Even a lost response cannot cause automatic replay of a possible final click.
    record_submit("intent")
    result = run_browser(app_config, platform, "submit", canceled=canceled, **kwargs)
    if result.get("evidence", {}).get("final_publish_clicked"):
        record_submit("clicked")
    return result


def detect_published_work(*, app_config, platform, serial, canceled, **kwargs):
    return run_browser(app_config, platform, "detect", canceled=canceled, **kwargs)


def collect_metrics(*, app_config, platform, serial, canceled=None, **kwargs):
    return run_browser(app_config, platform, "metrics", canceled=canceled, **kwargs)
