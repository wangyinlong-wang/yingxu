# 资讯自动采集优化：验收记录与交接

日期：2026-09-30。目标：解决“无法自动从 X 或其他来源获取 GPT / Claude / Grok 等最新资讯”。

## 根因（实测）

| 问题 | 证据 | 处理 |
|---|---|---|
| 没有自动采集 | 本地库 `schedules` 为空；唯一可用的 `pipeline` 计划每天一次且要求文本/图片/配音模型全部启用 | 新增 `collect` 资讯采集计划 |
| X 公开时间线 429 | 项目采集器对 OpenAI/AnthropicAI/xai/grok 全部 429；同一地址 curl HTTP/1.1 为 200、HTTP/2 与直连为 429，httpx 使用与 curl 相同请求头仍 429，判断为 X 按 TLS 握手特征限流 | 保留该方式但在界面提示不稳定；推荐改用 TikHub。未做 TLS 指纹伪装（合规与稳定性风险） |
| TikHub 403 | 旧 Key 被拒 | 用户更换 Key 后实测 200 |
| TikHub 新版返回结构无法解析 | 新 Key 返回 `data.timeline[]` 扁平结构（`tweet_id/text/author`），原解析器判为 `TIKHUB_RESPONSE_INVALID` | 兼容扁平结构（账号页与搜索页），保留旧 GraphQL/legacy 解析 |
| 相关度规则漏掉 Grok/ChatGPT/xAI/Sora/Llama | `_relevance` 对这些标题返回 0，X/HN 条目被丢弃 | 扩充词表；增加来源级关注词 |
| 代理下 DNS 单点 | 仅 `dns.google`，偶发握手超时 → `DNS_FAILED` | 增加 `cloudflare-dns.com` 备用；429/5xx/超时有界退避重试 |
| 一个 TikHub 失败中断整条流水线 | `pipeline_batch` 对任意 TikHub 异常直接抛出 | 明确失败继续其他来源；付费结果未知仍中止待核验 |

## 新能力

- **来源类型**：`x_tikhub_search`（`https://x.com/search?q=…&f=live|top`，支持 `from:`、`OR`、`min_faves:`、`-filter:replies`）；`html_list`（官方新闻页同站点、路径前缀下的文章链接，不递归）。
- **推荐来源**：`GET /source-presets`、`POST /source-presets/apply`；OpenAI News、Anthropic News、DeepMind、Mistral、Meta AI、Claude Code / Codex Releases（免费），及 “X · AI 厂商官方账号”“X · AI 热门讨论” 两个 TikHub 搜索（按次计费，默认不勾选）。
- **资讯采集计划**：`kind=collect`、`interval_minutes` 30–1440（默认 180）、`include_paid` 默认 false；任务 `COLLECT_BATCH` 不调用模型；同一计划未完成时不叠加。
- **来源健康**：`consecutive_failures / last_success_at / next_eligible_at`，15 分钟起倍增、最长 24 小时退避；仅定时采集遵守，手动采集与重新启用不受限。
- **跨来源合并与热度**：72 小时内其他来源的同一事件并入 `related` 并累加 `cluster_size`（同来源不合并、版本号需一致）；`GET /topics?sort=heat|relevance`，热度在读取时计算。

- **版本号标题补全**：RSS/Atom 标题仅为版本号时（大小写敏感，`GPT-6.1` 这类产品名不受影响），以“来源名去掉 Releases 后缀”或 GitHub `owner/repo` 作为前缀。上线时已备份并修正库内 20 条旧标题（同步重算 fingerprint）。

- **TikHub 密钥设置**：设置页「模型与视频 → 数据采集服务」卡片，钥匙串保存/清除，状态只显示来源；`model_credentials.py` 的 `set/delete/resolve_tikhub_key`、`tikhub_key_status`。更换密钥：在 TikHub 后台生成新 Key → 卡片保存 → 删除 `.env` 旧值。

## 数据库变更（迁移 `0005_source_automation`）

- `sources` 新增 `keywords JSON '[]'`、`options JSON '{}'`、`consecutive_failures INT 0`、`last_success_at`、`next_eligible_at`。
- `topics` 新增 `cluster_size INT 1`、`related JSON '[]'`。
- `schedules` 新增 `interval_minutes`（可空）、`include_paid BOOL false`。
- 仅新增带默认值的列，旧数据无需回填；旧客户端忽略新字段。
- 回滚：`alembic downgrade 0004`。若已存在 `x_tikhub_search/html_list` 来源或 `collect` 计划会拒绝降级，此时应恢复部署前自动生成的备份。

## 验证

| 项 | 结果 |
|---|---|
| 后端 ruff（CI 范围 app/tests） | 通过 |
| 后端 pytest | 481 passed（原 448 + 新增 33） |
| 前端 typecheck / vitest / build | 通过 / 15 passed / 通过 |
| 前端 Playwright | 46 passed，1 failed：`browser-publication.spec.ts:19`（发布页 ADB 提示，属进行中的发布通道改动，未涉及本次文件） |
| 真实库副本迁移 | 0004 → 0005 成功，9 个来源、82 个选题保留 |
| 真实采集（库副本） | 一轮 `COLLECT_BATCH`：10 个来源全部成功，新增 161 条，仅 1 次 TikHub 调用；首页热度前列为 GPT-6.1 Sol、GPT-6 Astra、Codex、Claude Sonnet 5.5 等 |
| 界面 | 新代码 + 库副本启动于独立端口核对来源页、健康列、热度卡片、推荐来源、X 搜索表单、资讯采集计划；修复推荐来源列表文字不可见 |

## 交接要点

- 关键代码：`backend/app/integrations/sources.py`（采集与解析）、`integrations/net.py`（DoH 备用与重试）、`providers.py::invoke_tikhub_source`（`search=` 参数）、`workflow.py::collect / collect_enabled_sources / collect_batch`、`worker.py::_tick_collect`、`topic_ranking.py`、`source_presets.py`；前端 `SourceView.vue`、`SetupView.vue` 计划弹窗、`lib.ts` 辅助函数。
- 测试：`backend/tests/test_source_automation.py`、`frontend/tests/lib.spec.ts`。
- 调参位置：流水线选题时间窗 `workflow.PIPELINE_TOPIC_DAYS`（默认 2 天，仅统计有发布时间的选题）；合并阈值 `topic_ranking.same_event`；热度权重 `topic_ranking.heat_score`；退避 `workflow.BACKOFF_*`；搜索基线相关度 `_collect_x_tikhub` 中的 `fallback`。
- 已知限制：x.ai 新闻页对程序访问返回 403，Grok/xAI 动态依赖 TikHub 搜索；X 公开时间线仍可能 429；TikHub 每次只读一页、不翻页；合并基于标题，偏保守，可能漏合并但尽量不误合并。
