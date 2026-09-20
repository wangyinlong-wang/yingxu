# 映序 V2：生产质量与经营闭环 SPEC

状态：FROZEN FOR IMPLEMENTATION\
日期：2026-09-13\
目标版本：V2.0\
基线：开源前 V1 本地发布工作流；公开仓库以脱敏源码快照起始，不包含原私有提交历史。

## 1. 背景与目标

当前系统已经具备来源采集、事实组织、脚本与分镜、图片与配音、FFmpeg 成片、人工审核、Android 辅助发布、发布防重、指标快照、备份恢复和本地 AI。V2 不替换现有 FastAPI + Vue + SQLite + Worker 架构，而是在其上补齐两条闭环：

1. 生产质量闭环：生成前明确创意方案、生产预设和交付质量；渲染后形成机器可读的质量报告，质量不合格时阻止审核通过。
2. 经营反馈闭环：把需求信号、对标拆解、内容实验和结构化合规从零散笔记变为本地可追踪数据，并与内容、发布和指标建立关联。

本设计吸收 OpenMontage 的声明式流水线、阶段产物、质量 Gate、Decision Log 与 Render Review 思路，以及 xhs-virtual-product 的选品、对标、内容实验和合规清单方法。不得复制 OpenMontage 的 AGPL 源码，不将外部 Skill 文案原样作为产品规则。

## 2. 角色与核心流程

### 2.1 角色

- 运营者：唯一的本机用户，选择选题、确认创意、编辑内容、审核合规并决定是否发布。
- 内容 Worker：执行 Brief、脚本、素材、渲染和质量检查任务。
- 设备 Worker：保留现有账号核验、上传准备、最终提交、结果核验和指标采集职责。
- 系统：负责状态、版本、幂等、成本/用量证据、质量门禁和审计时间线，不替用户作版权或平台规则的最终判断。

### 2.2 主流程

```text
来源/需求信号
  -> 选题
  -> 生成 3 个创意方案（Brief）
  -> 用户选择方案
  -> 生成脚本与多类型分镜
  -> 云端/本地素材生成
  -> FFmpeg 渲染
  -> 自动 Render Quality Review
  -> 人工事实、画面、文案与结构化合规审核
  -> Android 发布准备/提交/核验
  -> 指标快照
  -> 实验结论与下一步动作
```

## 3. 本轮范围

### 3.1 必须交付

1. 版本化内置生产预设目录和查询接口。
2. Production Brief 生成、三方案展示、方案选择和后续脚本约束。
3. `image / text_card / stat_card / quote_card` 四种镜头类型；后三种本地生成卡片图，不产生图片模型调用。
4. 每次成片后的自动质量检查、持久化报告、查询接口和审核 Gate。
5. 发布审核的结构化合规清单，继续绑定内容版本与审核快照。
6. 需求信号、对标记录、内容实验的本地 CRUD 和前端“策略与实验”工作台。
7. Job 详情中的统一时间线和可确认的用量/费用状态，不把估算当账单。
8. 后端迁移、OpenAPI、前端类型、单元测试、端到端测试和真机安全测试。

### 3.2 非目标

- 不改成 Agent 直接编排生产；现有 Worker/SQLite 是唯一运行控制平面。
- 不引入 Redis、Celery、远程数据库、云托管或新的第三方运行依赖。
- 不自动抓取小红书搜索、评论、店铺或竞品数据；V2 只接受人工录入的公开链接与摘要。
- 不实现小红书多图笔记自动发布；仍以现有 9:16 MP4 双平台路径为正式发布格式。
- 不接入股票视频、生成式视频、第三方素材库或新的付费模型。
- 不执行真实付费模型调用，不点击真实平台最终发布按钮，除非用户另行明确授权具体内容。
- 不改变已有已发布记录、审核快照、设备 PIN、模型密钥或自动发布设置。

## 4. 兼容性原则

- 现有内容、任务、模板、素材、审核、发布、指标和设置必须可迁移并继续读取。
- 旧 `Scene` 缺少 `scene_type` 和 `visual_data` 时按 `image` 与空对象处理。
- 旧客户端仍可只发送 `compliance_confirmed=true`；服务端保存 `legacy_boolean_only=true`。新版 UI 必须提交完整合规清单。
- 旧内容没有 Brief 时仍可沿用原 `script/assets/render/all` 流程。某内容一旦生成了 Brief，在选择方案前禁止生成脚本或执行 `all`。
- 新渲染自动产生质量报告。审核当前有视频但没有匹配 PASS 报告时返回 `QUALITY_REVIEW_REQUIRED`；历史已经完成的 Approval/Publication 不回溯失效。
- 不静默切换模型、提供商、渲染器或付费路径。

