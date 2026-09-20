from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import manage
from app.manage import backup, restore, verify_backup
from app.models import Job, Schedule, Setting
from app.services import AppError, enqueue


def test_template_versions_preserve_old_prompt_and_partial_schedule_edit(client):
    original = client.get("/api/v1/templates").json()["items"][0]
    changed = client.patch(f"/api/v1/templates/{original['id']}", json={"prompt": "新的测试提示词"})
    assert changed.status_code == 200, changed.text
    versions = client.get(f"/api/v1/templates/{original['id']}/versions").json()["items"]
    assert versions[0]["version"] == 2 and versions[1]["prompt"] == original["prompt"]
    created = client.post(
        "/api/v1/schedules",
        json={"name": "测试读数计划", "kind": "metrics", "time": "09:00", "timezone": "UTC"},
    ).json()
    changed = client.patch(f"/api/v1/schedules/{created['id']}", json={"enabled": True})
    assert changed.status_code == 200 and changed.json()["enabled"] is True
    assert changed.json()["name"] == "测试读数计划"


def test_backup_is_consistent_and_tampering_is_detected(client, engine, config, rendered_content):
    destination = backup(config)
    manifest = verify_backup(destination)
    assert len(manifest["assets"]) == 3 and manifest["secrets_included"] is False
    assert not (destination / ".env").exists()
    asset = destination / manifest["assets"][0]["path"]
    asset.write_bytes(b"fixture tampering")
    with pytest.raises(AppError) as failure:
        verify_backup(destination)
    assert failure.value.code == "INVALID_BACKUP"


def test_restore_disables_automatic_replay_and_preserves_current_backup(client, engine, config):
    with Session(engine) as session:
        pending = enqueue(session, kind="GENERATE_REPORT", payload={})
        identifier = pending.id
        session.add(Schedule(name="fixture", kind="metrics", time="09:00", timezone="UTC", enabled=True))
        session.commit()
    snapshot = backup(config)
    # Internal restore is exercised only against this isolated test directory, with no running service process.
    engine.dispose()
    result = restore(config, snapshot)
    assert Path(result["preserved_current"]).is_dir()
    with Session(engine) as session:
        assert session.get(Job, identifier).status == "WAITING_USER"
        assert session.get(Job, identifier).error_code == "RESTORE_REVIEW_REQUIRED"
        assert session.scalar(select(Schedule)).enabled is False
        assert not any(model["enabled"] for model in session.get(Setting, "app").value["models"].values())
    assert client.post(f"/api/v1/jobs/{identifier}/retry").status_code == 409
    response = client.post(
        f"/api/v1/jobs/{identifier}/resolve",
        json={"resolution": "not_completed", "confirmed": True, "notes": "测试：已核验恢复前未执行此任务"},
    )
    assert response.status_code == 200 and response.json()["status"] == "FAILED"


def test_device_pin_prompt_refuses_non_tty_without_calling_getpass(monkeypatch):
    monkeypatch.setattr(manage.getpass, "getpass", lambda _prompt: pytest.fail("must not read PIN"))
    with pytest.raises(AppError) as caught:
        manage._prompt_device_pin()
    assert caught.value.code == "DEVICE_PIN_TTY_REQUIRED"


def test_device_pin_set_uses_hidden_confirmed_input_and_prints_only_nonsecret_status(
    monkeypatch, config, capsys
):
    calls = []
    monkeypatch.setattr(manage, "_prompt_device_pin", lambda: "246810")
    monkeypatch.setattr("app.device_credentials.set_device_pin", lambda serial, pin: calls.append((serial, pin)))
    monkeypatch.setattr("app.integrations.device.clear_unlock_failure", lambda serial, cfg: calls.append((serial, cfg)))

    manage._manage_device_pin("set", "ABC123", config)

    output = capsys.readouterr().out
    assert calls == [("ABC123", "246810"), ("ABC123", config)]
    assert '"stored": true' in output and '"unlock_retry_blocked": false' in output
    assert "246810" not in output


def test_device_pin_status_does_not_return_secret(monkeypatch, config, capsys):
    monkeypatch.setattr("app.device_credentials.device_pin_status", lambda serial: {"stored": True})
    monkeypatch.setattr("app.integrations.device.unlock_failure_latched", lambda serial, cfg: True)

    manage._manage_device_pin("status", "ABC123", config)

    output = capsys.readouterr().out
    assert '"stored": true' in output and '"unlock_retry_blocked": true' in output
    assert "pin" not in output.lower()


def test_device_pin_cli_routes_subcommand_and_serial(monkeypatch, config):
    calls = []
    monkeypatch.setattr(manage, "AppConfig", lambda: config)
    monkeypatch.setattr(manage, "_manage_device_pin", lambda action, serial, cfg: calls.append((action, serial, cfg)))
    monkeypatch.setattr(manage.sys, "argv", ["manage", "device-pin", "status", "--serial", "ABC123"])

    manage.main()

    assert calls == [("status", "ABC123", config)]
