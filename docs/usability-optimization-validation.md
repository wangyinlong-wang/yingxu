# 可用性、发布恢复与媒体质量优化验收

> 历史验证记录：结果仅对应文中日期、代码和环境。私人标识已脱敏；后续变更见 [CHANGELOG](../CHANGELOG.md)，当前提交仍需按 [贡献指南](../CONTRIBUTING.md) 验证。

日期：2026-09-12。当前结论：**BLOCK（仅真机发布闭环）**。本地应用、API、媒体重合成、测试和部署为 PASS；当前没有连接 ADB 手机，因此本轮不能把自动点击最终发布、真实成功页和手机作品链接回读标记为已验收。

## 本轮完成

- 发布中心按真实设置显示“审核后自动发布”状态，不再用固定文案误导为只能人工发布。设备未连接时明确说明自动发布和手机识别均暂停。
- 新增 `POST /api/v1/publications/{id}/detect`。手动发布或自动发布结果待核验时，用户只需让绑定手机停留在目标作品详情页；服务端要求原准备记录已验证同一账号，并再次核对平台 App、已审核标题和唯一分享链接，随后原子记录作品 URL/ID。任何页面、标题、账号、记录状态或作品 ID 冲突都会停止；不会再次点击最终发布。
- 图片提示加入模板风格、镜头序号、统一视觉语言、相邻镜头差异、原生 9:16 构图、标题/字幕安全区和单一视觉焦点。真实方舟调用证明服务商仍可能在 `watermark=false` 时返回右下角字样，因此入库链路增加底部安全区清洁处理并把处理版本写入请求指纹；仍保留来源事实隔离、禁绘数字文字和逐镜头重绘防重规则。
- 最终混音将云端与本地旁白统一到 `-16 LUFS / -1.5 dBTP` 目标，保持自然时长与音色模型参数；渲染算法版本升至 7，避免复用旧响度缓存。
- 应用与 Worker 将 `httpx` 运行日志降至 WARNING，避免以后把带签名查询参数的临时媒体 URL 写入日志；未知 Worker 异常现在保留堆栈类型证据，同时对外继续返回安全错误。
- 部署检查现在识别 `.env` 中专用的 `PUBLISHER_ADB_PATH`，但不会解析或显示模型密钥。Playwright 测试可自动启动并回收 5173 前端服务。
- 完成度审计发现原路径指向微信开发者工具内置的 ADB 1.0.39（2017，缺少 mDNS）。本机现已安装官方 Platform Tools 37.0.1、切换到 ADB 1.0.41，并在部署检查和运行诊断中增加最低版本门禁，避免旧工具再次被误报为可用。
- 新增最小权限 GitHub Actions CI：只读仓库权限，执行后端 lint/测试、前端类型/单元/构建和隔离浏览器流程。

## 开源项目参考