## 5. 内置生产预设

新增只读的版本化预设目录，首版包含：

| id | 名称 | 适用内容 | 默认风格 | Brief | 本地卡片 |
|---|---|---|---|---|---|
| `tech_brief_video_v1` | 科技快讯 | 新闻、发布、产品更新 | 简洁专业、低信息密度 | 可选 | 支持 |
| `data_explainer_video_v1` | 数据解释 | 榜单、指标、实验结论 | 数据优先、中信息密度 | 推荐 | 支持 |
| `avatar_commentary_video_v1` | 数字人口播 | 观点、评论、教程 | 人物优先、低运动密度 | 推荐 | 支持 |

每个预设返回：

```json
{
  "id": "data_explainer_video_v1",
  "version": 1,
  "name": "数据解释",
  "description": "...",
  "content_type": "video",
  "supported_scene_types": ["image", "text_card", "stat_card", "quote_card"],
  "style": {
    "palette": ["#315bff", "#111827", "#f7f8fb", "#f2c94c"],
    "information_density": "medium",
    "motion_density": "medium",
    "transition": "fade"
  },
  "quality_rules": {
    "min_visual_types": 2,
    "max_consecutive_same_type": 2,
    "require_hero_scene": true
  }
}
```

预设由代码中的不可变目录提供，不新增自定义预设 CRUD。`Content.preset_id` 保存选定 id；未知 id 返回 `PRESET_NOT_FOUND`。现有内容迁移为 `tech_brief_video_v1`。

## 6. Production Brief 契约

### 6.1 数据

`Content` 新增：

- `preset_id: string`，默认 `tech_brief_video_v1`。
- `brief: JSON`，默认 `{}`。

Brief 结构：

```json
{
  "version": "1.0",
  "target_audience": "AI 产品与研发人员",
  "content_goal": "在 60 秒内解释发布价值与边界",
  "target_platform": "both",
  "quality_floor": "presentable",
  "concepts": [
    {
      "id": "c1",
      "title": "...",
      "hook": "...",
      "narrative_structure": "problem_solution",
      "visual_approach": "...",
      "core_message": "...",
      "why_this_works": "...",
      "estimated_calls": {"text": 1, "image": 7, "speech": 7},
      "estimated_cost_usd": null
    }
  ],
  "selected_concept_id": null,
  "selected_at": null
}
```

约束：

- 必须恰好返回 3 个 id 唯一的方案。
- `hook` 最多 100 字；标题最多 200 字；所有文本有现有输入长度边界。
- `narrative_structure` 仅允许 `problem_solution / comparison / myth_busting / timeline / tutorial / data_narrative / story`。
- 调用模型前由代码写入预计调用次数；模型不得决定价格。
- 没有用户配置的可靠价格时 `estimated_cost_usd=null`，UI 显示“费用未知”，不得显示 0。
- Brief 模型请求只带选题标题、摘要、来源 URL、模板、预设与平台，不带密钥、本人录音文字、照片、设备或历史发布数据。

### 6.2 API 与状态

- `GET /api/v1/presets`：返回内置预设。
- `POST /api/v1/contents/{id}/generate` 增加 `stage=brief`。走 content 队列、Idempotency-Key、每日额度和未知结果保护。
- `POST /api/v1/contents/{id}/brief/select`：

```json
{"version": 2, "concept_id": "c2"}
```

选择成功后内容版本加一，保存 revision，设置 `selected_concept_id/selected_at`，清除旧脚本、分镜、派生视频和旧审核。重复提交相同版本按现有版本冲突返回 409。

- `plan_script` 在有已选 Brief 时必须将选定方案作为约束，但来源事实仍来自真实来源，不能把 Brief 理由当作事实。

## 7. 多类型分镜

`Scene` 增加：

```json
{
  "scene_type": "image",
  "visual_data": {
    "headline": "",
    "body": "",
    "value": "",
    "source_url": "",
    "emphasis": ""
  },
  "hero_moment": false,
  "shot_intent": ""
}
```

规则：

