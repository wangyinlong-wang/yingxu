# 本地 browser-use / Chrome 发布与分析

发布中心支持按账号选择 `android` 或 `browser` 通道，浏览器通道使用本机 Google Chrome 和 browser-use **0.13.10** 的 Actor/CDP 接口，不调用 LLM 或 Browser Use 云服务。当前覆盖抖音、小红书的视频准备、受审核约束的提交、作品识别、作品指标读取；分析沿用现有累计快照和趋势页面。

## 安装与使用

```sh
./scripts/setup_browser_use.sh
```

依赖锁在 `scripts/browser_use/requirements.lock`，安装目录是项目内 `.tools/browser-use/.venv`，与后端、声音和视频环境隔离。默认 Chrome 路径为 macOS 的 `/Applications/Google Chrome.app/Contents/MacOS/Google Chrome`，可通过 `PUBLISHER_CHROME_PATH` 指定。`PUBLISHER_BROWSER_USE_PYTHON` 可指定装有同一锁定版本的 Python。

1. 更新工作台代码并构建前端、迁移数据库、重启本机服务。现有部署脚本会先备份数据库；若手动升级，也应先运行 `app.manage backup` 并停止本项目服务。
2. 打开“发布与设备”的“本地 Chrome 浏览器”，选择对应平台的“打开 Chrome”。每个平台有独立窗口和配置目录。
3. 在 Chrome 中本人完成登录、验证码、平台协议与授权。点击“检查账号”；编辑器和作品页不显示账号标识时，程序会在同一独立窗口临时打开首页核验，并关闭该临时页。绑定的账号标识必须与页面完整标识相同，不能用相似昵称代替。
4. 配置发布账号时选择“浏览器（本机 Chrome）”。选择已通过审核的成片并“开始准备”；浏览器核验账号，上传 MP4 与 PNG 封面，填写并回读平台文案。
5. 手动准备不点击最终发布。请核验内容并完成平台 AI 合成声明；控件、封面或视频无法确认时，工作台显示具体接管原因。准备完成后在 Chrome 手动发布，抖音打开目标作品的数据详情页，小红书打开笔记管理页并定位目标笔记，再点击“从浏览器识别”。仍可人工填写真实作品链接。
6. “审核后自动发布”默认关闭。启用现有开关后，新审核内容使用其账号绑定的通道。账号、素材哈希、文案、AI 声明和唯一发布按钮全部核验后，才允许一次最终点击。平台有额外弹窗、审核中、超时或未返回成功页面及唯一新作品链接时，保留 `VERIFYING`，由本人处理，绝不自动重复提交。
7. 在 Chrome 中打开抖音目标作品数据详情页，或小红书笔记管理中的目标卡片，在“数据复盘”选择作品，点击“从浏览器读取指标”。核验完整作品 ID、标题和账号后，只读取页面渲染的播放/阅读、点赞、评论、收藏、分享数值。抖音作品 ID 来自数据详情 URL，小红书 ID 来自该卡片的 DOM 属性；小红书无文字标签的指标按已人工核验的完整 SVG 图标哈希识别，未知图标不猜测。缺失或冲突的值保持 `null`，真实 `0` 保留；不会从标题数字推测指标。页面不能唯一定位目标作品时，需本人定位后重试。

本次接入不扩大到竞品批量采集、全站爬取或图文笔记生成。平台页面结构发生变化、账号信息不在页面中、封面上传入口不明确、上传后文件输入被清空等情况会停止自动操作并保留人工路径。上传或提交前每个独立窗口只保留一个对应平台标签页；只读账号检查允许多个标签，抖音指标读取可从列表与详情两个标签中选定唯一目标详情。

## 状态、隐私与恢复