- [MoneyPrinterTurbo](https://github.com/harry0703/MoneyPrinterTurbo)：参考其“一次输入形成脚本、素材、字幕、配音和竖屏成片”的完整流水线，以及 1080×1920 输出边界；本项目保留事实核验、人工审核和发布防重，不照搬无人值守发布。
- [ShortGPT](https://github.com/RayVentura/ShortGPT)：参考生成、编辑和持久状态分层；本项目继续复用现有 FastAPI 任务状态机，没有引入第二套工作流框架。
- [uiautomator2](https://github.com/openatx/uiautomator2) 与 [adbutils](https://github.com/openatx/adbutils)：参考设备状态、显式等待和连接状态跟踪思路；本项目仍要求唯一控件、前台包名、账号与作品证据，未知页面不猜测点击。
- [Qwen3-TTS](https://github.com/QwenLM/Qwen3-TTS)：核对参考录音+准确转写的声音复刻路径；现有 Qwen3 本地模型继续使用该契约。
- [Chatterbox](https://github.com/resemble-ai/chatterbox)：核对 `exaggeration` 与 `cfg_weight` 的表现力/节奏建议；当前选定的 0.75 表现力继续搭配 0.35 pace/CFG 权重，本轮再在成片层统一节目响度。
- [Remotion Audiogram Template](https://github.com/remotion-dev/template-audiogram)：参考社交视频按真实音频时序组织字幕的方式；当前实现保留实测音频时长和多句权重分段，没有新增 Remotion 依赖。

以上只借鉴公开架构和参数用法，没有复制第三方源码，也没有引入其依赖或许可证负担。

## 主 Agent 实际验证

| 命令或检查 | 结果 |
|---|---|
| `uv run --project backend ruff check backend/app backend/tests scripts/control.py scripts/local_ai_setup.py scripts/local_ai_runtime.py` | All checks passed |
| `uv run --project backend pytest -q` | **251 passed**；仅 2 条现有 TestClient/AnyIO 弃用警告 |
| `cd frontend && pnpm typecheck && pnpm test:unit -- --run && pnpm build` | 类型检查通过，**9 passed**，生产构建成功；保留 Element Plus 大包提示 |
| `cd frontend && pnpm test:e2e` | **16 passed**，自动启动隔离 Vite 服务；没有模型调用或真实发布 |
| `python3 scripts/control.py env` | PASS；项目专用 ADB 为 1.0.41 / Platform Tools 37.0.1，最低版本门禁通过 |
| `python3 scripts/control.py deploy` | 先备份后安装锁定依赖、构建、迁移并重启三个本项目服务；健康检查通过 |
| `python3 scripts/control.py verify-backup --from data/backups/20260912T050504432577Z` | 最新 ADB 版本门禁部署前备份 `valid=true`，93 个素材，schema revision `0002` |
| 在线 API | health、diagnostics、settings、publications、devices 均 HTTP 200；运行 OpenAPI 已包含 `/publications/{id}/detect` |
| 真实浏览器 | 发布页显示自动发布已开启、ADB 断连警告和禁用的“从手机识别”按钮；不再显示“只能人工复制链接”的固定说明 |
| 现有真实素材隔离重合成 | 8 镜头、62.166667 秒、1080×1920、H.264/AAC，`speech_tempo=1.0`；未调用模型、未写业务数据库 |
| FFmpeg EBU R128 检测 | 集成响度 `-16.4 LUFS`，LRA `3.2 LU`，True Peak `-1.4 dBFS`；满足目标且时长未改变 |
| Seedream 隔离真实调用 | 临时数据库只发起 1 次请求，调用记录 `SUCCEEDED / attempts=1`；产物 1600×2848、1,167,665 bytes。主体、景深、9:16 和上下安全区通过视觉检查；原图右下角仍有服务商“AI生成”字样，镜像模糊安全区处理后复检已消失且分辨率不变 |
| Git 候选文件保密检查 | 110 个候选文件、4 个本地实际秘密逐值比对，匹配数为 0；`.env`、`data/`、本地模型、浏览器快照和构建产物均忽略 |
| 开源前私有仓库的 GitHub Actions CI（历史记录） | Linux Runner 完整 PASS，耗时 2 分 53 秒；包含 FFmpeg、249 项后端、前端构建和 16 条 Chromium 流程；原私有运行链接不随公开文档发布。 |

## 数据、接口与恢复

没有新增数据库表、字段、迁移或第三方运行依赖。新发布识别结果复用 `Publication.work_url`、`work_id`、`status` 和 `evidence.detection`；旧客户端可以不调用新接口。识别操作不持有 SQLite 写事务执行手机或短链网络 I/O，写入前重新核对冻结状态，同一平台作品 ID 仍保持唯一关联。

部署前备份已校验。回退代码时旧数据仍可读取；版本 7 新渲染可删除后重新使用旧代码渲染，既有素材、已发布记录和审核快照不会被本轮验证修改。

## 未完成的外部验收

- 当前 `GET /api/v1/devices` 返回空列表。解除条件是重新连接绑定的 Android 手机并完成 USB 调试授权；随后对现有待处理记录先“继续准备”，再验证真实自动发布或在作品页执行“从手机识别”。
- macOS USB 树没有 Android、Xiaomi、Redmi、ADB 或 MTP 设备，官方新版 `adb devices -l` 和 `adb mdns services` 也都为空；因此当前不是应用选择器或旧 ADB 兼容问题。
- 本轮为实际验收产生了 1 次隔离 Seedream 图片请求；调用与产物保存在临时目录，不写正式内容、审核、发布和用量账本。该图证明新版提示的构图质量，也暴露并推动修复了服务商水印不遵守请求的问题；不会重复该付费请求。
- 不会为了通过验收重复发布既有作品，也不会将历史 `PUBLISHED` 记录改绑到新链接。

本轮 simplification review 已完成：复用现有账号快照、设备互斥锁、作品链接规范化、发布状态和前端 API 层；没有新增队列、数据库迁移、通用适配器或媒体框架。
