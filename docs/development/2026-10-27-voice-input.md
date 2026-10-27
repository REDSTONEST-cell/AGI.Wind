# 聊天语音输入与麦克风选择（2026-10-27）

## 行为与接线

输入框图片按钮旁增加麦克风按钮：点击开始，再点结束；本地 ASR 的分句结果实时填回草稿，结束时确认最后一句，由用户检查后手动发送。保留原有草稿，不自动创建聊天请求。最长录音两分钟；录音期间输入框只读，切换页面/窗口、Stop、关闭页面均释放资源；迟到授权和转写不能污染新窗口。

复用现有鉴权 `/api/v1/call/ticket` 与 `/api/v1/call/stream`，只发送 PCM 和 ping，不发送 speak，也不调用聊天/模型接口。该段录音使用现有通话槽，开始时沿用通话的独处任务取消规则；通话和语音输入互斥。结束录音立即释放实体麦克风，再以实时静音帧等待最多 1.4 秒以取得 VAD 最终校正；断线保留已识别草稿。无新增依赖、模型或服务。

“麦克风设置”列出输入设备并在当前浏览器存储选择，语音输入与通话共用。无自选时显式选系统 default 设备（若浏览器枚举支持），避免浏览器自动挑选虚拟麦克风；设备移除回退默认。检测按钮只短暂打开后立即释放设备，权限拒绝、无设备、占用分别提示。

## 排查证据与边界

当前用户 Edge 页 `http://127.0.0.1:18440/#/chat` 为安全上下文、麦克风 granted。未指定 deviceId 时实际选择 Steam Streaming Microphone；Senary Audio 阵列实体设备可打开。页面已显式选择 Senary Audio 并记住此浏览器选择；未修改 Windows 全局默认设备。

19 项 Node 针对性检查通过：原有通话/插话 14 项，新语音输入 5 项（设备选择、分句去重、窗口隔离、结束时最终校正、迟到授权释放）。真实 Edge 验证按钮加载、实体设备检测、ASR 连接和结束/释放；无 JS 错误。未发送聊天或真实 QQ，未调用付费模型；实际说话准确率、声音大小和真人通话往返仍由用户试说验收。截图 `data/voice-input-ready.png` 为本机运行资产。

## 设计依据

沿用仓库已有 Web Audio/AudioWorklet 与 Zipformer ASR，无需另引框架。设备选择按 WebRTC 官方实践：https://webrtc.org/getting-started/media-devices 及 MDN https://developer.mozilla.org/en-US/docs/Web/API/MediaDevices/getUserMedia 使用枚举设备和 deviceId 约束；只参考设计，无外部源码复制。
