# Nagi 开发交接：迁移 1 / 2 / 4

日期：2026-10-28  
当前分支：`feat/nagi-modules-234`  
当前提交：`c1e2a39 refactor: remove desktop ambient awareness`  
给 Open 5.5 的说明：先读本文，再动代码。不要把仓库当成空白项目重写。

---

## 1. 项目现状

Nagi 是一个本地优先的 AI 伴侣守护进程：

- Python 3.12 / FastAPI 网关
- PostgreSQL + SQLAlchemy + Alembic
- 原生 JS 客户端（由网关直接提供）
- OpenAI-compatible 模型 Provider
- 本地 TTS / ASR / 实时通话
- QQ 通过 SnowLuma / OneBot 接入
- 记忆、人格、主动消息、额度、CLI

当前代码已从 `agi_wind` 重命名为 `nagi`，包路径是 `app/src/nagi`。仓库目录名仍叫 `AGI.Wind`，这是历史目录，**不要**再恢复旧包名。

当前分支比 `main` 多 5 个提交：

```text
c1e2a39 refactor: remove desktop ambient awareness
8191401 feat(ui): expose purpose and token quota settings in Nagi client
2c81fd6 feat: rename to Nagi and add purpose, quota, memory cards, dual QQ
6b26b39 feat: give the model an authoritative clock and conversation timeline
feff8b1 feat: clean up companion UI and add memory retrieval features
```

基线：

- Python：`682 passed, 3 skipped`（移除桌面环境感知后）
- Node：`test-realtime-call-controller` 9/9，`test-reply-segments` 3/3
- 分支尚未 push，也没有部署到 NAS；NAS 上仍是旧镜像
- 仓库目前没有 LICENSE 文件；对外分发前必须先选许可证

---

## 2. 架构地图

```text
app/src/nagi/
  api/            FastAPI 应用、路由、客户端静态资源
  agent/          工具调用、审批、AgentRunner
  archive/        归档、导出、删除账本
  core/           设置、数据库、事件、类型
  delivery/       生成、分段、语音输出、主动消息、空闲认知
  identity/       人格、身份、人物档案
  integrations/   QQ / SnowLuma / OneBot
  jobs/           后台任务
  memory/         记忆抽取、召回、卡片、自传、合并
  providers/      OpenAI-compatible Provider
  purpose/        主动行为、目的、对话收尾
  usage/          额度、用量
  voice/          Breeze TTS、ASR、通话
```

关键入口：

- `app/src/nagi/api/app.py`：`create_app()` / `from_env()`，组装所有服务、启动后台任务、安装路由。
- `app/src/nagi/delivery/service.py`：`GenerationService.messages()` 组装系统提示词；`run()` 生成并落库。
- `app/src/nagi/memory/models.py`：记忆表、卡片、归档。
- `app/src/nagi/memory/cards.py`：`MemoryCardService`。
- `app/src/nagi/memory/consolidation.py`：每日/每周摘要。
- `app/src/nagi/memory/autobiography.py`：自传整理（preview → confirm → undo）。
- `app/src/nagi/core/settings.py`：`Settings` 及所有子设置。
- `app/src/nagi/integrations/qq.py`：QQ 收发、OneBot、多账号、命令。
- `app/migrations/versions/0020_drop_ambient_observations.py`：当前最新迁移。

客户端：

- `app/src/nagi/api/client/index.html`
- `app/src/nagi/api/client/app.js`
- 原生 JS，没有构建步骤，不要引入 React/Vue/Electron。

---

## 3. 本次要做的事

用户明确选择迁移 **1 / 2 / 4**：

1. **Todo / 提醒 + Session Dream**
2. **时序记忆（Graphiti 的思路，不是把 Graphiti 装进来）**
4. **Letta / Mem0 式记忆抽取与整理管线**

明确不做：

- 第 3 项 Live2D / 低延迟语音：用户已说“三去掉”
- SurfCore 整体框架替换
- B 站、桌宠、游戏 Agent
- NAS 部署（用户只允许本地先做，等明确指令再同步）
- 任何 AOCI 文件的修改

