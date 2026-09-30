"""Optional Hypit visual renderer; narration, captions and delivery remain in media.py."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from xml.sax.saxutils import quoteattr

from PIL import Image

from .errors import IntegrationError

HYPIT_VERSION = "0.2.10"


def hypit_version(cli: Path) -> str:
    if not cli.is_file():
        raise IntegrationError("HYPIT_NOT_INSTALLED", "请先运行 scripts/setup_hypit.sh 安装本地渲染工具")
    try:
        output = subprocess.run(
            [str(cli), "version", "--json"], capture_output=True, text=True, timeout=15, check=True
        )
        version = json.loads(output.stdout)["version"]
    except (OSError, subprocess.SubprocessError, ValueError, KeyError):
        raise IntegrationError("HYPIT_UNAVAILABLE", "无法读取 Hypit 版本，请检查本地工具") from None
    if version != HYPIT_VERSION:
        raise IntegrationError(
            "HYPIT_VERSION_MISMATCH", f"此适配器需要 Hypit {HYPIT_VERSION}，请按安装文档准备"
        )
    return version


def _write_json(path: Path, value: dict) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    temporary.replace(path)


def write_project(scenes: list[dict], durations: list[float], directory: Path, settings: dict) -> None:
    """Only generated identifiers/recipes are executable markup; user material is copied as bytes."""
    directory.mkdir(parents=True, exist_ok=True)
    width, height, fps = settings["width"], settings["height"], settings["fps"]
    # Round cumulative boundaries, avoiding per-scene rounding drift against narration.
    boundaries = [0]
    elapsed = 0.0
    for duration in durations:
        elapsed += duration
        boundaries.append(round(elapsed * fps))
    parts = ['<?svml using="@hypit/markup@1"?>', "<svml>"]
    for alias, package in (
        ("time", "timeline-author"),
        ("space", "spatial"),
        ("media", "media"),
        ("pipeline", "media-pipeline"),
        ("track", "media-track"),
        ("film", "film"),
        ("render", "render-hyperframes"),
    ):
        parts.append(f'<import as="{alias}" from="@hypit/{package}@1"/>')
    parts.extend(
        [
            '<import as="look" source="./look.svs"/>',
            f'<time:Clock id="clock" frame-rate="{fps}"/>',
            f'<time:Timeline id="program" clock={{clock}} end="{boundaries[-1]}f"/>',
            f'<space:Canvas id="canvas" width="{width}" height="{height}"/>',
            '<space:Frame id="frame" within={canvas} left="0px" top="0px" right="100%" bottom="100%"/>',
        ]
    )
    items = []
    for index, scene in enumerate(scenes):
        start, end = boundaries[index : index + 2]
        if end <= start:
            raise IntegrationError("INVALID_DURATION", "Hypit 分镜时长不足一帧")
        if scene.get("video_path"):
            asset_name = f"clip-{index}.mp4"
            shutil.copyfile(scene["video_path"], directory / asset_name)
            parts.extend(
                [
                    f'<media:Video id="asset{index}" src={quoteattr("./" + asset_name)}/>',
                    f'<pipeline:Normalize id="clip{index}" source={{asset{index}}} video="primary-moving" audio="none" span-authority="video" clock={{clock}}/>',
                ]
            )
            source = f"media={{clip{index}.media}}"
            appearance = "clip"
            sampling = ""
        else:
            asset_name = f"image-{index}.png"
            with Image.open(scene["image_path"]) as image:
                prepared = image.convert("RGB")
                prepared.save(directory / asset_name)
                iw, ih = prepared.size
            parts.extend(
                [
                    f'<media:Image id="asset{index}" src={quoteattr("./" + asset_name)}/>',
                    f'<space:Extent id="extent{index}" width="{iw}" height="{ih}"/>',
                ]
            )
            source = f"image={{asset{index}}} extent={{extent{index}}}"
            appearance = "still"
            motion = "none" if scene.get("scene_type") in {"text_card", "stat_card", "quote_card"} else scene.get("motion", "auto")
            first, last = (1.08, 1) if motion == "pull_out" else (1, 1.08)
            if motion == "none":
                first = last = 1
            x1 = x2 = 0
            if motion in {"pan_left", "pan_right"}:
                first = last = 1.08
                x1 = round(width * 0.025) * (1 if motion == "pan_left" else -1)
                x2 = -x1
            sampling = (
                f'<track:Sampling at="start" zoom="{first}" x="{x1}"/>'
                f'<track:Sampling at="end" zoom="{last}" x="{x2}" easing="ease-in-out"/>'
            )
        items.append(
            f'<track:Item id="scene{index}" {source} frame={{frame}} at="{start}f" for="{end - start}f" appearance={{look.media.{appearance}}}>{sampling}</track:Item>'
        )
    parts.extend(
        [
            '<track:Track id="visual" timeline={program.timeline} canvas={canvas}>',
            *items,
            "</track:Track>",
            '<film:Film id="main" canvas={canvas} timeline={program.timeline} appearance={look.film.main}><film:Track source={visual.visual}/></film:Film>',
            '<render:Video id="final" composition={main.composition} timeline={program.timeline}/>',
            "</svml>",
        ]
    )
    (directory / "main.svml").write_text("\n".join(parts), encoding="utf-8")
    (directory / "look.svs").write_text(
        """<?svml using="@hypit/svs@1"?>
