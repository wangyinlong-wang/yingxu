#!/usr/bin/env python3
"""Local setup/start/stop/status/backup tooling. Never manages unrelated processes."""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
PYTHON = BACKEND / ".venv" / "bin" / "python"
REQUIRED_SERVICES = {"api", "content", "device"}


def run(arguments: list[str], cwd: Path = ROOT, *, capture=False):
    return subprocess.run(arguments, cwd=cwd, check=True, text=True, capture_output=capture)


def version_tuple(value: str) -> tuple[int, ...]:
    match = re.search(r"\d+(?:\.\d+)+", value)
    return tuple(int(part) for part in match.group().split(".")) if match else ()


def command_probe(command: str, arguments: list[str]) -> dict:
    executable = shutil.which(command)
    if not executable:
        return {"available": False, "path": None, "version": ""}
    try:
        result = subprocess.run(
            [executable, *arguments], capture_output=True, text=True, timeout=10, check=False
        )
    except (OSError, subprocess.SubprocessError) as error:
        return {
            "available": False,
            "path": executable,
            "version": f"检测失败：{type(error).__name__}",
        }
    output = "\n".join(part.strip() for part in (result.stdout, result.stderr) if part.strip())
    return {
        "available": result.returncode == 0,
        "path": executable,
        "version": output.splitlines()[0] if output else "",
        "output": output,
    }


def configured_adb_path() -> Path | None:
    """Read only the dedicated ADB override; never parse or expose model credentials."""
    raw = os.environ.get("PUBLISHER_ADB_PATH", "").strip()
    env_file = ROOT / ".env"
    if not raw and env_file.is_file():
        try:
            for line in env_file.read_text(encoding="utf-8").splitlines():
                match = re.fullmatch(r"\s*PUBLISHER_ADB_PATH\s*=\s*(.*?)\s*", line)
                if match:
                    raw = match.group(1).strip()
                    break
        except OSError:
            return None
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in {'"', "'"}:
        raw = raw[1:-1]
    if not raw:
        return None
    candidate = Path(raw).expanduser()
    return candidate if candidate.is_file() and os.access(candidate, os.X_OK) else None


def environment_report() -> dict:
    checks = []

    def record(name: str, ok: bool, detail: str, *, required=True):
        checks.append({"name": name, "ok": ok, "required": required, "detail": detail})

    python_version = platform.python_version()
    record(
        "Python",
        (3, 11) <= sys.version_info[:2] < (3, 14),
        f"{python_version} ({sys.executable})，要求 >=3.11,<3.14",
    )
    record("操作系统", os.name == "posix", f"{platform.system()} {platform.machine()}，要求 POSIX 系统")

    probes = {
        "uv": command_probe("uv", ["--version"]),
        "Node.js": command_probe("node", ["--version"]),
        "pnpm": command_probe("pnpm", ["--version"]),
        "FFmpeg": command_probe("ffmpeg", ["-version"]),
        "ffprobe": command_probe("ffprobe", ["-version"]),
    }
    for name in ("uv", "pnpm", "ffprobe"):
        value = probes[name]
        record(name, value["available"], value["version"] or "未找到", required=True)
    node = probes["Node.js"]
    node_version = version_tuple(node["version"])
    record(
        "Node.js",
        node["available"] and bool(node_version) and node_version[0] >= 22,
        f"{node['version'] or '未找到'}，要求 >=22",
    )
    ffmpeg = probes["FFmpeg"]
    record("FFmpeg", ffmpeg["available"], ffmpeg["version"] or "未找到")
    record(
        "FFmpeg/libass",
        ffmpeg["available"] and "--enable-libass" in ffmpeg.get("output", ""),
        "已启用 libass" if "--enable-libass" in ffmpeg.get("output", "") else "未检测到 libass 字幕支持",
    )

    record("后端锁文件", (BACKEND / "uv.lock").is_file(), str(BACKEND / "uv.lock"))
    record("前端锁文件", (ROOT / "frontend" / "pnpm-lock.yaml").is_file(), str(ROOT / "frontend" / "pnpm-lock.yaml"))
    record("环境模板", (ROOT / ".env.example").is_file(), str(ROOT / ".env.example"))
    free_bytes = shutil.disk_usage(ROOT).free
    record(
        "可用磁盘",
        free_bytes >= 2 * 1024**3,
        f"{free_bytes / 1024**3:.1f} GiB，部署至少预留 2 GiB",
    )

    bundled_adb = ROOT / ".tools" / "platform-tools" / "adb"
    configured_adb = configured_adb_path()
    adb = command_probe(
        str(configured_adb or bundled_adb)
        if configured_adb or bundled_adb.is_file()
        else "adb",
        ["version"],
    )
    adb_version = version_tuple(adb["version"])
    adb_supported = adb["available"] and adb_version >= (1, 0, 41)
    adb_detail = adb["version"] or "未找到；仅影响安卓发布"
    if adb["available"] and not adb_supported:
        adb_detail += "；版本过旧，要求 Android Debug Bridge >=1.0.41"
    record(
        "ADB（可选）",
        adb_supported,
        adb_detail,
        required=False,
    )
    record(
        ".env（可选）",
        (ROOT / ".env").is_file(),
        "已存在" if (ROOT / ".env").is_file() else "部署时将从 .env.example 创建，模型密钥仍需本机填写",
        required=False,
    )
    blockers = [item for item in checks if item["required"] and not item["ok"]]
    return {"ok": not blockers, "checks": checks, "blockers": blockers}


