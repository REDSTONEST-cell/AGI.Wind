# AGI.Wind 开发记录（接手至 2026-10-25）

> **v7 更新**：下文保留 v6 交接时的历史快照。图片输入、两阶段图片回合、真实工具/思考轨迹与表情包库现已实现；当前验证为 99 passed / 1 skipped，另启用真实 DeepSeek 合成图片测试 1 passed。新增“＋”上传、剪贴板粘贴、扩大聊天/头像、去除侧栏品牌、可拖日志栏；详细证据与边界见 `docs/ops/progress.md`。下文“尚无图片/思维链”和 82 项测试属于更新前状态。

> 状态：Windows 本地可运行基础版。本文记录接手后实际做了什么、为什么这样做、当前怎么运行和交接，不是初始方案，也不把未完成能力写成已完成。
>
> 更新：2026-10-25，Asia/Shanghai。下一阶段设计见 [思维链、图片收发与表情包库开发计划](../superpowers/plans/2026-10-25-reasoning-images-stickers-plan.md)。

## 1. 一页摘要

当前交付的是一个本地优先的 AI 陪伴原型：FastAPI Gateway、PostgreSQL 原文档案、无构建 ES module 浏览器客户端。它不是完整 AI 伴侣；语音、向量、Live2D、声音训练、NAS 部署和本次新增的思维链/图片/表情包仍未完成。

| 项目 | 当前状态 |
|---|---|
| Gateway | 运行于 `http://127.0.0.1:18440`，`/health/ready` 为 ready |
| PostgreSQL | 17.6，`127.0.0.1:55432`，业务库与测试库分离 |
| 客户端 | `#/chat` 等七个页面，控制台式布局，浅色/深色/跟随系统 |
| 对话模型 | DeepSeek `deepseek-flash`，`direct_vendor`，不走 Letta |
| 视觉模型 | 当前未单独配置；新功能计划加入对话模型回退 |
| 自动化验证 | 82 项 Python 测试通过；v6 无头 Chromium 0 JS 报错 |
| AOCI | 代码认知层已维护并对齐；本次文档新增后需再次维护 |
| Git | 已初始化，分支 `main`；最近提交 `174db2f`，工作区有未提交改动 |

本文与以下文档分工：

- [初始交接](../../Codex开发交接.md)：用户约束、最终验收目标和最初的实施边界。
- [实施进度](../ops/progress.md)：逐任务状态、决策记录和待输入，是执行账本。
- [运维说明](../ops/runbook.md)：日常启动、停止、配置、API 和验证命令。
- [验收记录](../ops/acceptance-report.md)：已验证项与 A01—A20 的边界。
- 本文：接手后的实际开发叙事，解释系统为什么长成现在这样，以及下一任如何继续。

## 2. 接手时的起点

接手时项目只有开发设计、实施计划和目录边界，没有可用的正式客户端、目标服务器连接、厂商凭据、正式人格或声音素材。用户确认的工程约束是：

- 聊天与看图走厂商 API；语音、向量等后续可本地运行。
- 权威数据必须独立于 Letta/EverOS：原始经历、人格、媒体和来源可导出、可迁移。
- 换模型不能重建身份或丢记忆；向量和运行时历史只能是派生数据。
- 先在本机部署测试，通过后再考虑 NAS。
- 不伪造未验证能力；缺凭据时明确报错，不假装模型可用。
- 不复制来源不明或许可证不兼容的代码；视觉和交互参考只借鉴设计。

具体到本地环境：Windows 11、Python 3.12、Node 24、PostgreSQL 便携版、工作目录 `C:/Users/wind/Desktop/AGI.Wind`。业务代码放在 `app/`，数据、密钥、数据库、依赖环境与源码分离。

## 3. 已交付里程碑

时间均为 2026-10-25；下面按实际交付顺序记录，不把同一天的连续迭代编造成多个日期。

### 3.1 基础服务端与档案

