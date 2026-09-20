from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

CONTROL_PATH = Path(__file__).resolve().parents[2] / "scripts" / "control.py"
SPEC = importlib.util.spec_from_file_location("publisher_control", CONTROL_PATH)
control = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(control)


def test_version_tuple_accepts_common_tool_output():
    assert control.version_tuple("v22.20.0") == (22, 20, 0)
    assert control.version_tuple("uv 0.6.11 (build)") == (0, 6, 11)
    assert control.version_tuple("unknown") == ()
    assert control.version_tuple("Android Debug Bridge version 1.0.39") < (1, 0, 41)
    assert control.version_tuple("Android Debug Bridge version 1.0.41") >= (1, 0, 41)


def test_configured_adb_path_reads_only_dedicated_override(monkeypatch, tmp_path):
    executable = tmp_path / "platform tools" / "adb"
    executable.parent.mkdir()
    executable.write_text("fixture")
    executable.chmod(0o700)
    env_file = tmp_path / ".env"
    env_file.write_text(
        "PUBLISHER_TEXT_API_KEY=must-not-be-read-as-a-path\n"
        f'PUBLISHER_ADB_PATH="{executable}"\n',
        encoding="utf-8",
    )
    monkeypatch.delenv("PUBLISHER_ADB_PATH", raising=False)
    monkeypatch.setattr(control, "ROOT", tmp_path)

    assert control.configured_adb_path() == executable


def test_launch_is_idempotent_when_all_services_are_healthy(monkeypatch):
    monkeypatch.setattr(
        control,
        "status",
        lambda _config: [
            {"name": name, "pid": index, "running": True}
            for index, name in enumerate(("api", "content", "device"), start=1)
        ],
    )
    monkeypatch.setattr(control, "healthy", lambda _config: True)
    started = []
    monkeypatch.setattr(control, "start", lambda *_args, **_kwargs: started.append(True))

    control.launch({"port": 8765})

    assert started == []


def test_launch_restarts_only_tracked_partial_services(monkeypatch):
    monkeypatch.setattr(
        control,
        "status",
        lambda _config: [
            {"name": "api", "pid": 1, "running": True},
            {"name": "content", "pid": 2, "running": False},
        ],
    )
    events = []
    monkeypatch.setattr(control, "stop", lambda _config: events.append("stop"))
    monkeypatch.setattr(control, "start", lambda _config, _open: events.append("start"))

    control.launch({"port": 8765})

    assert events == ["stop", "start"]


def test_deploy_backs_up_and_stops_before_building(monkeypatch):
    config = {"data_dir": "/fixture/data", "port": 8765}
    events = []
    monkeypatch.setattr(control, "require_environment", lambda: events.append("environment"))
    monkeypatch.setattr(control, "ensure_env_file", lambda: events.append("env-file"))
    monkeypatch.setattr(control, "PYTHON", SimpleNamespace(is_file=lambda: True))
    monkeypatch.setattr(control, "configuration", lambda: config)
    monkeypatch.setattr(control, "backup_existing", lambda _config: events.append("backup") or "/backup")
    monkeypatch.setattr(
        control, "status", lambda _config: [{"name": "api", "pid": 1, "running": True}]
    )
    monkeypatch.setattr(control, "stop", lambda _config: events.append("stop"))
    monkeypatch.setattr(control, "install_and_build", lambda: events.append("build"))
    monkeypatch.setattr(control, "start", lambda _config, _open: events.append("start"))

    control.deploy()

    assert events == ["environment", "env-file", "backup", "stop", "build", "start"]
