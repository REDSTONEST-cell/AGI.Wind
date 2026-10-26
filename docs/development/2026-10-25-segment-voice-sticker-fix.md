# 分段语音与表情包言行一致性修复（2026-10-25）

## 起因

合并 Agent 能力之前就存在的两个交互 bug，用户在实际聊天中遇到：

1. 用户明确要表情包时，回复正文说「我这边发不了图，只能给你敲字」，随后旧 reflect 阶段又独立发了一张表情包。
2. 回复被展示成多段时，整轮只生成一条语音，覆盖所有句子；各分段几乎同时出现，没有人类打字节奏。

数据库证据（`data/local-db.json`）：

- 表情包矛盾：generation `ce810b7a-b7cc-478b-baa5-4575b63feffc`（19:36:32），正文「我这边发不了图，只能给你敲字。要不我用颜文字凑一个？」，trace 里 reflect 决定「用户明确要表情包，必须从库中选一张」并发送「困困摸鱼」。
- 整段语音：generation `862e0d29-54c0-462d-b15b-f100685c7d19`（19:37:03），回复是 3 个展示分段，但 `speech_segments` 只有一行 `segment_index=0`、`display_text` 为整段，语音 12960ms 覆盖三句。

## 实现

### 表情包由模型自己决定

- `EmotionTagStream` 扩展为隐藏指令解析：除 `[情绪:开心]` 外，可在正文前写 `[表情包]` / `[表情包:关键词]`；指令不进入正文、SSE、存档或历史。
- 普通聊天的 system prompt 增加表情包能力说明与当前表情包库名称/标签；明确要求不能说「不能发表情包/只能打字」，且写了标记必须真的发送。
- 发送改由回复里的隐藏标记驱动：服务端按关键词从库中选一张（失败时用受控随机挑一张），所有情况都复用现有 `sticker.sent` 附件链路。
- 删除普通聊天旧的 reflect_send 二次独立决策，避免正文和附件互相不知道；Agent 模式继续用 `send_sticker` 工具，图片观察轮仍保留 save/mimic，但本轮已用标记发送时不再重复发送。

### 分段语音

- 新增服务端权威分段器 `delivery/segments.py`，与浏览器 `reply-segments.js` 同样的规则（去句末标点、保留引号/括号、代码块和 URL 不拆），流式过程中逐段产出。
- 每完成一段就按当前模式独立决定文字或语音：
  - `off`：全文字；
  - `always`：每段都语音；
  - `auto`：每段独立按概率抽取。
- SSE 新增复用已有 `text.segment` 类型的事件，payload 带 `index/text/kind`，语音段带 `segment_id`；`speech_segments` 按 `segment_index` 一行一段，音频附件带 `segment_index/segment_id`。
- `VoiceService` 支持多段队列/合成/状态：`queue_segment`、多段 `speech_status().segments`、稳定的逐段情绪与失败回退；整轮情绪仍来自模型隐藏标签，缺标签时只调用一次受限情绪兜底。
- `content["segments"]` 保存最终分段结构；旧事件没有 `segments` 时客户端仍走旧渲染，兼容历史档案。
- 客户端：聊天改用服务端分段事件；每段逐字显示（约 55ms/汉字，标点略停），段间留打字间隔；语音段到顺序时显示语音气泡并等音频就绪，TTS 失败/取消时该段退回文字，不丢正文；贴纸等图片在正文队列之后出现。历史按 `segments` 顺序还原，语音失败段显示文字。

## 验证

- 完整后端：209 passed, 2 skipped（新增分段器、隐藏标记、每段语音、每段概率、表情包标记发送/空库等测试）。
- 协议检查通过；Node 分段测试 5/5 通过。
- headless Chromium：`scripts/verify-segment-voice.mjs` PASS——文字/语音/文字/图片顺序正确，语音在首段打字后出现、第三段等语音之后才出现，0 JS 异常；`verify-voice-client.mjs` 与 `verify-agent-client.mjs` 仍通过。
- 未重启真实 Gateway/Breeze，未开启 Agent，未修改 `data/settings.json`；真实模型、真实 TTS 的端到端分段时间仍需重载后实测。

## 边界与遗留

- 本次验证使用 mock SSE + 真实本机静态资源，没有调用真实模型或搜索服务；真实 Breeze 逐段合成待 Gateway 重载后最小实测。
- 并行结束时主工作区还有 Codex 未收尾的「电脑文件/管理员审批」功能（`agent/files.py`、`agent/permissions.py`、审批接口与 `agent-settings.js`）。本次只补齐了缺失的页面元素（文件访问模式、管理员状态/请求、审批 dialog）使其可加载，该功能的完整验收与文档仍待其负责人完成。
- AOCI 仍未对齐；本次修复后的最终维护需在真实重载和剩余收尾完成后执行。