- `image` 保持现有图片模型路径。
- `text_card`、`stat_card`、`quote_card` 使用 Pillow 和现有字体/画布能力在本地生成 9:16 图片素材，不调用图片模型。
- `stat_card.value` 和 `quote_card.body` 必须能关联到当前脚本中已验证且有来源的 Fact；否则审核前质量报告至少为 `REVISE`。
- 模型输出的 `scene_type` 只接受白名单；`visual_data` 不接受 HTML、命令、路径或任意表达式。
- 旧内容默认 `scene_type=image`。
- 预设质量规则用于报告：至少两种视觉类型、不得连续三个同类型镜头、至少一个 `hero_moment=true`。不足时报告 `REVISE`，不是自动修改内容。

## 8. Render Quality Review

### 8.1 数据表

新增 `quality_reviews`：

- `id`
- `content_id`，索引、外键
- `content_version`
- `video_asset_id`，外键
- `status: PASS | REVISE | FAIL`
- `report: JSON`
- `created_at / updated_at`
- 唯一约束 `(content_id, content_version, video_asset_id)`

### 8.2 自动检查

每次 `render` 成功登记 video/cover/subtitle 后，在同一任务中执行本地检查；检查失败不能把不完整报告当 PASS。

报告必须包含：

```json
{
  "version": "1.0",
  "status": "PASS",
  "video_asset_id": "...",
  "checks": {
    "technical": {},
    "visual": {},
    "audio": {},
    "subtitle": {},
    "structure": {},
    "fact_visual": {}
  },
  "issues": [],
  "warnings": [],
  "recommended_action": "present_for_review"
}
```

检查要求：

- technical：ffprobe 可解析；视频和音频流存在；分辨率、fps、codec、时长、文件大小有效；与设置不一致为 FAIL。
- visual：在避开 0 秒淡入的位置抽取开头、中段、高潮、结尾至少 4 帧；检测全黑/近全黑、解码失败和尺寸错误。临时帧测试结束清理，不进入业务素材。
- audio：检测音轨、EBU R128 集成响度和峰值；目标区间 `-18..-14 LUFS`、True Peak 不高于 `-1.0 dBFS`。无法解析为 REVISE，不伪造数值。
- subtitle：SRT 存在、非空、时间单调、结束时间不超过视频且覆盖率合理；烧录字幕仍以现有 ASS 路径为准。
- structure：视觉类型数量、连续重复、hero scene、shot intent 覆盖率。
- fact_visual：stat/quote 卡片来源与已验证 Fact 对应。
- 不依赖 OCR、联网或新的 AI 调用。无法自动判定的语义质量明确标为 `manual_required`。

### 8.3 API 与 Gate

- `GET /api/v1/contents/{id}/quality-reviews`：按时间倒序。
- `POST /api/v1/contents/{id}/quality-review`：对当前视频显式重检，返回 202 job；没有视频返回 `VIDEO_REQUIRED`。
- 内容详情返回 `latest_quality_review`。
- `POST /contents/{id}/approve` 要求当前 `content.version + video_asset_id` 存在 `PASS`；否则返回 `QUALITY_REVIEW_REQUIRED` 或 `QUALITY_REVIEW_NOT_PASSED`。
- 历史已批准和已发布记录不回溯撤销。

## 9. 结构化合规

`Approval` 新增 `compliance: JSON`，默认 `{}`。新版 `Review` 增加：

```json
{
  "compliance_confirmed": true,
  "compliance_checks": {
    "source_rights_confirmed": true,
    "no_unauthorized_material": true,
    "claims_not_exaggerated": true,
    "no_offplatform_diversion": true,
    "no_unverifiable_guarantees": true,
    "ai_disclosure_confirmed": true,
    "platform_rules_checked": true,
    "visuals_manually_reviewed": true
  }
}
```

- 新 UI 必须八项全部确认后才允许提交 APPROVED。
- 服务端再次验证全部为 true，并保存规则版本、内容版本、检查值和 notes。
- 旧客户端省略 `compliance_checks` 但传 `compliance_confirmed=true` 时保持兼容，并记录 `legacy_boolean_only=true`。
- 不把关键词扫描描述为法律结论；平台规则仍需人工按当前官方规则核对。

## 10. 策略与实验工作台

### 10.1 数据

新增三张表：

`demand_signals`

- `title`
- `source_kind: search | comment | forum | customer | manual`
- `source_url` 可空、仅公网 HTTP(S)
- `evidence` 文本
- `scores` JSON：`demand / production / delivery / presentation / compliance / iteration`，各 0..5
- `status: CANDIDATE | VALIDATED | REJECTED`
- 标准 Record 字段

`benchmarks`

- `platform: douyin | xiaohongshu | other`
- `account_name`
- `content_url` 可空、仅公网 HTTP(S)
- `title`
- `content_type: video | image_note | other`
- `analysis` JSON：`audience / pain_point / hook / cover / structure / cta / metrics / learnings / prohibited_copy`
- 标准 Record 字段