- 建立 `app/` 业务根、FastAPI 应用、SQLAlchemy 模型、Alembic 迁移和测试骨架。
- 实现原文事件归档、请求幂等、媒体内容寻址、人格版本、账号/角色/会话 scope。
- 建立 PostgreSQL 真实集成测试，使用随机临时 schema，不用 SQLite 替代。
- 资产按 SHA-256 去重；`asset_owners` 控制跨账号读取，`asset_refs` 记录事件与媒体用途。
- 打通导出、校验、导入、删除账本和媒体隔离的基础链路。

### 3.2 身份、人格与记忆

- 稳定 `companion_id`，人格快照与版本化更新；换模型不影响角色身份。
- 关系、承诺、来源引用进入档案模型。
- 中文关键词与精确子串检索，不把关键词检索冒称“向量语义检索”。
- 记忆纠错保留历史，来源不足的自动事实进入隔离流程。
- 上下文预算从写死值改为可配置字节上界，修复 `CONTEXT_TOO_LONG` 与误导性失败文案。

### 3.3 生成、流式与浏览器会话

- OpenAI-compatible 适配器、视觉观察适配器、ProviderRegistry 热加载。
- 生成请求先事务落盘，再异步执行；`request_id` 重试复用同一任务，不重复付费。
- SSE 事件持久化，支持 `Last-Event-ID` 重放与 `generation.completed` 终态。
- 打断使用幂等 `command_id` 与 epoch 校验；旧流不能覆盖新状态。
- 浏览器鉴权采用 Bearer → 一次性票据 → HttpOnly + SameSite=Strict cookie + CSRF；长期令牌不落 localStorage。
- 本机回环自动登录，同时校验来源与 Host，防 DNS rebinding。
- 服务器/客户端分离：客户端密钥、Bearer、CORS 白名单、局域网启动脚本。

### 3.4 客户端 v5：单会话与归档

- 每个角色最多一段 `active` 对话；新建对话自动归档旧对话。
- 恢复旧对话会自动归档当前 active；归档会话只读，聊天返回 `CONVERSATION_NOT_ACTIVE`。
- 归档页支持查看、恢复、隐藏、显示已隐藏；删除仅从界面隐藏，不物理删除原文。
- 「⋯」菜单在页面内完成重命名/归档/删除，不使用浏览器原生 `prompt/confirm`。
- 模型设置按六种能力分 Tab；只有对话与识图接入真实适配器，其余明确标注“待接入”。
- 连接页支持同源 cookie 和独立客户端 Bearer 两种形态。

### 3.5 客户端 v6：控制台式界面与系统日志

- 按 qq-bridge（MIT）的视觉语言重做：234px 分组侧栏、76px 顶栏、页面标题、指标条、卡片/表格/状态胶囊/粘性保存栏、浅色/深色/跟随系统。
- 七个独立页面与 `#/chat` 哈希路由；浏览器前进后退可用。
- 对话页改成单一聊天窗口；移除对话页指标行和输入栏旁说明文字。
- 灵魂档案移除版本显示，保留字段/体积/所属角色指标。
- 右侧系统日志抽屉：深色终端、Gateway stdout/stderr + 客户端活动混排、全部/系统/活动/错误筛选、跟随/暂停、清空、移动端全屏。
- 系统日志新增认证的增量读取接口，服务端脱敏凭据、数据库密码和 Bearer/赋值形态敏感串。
- 头像上传从“脚本点击隐藏 input”改为原生 file input 覆盖层，修复部分浏览器拦截合成点击的问题。
- 终端日志只在有新行时重绘，去掉逐行动画和光标闪烁，修复每 2 秒整块闪烁。

### 3.6 验证与交付状态

