"""Local FFmpeg composition with bounded inputs and cooperative cancellation."""

from __future__ import annotations

import json
import math
import re
import shutil
import subprocess
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont, ImageOps, ImageStat, UnidentifiedImageError

from ..schemas import split_narration
from .errors import CanceledError, IntegrationError

_FFMPEG_CANDIDATES = (Path("/opt/homebrew/bin/ffmpeg"), Path("/usr/local/bin/ffmpeg"))
_FFPROBE_CANDIDATES = (Path("/opt/homebrew/bin/ffprobe"), Path("/usr/local/bin/ffprobe"))
_FONT_CANDIDATES = (
    Path("/System/Library/Fonts/PingFang.ttc"),
    Path("/System/Library/Fonts/STHeiti Medium.ttc"),
    Path("/System/Library/Fonts/Supplemental/Arial Unicode.ttf"),
    Path("/Library/Fonts/Arial Unicode.ttf"),
    Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
    Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
)
_LOCAL_PROTOCOLS = "file"
_SAFE_INPUT_FORMATS = (
    "mov,mp4,m4a,3gp,3g2,mj2,matroska,webm,wav,mp3,flac,ogg,aac,ac3,eac3,"
    "png_pipe,jpeg_pipe,webp_pipe,gif,bmp_pipe,tiff_pipe"
)


def _binary(name: str) -> str:
    candidates = _FFMPEG_CANDIDATES if name == "ffmpeg" else _FFPROBE_CANDIDATES
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    found = shutil.which(name)
    if found:
        return found
    raise IntegrationError("MEDIA_TOOL_MISSING", f"未安装 {name}")


def _input_file(value: Any, *, kind: str) -> Path:
    try:
        path = Path(value).expanduser()
    except TypeError as exc:
        raise IntegrationError("INVALID_MEDIA", f"{kind}路径无效") from exc
    if not path.is_file():
        raise IntegrationError("MEDIA_NOT_FOUND", f"找不到{kind}文件")
    return path.resolve()


