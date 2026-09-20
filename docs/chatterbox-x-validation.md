# Chatterbox、配图关联与 X 数据采集验收

> 历史验证记录：结果仅对应文中日期、代码和环境。私人标识已脱敏；后续变更见 [CHANGELOG](../CHANGELOG.md)，当前提交仍需按 [贡献指南](../CONTRIBUTING.md) 验证。

日期：2026-09-11。本次整体结论 **BLOCK：X/TikHub 真实数据读取被服务端拒绝**。Chatterbox 安装与本人试听、单镜头重绘功能及代码回归均为 **PASS**；没有把接口接入完成等同于已取得 X 数据。

## 已完成的实际结果

用户授权安装 Chatterbox Multilingual V3，使用本人参考录音完成 0.35、0.75 两档试听，并明确选定 0.75。当前已保存 `voice_model=chatterbox`、`emotion_strength=0.75`，本人声音和数字人开关均为 true。原 Qwen3、MuseTalk、Whisper 保留且安装检查仍为通过。默认产品配置仍为 Qwen3 / 0.65，新安装不会自动采用当前用户选择。

| 本机实际推理 | 结果 |
|---|---|
| Chatterbox 安装检查 | Apple M4 Max / MLX；权重、S3 编码器、中文分词均可在断网进程加载，约 1.823 秒 |
| 0.35 声音试听 | 8.84 秒，24 kHz PCM16 WAV；推理约 10.239 秒 |
| 0.75 声音试听 | 9.12 秒，24 kHz PCM16 WAV；推理约 5.781 秒 |
| 本地 Whisper 复核生成的测试旁白 | 两档句意完整；低档 ASR 有“结论/节论”同音识别差异，高档识别文字一致。ASR 不作为音色相似度判定 |
| 0.75 数字人预览 | 9.12 秒，540×960 H.264/AAC；推理约 29.864 秒；在 0.8、4.0、7.8 秒抽帧检查嘴部变化与画面完整性 |

测试旁白为通用介绍句，个人原始录音文字、素材路径和凭据不写入本文。本地推理没有云端模型请求。Chatterbox 的表现力参数真实传入 `exaggeration`，但相似度和自然度仍需以用户试听判断，短素材耗时不构成长视频耗时承诺。

配图现在以每个镜头旁白、标题及最匹配的一条来源事实为依据，要求表现具体动作/流程；其他镜头的统计不混入，精确名称与数字由本地排版负责。用户分别授权了两次 Seedream 调用：第一张包含错误百分比口径和后续镜头数据，已拒绝；第二张改为无数字标签的流程示意，消除了已发现的错误标注。每次授权仅执行一次图片请求，没有第三次调用。

第二次修正生成 v5，1080×1920 H.264/AAC，42.266667 秒。8.7 秒抽帧确认完整主图、独立标题、黄色关键词与数字人分区；流程图属于概念示意，未宣称能由图像独立准确表达全部实验细节。其他六张图片与全部原配音素材 ID 保持一致。随后用户自行生成 v6 并完成发布，最终只读核对为 v6 / APPROVED，发布记录为 PUBLISHED。本任务保留该已发布版本，没有替换它的 Qwen3 音轨或重新发布；Chatterbox 0.75 用于之后明确生成的新旁白。

## X/TikHub 的真实边界

已配置 OpenAI、AnthropicAI、huggingface、GoogleDeepMind、github 五个 X 账号来源。先验证官方公开时间线格式；应用实际采集遭遇 HTTP 429，未规避限流。用户随后明确选择现有数据 API，并提供 TikHub Key，已仅保存在本机 `.env`，权限 0600。

