# V2 生产质量与经营闭环验收记录

> 历史验证记录：结果仅对应文中日期、代码和环境。私人标识已脱敏；后续变更见 [CHANGELOG](../CHANGELOG.md)，当前提交仍需按 [贡献指南](../CONTRIBUTING.md) 验证。

日期：2026-09-13（America/New_York；运行证据为 UTC 2026-09-14）。\
当前结论：**PASS（V2 功能、真机安全门与经授权双平台实发）**。代码、迁移、后端、前端、浏览器、本地运行态、成片质量和 Android 真机流程均已验收。用户明确授权后，同一条已审核内容分别发布到抖音 40.4.0 和小红书 9.46.0，两条 Publication 均通过绑定手机回读作品 ID/链接后记录为 `PUBLISHED`，没有重复提交。

## 完成范围

- 新增冻结实现 SPEC：`docs/v2-production-quality-growth-spec.md`。
- 三个只读版本化生产预设；Content 保存 `preset_id` 与 Production Brief。
- Brief 使用一次受保护文本调用生成三个结构化方案；代码覆盖预计调用数，费用保持未知。选择方案会升版本并清除旧脚本、分镜、媒体与审核绑定。
- 分镜增加 `image / text_card / stat_card / quote_card`、主视觉和镜头意图。三种卡片在本地生成，不调用图片模型；数据/引语卡需要匹配已核验事实。
- 每次渲染自动执行离线质量检查并持久化报告；当前版本和视频没有 `PASS` 时不能审核通过。
- 审核增加八项结构化合规，同时兼容旧客户端 boolean，并保留自动发布方式变更 409 与确认框显式提示。
- 新增需求信号、对标拆解、内容实验 CRUD；多发布实验分别展示最新指标和 nullable 增量。
- Job 详情增加脱敏时间线和费用未知状态。
- Alembic `0003` 增加 V2 列与表；降级会丢失 V2 数据，应从升级前备份恢复。

## 验证过程

后端、前端和设备流程经实现审查及回归修正后，独立执行下列验证。表格保留该次历史验收的实际结果，不代表后来任意提交均已通过。

## 主 Agent实际验证

| 命令或检查 | 结果 |
|---|---|
| `backend/.venv/bin/pytest -q` | **291 passed**；包含无线 ADB 物理身份、小红书 9.46 素材确认/锁屏恢复/两阶段提交与 `.cn` 短链回归；2 条现有 Starlette/httpx、AnyIO 弃用警告 |
| `backend/.venv/bin/ruff check backend/app backend/tests scripts/control.py scripts/local_ai_setup.py scripts/local_ai_runtime.py` | All checks passed |
| `pnpm typecheck` | 通过 |
| `pnpm test:unit -- --run` | **9 passed** |
| `pnpm build` | Vue 类型检查和 Vite 构建通过；主包 1,205.31 kB，保留既有大 chunk 警告 |
| `pnpm test:e2e` | 沙箱内 Chromium 因 MachPortRendezvous 权限在测试执行前失败；按既有授权在沙箱外重跑，最终 **20 passed** |
| 隔离数据库启动 FastAPI | 临时目录从空库自动执行 `0001 → 0002 → 0003`；`/health` 200，版本 `0.1.0` |
| 隔离 API | `/presets` 返回 3 项；需求、对标、实验实际 POST/GET 成功；无关联发布的实验返回空 `publication_metrics`，不伪造指标 |
| 真实 Vue + 临时 FastAPI | `/strategy` 与 `/contents` 成功加载真实后端数据；导航、预设、Brief 空态、评分列表布局可读 |
| 本地卡片 | 实际生成 1080×1920 中文数据卡；标题、数值、正文和强调文字可读，没有图片模型调用 |
| OpenAPI | 运行定义共 55 条路径；离线 `docs/openapi.json` 已由相同运行定义重新生成，含新增素材确认接口 |
| `git diff --check` | 通过 |

`uv run --project backend ...` 的主 Agent首次重跑仅因沙箱不能读取用户级 uv cache 失败；改用同一锁定项目的 `backend/.venv/bin/*` 后取得上述真实结果，属于环境限制，不冒充原命令通过。

## 安全、数据和兼容性