---

## 4. 外部项目审计结果

### 4.1 qq-maid-bot（MIT，Rust）

仓库：https://github.com/kuliantnt/qq-maid-bot

值得拿的：

- Todo / 提醒 / 每日摘要
- RSS / Atom 订阅
- 天气、列车、网页查询
- 个人记忆 / 群内个人画像 / 群公共记忆分域
- `MEMORY_CONSOLIDATION_ENABLED` 与 `MEMORY_DREAM_ENABLED` 分开开关
- Dream 只提取安全长期事实，不覆盖已确认记忆

冲突：

- 它是 Rust + SQLite；Nagi 是 Python + PostgreSQL
- 不能直接复制代码；只按行为规格重写
- 它的模型路由 / QQ 网关与 Nagi 现有实现重叠，不要搬整套

### 4.2 Graphiti（Apache-2.0，Python）

仓库：https://github.com/getzep/graphiti

值得拿的：

- 事实有 `valid_from` / `valid_to`
- 新事实让旧事实失效，而不是删除旧事实
- 每条事实能追溯到产生它的 episode
- 语义 + 关键词 + 图遍历混合检索

冲突：

- Graphiti 默认需要图数据库（Neo4j / FalkorDB / Kuzu）
- Nagi 现在是 PostgreSQL + usearch，没有图数据库
- 不要引入 Neo4j；只在现有 PostgreSQL 记忆表上实现“有效期 + 替代关系”
- 不复制 Graphiti 代码，只实现等价语义

### 4.3 Letta / Mem0（Apache-2.0）

- Letta 当前源码已迁移到 https://github.com/letta-ai/letta-code，TypeScript / Bun；旧的 `letta-ai/letta` 仓库只剩归档说明。
- Mem0：https://github.com/mem0ai/mem0，Python，Apache-2.0。

值得拿的：

- Memory Blocks：把“常驻摘要”与“长期事实”分开
- 后台 sleep-time 整理：不阻塞聊天，在空闲时合并/纠错
- Mem0 的“抽取 → 比较 → 新增/更新/失效/忽略”流程

冲突：

- Letta 现在是 TS/Bun 运行时，不是可嵌 Python 库
- Mem0 的默认依赖包含 qdrant-client、openai、posthog，且自带向量库抽象；直接安装会和 Nagi 的 usearch / Provider 层重复
- 不新增 `letta`、`mem0ai` 依赖；把流程重写成 `nagi.memory` 内的纯服务

### 4.4 许可证结论

- MIT / Apache-2.0：可以参考实现，复制代码时必须保留版权与许可声明。
- GPL / AGPL：只能参考思路，不要复制代码。
- Live2D Cubism SDK / 模型：专有许可，本次不碰。
- 本项目目前无 LICENSE；需要用户决定后再分发。

---

## 5. 先查重，再开发

Nagi 已经有这些能力，不要重复造：

- `memory/consolidation.py`：每日/每周摘要
- `memory/autobiography.py`：自传整理
- `memory/cards.py`：记忆卡片
- `memory/external.py`：外部 embedding 适配器
- `memory/rerank.py`：重排
- `memory/extraction.py`：记忆抽取
- `usage/`：额度
- `purpose/`：主动消息、对话收尾
- `delivery/idle_cognition.py`：空闲认知

真正缺的是：

1. **Todo / reminder 领域**：没有表、没有工具、没有 QQ 命令、没有到点投递。
2. **记忆有效期**：现有记忆是“当前状态”，没有 `valid_to` / `superseded_by`。
3. **Session Dream**：有摘要，但没有“从会话提取安全长期事实、候选确认、不覆盖已确认记忆”的独立流程。
4. **Memory Blocks 抽象**：有卡片，但没有“常驻块 / 长期事实 / 工作记忆”的明确分层。

---

## 6. 开发任务

### 任务 A：Todo + Reminder

新增：

- `app/src/nagi/todos/models.py`
- `app/src/nagi/todos/service.py`
- `app/src/nagi/todos/scheduler.py`
- `app/src/nagi/api/todo_routes.py`
- `app/migrations/versions/0021_todos.py`

