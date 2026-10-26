# 思维链、图片收发与表情包库——开发设计与实施计划（v7）

> 状态：**v7 已实施并通过本地验证**。99 passed / 1 skipped，另真实 DeepSeek 合成图片测试 1 passed；无头 Chromium 三档通过。详细证据见 `docs/ops/progress.md`。下文保留原设计，实际变更与用户后续 UI 要求见进度账本。
>
> 日期：2026-10-25，Asia/Shanghai。前置阅读：[开发记录](../../development/2026-10-25-development-record.md)、[初始交接](../../../Codex开发交接.md)、[运维说明](../../ops/runbook.md)。
>
> **修订说明**：初版把“思维链”理解成单块模型 reasoning，并采用“上传即自动入库 + 本地标签选图”。用户进一步说明要按 DSH 的处理逻辑：用户发来图片后，先分析，再调用回答；回答完再想一遍，觉得表情包不错才执行入库；偶尔调皮地模仿用户，把同一张图发回去。因此本版改为**两阶段视觉回合 agent + 真实工具步骤**，以下内容以本版为准。

## 1. 背景与目标

用户希望她处理图片消息时，过程像 DSH 一样可见、可分步：

1. 用户发来一张图片。
2. 她先分析图片。
3. 调用回答，生成正文。
4. 回答完再想一遍，判断这张图是否值得收进表情包库。
5. 如果觉得不错，执行入库。
6. 偶尔调皮一下，跟着用户学，把同一张图发回去。

因此本阶段交付：

1. **可展开的处理过程**：分析、回答、再思考、工具动作按真实顺序显示，不伪造步骤。
2. **图片输入**：用户可以在输入栏选择图片发送，图片进入视觉分析和档案。
3. **回答后的二次思考**：回答完成后，用第二次模型调用判断入库/模仿发送。
4. **真实工具动作**：观察图片、保存表情包、发送表情包都对应后端真实操作，并写入处理轨迹。
5. **表情包库**：用户图片由她决定入库；手动上传/改名/隐藏/删除；她可以发送库里的图，也可以偶尔模仿用户发同一张。
6. **历史回看**：重新打开对话时仍能看到处理轨迹、正文和她发过的图片。

## 2. 明确假设与非目标

### 假设

- “思维链”= 模型真实返回的 reasoning + 真实工具步骤；不是装饰性文案，也不是伪造的 DSH 工具执行记录。
- “她回图片”= 从本地表情包/图片库发送，或模仿用户刚发的图；**不是文生图**。
- 只有带图片的用户回合走两阶段视觉 agent；纯文字回合仍走现有单次生成。
- 用户一对一上传的图片是可信来源；入库由她的二次思考决定，不再上传即自动入库。
- DeepSeek `deepseek-flash` 默认开启思考，流式返回 `reasoning_content`；视觉未单独配置时回退当前对话模型。
- 本阶段不做通用 agent 平台，只做“图片回合”的受控工具编排。

### 非目标

- 不做通用 MCP / tool-call runtime / 任意代码执行。
- 不做文生图、图片编辑、群聊斗图、云端图库。
- 不把思考内容重新喂回模型；不记录完整 prompt/思考到系统日志。
- 不伪造“已分析/已保存”等未实际执行的动作。

## 3. 开源调研与取舍

按用户要求：先查活跃维护、许可证明确的同类项目，只借鉴设计，不复制 GPL/AGPL/无许可证代码。

