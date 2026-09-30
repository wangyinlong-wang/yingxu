from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from app.integrations import media
from app.integrations.errors import CanceledError, IntegrationError
from app.integrations.media import compose_video, probe_media


@pytest.fixture
def deterministic_codec_fixture(tmp_path: Path) -> tuple[Path, Path]:
    """Local geometry and sine audio used only to verify codecs, never as voice."""

    image_path = tmp_path / "geometry.png"
    image = Image.new("RGB", (320, 240), (18, 38, 70))
    draw = ImageDraw.Draw(image)
    draw.rectangle((40, 30, 280, 210), outline=(245, 180, 45), width=10)
    draw.line((40, 210, 280, 30), fill=(80, 210, 180), width=8)
    image.save(image_path)

    ffmpeg = shutil.which("ffmpeg") or "/opt/homebrew/bin/ffmpeg"
    audio_path = tmp_path / "codec-sine.wav"
    subprocess.run(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=48000:duration=0.7",
            "-c:a",
            "pcm_s16le",
            str(audio_path),
        ],
        check=True,
    )
    return image_path, audio_path


def test_compose_video_real_short_encode_with_unicode(deterministic_codec_fixture, tmp_path):
    image_path, audio_path = deterministic_codec_fixture
    output_dir = tmp_path / "output"
    events = []

    result = compose_video(
        title="本地自动发布测试",
        scenes=[
            {
                "image_path": str(image_path),
                "audio_segments": [{"path": str(audio_path), "text": "第一句：<b>{真实}</b>时长。"}],
                "caption": "第一句：<b>{真实}</b>时长。",
            }
        ],
        output_dir=output_dir,
        settings={"width": 160, "height": 240, "fps": 10, "font_path": ""},
        progress=lambda step, percent: events.append((step, percent)),
        canceled=lambda: False,
    )

    metadata = probe_media(Path(result["video_path"]))
    assert metadata["width"] == 160
    assert metadata["height"] == 240
    assert metadata["video_codec"] == "h264"
    assert metadata["audio_codec"] == "aac"
    assert 0.5 <= result["duration"] <= 1.0
    assert Path(result["cover_path"]).is_file()
    subtitle = Path(result["subtitle_path"]).read_text(encoding="utf-8")
    assert "第一句：＜b＞｛真实｝＜/b＞时长。" in subtitle
    assert "<b>" not in subtitle
    assert "00:00:00,000 -->" in subtitle
    assert events[-1] == ("完成", 100)
    assert result["voice_loudness_target_lufs"] == -16.0


def test_card_scene_uses_full_frame_without_duplicate_heading(deterministic_codec_fixture, tmp_path):
    _, audio_path = deterministic_codec_fixture
    card = tmp_path / "source-card.png"
    media.render_card(
        scene_type="text_card",
        visual_data={"headline": "Codex CLI 0.155.1", "body": "来源：Codex_Changelog\nAI 数字人演绎"},
        destination=card,
        width=320,
        height=568,
        palette=["#315bff", "#111827", "#f7f8fb", "#f2c94c"],
        caption="来源与演绎说明",
    )
    result = compose_video(
        title="片尾排版",
        scenes=[{
            "scene_type": "text_card",
            "image_path": str(card),
            "audio_segments": [{"path": str(audio_path), "text": "内容根据原帖整理，由人工智能数字人演绎。"}],
            "caption": "来源与演绎说明",
            "motion": "none",
            "transition": "cut",
        }],
        output_dir=tmp_path / "rendered-card",
        settings={"width": 320, "height": 568, "fps": 10},
        progress=lambda *_: None,
        canceled=lambda: False,
    )
    raw = subprocess.run(
        [media._binary("ffmpeg"), "-v", "error", "-ss", "0.3", "-i", result["video_path"],
         "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1"],
        capture_output=True,
        check=True,
    ).stdout
    frame = Image.frombytes("RGB", (320, 568), raw)
    assert frame.getpixel((50, round(568 * 0.325)))[2] > 100  # Full-width accent rule is visible.
    assert max(max(frame.getpixel((x, y))) for x in range(100, 220, 10) for y in range(20, 100, 10)) < 100