- 82 项 Python 测试通过。
- 无头 Chromium 在 1440 / 1024 / 390 三档验证七页路由、归档、模型页、主题、系统日志、头像上传，0 JS 报错。
- 本机自动登录、单活跃会话、客户端密钥、CSRF、CORS、系统日志脱敏与增量偏移均有测试或实测证据。
- AOCI 在最后一次代码改动后完成对齐；本次新增文档会改变受管理对象，收尾时重新维护。
- 当前不是完整 T01—T15 验收；语音、向量、重排序、Live2D、声音训练、NAS 仍待后续。

## 4. 当前架构

```text
Browser (vanilla ES modules, no build)
  ├─ hash router: #/chat #/archive #/soul #/memory #/model #/connection #/appearance
  ├─ same-origin cookie + CSRF, or cross-origin Bearer client key
  ├─ SSE: /api/v1/generations/{id}/events
  └─ system logs: /api/v1/system-logs (tail + offsets)
          │
          ▼
FastAPI Gateway (127.0.0.1:18440)
  ├─ auth: loopback auto-session, key-session, Bearer ticket
  ├─ API: companions/conversations/chat/persona/memory/settings/assets/archive/logs
  ├─ GenerationService: single-flight, SSE, interrupt, provider registry
  ├─ ArchiveService: events, assets, asset_refs, idempotency
  ├─ ContextBuilder: persona + recent events + keyword memory + vision observation
  └─ System log reader: data/gateway.stdout.log / gateway.stderr.log (redacted)
          │
          ├─ PostgreSQL 17.6 :55432
          │    accounts/users/companions/conversations/requests/generations/events
          │    assets/asset_owners/asset_refs/persona_versions/memories/outbox/jobs
          └─ Provider API (DeepSeek OpenAI-compatible, SSE)
```

### 4.1 数据权威边界

- PostgreSQL 与 `data/assets` 是权威档案。
- 向量索引、运行时历史、浏览器本地缓存都是可重建投影。
- 归档格式 v2（当前）包含原文、人格、媒体引用和删除账本；旧包按兼容规则导入。
- 删除事件会写删除账本，阻止旧备份复活已删内容；缺最新账本时只能隔离恢复。

### 4.2 认证边界

- 同源浏览器：回环访问自动建立 HttpOnly 会话；非回环访问使用客户端密钥换 cookie。
- 同源写请求必须带 `Origin` 和 `X-AGI-CSRF`。
- 独立客户端：每请求 `Authorization: Bearer <client_key>`，服务端只放行 `allowed_origins`。
- 客户端密钥默认值只用于内网调试；正式部署必须替换并启用 HTTPS。

## 5. 关键业务流程

### 5.1 一条消息如何落盘与流式返回

1. 客户端生成 `request_id`，POST `/api/v1/chat/requests`。
2. Gateway 在数据库事务中校验会话、附件所有权、幂等键，写入 request/user event/generation/outbox。
3. Worker 在单飞锁下调用 Provider，文本增量写 `generation_stream_events`，同时累加 assistant event content。
4. 客户端消费 SSE，只有真正收到 `data.payload.text` 才追加气泡；这是之前流式不显示的根因。
5. 完成后 assistant event 状态为 complete；历史接口按时间回看；重连通过 `Last-Event-ID` 重放。

### 5.2 单活跃会话

- 每个 companion 最多一个 active；创建新对话在同一 companion 锁内归档旧对话。
- 归档会话的 events 只读；恢复旧对话会归档当前 active。
- 隐藏只影响列表；原文不硬删除。

### 5.3 系统日志

- 启动脚本把 Gateway stdout/stderr 重定向到 `data/gateway.*.log`。
- `/api/v1/system-logs` 按字节 offset 读取增量，响应前脱敏。
- 客户端把 Gateway 行和页面活动行混排到终端抽屉；只在新行/筛选变化时重绘。

### 5.4 头像与媒体

- 上传走 `POST /api/v1/assets`，服务端用 Pillow 验证 MIME/尺寸，内容寻址存储。
- 同源图片用 `<img src>`；跨域 Bearer 图片通过 fetch blob 显示。
- 头像选择用原生 file input 覆盖层，不依赖脚本触发隐藏 input。