- `GET /api/v1/browser/status` 只读安装状态，不启动 Chrome；`POST /browser/{platform}/open` 打开窗口，`POST /browser/{platform}/inspect` 核验当前账号。
- `Account.channel` 新增 `android | browser`，旧数据默认 `android`；浏览器的保留资源使用 `browser:douyin` / `browser:xiaohongshu`，客户端不需要填写设备序列号。`Publication.channel` 来自审核发布快照；指标 `source` 新增 `browser`。已发布作品采集使用账号当前选定通道，发布历史仍保留原通道，因此 Android 发布的旧作品也可切换账号通道后用 Chrome 读取指标。
- 继续复用发布和指标 API、同一设备队列和跨进程 UI 锁，防止检查、上传、提交和指标任务互相切页。有待处理发布时，不允许替换账号绑定。
- 浏览器登录态仅保存在 `data/browser/<platform>/`，目录权限为 `0700`。不会接管日常 Chrome 个人配置，不把密码、Cookie 或完整页面转存数据库和任务日志。浏览器目录不进入现有只备份数据库与登记素材的备份包。
- Chrome 调试端口随机分配，仅连接 `127.0.0.1`；连接后核对启动参数中的本项目配置目录。运行时关闭 Browser Use 遥测、云同步、默认扩展；不继承模型密钥。关闭独立窗口即可结束该浏览器会话，再打开会复用本地登录态。
- 最终点击前，后端事务先保存 `submit_started_at`。崩溃、取消、超时或响应丢失都不抹除该证据；恢复任务不能再次提交。确认未发布后，按已有取消核验流程处理。

## 数据库迁移

Alembic `0004` 增加一个带默认值的字段（SQLite 元数据变更，无全表数据重写、无新增索引）：

```sql
ALTER TABLE accounts ADD COLUMN channel VARCHAR(20) NOT NULL DEFAULT 'android';
```

旧账号保持原 Android 行为，旧发布快照不需要回填。`0004` 的降级在存在浏览器账号或浏览器发布快照时拒绝执行；曾经创建过浏览器任务时，应停止服务并恢复升级前已校验备份，而不是让旧版 worker 处理浏览器任务。迁移验证包括旧数据默认值、前向升级、无浏览器记录时降级和有浏览器记录时拒绝降级。

## 验证入口与边界

```sh
cd backend
.venv/bin/pytest -q tests/test_browser_publishing.py tests/test_publishing.py tests/test_api.py tests/test_worker.py tests/test_v2_migration.py
cd ..
.tools/browser-use/.venv/bin/python scripts/browser_use/smoke.py
cd frontend
pnpm typecheck
pnpm test:e2e tests/e2e/browser-publication.spec.ts
```

`smoke.py` 启动临时 Chrome 配置和本机 HTTP 测试页，真实执行 Actor 的填写、文件选择、SHA-256 回读、指标解析和本地按钮点击，结束后关闭该测试浏览器。该测试不登录或发布平台内容，测试文件不是有效媒体成片。自动化测试与平台入口打开成功都不等于真实账号发布认证；真实上传与最终发布仍需具体作品授权；这些测试不代表双平台真实发帖全链路已认证。

参考：[browser-use 官方代码](https://github.com/browser-use/browser-use)、[Actor 接口](https://github.com/browser-use/browser-use/tree/main/browser_use/actor)。

## 本次本机验收（2026-09-21 UTC）

- browser-use 0.13.10 已在独立环境安装；两个独立 Chrome 已由本人登录，并核验账号与原绑定一致。
- 真实只读采集：抖音《AI智能体为何会钻规则空子？》播放 35、点赞 1，评论/收藏/分享为 0；小红书《AI界30位大佬，被国王叫去开会》阅读 15，评论/点赞/收藏/分享为 0。均从同一作品区域读取，并核验标题、作品 ID、账号；数值是该时刻快照。未将核验数据写入正式复盘记录。
- 已只读核验两个平台当前视频上传入口，小红书扩展名式文件选择已适配；真实平台未上传测试素材、未点击发布；本地测试页已实际验证填写与回读、文件选择及哈希、AI 声明读取、指标解析。
- 原账号默认保持 Android；在账号设置中改为浏览器通道后，新准备任务和指标采集才使用 Chrome。

最终代码验证：69 项后端定向回归、6 项前端浏览器回归、前端类型检查与构建通过；本地 Chrome Actor 测试通过。数据库迁移至 `0004` 前，备份已验证（122 个登记素材，升级前 schema `0003`）。重启后健康接口、运行时状态以及两个平台的真实账号检查均成功。
