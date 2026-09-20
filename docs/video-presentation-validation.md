# 竖屏排版与模型编排动画验收

> 历史验证记录：结果仅对应文中日期、代码和环境。私人标识已脱敏；后续变更见 [CHANGELOG](../CHANGELOG.md)，当前提交仍需按 [贡献指南](../CONTRIBUTING.md) 验证。

日期：2026-09-11。

**PASS：图片完整展示、标题避让、逐镜头动画和真实模型编排。** 本文记录该阶段的历史验收。后续已获授权安装 Chatterbox、完成双档试听，并将 0.75 保存为后续旁白默认值；见 [最新验收记录](chatterbox-x-validation.md)。

## 问题与实现

原渲染采用放大铺满、居中裁切和缩放，正方形或横图两侧因此被裁掉；标题直接压在图片上，与图片自带文字重叠。

现在图片等比缩放到独立主图区，完整保留边缘；标题在上方，字幕与数字人在下方。镜头缩放和平移按最大位移预留空间，移动终点也保留图片四角。封面使用相同的完整图片与独立标题排版，数字人高度上限调整为画面高度的 20%。字幕同时处理句末标点单独换行的情况。

每个镜头可选择 `motion=auto|none|push_in|pull_out|pan_left|pan_right` 和 `transition=fade|cut`。动作按当前镜头的实际音频时长执行平滑变化，渲染器只接受受限枚举，模型不能传入代码、FFmpeg 表达式或文件路径。

“AI 编排动画并合成”对应 `stage=motion`：调用一次现有文本模型，为现有镜头选择动作与转场，再复用图片和配音渲染。模型请求只含冻结的标题与分镜 ID、旁白、画面标题及图片提示词；不会带上本人参考录音文字或照片。返回的镜头 ID 必须一一对应，缺失、重复、多余 ID、未知字段和非法枚举均拒绝。

旧分镜省略动画字段时使用默认值，旧客户端 PATCH 不会清空既有动画。新客户端补默认值本身不算画面变化，因此仅勾选事实核验仍能复用现有视频。显式修改动画会废弃审核与派生成片，但保留原图片和音频。编排成功有持久化检查点，渲染恢复不重复调用模型；结果未知仍受原有防重计费机制约束。前端在确认框打开前锁定生成按钮，避免多次确认产生重复提交。

## 实际验证

| 主 Agent 执行 | 结果 |
|---|---|
| `cd backend && .venv/bin/pytest -q` | 194 passed，12.70 秒；仅两条既有 TestClient 弃用警告 |
| `backend/.venv/bin/ruff check backend/app backend/tests` | All checks passed |
| `cd frontend && pnpm test:unit` | 8 passed |
| `cd frontend && pnpm build` | Vue 类型检查与 Vite 构建成功；保留既有大 bundle 提示 |
| `cd frontend && pnpm test:e2e` | 11 passed，13.5 秒；包含动画字段保存、确认期间防重复、取消后恢复、只提交一次 |
| 真实 FFmpeg 帧级验证 | 五种动作的开头/结尾均保留四角标记，移动方向与缩放幅度符合选择，图片未进入标题或数字人区域 |
| 正方形、横图、竖图封面验证 | 原图比例保留，标题位于图片上方 |
| 真实 `qwen3.7-plus` 编排 | 用户明确授权后仅发起 1 次文本请求，记录 `SUCCEEDED`，尝试数为 1 |
| 完整内容生成 | v3，`REVIEW_REQUIRED`；1080×1920 H.264/AAC，42.266667 秒，4,218,382 字节 |
| 素材与音频核对 | 7 个镜头的图片和配音素材 ID 全部不变；新旧成片解码后的 PCM 哈希与字节数相同 |
| 实际浏览器检查 | 动画/转场控件显示正常，真实视频可加载，审核按钮可用，页面错误 0；未点击审核 |
| `python3 scripts/control.py verify-backup --from data/backups/20260911T080317132829Z` | `valid=true`，52 个素材，数据库版本 `0002` |