def probe_media(path: Path) -> dict:
    """Return stable duration and stream metadata for a local media file."""

    media_path = _input_file(path, kind="媒体")
    command = [
        _binary("ffprobe"),
        "-v",
        "error",
        "-show_streams",
        "-show_format",
        "-protocol_whitelist",
        _LOCAL_PROTOCOLS,
        "-format_whitelist",
        _SAFE_INPUT_FORMATS,
        "-i",
        str(media_path),
        "-of",
        "json",
    ]
    try:
        result = subprocess.run(command, capture_output=True, check=False, timeout=30)
    except subprocess.TimeoutExpired as exc:
        raise IntegrationError("MEDIA_PROBE_TIMEOUT", "媒体检查超时") from exc
    except OSError as exc:
        raise IntegrationError("MEDIA_PROBE_FAILED", "无法启动媒体检查") from exc
    if result.returncode != 0:
        raise IntegrationError("INVALID_MEDIA", "媒体文件格式无效或不受支持")
    try:
        payload = json.loads(result.stdout.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise IntegrationError("MEDIA_PROBE_FAILED", "媒体检查返回了无效数据") from exc

    streams = payload.get("streams") if isinstance(payload, dict) else None
    stream_list = streams if isinstance(streams, list) else []
    video = next((item for item in stream_list if item.get("codec_type") == "video"), None)
    audio = next((item for item in stream_list if item.get("codec_type") == "audio"), None)
    raw_duration = (payload.get("format") or {}).get("duration")
    if raw_duration is None:
        raw_duration = (video or audio or {}).get("duration")
    try:
        duration = float(raw_duration) if raw_duration is not None else 0.0
    except (TypeError, ValueError):
        duration = 0.0
    if not math.isfinite(duration) or duration < 0:
        duration = 0.0

    def frame_rate(value: Any) -> float | None:
        try:
            numerator, denominator = str(value).split("/", 1)
            result = float(numerator) / float(denominator)
        except (TypeError, ValueError, ZeroDivisionError):
            return None
        return result if math.isfinite(result) and result > 0 else None

    return {
        "duration": duration,
        "width": int(video["width"]) if video and video.get("width") is not None else None,
        "height": int(video["height"]) if video and video.get("height") is not None else None,
        "video_codec": video.get("codec_name") if video else None,
        "audio_codec": audio.get("codec_name") if audio else None,
        "fps": frame_rate(video.get("avg_frame_rate") or video.get("r_frame_rate")) if video else None,
        "has_video": video is not None,
        "has_audio": audio is not None,
        "format_name": (payload.get("format") or {}).get("format_name"),
        "size": media_path.stat().st_size,
    }


def render_card(
    *,
    scene_type: str,
    visual_data: dict,
    destination: Path,
    width: int,
    height: int,
    palette: list[str],
    font_path: str = "",
) -> None:
    """Render a bounded plain-text vertical card without interpreting markup or expressions."""

    if scene_type not in {"text_card", "stat_card", "quote_card"}:
        raise IntegrationError("INVALID_CARD_TYPE", "不支持的本地卡片类型")
    if not isinstance(visual_data, dict):
        raise IntegrationError("INVALID_CARD_DATA", "卡片数据无效")
    if width % 2 or height % 2 or not 320 <= width <= 2160 or not 320 <= height <= 3840:
        raise IntegrationError("INVALID_CARD_SIZE", "卡片画布尺寸无效")
    colors = [value for value in palette if isinstance(value, str) and re.fullmatch(r"#[0-9a-fA-F]{6}", value)]
    if len(colors) < 3:
        colors = ["#315bff", "#111827", "#f7f8fb", "#f2c94c"]
    settings = {"font_path": font_path}
    base_font, resolved_font, _family = _font(settings, width)

    def sized(size: int):
        try:
            return ImageFont.truetype(str(resolved_font) if resolved_font else "DejaVuSans.ttf", size)
        except OSError:
            return base_font

    image = Image.new("RGB", (width, height), colors[1])
    draw = ImageDraw.Draw(image, "RGBA")
    draw.rounded_rectangle(
        (round(width * 0.06), round(height * 0.10), round(width * 0.94), round(height * 0.90)),
        radius=max(18, width // 24),
        fill=colors[2],
    )
    accent = colors[3] if scene_type == "stat_card" else colors[0]
    draw.rounded_rectangle(
        (round(width * 0.10), round(height * 0.14), round(width * 0.90), round(height * 0.16)),
        radius=max(4, width // 120),
        fill=accent,
    )
    headline = str(visual_data.get("headline") or "").strip()
    body = str(visual_data.get("body") or "").strip()
    value = str(visual_data.get("value") or "").strip()
    emphasis = str(visual_data.get("emphasis") or "").strip()
    if not any((headline, body, value)):
        raise IntegrationError("CARD_TEXT_REQUIRED", "卡片至少需要一项可见文字")

    left, right = round(width * 0.14), round(width * 0.86)
    y = round(height * 0.22)

    def draw_wrapped(text: str, font, fill, *, spacing: int, max_lines: int) -> None:
        nonlocal y
        if not text:
            return
        lines = _wrap_title(draw, text, font, right - left)
        if len(lines) > max_lines:
            lines = lines[:max_lines]
            lines[-1] = lines[-1][:-1] + "…" if lines[-1] else "…"
        for line in lines:
            box = draw.textbbox((0, 0), line, font=font)
            height_value = max(1, box[3] - box[1])
            draw.text((left, y), line, font=font, fill=fill)
            y += height_value + spacing

    draw_wrapped(headline, sized(max(28, width // 15)), colors[1], spacing=width // 40, max_lines=4)
    if scene_type == "stat_card":
        y += round(height * 0.04)
        draw_wrapped(value, sized(max(42, width // 8)), accent, spacing=width // 28, max_lines=3)
    elif scene_type == "quote_card":
        quote_font = sized(max(32, width // 13))
        y += round(height * 0.03)
        draw.text((left, y), "“", font=sized(max(64, width // 6)), fill=accent)
        y += round(height * 0.08)
        draw_wrapped(body, quote_font, colors[1], spacing=width // 30, max_lines=9)
        body = ""
    draw_wrapped(body, sized(max(24, width // 20)), colors[1], spacing=width // 35, max_lines=10)
    if emphasis:
        draw_wrapped(emphasis, sized(max(20, width // 24)), accent, spacing=width // 40, max_lines=3)
    destination.parent.mkdir(parents=True, exist_ok=True)
    image.save(destination, format="PNG", optimize=True)


def _subtitle_seconds(value: str) -> float:
    match = re.fullmatch(r"(\d{2}):(\d{2}):(\d{2})[,\.](\d{3})", value.strip())
    if not match:
        raise ValueError("invalid subtitle time")
    hours, minutes, seconds, milliseconds = (int(part) for part in match.groups())
    if minutes >= 60 or seconds >= 60:
        raise ValueError("invalid subtitle time")
    return hours * 3600 + minutes * 60 + seconds + milliseconds / 1000


def _review_subtitle(path: Path, duration: float) -> tuple[dict, list[dict]]:
    issues: list[dict] = []
    try:
        text = _input_file(path, kind="字幕").read_text(encoding="utf-8")
    except (IntegrationError, OSError, UnicodeError):
        return {"exists": False, "nonempty": False}, [
            {"code": "SUBTITLE_INVALID", "message": "字幕不存在或无法读取", "severity": "FAIL"}
        ]
    matches = re.findall(
        r"(?m)^(\d{2}:\d{2}:\d{2}[,.]\d{3})\s+-->\s+(\d{2}:\d{2}:\d{2}[,.]\d{3})\s*$",
        text,
    )
    intervals = []
    try:
        intervals = [(_subtitle_seconds(start), _subtitle_seconds(end)) for start, end in matches]
    except ValueError:
        issues.append({"code": "SUBTITLE_TIME_INVALID", "message": "字幕时间格式无效", "severity": "FAIL"})
    monotonic = bool(intervals) and all(
        start >= 0 and end > start and (index == 0 or start >= intervals[index - 1][1] - 0.001)
        for index, (start, end) in enumerate(intervals)
    )
    within_video = bool(intervals) and intervals[-1][1] <= duration + 0.25
    coverage = sum(max(0.0, end - start) for start, end in intervals) / duration if duration > 0 else 0.0
    if not text.strip() or not intervals:
        issues.append({"code": "SUBTITLE_EMPTY", "message": "字幕为空", "severity": "FAIL"})
    elif not monotonic:
        issues.append({"code": "SUBTITLE_NOT_MONOTONIC", "message": "字幕时间不单调", "severity": "FAIL"})
    if intervals and not within_video:
        issues.append({"code": "SUBTITLE_EXCEEDS_VIDEO", "message": "字幕超出视频时长", "severity": "FAIL"})
    if intervals and coverage < 0.6:
        issues.append({"code": "SUBTITLE_COVERAGE_LOW", "message": "字幕覆盖率偏低", "severity": "REVISE"})
    return {
        "exists": True,
        "nonempty": bool(text.strip()),
        "cue_count": len(intervals),
        "monotonic": monotonic,
        "within_video": within_video,
        "coverage": round(coverage, 4),
    }, issues


def _review_loudness(path: Path) -> tuple[dict, list[dict]]:
    command = [
        _binary("ffmpeg"),
        "-hide_banner",
        "-nostdin",
        "-i",
        str(_input_file(path, kind="视频")),
        "-vn",
        "-af",
        "loudnorm=I=-16:TP=-1:LRA=11:print_format=json",
        "-f",
        "null",
        "-",
    ]
    try:
        result = subprocess.run(command, capture_output=True, check=False, timeout=45, text=True)
        blocks = re.findall(r"\{[^{}]*\}", result.stderr, re.DOTALL)
        payload = json.loads(blocks[-1]) if result.returncode == 0 and blocks else {}
        integrated = float(payload["input_i"])
        true_peak = float(payload["input_tp"])
        if not math.isfinite(integrated) or not math.isfinite(true_peak):
            raise ValueError("non-finite loudness")
    except (OSError, subprocess.TimeoutExpired, ValueError, KeyError, json.JSONDecodeError):
        return {"integrated_lufs": None, "true_peak_dbfs": None, "parsed": False}, [
            {
                "code": "LOUDNESS_UNAVAILABLE",
                "message": "无法解析音频响度或真峰值",
                "severity": "REVISE",
            }
        ]
    issues = []
    if not -18 <= integrated <= -14:
        issues.append({"code": "LOUDNESS_OUT_OF_RANGE", "message": "整体响度不在 -18..-14 LUFS", "severity": "REVISE"})
    if true_peak > -1.0:
        issues.append({"code": "TRUE_PEAK_TOO_HIGH", "message": "真峰值高于 -1.0 dBFS", "severity": "REVISE"})
    return {
        "integrated_lufs": integrated,
        "true_peak_dbfs": true_peak,
        "parsed": True,
        "target_lufs": [-18.0, -14.0],
        "max_true_peak_dbfs": -1.0,
    }, issues


def _review_frames(path: Path, duration: float, scenes: list[dict]) -> tuple[dict, list[dict]]:
    hero_index = next((index for index, scene in enumerate(scenes) if scene.get("hero_moment")), None)
    fade = min(0.35, duration / 4)
    margin = max(0.01, min(0.05, duration * 0.05))
    opening = min(fade + margin, duration * 0.4)
    ending = max(duration * 0.6, duration - fade - margin)
    hero = (
        duration * min(0.9, max(0.1, (hero_index + 0.5) / len(scenes)))
        if hero_index is not None and scenes
        else duration * 0.7
    )
    samples = [("opening", opening), ("middle", duration * 0.5), ("hero", hero), ("ending", ending)]
    frames = []
    issues: list[dict] = []
    with tempfile.TemporaryDirectory(prefix="yingxu-render-review-") as temporary:
        for index, (name, requested_at) in enumerate(samples):
            timestamp = min(max(0.01, requested_at), max(0.01, duration - 0.01))
            destination = Path(temporary) / f"frame-{index}.png"
            command = [
                _binary("ffmpeg"),
                "-hide_banner",
                "-loglevel",
                "error",
                "-nostdin",
                "-ss",
                f"{timestamp:.6f}",
                "-i",
                str(_input_file(path, kind="视频")),
                "-frames:v",
                "1",
                "-y",
                str(destination),
            ]
            try:
                result = subprocess.run(command, capture_output=True, check=False, timeout=20)
                if result.returncode != 0 or not destination.is_file():
                    raise ValueError("frame decode failed")
                with Image.open(destination) as image:
                    grayscale = ImageOps.grayscale(image.convert("RGB"))
                    mean = float(ImageStat.Stat(grayscale).mean[0])
                    extrema = grayscale.getextrema()
                    frames.append(
                        {
                            "name": name,
                            "at_seconds": round(timestamp, 4),
                            "decoded": True,
                            "width": image.width,
                            "height": image.height,
                            "mean_luma": round(mean, 3),
                            "near_black": mean < 8.0 and extrema[1] < 24,
                        }
                    )
            except (OSError, subprocess.TimeoutExpired, ValueError, UnidentifiedImageError):
                frames.append({"name": name, "at_seconds": round(timestamp, 4), "decoded": False})
    if any(not frame["decoded"] for frame in frames):
        issues.append({"code": "FRAME_DECODE_FAILED", "message": "一个或多个审查帧解码失败", "severity": "FAIL"})
    if any(frame.get("near_black") for frame in frames):
        issues.append({"code": "NEAR_BLACK_FRAME", "message": "检测到全黑或近全黑审查帧", "severity": "FAIL"})
    return {"sample_count": len(frames), "frames": frames}, issues


def review_render_quality(
    *,
    video_path: Path,
    subtitle_path: Path,
    video_asset_id: str,
    scenes: list[dict],
    facts: list[dict],
    settings: dict,
    quality_rules: dict,
) -> dict:
    """Perform a deterministic offline render review and return a complete safe report."""

    checks: dict[str, dict] = {
        "technical": {},
        "visual": {},
        "audio": {},
        "subtitle": {},
        "structure": {},
        "fact_visual": {},
    }
    issues: list[dict] = []
    warnings = [
        {
            "code": "SEMANTIC_REVIEW_MANUAL",
            "message": "画面语义、版权与平台合规仍需人工确认",
            "manual_required": True,
        }
    ]
    try:
        media = probe_media(video_path)
        technical = {
            key: media.get(key)
            for key in (
                "duration",
                "width",
                "height",
                "fps",
                "video_codec",
                "audio_codec",
                "has_video",
                "has_audio",
                "size",
            )
        }
        checks["technical"] = technical
        if not media["has_video"] or not media["has_audio"]:
            issues.append({"code": "MEDIA_STREAM_MISSING", "message": "成片缺少视频或音频流", "severity": "FAIL"})
        if (
            media["width"] != int(settings.get("width", 1080))
            or media["height"] != int(settings.get("height", 1920))
            or media["fps"] is None
            or abs(media["fps"] - int(settings.get("fps", 30))) > 0.5
        ):
            issues.append({"code": "MEDIA_SETTINGS_MISMATCH", "message": "成片分辨率或帧率与设置不一致", "severity": "FAIL"})
        if media["video_codec"] != "h264" or media["audio_codec"] != "aac":
            issues.append({"code": "MEDIA_CODEC_MISMATCH", "message": "成片编码不是 H.264/AAC", "severity": "FAIL"})
        if media["duration"] <= 0 or media["size"] <= 0:
            issues.append({"code": "MEDIA_EMPTY", "message": "成片时长或文件大小无效", "severity": "FAIL"})
    except (IntegrationError, KeyError, OSError, TypeError, ValueError):
        media = {"duration": 0.0, "has_audio": False}
        checks["technical"] = {"probeable": False}
        issues.append({"code": "MEDIA_PROBE_FAILED", "message": "ffprobe 无法解析成片", "severity": "FAIL"})

    duration = float(media.get("duration") or 0)
    if duration > 0 and checks["technical"].get("has_video"):
        checks["visual"], frame_issues = _review_frames(video_path, duration, scenes)
        issues.extend(frame_issues)
    else:
        checks["visual"] = {"sample_count": 0, "frames": []}
    if checks["technical"].get("has_audio"):
        checks["audio"], audio_issues = _review_loudness(video_path)
        issues.extend(audio_issues)
    else:
        checks["audio"] = {"integrated_lufs": None, "true_peak_dbfs": None, "parsed": False}
    checks["subtitle"], subtitle_issues = _review_subtitle(subtitle_path, duration)
    issues.extend(subtitle_issues)

    scene_types = [scene.get("scene_type", "image") for scene in scenes]
    max_consecutive = 0
    run = 0
    previous = None
    for scene_type in scene_types:
        run = run + 1 if scene_type == previous else 1
        previous = scene_type
        max_consecutive = max(max_consecutive, run)
    intent_count = sum(bool(str(scene.get("shot_intent") or "").strip()) for scene in scenes)
    intent_coverage = intent_count / len(scenes) if scenes else 0.0
    structure = {
        "visual_types": sorted(set(scene_types)),
        "visual_type_count": len(set(scene_types)),
        "max_consecutive_same_type": max_consecutive,
        "hero_scene_count": sum(bool(scene.get("hero_moment")) for scene in scenes),
        "shot_intent_coverage": round(intent_coverage, 4),
    }
    checks["structure"] = structure
    if structure["visual_type_count"] < int(quality_rules.get("min_visual_types", 2)):
        issues.append({"code": "VISUAL_TYPES_INSUFFICIENT", "message": "视觉类型数量不足", "severity": "REVISE"})
    if max_consecutive > int(quality_rules.get("max_consecutive_same_type", 2)):
        issues.append({"code": "SCENE_TYPE_REPETITION", "message": "同类型镜头连续超过预设上限", "severity": "REVISE"})
    if quality_rules.get("require_hero_scene", True) and structure["hero_scene_count"] < 1:
        issues.append({"code": "HERO_SCENE_REQUIRED", "message": "缺少高潮镜头", "severity": "REVISE"})
    if intent_coverage < 0.8:
        issues.append({"code": "SHOT_INTENT_COVERAGE_LOW", "message": "镜头意图填写不足", "severity": "REVISE"})

    verified = [fact for fact in facts if fact.get("verified") and fact.get("source_url")]
    unmatched = []
    for scene in scenes:
        if scene.get("scene_type", "image") not in {"stat_card", "quote_card"}:
            continue
        visual = scene.get("visual_data") if isinstance(scene.get("visual_data"), dict) else {}
        value = str(
            visual.get("value") if scene.get("scene_type") == "stat_card" else visual.get("body") or ""
        ).strip()
        source_url = str(visual.get("source_url") or "").strip()
        matched = bool(value and source_url) and any(
            fact["source_url"] == source_url
            and (value.casefold() in str(fact.get("claim") or "").casefold() or str(fact.get("claim") or "").casefold() in value.casefold())
            for fact in verified
        )
        if not matched:
            unmatched.append(scene.get("id"))
    checks["fact_visual"] = {
        "verified_fact_count": len(verified),
        "checked_card_count": sum(
            scene.get("scene_type", "image") in {"stat_card", "quote_card"} for scene in scenes
        ),
        "unmatched_scene_ids": unmatched,
        "manual_required": True,
    }
    if unmatched:
        issues.append({"code": "FACT_VISUAL_UNMATCHED", "message": "数据或引语卡片未匹配已核实事实", "severity": "REVISE"})

    status = "FAIL" if any(issue["severity"] == "FAIL" for issue in issues) else (
        "REVISE" if issues else "PASS"
    )
    return {
        "version": "1.0",
        "status": status,
        "video_asset_id": video_asset_id,
        "checks": checks,
        "issues": issues,
        "warnings": warnings,
        "recommended_action": {
            "PASS": "present_for_review",
            "REVISE": "revise",
            "FAIL": "rerender",
        }[status],
    }


def _notify(callback: Callable[[str, int], None], step: str, percent: int) -> None:
    try:
        callback(step, max(0, min(100, int(percent))))
    except Exception as exc:
        raise IntegrationError("PROGRESS_CALLBACK_FAILED", "更新任务进度失败") from exc


def _cancel_requested(callback: Callable[[], bool]) -> bool:
    try:
        return bool(callback())
    except Exception as exc:
        raise IntegrationError("CANCEL_CALLBACK_FAILED", "检查取消状态失败") from exc


def _check_canceled(callback: Callable[[], bool]) -> None:
    if _cancel_requested(callback):
        raise CanceledError()


def _run_ffmpeg(
    arguments: list[str],
    *,
    step: str,
    percent: int,
    progress: Callable[[str, int], None],
    canceled: Callable[[], bool],
) -> None:
    _check_canceled(canceled)
    command = [_binary("ffmpeg"), "-hide_banner", "-loglevel", "error", "-nostats", "-y", *arguments]
    with tempfile.TemporaryFile() as stderr_file:
        try:
            process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=stderr_file)
        except OSError as exc:
            raise IntegrationError("FFMPEG_FAILED", "无法启动 FFmpeg") from exc
        last_heartbeat = 0.0
        try:
            while process.poll() is None:
                if _cancel_requested(canceled):
                    raise CanceledError()
                now = time.monotonic()
                if now - last_heartbeat >= 1.0:
                    _notify(progress, step, percent)
                    last_heartbeat = now
                time.sleep(0.1)
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=3)
        if process.returncode != 0:
            raise IntegrationError("FFMPEG_FAILED", f"FFmpeg 在“{step}”阶段执行失败")
    _notify(progress, step, percent)


def _font(
    settings: dict, width: int
) -> tuple[ImageFont.FreeTypeFont | ImageFont.ImageFont, Path | None, str]:
    configured = str(settings.get("font_path") or "").strip()
    candidates = [Path(configured).expanduser()] if configured else list(_FONT_CANDIDATES)
    font_size = max(16, min(88, width // 14))
    for candidate in candidates:
        if not candidate.is_file():
            continue
        try:
            loaded = ImageFont.truetype(str(candidate), font_size)
            family = loaded.getname()[0]
            safe_family = "".join(char for char in family if char not in "\\':;,[]") or "sans-serif"
            return loaded, candidate.resolve(), safe_family
        except OSError:
            continue
    if configured:
        raise IntegrationError("INVALID_FONT", "无法加载已配置的字体")
    try:
        loaded = ImageFont.truetype("DejaVuSans.ttf", font_size)
        family = loaded.getname()[0]
        return loaded, None, family
    except OSError:
        return ImageFont.load_default(), None, "sans-serif"


def _wrap_title(
    draw: ImageDraw.ImageDraw, title: str, font: ImageFont.ImageFont, max_width: int
) -> list[str]:
    lines: list[str] = []
    current = ""
    for character in title:
        candidate = current + character
        left, _top, right, _bottom = draw.textbbox((0, 0), candidate, font=font)
        if current and right - left > max_width:
            lines.append(current)
            current = character
        else:
            current = candidate
    if current:
        lines.append(current)
    if len(lines) > 1 and all(character in "，。！？；：、,.!?;:" for character in lines[-1]):
        combined = lines[-2] + lines[-1]
        candidates = []
        for index in range(1, len(combined)):
            left, right = combined[:index], combined[index:]
            if right[0] in "，。！？；：、,.!?;:":
                continue
            if left[-1].isascii() and right[0].isascii() and not (left[-1].isspace() or right[0].isspace()):
                continue
            left_width = draw.textlength(left, font=font)
            right_width = draw.textlength(right, font=font)
            if max(left_width, right_width) <= max_width:
                candidates.append((abs(left_width - right_width), left, right))
        if candidates:
            _, lines[-2], lines[-1] = min(candidates)
    return lines


def _scene_canvas(
    image_path: Path,
    width: int,
    height: int,
    *,
    reserve_avatar: bool = False,
    zoom_max: float = 1.0,
    pan_margin: float = 0.0,
) -> Image.Image:
    """Keep the whole source inside its own region, including at the largest animation zoom."""
    # Apply the inverse of the final center zoom to all four region edges.
    # This preserves the picture's corners throughout the animation rather than
    # covering the frame and cropping text, charts, or subjects from its sides.
    left = round(width / 2 + (width * 0.05 + pan_margin - width / 2) / zoom_max)
    right = round(width / 2 + (width * 0.95 - pan_margin - width / 2) / zoom_max)
    top = round(height / 2 + (height * 0.20 - height / 2) / zoom_max)
    bottom = round(height / 2 + (height * (0.64 if reserve_avatar else 0.70) - height / 2) / zoom_max)
    try:
        with Image.open(image_path) as source:
            fitted = ImageOps.contain(
                ImageOps.exif_transpose(source).convert("RGB"),
                (max(1, right - left - 4), max(1, bottom - top - 4)),
                method=Image.Resampling.LANCZOS,
            )
    except (OSError, UnidentifiedImageError) as exc:
        raise IntegrationError("INVALID_IMAGE", "无法读取场景图片") from exc
    canvas = Image.new("RGB", (width, height), (18, 30, 48))
    canvas.paste(
        fitted, (left + (right - left - fitted.width) // 2, top + (bottom - top - fitted.height) // 2)
    )
    return canvas


def _make_cover(
    image_path: Path, title: str, destination: Path, width: int, height: int, font: ImageFont.ImageFont
) -> None:
    cover = _scene_canvas(image_path, width, height)
    draw = ImageDraw.Draw(cover, "RGBA")
    padding = max(12, width // 14)
    title_top, title_bottom = round(height * 0.04), round(height * 0.18)
    while True:
        lines = _wrap_title(draw, title, font, width - 2 * padding)
        if not lines:
            raise IntegrationError("INVALID_TITLE", "请填写视频标题")
        spacing = max(2, width // 100)
        boxes = [draw.textbbox((0, 0), line, font=font) for line in lines]
        line_heights = [max(1, box[3] - box[1]) for box in boxes]
        total_height = sum(line_heights) + spacing * (len(lines) - 1)
        if total_height <= title_bottom - title_top:
            break
        if not isinstance(font, ImageFont.FreeTypeFont) or font.size <= 8:
            raise IntegrationError("CAPTION_TOO_LONG", "封面标题过长，请缩短后重新合成")
        font = font.font_variant(size=max(8, font.size - 2))
    y = title_top + (title_bottom - title_top - total_height) // 2
    for line, line_height in zip(lines, line_heights):
        draw.text(
            (width // 2, y),
            line,
            font=font,
            anchor="mt",
            fill=(255, 255, 255, 255),
            stroke_width=1,
            stroke_fill=(0, 0, 0, 220),
        )
        y += line_height + spacing
    cover.save(destination, format="PNG", optimize=True)


def _srt_time(seconds: float) -> str:
    milliseconds = max(0, round(seconds * 1000))
    hours, milliseconds = divmod(milliseconds, 3_600_000)
    minutes, milliseconds = divmod(milliseconds, 60_000)
    whole_seconds, milliseconds = divmod(milliseconds, 1_000)
    return f"{hours:02d}:{minutes:02d}:{whole_seconds:02d},{milliseconds:03d}"


def _subtitle_text(value: Any) -> str:
    return " ".join(str(value or "").replace("\x00", "").split()).strip()


def _safe_srt_text(value: str) -> str:
    # SubRip renderers may interpret HTML/ASS controls; render source text literally.
    return value.translate(str.maketrans({"<": "＜", ">": "＞", "{": "｛", "}": "｝"}))


def _write_srt(entries: list[tuple[float, float, str]], destination: Path) -> None:
    blocks = []
    for index, (start, end, text) in enumerate(entries, 1):
        blocks.append(f"{index}\n{_srt_time(start)} --> {_srt_time(end)}\n{_safe_srt_text(text)}\n")
    destination.write_text("\n".join(blocks), encoding="utf-8")


def _style_number(style: dict, key: str, default: float, low: float, high: float) -> float:
    try:
        value = float(style.get(key, default))
    except (TypeError, ValueError):
        return default
    if not math.isfinite(value):
        return default
    return max(low, min(high, value))


def _local_input(path: Path) -> list[str]:
    return [
        "-protocol_whitelist",
        _LOCAL_PROTOCOLS,
        "-format_whitelist",
        _SAFE_INPUT_FORMATS,
        "-i",
        str(path),
    ]


def _ass_time(seconds: float) -> str:
    value = max(0, round(seconds * 100))
    hours, value = divmod(value, 360_000)
    minutes, value = divmod(value, 6_000)
    whole, fraction = divmod(value, 100)
    return f"{hours}:{minutes:02d}:{whole:02d}.{fraction:02d}"


def _emphasized_ass(lines: list[str], terms: list[str]) -> str:
    text = "".join(lines)
    safe_terms = {_safe_srt_text(term).replace("\\", "＼") for term in terms if term}
    marked = [False] * len(text)
    if safe_terms:
        pattern = "|".join(
            re.escape(term) for term in sorted(safe_terms, key=lambda term: (-len(term), term))
        )
        for match in re.finditer(pattern, text):
            marked[match.start() : match.end()] = [True] * (match.end() - match.start())
    output, offset = [], 0
    for line in lines:
        rendered, highlighted = [], False
        for index, character in enumerate(line, offset):
            if marked[index] != highlighted:
                highlighted = marked[index]
                rendered.append(r"{\c&H00D7FF&}" if highlighted else r"{\c&HFFFFFF&}")
            rendered.append(character)
        if highlighted:
            rendered.append(r"{\c&HFFFFFF&}")
        output.append("".join(rendered))
        offset += len(line)
    return r"\N".join(output)


def _write_ass(
    entries,
    headings,
    destination,
    font_path,
    font_name,
    width,
    height,
    subtitle_terms=None,
    heading_terms=None,
    reserve_avatar=False,
):
    """Explicit video-sized coordinates avoid SubRip's default reference-resolution font scaling."""
    subtitle_size = max(8, round(width * 0.048))
    heading_size = max(8, min(round(width * 0.060), round(height * 0.026)))
    side = max(10, round(width * 0.08))
    narration_right = round(width * 0.38) if reserve_avatar else side
    styles = [
        "[Script Info]",
        "ScriptType: v4.00+",
        f"PlayResX: {width}",
        f"PlayResY: {height}",
        "ScaledBorderAndShadow: yes",
        "WrapStyle: 2",
        "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        f"Style: Narration,{font_name},{subtitle_size},&H00FFFFFF,&H00FFFFFF,&H00302010,&H90000000,0,0,0,0,100,100,0,0,1,2,1,2,{side},{narration_right},{round(height * 0.15)},1",
        f"Style: Heading,{font_name},{heading_size},&H00FFFFFF,&H00FFFFFF,&H00302010,&H90000000,-1,0,0,0,100,100,0,0,1,2,1,5,{side},{side},0,1",
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    measuring = ImageDraw.Draw(Image.new("RGB", (width, height)))
    for label, events, size, highlights in (
        ("Narration", entries, subtitle_size, subtitle_terms),
        ("Heading", headings, heading_size, heading_terms),
    ):
        try:
            font = ImageFont.truetype(str(font_path) if font_path else "DejaVuSans.ttf", size)
        except OSError:
            font = ImageFont.load_default()
        for index, (start, end, text) in enumerate(events):
            safe = _safe_srt_text(text).replace("\\", "＼")
            right = narration_right if label == "Narration" else side
            lines = _wrap_title(measuring, safe, font, width - side - right)
            if len(lines) > (3 if label == "Narration" else 4):
                raise IntegrationError("CAPTION_TOO_LONG", "单段字幕或画面标题过长，请拆分短句后重新合成")
            styles.append(
                f"Dialogue: 0,{_ass_time(start)},{_ass_time(end)},{label},,0,0,0,,"
                + (rf"{{\pos({width // 2},{round(height * 0.11)})}}" if label == "Heading" else "")
                + _emphasized_ass(lines, highlights[index] if highlights else [])
            )
    destination.write_text("\n".join(styles) + "\n", encoding="utf-8")


def compose_video(
    *,
    title: str,
    scenes: list[dict],
    output_dir: Path,
    settings: dict,
    progress: Callable[[str, int], None],
    canceled: Callable[[], bool],
) -> dict:
    """Compose a vertical H.264/AAC MP4 and its cover/SRT artifacts."""

    if not callable(progress) or not callable(canceled):
        raise IntegrationError("INVALID_CALLBACK", "缺少进度或取消回调")
    clean_title = _subtitle_text(title)
    if not clean_title or len(clean_title) > 500:
        raise IntegrationError("INVALID_TITLE", "视频标题不能为空且不能超过 500 个字符")
    if not isinstance(scenes, list) or not scenes or len(scenes) > 100:
        raise IntegrationError("INVALID_SCENES", "场景数量必须在 1 到 100 之间")
    if not isinstance(settings, dict):
        raise IntegrationError("INVALID_SETTINGS", "视频设置无效")
    try:
        width = int(settings.get("width", 1080))
        height = int(settings.get("height", 1920))
        fps = int(settings.get("fps", 30))
    except (TypeError, ValueError) as exc:
        raise IntegrationError("INVALID_SETTINGS", "视频尺寸或帧率无效") from exc
    if not 64 <= width <= 4096 or not 64 <= height <= 4096 or not 1 <= fps <= 60:
        raise IntegrationError("INVALID_SETTINGS", "视频尺寸或帧率超出安全范围")
    if width % 2 or height % 2:
        raise IntegrationError("INVALID_SETTINGS", "视频宽高必须为偶数")

    try:
        destination = Path(output_dir).expanduser()
        destination.mkdir(parents=True, exist_ok=True)
    except (TypeError, OSError) as exc:
        raise IntegrationError("INVALID_OUTPUT", "无法准备视频输出目录") from exc
    if not destination.is_dir():
        raise IntegrationError("INVALID_OUTPUT", "视频输出目录无效")

    style = settings.get("style") if isinstance(settings.get("style"), dict) else {}
    fade_seconds = _style_number(style, "fade_seconds", 0.35, 0.0, 1.0)
    zoom_max = _style_number(style, "zoom_max", 1.10, 1.0, 1.2)
    bgm_volume = _style_number(style, "bgm_volume", 0.12, 0.0, 0.3)
    image_paths: list[Path] = []
    scene_audio: list[list[tuple[Path, float, str]]] = []
    subtitle_entries: list[tuple[float, float, str]] = []
    heading_entries: list[tuple[float, float, str]] = []
    subtitle_terms: list[list[str]] = []
    heading_terms: list[list[str]] = []
    motions: list[str] = []
    transitions: list[str] = []
    total_duration = 0.0
    total_input_bytes = 0

    _check_canceled(canceled)
    _notify(progress, "检查媒体", 2)
    for scene in scenes:
        if not isinstance(scene, dict):
            raise IntegrationError("INVALID_SCENES", "场景数据无效")
        motion = scene.get("motion", "auto")
        transition = scene.get("transition", "fade")
        if not isinstance(motion, str) or not isinstance(transition, str):
            raise IntegrationError("INVALID_ANIMATION", "镜头动画或转场类型无效")
        if motion not in {
            "auto",
            "none",
            "push_in",
            "pull_out",
            "pan_left",
            "pan_right",
        } or transition not in {"fade", "cut"}:
            raise IntegrationError("INVALID_ANIMATION", "镜头动画或转场类型无效")
        motions.append("push_in" if motion == "auto" else motion)
        transitions.append(transition)
        image_path = _input_file(scene.get("image_path"), kind="场景图片")
        image_paths.append(image_path)
        total_input_bytes += image_path.stat().st_size
        segments = scene.get("audio_segments")
        if not isinstance(segments, list) or not segments or len(segments) > 100:
            raise IntegrationError("MISSING_AUDIO", "每个场景至少需要一段音频")
        prepared_segments: list[tuple[Path, float, str]] = []
        fallback_caption = _subtitle_text(scene.get("caption"))
        terms = scene.get("highlight_terms", [])
        if (
            not isinstance(terms, list)
            or len(terms) > 8
            or any(not isinstance(term, str) or not 1 <= len(term) <= 40 for term in terms)
        ):
            raise IntegrationError("INVALID_CAPTION", "字幕关键词格式无效")
        scene_start = total_duration
        for segment in segments:
            if not isinstance(segment, dict):
                raise IntegrationError("INVALID_AUDIO", "音频片段数据无效")
            audio_path = _input_file(segment.get("path"), kind="音频")
            metadata = probe_media(audio_path)
            if not metadata["has_audio"] or metadata["duration"] <= 0:
                raise IntegrationError("INVALID_AUDIO", "音频片段中没有可用音轨")
            text = _subtitle_text(segment.get("text"))
            if not text and len(segments) == 1:
                text = fallback_caption
            if not text:
                raise IntegrationError("MISSING_CAPTION", "每个音频片段都需要字幕文本")
            if len(text) > 2_000:
                raise IntegrationError("INVALID_CAPTION", "单段字幕不能超过 2,000 个字符")
            start = total_duration
            total_duration += metadata["duration"]
            phrases = split_narration(text)
            total_weight = sum(len(phrase) for phrase in phrases)
            elapsed_weight = 0
            for phrase in phrases:
                phrase_start = start + metadata["duration"] * elapsed_weight / total_weight
                elapsed_weight += len(phrase)
                phrase_end = start + metadata["duration"] * elapsed_weight / total_weight
                subtitle_entries.append((phrase_start, phrase_end, phrase))
                subtitle_terms.append(terms)
            prepared_segments.append((audio_path, metadata["duration"], text))
            total_input_bytes += audio_path.stat().st_size
        scene_audio.append(prepared_segments)
        if fallback_caption:
            heading_entries.append((scene_start, total_duration, fallback_caption))
            heading_terms.append(terms)
    if total_duration <= 0 or total_duration > 3_600:
        raise IntegrationError("INVALID_DURATION", "合成视频时长超出安全范围")
    timing_mode = settings.get("timing_mode", "natural")
    if timing_mode not in {"natural", "fit_target"}:
        raise IntegrationError("INVALID_SETTINGS", "视频时长模式无效")
    try:
        target_duration = (
            float(settings.get("target_seconds") or total_duration)
            if timing_mode == "fit_target"
            else total_duration
        )
    except (TypeError, ValueError) as error:
        raise IntegrationError("INVALID_DURATION", "目标时长无效") from error
    tempo = total_duration / target_duration if target_duration > 0 else 0
    if not math.isfinite(target_duration) or not 0 < target_duration <= 3_600 or not 0.5 <= tempo <= 2.0:
        raise IntegrationError(
            "NARRATION_LENGTH_MISMATCH",
            "旁白自然时长与目标差距过大，请调整文案或目标时长；系统不会自动重新调用配音",
        )
    time_scale = target_duration / total_duration
    subtitle_entries = [(start * time_scale, end * time_scale, text) for start, end, text in subtitle_entries]
    heading_entries = [(start * time_scale, end * time_scale, text) for start, end, text in heading_entries]

    bgm_path = _input_file(settings["bgm_path"], kind="背景音乐") if settings.get("bgm_path") else None
    logo_path = _input_file(settings["logo_path"], kind="Logo") if settings.get("logo_path") else None
    if bgm_path:
        bgm_metadata = probe_media(bgm_path)
        if not bgm_metadata["has_audio"]:
            raise IntegrationError("INVALID_AUDIO", "背景音乐中没有可用音轨")
        total_input_bytes += bgm_path.stat().st_size
    if logo_path:
        total_input_bytes += logo_path.stat().st_size

    required_free = max(64 * 1024 * 1024, total_input_bytes * 4)
    temp_root = Path(tempfile.gettempdir())
    if min(shutil.disk_usage(destination).free, shutil.disk_usage(temp_root).free) < required_free:
        raise IntegrationError("INSUFFICIENT_STORAGE", "磁盘空间不足，无法合成视频")

    font, font_path, font_name = _font(settings, width)
    final_video = destination / "video.mp4"
    final_cover = destination / "cover.png"
    final_subtitle = destination / "subtitles.srt"

    with tempfile.TemporaryDirectory(prefix="local-publisher-compose-") as work_value:
        work = Path(work_value)
        temp_cover = work / "cover.png"
        temp_subtitle = work / "captions.srt"
        temp_ass = work / "captions.ass"
        temp_video = work / "video.mp4"
        if font_path:
            copied_font = work / ("font" + font_path.suffix)
            shutil.copyfile(font_path, copied_font)
            font_path = copied_font
        _make_cover(image_paths[0], clean_title, temp_cover, width, height, font)
        _write_srt(subtitle_entries, temp_subtitle)
        _write_ass(
            subtitle_entries,
            heading_entries,
            temp_ass,
            font_path,
            font_name,
            width,
            height,
            subtitle_terms,
            heading_terms,
            bool(settings.get("avatar_enabled")),
        )

        rendered_scenes: list[Path] = []
        for index, (image_path, segments) in enumerate(zip(image_paths, scene_audio)):
            _check_canceled(canceled)
            audio_output = work / f"scene-{index:03d}.m4a"
            audio_arguments: list[str] = []
            for audio_path, _duration, _text in segments:
                audio_arguments.extend(_local_input(audio_path))
            if len(segments) == 1:
                audio_arguments.extend(
                    [
                        "-map",
                        "0:a:0",
                        "-vn",
                        "-af",
                        "aresample=48000,aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo",
                    ]
                )
            else:
                chains = [
                    f"[{item_index}:a:0]aresample=48000,aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo[a{item_index}]"
                    for item_index in range(len(segments))
                ]
                inputs = "".join(f"[a{item_index}]" for item_index in range(len(segments)))
                chains.append(f"{inputs}concat=n={len(segments)}:v=0:a=1[aout]")
                audio_arguments.extend(["-filter_complex", ";".join(chains), "-map", "[aout]", "-vn"])
            scene_duration = sum(item[1] for item in segments)
            audio_arguments.extend(
                ["-t", f"{scene_duration:.6f}", "-c:a", "aac", "-b:a", "192k", str(audio_output)]
            )
            _run_ffmpeg(
                audio_arguments,
                step=f"合成场景音频 {index + 1}/{len(scenes)}",
                percent=5 + int(20 * (index + 1) / len(scenes)),
                progress=progress,
                canceled=canceled,
            )

            scene_output = work / f"scene-{index:03d}.mp4"
            scene_image = work / f"scene-{index:03d}.png"
            motion = motions[index]
            scene_zoom = 1.0 if motion == "none" else zoom_max
            pan_margin = (
                min(width * 0.035, width * (scene_zoom - 1) / 2) if motion.startswith("pan_") else 0.0
            )
            _scene_canvas(
                image_path,
                width,
                height,
                reserve_avatar=bool(settings.get("avatar_enabled")),
                zoom_max=scene_zoom,
                pan_margin=pan_margin,
            ).save(scene_image, format="PNG")
            fade = min(fade_seconds, scene_duration / 4) if transitions[index] == "fade" else 0.0
            frame_count = max(2, round(scene_duration * fps))
            eased = f"(0.5-0.5*cos(PI*min(on/{frame_count - 1},1)))"
            zoom = "1"
            x = "iw/2-(iw/zoom/2)"
            if motion == "push_in":
                zoom = f"1+{scene_zoom - 1:.6f}*{eased}"
            elif motion == "pull_out":
                zoom = f"{scene_zoom:.6f}-{scene_zoom - 1:.6f}*{eased}"
            elif motion.startswith("pan_"):
                zoom = f"{scene_zoom:.6f}"
                direction = 1 if motion == "pan_left" else -1
                x += f"+{direction}*{pan_margin:.6f}/zoom*(2*{eased}-1)"
            video_filter = f"zoompan=z='{zoom}':x='{x}':y='ih/2-(ih/zoom/2)':d=1:s={width}x{height}:fps={fps}"
            if fade > 0:
                video_filter += f",fade=t=in:st=0:d={fade:.4f},fade=t=out:st={max(0.0, scene_duration - fade):.4f}:d={fade:.4f}"
            video_filter += ",format=yuv420p"
            _run_ffmpeg(
                [
                    "-loop",
                    "1",
                    *_local_input(scene_image),
                    *_local_input(audio_output),
                    "-map",
                    "0:v:0",
                    "-map",
                    "1:a:0",
                    "-vf",
                    video_filter,
                    "-t",
                    f"{scene_duration:.6f}",
                    "-r",
                    str(fps),
                    "-c:v",
                    "libx264",
                    "-preset",
                    "medium",
                    "-crf",
                    "20",
                    "-c:a",
                    "aac",
                    "-b:a",
                    "192k",
                    "-movflags",
                    "+faststart",
                    str(scene_output),
                ],
                step=f"渲染场景 {index + 1}/{len(scenes)}",
                percent=25 + int(45 * (index + 1) / len(scenes)),
                progress=progress,
                canceled=canceled,
            )
            rendered_scenes.append(scene_output)

        final_arguments: list[str] = []
        for scene_path in rendered_scenes:
            final_arguments.extend(_local_input(scene_path))
        bgm_index = None
        if bgm_path:
            bgm_index = len(rendered_scenes)
            final_arguments.extend(["-stream_loop", "-1", *_local_input(bgm_path)])
        logo_index = None
        if logo_path:
            logo_index = len(rendered_scenes) + (1 if bgm_path else 0)
            final_arguments.extend(_local_input(logo_path))

        filters: list[str] = []
        if len(rendered_scenes) == 1:
            filters.extend(["[0:v:0]setpts=PTS-STARTPTS[vbase]", "[0:a:0]asetpts=PTS-STARTPTS[abase]"])
        else:
            concat_inputs = "".join(f"[{index}:v:0][{index}:a:0]" for index in range(len(rendered_scenes)))
            filters.append(f"{concat_inputs}concat=n={len(rendered_scenes)}:v=1:a=1[vbase][abase]")
        filters.append(f"[vbase]setpts={time_scale:.10f}*PTS[vpaced]")
        filters.append(
            "[abase]anull[apaced]" if timing_mode == "natural" else f"[abase]atempo={tempo:.10f}[apaced]"
        )
        # Keep cloud and local voices at a consistent mobile-friendly programme level without changing pace.
        filters.append(
            "[apaced]loudnorm=I=-16:TP=-1.5:LRA=11,"
            "aformat=sample_rates=48000:channel_layouts=stereo[voice]"
        )
        filters.append(f"[vpaced]ass=filename='{temp_ass}':fontsdir='{work}'[subbed]")
        if logo_index is not None:
            logo_width = max(24, int(width * 0.16))
            margin = max(8, int(width * 0.035))
            filters.extend(
                [
                    f"[{logo_index}:v:0]scale={logo_width}:-1[logo]",
                    f"[subbed][logo]overlay=W-w-{margin}:{margin}[vout]",
                ]
            )
        else:
            filters.append("[subbed]null[vout]")
        if bgm_index is not None:
            filters.extend(
                [
                    f"[{bgm_index}:a:0]volume={bgm_volume:.4f}[music]",
                    "[voice][music]amix=inputs=2:duration=first:dropout_transition=0[aout]",
                ]
            )
        else:
            filters.append("[voice]anull[aout]")

        final_arguments.extend(
            [
                "-filter_complex",
                ";".join(filters),
                "-map",
                "[vout]",
                "-map",
                "[aout]",
                "-t",
                f"{target_duration:.6f}",
                "-r",
                str(fps),
                "-c:v",
                "libx264",
                "-preset",
                "medium",
                "-crf",
                "20",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                "-b:a",
                "192k",
                "-movflags",
                "+faststart",
                str(temp_video),
            ]
        )
        _run_ffmpeg(
            final_arguments,
            step="合成视频",
            percent=90,
            progress=progress,
            canceled=canceled,
        )
        final_metadata = probe_media(temp_video)
        if (
            not final_metadata["has_video"]
            or not final_metadata["has_audio"]
            or final_metadata["duration"] <= 0
        ):
            raise IntegrationError("INVALID_OUTPUT", "合成视频缺少有效画面或音轨")
        _check_canceled(canceled)
        temp_video.replace(final_video)
        temp_cover.replace(final_cover)
        temp_subtitle.replace(final_subtitle)

    _notify(progress, "完成", 100)
    return {
        "video_path": str(final_video.resolve()),
        "cover_path": str(final_cover.resolve()),
        "subtitle_path": str(final_subtitle.resolve()),
        "duration": float(final_metadata["duration"]),
        "natural_duration": total_duration,
        "speech_tempo": tempo,
        "voice_loudness_target_lufs": -16.0,
        "timing_mode": timing_mode,
        "subtitle_timing": "measured_audio; multi_sentence_clips_split_by_text_weight",
    }


def overlay_avatar(*, video_path: Path, avatar_path: Path, output_path: Path, progress, canceled) -> dict:
    """Overlay the locally generated portrait while retaining only the base narration audio."""
    base = probe_media(video_path)
    portrait = probe_media(avatar_path)
    if not base["has_video"] or not base["has_audio"] or not portrait["has_video"]:
        raise IntegrationError("AVATAR_MEDIA_INVALID", "数字人叠加需要有效成片、旁白和人像视频")
    if abs(base["duration"] - portrait["duration"]) > 0.3:
        raise IntegrationError("AVATAR_DURATION_MISMATCH", "数字人与成片时长不一致，未覆盖原成片")
    width, height = base["width"], base["height"]
    portrait_width, portrait_height = portrait["width"], portrait["height"]
    if not all((width, height, portrait_width, portrait_height)):
        raise IntegrationError("AVATAR_MEDIA_INVALID", "视频缺少有效尺寸")
    scale = min(width * 0.26 / portrait_width, height * 0.20 / portrait_height)
    overlay_width = max(2, int(portrait_width * scale) // 2 * 2)
    overlay_height = max(2, int(portrait_height * scale) // 2 * 2)
    x, y = width - overlay_width - round(width * 0.08), height - overlay_height - round(height * 0.15)
    destination = Path(output_path).resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination in {Path(video_path).resolve(), Path(avatar_path).resolve()}:
        raise IntegrationError("AVATAR_OUTPUT_CONFLICT", "数字人叠加不能覆盖输入素材")
    with tempfile.TemporaryDirectory(prefix="avatar-overlay-", dir=destination.parent) as temporary:
        target = Path(temporary) / "composed.mp4"
        _run_ffmpeg(
            [
                *_local_input(Path(video_path).resolve()),
                *_local_input(Path(avatar_path).resolve()),
                "-filter_complex",
                f"[1:v]scale={overlay_width}:{overlay_height},setsar=1[portrait];[0:v][portrait]overlay={x}:{y}:eof_action=repeat:shortest=0,format=yuv420p[v]",
                "-map",
                "[v]",
                "-map",
                "0:a:0",
                "-c:v",
                "libx264",
                "-preset",
                "medium",
                "-crf",
                "20",
                "-c:a",
                "copy",
                "-t",
                f"{base['duration']:.6f}",
                "-movflags",
                "+faststart",
                str(target),
            ],
            step="合成右下角数字人",
            percent=95,
            progress=progress,
            canceled=canceled,
        )
        final = probe_media(target)
        if not final["has_audio"] or (final["width"], final["height"]) != (width, height):
            raise IntegrationError("AVATAR_OUTPUT_INVALID", "数字人叠加结果未通过检查")
        _check_canceled(canceled)
        target.replace(destination)
    return {"video_path": str(destination), "duration": final["duration"]}