- 本次用户授权了一条内容的真实生成与双平台发布。实际记录为 2 次文本、4 次图片服务调用；8 段配音和数字人使用本地能力。未取得服务商账单，费用仍为未知，不写 0。
- 只新增两条本次授权的 Publication，没有改写历史 `PUBLISHED`、Approval 或指标记录；最终有效任务与发布占用数都为 0。
- 隔离应用运行只写 `/private/tmp/yingxu-v2-runtime.*`，没有迁移或覆盖正式 `data/publisher.sqlite3`。真机测试另外向手机 `Movies/LocalPublisher/lp_文件指纹` 传输了明确标记“不会发布”的 4 秒测试视频和封面。
- Brief 重生成先冻结旧 revision 再升版本；历史 Publication snapshot 和 Asset 不变。
- V1 Approval 的旧快照在 `compliance={}` 时按 legacy snapshot 兼容；新审核要求匹配当前版本与当前视频的 PASS。
- 时间线只返回受控 step 文本和数值 usage；ModelCall 原响应、任意 provider body、签名 URL 和密钥不进入新增时间线。
- 策略 URL 只保存人工输入，不自动访问；前端外链使用 `noopener noreferrer`。
- OpenMontage 仅借鉴公开架构思想，没有复制 AGPL 源码或引入其依赖。
- 隔离 FastAPI、Vite、临时数据库、截图和测试媒体在验收后均已停止或删除；无线 ADB 连接保留，手机上的测试素材和草稿按设备安全契约不自动删除。

## 真机证据与保留边界

- 官方 ADB 1.0.41 通过 mDNS 连接经授权的无线调试设备；连接地址、设备型号与物理序列号已脱敏。
- 真实测试发现无线传输标识无法命中物理 serial 已保存的钥匙串 PIN。实现最小兼容后，系统仍用 IP:port 发送 ADB 命令，但用受控 `ro.serialno` 查找钥匙串与失败锁；对应专项回归通过。
- 检查时系统 `mWakefulness=Dozing`、前台为 NotificationShade；发送一次唤醒后，现有一次性 PIN 保护实际解锁成功，失败锁保持 false，没有打印 PIN。
- 抖音 40.4.0 本人主页与正式绑定账号实际匹配。第一次从另一平台启动准备按 `ACCOUNT_VERIFICATION_REQUIRED` 停止、未传输；安全导航回本人主页后，4 秒 H.264/AAC 测试视频完成传输、SHA-256、独立相册、媒体库唯一视频、选材、标题和正文回读，最终返回 `WAITING_USER / editor / final_publish_clicked=false`。
- 用户随后确认小红书已登录。9.46.0 初次因本人主页标签从“编辑资料”变为“编辑主页”未识别；补充白名单与回归后，实际 inspect 为 `ready=true`、账号可见、warnings 空，具体账号没有输出。
- 小红书创建页实际为“从相册选择 / 拍摄与直播”，媒体页为“全部 / 视频 / 照片”；补充版本识别后可稳定到达自定义网格。网格不暴露 `lp_文件指纹.mp4`，因此正式产品仍返回 `NEEDS_ATTENTION / MEDIA_SELECTION_REQUIRED` 等待用户选材，不按缩略图位置盲点。
- 本次专用测试素材额外满足可自动证明的条件：远端 SHA-256 与大小一致、网格只有一个 `00:04` 节点，该方格 93.16% 像素匹配纯蓝 `#315bff` 测试画面。四项重新核验后只点击该方格，并通过唯一“下一步”到达 `CapaPostNotePlatformActivity`。
- 9.46.0 最终编辑页实际使用“添加作品描述”和“发笔记”；加入严格白名单与回归后，测试标题、正文均精确回读，`page=editor`、`final_button_visible=true`、`final_publish_clicked=false`。

## 经授权的实际生成与发布

- 来源选用 Yoshua Bengio 2026-09-11 的 *Why are AI agents lying, cheating and coordinating?*，脚本严格区分已观察事件、机制假设与猜想，不把个别事件泛化为所有 AI。
- 最终成片为 1080×1920、30 fps、H.264/AAC、62.167 秒、3,951,644 bytes；响度 -16.0 LUFS、真峰值 -1.45 dBFS，9 条字幕时间单调且覆盖 99.99%。3 种视觉类型、最长连续同类型 1、hero 1、镜头意图覆盖 100%，质量报告 `PASS`。
- 抖音从已绑定本人主页完成专用指纹相册、唯一视频、标题/正文及一次提交围栏；后续通过目标作品页回读唯一作品 ID 并记录 `PUBLISHED`。
- 小红书媒体网格有两个 1:02 视频，因此没有盲点。主 Agent 用本地/远程 SHA-256、MediaStore 时长与新旧顺序、预览首帧三重核对后写入 operator confirmation；最终正确声明“含 AI 合成内容”，作品详情也显示同等作者声明。
- 小红书官方 `.cn` 分享短链经受限公网解析取得作品 ID；持久化 `work_url` 已去除分享 token、账号标识和追踪查询参数。

## 恢复与交接

- 正式升级前运行现有备份并验证；`0003` 降级会删除 Brief、质量、合规明细和策略实验数据。
- 当前未执行 `git add/commit/push`，未发布远程服务环境；仅部署到本机 `127.0.0.1:8765`，API/content/device 三个进程均健康。
- 旧的“映序V2自动测试”抖音草稿已在明确识别后不保存返回，不可恢复；其他手机素材和历史草稿未删除。小红书文件名隐藏时仍需操作员核对素材；验证码、登录、权限和账号绑定继续由用户控制。
