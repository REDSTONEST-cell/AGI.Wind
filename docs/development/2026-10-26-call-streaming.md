# 实时语音通话：Breeze 流式合成改造

## 问题

用户反馈通话时“鱼丸会先合成，而不是流式输出”。检查 Codex 写进的通话链路（`voice/call.py` + 客户端 `realtime-call.js`）后确认：

- 客户端把模型流式文本按 20/40 字切块，通过 `speak` 控制消息发给 Gateway；
- Gateway 对每个切块**单独发一次** Breeze HTTP `/v1/audio/speech` 请求，串行等待、一次只合成一块；
- 两块之间会出现明显接缝（实测约 160ms 静音，取决于请求完成与播放调度）；
- 短回复（20–50 字）在 0.2–0.4 秒内就生成完，而 Breeze 首包约 300–450ms，因此听感就是“文字先全部出来，再开始合成、再播放”。

Breeze HTTP 接口本身是流式的，但按块重建请求会重复握手、串行等待，并丢失“文本还在生成时就开始说”的意义。

## 改造

Breeze 自带 WebSocket 协议（`docs/websocket.md`），明确用于“text in as it arrives”：客户端持续发文本，服务端按句子边界排队合成并连续返回 PCM；支持中途 `cancel`、`instruction` 切换。

1. 新增 `app/src/agi_wind/voice/breeze_ws.py`：Breeze WebSocket 会话客户端（`start` / `text` / `flush` / `cancel` / `close`，二进制 PCM 回调），连接地址从 `/health` 的 `ws_port` 读取（默认 HTTP 端口 +1，即 8138）。
2. `voice/call.py` 改为**每次通话一个 Breeze WebSocket 会话**：不再逐块 HTTP 请求；`speech_text` 把模型原始文本增量直接喂给 Breeze，`speech_flush` 在文本结束时冲刷缓冲；`cancel` 映射到 Breeze 的 `cancel`，旧音频在收到 `cancelled` 前被抑制，不会串到新一轮。
3. 客户端 `realtime-call.js` 去掉 `SpeechChunks` 切块和 `speech_credit` 计数：`text.delta` 直接发 `speech_text`，`text.completed`/`generation.completed` 发一次 `speech_flush`；音频仍用 `PcmPlayer` 逐包调度播放，epoch 过滤迟到音频。
4. **中文标点归一化**：Breeze WS 的句子切分只认 ASCII 标点和空格，全角 `，。！？` 不会触发分句，中文会一直滞留在缓冲里（实测无任何音频）。因此 WS 路径发送前把全角标点替换为 ASCII 标点 + 空格（`，→", "`、`。→". "` 等），保留原文明细与显示不变；HTTP 合成路径不受影响。
5. `voice.health()` 现在透传 `ws_port`，`/api/v1/call/status` 可看到 WebSocket 端口。

## 实测

浏览器端使用**假麦克风**（Breeze 合成的提问 WAV，Chrome `--use-file-for-fake-audio-capture`）驱动完整通话链路，并注入 WebSocket/SSE/AudioContext 时间线探针：

| 指标 | 结果 |
| --- | --- |
| ASR final → 客户端首个 `speech_text` | 紧随首个文本 delta（token 级，不再等 20/40 字切块） |
| 首个文本 delta → 首个 PCM 音频包 | 约 450ms（Breeze 首包延迟，含 WS 握手） |
| 连续音频缓冲之间的排期间隙 | **0ms**（旧 HTTP 逐块路径有约 160ms 静音接缝） |
| 首段音频排期时长 / 总排期 | 8 段连续，无 JS 异常 |

直接对通话 WebSocket 压测（`scripts/verify-call-stream.py`）：`audio_start` 约 36ms，首个 PCM 约 315ms，10 个音频帧连续到达，最大到达间隔约 409ms。

说明：短回复的文本生成速度（约 0.2–0.4s）仍然快于 Breeze 首包（约 0.3–0.45s），所以极短回复的听感仍是“文字刚完，声音接上”，这不是一次性整段合成，而是 TTS 首包延迟；长回复会边生成文字边出声。

## 验证

- 新增 `tests/unit/test_breeze_ws.py`：文本增量/标点归一化/flush、cancel 后抑制旧音频、`start` 失败的类型化错误。
- 既有 `tests/integration/test_call.py`（票据、Origin、PCM、断连清理、realtime 流式回复）保持通过。
- 新增 `scripts/verify-call-stream.py` 可重复验证通话音频流式输出。
- Chromium 假麦克风通话测试：0 个 JS 异常，音频排期连续。

## 后续可做

- 在有参考音色时把 `instruction` 与情绪标签动态下发（Breeze WS 支持逐句切换）。
- 播放侧加入 ~100–150ms 抖动缓冲，进一步降低极端网络/GPU 抖动下的卡顿风险。
- 若需要“文字还没生成完就开始说话”的更激进体验，可把首段 `split_chars` 调小（例如 12–18），代价是语句切分更碎。

> 2026-10-26 后续延迟优化（VAD 0.3s、首句早 flush、同音色垫音，第一声从“无”降到约 222ms）见 [通话等待优化](2026-10-26-call-latency.md)。
