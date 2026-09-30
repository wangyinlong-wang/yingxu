# 模型来源与密钥配置验收

本轮需求：在「模板与设置 → 模型与视频」中配置 Base URL、模型名称、API Key，并切换本地与第三方模型。

## 配置行为

- 文本、图像、语音分别选择第三方模型或本地 URL 服务；语音另可选择已有的本地声音引擎。
- 两套 URL、模型名称、启用状态及协议参数分别保留；选择来源后点击「保存设置」才生效。本地 URL 服务使用 OpenAI 兼容协议，仅接受本机回环地址。
- 「保存密钥 / 清除已保存密钥」独立操作本机 macOS 钥匙串，按数据目录、模型能力、来源及 URL 隔离。接口仅返回配置状态，普通设置不携带 API Key。
- 第三方环境变量密钥继续兼容；本地服务没有保存密钥时不携带 Authorization。保存设置或密钥不会调用模型。
- 本地声音来源与原有「后续新旁白使用本地本人声音」开关同步，继续检查已有模型和参考素材。备份恢复会禁用两套 HTTP 配置及本地声音。
- 数据字段采用现有 JSON 设置扩展，无数据库表结构迁移。旧客户端只更新本地声音开关的行为保留。

## 实现与审查

Kimi CLI 负责实现和自动化检查，主 Agent 负责契约、差异审查与浏览器验收。Grok CLI 完成只读源码和测试覆盖审查，没有独立执行测试命令。

主 Agent 与 Grok 确认的钥匙串错误回退及错误码被覆盖问题已修正：锁定、拒绝访问或暂时故障不会切换到环境变量账号，也不会新增发送记录。确认没有钥匙串条目时继续兼容环境变量；不支持 macOS 钥匙串的平台与暂时不可用分开处理。

IPv6 主机与端口已分开规范化；异步密钥结果绑定提交时的 URL，保留请求期间新输入的草稿；初次加载与保存后回读共用旧格式归一化；390px 密钥区域改为完整卡片宽度。主 Agent 复核源码与实际画面，未发现阻止本轮功能交付的问题。

两套配置复用一个端点字段定义，保留各自协议与 URL 校验；未引入提供商框架或新依赖。临时测量脚本已移除，有维护价值的回归测试保留。

## 最终验证

以下为集中修正后同一份代码的实际结果，由 Kimi 执行并由主 Agent 核对日志与差异。

| 检查 | 结果 |
| --- | --- |
| 后端定向回归 | 158 passed，exit 0；覆盖密钥、设置、provider、设备凭据、API、本地声音、备份和工作流 |
| Ruff（本轮修正 Python 文件） | All checks passed，exit 0 |
| 前端 `pnpm typecheck` | exit 0 |
| 前端 `pnpm test:unit` | 3 个文件，11/11 通过 |
| 前端 `pnpm build` | exit 0；保留既有大于 500 kB 的包体提示，未降低检查阈值 |
| 模型配置、operations、local-ai 端到端用例 | 16/16 通过，exit 0；包括慢响应、草稿保护、旧格式及 390px 对齐 |
| 离线 OpenAPI | JSON 有效；新增专用密钥接口，API Key 字段标为 password / writeOnly |
| 差异与简化审查 | 无空白错误；原有页面改版修改保留 |

后端命令（工作目录 `backend`）：

```sh
.venv/bin/python -m pytest tests/test_model_credentials.py tests/test_model_settings.py tests/test_providers.py tests/test_device_credentials.py tests/test_api.py tests/test_local_ai.py tests/test_maintenance.py tests/test_workflow.py -q
.venv/bin/python -m ruff check app/model_credentials.py app/schemas.py app/api.py tests/test_model_credentials.py tests/test_model_settings.py
```

端到端命令（工作目录 `frontend`）：

```sh
pnpm exec playwright test tests/e2e/model-settings.spec.ts tests/e2e/operations.spec.ts tests/e2e/local-ai.spec.ts
```

验证输出保留在 `/private/tmp/yingxu-model-settings-20260920/rework-backend-validation.log`、`rework-frontend-validation.log`、`rework-e2e-validation.log`。Grok 的静态审查为同目录的 `grok-review.txt`。

## 本地运行与浏览器验收

- 重启前确认 QUEUED / RUNNING 任务均为 0，定时计划为 0；通过项目控制脚本只重启已登记的本地服务，退出 0。
- 实际健康接口返回 `status=ok`；新密钥接口已出现在运行中的 OpenAPI；GET 设置返回两套配置及密钥状态，不返回密钥值。
- 既有文本、图像第三方配置保留，既有本地声音选择映射为 builtin；没有修改用户的模型配置或 API Key。
- 实际入口 `http://127.0.0.1:8765/setup?tab=settings` 已显示新表单；只切换页面草稿确认两套字段独立且切回后保留原参数，没有提交真实配置。
- 桌面和 390px 画面已复查。证据为临时目录中的 `models-live-desktop.png`、`models-mobile-after.png`。重启后核对 13 个相关源码及构建文件哈希，无变化。
- 当前 8765 页面未捕获到 console warning/error。开发服务曾出现既有 `ElOption.value=null` 提示（HEAD 已有该选项定义），本轮未修改此旧选项行为。

## 验证边界

自动化测试使用临时数据库、内存凭据后端与模拟 HTTP 响应。没有保存真实 API Key、调用实际模型、下载模型或触发内容发布。

Grok 的独立终端测试仍为 **BLOCK**：此前后台终端命令因权限确认取消，本轮完成只读审查，不能计为独立跑测。Third-Party CLI 技能 要求 “neither the worker nor the lead agent may force it through”，因此没有修改 CLI 权限或使用绕过参数。解除条件是通过 Grok 的正常权限流程授权相关测试命令后执行。真实钥匙串保存授权与具体服务商可用性未使用真实密钥验证。

本轮不执行 Git 暂存、提交或推送。原有页面改版的未提交内容保留。
