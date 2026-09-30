# 第三方组件与许可范围

本仓库原创代码和文档使用 [MIT License](LICENSE)。MIT 不会替代第三方依赖、模型权重、数据集、字体、素材或外部服务的原有条款。

本文件是组件来源索引，不是完整的传递依赖清单。发布包含第三方组件的二进制、安装包或镜像时，须按实际包含的版本检查并保留其许可文件与版权声明。

## 应用依赖

| 组件范围 | 版本与来源记录 | 说明 |
|---|---|---|
| FastAPI、SQLAlchemy、Alembic、Pydantic、HTTPX、Pillow、UIAutomator2 等 Python 包 | [backend/pyproject.toml](backend/pyproject.toml)、[backend/uv.lock](backend/uv.lock) | 各包及其传递依赖保留各自许可；安装环境内的包元数据与随附 LICENSE 是核对入口。 |
| Vue、Vue Router、Pinia、Element Plus、Vite、Playwright 等 JavaScript 包 | [frontend/package.json](frontend/package.json)、[frontend/pnpm-lock.yaml](frontend/pnpm-lock.yaml) | 按锁文件安装，保留实际发行物中的第三方声明。 |
| FFmpeg / ffprobe | 使用者安装的系统工具 | 许可取决于所用构建与启用的组件；参见 [FFmpeg 许可说明](https://ffmpeg.org/legal.html)。本仓库不分发其二进制。 |
| Android Platform Tools / ADB | 使用者安装的官方工具 | 参见 [官方发行页面](https://developer.android.com/tools/releases/platform-tools)；本仓库不分发平台工具或目标 App。 |
| 字体、图片、音频、肖像与参考作品 | 使用者提供或自行生成 | 使用者应具备相应使用授权；项目许可证不授予这些内容的权利。 |

## 可选本地 AI

本地模型与第三方源码由用户显式运行安装脚本后下载到 `.tools/local-ai/`，不包含在本仓库源码交付中。固定 revision、下载来源和文件校验由 [安装脚本](scripts/local_ai_setup.py)、[依赖锁文件目录](scripts/local_ai) 及本机下载清单记录。

| 可选组件 | 上游许可核对入口 |
|---|---|
| MLX Audio | [上游仓库](https://github.com/Blaizzy/mlx-audio) |
| Qwen3-TTS | [原始项目](https://github.com/QwenLM/Qwen3-TTS)、[本项目使用的 MLX 模型](https://huggingface.co/mlx-community/Qwen3-TTS-12Hz-1.7B-Base-bf16) |
| Chatterbox / S3Tokenizer | [原始项目](https://github.com/resemble-ai/chatterbox)、[MLX 模型](https://huggingface.co/mlx-community/chatterbox-multilingual-v3)、[编码器模型](https://huggingface.co/mlx-community/S3TokenizerV2) |
| MuseTalk / MuseTalk-Metal | [原始项目](https://github.com/TMElyralab/MuseTalk)、[Mac 实现](https://github.com/stefanskiasan/MuseTalk-Metal)、[模型卡](https://huggingface.co/TMElyralab/MuseTalk) |
| SD VAE | [模型卡](https://huggingface.co/stabilityai/sd-vae-ft-mse) |
| Whisper tiny | [模型卡](https://huggingface.co/openai/whisper-tiny) |

代码许可证与模型权重许可证可能不同，不能将以上内容统称为 MIT。下载新版本、再分发或商业使用前，应核对所选 revision 的实际条款。本地集成的安装和平台边界见 [本地 AI 文档](docs/local-ai.md)。

## 外部服务与独立项目

browser-use 0.13.10 作为可选本地浏览器控制运行时，安装到 `.tools/browser-use/.venv`，使用其 MIT 许可证；本项目没有打包 Google Chrome。完整传递依赖版本见 [运行时锁文件](scripts/browser_use/requirements.lock)，许可证以各安装分发包为准。上游：[browser-use](https://github.com/browser-use/browser-use)、[许可证](https://github.com/browser-use/browser-use/blob/main/LICENSE)。

OpenAI 兼容接口、火山方舟、百炼、Metaso MiniMax H3、TikHub、RSS 来源及目标发布平台分别适用其服务和内容条款。本项目没有附赠账号、额度、素材授权或平台合作资格；软件许可证不构成对这些服务的授权。

V2 设计参考了 OpenMontage 等公开项目的架构思路；本仓库没有将其源码作为依赖引入。Hypit 已作为独立可选本地渲染运行时接入，安装脚本下载固定版本 0.2.10；本仓库不分发其源码或二进制。Hypit 使用其自己的 source-available 许可，不属于本项目 MIT 授权范围；其许可证对内部使用、向第三方托管及商业分发规定了不同条件。使用与再分发须核对 [Hypit LICENSE](https://github.com/hypit-ai/hypit/blob/main/LICENSE)。HyperFrames、Chrome/Chromium 及其传递依赖同样适用各自条款。
