# 画面、配音与自动发布调整

> 历史验证记录：结果仅对应文中日期、代码和环境。私人标识已脱敏；后续变更见 [CHANGELOG](../CHANGELOG.md)，当前提交仍需按 [贡献指南](../CONTRIBUTING.md) 验证。

> 此文件保留本地模型接入前的增强验收结果。本人声音与照片数字人的最新实现和验证见 [本地功能验收记录](local-ai-validation.md)。

日期：2026-09-10。本机 Vue 3 / FastAPI 工作台，USB 连接 Redmi Android 16。

## 需求与结果

| 项目 | 当前结果 |
|---|---|
| 图片右下角字样 | 方舟请求原来固定 `watermark=true`，现为可配置且默认关闭。本轮没有重新付费生成图片，已有图片中的字样仍在；发布平台的 AI 内容声明独立保留。 |
| 生硬配音 | 实测旧音频自然时长 38.976 秒，原成片把它放慢为 60 秒（0.6496 倍）。默认改为保持自然语速，并按整镜头生成旁白，增加可配置朗读指导。预览复用旧音频，仅验证取消后期变速；新的指导参数尚未做额外付费试听。 |
| 黄色字幕重点 | 分镜增加字面关键词，字幕与画面标题只高亮匹配部分；其余文字为白色。已检查真实视频帧，包括跨行的数值高亮。SRT 保持纯文本。 |
| 本人声纹与照片数字人 | 用户表示已有声纹 ID 或数字人接口，稍后提供。现有音色字段可填写同模型的复刻 ID；数字人尚未接入，等待接口鉴权、请求和输出契约，没有擅自切换模型或发起费用。 |
| 保存 PIN、自动解锁 | 已实际写入 macOS 钥匙串，并从新进程读取、成功解锁。Android 16 的被遮挡 activity 与系统窗口包名不同，已按真实节点包名校准。未写入项目配置、日志、参数或备份。 |
| 审核后自动发布 | 用户明确授权后实现并开启。审核与任务入队同事务；双平台共用手机时串行预留。最终点击前保存提交记录，未知结果、取消和重启均不会重复提交。尚未对新内容实际执行自动最终发布；已发布的旧作品保留原记录。 |

## 代码与契约

- [模型协议](../backend/app/protocols.py)：`models.image.watermark` 默认 `false`；`models.speech.instruction` 默认空，只对当前百炼 Qwen-Audio-3.0-TTS 发送独立 `input.instruction`，不拼入朗读正文。
- [分镜与设置](../backend/app/schemas.py)：`Scene.highlight_terms` 默认空，最多 8 个、每个最多 40 字，必须出现在本镜头旁白或标题；`video.timing_mode` 默认 `natural`，可显式选择兼容的 `fit_target`。
- [媒体合成](../backend/app/integrations/media.py)、[工作流](../backend/app/workflow.py)：自然时长、受控 ASS 颜色、整镜头配音；关键词与渲染版本参与缓存。关键词修改会使审核和原渲染失效，可复用原图与音频。
- [发布事务](../backend/app/services.py)、[API](../backend/app/api.py)、[设备队列](../backend/app/worker.py)、[最终提交](../backend/app/integrations/publishing.py)：配置 `publishing.auto_publish_after_approval` 默认关闭，本机已按授权开启。仅在新的审核动作中入队，不追溯启动历史审核。同内容已有关联发布时跳过，避免重复。
- 发布快照保存 `auto_publish`；公开记录增加只读 `auto_publish`。审核请求可带 `auto_publish_expected`，若确认框展示的方式与服务端当前方式不同，返回 409，不按另一种方式发布。
- 最终点击前持久化 `submit_started_at`，点击返回后保存 `submit_clicked_at`；结果不明保持 `VERIFYING`，继续准备接口拒绝重放。只有用户核验未发布，并提供 `confirmed_not_published=true` 和说明，才可取消这类记录。
- [钥匙串存储](../backend/app/device_credentials.py)、[解锁](../backend/app/integrations/device.py)、[维护入口](../backend/app/manage.py)：原生 SecItem 调用，项目与设备隔离、禁止同步和后台认证弹窗。错误 PIN 或中断触发跨进程失败锁存，必须显式更新凭据后解除。
- 前端修改涉及 [内容编辑](../frontend/src/views/ContentView.vue)、[编辑状态识别](../frontend/src/content-editor.ts)、[设置](../frontend/src/views/SetupView.vue)、[发布中心](../frontend/src/views/PublicationView.vue) 和 [类型](../frontend/src/types.ts)。关键词使用现有组件库的标签输入控件，回车即可确认；修改关键词会先保存再渲染。

没有新增依赖或数据库表。数据库版本仍为 `0002`；新增业务字段存于现有 JSON，旧请求可省略新字段。回退旧代码时应恢复改动前备份，避免旧版严格字段校验拒绝新配置。恢复功能会关闭模型、计划和审核后自动发布；钥匙串凭据单独管理，不包含在业务备份内。

## 实际验证