def test_srt_uses_each_audio_segments_actual_duration(deterministic_codec_fixture, tmp_path):
    image_path, first_audio = deterministic_codec_fixture
    ffmpeg = shutil.which("ffmpeg") or "/opt/homebrew/bin/ffmpeg"
    second_audio = tmp_path / "codec-sine-second.wav"
    subprocess.run(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=660:sample_rate=48000:duration=0.4",
            "-c:a",
            "pcm_s16le",
            str(second_audio),
        ],
        check=True,
    )

    result = compose_video(
        title="分句字幕",
        scenes=[
            {
                "image_path": str(image_path),
                "audio_segments": [
                    {"path": str(first_audio), "text": "第一句"},
                    {"path": str(second_audio), "text": "第二句"},
                ],
                "caption": "第一句 第二句",
            }
        ],
        output_dir=tmp_path / "segmented-output",
        settings={"width": 160, "height": 240, "fps": 10},
        progress=lambda *_args: None,
        canceled=lambda: False,
    )

    subtitle = Path(result["subtitle_path"]).read_text(encoding="utf-8")
    assert "00:00:00,000 --> 00:00:00,700\n第一句" in subtitle
    assert "00:00:00,700 --> 00:00:01,100\n第二句" in subtitle


def test_whole_scene_voice_uses_readable_sentence_cues_without_changing_audio_speed(
    deterministic_codec_fixture, tmp_path
):
    image_path, audio_path = deterministic_codec_fixture
    result = compose_video(
        title="完整镜头",
        scenes=[
            {
                "image_path": str(image_path),
                "audio_segments": [{"path": str(audio_path), "text": "第一句。第二句。"}],
                "caption": "完整镜头",
            }
        ],
        output_dir=tmp_path / "sentence-cues",
        settings={"width": 160, "height": 240, "fps": 10},
        progress=lambda *_: None,
        canceled=lambda: False,
    )
    subtitle = Path(result["subtitle_path"]).read_text()
    assert "00:00:00,000 --> 00:00:00,350\n第一句。" in subtitle
    assert "00:00:00,350 --> 00:00:00,700\n第二句。" in subtitle
    assert result["speech_tempo"] == 1.0


def test_compose_video_supports_bounded_local_bgm_and_logo(deterministic_codec_fixture, tmp_path):
    image_path, audio_path = deterministic_codec_fixture
    result = compose_video(
        title="背景音乐与 Logo",
        scenes=[
            {
                "image_path": str(image_path),
                "audio_segments": [{"path": str(audio_path), "text": "本地素材"}],
                "caption": "本地素材",
            }
        ],
        output_dir=tmp_path / "bgm-logo-output",
        settings={
            "width": 160,
            "height": 240,
            "fps": 10,
            "bgm_path": str(audio_path),
            "logo_path": str(image_path),
            "style": {"bgm_volume": 0.1},
        },
        progress=lambda *_args: None,
        canceled=lambda: False,
    )

    metadata = probe_media(Path(result["video_path"]))
    assert metadata["has_video"] is True
    assert metadata["has_audio"] is True


def test_compose_video_rejects_missing_media(tmp_path):
    with pytest.raises(IntegrationError) as caught:
        compose_video(
            title="测试",
            scenes=[
                {
                    "image_path": str(tmp_path / "missing.png"),
                    "audio_segments": [{"path": str(tmp_path / "missing.wav"), "text": "文本"}],
                    "caption": "文本",
                }
            ],
            output_dir=tmp_path / "out",
            settings={"width": 160, "height": 240, "fps": 10},
            progress=lambda *_args: None,
            canceled=lambda: False,
        )

    assert caught.value.code == "MEDIA_NOT_FOUND"


def test_compose_video_honors_cancellation_before_work(deterministic_codec_fixture, tmp_path):
    image_path, audio_path = deterministic_codec_fixture
    with pytest.raises(CanceledError):
        compose_video(
            title="测试",
            scenes=[
                {
                    "image_path": str(image_path),
                    "audio_segments": [{"path": str(audio_path), "text": "文本"}],
                    "caption": "文本",
                }
            ],
            output_dir=tmp_path / "out",
            settings={"width": 160, "height": 240, "fps": 10},
            progress=lambda *_args: None,
            canceled=lambda: True,
        )


def test_probe_media_rejects_missing_path(tmp_path):
    with pytest.raises(IntegrationError) as caught:
        probe_media(tmp_path / "missing.mp4")
    assert caught.value.code == "MEDIA_NOT_FOUND"


