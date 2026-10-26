# 已锁版本与证据

- Python 3.12.14（复用已有 uv 安装）；Node v24.21.0；pnpm 11.7.0。
- Python 直接依赖精确版本见 app/pyproject.toml，全部传递依赖见 app/uv.lock；JS 依赖见 pnpm-lock.yaml。
- PostgreSQL Windows 开发版：17.6，EDB 二进制包 postgresql-17.6-1-windows-x64-binaries.zip。
- 包下载 SHA256：d378882abd001a186735acd6f6ba716bca6ccd192e800412d4fd15ed25376b3e。此为本地记录哈希，并非厂商签名验证。
- 来源：[PostgreSQL Windows 官方入口](https://www.postgresql.org/download/windows/)、[EDB 二进制下载](https://www.enterprisedb.com/download-postgresql-binaries)。开发包只用于合成数据事务测试；部署版独立锁定。
- 协议 1.0：Pydantic 生成 JSON Schema，再由 json-schema-to-typescript 15.0.4 生成 TypeScript；pnpm contracts:check 检查漂移。
- 本地语音：Breeze-TTS-2.cpp v0.1.3 Windows x64 Vulkan；模型 `breeze-tts-2-q8_0.gguf` 3,568,844,480 bytes，SHA256 `A02BCC4B69B0601032727F8040C4942149B1B73AA0F69022FE5AAA6A8F0EF879`；运行包 73,939,562 bytes，SHA256 `D31FEEE5380E0609DC44B537D22CBE4F3CF8E93495756F3A580D8B448DB79F7F`。音色 `yuwan.breeze`（106 frames / 8.48s），参考音频 `data/voice/yuwan-reference.wav`。权重许可为研究/非商业用途，不随项目分发。
- 2026-10-26 语音模型升级：由 HuggingFace `BreezeBlue/Breeze-TTS-2` 原 BF16 权重经仓库 v0.1.3 转换脚本得到 F16 GGUF（5.85 GB），再量化成 **Q8_0 + F16 depth decoder**：`breeze-tts-2-q8_0-f16depth.gguf` 3,883,417,280 bytes，SHA256 `C1E13CD2A9D5818B3B33A5A4528FDB98B262BCC5958FE687EC0D8040CF1303AE`；实测显存 4461 MiB，首包约 557ms，RTF 0.97（旧 Q8_0 为 0.66）。但实时通话余量不足，**当前已切回原 `breeze-tts-2-q8_0.gguf`（RTF 0.66）**，F16-depth/纯转换 Q8_0/F16 文件保留用于回滚/A-B，详见 [语音模型升级记录](../development/2026-10-26-voice-model-f16depth.md)。
- 本地 ASR（2026-10-26）：sherpa-onnx 2.0.0；流式 Zipformer 中文 int8 负责实时 partial + VAD；GTCRN 在线降噪 `gtcrn_simple.onnx` 535,638 bytes，SHA256 `E77603AC0C23DAC3227DD2D7135B3A585CBEE2679048AECFA886657D3AE1B534`；每段结束时用 SenseVoice int8 最终重解码 `model.int8.onnx`（2025-09-09 包，237,115,547 bytes，SHA256 `12CA1A2AE7ECF3E0019EF2822307EE0B5CADC9196569E379B4C4026F8205276D`）。配置见 `config/asr.local.json`，取舍见 [降噪与识别准确率](../development/2026-10-26-asr-denoise-accuracy.md)。
- Letta/GPT-SoVITS/EverOS 尚未安装；SenseVoice ASR 与 BGE CPU 语义模型已部署，版本、权重与验收以各自开发记录为准。


## 2026-10-26 记忆层补齐

已部署本地 BGE 中文 CPU 语义检索、结构化事实更新与争议记录、固定与保留期、有来源的阶段摘要、预算及独立后台。迁移至 0013_memory_projection_compat，现有聊天/媒体保留。依据和验收边界见 [开发记录](../development/2026-10-26-memory-completion.md)。源码保留原分支未提交改动，没有混入旧快照分支。
