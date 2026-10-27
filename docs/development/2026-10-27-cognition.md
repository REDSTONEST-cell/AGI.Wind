# 主对话思维链与回复后独处思考

日期：2026-10-27。目标：给主对话加入可观察的轮数/步数，并让角色在回复后短暂独处思考，必要时主动回来找用户。

## 用户可见行为

- 用户发消息 → 正常生成回复；日志抽屉里的思考/工具行显示 `第 N 轮 · 第 M 步`。
- 聊天头部出现轻量认知状态：生成中显示「思考 · 第 r 轮 / 第 s 步」，完成后显示本轮摘要。
- 回复完成约 150 秒（可配置 30–3600 秒）后，如果用户没有新消息：
  - 后台启动一次独处思考，按轮/步进行；
  - 如果决定开口，像主动消息一样在聊天中发来一条新消息；
  - 如果决定保持安静，只记录思考，不打扰。
- 用户在这 150 秒内回复、按停止、归档/删除会话或切走生成时，这次思考取消。
- 主动消息设置页可调：是否启用、无回复延迟、最多轮数、每轮最多步骤、每天最多思考次数，并显示最近一次思考状态。
- 日志抽屉实时显示独处思考的轮/步；主动消息设置页显示最近一次思考记录。

## 后端实现

### 主对话轮/步

- `agent/runner.py`：新增可选同步 `on_step` 钩子和 `AgentResult.rounds_used/steps_used`；工具 trace 事件带 `round/step`；普通回复按 1 轮 1 步记录。
- `agent/delivery.py`：把轮/步传给 `GenerationService`，`record_reasoning` 按 round/step 分组持久化。
- `delivery/service.py`：生成完成时在 assistant 事件 `content.cognition` 写入 `{rounds, steps, tool_calls, duration_ms}`，并发出 `cognition.completed` 流事件；不改变既有文本/语音/记忆字段。
- 前端：日志标签、聊天头部认知状态条；历史 trace 回放也能看到轮/步。

### 回复后独处思考

- 新模块 `delivery/idle_cognition.py`，运行状态表 `idle_cognition_runs`（派生状态，不进入归档导出；会话删除级联清理）。
- 迁移 `0015_idle_cognition.py` 创建表及（会话待办唯一）部分索引。
- 设置扩展 `ProactiveSettings`：
  `after_reply_enabled=true`、`after_reply_delay_seconds=150`、`after_reply_max_rounds=2`、
  `after_reply_max_steps=4`、`after_reply_max_runs_per_day=6`；安静时段、每日主动消息上限沿用原配置。
- 回复完成提交后入队 pending；后台每 10 秒领取到期任务，模型调用不持有数据库锁。
- 独处思考每轮调用一次 `provider.complete`，模型严格输出 JSON：
  `{"thought","steps":[...],"action":"continue|reach_out|silent","message"}`。
  轮/步上限、超时、安静时段、每日上限、用户是否回复、是否有新生成、原回复是否仍可见都在调用前和发送前复核。
- `reach_out` 且消息非空时，复用 `ProactiveService.deliver(...)` 写入与实时推送，来源 `after_reply`；不二次调用模型。
- 用户新请求、Stop/打断、已有主动消息发送、会话归档/删除都会取消待办；重启后 pending 任务继续，过期 running 会恢复或失败关闭。

## API / UI

- `GET/PATCH /api/v1/proactive/settings` 增加独处思考字段与 `recent_cognition`。
- `GET /api/v1/proactive/cognitions?companion_id=...&limit=...` 返回最近思考运行记录（状态、轮/步、步骤摘要）。
- 界面：聊天头部认知状态条、日志轮/步编号、主动消息卡片新增控制与最近思考。
- 实时事件：`idle_cognition.started|step|completed`（conversation live stream）；实际消息仍用原有 `proactive.message`。

## 边界

- 只对网页主人私聊生效；QQ 私聊/群聊主动消息仍不发送，群场景不会调度。
- 使用当前对话 provider/model，不新增模型角色；每次回复最多一次独处思考，每日模型调用有上限。
- 独处思考的完整原文属于私有运行状态，只在日志抽屉显示；导出/恢复不包含该表。
- 模型未配置、超时、JSON 非法、额度用尽时失败关闭，不发送、不重试风暴。
- 长空闲主动消息逻辑保留；独处思考发出的消息会刷新 `proactive_messages`，后续长空闲仍受原有间隔/上限约束。

## 验证

- `tests/unit/test_agent_runner.py`：轮/步计数和工具事件编号。
- `tests/integration/test_idle_cognition.py`：等待后主动发送、silent、用户回复取消、安静时段、连续两轮与步数上限、非法 JSON、主对话 cognition 元数据。
- `tests/integration/test_proactive.py`：新设置字段持久化、最近思考接口、既有主动消息回归。
- 无头 Chromium：主动消息卡片字段读取/修改/重载后持久化，0 JS 异常。
- 说明：完整 pytest 运行时工作区同时存在另一并行开发留下的未提交记忆自动合并与语音分段改动，相关旧测试会失败；本功能专项测试通过。最终整体数字以合并后全量回归为准。