`experiments`

- `name`
- `hypothesis`
- `variable: angle | title | cover | format | publish_time | other`
- `status: DRAFT | RUNNING | COMPLETED | CANCELED`
- `content_ids` JSON，校验内容存在
- `publication_ids` JSON，校验发布存在
- `observation_windows` JSON，允许 `2h / 24h / 72h` 及自定义标签
- `conclusion`
- `next_action: continue | revise_title | revise_cover | change_angle | stop | undecided`
- 标准 Record 字段

### 10.2 API

每类资源提供 `GET list`、`POST create`、`PATCH /{id}`。V2 不提供 DELETE；无效项通过状态关闭。列表统一 `{items,total}`，输入 `extra=forbid`，错误使用现有 envelope。

路径：

- `/api/v1/strategy/demand-signals`
- `/api/v1/strategy/benchmarks`
- `/api/v1/strategy/experiments`

Experiment 列表额外返回所关联 Publication 的最新 Metric 和可计算增量；缺失值保持 `null`，不把 null 当 0，不把多个时间点相加。

### 10.3 前端

新增 `/strategy` 页面和导航“策略与实验”，包含三个 Tab：

- 需求信号：列表、六维评分、状态、新建/编辑。
- 对标拆解：账号、链接、封面/钩子/结构/CTA、可借鉴点、禁止照搬内容。
- 内容实验：假设、变量、关联内容/发布、观察窗口、最新指标、结论和下一步。

所有写操作有 loading、防重复提交和真实错误展示；外部链接使用 `noopener noreferrer`。

## 11. Job 时间线与费用状态

扩展 `job_dict(detail=true)` 返回：

```json
{
  "timeline": [
    {"at": "...", "kind": "step", "name": "render", "status": "SUCCEEDED", "detail": "..."},
    {"at": "...", "kind": "model_call", "name": "image", "status": "SUCCEEDED", "usage": {}}
  ],
  "cost": {
    "estimated_usd": null,
    "actual_usd": null,
    "status": "UNKNOWN"
  }
}
```

- 时间线由已有 JobStep 与 ModelCall 组合，稳定按时间排序，不复制敏感响应。
- 没有可靠价格配置和服务商账单时费用为 null/UNKNOWN。
- Model usage 可展示调用数和 token，不得从缺失 usage 推算 0 成本。
- 前端任务详情展示时间线和“实际费用未知/服务商账单为准”。

## 12. 数据迁移

新增 Alembic `0003_v2_quality_growth.py`：

- `contents.preset_id`，非空，默认 `tech_brief_video_v1`。
- `contents.brief`，JSON，默认 `{}`。
- `approvals.compliance`，JSON，默认 `{}`。
- `quality_reviews`、`demand_signals`、`benchmarks`、`experiments`。

升级前沿用现有部署备份。降级删除新增表和列，会丢失 V2 Brief、质量报告、合规明细和策略数据；文档必须明确应优先从升级前备份恢复。迁移测试覆盖旧库升级、默认值、唯一约束和降级边界。

## 13. 安全与外部操作

- 来源 URL 继续使用现有 URL/SSRF 校验；策略记录不自动抓取 URL。
- 不存储模型密钥、登录凭据、PIN、手机截图或完整 UI hierarchy。
- 卡片文字按纯文本绘制；禁止 HTML/ASS/FFmpeg 表达式注入。
- 质量检查 subprocess 使用参数数组、受控路径和超时；清理临时文件。
- 真机测试允许读取设备、检查账号、传输测试素材和停在最终编辑页。只有用户对具体内容和目标账号明确授权真实发布时，才可在持久化提交围栏后继续最终点击。
- 测试不得覆盖现有 `PUBLISHED` 记录，不得重放结果未知的模型或发布任务。

## 14. 验收标准

### 14.1 后端

- 旧数据库成功升级，现有记录可读取。
- Brief 返回三个结构化方案；选择后版本、revision、失效规则正确。
- 有未选择 Brief 时脚本生成被阻止；无 Brief 的旧内容仍兼容。
- 四种 Scene 均可通过 schema；三种卡片本地生成且图片模型调用数为 0。
- 正常视频自动产生 PASS 质量报告；错误分辨率、黑帧、无音轨、字幕时间错误和结构重复分别有覆盖。
- 非 PASS 或缺报告不能新建 Approval；历史 Approval/Publication 不变。
- 结构化合规保存到 Approval；旧 boolean 客户端兼容。
- 策略 CRUD、URL 校验、关联 ID 校验、null 指标语义正确。
- Job timeline 不含密钥、任意 provider body 或签名 URL。