| 项目 | 许可证 | 可借鉴 | 风险/结论 |
|---|---|---|---|
| [LibreChat](https://github.com/LibreChat-AI/LibreChat) | MIT | reasoning 与正文分离；[PR #5440](https://github.com/LibreChat-AI/LibreChat/pull/5440) 的 Thinking 折叠 UI；[issue #8180](https://github.com/LibreChat-AI/LibreChat/issues/8180) 暴露 reasoning 字段回归风险 | 借鉴“分离 + 折叠 + 双字段兼容 + 契约测试”，不复制 React 组件 |
| [DeepSeek Thinking Mode](https://api-docs.deepseek.com/guides/thinking_mode/) / [Vision](https://api-docs.deepseek.com/guides/vision) | API 规范 | 流式 `reasoning_content`；默认思考；非 tools 请求不要把 reasoning 再传回；支持 PNG/JPEG/GIF/WebP | 只依赖 API 协议；不引入厂商 SDK |
| [vLLM](https://github.com/vllm-project/vllm) | Apache-2.0 | reasoning parser 把思考/正文严格分离 | 仅参考设计，不引入推理引擎 |
| [AstrBot 智能图片对话插件](https://github.com/QingchenWait/astrbot_plugin_smart_imagechat_hub) | GPL-3.0 | 表情包标签库、主动表情、自动收集先入待筛选池；主动表情有额外 token 成本 | **不复制代码**；本项目用两阶段视觉 agent 做入库/模仿判断；不做群聊偷图 |
| [LangBot](https://github.com/langbot-app/LangBot) | Apache-2.0 | 媒体内容寻址、外部引用、保留/清理 | 仅参考；AGI.Wind 已有 SHA-256 + `asset_refs`，不重做存储层 |
| [SillyTavern](https://github.com/SillyTavern/SillyTavern) / [sillyimages](https://github.com/0xl0cal/sillyimages) | AGPL-3.0 / 无许可证 | `[IMG:GEN:...]` 标记驱动图片 | 许可不兼容/无许可证，标记协议脆弱；不采用、不复制 |

### 结论

- 用**类型化 SSE 事件**分离思考、工具步骤与正文，不用正文标记。
- 图片回合使用**两阶段模型调用**：回答 + 回答后反思；观察图片作为独立工具步骤。
- 工具步骤是**真实后端动作**：视觉观察、数据库入库、附件发送；不是 UI 伪造。
- 不依赖厂商原生 tool-calling 协议，避免 DeepSeek tools 模式下 reasoning 必须回传的兼容问题；本阶段用受控阶段编排 + 严格 JSON 动作计划。
- 只展示 provider 真实返回的 reasoning，不伪造思考。

## 4. 用户体验流程

### 4.1 用户发图片

1. 点输入栏“图片”，原生文件选择器打开；可多选，最多 4 张。
2. 上传后显示缩略图、可移除；文字可以为空。
3. 发送后用户消息里立即显示图片；服务端把图片写入档案。
4. 进入图片回合 agent：
   - **工具：观察图片** → 视觉模型生成描述，写入工具结果；
   - **思考/回答** → 主模型流式产出思考与正文；
   - **工具：生成回复** → 记录回答已完成；
   - **思考：再想一遍** → 第二次模型调用，判断这张图是否值得入库、是否模仿；
   - **工具：保存表情包**（如果决定入库）；
   - **工具：发送表情包**（如果决定发送/模仿）。
5. 处理过程在消息中可见；正文之后可能跟一张她发的图片。

### 4.2 回答后的二次思考

第二次调用看到的资料：

- 用户文本；
- 图片观察描述；
- 她的正文回复；
- 表情包库中可用项的名称/标签（最多 60 个）；
- 角色设定/语气（简要）；
- 最近是否已经模仿过（避免连续刷屏）。

它只输出一个严格 JSON 计划：

```json
{
  "save": {
    "name": "委屈巴巴",
    "tags": ["表情包", "委屈", "撒娇"],
    "description": "一张适合撒娇时用的图"
  },
  "mimic": true,
  "send_sticker": null
}
```

- `save` 为 null 表示不入库。
- `mimic` 为 true 表示把用户这张图原样发回。
- `send_sticker` 可选：从库里另选一张；与 mimic 二选一，最多发一张。
- 服务端会校验动作，不执行 JSON 之外的操作。

### 4.3 “偶尔调皮一下”

- 二次思考可以决定 `mimic: true`，把用户刚发的同一张图发回去。
- 服务端限制：
  - 连续 3 个图片回合内最多模仿 1 次；
  - 模型说模仿时再以约 50% 概率放行，避免每轮都学；
  - 不模仿也可以正常保存/回复。
- 提示词要求“只有自然、偶尔时才模仿；不要每轮都发；不要解释这是模仿”。
- 图片发送后出现在正文下方，作为这一轮助手消息的附件，不单独开一条无意义消息。

### 4.4 历史回看

一条助手消息按以下顺序渲染：

```text
[处理过程：观察图片 / 思考 / 生成回复]   ← 默认折叠
[正文]
[处理过程：再想一遍 / 保存表情包 / 模仿发送] ← 默认折叠
[她发的图片]
```

旧消息没有 trace/附件时行为不变。

### 4.5 表情包库页面

新增 `#/stickers` 页面（侧栏“她的档案”组）：

- 图片网格，显示名称、标签、来源（收到的/上传的）、使用次数、最近使用时间；
- 手动上传图片到库；
- 改名、编辑标签、隐藏/恢复、删除；
- 图片点击打开轻量 lightbox；删除使用现有页面内 modal，不用原生 `confirm`；
- 空状态说明“她会在回答后决定是否把图片收进来”。

## 5. 架构决策

### 5.1 两阶段视觉回合 agent

触发条件：`ChatInput.asset_ids` 至少包含一张图片。

```text
用户图片
  │
  ├─ Tool: observe_image   (VisionAdapter，真实视觉调用)
  ├─ LLM reply             (reasoning + text，流式)
  ├─ Tool: reply           (记录正文完成，真实阶段边界)
  ├─ LLM reflect           (reasoning + JSON action plan，流式)
  └─ Tools:
       ├─ save_sticker     (真实数据库入库)
       └─ send_sticker     (真实附件关联 + sticker.sent)
```

这不是通用 agent 框架，只是图片回合的固定阶段编排：

- 每个阶段都有真实的 Provider/DB 操作；
- UI 上的“工具”行对应真实动作，不是装饰；
- 不执行模型任意生成的工具名；
- 第二阶段只输出受限 JSON，服务端只接受 `save` / `mimic` / `send_sticker`。

### 5.2 为什么不用厂商原生 tool-call

- 目标体验是“分步可见”，不是接入 MCP 或任意工具生态；
- DeepSeek tools 模式要求 reasoning 在后续请求中回传，增加上下文复杂度；
- 不同 OpenAI-compatible 厂商 tool-call 流式格式不统一；
- 两阶段 + JSON 动作计划足够实现“分析 → 回答 → 再想 → 入库/模仿”，且容易测试、回放和审计。

### 5.3 思维链

- Provider 输出改为类型化 chunk：`text | reasoning`。
- 第一次调用：reasoning 标记 `stage="reply"`；正文走现有 `text.delta`。
- 第二次调用：reasoning 标记 `stage="reflect"`；最终 JSON 只作为动作计划，不进入正文。
- 兼容 `delta.reasoning_content`（DeepSeek/vLLM）与 `delta.reasoning`（部分 OpenAI-compatible）。
- 最终 assistant content 保存 `trace`；`reasoning` 作为兼容拼接字段。
- reasoning 不进入模型上下文、不建记忆切片、不写系统日志正文。

### 5.4 图片与媒体

- 复用现有内容寻址 `assets` + `asset_refs`，不把 base64 写进事件。
- 用户图片：`ChatInput.asset_ids`；历史接口返回 `attachments`。
- 她发的图片：assistant content 增加 `attachments`，同时写 `asset_refs`。
- 模仿用户时附件直接引用用户原 asset；不复制文件。
- 格式：PNG/JPEG/WebP/GIF；拒绝 SVG/HTML；保留原图，浏览器显示动图。

### 5.5 入库时机

- **不再在 accept 时自动入库**。
- 只有二次思考输出 `save`，或兜底失败策略触发时，才写 `stickers`。
- 二次思考失败/JSON 无效：
  - 不改正文；
  - 记录 `tool.failed`；
  - 自动收藏作为兜底（用户直接上传的图片可信），但不模仿；
  - UI 仍显示“已保存（兜底）”，避免图片白丢。
- 用户可在用户图片上点“保存为表情包”手动补存（可选按钮）。

## 6. 数据模型与迁移

### 6.1 `stickers` 表（迁移 `0008_reasoning_stickers`）

```text
stickers
  id uuid pk
  account_id uuid fk accounts
  companion_id uuid fk companions
  asset_id uuid fk assets
  name text not null
  description text not null default ''
  tags jsonb not null default '[]'
  state text not null check (state in ('active','hidden'))
  source text not null check (source in ('captured','uploaded'))
  source_event_id uuid fk events nullable on delete set null
  use_count integer not null default 0
  last_used_at timestamptz null
  created_at timestamptz not null default now()
  unique(companion_id, asset_id)
```

说明：

- 只存 asset 引用，不存二进制；同一图片在同一角色下只入一次库。
- `source_event_id` 用于删除来源消息时清理自动收藏；`on delete set null` 避免删除事件被 FK 阻塞。
- 不在本阶段做“待筛选”状态；用户直接上传视为可信。
- 迁移创建新表，不改已有列；现有数据无需回填。
- 档案格式从 v2 升到 v3；旧 v1/v2 包仍可导入，缺少 `stickers` 按空表处理。

### 6.2 Assistant 事件内容

```json
{
  "text": "这张图好可爱。",
  "reasoning": "用户发来一张……",
  "trace": [
    {
      "id": "uuid",
      "type": "tool",
      "tool": "observe_image",
      "stage": "observe",
      "status": "completed",
      "summary": "已读取图片：一只委屈的猫"
    },
    {
      "id": "uuid",
      "type": "reasoning",
      "stage": "reply",
      "text": "先看图，再决定怎么回……"
    },
    {
      "type": "tool",
      "tool": "reply",
      "stage": "reply",
      "status": "completed",
      "summary": "已生成回复"
    },
    {
      "type": "reasoning",
      "stage": "reflect",
      "text": "这张图挺适合做表情包，回复也自然……"
    },
    {
      "type": "tool",
      "tool": "save_sticker",
      "stage": "reflect",
      "status": "completed",
      "summary": "已收进表情包库：委屈巴巴"
    },
    {
      "type": "tool",
      "tool": "send_sticker",
      "stage": "reflect",
      "status": "completed",
      "summary": "模仿用户发送了同一张图",
      "asset_id": "uuid"
    }
  ],
  "attachments": [
    {
      "asset_id": "uuid",
      "kind": "sticker",
      "name": "委屈巴巴",
      "mime": "image/png",
      "width": 240,
      "height": 240
    }
  ],
  "state": "complete"
}
```

- `text` 保持旧键；旧上下文/检索只读 text。
- `reasoning` 兼容拼接；`trace` 是新的权威顺序。
- `trace` 中每个工具步骤都必须对应真实执行结果；失败也有 `status:"failed"` + 安全错误码。
- reasoning 上限 64,000 字符；超出停止继续写，最终带 `reasoning_truncated: true`。

### 6.3 SSE 事件

```text
reasoning.delta      {"stage": "reply"|"reflect", "text": "..."}
tool.started         {"step_id": "...", "tool": "...", "stage": "...", "summary": "..."}
tool.completed       {"step_id": "...", "tool": "...", "status": "completed"|"failed", "summary": "...", "asset_id": "..."}
sticker.sent         {"attachment": {...}, "trigger": "mimic"|"library"}
text.delta           {"text": "..."}      # 现有事件不变
text.completed
reasoning.completed  {"stage": "reply"|"reflect", "truncated": false}
generation.completed
```

- 事件写 `generation_stream_events`，`Last-Event-ID` 可重放。
- 先 `tool.started`，执行后 `tool.completed`；失败也写完成事件，带 `failed` 状态。
- `sticker.sent` 先确保 assistant 事件存在，再写附件和事件，避免历史缺图。
- 不发送任何未实际执行的动作事件。

### 6.4 API

```text
GET    /api/v1/stickers?companion_id=...
POST   /api/v1/stickers          {companion_id, asset_id, name?, tags?}
PATCH  /api/v1/stickers/{id}     {name?, tags?, state?}
DELETE /api/v1/stickers/{id}
GET    /api/v1/conversations/{id}/events   # 每条事件增加 attachments 和 trace
```

- 附件结构：`{asset_id, kind, name, mime, width, height}`。
- 所有接口复用现有认证、CSRF、账号/角色 scope 校验。
- `capabilities.image` 在独立视觉或对话回退可用时为 true。

## 7. 后端实施

### 7.1 Provider

- `providers/protocol.py`：新增 `ProviderChunk(kind, text)`；`stream()` 返回 chunk。
- `providers/openai_compatible.py`：
  - 解析 `delta.reasoning_content`，兼容 `delta.reasoning`；
  - 解析 `delta.content`；
  - 同一 delta 两者都有时按顺序产出；
  - `complete()` 保持只返回正文。
- 更新所有 FakeProvider/测试；旧只返回文本的 provider 适配新接口。

### 7.2 图片回合编排器

新增 `delivery/visual_agent.py`（或 `delivery/turn_agent.py`）：

- 输入：scope、generation、request、current input、provider、vision、sticker service、context。
- 阶段：
  1. `observe_image`：调用 VisionAdapter；记录工具开始/完成；失败则明确报错，不伪造描述。
  2. `reply`：调用现有 `provider.stream`；reasoning `stage=reply`；text 走现有归档/SSE。
  3. `reply` 工具完成：记录“已生成回复”，不额外生成内容。
  4. `reflect`：第二次 `provider.stream`，产物是 reasoning + JSON 动作计划；reasoning `stage=reflect`；正文 JSON 不展示。
  5. 执行 `save_sticker` / `send_sticker`；真实写库/附件；记录工具事件。
- 失败处理：
  - 视觉失败：整轮返回 `VISION_FAILED`，不回复假描述；
  - 回复失败：保留已生成部分；
  - 反思失败/JSON 无效：正文保留，工具失败 + 兜底入库（不模仿）；
  - 工具执行失败：记录 failed，不影响正文。
- 预算：
  - 反思调用限制输出约 300 tokens；
  - 表情包库最多注入 60 个名称/标签；
  - 反思调用超时不影响正文。
- 图片回合限定：`asset_ids` 含 image 时启用；纯文字回合走现有单次生成。

### 7.3 交付与归档

- `delivery/service.py`：
  - 对图片回合调用 VisualAgent；纯文字回合维持现有路径；
  - 统一写 trace / reasoning / attachments / state；
  - 最终 `generation.completed` 与现有终态一致。
- `archive/service.py`：
  - `append_output` 合并而不是覆盖 content；
  - 新增 `append_trace_step`、`append_reasoning`、`ensure_assistant_event`、`attach_output_asset`；
  - 所有写仍走 companion/generation 锁与 epoch 校验。
- `memory/context.py`：
  - recent 预算只按 text + 附件元数据估算，不让 reasoning/trace 挤占上下文；
  - `messages()` 只取正文；reasoning/trace 不回传；
  - 最近图片的图像观察作为“非指令资料”进入上下文。

### 7.4 图片与视觉

- `archive/assets.py`：
  - 增加 `image/gif`；校验帧数/尺寸/总像素；
  - 保留原图；
  - 在 asset metadata 记录原始文件名，用于表情包默认命名。
- `providers/vision.py`：
  - 使用当前视觉模型；问题为空时用默认观察问题；
  - 观察结果用于二次思考；
  - 不打印 base64。
- `api/app.py`：
  - `models.vision` 未配置时回退当前对话模型；
  - 独立视觉优先；
  - 历史接口批量查询 asset_refs，返回 attachments；trace 存在 content 中。

### 7.5 入库与模仿

新增 `archive/stickers.py`：

- `save_from_asset(scope, asset_id, source_event_id, name, description, tags)`：
  - 同一角色+asset 唯一；
  - 已存在则更新 description/tags/name（用户手动改名不覆盖）；
  - 写 source_event_id；
  - 返回是否有变化。
- `pick` 仍可用于“从库里另选一张”的可选动作；不是主流程。
- `attach_to_generation(...)`：
  - 写 assistant attachments + asset_ref；
  - 更新 use_count/last_used_at；
  - 发 `sticker.sent`；
  - 失败只记 warning，不影响正文。
- 模仿限制：
  - `VisualAgent` 维护最近图片回合的 mimic 记录（查询最近 assistant trace，或新增轻量字段）；
  - 连续 3 个图片回合最多 1 次；
  - 模型决定 mimic 后以约 50% 概率放行；
  - 不满足时记录 `tool.completed` 为 `skipped`，不发图。

### 7.6 导出、导入、删除

- `stickers` 加入导出/导入表顺序，置于 `asset_refs` 后。
- 归档 manifest：`archive_format_version=3`、`schema_version="0008_reasoning_stickers"`。
- 导入兼容 v1/v2；旧包缺失 stickers 视为空。
- 删除事件时：
  - 自动收藏 sticker 的 `source_event_id` 置空或删除该 sticker；
  - 仍被其他 sticker/事件引用的 asset 不删；
  - asset.gc 同时检查 sticker 引用。
- 手动上传的 sticker 不随聊天删除。

## 8. 前端实施

### 8.1 分阶段处理轨迹

不能只做一个“思考”块，需要能表达“先分析、回答、再想、执行工具”的顺序：

- assistant 消息内新增 `trace` 容器，分两组：
  - `replyTrace`：观察图片、reply reasoning、生成回复；
  - `reflectTrace`：reflect reasoning、保存表情包、模仿发送。
- 每组用可折叠标题：
  - “处理过程 · 观察图片 / 思考 / 回复”
  - “回应之后 · 再想一下 / 保存表情包 / 模仿”
- 工具行显示小标签和状态：
  - `观察图片`
  - `生成回复`
  - `保存表情包`
  - `发送表情包`
  - 失败显示 `失败`，跳过显示 `已跳过`。
- reasoning 行只显示真实 reasoning 文本；不把 JSON 动作计划当正文。
- 流式更新只改对应节点，不重建整个消息。
- 首个正文 delta 时 `replyTrace` 自动折叠；`reflectTrace` 在完成后显示并默认折叠。
- 历史回看默认折叠；点击展开。
- 没有 trace 的旧消息行为不变。

### 8.2 正文与图片顺序

- 正文流式显示在 `replyTrace` 之后。
- `reflectTrace` 在正文之后；她发的图片（模仿/库图）在 reflectTrace 之后。
- 模仿图片不重复用户消息里的图；它是 assistant 附件。
- 图片使用固定宽高比占位，避免加载跳动；点击打开 lightbox。
- 只有图片没有文字的消息也要显示。

### 8.3 输入栏图片

- 原生 `<label>` + `<input type=file accept="image/*" multiple>`，不使用合成 `.click()`。
- 待发送缩略图区；最多 4 张；可移除；上传中禁止发送。
- 发送 `input: {text, asset_ids}`，允许纯图片。
- 用户消息立即显示图片；跨域用 `assetObjectUrl`。
- 历史只渲染 `user_input` / `assistant_output`，不把 `vision_observation` 当用户消息。

### 8.4 表情包库

- 新增 `#/stickers` 页面与侧栏入口。
- 网格、上传、改名、标签、隐藏/恢复、删除、来源/使用次数。
- 图片 lightbox；删除用页面内 modal。
- 空状态说明“她会在回答后决定是否收进来”。

## 9. 安全、隐私与失败模式

- 图片只接受 PNG/JPEG/WebP/GIF；拒绝 SVG/HTML；服务端 Pillow 校验。
- 图片、思考不发系统日志正文；日志只记长度/动作。
- reasoning/trace 不进入模型上下文、不索引记忆；DeepSeek 非 tools 请求也不需要回传。
- 工具步骤必须对应真实动作；失败写 failed，不假装完成。
- 视觉失败不伪造描述；图片仍保存在档案。
- 反思失败不拖垮正文；兜底入库但不模仿。
- 表情包发送失败不拖垮正文；无匹配/被限制则不发送。
- 删除来源消息时清理 sticker 引用，避免 FK/悬空；共享 asset 不误删。
- 跨域图片通过授权 blob 加载，不把密钥放进 URL。
- 不复制 AGPL/GPL/无许可证代码；参考项目只在文档中标注。

## 10. 测试与验收

### 后端

- Provider 契约：
  - `reasoning_content` / `reasoning` / 混合 / 纯正文；
  - 流中断、错误脱敏；
  - reasoning 不混进 text。
- 视觉回合：
  - 工具顺序：observe → reply reasoning/text → reply completed → reflect reasoning → save/mimic；
  - 二次思考的 JSON 被解析，未知字段/多余动作被拒绝；
  - 保存发生在正文之后，不在 accept 时；
  - mimic 引用用户原 asset，不复制文件；
  - 连续 3 回合最多 1 次模仿；概率可注入测试；
  - 反思失败保留正文并兜底入库，不模仿；
  - 视觉失败整轮明确报错，不伪造描述。
- 归档/上下文：
  - assistant content 同时含 trace/text/attachments/state；
  - 失败/取消保留部分 trace 与已发图；
  - reasoning/trace 不进入 `messages()`；上下文成本不被膨胀；
  - 导出/导入 v3、v2 兼容、删除引用一致。
- 现有 82 项回归全绿。

### 前端无头 Chromium

- mock SSE：tool observe → reasoning reply → text → reply completed → reasoning reflect → save → mimic → sticker sent。
- 处理轨迹按顺序显示，工具状态正确，正文和图片顺序正确。
- 原生图片选择、预览、上传、发送、用户消息图片。
- 折叠/展开、历史回看、表情包库上传/改名/隐藏/删除。
- 1440 / 1024 / 390，无横向溢出，0 JS 报错；截图 `.devtools/v7-*.png`。

## 11. 实施顺序

1. 迁移 `0008` + `stickers` + 导出/导入/删除兼容 + 测试。
2. ProviderChunk + reasoning 解析 + 契约测试。
3. Delivery/Archive trace/reasoning/attachments 持久化、上下文排除。
4. 图片上传扩展（GIF/原文件名）、视觉回退、历史 attachments。
5. VisualAgent 两阶段编排 + 工具事件 + 保存/模仿动作。
6. Sticker API + 权限测试。
7. 前端双处理轨迹、输入栏图片、历史/归档图片、表情包页。
8. 无头验证 + 文档 + AOCI + 重启/强刷。

## 12. 发布与回滚

- 迁移通过 `alembic upgrade head` 或启动脚本执行；只新增表。
- 旧归档可导入；新导出为 v3；不删除已有资产。
- 后端改动需重启 Gateway；静态文件改动后 Ctrl+F5。
- 若需回滚：先停新客户端/新接口使用，再降级应用；`stickers` 表可保留（旧代码忽略），不要删用户资产。
- 更新 README、runbook、progress；AOCI 维护后 `verify/check`。

## 13. 实施检查清单

- [x] 调研结论与许可证记录已确认。
- [x] `0008` 迁移、`stickers` 表、档案 v3、旧包兼容。
- [x] ProviderChunk、`reasoning_content`/`reasoning` 解析与契约测试。
- [x] `reasoning.delta` / `tool.started` / `tool.completed` / `sticker.sent` 事件。
- [x] assistant content 合并保存 trace/reasoning/attachments，失败保留 partial。
- [x] reasoning/trace 排除出模型上下文与记忆索引。
- [x] 用户图片上传、GIF、历史 attachments。
- [x] 视觉模型回退对话模型。
- [x] VisualAgent：observe → reply → reflect → save/mimic。
- [x] 二次思考失败兜底入库、不模仿。
- [x] 模仿限制：连续 3 回合最多 1 次、概率放行。
- [x] Sticker CRUD API 与权限。
- [x] 前端双处理轨迹、图片输入/显示、表情包库、历史/归档。
- [x] 无头验证 1440/1024/390，0 JS 报错。
- [x] README/runbook/progress/AOCI 更新。

## 14. 明确不做

- 不做通用 agent/MCP/任意工具调用。
- 不做文生图/图片编辑。
- 不做群聊自动偷图、斗图、云端图库。
- 不把思考正文写入系统日志或重新喂给模型。
- 不伪造未执行的工具步骤。
- 不引入前端框架或构建步骤。