<sheet version="1">
film.main { background: #13232e; }
media.still { stack-order: 0; fit: cover; }
media.clip { stack-order: 0; fit: cover; playback: hold-start; }
</sheet>""",
        encoding="utf-8",
    )
    (directory / "main.svrun").write_text(
        """<?svml using="@hypit/run-markup@1"?>
<svrun version="1"><author source="./main.svml"/><target output="final.video"/></svrun>""",
        encoding="utf-8",
    )


def render_visual(*, scenes, durations, output_dir, settings, progress, canceled) -> Path:
    cli = Path(settings["hypit_cli"]).resolve()
    hypit_version(cli)
    runtime = Path(settings["hypit_runtime"]).resolve()
    if not runtime.is_file():
        raise IntegrationError("HYPIT_NOT_CONFIGURED", "Hypit 尚未准备完成，请运行 scripts/setup_hypit.sh")
    directory = Path(output_dir).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "HYPIT_STATE_HOME": str(runtime.parent / "host")}

    def command(*arguments, timeout=60, allow_failure=False):
        try:
            output = subprocess.run(
                [
                    str(cli),
                    *arguments,
                    "--workspace",
                    str(directory),
                    *([] if arguments[0] in {"check", "get"} else ["--runtime", str(runtime)]),
                    "--json",
                ],
                env=env,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
            value = json.loads(output.stdout)
        except (OSError, subprocess.SubprocessError, ValueError):
            raise IntegrationError(
                "HYPIT_COMMAND_FAILED", "Hypit 命令未返回有效结果；请查看本地构建记录"
            ) from None
        if (output.returncode or value.get("ok") is False) and not allow_failure:
            raise IntegrationError("HYPIT_COMMAND_FAILED", "Hypit 执行失败；请查看本地构建记录")
        return value

    fingerprint = hashlib.sha256(
        json.dumps(
            {"scenes": scenes, "durations": durations, "settings": settings, "version": HYPIT_VERSION},
            sort_keys=True,
        ).encode()
    ).hexdigest()
    checkpoint_path = directory / "build.json"
    checkpoint = json.loads(checkpoint_path.read_text()) if checkpoint_path.exists() else {}
    if checkpoint and checkpoint.get("fingerprint") != fingerprint:
        raise IntegrationError("HYPIT_INPUT_CHANGED", "恢复渲染的输入发生变化，请重新创建渲染任务")
    build_id = checkpoint.get("build_id")
    if checkpoint and not build_id:
        raise IntegrationError(
            "HYPIT_BUILD_UNKNOWN", "Hypit 构建提交结果未明，请核验构建列表，系统不会自动重复提交"
        )
    if canceled():
        if build_id:
            command("cancel", build_id, allow_failure=True)
        raise IntegrationError("TASK_CANCELED", "任务已取消")
    if not build_id:
        write_project(scenes, durations, directory, settings)
        command("check", str(directory / "main.svrun"))
        _write_json(checkpoint_path, {"fingerprint": fingerprint, "submitting": True})
        result = command("build", str(directory / "main.svrun"), timeout=120)
        build_id = result.get("build", {}).get("id")
        if not isinstance(build_id, str) or not re.fullmatch(r"bld_[A-Za-z0-9_]+", build_id):
            raise IntegrationError("HYPIT_BUILD_UNKNOWN", "Hypit 未返回构建 ID，请核验构建列表")
        _write_json(checkpoint_path, {"fingerprint": fingerprint, "build_id": build_id})
    deadline = time.monotonic() + 1800
    while True:
        if canceled() or time.monotonic() > deadline:
            command("cancel", build_id, allow_failure=True)
            raise IntegrationError(
                "TASK_CANCELED" if canceled() else "HYPIT_TIMEOUT", "本地 Hypit 渲染已请求停止"
            )
        state = command("status", build_id, allow_failure=True).get("build", {})
        work = state.get("work", {})
        if work.get("state") == "done":
            if work.get("outcome") != "complete":
                raise IntegrationError(
                    "HYPIT_RENDER_FAILED", f"Hypit 构建失败：{build_id}；请检查本地构建日志"
                )
            break
        if not work:
            raise IntegrationError("HYPIT_STATUS_UNKNOWN", "无法确认 Hypit 构建状态，已保留构建 ID")
        progress("Hypit 本地渲染画面", 15)
        for _ in range(10):
            if canceled():
                break
            time.sleep(0.25)
    target = directory / "visual.mp4"
    command("get", build_id, "--output", "final.video", "--to", str(target))
    from .media import probe_media

    metadata = probe_media(target)
    if not metadata.get("has_video") or metadata.get("duration", 0) <= 0:
        raise IntegrationError("HYPIT_OUTPUT_INVALID", "Hypit 输出未通过视频检查")
    return target
