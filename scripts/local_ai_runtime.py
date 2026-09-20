#!/usr/bin/env python3
"""Offline, one-request subprocess for MLX voice cloning and MuseTalk on MPS."""

from __future__ import annotations

import contextlib
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOCAL = ROOT / ".tools" / "local-ai"
MODELS = LOCAL / "models"
VENDOR = LOCAL / "vendor" / "musetalk"
_FORMATS = "mov,mp4,m4a,3gp,3g2,mj2,matroska,webm,wav,mp3,flac,ogg,aac"


class LocalError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


def binary(name):
    path = shutil.which(name)
    if path:
        return path
    for folder in ("/opt/homebrew/bin", "/usr/local/bin"):
        candidate = Path(folder) / name
        if candidate.is_file():
            return str(candidate)
    raise LocalError("LOCAL_MEDIA_TOOL_MISSING", "未找到本地音视频工具")


def media_info(path):
    source = Path(path).resolve()
    if not source.is_file() or source.stat().st_size > 500 * 1024**2:
        raise LocalError("LOCAL_MEDIA_INVALID", "本地输入素材不存在或过大")
    result = subprocess.run(
        [
            binary("ffprobe"),
            "-v",
            "error",
            "-protocol_whitelist",
            "file",
            "-format_whitelist",
            _FORMATS,
            "-i",
            str(source),
            "-show_format",
            "-show_streams",
            "-of",
            "json",
        ],
        capture_output=True,
        check=False,
        timeout=30,
    )
    if result.returncode:
        raise LocalError("LOCAL_MEDIA_INVALID", "本地输入不是有效音视频文件")
    payload = json.loads(result.stdout)
    duration = float(payload.get("format", {}).get("duration", 0))
    if not math.isfinite(duration) or not 0 < duration <= 180:
        raise LocalError(
            "LOCAL_MEDIA_DURATION", "本地音频或视频时长需大于 0 且不超过 180 秒"
        )
    return duration, payload


def audio_wav(source, destination, sample_rate=16000):
    duration, payload = media_info(source)
    if not any(
        stream.get("codec_type") == "audio" for stream in payload.get("streams", [])
    ):
        raise LocalError("LOCAL_AUDIO_REQUIRED", "素材中没有可用的人声音轨")
    result = subprocess.run(
        [
            binary("ffmpeg"),
            "-v",
            "error",
            "-y",
            "-protocol_whitelist",
            "file",
            "-format_whitelist",
            _FORMATS,
            "-i",
            str(source),
            "-vn",
            "-ac",
            "1",
            "-ar",
            str(sample_rate),
            "-c:a",
            "pcm_s16le",
            str(destination),
        ],
        capture_output=True,
        check=False,
        timeout=90,
    )
    if result.returncode:
        raise LocalError("LOCAL_AUDIO_DECODE_FAILED", "无法读取本地音频")
    return duration


