# 本地本人声音与照片数字人

本功能在 Apple Silicon Mac 上运行。语音可选 Qwen3-TTS 1.7B Base 或 Chatterbox Multilingual V3 的 MLX 实现，照片口型采用 MuseTalk 1.5 的 MPS 实现，参考录音转文字使用本地 Whisper tiny。照片与参考录音不会加入文本或图片模型的云端请求；本地推理子进程通过 macOS 网络隔离运行，禁止联网。

## 使用入口

打开工作台的“模板与设置 → 本人声音与数字人”，或访问 [本地配置页](http://127.0.0.1:8765/setup?tab=local-ai)。

1. 上传本人参考录音：3～30 秒，建议 10～20 秒；单人清晰讲话、无背景音乐。支持 WAV、MP3、M4A、AAC、FLAC、OGG。
2. 使用 Qwen3 时识别并核对“录音对应文字”，文字需要与录音一致。Chatterbox 仅需参考录音，不读取这段参考文字。
3. 上传本人正面照片：单人、面部清晰无遮挡、背景简单，最短边至少 256 像素。
4. 选择试听模型；Chatterbox 可调表现力强度 0～1，例如 0.35 和 0.75 对比。此参数实际传入模型的 `exaggeration`，不是朗读指导文本；越高不代表相似度或自然度一定更好。先生成声音试听，再生成照片数字人预览。数字人优先使用本次试听音频，没有试听时使用参考录音。
5. 满意后选择后续使用的模型与表现力强度，开启“后续新旁白使用本地本人声音”和需要的“右下角数字人”，保存设置。单次试听不会自动修改后续默认模型。

已有内容不会因配置变化自动被替换。在内容页选择“生成 → 用本人声音生成视频”，可明确替换旧旁白并重新合成；旧素材与已发布记录保留，新的成片需要重新审核。这项动作只使用现有图片与本地声音模型，不调用文本、图片或云语音接口。

Base 模型的声音特点主要来自参考录音和文案；现有云端配音配置与朗读指导仍保留，不被本地设置覆盖。本人声音与数字人不默认开启。参考录音、肖像和生成结果保存在本项目的素材目录中。

## 安装与维护

在项目根目录，使用现有工作台 Python 运行：

```sh
backend/.venv/bin/python scripts/local_ai_setup.py install
# 仅下载/补齐固定版本权重：
backend/.venv/bin/python scripts/local_ai_setup.py download
# 在断网约束下重新检查安装：
backend/.venv/bin/python scripts/local_ai_setup.py verify
# 为已有安装补齐 Chatterbox、语音编码器与中文分词：
backend/.venv/bin/python scripts/local_ai_setup.py install --component chatterbox
backend/.venv/bin/python scripts/local_ai_setup.py verify --component chatterbox
```

安装需要联网下载 Python 依赖与公开源码。原三组件权重约 8.5 GB，Chatterbox 与 S3 编码器新增约 3.2 GB 权重，另有中文分词资源和包缓存。模型文件与两个独立环境位于 `.tools/local-ai/`，不加入业务备份；现有后端的 Python 依赖没有安装 MLX 或 PyTorch。依赖版本记录在 `scripts/local_ai/*.requirements.lock`。安装验证期间与内容任务共用本地模型锁。

只有完成本机模型检查的组件才标记“已安装”。运行环境或权重缺失、安装版本变化、网络隔离不可用、GPU不可用时停止本地生成，不会切换到云服务。内容任务和预览共用串行队列；取消或超时只终止本任务的进程组。

## 固定组件

| 组件 | 固定版本 / 来源 |
|---|---|
| MLX Audio | `0.5.3`，独立 TTS 环境 |
| Qwen3-TTS | `mlx-community/Qwen3-TTS-12Hz-1.7B-Base-bf16`，revision `a6eb4f68e4b056f1215157bb696209bc82a6db48` |
| Chatterbox | `mlx-community/chatterbox-multilingual-v3`，revision `03565773edd72e949572557597af8063bb49a18a` |
| Chatterbox 语音编码器 | `mlx-community/S3TokenizerV2`，revision `e0c9886f0e1c35ae85b1f27277416fb19fc72bec` |
| 中文分词 | `spacy-pkuseg 1.0.1`，固定 `spacy_ontonotes` 数据，下载清单保存 SHA-256 |
| MuseTalk Mac 代码 | MuseTalk-Metal commit `895747b82968320282dc9e20245ca7ded81aa79d` |
| MuseTalk 1.5 权重 | `TMElyralab/MuseTalk`，revision `3ef28bc5cff08c90ad8178a25f1b570cd800170f` |
| SD VAE | `stabilityai/sd-vae-ft-mse`，revision `31f26fdeee1355a5c34592e401dd41e45d25a493` |
| Whisper tiny | `openai/whisper-tiny`，revision `169d4a4341b33bc18d8881c4b69c2e104e1cc0af` |

安装保存下载来源与文件哈希。大文件按 Hugging Face LFS 的 SHA-256 校验；数据采用 safetensors，MuseTalk 与人脸解析的 PyTorch 状态字典使用受限 `weights_only=True` 加载。没有启动社区封装中的云语音服务，也没有安装其训练用 TensorFlow 或网页演示依赖。

完整 BiSeNet 检查点已包含 ResNet 骨干参数，安装补丁取消了之前重复的旧格式 ResNet 初始化；完整检查点仍使用严格的参数匹配与受限加载。补丁标记为 `weights-only-v2`。

MuseTalk 代码为 MIT；所用权重的模型卡标注为 CreativeML OpenRAIL-M。其他模型的许可记录在下载清单和随附文档内，应按各自条款使用，不能把所有权重一概称为 MIT。

## 数据与接口

本地 AI 功能复用现有素材与设置记录，无独立数据库迁移。整个应用按当前代码迁移到最新 schema；设置位于 `local_ai`：

```json
{
  "voice_enabled": false,
  "avatar_enabled": false,
  "voice_model": "qwen3",
  "emotion_strength": 0.65,
  "reference_audio_asset_id": null,
  "reference_text": "",
  "portrait_asset_id": null
}
```

- `POST /api/v1/assets` 增加 `voice_reference` 和 `portrait` 类型，校验大小、真实格式、音频时长和图片尺寸。
- `GET /api/v1/local-ai/status` 只读检查 `tts`、`chatterbox`、`avatar`、`transcribe` 四项安装标记、模型文件与占用状态，不加载、下载或执行模型。
- `POST /api/v1/local-ai/transcribe` 接收参考录音素材 ID，返回任务；结果中的文字由用户核对后保存，不自动覆盖全局配置。
- `POST /api/v1/local-ai/preview` 提交本地声音或数字人测试任务；声音试听可覆盖 `voice_model`、`emotion_strength`，不修改全局设置。输入资料、文本、素材哈希、模型版本和表现力强度会固定在任务中。
- 内容生成增加 `stage=local_voice`，用于明确替换旧旁白并合成视频。

本地生成不写入云端计费调用记录。缓存包含实际使用的参考录音、文字、肖像、旁白、模型与表现力参数，改变输入会产生新缓存键。Chatterbox 不向推理进程传递参考文字；Qwen3 保留原有音频与文字复刻路径。模型加载时同时检查中文分词和编码器完整性，缺失即停止；离线运行不继承网络代理与云端密钥。任务结果先通过音视频检查再登记；数字人生成或叠加失败时，不会把缺少数字人的基础视频当作完成结果。

数字人叠加保留基础视频的音轨，忽略数字人文件中的重复音轨；字幕在启用数字人时为右侧留出空间。预览的口型视频保留照片原构图，不等同于重新生成全身动作。

本机安装与实际推理数据见 [本地功能验收记录](local-ai-validation.md)。声音相似度、口型自然程度取决于照片与参考录音；公开测试素材的运行通过不代表本人效果已经验收。

历史 Chatterbox 双档试听结果见 [Chatterbox 与 X 接入验收记录](chatterbox-x-validation.md)。表现力参数应由使用者根据自己的试听结果选择。

## 参考资料

- [MLX Audio](https://github.com/Blaizzy/mlx-audio)
- [Qwen3-TTS](https://github.com/QwenLM/Qwen3-TTS)
- [Chatterbox](https://github.com/resemble-ai/chatterbox)
- [MLX Chatterbox 模型](https://huggingface.co/mlx-community/chatterbox-multilingual-v3)
- [MuseTalk](https://github.com/TMElyralab/MuseTalk)
- [MuseTalk-Metal](https://github.com/stefanskiasan/MuseTalk-Metal)
