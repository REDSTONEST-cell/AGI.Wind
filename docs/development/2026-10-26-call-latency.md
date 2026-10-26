# 实时通话延迟优化（保持流式输出，不切片段）

日期：2026-10-26。目标：恢复流式输出体验、不把用户的话或回复切成碎片，同时尽量压低延迟。

## 采用的方式

- **输出保持流式**：模型文本按 token 持续喂给 Breeze WebSocket，TTS 连续出 PCM；不做客户端 8 字早 flush，Breeze `split_chars` 保持 24，按完整句子出声。
- **输入不切碎片**：VAD 0.3s 只检测停顿并通知客户端；服务端继续累积音频，0.9s 长静音才用 SenseVoice 对完整一句重新识别，避免把“鱼丸”这种短片段单独识别成乱码。
- **延迟锚点修正**：VAD 已经消耗的 0.3s 尾静音不再重复计入聚合窗口——修复前 final 实际要等 0.3+1.0=1.3~1.5s，现在约 **0.9s** 后出 final。
- **垫音更早**：同音色垫音在 `speech_end` 后 **150ms** 播放（约说完后 0.45s 出声），真实回复音频到达即停。
- **Breeze 预热**：通话接通就先建立 Breeze WebSocket 会话，首句不必再付握手延迟。
- **播放抖动缓冲**：首块 100ms、后续 60ms（原 220ms），减少首声延迟又保留抗抖动余量。
- TTS 模型仍为原 `Q8_0`（RTF 0.66、原音色）；ASR 的 GTCRN 降噪和 SenseVoice 重解码保留。

## 延迟分解（当前）

| 节点 | 时间 |
| --- | --- |
| 用户说完 → `speech_end`（可能说完了） | 约 0.3s |
| `speech_end` → 垫音第一声 | 约 0.15s（合计约 0.45s 有反馈） |
| 用户说完 → final（完整句识别） | 约 0.9s |
| final → 模型首 token | 约 0.5–0.7s（DeepSeek Flash） |
| 首 token → Breeze 首段 PCM | 约 0.25–0.35s |
| **真实回复第一声** | 约 **1.7–2.0s**（垫音期间不算干等） |

## 验证

- 独立 ASR WebSocket 回放 `output/fake-mic.wav`：0.9s 聚合后一次 final = `鱼丸你在吗我想听你说句话`，短片段不再单独出垃圾文本。
- 通话 WS 音频链路：首段 PCM 约 265ms，最大到达间隔 375ms，PASS。
- 假麦克风浏览器检查：垫音约 150–225ms 内出声、真实音频排期 0ms 间隙、无 JS 异常；Chrome 假音频设备有时在通话接通前就开始播放，会看到跨文件循环的额外 final，这不是线上麦克风行为。
- 全量测试 250 passed / 2 skipped。

## 备注：本地数据库迁移

排查这次“回复失败”时发现本地 `agi_wind_local` 数据库停在迁移 `0011_proactive`，而代码已需要 `0012_memory_completion`（`memory_items.subject_key` 等字段），导致记忆上下文查询失败。已先备份 `.tmp/agi_wind_local_before_0012.dump`，再执行 `alembic upgrade head` 到 `0012_memory_completion`；重启网关后回复恢复正常。以后更新代码后若出现 “column ... does not exist”，先检查 `alembic_version` 是否落后。
