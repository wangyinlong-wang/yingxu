from __future__ import annotations

import os
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[2]


def local_timezone() -> str:
    candidates = [os.environ.get("TZ", "")]
    try:
        target = str(Path("/etc/localtime").resolve())
        if "zoneinfo/" in target:
            candidates.append(target.split("zoneinfo/", 1)[1])
    except OSError:
        pass
    for candidate in candidates:
        try:
            ZoneInfo(candidate)
            return candidate
        except (ValueError, ZoneInfoNotFoundError):
            continue
    return "UTC"


class AppConfig(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="PUBLISHER_", env_file=ROOT / ".env", extra="ignore")

    data_dir: Path = ROOT / "data"
    timezone: str = Field(default_factory=local_timezone)
    port: int = 8765
    adb_path: Path | None = None
    text_api_key: SecretStr = SecretStr("")
    image_api_key: SecretStr = SecretStr("")
    speech_api_key: SecretStr = SecretStr("")
    video_api_key: SecretStr = SecretStr("")
    hypit_cli: Path = ROOT / ".tools/hypit/node_modules/.bin/hypit"
    browser_use_python: Path = ROOT / ".tools/browser-use/.venv/bin/python"
    chrome_path: Path = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
    tikhub_api_key: SecretStr = SecretStr("")

    @property
    def database_url(self) -> str:
        return f"sqlite:///{self.data_dir.resolve() / 'publisher.sqlite3'}"

    def key(self, kind: str) -> str:
        if kind not in {"text", "image", "speech", "video"}:
            raise ValueError("Unknown model capability")
        return getattr(self, f"{kind}_api_key").get_secret_value()

    def prepare_directories(self) -> None:
        for name in ("", "assets", "runs", "logs", "backups"):
            path = self.data_dir / name
            path.mkdir(parents=True, exist_ok=True, mode=0o700)

    def configure_tools(self) -> None:
        if self.adb_path and self.adb_path.is_file():
            parent = str(self.adb_path.resolve().parent)
            paths = os.environ.get("PATH", "").split(os.pathsep)
            if parent not in paths:
                os.environ["PATH"] = parent + os.pathsep + os.environ.get("PATH", "")


def default_model_profile(kind: str) -> dict:
    return {
        "base_url": "",
        "model": "",
        "timeout_seconds": 180 if kind != "text" else 90,
        "voice": "alloy",
        "size": "1024x1536",
        "json_mode": False,
        "enabled": False,
        "protocol": "minimax_h3" if kind == "video" else "openai",
        "language_type": "Chinese",
        "max_completion_tokens": None,
        "enable_thinking": None,
        "watermark": False,
        "instruction": "",
    }


def default_settings(timezone: str) -> dict:
    # Stored records keep the legacy shape on purpose: db.settings resolves the additive
    # source/local profile fields in memory, which keeps the voice_enabled->builtin mapping
    # intact for every record that never saved an explicit source choice.
    return {
        "models": {kind: default_model_profile(kind) for kind in ("text", "image", "speech", "video")},
        "video": {
            "renderer": "ffmpeg",
            "clip_duration": 6,
            "clip_resolution": "768P",
            "width": 1080,
            "height": 1920,
            "fps": 30,
            "target_seconds": 60,
            "timing_mode": "natural",
            "font_path": "",
            "bgm_asset_id": None,
            "logo_asset_id": None,
        },
        "limits": {"daily_calls": 40, "daily_videos": 1},
        "publishing": {"auto_publish_after_approval": False},
        "local_ai": {
            "voice_enabled": False,
            "avatar_enabled": False,
            "voice_model": "qwen3",
            "emotion_strength": 0.65,
            "reference_audio_asset_id": None,
            "reference_text": "",
            "portrait_asset_id": None,
        },
        "timezone": timezone,
    }