def tts(request, *, chatterbox=False):
    import mlx.core as mx
    import numpy as np
    import soundfile as sf
    from mlx_audio.tts.utils import load_model

    text = str(request.get("text") or "").strip()
    reference_text = str(request.get("reference_text") or "").strip()
    if (
        not text
        or len(text) > 500
        or (not chatterbox and (not reference_text or len(reference_text) > 2000))
    ):
        raise LocalError(
            "LOCAL_VOICE_TEXT_REQUIRED", "请填写旁白及与参考录音一致的文字"
        )
    reference = Path(request["reference_audio_path"]).resolve()
    strength = 0.65
    if chatterbox:
        try:
            strength = float(request.get("emotion_strength", 0.65))
        except (TypeError, ValueError):
            raise LocalError(
                "LOCAL_EMOTION_INVALID", "表现力强度需在 0 到 1 之间"
            ) from None
        if not math.isfinite(strength) or not 0 <= strength <= 1:
            raise LocalError("LOCAL_EMOTION_INVALID", "表现力强度需在 0 到 1 之间")
    seconds, _ = media_info(reference)
    if not 3 <= seconds <= 30:
        raise LocalError("LOCAL_VOICE_REFERENCE_DURATION", "参考录音需为 3 至 30 秒")
    output = Path(request["output_path"]).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="voice-", dir=output.parent) as temporary:
        wav = Path(temporary) / "reference.wav"
        audio_wav(reference, wav, sample_rate=24000 if chatterbox else 16000)
        samples, reference_rate = sf.read(wav, dtype="float32")
        if not np.isfinite(samples).all() or np.max(np.abs(samples)) < 0.002:
            raise LocalError(
                "LOCAL_VOICE_REFERENCE_SILENT", "参考录音过于安静或包含无效采样"
            )
        mx.random.seed(0)
        model = load_model(str(MODELS / ("chatterbox" if chatterbox else "qwen3-tts")))
        if chatterbox and (
            model._s3_tokenizer is None
            or model.mtl_tokenizer is None
            or not model.mtl_tokenizer.cangjie_converter.word2cj
            or model.mtl_tokenizer.cangjie_converter.segmenter is None
        ):
            raise LocalError(
                "LOCAL_CHATTERBOX_NOT_INSTALLED",
                "Chatterbox 的中文前处理或语音编码器不完整",
            )
        mx.random.seed(0)
        options = (
            {
                "audio_prompt": mx.array(samples),
                "audio_prompt_sr": reference_rate,
                "lang_code": "zh",
                "exaggeration": strength,
                "cfg_weight": 0.35,
                "max_tokens": 4096,
            }
            if chatterbox
            else {
                "ref_audio": str(wav),
                "ref_text": reference_text,
                "lang_code": "Chinese",
                "max_tokens": 2400,
            }
        )
        chunks = [
            result.audio
            for result in model.generate(
                text=text,
                verbose=False,
                stream=False,
                **options,
            )
        ]
        if not chunks:
            raise LocalError("LOCAL_TTS_EMPTY", "本地声音模型没有生成音频")
        audio = mx.concatenate(chunks)
        mx.eval(audio)
        result = np.asarray(audio, dtype=np.float32).reshape(-1)
        rate = int(model.sample_rate)
        duration = len(result) / rate
        if (
            not np.isfinite(result).all()
            or not 0 < duration < 180
            or np.max(np.abs(result)) < 0.0001
        ):
            raise LocalError("LOCAL_TTS_INVALID", "本地声音输出无效或超过时长限制")
        target = Path(temporary) / "voice.wav"
        sf.write(target, result, rate, subtype="PCM_16")
        target.replace(output)
    return {
        "output_path": str(output),
        "duration": duration,
        "engine": "mlx",
        "model": "mlx-community/chatterbox-multilingual-v3"
        if chatterbox
        else "mlx-community/Qwen3-TTS-12Hz-1.7B-Base-bf16",
    }


def avatar_models():
    import torch
    from transformers import WhisperModel

    if not torch.backends.mps.is_available():
        raise LocalError(
            "LOCAL_MPS_UNAVAILABLE", "当前进程无法使用 Apple GPU，未改用其他模型"
        )
    if not (VENDOR / ".yingxu-revision").is_file():
        raise LocalError("LOCAL_AVATAR_NOT_INSTALLED", "本地数字人代码尚未安装")
    sys.path.insert(0, str(VENDOR))
    os.chdir(VENDOR)
    from musetalk.models.unet import PositionalEncoding, UNet
    from musetalk.models.vae import VAE
    from musetalk.utils.audio_processor import AudioProcessor
    from musetalk.utils.face_parsing import FaceParsing

    model_dir = MODELS / "avatar"
    os.environ["MUSETALK_DEVICE"] = "mps"
    device = torch.device("mps")
    torch.manual_seed(0)
    vae = VAE(model_path=str(model_dir / "sd-vae"))
    unet = UNet(
        unet_config=str(model_dir / "musetalkV15" / "musetalk.json"),
        model_path=str(model_dir / "musetalkV15" / "unet.pth"),
        device=device,
    )
    unet.model.eval()
    vae.vae.eval()
    pe = PositionalEncoding(384).to(device)
    whisper = (
        WhisperModel.from_pretrained(str(model_dir / "whisper"), local_files_only=True)
        .to(device)
        .eval()
    )
    processor = AudioProcessor(feature_extractor_path=str(model_dir / "whisper"))
    parser = FaceParsing()
    return torch, device, vae, unet, pe, whisper, processor, parser


