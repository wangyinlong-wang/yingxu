# 本地声音与数字人开发安装验收

> 历史验证记录：结果仅对应文中日期、代码和环境。私人标识已脱敏；后续变更见 [CHANGELOG](../CHANGELOG.md)，当前提交仍需按 [贡献指南](../CONTRIBUTING.md) 验证。

本文保留首次安装及后续本人资料验收的历史结果。2026-09-11 已增加 Chatterbox，并选定 0.75 用于后续生成；最新结果见 [Chatterbox 与 X 接入验收记录](chatterbox-x-validation.md)。

验证时间：2026-09-10（本机 America/New_York；UTC 为 2026-09-11）。

结论：**PASS（本地功能开发、安装及非本人测试素材的完整推理）**。随后已使用用户提供的本人资料生成首版试听和数字人预览，详见文末追加记录；声音相似度与肖像口型自然度仍待用户确认。原有双平台发布的外部验收边界不因此改变。

## 交付范围

- 新增“本人声音与数字人”配置页：本机上传、录音转写与校对、声音试听、照片口型预览，以及用于后续生成的两个显式开关。
- 安装固定版本 Qwen3-TTS 1.7B Base（MLX）、MuseTalk 1.5（Apple MPS）、Whisper tiny（CPU），共用业务任务队列与本地推理锁。模型和独立环境约占 9.5 GiB；不含包下载缓存。
- 新增 `local_voice` 内容生成阶段，复用现有图片生成本人旁白与成片。启用时叠加数字人、给字幕留出空间，并重新要求审核。
- 本地模型进程禁止联网，不继承云端凭据。没有发起新的付费模型请求或发布测试作品。

## 主 Agent 实际验证

| 命令或检查 | 实际结果 |
|---|---|
| 后端 `pytest` | 176 passed，11.47 秒；覆盖本地资料校验、任务快照、缓存、取消、锁竞争、恢复、无云调用以及数字人失败不登记成片 |
| `backend/.venv/bin/ruff check backend/app backend/tests scripts/local_ai_setup.py scripts/local_ai_runtime.py` | All checks passed |
| 前端 `pnpm typecheck`、`pnpm test:unit`、`pnpm build` | 类型检查和构建成功，7 项单元测试通过 |
| 前端 `pnpm test:e2e` | 10 passed；包含未安装状态下上传入口可用、转写校对保存、试听音频复用于数字人 |
| `backend/.venv/bin/python scripts/local_ai_setup.py download --component all` | 固定权重全部下载、大小及哈希复核；清单保存在本地安装目录 |
| `backend/.venv/bin/python scripts/local_ai_setup.py verify --component all`，修复后 `verify --component avatar` | TTS 在 Apple M4 Max 完成参数加载；MuseTalk 完成 UNet、VAE、Whisper GPU 前向；Whisper 转写组件加载成功，最终三个组件均已安装 |
| 隔离 TestClient + 实际 Worker + 实际断网模型子进程 | 参考素材上传、设置保存、转写、声音试听、数字人预览全部 `SUCCEEDED` |
| 隔离内容接口 `POST /contents/{id}/generate`，`stage=local_voice` | 实际生成 1080×1920 H.264/AAC 视频，时长 3.656 秒；内容 v2 为 `REVIEW_REQUIRED`，云端调用记录为 0 |
| 本机在线 `GET /api/v1/local-ai/status` | TTS、avatar、transcribe 均 `installed=true`，空闲时 `busy=false` |
| 实际浏览器只读检查 | 3 个“已安装”标签，上传按钮可用，页面错误 0；配置页和成片抽帧均完成视觉检查 |
| `python3 scripts/control.py verify-backup --from data/backups/20260911T021008451742Z` | `valid=true`，30 个素材，schema revision `0002` |
| `python3 scripts/control.py status` | API、内容进程、设备进程均在运行 |

实际测试使用公开 NASA 宇航员示例照片和已有通用合成旁白，仅存在隔离安装验证目录，没有保存为用户的本人资料，没有进入真实内容列表。测试目录的模型调用数为 0；现有业务库模型调用数仍为 19。正式配置中本地开关默认关闭，尚无本人录音或照片。

| 实际模型步骤 | 输出 | 本机耗时（包括排队接口及登记） |
|---|---|---|
| Whisper 参考录音转写 | 42 个字符，结果可手工修正 | 4.312 秒 |
| Qwen3-TTS 声音试听 | 3.655958 秒 WAV / PCM16，24 kHz | 9.266 秒 |
| MuseTalk 照片口型预览 | 3.656 秒，512×512，H.264/AAC | 54.551 秒 |
| 完整内容阶段 | 1080×1920，包含字幕、数字人与旁白；允许复用本地缓存 | 24.298 秒 |