模型实际编排结果：

| 镜头 | 动画 | 转场 |
|---|---|---|
| 实验背景介绍 | 缓慢推进 | 直接切换 |
| 推理预填方法 | 保持静止 | 直接切换 |
| Qwen 3.8 表现突出 | 缓慢推进 | 直接切换 |
| STEM 领域大幅提升 | 向右平移 | 直接切换 |
| 其他模型对比 | 保持静止 | 直接切换 |
| 推测模型来源 | 缓慢拉远 | 淡入淡出 |
| 开放讨论 | 保持静止 | 淡入淡出 |

在 2.983、8.727、14.727、20.727、26.351、32.935、39.455 秒逐镜头抽帧检查。主图完整，标题与图内文字分区，字幕与数字人不重叠。字幕黄色关键词、本人旁白和数字人口型均保留。模型调用累计记录由 28 增至 29，本次没有生图或语音云端调用，账单金额仍未知。

用户反馈时，旧版自动发布任务正在传输素材。为修正当前视频，通过原取消接口终止该任务，最终确认 `CANCELED`，未进入最终提交记录。新版停留在待审核状态，没有自动提交发布。

## 修改文件与接口影响

- 渲染：`backend/app/integrations/media.py`。
- 数据契约与编排：`backend/app/schemas.py`、`backend/app/api.py`、`backend/app/workflow.py`。
- 前端：`frontend/src/types.ts`、`frontend/src/content-editor.ts`、`frontend/src/views/ContentView.vue`。
- 长期回归测试：`backend/tests/test_integrations_media.py`、`backend/tests/test_animation_direction.py`、`backend/tests/test_workflow.py`、`frontend/tests/content-editor.spec.ts`、`frontend/tests/e2e/content-workspace.spec.ts`。
- README、架构、实现契约、离线 OpenAPI 和本记录已同步。

没有新增数据库表、迁移或第三方依赖。动画字段继续保存在现有分镜 JSON 中；原有版本、鉴权、任务、审核和发布接口的保护规则继续生效。`renderer_version=6`、`overlay_version=2` 使新布局不会复用旧渲染缓存。既有图片、音频、旧成片与已发布记录均保留。

本次差异已完成 simplification review：复用现有内容任务和模型调用路径，只新增严格校验所需的两个 Pydantic 输出结构，没有新增服务、队列或通用编排抽象。没有 Git 暂存、提交、推送或远程部署。

## 当时的情感配音边界

当前已安装 Qwen3-TTS Base 的本人复刻分支不会接收 `instruct`，不能仅增加一个“有感情”的文本框就宣称生效。已核对本机实现与 [MLX Audio 的 Qwen 说明](https://github.com/Blaizzy/mlx-audio/blob/main/docs/models/tts/qwen3-tts.md)。

当时向用户提出 Chatterbox Multilingual V3 本地试听方案：保留参考音色并使用实际的表现力参数，先对比试听，再决定是否用于内容。该方案依据 [Chatterbox 官方说明](https://github.com/resemble-ai/chatterbox) 与 [MLX 模型卡](https://huggingface.co/mlx-community/chatterbox-multilingual-v3)。这一阶段尚未安装；后续授权与安装结果另有最新记录。本文中的 v3 原音轨仍是 Qwen3，不能描述为已增加 Chatterbox 表现力。

## 继续使用与恢复

进入当前内容的“镜头与素材”可手动改动画，再选择“仅渲染视频”；这条路径没有文本模型调用。选择“AI 编排动画并合成”会在确认后调用文本模型。新版必须再次审核，通过后按现有自动发布设置执行。

旧文件和修复前备份可用于回看与恢复；本次未执行数据库回滚。模型结果未知时按任务中心提示核验，不能盲目重试。Notion 知识归档仍待当前项目的归属确认。