- `backend/.venv/bin/pytest backend/tests -q`：163 项通过。
- `backend/.venv/bin/ruff check backend/app backend/tests`：通过。
- 前端 `pnpm typecheck`、`pnpm test:unit`、`pnpm build`：通过，单元测试 7 项。
- `PLAYWRIGHT_EXECUTABLE_PATH=本机已安装测试浏览器 pnpm test:e2e`：8 项通过，使用隔离接口夹具。覆盖关键词单独修改、渲染前保存、自动发布说明与方式绑定。
- 使用已有 9 张图片、9 段真实配音重新合成预览：38.976 秒，1080×1920，`speech_tempo=1.0`，没有新增模型调用。
- 工作台当前内容为 v6、待审核，视频素材 `[已脱敏记录ID]`。真实浏览器加载为 38.976 秒、1080×1920、readyState 4，无页面运行错误。已发布记录仍关联 v5 与原作品 ID，没有再次发布。
- 多句整镜头旁白保持完整音频，在实测音频区间内按句子字数估计字幕分段，避免整段堆积或过长失败；这不是字级强制对齐。单句配音区间仍使用实测时长。
- Keychain 的 `device-pin set` 实际保存成功；后台新进程返回 `unlocked=true`、`keyguard_showing=false`、`retry_blocked=false`。
- 自动导航实际关闭“链接复制成功”提示并返回抖音本人主页，核验绑定账号一致；没有点击分享给他人、草稿退出或最终发布。
- `python3 scripts/control.py backup` 与 `verify-backup --from <项目目录>/data/backups/20260910T120242646194Z`：30 个素材和数据库哈希校验通过，schema 为 `0002`。离线 OpenAPI 与运行服务一致。
- 三类模型密钥与手机 PIN 对 75 个源码、日志、运行记录及数据库检查项未发现泄漏匹配。模型用量仍为 19 次；本轮新增付费调用为 0，账单费用未知。
- 自动提交测试覆盖：先持久化再点击、提交记录失败不点击、文案变化不点击、缺少成功证据保留未知、两平台共用手机排队、提交后取消保留已确认结果、进程中断不重放、审核时发布方式变化拒绝执行。

初次相关回归发现：旧用例仍预期强制拉伸时长、测试夹具缺少分镜、字幕关键词未计入前端编辑状态、下拉创建控件不能稳定回车确认。这些已分别改为检验自然时长、补全有效夹具、纳入编辑指纹和使用标签输入控件。后续回归全部通过。真实解锁初次在输入前因 Android 16 窗口识别停止，未输入错误密码；修正后已实测成功。

已执行 simplification review：没有增加通用队列、策略层或新的基础设施；仅把两处共用的发布创建事务复用为一个函数。钥匙串与最终发布各有一个最小独立边界，便于限制秘密访问和不可逆动作。

## 尚未验证的边界

自动最终发布的真实成功页面与链接回读，需要下一条明确审核通过的新内容；当前已发布内容不会重复发布。小红书仍需有效登录和本人账号绑定。声纹与数字人实际效果等待用户已有 ID／接口后验证。照片数字人叠加、口型与配音同步、额外费用均未描述为已完成。

## 服务商依据

- [火山官方 SDK 的图片参数](https://github.com/volcengine/volcengine-python-sdk/blob/master/volcenginesdkarkruntime/resources/images/images.py)
- [百炼 Qwen-Audio-TTS HTTP 参数](https://help.aliyun.com/zh/model-studio/cosyvoice-tts-http-api)
- [百炼声音复刻 API](https://help.aliyun.com/zh/model-studio/voice-clone-design-http-api)
- [照片与音频驱动数字人示例](https://help.aliyun.com/zh/model-studio/wan-s2v-api)：用于说明能力可行，不代表已选用或调用该模型。

## 模型高亮词不匹配修复（2026-09-11）

结论：**PASS**。最新失败任务的第 8 个镜头旁白为“更低的API价格”，模型高亮词为“更低API价格”；另一次结果将中文数字改为“9月14日”等表示。原来的严格原文匹配使整条文案生成失败，并被统一包装成 `MODEL_OUTPUT_INVALID`。

仅在模型生成分镜时，通过服务端 Pydantic 校验上下文允许取消这些不匹配的高亮。原旁白、画面标题、图片提示、动画及有效高亮均保留；全不匹配时使用普通字幕。任务 `result.highlight_warnings` 保存镜头 ID 与被取消的词，既有任务步骤详情显示具体原因。手工编辑仍须原文匹配。字段类型、数量、长度、动画枚举、平台文案和事实来源规则保持原有校验。

修改文件为 `backend/app/schemas.py`、`backend/app/workflow.py`、`backend/tests/test_workflow.py` 和本记录。没有新增数据库表、迁移、依赖或前端改动；任务结果仅增加兼容字段。已完成 simplification review，复用现有 Scene 校验、任务结果及步骤详情，未新增抽象层。

- `cd backend && .venv/bin/pytest -q tests/test_workflow.py tests/test_platform_rules.py`：16 passed，1.79 秒。
- `cd backend && .venv/bin/pytest -q`：245 passed，16.12 秒；包含同目录启动脚本任务当时已有的用例，未修改该任务文件。仅两条既有 TestClient/AnyIO 弃用警告。
- `backend/.venv/bin/ruff check backend/app/schemas.py backend/app/workflow.py backend/tests/test_workflow.py`：All checks passed。
- 两次真实失败任务在临时 SQLite 副本中调用实际 `plan_script` 与缓存读取路径回放：均保留 8 个镜头并完成校验；第一次记录 1 个镜头警告，第二次记录 5 个镜头警告。网络客户端设置为调用即失败，实际调用数为 0，调用记录与尝试次数不变。临时副本验证结束自动清理，原业务数据未被回放覆盖。
- `python3 scripts/control.py restart`、`status`：本地 API 与两个 Worker 重启成功；`GET /api/v1/health` 返回 `status=ok`。Chatterbox 0.75 及原两个本地开关保留。

本次没有实际生成新配图、完整视频或发布作品。原失败任务仍保留失败历史；用户可在任务中心对最新任务显式重试。现有 `MODEL_OUTPUT_INVALID` 重试逻辑会重新请求文本模型，之后的一键生成还会继续素材生成；本次没有擅自发起该付费续跑，也没有将离线缓存验证称作真实任务已恢复。未执行 Git 操作。此为局部缺陷修复，不触发新增功能的 Notion 已验收知识写入。
