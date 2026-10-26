# 语音模型升级：原权重转 F16 后做 Q8_0 + F16 depth

日期：2026-10-26。目标是在 8 GB 显存的 RTX 4060 Laptop 上，用原版权重提升一点音质，同时不显著恶化实时通话。

## 结论

- 部署模型：`C:/Users/wind/Models/Breeze-TTS-2/breeze-tts-2-q8_0-f16depth.gguf`（3.62 GB）
- 相比原 Q8_0（3.32 GB / 4322 MiB 显存）：显存 +139 MiB，实测 **4461 MiB（≈4.36 GB）**；整卡 5576/8188 MiB
- 音质侧：high-frequency（>6 kHz）能量占比 0.88% → 2.83%（同一段文本、seed 42、同一 instruction；这是亮度指标，不是听感结论）
- 代价：生成速度从 RTF 0.66 降到 **0.97**（仅比实时快约 3%）；同一段文本的音频时长 18.72s → 22.4s（F16 depth 改变了采样轨迹）；首包 484ms → 557ms
- 实时通话链路验证仍 PASS：通话 WS 首包 375ms，最大到达间隔 454ms
- 全量 F16（5.85 GB，预计约 7 GB 显存）没有部署：8 GB 卡加桌面占用风险过高；F32（约 11.8 GB）不可行

## 原始权重与转换

模型源：HuggingFace `BreezeBlue/Breeze-TTS-2`（BF16，safetensors 共约 7.12 GB，含 `audio_tokenizer/`）。
本地保存：`C:/Users/wind/Models/Breeze-TTS-2/original/Breeze-TTS-2/`。

用 Breeze-TTS-2.cpp **v0.1.3** 的转换器（与运行二进制同版本）：

```powershell
# 依赖只装在 .tmp/breeze-convert/libs，不污染项目 venv
$env:PYTHONPATH="$PWD\.tmp\breeze-convert\libs"
python .tmp\breeze-convert\convert_hf_to_gguf.py `
  .tmp\breeze-build\original\Breeze-TTS-2 `
  -o .tmp\breeze-build\breeze-tts-2-f16.gguf --dtype f16
# 输出 5.85 GB，等价原 BF16 精度

breeze-quantize.exe breeze-tts-2-f16.gguf breeze-tts-2-q8_0-f16depth.gguf q8_0 --depth f16
# 输出 3.62 GB：Q8_0 主体 + F16 depth decoder
```

转换器与 requirements 来自仓库 `v0.1.3` tag 的 `scripts/`（`convert_hf_to_gguf.py`、`convert_codec.py`、`requirements.txt`，依赖 numpy + gguf）。备注：Python 子进程在当前 DSH 会话里不能直接在 `C:\Users\wind\Models` 写文件，所以下载/转换先放在工作区 `.tmp/breeze-build/`，完成后用 PowerShell 移动到 Models 目录。

## 部署与回滚

- 配置文件：`config/voice.local.json` → `"model": ".../breeze-tts-2-q8_0-f16depth.gguf"`
- 旧配置备份：`.devtools/voice.local.before-f16depth-20261026.json`
- 重启语音：停止 `breeze-server` 后运行 `powershell -File scripts/start-voice.ps1`
- 回滚：把 `model` 改回 `C:/Users/wind/Models/Breeze-TTS-2/breeze-tts-2-q8_0.gguf` 并重启语音（旧 Q8_0 文件保留，RTF 0.66）
- 另有对照模型：`breeze-tts-2-q8_0-converted.gguf`（3.32 GB，由同一份 F16 转出，RTF/时长与旧 Q8_0 一致）

## A/B 样本

同一段文本、`voice_id=yuwan`、`instruction=温和、自然地说话`、`cfg_scale=1.0`、`seed=42`：

| 文件 | 模型 | 首包 | 时长 | RTF | >6 kHz |
| --- | --- | --- | --- | --- | --- |
| `output/voice-ab-before.wav` | 旧 Q8_0（预构建） | 484ms | 18.72s | 0.66 | 0.88% |
| `output/voice-ab-q8converted.wav` | 由 F16 重转的纯 Q8_0 | 439ms | 18.72s | 0.66 | — |
| `output/voice-ab-final.wav` | **Q8_0 + F16 depth** | 557ms | 22.40s | 0.97 | 2.83% |

注意：RTF 0.97 意味着合成只比实时播放快约 3%，在 GPU 被浏览器/游戏占用时可能跟不上播放；如果通话出现卡顿，优先回滚到 Q8_0。F16 depth 是否“更好听”需要人耳 A/B，以上只是工程与客观指标记录。
