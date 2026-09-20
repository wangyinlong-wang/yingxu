# Changelog

本项目变更记录遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/) 的分组约定。

## [Unreleased]

本次为开源准备与尚未正式打 tag 的 V2 特性汇总。仓库当前包版本仍为 `0.1.0`，尚未发布任何正式版本；以下条目汇总开源初始快照中的 V2 与开源整理工作。

### Added

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

- 内容一旦生成 Brief，选定方案前禁止生成脚本或执行 `all`；重新生成 Brief 先冻结旧 revision 再升版本，不修改历史发布快照或删除素材。
- 自动发布保持默认关闭；审核确认框在开启“审核通过后自动发布”时显式提示通过后将自动上传并发布。
- 发布证据规则收紧：只有成功页面、发布快照中的平台标题与新复制作品链接一致时才记录 `PUBLISHED`，其他结果停在 `VERIFYING` 且不重放提交。

### Fixed

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
