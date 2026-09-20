# 贡献指南

本项目是单人本地内容运营工作台，欢迎 Issue 和 Pull Request。参与交流时请遵守 [社区行为准则](CODE_OF_CONDUCT.md)。

## 提交 Issue

- 先搜索已有 Issue，避免重复。
- 说明运行环境（macOS 版本、Python、Node.js、FFmpeg 版本）、复现步骤、期望行为与实际行为。
- **不要**在 Issue 中粘贴真实模型密钥、账号标识、设备序列号、IP 地址、个人路径、手机号、作品 ID 或私人素材；日志和截图请先脱敏（见下文“脱敏示例”）。
- 安全漏洞**不要**发公开 Issue，请按 [SECURITY.md](SECURITY.md) 的流程私下报告。

## Pull Request 流程

1. Fork 仓库并创建专题分支，保持改动小而聚焦；一个 PR 只做一件事。
2. 遵循现有代码风格（后端 ruff 规则、前端现有 Vue/TypeScript 约定）。较大的架构或依赖变化建议先通过 Issue 讨论。
3. PR 描述写清动机、改动内容和实际执行过的验证命令及结果。
4. 提交前在本地完成“最小影响验证”，并保证 CI（`.github/workflows/ci.yml`）通过。
5. 不要提交 `.env`、密钥、个人数据、真实作品素材或 `data/` 运行数据。

## 开发环境

要求：Python 3.11～3.13、Node.js 22+、uv、pnpm、带 libass 的 FFmpeg/ffprobe。

```sh
cp .env.example .env          # 密钥留空即可参与开发，默认测试不需要真实密钥
./scripts/deploy-local.sh --check
python3 scripts/control.py setup
```

## 最小影响验证

普通文档改动只需检查内容、链接和 `git diff --check`。代码改动优先运行覆盖受影响行为的现有测试；以下是从项目根目录执行的模块级命令，完整 CI 仍会运行所有规定检查：

```sh
# 后端改动
uv run --project backend ruff check backend/app backend/tests scripts/control.py scripts/local_ai_setup.py scripts/local_ai_runtime.py
uv run --project backend pytest -q

# 前端改动
(cd frontend && pnpm typecheck && pnpm test:unit -- --run && pnpm build)

# 涉及浏览器流程的改动（需要 Playwright Chromium）
(cd frontend && pnpm test:e2e)

# 环境/部署脚本改动
python3 scripts/control.py env
```

CI 在 Linux 上执行后端、前端及浏览器检查；本地环境检查 `control.py env` 用于部署脚本诊断。检查环境受阻时，在 PR 中说明实际失败及未覆盖项，不将其描述为通过。

## 非破坏性贡献

- 不删除或改写既有业务数据、迁移历史、审核快照、发布记录、指标记录或用户素材的语义。
- 不改变默认行为：模型开关、自动计划、自动发布默认保持关闭。
- 新增配置必须有安全默认值；新增接口遵守现有 `{items,total}` / 错误信封 / `Idempotency-Key` 约定（见 [docs/architecture.md](docs/architecture.md)）。

## 迁移兼容与回滚

- 数据库变更通过 Alembic 迁移实现，必须能从旧库正常升级，并附迁移测试（升级、默认值、约束与降级边界）。
- 文档和 PR 描述中必须写明降级影响；已有约定：迁移 `0002` 降级会丢失模板历史，迁移 `0003` 降级会丢失 Brief、质量报告、合规明细与策略实验数据。
- 升级前应先备份；数据丢失场景优先从升级前备份恢复，不指望降级无损。

## 必须保持的保障

改动不得绕过以下项目原则：

- **事实核验**：模型声明的事实不会被自动标记为人工核实；数据卡/引用卡必须关联已核验事实。
- **人工审核**：语义合规、版权与平台规则以人工复核为最终发布门槛，关键词匹配不能当作法律判定。
- **防重与幂等**：结果不明的模型请求和发布任务禁止自动重发/重放；防重约束不能因重试被绕过。
- **权限与凭据边界**：真实密钥由环境变量或本机 `.env` 提供，不进入前端、数据库、日志、备份、时间线或测试夹具；设备 PIN 只存本机钥匙串。

## 测试边界

- **禁止把真实付费模型调用或真实平台发布作为默认测试**：单元/集成/端到端测试一律使用 mock、固定夹具和本地生成的测试素材。
- 真机流程测试只能使用明确标记的测试素材，并在最终发布按钮前停止；真实发布必须获得用户对具体内容和目标账号的显式授权，且永远不属于 CI。
- 测试不得覆盖历史 `PUBLISHED` 记录，不得重放结果未知的任务。

## 脱敏示例

提交日志、截图或测试数据时，用占位符替换真实信息：

| 真实信息 | 替换为 |
|---|---|
| 模型 API Key | `sk-...` 或 `<redacted>` |
| 设备序列号 | `DEVICE_SERIAL` |
| 内网/公网 IP | `192.0.2.1`（文档保留段） |
| 个人主目录路径 | `/path/to/yingxu` |
| 平台账号/作品 ID | `example_account` / `example_work_id` |
| 链接 | `https://example.com/...` |

## 许可证与第三方

- 贡献即表示你同意按本项目的 [MIT 许可证](LICENSE) 授权你的贡献；不强制要求 CLA 或 DCO 签署。
- 引入或变更第三方组件时，请同步检查 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) 是否需要更新。
- 安全漏洞报告流程见 [SECURITY.md](SECURITY.md)。

## 响应说明

本项目由个人维护，不承诺固定的响应或修复时限（SLA）。维护者会尽力审阅 Issue 与 PR，但请理解可能存在延迟。