## 6. 代码地图

| 路径 | 职责 |
|---|---|
| `app/src/agi_wind/api/app.py` | FastAPI 装配、认证、聊天/会话/设置/资产/归档/日志端点 |
| `app/src/agi_wind/api/auth.py`、`browser.py` | Bearer、票据、浏览器会话、CSRF |
| `app/src/agi_wind/api/logs.py` | Gateway 日志增量读取与脱敏 |
| `app/src/agi_wind/archive/` | 数据模型、事务归档、资产、导出/导入、删除账本 |
| `app/src/agi_wind/identity/` | 人格版本、身份、模型绑定 |
| `app/src/agi_wind/memory/` | 关键词检索、切片、上下文、纠错、隔离 |
| `app/src/agi_wind/providers/` | OpenAI-compatible、视觉、ProviderRegistry |
| `app/src/agi_wind/delivery/` | 生成服务、SSE 事件、中断/重放 |
| `app/src/agi_wind/api/client/` | 无构建客户端：index.html、styles.css、app.js、SDK |
| `app/migrations/versions/` | Alembic 迁移；当前 head 为 `0007_conversation_lifecycle` |
| `app/tests/` | unit/contract/integration；真实 PostgreSQL 临时 schema |
| `scripts/` | 启动/停止/状态/smoke/数据库初始化 |
| `docs/ops/` | 环境、版本、许可、运维、进度、验收 |

## 7. 关键决策与取舍

- **单活跃 + 归档**：换取明确的“当前关系”语义，代价是不能并行多会话。
- **删除=隐藏**：避免误删共同经历，代价是数据持续增长。
- **关键词检索**：先保证可解释、可重建，不冒称语义检索；向量后续接入。
- **direct_vendor**：本地开发显式使用厂商 API，不自动回退 Letta；Letta 未验收。
- **浏览器会话进程内存储**：不把长期令牌写 localStorage；Gateway 重启需重新登录。
- **客户端无构建步骤**：减少工具链和依赖，代价是复杂交互要自行维护。
- **qq-bridge 只借鉴设计**：MIT 视觉语言参考，不复制其单文件控制台 DOM/JS。
- **不伪造未接能力**：语音/向量/重排序只保存配置并标注“待接入”。
- **系统日志脱敏**：日志可读，但不能回显密钥、数据库密码或完整 prompt。

## 8. 已修复问题

| 问题 | 根因 | 处理 |
|---|---|---|
| 流式回答不显示 | SSE 数据是 StreamEvent 信封，文本在 `data.payload.text`，旧代码读 `data.text` | 修正客户端读取层级；补 SSE 测试 |
| `CONTEXT_TOO_LONG` 误导 | 预算写死且失败文案暗示“模型没返回文字” | 可配置预算 + 可操作错误提示 + 跳转灵魂档案 |
| 头像上传无反应 | 依赖脚本 `.click()` 触发隐藏 file input，部分浏览器拦截 | 原生 file input 透明覆盖层 |
| 系统日志一直闪烁 | 每 2 秒无条件 `replaceChildren()` 重建整个终端，且有入场动画 | 仅新增行/重置/筛选时重绘；去掉逐行动画与光标闪烁 |
| 日志抽屉压住顶栏 | 抽屉与顶栏层叠/收缩关系不清 | 顶栏移出 workspace 保持全宽；抽屉从 76px 下方开始 |
| 移动端日志不可用 | 桌面并排布局直接沿用 | ≤760px 全屏抽屉 + scrim；≤1180px 覆盖式面板 |

## 9. 验证与证据