TikHub 的 [X 用户推文接口](https://docs.tikhub.io/191321711e0) 为 `GET /api/v1/twitter/web/fetch_user_post_tweet`。用户曾提供的 Threads 文档不是此任务最终选用的来源；没有改做 Threads。五个账号保留规范化 X 主页，仅将来源类型改为 `x_tikhub`。

只对 OpenAI 发出一次正式采集和一次同任务的错误原因诊断重试：两次均 HTTP 403，任务 FAILED，调用记录 attempts=2，安全原因分类为 `unspecified`。未得到帖子数据；其余四个账号未继续请求。响应没有可确认的具体原因，不能断言余额不足、Key 无效或 IP 白名单问题。费用没有账单证据，保持未知。

解除条件：在 TikHub 确认当前 Key 能访问该 X 接口，检查接口权限、账户状态及后台错误信息；账户或凭据状态改变后再明确重试。当前不会自动重发、换 Key、换来源或继续调用其他账号。

## 主 Agent 最终验证

以下回归在同目录另一项平台文案规则工作完成后运行，保留其修改并验证当前整套代码。

| 实际命令或检查 | 结果 |
|---|---|
| `cd backend && .venv/bin/pytest -q` | **234 passed**，15.75 秒；仅 2 条现有 TestClient/AnyIO 弃用警告 |
| `backend/.venv/bin/ruff check backend/app backend/tests scripts/local_ai_setup.py scripts/local_ai_runtime.py` | All checks passed |
| `cd frontend && pnpm test:unit && pnpm build` | **9 passed**；Vue 类型检查、Vite 生产构建成功，保留既有大包体提示 |
| `PLAYWRIGHT_EXECUTABLE_PATH='<本机测试浏览器路径>' pnpm test:e2e` | **16 passed**，22.2 秒；使用隔离接口夹具，无付费调用或真实发布 |
| `backend/.venv/bin/python scripts/local_ai_setup.py verify --component chatterbox` | 实际断网模型加载成功；在线状态四组件均 installed=true，busy=false |
| `ffprobe -v error -show_entries format=duration,size:stream=index,codec_name,codec_type,width,height,sample_rate -of json <成片>` | 高档数字人和修正视频格式、时长、音视频流均有效，已抽帧查看 |
| `python3 scripts/control.py verify-backup --from data/backups/20260911T094310974178Z` | valid=true，73 个素材，schema revision 0002 |
| `python3 scripts/control.py status` | API、内容 Worker、设备 Worker 均 running=true；受限命令不能读取 ps，提升本次只读进程检查权限后取得真实结果 |
| 在线 API 只读核对 | 后续 Chatterbox / 0.75 / 两个开关 true；v6 已发布记录不变；目标外图片与全部音频保留；TikHub 明确 FAILED / HTTP 403 |
| 本地保密与文档结构检查 | 4 个实际密钥在 100 个源码/测试/文档/日志/记录文件中匹配数为 0；33 条调用记录的响应与事件匹配数为 0；`.env` 为 0600。9 份 Markdown 本地链接有效、代码围栏闭合，OpenAPI 新字段存在 |

## 文件、契约和复审

- 本地模型：`scripts/local_ai_setup.py`、`scripts/local_ai_runtime.py`、`scripts/local_ai/tts.requirements.{in,lock}`；固定 Chatterbox 与 S3 权重，增加中文分词资源校验和离线缓存。原依赖版本比对无漂移，仅增加 `spacy-pkuseg 1.0.1`、`srsly 2.5.3`、`catalogue 2.0.10`。
- 后端：`backend/app/{config,schemas,api,services,workflow,providers}.py`、`backend/app/integrations/{local_ai,sources}.py`。新增模型选择和表现力字段、`images` 阶段的 `scene_id`、X 来源类型及付费读取防重。
- 前端：`frontend/src/components/LocalAISettings.vue`、`frontend/src/views/{SetupView,SourceView,ContentView}.vue`、`frontend/src/{api,types}.ts`，及模型/数据额度标签。
- 保留长期回归测试，覆盖新模型输入与缓存、目标重绘、来源解析、付费快照、结果未知、限额、重试及密钥不外泄。README、架构、契约、本地使用说明和离线 OpenAPI 同步。

没有数据库表、字段或迁移变化；继续使用现有设置、任务、素材和调用记录。旧客户端可省略新增声音字段；免费来源旧请求仍可用，TikHub 必须显式绑定提供方式。细节见 [接口契约](implementation-contract.md) 与 [离线 OpenAPI](openapi.json)。

本轮 simplification review 已完成：复用现有任务、预览、额度和调用记录，不增加服务、队列或通用适配层；仅为真实 Chatterbox 中文推理补齐必要的独立环境依赖。首次离线加载遇到代理客户端可选依赖问题，修复为清除断网模型进程内无用的代理变量，未关闭 TLS 或放宽网络隔离。

异常时可按上述命令复核组件，在任务中心查看安全错误。停用 Chatterbox 可在本地设置中明确切回仍保留的 Qwen3；旧素材与已发布快照不会随设置改变。没有卸载、清理业务数据、Git 暂存/提交/推送或远程部署。Notion 知识补录仍受项目知识库映射不明确阻塞，本轮整体 BLOCK 不写入已验收知识。

本次临时 Vite 开发进程已停止，8765 上的正式本地工作台与三个服务继续运行。结构检查脚本首次使用了不在 OpenAPI 中公开的内部设置模型名，修正为检查实际 `LocalAIPreview` / `Generation` 定义后通过；没有据此修改产品契约。