建议表：

```text
todo_items
  id uuid pk
  account_id uuid
  companion_id uuid
  scope            owner | group
  group_id         text null
  conversation_id  uuid null
  title            text
  notes            text
  status           open | done | cancelled
  priority         int
  due_at           timestamptz null
  timezone         text
  recurrence_rule  text null   -- RRULE 或受限 JSON
  source_event_id  uuid null
  created_at / updated_at / completed_at

todo_reminders
  id uuid pk
  todo_id uuid fk
  fire_at timestamptz
  channel          web | qq
  status           pending | sent | failed | cancelled
  dedupe_key       text unique
  sent_at          timestamptz null
```

要求：

- 时区用 `Settings.timezone`，禁止用服务器本地时间猜。
- 重复提醒支持：每天 / 每周 / 每月 / 自定义 RRULE 子集；先做每天、每周、每月。
- 到点投递复用现有 QQ 发送与 Web 事件通道；不要在 scheduler 里直接拼 OneBot payload。
- 群待办只允许群主/管理员操作；私聊待办只允许 owner。
- 幂等：同一 `dedupe_key` 只能投递一次。
- 模型工具：`todo_create` / `todo_list` / `todo_update` / `todo_complete` / `todo_snooze` / `todo_delete`。
- QQ 命令：`/todo`、`/todo add`、`/todo done`、`/todo list`，可选；命令解析不要依赖模型。
- API：`GET/POST/PATCH/DELETE /api/v1/todos`，`POST /api/v1/todos/{id}/complete`。
- 客户端：设置页或侧栏加一个最小 Todo 面板即可，不要求大改 UI。

验收：

- 建、改、完成、取消、删除。
- 单次提醒准时投递；重复提醒按规则生成下一次。
- 群权限隔离、时区、幂等、重启恢复。
- 至少 15 个单元测试 + 1 个集成测试。

### 任务 B：Session Dream

新增：

- `app/src/nagi/memory/dream.py`
- 在 `jobs/` 或 `delivery/idle_cognition.py` 挂后台任务
- `Settings` 增加 `memory_dream_enabled`、`memory_dream_interval_hours`、`memory_dream_max_candidates`

要求：

- 只在空闲时段运行，不阻塞聊天。
- 输入：最近会话事件；输出：候选事实（人物、偏好、关系、计划、边界）。
- 先写候选，不直接覆盖已确认记忆；候选可确认/拒绝。
- 去重：与现有 `memory_items` 做语义相似 + 原文比对。
- 每个候选保留 `source_event_id`、`confidence`、`created_at`。
- 不做“人格改写”；人格只由用户显式编辑。
- 失败要静默重试，不能影响正常回复。

验收：

- 能从一段对话生成候选。
- 不覆盖已确认记忆。
- 重复运行不产生重复候选。
- 关闭开关后完全不运行。

### 任务 C：时序记忆

在现有 `memory_items` 上增加列，不新建图数据库：

```text
valid_from       timestamptz
valid_to         timestamptz null
superseded_by    uuid null
source_event_id  uuid null
```

要求：

- 默认召回只看 `valid_to is null`。
- 新事实与旧事实冲突时：旧事实 `valid_to=now`，新事实 `superseded_by` 指向旧事实，或反之；禁止物理删除。
- 记忆卡片显示“当前有效”；加一个“历史”抽屉显示失效事实。
- 自传/摘要必须能读到失效事实，但标注时间范围。
- 迁移时给已有行 `valid_from=created_at`，`valid_to=null`。

验收：

- 用户改偏好后，旧偏好不再进入当前上下文。
- 仍能查询“以前是什么样”。
- 卡片不重复、不丢来源。

### 任务 D：记忆抽取/整理管线（Mem0/Letta 思路）

新增：

- `app/src/nagi/memory/refinement.py`

流程：

```text
recent events
  -> extract candidates
  -> compare with active facts
  -> ADD | UPDATE | INVALIDATE | NOOP
  -> persist with source_event_id
  -> optional async compaction
```