### 14.2 前端

- 内容页可选预设、生成 Brief、比较三个方案、选择后生成脚本。
- 镜头页可编辑类型、hero、shot intent 和卡片字段；不兼容字段有明确提示。
- 预览/审核页展示质量报告；非 PASS 时审核按钮禁用并显示具体问题。
- 审核确认框包含八项合规确认。
- `/strategy` 三个 Tab 可完成创建、编辑、筛选与错误恢复。
- 任务中心显示统一时间线和费用未知状态。
- 移动/桌面常用宽度无关键控件遮挡。

### 14.3 自动验证命令

```sh
uv run --project backend ruff check backend/app backend/tests scripts/control.py scripts/local_ai_setup.py scripts/local_ai_runtime.py
uv run --project backend pytest -q
cd frontend && pnpm typecheck
cd frontend && pnpm test:unit -- --run
cd frontend && pnpm build
cd frontend && pnpm test:e2e
python3 scripts/control.py env
```

部署/启动后执行只读和隔离验收：

```sh
python3 scripts/control.py deploy
python3 scripts/control.py status
curl -fsS http://127.0.0.1:8765/api/v1/health
curl -fsS http://127.0.0.1:8765/api/v1/diagnostics
```

### 14.4 真机安全验收

1. `adb devices -l` 必须出现唯一且已授权设备。
2. `GET /devices` 和既有 inspect 流程识别目标设备与平台 App。
3. 使用未发布测试素材验证传输、哈希、媒体库唯一项、账号核验和编辑页准备。
4. 在最终发布按钮前停止；检查标题与正文回读一致。
5. 不改变历史已发布记录，不执行真实发布，不采集验证码或登录凭据。

若 ADB 无设备、平台未登录或页面版本不受支持，代码/模拟验证可独立 PASS，真机项必须报告 BLOCK 及解除条件。

### 14.5 经授权的真实发布验收

1. 小红书 9.46 媒体网格隐藏文件名时，只能在操作员已核对本地/手机 SHA-256、MediaStore 时长及预览画面后调用 `POST /publications/{id}/confirm-media-selection`。请求必须 `confirmed=true` 且 notes 非空；确认本身不写入提交时间戳。
2. 确认只对原 `MEDIA_SELECTION_REQUIRED` 记录生效，要求原 job 停稳、Approval/快照/账号/设备/App 版本/远程文件哈希一致，并用状态摘要防并发覆盖。锁屏时复用既有一次性安全解锁和失败锁。
3. 小红书 9.46 的真实提交是两阶段：首先点击唯一“发笔记”，再在“声明内容来源”中选择“含 AI 合成内容”并点击“确认并发布”。第二阶段结构不匹配时停止，不选其他声明。
4. 作品检测使用发布快照中的平台专用标题，不使用工作台主标题。官方 `xhslink.cn` 短链经公网 DNS/SSRF/跳转校验解析，落库链接去除分享追踪参数。
5. 只有目标标题、新复制的唯一作品链接和作品 ID 一致时记录 `PUBLISHED`；其他结果保持 `VERIFYING` 且不重放提交。

## 15. 实现分工

- 主 Agent：本 SPEC、契约冻结、差异审查、迁移审查、安全审查、全量回归、部署和真机验收、最终结论。
- Backend 子 Agent：`backend/**`，实现模型、迁移、schemas、API、workflow、媒体质量、策略 CRUD、时间线及后端测试。
- Frontend 子 Agent：`frontend/**`，实现预设/Brief/分镜/质量报告/合规/策略页/任务时间线及前端测试。
- 子 Agent 不修改本 SPEC、README、其他 docs、脚本、本地配置和 Git 状态；不得提交、推送、部署、调用付费模型或操作最终发布。

## 16. 最终交付门槛

只有以下条件同时满足才可整体 `PASS`：

- 主 Agent 审查所有差异且 simplification review 完成。
- 迁移、后端全量、前端类型/单元/构建/E2E 全部通过。
- 本地部署健康，备份可验证。
- 真机可见时完成安全验收；真机不可见时必须将整体结论标为 `BLOCK`，不得用模拟测试替代。
- 文档、运行 OpenAPI 与离线 OpenAPI 一致。
- 未泄漏密钥、PIN、签名 URL、个人素材和设备敏感信息。
