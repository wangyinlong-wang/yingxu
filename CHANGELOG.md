# Changelog

本项目变更记录遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/) 的分组约定。

## [Unreleased]

本次为开源准备与尚未正式打 tag 的 V2 特性汇总。仓库当前包版本仍为 `0.1.0`，尚未发布任何正式版本；以下条目汇总开源初始快照中的 V2 与开源整理工作。

### Added

- **资讯自动采集**：新增「资讯采集」定时计划（`kind=collect`，30 分钟–24 小时间隔，默认不含付费来源，未完成的一轮不会叠加）；新增来源类型 `x_tikhub_search`（TikHub X 高级搜索）与 `html_list`（无 RSS 官方新闻页的同站点文章列表）；「推荐来源」一键添加 OpenAI / Anthropic / DeepMind / Mistral / Meta AI / Claude Code / Codex 等官方源；来源级关注词；跨来源同一事件合并与时间衰减热度排序（`GET /topics?sort=heat|relevance`）；来源健康状态与连续失败自动退避。迁移 `0005_source_automation`。
- **本地浏览器发布与分析**：独立部署 browser-use 0.13.10，抖音/小红书账号可选择 Chrome 通道；复用审核、一次提交防重、作品链接核验和指标快照。旧账号默认继续使用 Android，真实平台验收需本人登录与具体作品授权。

- **可选视频制作**：Metaso MiniMax H3 V2 文生视频、按分镜费用确认、URL 绑定的密钥存储、异步任务恢复和视频素材预览。仅显式请求生成视频片段时调用；不随普通一键生成自动计费。
- **Hypit 本地渲染**：固定版本可选安装、图片/视频复用、Build ID 持久化与显式取消；最终 MP4、封面、SRT 继续使用原有质检与审核流程。

- **生产质量闭环（V2）**
  - 只读、版本化内置生产预设目录：科技快讯 `tech_brief_video_v1`、数据解释 `data_explainer_video_v1`、数字人口播 `avatar_commentary_video_v1`，及 `GET /api/v1/presets` 查询接口。
  - Production Brief：一次受保护文本调用生成三个结构化创意方案（钩子、叙事结构、视觉路径、预计调用次数），人工选择后升版本并约束后续脚本生成；费用无可靠价格时保持未知。
  - 多类型分镜 `image / text_card / stat_card / quote_card`；后三种卡片由 Pillow 在本地绘制，不产生图片模型调用；数据卡与引用卡必须关联已核验事实。
  - 每次渲染后自动执行离线质量检查并持久化报告（`PASS / REVISE / FAIL`），覆盖技术格式、抽帧黑屏、EBU R128 响度、字幕时序、镜头结构与事实来源；当前版本与当前视频无 `PASS` 时禁止审核通过。
  - 审核增加八项结构化合规清单，绑定内容版本与审核快照；兼容旧客户端单 boolean 提交（记录 `legacy_boolean_only`）。
- **经营反馈闭环（V2）**
  - 策略与实验工作台（`/strategy`）：需求信号（六维评分）、对标拆解、内容实验的本地 CRUD；实验可查看关联发布的最新指标与增量，缺失值保持为空。
  - Job 详情统一时间线（步骤 + 模型调用，脱敏）与费用未知状态；用量可展示，不从缺失 usage 推算成本。
- **双平台流程增强**
  - 小红书 9.46.0：自定义媒体网格隐藏文件名时的可审计人工素材确认接口；两阶段真实提交（“发笔记” → 选择“含 AI 合成内容” → “确认并发布”）。
  - 官方 `xhslink.cn/.com` 短链经受限公网 DNS/SSRF/跳转校验解析作品 ID，落库链接去除分享追踪参数。
  - 无线 ADB `host:port` 连接下，通过受控 `ro.serialno` 匹配物理设备对应的钥匙串 PIN 与失败锁。
- **数据迁移**：Alembic `0003_v2_quality_growth` 新增 `contents.preset_id`、`contents.brief`、`approvals.compliance` 及 `quality_reviews / demand_signals / benchmarks / experiments` 表；含旧库升级与降级边界测试。
- **开源准备**：MIT 许可证、贡献指南、社区行为准则、安全政策、第三方声明、Issue/PR 模板与对外 README。

