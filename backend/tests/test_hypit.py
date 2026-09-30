from __future__ import annotations

import json
import subprocess
from types import SimpleNamespace

import pytest
from PIL import Image

from app.integrations import hypit
from app.integrations.errors import IntegrationError


@pytest.fixture
def setup_render(tmp_path, monkeypatch):
    image = tmp_path / "input.png"
    Image.new("RGB", (100, 100), "blue").save(image)
    runtime = tmp_path / "runtime.json"
    runtime.write_text("{}")
    arguments = {
        "scenes": [{"image_path": str(image), "motion": "push_in"}],
        "durations": [2.0],
        "output_dir": tmp_path / "output",
        "settings": {
            "width": 320,
            "height": 568,
            "fps": 24,
            "hypit_cli": str(tmp_path / "hypit"),
            "hypit_runtime": str(runtime),
        },
        "progress": lambda *_: None,
        "canceled": lambda: False,
    }
    monkeypatch.setattr(hypit, "hypit_version", lambda _: hypit.HYPIT_VERSION)
    monkeypatch.setattr("app.integrations.media.probe_media", lambda _: {"has_video": True, "duration": 2})
    monkeypatch.setattr(hypit.time, "sleep", lambda _: None)
    return arguments


def test_completed_build_is_exported_and_resume_never_builds_again(setup_render, monkeypatch):
    calls = []

    def run(args, **kwargs):
        calls.append(args[1])
        if args[1] == "check":
            assert "--runtime" not in args
        if args[1] == "get":
            assert "--runtime" not in args
            from pathlib import Path

            Path(args[args.index("--to") + 1]).write_bytes(b"mp4")
        value = {"build": {"id": "bld_fixture", "work": {"state": "done", "outcome": "complete"}}}
        return SimpleNamespace(returncode=0, stdout=json.dumps(value))

    monkeypatch.setattr(hypit.subprocess, "run", run)
    first = hypit.render_visual(**setup_render)
    assert first.read_bytes() == b"mp4"
    assert hypit.render_visual(**setup_render) == first
    assert calls == ["check", "build", "status", "get", "status", "get"]


def test_card_scene_stays_still_for_readability(setup_render):
    args = setup_render
    args["scenes"][0].update(scene_type="text_card", motion="push_in")
    hypit.write_project(args["scenes"], args["durations"], args["output_dir"], args["settings"])
    project = (args["output_dir"] / "main.svml").read_text()
    assert 'at="start" zoom="1"' in project
    assert 'at="end" zoom="1"' in project


def test_uncertain_submission_is_not_repeated(setup_render, monkeypatch):
    calls = []

    def run(args, **kwargs):
        calls.append(args[1])
        if args[1] == "build":
            raise subprocess.TimeoutExpired(args, 120)
        return SimpleNamespace(returncode=0, stdout='{"ok":true}')

    monkeypatch.setattr(hypit.subprocess, "run", run)
    with pytest.raises(IntegrationError) as first:
        hypit.render_visual(**setup_render)
    assert first.value.code == "HYPIT_COMMAND_FAILED"
    with pytest.raises(IntegrationError) as second:
        hypit.render_visual(**setup_render)
    assert second.value.code == "HYPIT_BUILD_UNKNOWN"
    assert calls == ["check", "build"]


def test_cancellation_explicitly_cancels_known_build(setup_render, monkeypatch):
    calls = []
    cancellation = {"value": False}

    def run(args, **kwargs):
        calls.append(args[1])
        if args[1] == "build":
            cancellation["value"] = True
        return SimpleNamespace(returncode=0, stdout=json.dumps({"build": {"id": "bld_fixture"}}))

    monkeypatch.setattr(hypit.subprocess, "run", run)
    setup_render["canceled"] = lambda: cancellation["value"]
    with pytest.raises(IntegrationError) as caught:
        hypit.render_visual(**setup_render)
    assert caught.value.code == "TASK_CANCELED"
    assert calls == ["check", "build", "cancel"]
    assert json.loads((setup_render["output_dir"] / "build.json").read_text())["build_id"] == "bld_fixture"


def test_failed_build_is_not_silently_replaced_by_ffmpeg(setup_render, monkeypatch):
    calls = []

    def run(args, **kwargs):
        calls.append(args[1])
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {"build": {"id": "bld_fixture", "work": {"state": "done", "outcome": "failed"}}}
            ),
        )

    monkeypatch.setattr(hypit.subprocess, "run", run)
    with pytest.raises(IntegrationError) as caught:
        hypit.render_visual(**setup_render)
    assert caught.value.code == "HYPIT_RENDER_FAILED"
    assert "get" not in calls