def print_environment(report: dict) -> None:
    print("环境检测：" + ("PASS" if report["ok"] else "BLOCK"))
    for item in report["checks"]:
        state = "OK" if item["ok"] else ("FAIL" if item["required"] else "WARN")
        print(f"  [{state}] {item['name']}: {item['detail']}")


def require_environment() -> None:
    report = environment_report()
    print_environment(report)
    if not report["ok"]:
        names = "、".join(item["name"] for item in report["blockers"])
        raise RuntimeError(f"环境检测未通过：{names}；未安装、未迁移、未停止现有服务")


def configuration():
    if not PYTHON.is_file():
        raise RuntimeError("依赖尚未安装，请先运行 ./scripts/deploy-local.sh")
    result = run(
        [
            str(PYTHON),
            "-c",
            (
                "from app.config import AppConfig; import json; c=AppConfig(); "
                "print(json.dumps({'data_dir':str(c.data_dir.resolve()),'port':c.port}))"
            ),
        ],
        BACKEND,
        capture=True,
    )
    return json.loads(result.stdout)


def signature(pid: int) -> str:
    result = subprocess.run(
        ["ps", "-p", str(pid), "-o", "lstart=,command="],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def owned(entry: dict) -> bool:
    actual = signature(int(entry["pid"]))
    if not actual or actual != entry["signature"] or str(ROOT) not in actual:
        return False
    if entry["name"] not in {"api", "content", "device"}:
        return False
    expected = "app.main:app" if entry["name"] == "api" else f"app.worker --queue {entry['name']}"
    return expected in actual


def manifest_path(config) -> Path:
    return Path(config["data_dir"]) / "runs" / "services.json"


def services(config) -> list[dict]:
    path = manifest_path(config)
    return json.loads(path.read_text()).get("services", []) if path.is_file() else []


def status(config):
    return [
        {"name": entry["name"], "pid": entry["pid"], "running": owned(entry)} for entry in services(config)
    ]


def health_url(config) -> str:
    return f"http://127.0.0.1:{config['port']}"


def healthy(config) -> bool:
    try:
        with urllib.request.urlopen(health_url(config) + "/api/v1/health", timeout=2) as response:
            return response.status == 200
    except (urllib.error.URLError, TimeoutError):
        return False


def task_environment(config):
    result = os.environ.copy()
    result["PUBLISHER_DATA_DIR"], result["PUBLISHER_PORT"] = config["data_dir"], str(config["port"])
    platform_tools = ROOT / ".tools" / "platform-tools"
    if platform_tools.is_dir():
        result["PATH"] = str(platform_tools) + os.pathsep + result.get("PATH", "")
    return result


def start(config, open_browser=False):
    if any(item["running"] for item in status(config)):
        raise RuntimeError("已有本项目服务在运行，请先查看 status 或停止后再启动")
    with socket.socket() as probe:
        if probe.connect_ex(("127.0.0.1", config["port"])) == 0:
            raise RuntimeError(f"端口 {config['port']} 已被占用；不会停止或替换已有进程")
    if not (ROOT / "frontend" / "dist" / "index.html").is_file():
        raise RuntimeError("前端尚未构建，请先执行 setup 或在 frontend 目录执行 pnpm build")
    run([str(PYTHON), "-m", "app.manage", "init"], BACKEND)
    data_dir = Path(config["data_dir"])
    logs = data_dir / "logs"
    logs.mkdir(parents=True, exist_ok=True, mode=0o700)
    commands = {
        "api": [
            str(PYTHON),
            "-m",
            "uvicorn",
            "app.main:app",
            "--app-dir",
            str(BACKEND),
            "--host",
            "127.0.0.1",
            "--port",
            str(config["port"]),
            "--no-access-log",
        ],
        "content": [str(PYTHON), "-m", "app.worker", "--queue", "content", "--workspace", str(ROOT)],
        "device": [str(PYTHON), "-m", "app.worker", "--queue", "device", "--workspace", str(ROOT)],
    }
    launched = []
    try:
        for name, command in commands.items():
            with (logs / f"{name}.log").open("a") as log:
                process = subprocess.Popen(
                    command,
                    cwd=BACKEND,
                    env=task_environment(config),
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
            time.sleep(0.08)
            launched.append({"name": name, "pid": process.pid, "signature": signature(process.pid)})
        manifest_path(config).write_text(json.dumps({"services": launched}, indent=2), encoding="utf-8")
        url = health_url(config)
        for _ in range(50):
            if not all(owned(entry) for entry in launched):
                raise RuntimeError(f"服务启动失败，请查看 {logs} 中的日志")
            try:
                with urllib.request.urlopen(url + "/api/v1/health", timeout=1) as response:
                    if response.status == 200:
                        print(f"映序已启动：{url}\n日志目录：{logs}")
                        if open_browser:
                            webbrowser.open(url)
                        return
            except (urllib.error.URLError, TimeoutError):
                time.sleep(0.2)
        raise RuntimeError(f"服务启动超时，请查看 {logs}")
    except BaseException:
        for entry in launched:
            if owned(entry):
                os.killpg(entry["pid"], signal.SIGTERM)
        raise


def launch(config, open_browser=False):
    current = status(config)
    running = {item["name"] for item in current if item["running"]}
    if running == REQUIRED_SERVICES and healthy(config):
        url = health_url(config)
        print(f"映序已在运行：{url}")
        if open_browser:
            webbrowser.open(url)
        return
    if running:
        print("检测到本项目服务不完整或健康检查未通过，正在安全重启……")
        stop(config)
    start(config, open_browser)


def stop(config):
    entries = services(config)
    stopping = [entry for entry in entries if owned(entry)]
    for entry in reversed(stopping):
        os.killpg(entry["pid"], signal.SIGTERM)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and any(owned(entry) for entry in stopping):
        time.sleep(0.2)
    for entry in stopping:
        if owned(entry):
            os.killpg(entry["pid"], signal.SIGKILL)
    if manifest_path(config).exists():
        manifest_path(config).write_text(
            json.dumps({"services": [], "stopped_at": time.time()}), encoding="utf-8"
        )
    print("已停止本项目匹配的服务；未完成任务保留，未知模型/发布结果会在恢复后等待核验。")


def install_and_build():
    run(
        ["uv", "sync", "--project", "backend", "--locked", "--cache-dir", str(ROOT / "data" / "cache" / "uv")]
    )
    run(["pnpm", "install", "--frozen-lockfile"], ROOT / "frontend")
    run(["pnpm", "build"], ROOT / "frontend")


def setup():
    require_environment()
    install_and_build()
    run([str(PYTHON), "-m", "app.manage", "init"], BACKEND)
    print("安装与构建完成。模型密钥请保存在本地 .env，然后执行 start。")


def ensure_env_file() -> bool:
    target = ROOT / ".env"
    if target.exists():
        return False
    source = ROOT / ".env.example"
    target.write_bytes(source.read_bytes())
    target.chmod(0o600)
    print("已从 .env.example 创建本地 .env；模型密钥保持为空，按需在本机填写。")
    return True


def backup_existing(config) -> str | None:
    database = Path(config["data_dir"]) / "publisher.sqlite3"
    if not database.is_file():
        return None
    result = run([str(PYTHON), "-m", "app.manage", "backup"], BACKEND, capture=True)
    backup_path = result.stdout.strip().splitlines()[-1]
    print(f"部署前备份完成：{backup_path}")
    return backup_path


def deploy(open_browser=False):
    require_environment()
    ensure_env_file()

    existing_config = configuration() if PYTHON.is_file() else None
    backed_up = False
    if existing_config:
        backed_up = backup_existing(existing_config) is not None
        if any(item["running"] for item in status(existing_config)):
            stop(existing_config)

    install_and_build()
    config = configuration()
    if not backed_up:
        backup_existing(config)
    start(config, open_browser)
    print("本机部署完成；依赖、前端构建、数据库迁移和健康检查均已完成。")


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description="映序本地服务控制")
    parser.add_argument(
        "action",
        choices=[
            "setup",
            "deploy",
            "env",
            "start",
            "launch",
            "stop",
            "restart",
            "status",
            "doctor",
            "backup",
            "restore",
            "verify-backup",
        ],
    )
    parser.add_argument("--open", action="store_true")
    parser.add_argument("--from", dest="source", type=Path)
    parser.add_argument("--confirm-restore", action="store_true")
    args = parser.parse_args()
    if args.action == "env":
        require_environment()
        return
    if args.action == "setup":
        setup()
        return
    if args.action == "deploy":
        deploy(args.open)
        return
    config = configuration()
    if args.action == "start":
        start(config, args.open)
    elif args.action == "launch":
        launch(config, args.open)
    elif args.action == "stop":
        stop(config)
    elif args.action == "restart":
        if not (ROOT / "frontend" / "dist" / "index.html").is_file():
            raise RuntimeError("前端尚未构建；保留现有服务，请先在 frontend 目录执行 pnpm build")
        stop(config)
        start(config, args.open)
    elif args.action == "status":
        print(
            json.dumps(
                {"url": f"http://127.0.0.1:{config['port']}", "services": status(config)},
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        if args.action == "restore" and any(item["running"] for item in status(config)):
            raise RuntimeError("恢复前必须先停止本项目 API 和任务进程")
        command = [str(PYTHON), "-m", "app.manage", args.action]
        if args.source:
            command += ["--from", str(args.source.resolve())]
        if args.confirm_restore:
            command.append("--confirm-restore")
        subprocess.run(command, cwd=BACKEND, env=task_environment(config), check=True)


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, subprocess.CalledProcessError) as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)