def portrait_frame(path):
    import numpy as np
    import Quartz
    import Vision
    from Foundation import NSData
    from PIL import Image, ImageOps

    with Image.open(path) as image:
        if image.width * image.height > 40_000_000 or min(image.size) < 256:
            raise LocalError("LOCAL_PORTRAIT_SIZE", "照片尺寸不符合要求")
        image = ImageOps.exif_transpose(image).convert("RGB")
        image.thumbnail((960, 960))
        width, height = image.size
        image = image.resize((width - width % 2, height - height % 2))
        rgb = np.asarray(image)
    height, width = rgb.shape[:2]
    data = NSData.dataWithBytes_length_(rgb.tobytes(), rgb.nbytes)
    provider = Quartz.CGDataProviderCreateWithCFData(data)
    cg = Quartz.CGImageCreate(
        width,
        height,
        8,
        24,
        width * 3,
        Quartz.CGColorSpaceCreateDeviceRGB(),
        Quartz.kCGBitmapByteOrderDefault,
        provider,
        None,
        False,
        Quartz.kCGRenderingIntentDefault,
    )
    handler = Vision.VNImageRequestHandler.alloc().initWithCGImage_options_(cg, None)
    request = Vision.VNDetectFaceRectanglesRequest.alloc().init()
    ok, _ = handler.performRequests_error_([request], None)
    if not ok or len(request.results() or []) != 1:
        raise LocalError(
            "LOCAL_SINGLE_FACE_REQUIRED", "请使用清晰、无遮挡的单人正面照片"
        )
    sys.path.insert(0, str(VENDOR))
    from musetalk.utils.vision_landmarks import get_landmarks_68

    frame = np.ascontiguousarray(rgb[:, :, ::-1])
    points = get_landmarks_68(frame)
    if points is None or not np.isfinite(points).all():
        raise LocalError(
            "LOCAL_FACE_LANDMARKS_MISSING", "照片中的面部关键点无法识别，请更换正面照片"
        )
    lower = points[:, 1].max()
    upper = max(0, points[29, 1] - (lower - points[29, 1]))
    box = [
        max(0, int(points[:, 0].min())),
        int(upper),
        min(width, int(points[:, 0].max())),
        min(height, int(lower) + 10),
    ]
    if box[2] - box[0] < 64 or box[3] - box[1] < 64:
        raise LocalError(
            "LOCAL_FACE_TOO_SMALL", "照片中的人脸过小，请使用近一些的半身照"
        )
    return frame, box


def avatar(request):
    import cv2
    import numpy as np

    output = Path(request["output_path"]).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    frame, box = portrait_frame(Path(request["image_path"]).resolve())
    with tempfile.TemporaryDirectory(prefix="avatar-", dir=output.parent) as temporary:
        wav = Path(temporary) / "voice.wav"
        duration = audio_wav(Path(request["audio_path"]).resolve(), wav)
        torch, device, vae, unet, pe, whisper, processor, parser = avatar_models()
        from musetalk.utils.blending import (
            get_image_blending,
            get_image_prepare_material,
        )

        x1, y1, x2, y2 = box
        with torch.inference_mode():
            features, sample_count = processor.get_audio_feature(str(wav))
            chunks = processor.get_whisper_chunk(
                features, device, torch.float32, whisper, sample_count, fps=25
            )
            face = cv2.resize(
                frame[y1:y2, x1:x2], (256, 256), interpolation=cv2.INTER_LANCZOS4
            )
            latent = vae.get_latents_for_unet(face)
            mask, crop_box = get_image_prepare_material(
                frame, box, fp=parser, mode="jaw"
            )
            silent_video = Path(temporary) / "silent.mp4"
            height, width = frame.shape[:2]
            with tempfile.TemporaryFile() as errors:
                encoder = subprocess.Popen(
                    [
                        binary("ffmpeg"),
                        "-v",
                        "error",
                        "-y",
                        "-f",
                        "rawvideo",
                        "-pix_fmt",
                        "bgr24",
                        "-s",
                        f"{width}x{height}",
                        "-r",
                        "25",
                        "-i",
                        "pipe:0",
                        "-an",
                        "-c:v",
                        "libx264",
                        "-preset",
                        "fast",
                        "-crf",
                        "20",
                        "-pix_fmt",
                        "yuv420p",
                        str(silent_video),
                    ],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.DEVNULL,
                    stderr=errors,
                )
                try:
                    for start in range(0, len(chunks), 4):
                        batch = chunks[start : start + 4]
                        latents = latent.repeat(len(batch), 1, 1, 1).to(device)
                        prediction = unet.model(
                            latents,
                            torch.tensor([0], device=device),
                            encoder_hidden_states=pe(batch),
                        ).sample
                        frames = vae.decode_latents(prediction)
                        for generated in frames:
                            face = cv2.resize(
                                generated.astype(np.uint8), (x2 - x1, y2 - y1)
                            )
                            combined = get_image_blending(
                                frame, face, box, mask, crop_box
                            )
                            encoder.stdin.write(
                                np.ascontiguousarray(combined).tobytes()
                            )
                    encoder.stdin.close()
                    if encoder.wait(timeout=90) != 0:
                        raise LocalError(
                            "LOCAL_AVATAR_ENCODE_FAILED", "本地数字人视频编码失败"
                        )
                finally:
                    if encoder.poll() is None:
                        encoder.terminate()
                        encoder.wait(timeout=10)
        target = Path(temporary) / "avatar.mp4"
        result = subprocess.run(
            [
                binary("ffmpeg"),
                "-v",
                "error",
                "-y",
                "-i",
                str(silent_video),
                "-i",
                str(wav),
                "-map",
                "0:v:0",
                "-map",
                "1:a:0",
                "-c:v",
                "copy",
                "-c:a",
                "aac",
                "-t",
                str(duration),
                "-movflags",
                "+faststart",
                str(target),
            ],
            capture_output=True,
            check=False,
            timeout=120,
        )
        if result.returncode:
            raise LocalError("LOCAL_AVATAR_ENCODE_FAILED", "本地数字人音画合并失败")
        target.replace(output)
    return {
        "output_path": str(output),
        "duration": duration,
        "engine": "mps",
        "model": "MuseTalk-1.5",
    }


