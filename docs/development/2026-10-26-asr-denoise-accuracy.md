# 通话麦克风降噪与语音识别准确率

日期：2026-10-26。目标是解决“我这边环境噪音”和“语音识别非常不准确”两件事，同时保持音色不变、通话仍可实时接话。

## 原状

- 浏览器采集已经开启 `echoCancellation` / `noiseSuppression` / `autoGainControl`（WebRTC 自带降噪），但对风扇、键盘、环境人声这类噪声能力有限。
- ASR 只用流式 Zipformer（中文 int8，greedy_search），没有额外降噪；VAD 和识别共用原始音频，噪音容易触发假端点。
- 流式识别会在自然停顿处提前切句，客户端把每个 `final` 当一轮用户发言，导致一句话被拆成多次回复、识别文本变碎。

## 改动

1. **服务端流式降噪 GTCRN**：ASR 侧车在识别/VAD 之前先跑 sherpa-onnx 2.0 的 `OnlineSpeechDenoiser`（GTCRN，523 KB）。降噪后的音频同时驱动流式识别、VAD 和最终重解码。
   - 配置：`config/asr.local.json` 的 `denoise: true` / `denoiser_model`。
   - 代码：`scripts/asr_server.py` 的 `create_denoiser()`、`RecognizerSession.enhance()`。
2. **SenseVoice 最终重解码**：流式 Zipformer 继续负责实时字幕（partial）；一个完整 utterance（等 0.9s 长静音）结束时，用 SenseVoice int8 对整段音频重新识别，作为最终文本，带 ITN 和标点。
   - 模型：`C:\Users\wind\Models\ASR-SenseVoice\sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2025-09-09\model.int8.onnx`（226 MB，SHA256 `12CA1A2A…5276D`）。
   - 代码：`create_offline_recognizer()`、`RecognizerSession.transcribe()`；单段解码耗时约 75–150ms；失败自动回退流式文本。
3. **碎句恶性 bug 修复**：VAD `silence_seconds` 0.3 只发 `speech_end` 提示；`utterance_silence: 0.9`，短停顿期间保持同一个流式识别流和完整音频缓冲，只有 0.9s 长静音才提交 final。**客户端 600ms 合并和“8 个字早 flush”已删除**，改由服务端保证一句完整的话只产生一个 final。
4. **Breeze 回到 Q8_0**：实时通话优先，模型从 Q8_0+F16 depth 切回原 `breeze-tts-2-q8_0.gguf`（RTF 0.66，原音色）。F16-depth 文件保留备用。
5. 通话播放端给合成音频首块 100ms 抖动缓冲（后续 60ms），减少近实时合成下的播放断缝；Breeze `split_chars` 保持 24，不再把一句话提前切开。

## 恶性碎句 bug 的根因与修复

用户反馈“根本听不清楚一句话，感觉像是在乱说”。复现结果：

- 假麦克风音频原文：`鱼丸你在吗我想听你说句话`
- VAD 在自然停顿处把它切成 3 段，SenseVoice 对**很短的单段**分别解码得到：`YOU啊` / `你在吗` / `我想听你说句话`
- 客户端又把三段拼成 `YOU啊，你在吗，我想听你说句话` 发给模型，模型只能对着乱码回答

修复：服务端把短停顿后的音频继续累积，等长静音才用 SenseVoice 对完整 utterance 解码。同一段音频现在 final = **`鱼丸你在吗我想听你说句话`**；同时去掉了客户端的 600ms 拼接和 8 字早 flush（后者会把 TTS 一句话切碎，声音发断）。

## 实测

同一段 18.7s 中文文本（TTS 生成，已知原文）A/B：

| 场景 | 流式识别（旧最终文本） | 新管线（GTCRN + SenseVoice 最终文本） |
| --- | --- | --- |
| 干净音频 | CER ≈ 2.3% | **CER ≈ 1.1%**，带标点 |
| 10 dB 白噪声 | CER ≈ 2.3% | CER ≈ 2.3% |
| 5 dB 白噪声 | CER ≈ 2.3% | CER ≈ 3.4% |
| 0 dB 白噪声 | CER ≈ 6.8% | CER ≈ 2.3% |

说明：这是脚本把音频快速灌入 ASR 的测试，和实时麦克风链路不完全等价，数值只用于方向性比较；干净/低噪下新最终文本明显更完整、带标点。关闭 GTCRN 时，噪声场景出现过 VAD 不结束、无 final 的情况，因此保留降噪。

独立回放 `output/fake-mic.wav`：0.9s 聚合后一次 final = `鱼丸你在吗我想听你说句话`，短片段不再单独出垃圾文本。

## 延迟

- 用户说完 → `speech_end` 约 0.3s，垫音约 0.45s 出声。
- 用户说完 → final 约 0.9s；final 后 LLM 首 token 约 0.5–0.7s，TTS 首包约 0.25–0.35s。
- 真实回复第一声约 1.7–2.0s，等待段由同音色垫音覆盖。

## 回滚

- 关降噪：`config/asr.local.json` 改 `"denoise": false`，重启 ASR。
- 关 SenseVoice 重解码：删除 `offline` 段，重启 ASR。
- 恢复更快端点：`silence_seconds` 调小、`utterance_silence` 调小（会更早 final，但碎句风险回来）。
- 恢复 F16-depth 音质模型：`config/voice.local.json` 的 `model` 指回 `breeze-tts-2-q8_0-f16depth.gguf`，重启 Breeze；注意 RTF 0.97 下通话余量很小。

## 文件

- `config/asr.local.json`、`scripts/asr_server.py`
- `app/src/agi_wind/api/client/realtime-call.js`、`call-audio.js`
- `scripts/make-voice-fillers.py`（垫音）
- 模型：`C:\Users\wind\Models\ASR-Zipformer\gtcrn_simple.onnx`（SHA256 `E77603AC…1B534`）、`C:\Users\wind\Models\ASR-SenseVoice\…-2025-09-09\`
