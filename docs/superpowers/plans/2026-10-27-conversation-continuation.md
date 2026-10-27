# 连续会话 Agent Implementation Plan

> **For agentic workers:** Use superpowers:executing-plans; inline execution with focused tests. Preserve existing uncommitted work. No paid model tests or real QQ test messages.

**Goal:** 主人私聊中，同一 Agent 任务能中途说话、使用既有只读工具、持久等待后继续，直至结束或被用户打断。

**Architecture:** 复用 IdleCognitionService，定时唤醒与回复后任务进入同一个执行器。`say/wait/finish` 是行为决定，推理仅来自供应商通道。复用 PostgreSQL 运行表，增加无正文 checkpoint；等待使用 pending/due_at，释放工作槽。

**Tech Stack:** Python/FastAPI, SQLAlchemy/PostgreSQL, native JS, existing DeepSeek adapter and QQBridge. No new dependency.

**Spec:** 用户已在本对话确认的“中途说话不结束任务，等待后继续，新消息及 Stop 打断”。

## Global Constraints

- 定时仅主人私聊；群沿用原有有限追话，不开放持续自主任务或本地命令权限。
- 保留已有权限、quiet hours、每日消息/任务额度、版本与场景隔离、QQ 逐段校验。
- 等待不调用模型、不占用 coroutine；重启恢复同一任务 ID、累计轮数/工具数/发信数。
- checkpoint 不重复保存用户正文、工具结果或推理；由权威事件重建上下文。已发消息不重放；意外中断的在途副作用失败关闭。
- 默认持续任务 6 轮模型、3 次发信、单次等待最多 900 秒、总生命周期 30 分钟，可管理；原工具预算继续生效。

## Review Focus

- 说话后仍须校验下一步，用户新消息优先且旧任务不续说。
- 断电重启等待恢复同 ID，发过的消息不重复；累计预算不能清零。
- QQ 换绑、人格记忆更新、归档或通话发生时不得沿旧上下文外发。
- Stop 取消 pending 等待和 active 工具请求。
- 达到模型/工具/消息/生命周期预算即结束，不靠一直唤醒逃逸额度。

## Tasks

- [x] 1. 先复现 9 项失败；实现 say→wait→同 ID 恢复→工具→say→finish 与 QQ、新消息/Stop、累计预算及失效回归。
- [x] 2. 增加 checkpoint 和兼容迁移 0017；定时与回复后复用执行器，保留旧 reach_out/silent，移除旧定时生成循环。
- [x] 3. 接管理开关/预算与等待状态；相关 72 项回归通过，浏览器 mock 验证通过，JS 语法与 diff 空白检查通过。
- [x] 4. 完成跨模块复核，使用既有部署脚本重载 Gateway；运行及 AOCI 收尾结果按下方证据入口核对。

## Design references

参考 [LangGraph durable execution](https://docs.langchain.com/oss/python/langgraph/durable-execution) 的持久检查点与副作用隔离，以及 [Temporal timers/activities](https://docs.temporal.io/tasks) 的等待释放工作槽。LangGraph 和 Temporal Python SDK 均为 MIT；本项目已有 Postgres 调度，采用设计模式而不引入框架、服务或复制代码。两者都要求处理副作用重放/幂等，故本实现不自动重发已经归档的消息。

## Implementation record

- 入口：`ProactiveService.send → IdleCognitionService.start_wake/_run_one`，回复后入队也走 `_run_one`。主人私聊的 `say` 发出后继续；`wait` 或带等待的 `say` 保存同一运行 ID 并返回，后续 tick 重新装配权威上下文。不是等待期间一直调用模型。
- checkpoint 仅含源场景、版本、QQ 绑定、截止时间、累计轮数/发信/工具次数及调用 ID；正文以事件为准，工具结果和原生推理不重复存进去。本次不增加长期记忆事实写入逻辑，派生运行不进入开放归档包。
- 正常等待恢复同 ID；意外中断的 active 段因可能已发生外部副作用而失败关闭，不重放。每轮调用前预留计数，等待不重置总预算；修改设置会在下一步生效。关闭连续模式或总开关会立即取消已保存的等待。
- 手动启动的例外只用于当前发信前的操作；一旦发信或进入等待，后续行为恢复自动约束，包括安静时段和每日上限。
- 管理入口：模型与语音 → 定时唤醒与主动消息 → 连续自主任务。默认 6 次模型调用、3 次发信行动、单次等待最多 900 秒、最长 30 分钟；实际可以提前 finish。每次行动可按原规则分成多个气泡，工具仍共享原来的 0–3 次预算。
- 本次连续循环用于回复后及定时任务；普通主聊天请求仍保留原有执行方式，群追话不启用持续任务。自主工具仍仅记忆、表情查询与联网；没有开放后台文件写入/命令执行或 QQ 空间发布。
- Ruling: 复用现有 PostgreSQL worker，不加入 LangGraph/Temporal 依赖；按用户额度偏好串行实现与定向验证，保留已有未提交改动，不自动提交或推送。

## Evidence

- `app/tests/integration/test_conversation_continuation.py` 覆盖中途发信后工具执行、跨重启等待、新消息/Stop、QQ 换绑、记忆失效、累计预算、运行中调低预算、开关取消、已有表迁移和手动启动后安静时段。
- 相关测试命令：`python -m pytest tests/integration/test_conversation_continuation.py tests/integration/test_native_idle_reasoning.py tests/integration/test_proactive_wake.py tests/integration/test_proactive.py tests/integration/test_idle_cognition.py tests/integration/test_idle_cognition_controls.py tests/integration/test_system_links.py tests/integration/test_qq_idle.py tests/unit/test_idle_tools.py tests/unit/test_call_provider.py -q` → **72 passed**。
- `node scripts/verify-proactive-client.mjs` → PASS（中途消息、等待状态、连续设置、零 JS 异常）；网络写请求均 mock，不发送真实 QQ 消息。
- 部署使用 `scripts/stop-local.ps1 -KeepVoice` 与 `scripts/start-local.ps1 -BindHost 0.0.0.0`。AOCI 最终证据位于忽略的 `data/continuation-final-verify.json`、`data/continuation-final-check.json`、`data/continuation-final-guide.json`，以其实际状态为准。

## 后续日志刷屏修复

确认原因为每次重建会话 watcher 都从 live 序号 0 回放，任务结束事件未去重，且日志使用接收时间，令旧取消和额度记录看似同一秒新产生。新增授权只读 live-cursor，在读取历史前捕获序号，随后只订阅之后的事件，避免历史读取期间漏消息；本页按网关和会话保留最多 64 个游标，切换/重连不回退并丢弃重复包。零调用的取消或额度跳过不作为思考完成日志，真实模型工作和失败仍可见，使用服务端事件时间。后端事件保留，不删除业务记录。

验证：Node 日志/游标纯函数 10 项、只读快照及鉴权 1 项，以及浏览器快照/重连去重/零调用过滤回归；均使用合成事件，没有付费模型或真实 QQ 发信。