- **后端**：82 项 pytest 通过；数据库集成使用真实 PostgreSQL 随机临时 schema。
- **客户端 v6**：1440 / 1024 / 390 三档，七页路由、归档、模型页、主题、日志抽屉、头像上传均通过，0 JS 报错。
- **日志**：终端打开静置 4.6 秒 DOM 变更 0 次（防闪烁验证）；增量 offset 与脱敏有后端测试。
- **头像**：真实鼠标点击 + 原生文件选择验证上传成功、预览出现、提示出现。
- **AOCI**：代码认知层已对齐；文档修改后需重新维护，不能沿用旧结论。
- 截图：`.devtools/v6-chat-light.png`、`v6-log-chat.png`、`v6-log-drawer.png`、`v6-soul.png`、`v6-mobile-log.png`、`v6-avatar-upload.png`。

## 10. 运行与交接操作

### 启动与停止

```powershell
.\scripts\start-local.ps1                 # 本机 127.0.0.1:18440
.\scripts\start-local.ps1 -BindHost 0.0.0.0   # 局域网
.\scripts\status-local.ps1
.\scripts\stop-local.ps1 -StopDatabase
```

浏览器打开 `http://127.0.0.1:18440/`；状态页是 `/status`，机器健康接口是 `/health/ready`。静态文件改动后 **Ctrl+F5**；后端改动需重启 Gateway。

### 测试

```powershell
cd app
..\app\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider
```

真实厂商、语音、向量和 NAS 不在自动验证范围；未配置模型时聊天明确返回 `MODEL_UNCONFIGURED`，不会伪造回答。

### 数据安全

- 不把 `data/`、`.venv`、数据库目录、密钥或 `.devtools` 提交。
- 不在验证中归档/删除用户当前对话；UI 检查使用 mock 或只读页面。
- 需要断线重连、归档恢复等破坏性操作时，明确选择合成 schema 或克隆环境。
- 修改受管理对象后，按仓库 AOCI 流程维护并验证；不要手改索引正文。

## 11. 已知边界

- Letta、EverOS、pgvector/BGE、ASR/TTS、重排序、Live2D、声音训练均未验收。
- NAS 尚未安装；仅做过只读环境核实。
- 系统日志依赖启动脚本的 stdout/stderr 重定向；手动启动时文件可能为空。
- 当前没有图片输入、思维链、表情包库或图片回复；这些是下一阶段。
- 不是完整 AI 伴侣，也不是 T01—T15 全量完成状态。

## 12. 接手检查清单

1. 先读本文 → `progress.md` → `runbook.md`，再按需读初始设计与计划。
2. 运行 `status-local.ps1` 和完整 pytest，确认 82 项通过；再做 UI 无头验证。
3. 确认 `git status` 有未提交改动，不要 reset/覆盖；先理解现有工作树。
4. 启动 Gateway 后打开客户端，确认 `服务就绪` 和七个页面可用。
5. 检查 `data/settings.json` 的模型配置和 `data/secrets.json` 仅被服务端读取。
6. 修改代码前确认 AOCI 当前状态；完成后维护 `aoci.code.txt` 并 `verify/check`。
7. 新功能按 [开发计划](../superpowers/plans/2026-10-25-reasoning-images-stickers-plan.md) 执行，不要直接在当前工作树上另起一套图片/表情包实现。

## 13. 下一阶段

下一阶段是“思维链 + 图片收发 + 表情包库”。该功能当前**只有设计和调研，没有实现**。其范围、开源调研、许可证判断、数据/API/事件、前端设计、测试与发布顺序见：

- [思维链、图片收发与表情包库开发计划](../superpowers/plans/2026-10-25-reasoning-images-stickers-plan.md)

在开始前，先确认三件事：

- 用户要的是“发送已有表情包/图片”，不是文生图。
- 图片回合按 DSH 式处理顺序编排：观察图片 → 调用回答 → 回答后二次思考 → 入库/模仿发送。
- “处理过程”展示真实 reasoning 与真实工具动作；只做图片回合的受控 agent，不做通用 MCP/任意工具 runtime，也不伪造未执行的步骤。