### Changed

- 内容流水线采集阶段：TikHub 明确失败（如 403、响应无效、额度用完）只记录该来源失败并继续其余来源；只有付费结果未知仍暂停等待人工核验。流水线选题改按热度排序。
- 相关度规则补充 Grok、xAI、ChatGPT、Sora、Codex、Llama、Mistral、Kimi 等当前主流 AI 产品名。
- 设置页「模型与视频」新增「TikHub（X 数据采集）」卡片：显示密钥是否配置及来源（钥匙串 / 环境变量 / 未配置），可将密钥保存到 macOS 钥匙串或清除，保存后下次采集即生效；钥匙串优先，未保存时回退 `.env`，钥匙串锁定时报错而不静默回退。接口 `GET/POST /settings/tikhub-key`，只返回元数据。
- 候选选题默认只显示最近 2 天发布的内容（按原文发布时间，可切换 7 天 / 全部；无发布时间的条目仅在「全部」中显示）。
- 内容流水线自动选题从“近 7 天（含无发布时间）”收紧为“原文发布于近 2 天”（`workflow.PIPELINE_TOPIC_DAYS`），无发布时间的选题不再被自动选中，仍可人工创建内容。
- RSS/Atom 条目标题只有版本号（如 GitHub Releases 的 `0.159.1`、`v2.1.285`、`rust-v0.160.0`）时，自动加上产品名（来源名去掉 Releases 后缀，缺省用 GitHub `owner/repo`），如 `OpenAI Codex 0.159.1`。
- 内容一旦生成 Brief，选定方案前禁止生成脚本或执行 `all`；重新生成 Brief 先冻结旧 revision 再升版本，不修改历史发布快照或删除素材。
- 自动发布保持默认关闭；审核确认框在开启“审核通过后自动发布”时显式提示通过后将自动上传并发布。
- 发布证据规则收紧：只有成功页面、发布快照中的平台标题与新复制作品链接一致时才记录 `PUBLISHED`，其他结果停在 `VERIFYING` 且不重放提交。

### Fixed

- 修复 TikHub 返回新版扁平结构（`timeline` / `tweet_id` / `author`）时被判定为 `TIKHUB_RESPONSE_INVALID` 导致 X 采集全部失败。
- 代理环境下公网 DNS 查询增加备用解析服务；RSS、Hacker News、网页列表遇到 429/5xx/超时按上限退避重试。
- 真机复核中修正：小红书“编辑主页”标签与“从相册选择 / 全部/视频/照片”页面识别、失效控件句柄、“发笔记”与“添加作品描述”最终编辑页控件、平台专用标题用于作品检测。
- 修复策略 PATCH 只读字段、旧质量报告误放行新视频、Brief 选择前未保存等前端问题。

### Security

- 将文档中的真实设备信息、个人路径、作品与内部记录 ID 脱敏；无线设备测试改用文档专用 IP。
- 扩展 Git 忽略规则，排除环境配置、私钥、凭据文件、数据库、日志、运行数据、本地模型与 Agent 临时状态；保留空白的 `.env.example`。
- 策略记录的外部 URL 只保存人工输入，不自动抓取；前端外链使用 `noopener noreferrer`。
- 时间线只返回受控步骤文本与数值 usage；模型原始响应、任意服务商请求体、签名 URL 和密钥不进入时间线。

## 历史概述（未打 tag 的私有迭代）

以下为开源前的开发背景，不构成正式版本发布记录：

- 初始实现交付可用的本地发布流程（采集、生产、审核、Android 辅助发布、指标与备份）。
- 随后若干提交修复 CI 运行环境（setup-uv 版本、action 运行时、FFmpeg 依赖）、Seedream 底部安全区清理，以及要求使用现代 Android Debug Bridge（≥ 1.0.41）。
- V2 在既有本地发布流程之上增加生产质量与经营反馈闭环，历史验收记录见 [V2 验收记录](docs/v2-production-quality-growth-validation.md)。
- 公开仓库从脱敏后的完整源码快照开始，私有迭代的 Git 历史不随公开仓库发布；提交作者使用 GitHub 隐私邮箱。