@pytest.mark.parametrize("motion", ["none", "push_in", "pull_out", "pan_left", "pan_right"])
def test_animation_preserves_all_picture_corners_and_separates_title(
    deterministic_codec_fixture, tmp_path, motion
):
    _, audio_path = deterministic_codec_fixture
    source = Image.new("RGB", (400, 400), "white")
    draw = ImageDraw.Draw(source)
    for rectangle, color in (
        ((0, 0, 59, 59), (240, 25, 25)),
        ((340, 0, 399, 59), (25, 220, 25)),
        ((0, 340, 59, 399), (25, 25, 240)),
        ((340, 340, 399, 399), (240, 25, 220)),
    ):
        draw.rectangle(rectangle, fill=color)
    picture = tmp_path / "picture-with-corner-labels.png"
    source.save(picture)
    result = compose_video(
        title="独立封面标题",
        scenes=[
            {
                "image_path": str(picture),
                "audio_segments": [{"path": str(audio_path), "text": "底部字幕"}],
                "caption": "独立标题",
                "motion": motion,
                "transition": "cut",
            }
        ],
        output_dir=tmp_path / motion,
        settings={"width": 320, "height": 568, "fps": 10, "avatar_enabled": True, "style": {"zoom_max": 1.2}},
        progress=lambda *_: None,
        canceled=lambda: False,
    )
    checks = (
        lambda r, g, b: r > 170 and g < 85 and b < 85,
        lambda r, g, b: g > 150 and r < 85 and b < 85,
        lambda r, g, b: b > 170 and r < 85 and g < 85,
        lambda r, g, b: r > 170 and b > 150 and g < 85,
    )
    corner_centers = []
    for second in (0.0, 0.6):
        raw = subprocess.run(
            [
                media._binary("ffmpeg"),
                "-v",
                "error",
                "-ss",
                str(second),
                "-i",
                result["video_path"],
                "-frames:v",
                "1",
                "-f",
                "rawvideo",
                "-pix_fmt",
                "rgb24",
                "pipe:1",
            ],
            capture_output=True,
            check=True,
        ).stdout
        frame = Image.frombytes("RGB", (320, 568), raw)
        pixels = list(frame.get_flattened_data())
        centers = []
        for check in checks:
            locations = [(index % 320, index // 320) for index, color in enumerate(pixels) if check(*color)]
            assert len(locations) > 100, "A source corner was cropped or hidden"
            xs, ys = zip(*locations)
            assert min(xs) >= 12 and max(xs) <= 308
            assert min(ys) >= 110 and max(ys) <= 367, "Picture overlaps heading or presenter area"
            centers.append((sum(xs) / len(xs), sum(ys) / len(ys)))
        assert sum(1 for r, g, b in pixels[: 320 * 100] if min(r, g, b) > 170) > 20, (
            "Heading missing from separate top area"
        )
        corner_centers.append(centers)
    first, last = corner_centers
    if motion == "pan_left":
        assert last[0][0] < first[0][0] - 10
    elif motion == "pan_right":
        assert last[0][0] > first[0][0] + 10
    elif motion == "push_in":
        assert last[1][0] - last[0][0] > first[1][0] - first[0][0] + 10
    elif motion == "pull_out":
        assert last[1][0] - last[0][0] < first[1][0] - first[0][0] - 10
    else:
        assert last[0] == pytest.approx(first[0], abs=1)


@pytest.mark.parametrize("picture_size", [(640, 360), (360, 640), (400, 400)])
def test_cover_displays_whole_picture_below_its_title(tmp_path, picture_size):
    source = Image.new("RGB", picture_size, (235, 30, 30))
    picture = tmp_path / "whole-picture.png"
    source.save(picture)
    cover = tmp_path / "cover.png"
    font, _, _ = media._font({}, 320)
    media._make_cover(picture, "封面标题单独排版，不覆盖原图中的文字", cover, 320, 568, font)
    with Image.open(cover) as frame:
        red = [
            (i % 320, i // 320)
            for i, (r, g, b) in enumerate(frame.convert("RGB").get_flattened_data())
            if r > 200 and g < 60 and b < 60
        ]
    xs, ys = zip(*red)
    assert min(ys) >= 113
    observed_ratio = (max(xs) - min(xs) + 1) / (max(ys) - min(ys) + 1)
    assert observed_ratio == pytest.approx(picture_size[0] / picture_size[1], rel=0.02)


def test_wrapping_does_not_leave_sentence_punctuation_on_its_own_line():
    font, _, _ = media._font({}, 320)
    draw = ImageDraw.Draw(Image.new("RGB", (320, 568)))
    text = "结果 Qwen 3.8 变化最大。"
    limit = draw.textlength(text[:-1], font=font)
    lines = media._wrap_title(draw, text, font, int(limit))
    assert "".join(lines) == text
    assert len(lines) > 1
    assert all(any(character not in "，。！？；：、,.!?;:" for character in line) for line in lines)
    assert any("3.8" in line for line in lines)


@pytest.mark.parametrize("motion", ["zoompan=z=99", {"command": "unsafe"}])
def test_compositor_rejects_arbitrary_animation_commands(deterministic_codec_fixture, tmp_path, motion):
    picture, audio = deterministic_codec_fixture
    with pytest.raises(IntegrationError) as caught:
        compose_video(
            title="测试",
            scenes=[
                {
                    "image_path": str(picture),
                    "audio_segments": [{"path": str(audio), "text": "文本"}],
                    "motion": motion,
                }
            ],
            output_dir=tmp_path / "invalid",
            settings={"width": 320, "height": 568},
            progress=lambda *_: None,
            canceled=lambda: False,
        )
    assert caught.value.code == "INVALID_ANIMATION"


def test_avatar_overlay_preserves_base_audio_and_uses_reserved_corner(deterministic_codec_fixture, tmp_path):
    image_path, audio_path = deterministic_codec_fixture
    base = compose_video(
        title="角落叠加",
        scenes=[
            {
                "image_path": str(image_path),
                "audio_segments": [{"path": str(audio_path), "text": "原始旁白"}],
                "caption": "测试",
            }
        ],
        output_dir=tmp_path / "base",
        settings={"width": 320, "height": 568, "fps": 10, "avatar_enabled": True},
        progress=lambda *_: None,
        canceled=lambda: False,
    )
    avatar = tmp_path / "synthetic-avatar.mp4"
    subprocess.run(
        [
            media._binary("ffmpeg"),
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=red:s=128x128:r=10:d=0.7",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=880:sample_rate=48000:duration=0.7",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-shortest",
            str(avatar),
        ],
        check=True,
    )
    result = media.overlay_avatar(
        video_path=Path(base["video_path"]),
        avatar_path=avatar,
        output_path=tmp_path / "with-avatar.mp4",
        progress=lambda *_: None,
        canceled=lambda: False,
    )
    frame = tmp_path / "frame.png"
    subprocess.run(
        [
            media._binary("ffmpeg"),
            "-v",
            "error",
            "-y",
            "-ss",
            "0.3",
            "-i",
            result["video_path"],
            "-frames:v",
            "1",
            str(frame),
        ],
        check=True,
    )
    with Image.open(frame) as image:
        red, green, blue = image.convert("RGB").getpixel((253, 442))
        assert red > 180 and green < 80 and blue < 80

    def decoded_audio(path):
        return subprocess.run(
            [
                media._binary("ffmpeg"),
                "-v",
                "error",
                "-i",
                str(path),
                "-map",
                "0:a:0",
                "-f",
                "s16le",
                "-acodec",
                "pcm_s16le",
                "pipe:1",
            ],
            capture_output=True,
            check=True,
        ).stdout

    assert decoded_audio(base["video_path"]) == decoded_audio(result["video_path"])


def test_avatar_layout_keeps_subtitles_to_the_left(tmp_path):
    target = tmp_path / "layout.ass"
    media._write_ass([(0, 1, "关键词")], [], target, None, "sans-serif", 1080, 1920, [["关键词"]], [], True)
    narration = next(line for line in target.read_text().splitlines() if line.startswith("Style: Narration"))
    assert narration.split(",")[-3] == "410"


@pytest.mark.parametrize("mode,seconds,tempo", [("natural", 0.7, 1.0), ("fit_target", 1.1, 0.7 / 1.1)])
def test_speech_only_changes_speed_when_explicitly_selected(
    deterministic_codec_fixture, tmp_path, mode, seconds, tempo
):
    image_path, audio_path = deterministic_codec_fixture
    result = compose_video(
        title="自然语速",
        scenes=[
            {
                "image_path": str(image_path),
                "audio_segments": [{"path": str(audio_path), "text": "保持自然"}],
                "caption": "自然",
                "highlight_terms": ["自然"],
            }
        ],
        output_dir=tmp_path / mode,
        settings={"width": 160, "height": 240, "fps": 10, "target_seconds": 1.1, "timing_mode": mode},
        progress=lambda *_args: None,
        canceled=lambda: False,
    )
    assert result["speech_tempo"] == pytest.approx(tempo)
    assert result["duration"] == pytest.approx(seconds, abs=0.15)
    assert result["natural_duration"] == pytest.approx(0.7)
    assert "{\\c" not in Path(result["subtitle_path"]).read_text()


def test_emphasis_survives_line_wrapping_and_escapes_source_controls(tmp_path):
    wrapped = media._emphasized_ass(["参数约3.", "85亿，开放"], ["3.85亿"])
    assert wrapped == "参数约{\\c&H00D7FF&}3.{\\c&HFFFFFF&}\\N{\\c&H00D7FF&}85亿{\\c&HFFFFFF&}，开放"
    target = tmp_path / "literal.ass"
    font_path = next((path for path in media._FONT_CANDIDATES if path.is_file()), None)
    media._write_ass(
        [(0, 1, "重点{\\pos(1,2)}"), (1, 2, "下一镜头重点")],
        [],
        target,
        font_path,
        "sans-serif",
        1080,
        1920,
        [["重点"], []],
        [],
    )
    events = [line for line in target.read_text().splitlines() if line.startswith("Dialogue:")]
    assert r"{\c&H00D7FF&}重点{\c&HFFFFFF&}" in events[0]
    assert r"{\pos" not in events[0]
    assert r"\c&H00D7FF&" not in events[1]


def test_probe_media_rejects_disguised_remote_playlist(tmp_path):
    disguised = tmp_path / "voice.mp3"
    disguised.write_text(
        "#EXTM3U\n#EXT-X-TARGETDURATION:10\n#EXTINF:10,\nhttp://127.0.0.1:9/private.ts\n",
        encoding="utf-8",
    )

    with pytest.raises(IntegrationError) as caught:
        probe_media(disguised)

    assert caught.value.code == "INVALID_MEDIA"


def test_ffmpeg_process_is_reaped_on_worker_interrupt(monkeypatch):
    class InterruptedProcess:
        def __init__(self):
            self.returncode = None
            self.terminated = False
            self.waited = False

        def poll(self):
            return self.returncode

        def terminate(self):
            self.terminated = True
            self.returncode = -15

        def wait(self, timeout):
            assert timeout == 3
            self.waited = True
            return self.returncode

        def kill(self):
            self.returncode = -9

    process = InterruptedProcess()
    cancellation_checks = iter((False,))

    def interrupted_after_start():
        try:
            return next(cancellation_checks)
        except StopIteration:
            raise KeyboardInterrupt from None

    monkeypatch.setattr(media, "_binary", lambda _name: "ffmpeg")
    monkeypatch.setattr(media.subprocess, "Popen", lambda *_args, **_kwargs: process)

    with pytest.raises(KeyboardInterrupt):
        media._run_ffmpeg(
            ["-version"],
            step="测试中断",
            percent=50,
            progress=lambda *_args: None,
            canceled=interrupted_after_start,
        )

    assert process.terminated is True
    assert process.waited is True


@pytest.mark.parametrize("native_audio", [False, True])
def test_generated_video_clip_holds_last_frame_and_keeps_narration(deterministic_codec_fixture, tmp_path, native_audio):
    image_path, audio_path = deterministic_codec_fixture
    clip = tmp_path / "red-clip.mp4"
    subprocess.run(
        [
            shutil.which("ffmpeg"),
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=red:s=160x240:r=10:d=0.3",
            *(["-i", str(audio_path), "-c:a", "aac"] if native_audio else []),
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(clip),
        ],
        check=True,
    )
    result = compose_video(
        title="视频素材回归",
        scenes=[
            {
                "image_path": str(image_path),
                "video_path": str(clip),
                "audio_segments": [{"path": str(clip if native_audio else audio_path), "text": "保留原旁白"}],
                "caption": "",
                "transition": "cut",
            }
        ],
        output_dir=tmp_path / "video-clip-output",
        settings={"width": 160, "height": 240, "fps": 10},
        progress=lambda *_: None,
        canceled=lambda: False,
    )
    metadata = probe_media(Path(result["video_path"]))
    assert metadata["has_audio"] and metadata["video_codec"] == "h264"
    assert abs(metadata["duration"] - 0.7) < 0.15
    assert "保留原旁白" in Path(result["subtitle_path"]).read_text()
    frame = tmp_path / "held.png"
    subprocess.run(
        [
            shutil.which("ffmpeg"),
            "-v",
            "error",
            "-y",
            "-ss",
            "0.5",
            "-i",
            result["video_path"],
            "-frames:v",
            "1",
            str(frame),
        ],
        check=True,
    )
    with Image.open(frame) as picture:
        red, green, blue = picture.convert("RGB").getpixel((80, 100))
        assert red > 150 and green < 80 and blue < 80