这些是一次短素材实测值，不能直接作为长视频耗时承诺。口型预览在 0.4、1.5、2.8 秒抽帧可见嘴部变化；成片检查确认黄色关键词、字幕换行和右下角数字人区域没有重叠。叠加函数另有真实 FFmpeg 校验，解码后的基础音轨在叠加前后相同。

## 文件与契约影响

- 后端：`backend/app/{api,config,db,schemas,services,worker,workflow}.py`、`backend/app/integrations/{local_ai,media}.py`；长期测试 `backend/tests/test_local_ai.py` 与 `test_integrations_media.py`。
- 前端：`frontend/src/components/LocalAISettings.vue`、`views/{SetupView,ContentView}.vue`、`api.ts`、`types.ts`、`lib.ts` 及对应 E2E 用例。
- 安装与文档：`scripts/local_ai_setup.py`、`scripts/local_ai_runtime.py`、`scripts/local_ai/*.requirements.in`、`*.requirements.lock`、README、架构、接口契约、离线 OpenAPI 和本地使用/验收说明。
- 数据库没有新增表、字段或迁移；使用既有资产、任务、设置表。新增配置为 `local_ai`，旧客户端可省略。新增接口 `/local-ai/status`、`/local-ai/transcribe`、`/local-ai/preview`；既有生成接口增加 `local_voice` 阶段，上传增加 `voice_reference`、`portrait`。
- 状态、鉴权、幂等与错误封装沿用本地工作台规则；细节见 `implementation-contract.md`。素材和已发布快照保留。

## 修复、复审与限制

首次 MuseTalk 检查失败于重复加载的旧 tar 格式 ResNet 权重。完整 BiSeNet 检查点已包含所有骨干参数，修复为取消重复初始化，继续通过 `weights_only=True` 与严格 `load_state_dict` 加载完整检查点；随后 GPU 前向与真实口型流程通过。未放宽反序列化限制。

受限命令环境最初阻止 Metal GPU、Chromium 和 `ps`；按本机验证授权在可访问这些系统资源的进程中重试，模型子进程仍执行网络禁止规则。此为工具执行环境限制，未作为产品成功结果跳过。

本次差异已完成 simplification review：沿用原任务、资产和设置机制，无额外服务、表、适配器层；安装时仅保留推理所需依赖与代码，删除开发过程中的一次性兼容迁移分支。公开测试运行脚本不纳入正式源文件。

## 后续本人素材验收与恢复

1. 在 `/setup?tab=local-ai` 上传清晰正面照片和 3～30 秒本人录音，建议 10～20 秒、无音乐。
2. 转写后逐字核对，保存，先试听，再检查照片口型。本人效果由这次素材测试确认。
3. 开启需要的本地生成选项，已有内容明确选择“用本人声音生成视频”，重新检查并审核。长视频与本人资料的效果尚未实际测试。

关闭两个本地开关并保存即可停止后续自动使用；关闭时不会改写已有成片。没有自动卸载模型或删除资料。环境异常可运行 `scripts/local_ai_setup.py verify`，任务异常从任务中心查看安全错误与重试条件。已有备份已复核有效。此目录没有 Git 仓库，本次未暂存、提交、推送或远程部署。

Notion 知识归档：待确定当前项目在“项目知识库”下的归属，不影响以上本地功能 PASS。

## 本人资料首版预览（2026-09-10 追加）

用户随后提供本人照片与录音。通过既有上传接口导入本机：录音 8.477333 秒、AAC；照片 1080×1919。Whisper 转写成功，自动识别文字已保存用于预览，仍标记为待用户核对。未将原始录音、照片或转写文字加入云端模型调用。

- 实际本地声音预览：`SUCCEEDED`，6.844167 秒、24 kHz PCM16 WAV；推理 6.066 秒。
- 实际本人照片数字人：`SUCCEEDED`，6.845 秒、540×960 H.264/AAC；推理 25.047 秒，使用同一段试听音频。
- ffprobe 校验与成片抽帧检查完成，业务云端调用总数仍为 19。没有提交发布。
- 参考资料已保存，后续本人声音与数字人开关保持关闭，等待用户检查首版效果后再决定启用。

本次导入与本地生成 `PASS`；音色相似度和口型自然程度不能仅由文件成功生成判定。本人媒体仅位于本地素材目录，文档不记录原始个人文件路径、素材标识或录音内容。

用户随后确认首版试听和口型效果。已通过设置接口开启 `local_ai.voice_enabled` 与 `local_ai.avatar_enabled`，回读均为 true，参考资料与已确认预览一致，其余配置保持原值。此配置确认未重新生成旧内容或提交发布。