def transcribe(request):
    import numpy as np
    import soundfile as sf
    import torch
    from transformers import WhisperForConditionalGeneration, WhisperProcessor

    source = Path(request["audio_path"]).resolve()
    seconds, _ = media_info(source)
    if not 3 <= seconds <= 30:
        raise LocalError("LOCAL_VOICE_REFERENCE_DURATION", "参考录音需为 3 至 30 秒")
    folder = MODELS / "avatar" / "whisper"
    with tempfile.TemporaryDirectory(
        prefix="transcribe-", dir=LOCAL / "cache"
    ) as temporary:
        wav = Path(temporary) / "reference.wav"
        audio_wav(source, wav)
        samples, rate = sf.read(wav, dtype="float32")
        if np.max(np.abs(samples)) < 0.002:
            raise LocalError("LOCAL_VOICE_REFERENCE_SILENT", "参考录音过于安静")
        processor = WhisperProcessor.from_pretrained(str(folder), local_files_only=True)
        model = WhisperForConditionalGeneration.from_pretrained(
            str(folder), local_files_only=True
        ).eval()
        with torch.inference_mode():
            features = processor(
                samples, sampling_rate=rate, return_tensors="pt"
            ).input_features
            tokens = model.generate(
                features, language="zh", task="transcribe", max_new_tokens=256
            )
        text = processor.batch_decode(tokens, skip_special_tokens=True)[0].strip()
        if not text:
            raise LocalError(
                "LOCAL_TRANSCRIPT_EMPTY", "未识别到清晰文字，请手动填写与录音一致的内容"
            )
    return {"transcript": text, "engine": "cpu", "model": "whisper-tiny"}