要求：

- 纯 Python，不调用 Mem0/Letta 服务，不新增 qdrant/neo4j 依赖。
- 复用现有 Provider 的 `memory_extract` 角色；超时/失败降级为 NOOP。
- 每轮最多处理 N 条，受 `usage` 额度控制。
- 所有写操作带来源；禁止无来源记忆。
- 与 `memory/consolidation.py` 分工：consolidation 做摘要，refinement 做事实级变更。

---

## 7. 集成点

- 设置：`app/src/nagi/core/settings.py`，新增 `TodoSettings` / `MemoryDreamSettings`，并更新 PATCH/序列化测试。
- 路由：`api/app.py` 里 `install_*_routes()`。
- 模型工具：`agent/` 的 ToolSpec 注册；工具必须做权限与参数校验。
- 提示词：`delivery/service.py` 的 `messages()`；不要把整份 Todo 列表塞进去，只注入“今天到期/最近提醒”。
- QQ：`integrations/qq.py`，复用现有发送队列、分段、限速；不要绕开。
- 客户端：`api/client/index.html` + `app.js`，原生 JS；沿用现有 fetch/鉴权，不引入构建工具。
- 数据库：新增迁移 0021；不要改 0019/0020。

---

## 8. 运行与测试

```powershell
cd C:\Users\wind\Desktop\AGI.Wind\app
.\.venv\Scripts\python.exe -m pytest tests -q
.\.venv\Scripts\python.exe -m pytest tests/unit/test_xxx.py -q
.\.venv\Scripts\python.exe -m alembic upgrade head
```

本地启动：

```powershell
cd C:\Users\wind\Desktop\AGI.Wind
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\start-local.ps1 -BindHost 127.0.0.1
```

Node 回归：

```powershell
node scripts\test-reply-segments.mjs
node scripts\test-realtime-call-controller.mjs
```

提交前必须：

- `pytest` 全绿（允许原有 3 skip）
- 新迁移可升级、可降级
- `git status` 只包含预期文件
- 不提交 `data/`、AOCI、日志、密钥
- README 当前是乱码，属于已知问题；如果时间允许顺手重写，不要用它当功能依据

---

## 9. 给 Open 5.5 的执行顺序

1. 先读 `app/src/nagi/memory/` 与 `purpose/`，确认不重复实现。
2. 做任务 A（Todo/reminder），独立迁移 0021，先跑通测试。
3. 做任务 C（时序记忆），只改 `memory_items` 与召回过滤。
4. 做任务 B（Session Dream），依赖 C 的有效期字段。
5. 做任务 D（refinement），最后接入空闲任务与额度。
6. 每步单独提交，避免一个巨大 commit。
7. 不 push main，不部署 NAS；完成后由用户决定合并/部署。

---

## 10. 禁止事项

- 不复制 GPL/AGPL 代码（MaiBot、Moui、AstrBot、SillyTavern）。
- 不引入 Live2D Cubism SDK 或模型。
- 不引入 Electron/React/Vue；客户端保持原生 JS。
- 不引入 Neo4j / Qdrant / Redis；PostgreSQL + 现有 usearch 足够。
- 不修改 `data/` 下的真实数据库和密钥。
- 不修改 `aoci.txt` / `aoci.meta.txt` / `aoci.code.txt` / `.aoci/`。
- 不直接改 NAS 上的 `/vol2/agi-wind`；本地完成并通过测试后再谈部署。
- 不把 Todo、记忆原文、QQ 会话发给第三方模型之外的服务。

---

## 11. 参考

- qq-maid-bot：https://github.com/kuliantnt/qq-maid-bot（MIT）
- Graphiti：https://github.com/getzep/graphiti（Apache-2.0）
- Letta current：https://github.com/letta-ai/letta-code（Apache-2.0）
- Mem0：https://github.com/mem0ai/mem0（Apache-2.0）
- Nagi 记忆现状：`app/src/nagi/memory/README.md`（如存在）与 `cards.py` / `consolidation.py`
