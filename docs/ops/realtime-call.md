# 实时语音通话

2026-10-27 接线复验：通话票据现在返回 call_id；前端用它提交 realtime 聊天，服务端只接受已连接且账号／窗口匹配的请求。挂断、断连、插话和全局 Stop 取消该通话未完成生成，普通聊天不受影响；通话期间暂停独处追话。当前验证和修复记录见 [功能联动审计](../development/2026-10-27-integration-audit.md)，下方旧日期数字只代表当时测试。

入口：打开 Nagi 对话，在输入区点击「通话」，允许浏览器使用麦克风。接通后持续收音、显示临时字幕，检测到一句话结束后发送最终文字；回复通过 Breeze PCM 流播放。说话可打断上一轮；可静音、取消静音或挂断。输入 Stop / 停止也会挂断并停止动作。

浏览器要求：支持 AudioWorklet 的现代 Chromium/Edge；本机使用 http://localhost:18440 或 http://127.0.0.1:18440。手机、局域网 IP 页面需要 HTTPS 安全上下文。首先使用耳机验证；扬声器效果依赖浏览器/设备回声消除，尚未实际验收。录音不默认落盘，临时字幕只在内存中，最终识别文字按普通聊天写入档案。

## 服务

- Gateway：18440，提供认证 `/api/v1/call/status`、`POST /api/v1/call/ticket` 和 `/api/v1/call/stream` WebSocket。
- ASR：127.0.0.1:8139；独立 `.devtools/asr-venv`，sherpa-onnx 2.0.0 + 中文流式 Zipformer INT8 + Silero VAD，CPU 两线程。仅 Gateway 用本地私有凭据连接。
- Breeze：127.0.0.1:8137，沿用当前音色，合成 PCM 到达即播放。
- 首次部署：`powershell -ExecutionPolicy Bypass -File scripts/setup-asr.ps1`。之后普通启动脚本自动启动 ASR；普通停止脚本停止 ASR；`-KeepVoice` 同时保留 ASR 和 Breeze。
- 模型默认位于 `%USERPROFILE%/Models/ASR-Zipformer/`；配置在 `config/asr.local.json`。`threads` 当前 2，`silence_seconds` 当前 0.4。缩短静音阈值会加快接话，也更容易在思考停顿时误断句。
- 版本：`scripts/requirements-asr.txt` 固定顶层依赖，`scripts/requirements-asr.lock.txt` 记录本机完整依赖。模型来自 sherpa-onnx 官方 asr-models Release。

## 通话与普通聊天

通话请求使用 `output.realtime=true, output.voice=off`，聊天的档案、人格、记忆和 SSE 仍沿用原实现；语音通过通话流合成，不创建普通语音消息附件。第一版通话优先即时回应，跳过需要整段等待的 Agent 工具规划。工具操作继续使用文字聊天。

已验证的官方 DeepSeek `deepseek-flash` / `deepseek-v4-pro` 通话请求设置 `thinking.type=disabled`，减少接话等待；普通聊天保持原设置。其他模型不自动注入厂商专用参数。依据：https://api-docs.deepseek.com/guides/thinking_mode/ 。

通话票据 30 秒、单次使用，在 WebSocket 首帧发送，避免凭据出现在 URL 日志。每账号最多一条通话。ASR 每帧 40ms，网关验证格式/有限数值/上传速率；TTS 使用有界 credit 流控。插话提升 epoch，停止当前播放和待播队列，丢弃迟到音频。挂断/断连会关闭识别连接，并由服务端取消属于该通话的未完成聊天生成；已经完成的档案保留。

## 验证记录与边界（2026-10-26）

- Python 定向 15 项通过（通话、工具路径回归、供应商契约），Node 通话相关 9 项通过，协议生成检查通过。
- 官方 5.61 秒中文音频实时分块识别成功，首条临时文字约 0.76 秒；静音 reset 后第二轮 final 编号为 2，未丢句。数据：`data/asr-benchmark.json`。
- 部署后的 Gateway WebSocket 真实识别通过；Breeze 首包 334ms，服务端取消确认 5ms。测试用公共音频与固定测试句，不读取麦克风或写入聊天。复测：独立 ASR 环境运行 `scripts/smoke-call.py`；数据：`data/call-smoke.json`。
- 官方 DeepSeek 短合成提示实测首字 480ms、完整一句 600ms、无 reasoning 字符；不代表带完整人格/记忆上下文时的延迟。数据：`data/call-model-benchmark.json`。
- 尚未完成真实麦克风/设备回声、自然停顿、噪声、多轮主观体验和长时间通话验收。上面的分项耗时不是端到端接话延迟保证。

## 延迟调整（2026-10-26）

- 断句静音阈值从 550ms 调为 400ms，麦克风帧从 80ms 缩短到 40ms。
- 仅实时通话 SSE 轮询从 250ms 调为 50ms；普通文字对话保持原值。
- 短完整句子立即进入合成；跳过过早的逗号后，仍可在后面的正常边界分段，避免被第一个短逗号一直堵住。第一段无标点文本的攒字上限从 40 字缩为 20 字，后续仍为 40 字。
- 同样的 40ms 输入节奏，在相同公共测试音频上比较：最终识别 5619ms → 5439ms，提前 180ms，文字一致，reset 后编号仍为 2。这是一次本机分项对比，不是完整通话延迟保证。
- 定向验证：Node 11 项、通话后端 6 项通过。原配置/代码备份：`.devtools/call-latency-before/`。原语音音色与合成分块配置保持不变。