def probe(component):
    if component == "chatterbox":
        import mlx.core as mx
        from mlx_audio.tts.utils import load_model

        model = load_model(str(MODELS / "chatterbox"))
        if (
            model._s3_tokenizer is None
            or model.mtl_tokenizer is None
            or not model.mtl_tokenizer.cangjie_converter.word2cj
            or model.mtl_tokenizer.cangjie_converter.segmenter is None
        ):
            raise LocalError(
                "LOCAL_CHATTERBOX_NOT_INSTALLED", "Chatterbox 的编码器或分词器不完整"
            )
        mx.eval(model.parameters(), model._s3_tokenizer.parameters())
        model._tokenize_text("本地语音环境检查", "zh")
        return {
            "engine": "mlx",
            "model": "mlx-community/chatterbox-multilingual-v3",
            "sample_rate": model.sample_rate,
            "gpu": mx.device_info().get("device_name"),
            "emotion_control": True,
        }
    if component == "tts":
        import mlx.core as mx
        from mlx_audio.tts.utils import load_model

        model = load_model(str(MODELS / "qwen3-tts"))
        mx.eval(model.parameters())
        if model.speech_tokenizer is None:
            raise LocalError("LOCAL_TTS_NOT_INSTALLED", "本地声音模型的解码器不完整")
        return {
            "engine": "mlx",
            "model": "mlx-community/Qwen3-TTS-12Hz-1.7B-Base-bf16",
            "sample_rate": model.sample_rate,
            "gpu": mx.device_info().get("device_name"),
        }
    if component == "avatar":
        torch, device, vae, unet, pe, whisper, _, _ = avatar_models()
        with torch.inference_mode():
            prediction = unet.model(
                torch.zeros((1, 8, 32, 32), device=device),
                torch.tensor([0], device=device),
                encoder_hidden_states=pe(torch.zeros((1, 50, 384), device=device)),
            ).sample
            decoded = vae.decode_latents(prediction)
            whisper.encoder(torch.zeros((1, 80, 3000), device=device))
        return {
            "engine": "mps",
            "model": "MuseTalk-1.5",
            "decoded_shape": list(decoded.shape),
        }
    if component == "transcribe":
        from transformers import WhisperForConditionalGeneration, WhisperProcessor

        path = str(MODELS / "avatar" / "whisper")
        WhisperProcessor.from_pretrained(path, local_files_only=True)
        WhisperForConditionalGeneration.from_pretrained(path, local_files_only=True)
        return {"engine": "cpu", "model": "whisper-tiny"}
    raise LocalError("LOCAL_OPERATION_INVALID", "本地模型组件无效")


def main():
    # Offline inference has no use for outbound proxies. Removing them also
    # prevents optional SOCKS transport imports before Hugging Face's offline check.
    for key in list(os.environ):
        if key.lower().endswith("_proxy"):
            os.environ.pop(key, None)
    os.environ.update(
        HF_HUB_OFFLINE="1",
        TRANSFORMERS_OFFLINE="1",
        HF_HUB_DISABLE_TELEMETRY="1",
        HF_HUB_DISABLE_IMPLICIT_TOKEN="1",
        HF_HOME=str(LOCAL / "cache" / "huggingface"),
        HF_HUB_CACHE=str(LOCAL / "cache" / "huggingface" / "hub"),
        PKUSEG_HOME=str(LOCAL / "cache" / "pkuseg"),
        NUMBA_CACHE_DIR=str(LOCAL / "cache" / "numba"),
        PYTORCH_ENABLE_MPS_FALLBACK="0",
    )
    os.environ.pop("MUSETALK_TAESD", None)
    (LOCAL / "cache").mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    try:
        raw = sys.stdin.buffer.read(64001)
        if len(raw) > 64000:
            raise LocalError("LOCAL_REQUEST_TOO_LARGE", "本地请求超过限制")
        request = json.loads(raw)
        operation = request.get("operation")
        with contextlib.redirect_stdout(sys.stderr):
            if operation == "probe":
                result = probe(request.get("component"))
            elif operation == "tts":
                result = tts(request)
            elif operation == "chatterbox":
                result = tts(request, chatterbox=True)
            elif operation == "avatar":
                result = avatar(request)
            elif operation == "transcribe":
                result = transcribe(request)
            else:
                raise LocalError("LOCAL_OPERATION_INVALID", "本地任务类型无效")
        result["elapsed_seconds"] = round(time.monotonic() - started, 3)
        print("LOCAL_AI_RESULT=" + json.dumps(result, ensure_ascii=False), flush=True)
    except Exception as error:  # noqa: BLE001 - isolate all model errors without exposing personal input
        print(
            "LOCAL_AI_RESULT="
            + json.dumps(
                {
                    "error": {
                        "code": getattr(error, "code", "LOCAL_INFERENCE_FAILED"),
                        "message": getattr(
                            error, "message", "本地推理失败：" + type(error).__name__
                        ),
                    }
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
        print("Local runtime error type: " + type(error).__name__, file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
