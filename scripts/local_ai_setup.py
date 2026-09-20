#!/usr/bin/env python3
"""Install pinned local inference environments and model data, then verify offline."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import io
import json
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import time
import zipfile
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
LOCAL = ROOT / ".tools" / "local-ai"
VENDOR_COMMIT = "895747b82968320282dc9e20245ca7ded81aa79d"
REPOSITORIES = (
    (
        "mlx-community/chatterbox-multilingual-v3",
        "03565773edd72e949572557597af8063bb49a18a",
        "chatterbox",
        {
            "Cangjie5_TC.json",
            "README.md",
            "config.json",
            "model.safetensors",
            "tokenizer.json",
        },
    ),
    (
        "mlx-community/S3TokenizerV2",
        "e0c9886f0e1c35ae85b1f27277416fb19fc72bec",
        "s3tokenizer",
        {"README.md", "config.json", "model.safetensors"},
    ),
    (
        "mlx-community/Qwen3-TTS-12Hz-1.7B-Base-bf16",
        "a6eb4f68e4b056f1215157bb696209bc82a6db48",
        "qwen3-tts",
        None,
    ),
    (
        "TMElyralab/MuseTalk",
        "3ef28bc5cff08c90ad8178a25f1b570cd800170f",
        "avatar",
        {"musetalkV15/musetalk.json", "musetalkV15/unet.pth", "README.md"},
    ),
    (
        "stabilityai/sd-vae-ft-mse",
        "31f26fdeee1355a5c34592e401dd41e45d25a493",
        "avatar/sd-vae",
        {"config.json", "diffusion_pytorch_model.safetensors", "README.md"},
    ),
    (
        "openai/whisper-tiny",
        "169d4a4341b33bc18d8881c4b69c2e104e1cc0af",
        "avatar/whisper",
        {
            "config.json",
            "generation_config.json",
            "model.safetensors",
            "preprocessor_config.json",
            "tokenizer.json",
            "tokenizer_config.json",
            "vocab.json",
            "merges.txt",
            "normalizer.json",
            "added_tokens.json",
            "special_tokens_map.json",
            "README.md",
        },
    ),
)


def sha256(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def save_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".partial.json")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    temporary.replace(path)


def download(
    url: str, path: Path, *, expected_sha: str | None = None, size: int | None = None
) -> dict:
    if path.is_file() and (not size or path.stat().st_size == size):
        observed = sha256(path)
        if expected_sha is None or observed == expected_sha:
            return {"size": path.stat().st_size, "sha256": observed}
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_suffix(path.suffix + ".partial")
    for attempt in range(3):
        offset = partial.stat().st_size if partial.is_file() else 0
        try:
            with httpx.Client(
                timeout=httpx.Timeout(60, read=120), follow_redirects=True
            ) as client:
                headers = {"Range": f"bytes={offset}-"} if offset else {}
                with client.stream("GET", url, headers=headers) as response:
                    response.raise_for_status()
                    if response.status_code == 206:
                        if not response.headers.get("Content-Range", "").startswith(
                            f"bytes {offset}-"
                        ):
                            raise RuntimeError("Download range mismatch")
                    else:
                        offset = 0
                    downloaded = offset
                    next_report = downloaded + 256 * 1024 * 1024
                    with partial.open("ab" if offset else "wb") as output:
                        for block in response.iter_bytes(chunk_size=1024 * 1024):
                            downloaded += len(block)
                            if downloaded > (size or 10 * 1024**3):
                                raise RuntimeError(
                                    "Download exceeded declared model size"
                                )
                            output.write(block)
                            if downloaded >= next_report:
                                print(
                                    f"{path.name}: {downloaded // (1024**2)} MiB",
                                    flush=True,
                                )
                                next_report += 256 * 1024 * 1024
                        output.flush()
                        os.fsync(output.fileno())
            if size and partial.stat().st_size != size:
                raise RuntimeError("Incomplete model download")
            observed = sha256(partial)
            if expected_sha and observed != expected_sha:
                partial.unlink()
                raise RuntimeError("Model checksum mismatch")
            partial.replace(path)
            print(f"Verified {path.relative_to(LOCAL)}", flush=True)
            return {"size": path.stat().st_size, "sha256": observed}
        except (httpx.HTTPError, OSError) as error:
            if attempt == 2:
                raise RuntimeError(
                    f"Download failed for {path.name}: {type(error).__name__}"
                ) from None
            time.sleep(2)
    raise RuntimeError("Model download did not complete")


def install_environments():
    uv = shutil.which("uv")
    if not uv:
        raise RuntimeError("uv is required; use the existing project setup first")
    for component in ("tts", "avatar"):
        interpreter = LOCAL / component / "bin" / "python"
        if not interpreter.is_file():
            subprocess.run(
                [
                    uv,
                    "--cache-dir",
                    str(LOCAL / "cache" / "uv"),
                    "venv",
                    "--python",
                    sys._base_executable,
                    str(LOCAL / component),
                ],
                check=True,
            )
        subprocess.run(
            [
                uv,
                "pip",
                "sync",
                str(ROOT / "scripts" / "local_ai" / f"{component}.requirements.lock"),
                "--python",
                str(interpreter),
                "--cache-dir",
                str(LOCAL / "cache" / "uv"),
            ],
            check=True,
        )


def install_vendor():
    destination = LOCAL / "vendor" / "musetalk"
    marker = destination / ".yingxu-revision"
    if (
        marker.is_file()
        and marker.read_text().strip() == VENDOR_COMMIT + ":weights-only-v2"
    ):
        return
    if destination.exists():
        raise RuntimeError(
            "An unrecognized vendor directory exists; preserve it and inspect before updating"
        )
    url = f"https://codeload.github.com/stefanskiasan/MuseTalk-Metal/tar.gz/{VENDOR_COMMIT}"
    with httpx.Client(timeout=120, follow_redirects=True) as client:
        response = client.get(url)
        response.raise_for_status()
    if len(response.content) > 250 * 1024**2:
        raise RuntimeError("Vendor archive exceeded size limit")
    destination.mkdir(parents=True)
    with tarfile.open(fileobj=io.BytesIO(response.content), mode="r:gz") as archive:
        for member in archive.getmembers():
            relative = Path(*Path(member.name).parts[1:])
            if not member.isfile() or not relative.parts:
                continue
            if not (
                relative.parts[0] == "musetalk"
                and relative.suffix == ".py"
                or str(relative) in {"LICENSE", "README_METAL.md"}
            ):
                continue
            target = (destination / relative).resolve()
            if not target.is_relative_to(destination.resolve()):
                raise RuntimeError("Unsafe vendor archive path")
            target.parent.mkdir(parents=True, exist_ok=True)
            source = archive.extractfile(member)
            if source is None:
                raise RuntimeError("Missing vendor archive member")
            target.write_bytes(source.read())
    # Keep restricted state-dictionary loading throughout the vendored code.
    for relative in (
        "musetalk/models/unet.py",
        "musetalk/utils/face_parsing/__init__.py",
        "musetalk/utils/face_parsing/resnet.py",
    ):
        path = destination / relative
        source = path.read_text()
        if source.count("weights_only=False") != 1:
            raise RuntimeError("Pinned vendor patch no longer matches")
        path.write_text(source.replace("weights_only=False", "weights_only=True"))
    path = destination / "musetalk/utils/face_parsing/resnet.py"
    source = path.read_text()
    if source.count("        self.init_weight(model_path)") != 1:
        raise RuntimeError("Pinned backbone patch no longer matches")
    path.write_text(
        source.replace(
            "        self.init_weight(model_path)",
            "        # FaceParsing strictly loads the complete BiSeNet checkpoint, including this backbone.",
        )
    )
    (destination / "models").symlink_to(
        LOCAL / "models" / "avatar", target_is_directory=True
    )
    marker.write_text(VENDOR_COMMIT + ":weights-only-v2\n")


def install_chinese_frontend():
    """Preload the Chinese segmenter so personal-voice inference stays offline."""
    folder = LOCAL / "cache" / "pkuseg"
    archive_path = folder / "spacy_ontonotes.zip"
    url = "https://github.com/explosion/spacy-pkuseg/releases/download/v0.0.26/spacy_ontonotes.zip"
    expected = "b216e7f92de7ae285aeab8feba2faa8ea8216e5995ff6fb3d391cc8356db1bfe"
    metadata = download(url, archive_path, expected_sha=expected)
    destination = folder / "spacy_ontonotes"
    with zipfile.ZipFile(archive_path) as archive:
        if sum(item.file_size for item in archive.infolist()) > 512 * 1024**2:
            raise RuntimeError("Chinese model archive exceeds expected bounds")
        for item in archive.infolist():
            if item.is_dir():
                continue
            target = (destination / item.filename).resolve()
            if not target.is_relative_to(destination.resolve()):
                raise RuntimeError("Unsafe Chinese model archive path")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(archive.read(item))
    save_json(LOCAL / "chinese-frontend.json", {"source": url, **metadata})


def download_models(component="all"):
    record_path = LOCAL / f"downloads-{component}.json"
    record = (
        json.loads(record_path.read_text())
        if record_path.is_file()
        else {"models": {}, "files": {}}
    )
    for repo, revision, folder, wanted in REPOSITORIES:
        owner = (
            "chatterbox"
            if folder in {"chatterbox", "s3tokenizer"}
            else "tts"
            if folder == "qwen3-tts"
            else "avatar"
        )
        if component not in {"all", owner}:
            continue
        with httpx.Client(timeout=30, follow_redirects=True) as client:
            response = client.get(
                f"https://huggingface.co/api/models/{repo}/revision/{revision}",
                params={"blobs": "true"},
            )
            response.raise_for_status()
            metadata = response.json()
        if metadata["sha"] != revision:
            raise RuntimeError("Model revision mismatch")
        record["models"][repo] = {
            "revision": revision,
            "license": metadata.get("cardData", {}).get("license"),
        }
        for item in metadata["siblings"]:
            name = item["rfilename"]
            if wanted is not None and name not in wanted or name.startswith("."):
                continue
            path = LOCAL / "models" / folder / name
            if not path.resolve().is_relative_to((LOCAL / "models").resolve()):
                raise RuntimeError("Unsafe model file path")
            result = download(
                f"https://huggingface.co/{repo}/resolve/{revision}/{name}?download=true",
                path,
                expected_sha=item.get("lfs", {}).get("sha256"),
                size=item.get("size"),
            )
            record["files"][str(path.relative_to(LOCAL))] = {
                **result,
                "repository": repo,
                "revision": revision,
            }
            save_json(record_path, record)
    if component in {"all", "chatterbox"}:
        # The pinned MLX Chatterbox hook resolves this public companion through
        # Hugging Face's offline snapshot cache. Point it at our verified files.
        revision = "e0c9886f0e1c35ae85b1f27277416fb19fc72bec"
        cached_repo = (
            LOCAL / "cache/huggingface/hub/models--mlx-community--S3TokenizerV2"
        )
        snapshot = cached_repo / "snapshots" / revision
        snapshot.mkdir(parents=True, exist_ok=True)
        for name in ("config.json", "model.safetensors"):
            target = LOCAL / "models/s3tokenizer" / name
            link = snapshot / name
            if link.exists() or link.is_symlink():
                if link.resolve() != target.resolve():
                    raise RuntimeError(
                        "Companion model cache points to an unexpected file"
                    )
            else:
                link.symlink_to(target)
        refs = cached_repo / "refs"
        refs.mkdir(parents=True, exist_ok=True)
        (refs / "main").write_text(revision)
        install_chinese_frontend()
    if component in {"all", "avatar"}:
        extras = (
            (
                "https://drive.usercontent.google.com/download?id=154JgKpzCPW82qINcVieuPH3fZ2e0P812&export=download&confirm=t",
                "79999_iter.pth",
            ),
        )
        for url, name in extras:
            path = LOCAL / "models" / "avatar" / "face-parse-bisent" / name
            known = record["files"].get(str(path.relative_to(LOCAL)), {})
            result = download(url, path, expected_sha=known.get("sha256"))
            if result["size"] < 10_000_000:
                raise RuntimeError("Face checkpoint download is not a model file")
            record["files"][str(path.relative_to(LOCAL))] = {**result, "source": url}
            save_json(record_path, record)


def verify_components(selected):
    manifest_path = LOCAL / "manifest.json"
    manifest = (
        json.loads(manifest_path.read_text())
        if manifest_path.is_file()
        else {
            "schema_version": 1,
            "revision": "qwen-1.7b-musetalk15-local-v1",
            "components": {},
        }
    )
    for component, engine, model in (
        ("tts", "mlx", "mlx-community/Qwen3-TTS-12Hz-1.7B-Base-bf16"),
        ("chatterbox", "mlx", "mlx-community/chatterbox-multilingual-v3"),
        ("avatar", "mps", "MuseTalk-1.5"),
        ("transcribe", "cpu", "whisper-tiny"),
    ):
        owner = "avatar" if component in {"avatar", "transcribe"} else component
        if selected not in {"all", owner}:
            continue
        interpreter = (
            LOCAL
            / ("tts" if component in {"tts", "chatterbox"} else "avatar")
            / "bin"
            / "python"
        )
        environment = {
            k: v
            for k, v in os.environ.items()
            if not any(
                t in k.upper() for t in ("TOKEN", "SECRET", "API_KEY", "PASSWORD")
            )
        }
        environment.update(
            HF_HUB_OFFLINE="1",
            TRANSFORMERS_OFFLINE="1",
            HF_HUB_DISABLE_TELEMETRY="1",
            HF_HOME=str(LOCAL / "cache" / "huggingface"),
        )
        result = subprocess.run(
            [
                "/usr/bin/sandbox-exec",
                "-p",
                "(version 1)(allow default)(deny network*)",
                str(interpreter),
                str(ROOT / "scripts" / "local_ai_runtime.py"),
            ],
            input=json.dumps({"operation": "probe", "component": component}),
            text=True,
            capture_output=True,
            check=False,
            env=environment,
            timeout=600,
        )
        line = next(
            (
                line
                for line in result.stdout.splitlines()
                if line.startswith("LOCAL_AI_RESULT=")
            ),
            None,
        )
        value = (
            json.loads(line.split("=", 1)[1])
            if line
            else {
                "error": {"code": "PROBE_FAILED", "message": "Local model probe failed"}
            }
        )
        ready = result.returncode == 0 and "error" not in value
        manifest["components"][component] = {
            "installed": ready,
            "model": model,
            "engine": engine,
            "probe": value,
        }
        save_json(LOCAL / "manifest.json", manifest)
        print(
            json.dumps({"component": component, "installed": ready, "probe": value}),
            flush=True,
        )
        if not ready:
            raise RuntimeError(f"Offline verification failed for {component}")


def verify(selected):
    sys.path.insert(0, str(ROOT / "backend"))
    from app.config import AppConfig

    lock_path = AppConfig().data_dir / "runs" / "local-ai.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("ab") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        verify_components(selected)


def main():
    parser = argparse.ArgumentParser(
        description="Install and verify local voice/avatar models"
    )
    parser.add_argument("action", choices=("install", "download", "verify"))
    parser.add_argument(
        "--component", choices=("all", "tts", "chatterbox", "avatar"), default="all"
    )
    args = parser.parse_args()
    if sys.platform != "darwin" or platform.machine() != "arm64":
        raise RuntimeError("This installation requires an Apple Silicon Mac")
    LOCAL.mkdir(parents=True, exist_ok=True)
    if args.action == "install":
        install_environments()
    if args.action in {"install", "download"}:
        if args.component in {"all", "avatar"}:
            install_vendor()
        download_models(args.component)
    if args.action in {"install", "verify"}:
        verify(args.component)


if __name__ == "__main__":
    try:
        main()
    except (
        RuntimeError,
        OSError,
        subprocess.SubprocessError,
        httpx.HTTPError,
    ) as error:
        print(
            f"Local installation stopped: {type(error).__name__}: {error}",
            file=sys.stderr,
        )
        raise SystemExit(1) from None
