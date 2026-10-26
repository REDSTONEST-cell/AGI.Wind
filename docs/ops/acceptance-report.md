# 本地基础版本验收记录

2026-10-25，Asia/Shanghai。完整方案未完成，本记录不替代 A01—A20 全链路验收。

## v7 新增验收（旧基础记录保留）

- 完整 pytest：99 passed、1 skipped（默认跳过真实厂商）。PostgreSQL 随机 schema，不改业务会话。
- 单独启用 `test_visual_live.py`：1 passed，真实 DeepSeek `deepseek-flash`，合成图片的视觉观察、回答、反思全部完成；本轮实测真实 reasoning 为 629 字符，不把正文写进报告。
- `node scripts/verify-visual-client.mjs`：真实无头 Chromium，1440/1024/390，无横向溢出、0 JS 异常；模拟 SSE 及写端点；上传/粘贴/纯图片、前后处理轨迹、附件实际解码、历史、表情包 CRUD/lightbox、断线保留正文、52px 桌面头像和可拖日志宽度通过。
- `pnpm contracts:check`：Python schema 和生成 TypeScript 无漂移；新增事件类型已同步协议生成物。
- 独立源码审查发现的观察删除/取消边界、工具失败状态、SSE 断线部分正文、多图历史顺序，均有 RED→GREEN 回归；相关测试 15 passed。
- 业务库已做 pg_dump 备份，再应用仅新增表的 0008，Gateway 已重启。真实调用成功不代表语音、向量、Live2D、NAS 或全量 A01—A20 验收完成。
- 用户后续 UI 回归：自己的头像上传/保存/聊天展示/移除；头像重复重绘不叠图；裁剪非正方形双色图片，缩放与真实指针拖动后上传 512×512 PNG，中心像素与手工期望红色一致；页面刷新后日志包含完整历史思考与回复正文。
- 分段回复与输入框：5项 Node 规则测试通过；Chromium 验证标点分段/句首问叹号保留/图片独立条目/历史一致，40条历史在1440/1024/390下开启日志仍可点击输入、选图、发送。纯图片回复后断线仍显示失败说明，文件名/编号不会被英文点拆散。
- 表情包修复（2026-10-25）：完整 pytest `102 passed, 1 skipped`；新增文本回合发送库内表情包、未命中不发送、手动上传自动命名/标签 3 项测试。Chromium 脚本新增“离开再进入库页仍只有 1 张卡片”回归；真实配置下对已有未标签图片执行 autotag，返回“困困摸鱼”和 4 个标签后重新读取一致。纯文字“发给我”的真实厂商发送分支未在用户当前会话中实弹验证，证据来自受限编排集成测试。
- 消息出现动画：Chromium 断言实时气泡带 `.animate-in` 且动画时长 ≥0.2s；历史/归档不带动画类。分段按流式顺序逐条出现，新增气泡间隔约 700ms，避免“瞬间几条”；`prefers-reduced-motion` 禁用延迟。
- 日志升级：真实 Chromium 验证头像水印背景、头像徽章、等级/详细字段、搜索过滤与空结果、0 JS 异常；`memory.context` 新字段与生成链路由后端测试覆盖，写请求审计行只记录 method/path/status/耗时。
- 空回复恢复：浏览器 mock 验证事件流无终态时续传，仍无正文时从历史按 generation_id 补回正文并显示，日志出现恢复记录；真实模型/网络故障下的根因仍以现场日志为准。
- 归档永久删除：`DELETE /api/v1/conversations/{id}` 写删除账本，清事件、请求/生成、附件引用与派生记忆来源；生成中的对话 409 拒绝。service 测试覆盖，归档页 Chromium mock 验证无「显示已隐藏」、按钮为查看/恢复/删除且发出真实 DELETE；完整 pytest `124 passed, 1 skipped`。

## 本地语音回复验收（2026-10-25）

- **实现范围**：Breeze-TTS-2 Q8_0 + Vulkan，`yuwan` 音色；对话输入框语音开关（按设备）+ 概率浮层（服务端持久化）；独立语音气泡、播放/暂停/时长/状态；历史/归档回放；模型自报隐藏情绪标记 + 服务端剥离 + 缺失时受限语气导演兜底；`GET/PATCH /voice/settings`、`/voices`、`/voice/preview`、`/generations/{id}/speech`；生成流 `speech.queued`。复用 `speech_segments`，**无数据库迁移**。
- **完整 pytest**：`153 passed, 2 skipped`（默认跳过真实 GPU/vendor live）。新增 voice text/emotion/engine/API/reply/interrupt/stale 测试；覆盖隐藏情绪标记流式剥离、标签缺失导演兜底、always/auto/off、概率 0/1、TTS 失败、Breeze 不可达、打断取消、历史音频附件、预览与设置持久化。
- **协议**：`node scripts/generate-contracts.mjs` 后 `node scripts/check-contracts.mjs` 通过；`OutputOptions.voice` 与 `StreamEvent.type=speech.queued` 已进入 schema/TS。
- **真实 Breeze**：`POST /api/v1/voice/preview` 200、`audio/wav`、24000 Hz；5.36 秒音频总耗时 3.80 秒、RTF 0.71、首包 434 ms。旧导演链路真实 DeepSeek 聊天回合产生 12.96 秒语音（情绪“惊讶”）。新自报标记链路 live test 通过：`emotion_source=model_tag`、5.76 秒语音、正文不含 `[情绪:`；`speech_segments.state=ready`，事件 attachments 含音频 asset，`/speech` 与历史一致。
- **无头 Chromium**：`scripts/verify-voice-client.mjs` 验证开关 `aria-checked`/localStorage、概率浮层 Escape 关闭、模型页 TTS 面板显示“可用 · 24000Hz”、`yuwan` 可选；并从真实历史中渲染出 1 条语音气泡（0:12，未失败），0 JS 异常；截图 `.devtools/voice-controls-live.png`、`.devtools/voice-chat-live.png`。未通过浏览器发送真实聊天（避免写入用户对话），聊天链路由上面的真实 API/服务测试覆盖。
- **运维**：`start-local.ps1` 自动复用健康 Breeze 或隐藏启动，输出 `Voice running`/`Voice id: yuwan`；`status-local.ps1` 显示 Voice ready；`stop-local.ps1 -KeepVoice` 保留语音进程。启动/停止脚本未在验收中反复杀进程，实际复用与状态读取已验证。
- **边界**：一回复一语音，非逐句流式；400 字以上读节选；不做 ASR/麦克风/全双工/Live2D 口型；`.breeze` 与参考音频不进归档包，NAS 迁移需单独备份；Breeze 权重研究/非商业用途。

## 长期记忆阶段一验收（2026-10-25）

- 依据桌面交接 `05-记忆系统开发要求` 与 `06-长期记忆详细设计`；先审计现有 memory/archive/identity/jobs 能力，缺口表见 [2026-10-25-long-term-memory-phase1.md](../development/2026-10-25-long-term-memory-phase1.md)。
- 完整 pytest：**123 passed, 1 skipped**（新增 21 项：提取/治理 15 项 + 生命周期 5 项 + 记忆设置持久化 1 项），记忆页面无头 Chromium 回归通过。
- 迁移 `0009_memory_extraction`：在临时 schema 中先升级到 head，再删除新增列/索引并把版本回退到 `0008_reasoning_stickers`，重新 upgrade 成功补列建索引；旧库路径已验证。未触碰业务库会话数据。
- 控制闭环已验证：`generation.completed` 入队 → 短事务读游标 → 事务外提炼 → 短事务复核租约/游标/memory_revision/来源可见性与 content_sha256 → 写入或隔离 → 推进游标。错误响应只返回可读错误码，不写入未证明候选。
- 记忆管理页面（2026-10-25）：列表/搜索/类型状态筛选、详情来源 quote、纠正来源下拉、隐藏/恢复、提炼任务状态与“立即提炼一批”；新增 `PATCH /api/v1/memory/items/{id}` 与 `POST /api/v1/memory/extractions`。完整 pytest `123 passed, 1 skipped`；`scripts/verify-memory-client.mjs` 无头 Chromium + mock 记忆 API 通过（列表、详情、纠正弹层、隐藏、筛选、立即提炼，0 JS 异常），截图 `.devtools/memory-client-1440.png`。隐藏/替代/争议同内容不会被再次提炼复活（ignored）。
- 记忆生命周期（2026-10-25，借鉴 LivingMemory 设计但未复制 AGPL 代码）：迁移 0010、类型化 TTL/importance/decay、访问强化、到期维护、召回测试、重要度标记与生命周期 UI。完整 pytest `123 passed, 1 skipped`；Chromium 新增生命周期行、排序、召回测试、维护、重要度断言，0 JS 异常。用户手动触发的真实提炼批次 completed（8 事件、0 候选），尚无真实结构记忆可做质量验收。
- 记忆概览与设置（2026-10-25）：指标条、重要性/类型分布、自动提炼/维护设置、`GET /api/v1/memory/stats` 与 `GET/PATCH /api/v1/memory/settings`；设置持久化到 `data/settings.json` 的 memory 段，保存后热生效。Chromium 验证指标、分布、设置读取/保存、向量入口，0 JS 异常。
- 结构化召回新增“来源事件原文匹配”，纠正后当前事实替换且旧记忆不进入当前命中；这些证据来自 synthetic 数据与 fake extractor/provider。
- 未验证：真实厂商提炼质量、中文质量集、每日费用上限、时间有效期/冲突/变更历史、独立 worker、embedding/混合检索/图谱；本阶段不能声称完整长期记忆已交付。

## 实际证据

| 检查 | 结果 | 边界 |
|---|---|---|
| Python 完整 pytest | 62 passed，0 failed | PostgreSQL 真实临时 schema，厂商/视觉合成响应 |
| 浏览器主页 / | HTTP 200，状态展示/刷新按钮通过 | 模型未配置时仍能打开，健康接口保持真实 503；截图 output/local-service-homepage.png |
| pnpm contracts:check | 协议无漂移 | Python/JSON Schema/生成 TS |
| TypeScript strict noEmit | exit 0 | 契约编译，无 SDK 客户端验收 |
| scripts/smoke-local.py | 通过 | 实际 HTTP，data/local-smoke/report.json |
| /health/live | HTTP 200 | 仅进程存活 |
| 未认证 capabilities | HTTP 401 | 鉴权 |
| /health/ready | HTTP 503 degraded | DB/媒体可用，模型/语音未配置 |
| 重复 request_id | 同一 generation_id | 原始记录/生成任务不重复 |
| 未配置模型 | failed / MODEL_UNCONFIGURED | 输入仍可检索，无真实厂商调用 |
| 导出与 CLI verify | 计数/哈希通过 | 基础档案实体，声音/头像包尚未实现 |
| 空 PostgreSQL schema 导入 | 身份 ID、原文哈希、媒体字节一致 | 无 Letta 私有目录依赖 |
| 旧包 + 最新墓碑 / clone | 已删源记录未复活 | 合成数据，异盘账本尚未验收 |
| Windows PowerShell 5.1 | 停止 Gateway/DB 再启动通过 | 记录/人格保留，非 OS 冷重启 |

## A01—A20 边界

| ID | 当前证据 | 待完成 |
|---|---|---|
| A01 | 正常重启保留记录/人格 | OS 冷重启真实续聊 |
| A02 | 换模型角色不变、拒绝错误供应商路由 | 两个真实模型引用共同经历 |
| A03 | 同维度 fingerprint 拒绝混用 | BGE/pgvector 索引空间 |
| A04 | 关键词重建不改原文/人格 | 真实向量删除/重建 |
| A05 | 基础档案导入空 schema | 声音/头像完整包和异机恢复 |
| A06 | 并发幂等/SSE 重放测试 | 正式客户端重连和播放 |
| A07 | 纠错保留历史、优先有效事实 | 真实模型来源表达 |
| A08 | 无出处/AI 伪用户事实隔离；阶段一接入异步提炼、quote 唯一校验、游标幂等与租约复核 | 真实模型提炼质量与每日预算未验收；时间语义/冲突状态未实现 |
| A09 | 原图/合成观察/引用归档 | 真实视觉模型 |
| A10 | 本地 Breeze TTS + `yuwan` 已接入，真实合成与聊天语音通过 | 远端 TTS、逐句流式、解码/GPU 压力与长时稳定 |
| A11 | 待素材与实施 | 微调权重/清单/用户试听 |
| A12 | 打断 epoch/幂等控制测试 | 真音频/口型/时延 |
| A13 | 并发单任务/共享人格 | 两设备真实播放回执 |
| A14 | Live2D 原件保留 | SDK/未知动作回落 |
| A15 | 文字失败保留 partial；TTS 失败/不可达只影响语音并保留正文 | 真实显存 OOM 与长时压力 |
| A16 | 账号/会话/媒体/检索隔离 | 正式客户端验证 |
| A17 | 未实施 | 训练/在线互斥与回滚 |
| A18 | 配置默认关闭 | 主动调度/安静时段 |
| A19 | 未实施 | ASR/原音频/转写修正 |
| A20 | 基础 fsync/hash 链、旧包防复活、缺账本隔离 | 外部副本物理清理、异盘账本/备份轮换 |

## 复核

按交接串行实施，作者自查，未做独立人员审计。6 项新增失败用例捕获供应商路由、媒体限额、缺账本激活、媒体 ID 冲突及 HTTP 异常脱敏，修复后完整测试通过。无真实厂商/GPU/语音/客户端/用户试听结果。

未初始化 Git 或提交旧资产；源码/锁文件/文件清单用于本轮代码恢复。任务状态、取舍和待条件见 progress.md。

## Agent 分支验证（2026-10-25）

隔离分支codex/agent-capabilities：基线147 passed/1 skipped；初次完整功能回归174 passed/1 skipped。Chromium管理页保存/密钥处理/刷新/1440与390宽度通过，0 JS异常；Node分段5项通过。独立审查的删除副本与并发PATCH问题均有失败复现和修复后通过的回归。真实聊天厂商、搜索服务以及与DeepSeek最终开发成果合并后的运行验收尚未进行。

修复后最终完整回归：176 passed, 1 skipped，68.42秒。AOCI维护已尝试但stopped/blocked，仍未对齐；按用户节省额度要求停止额外治理循环，保留明确待办。与DeepSeek最终版本的集成和真实模型/搜索验收仍待完成。

主工作区源码整合后：183 passed, 2 skipped，56.38秒；协议检查与5项分段测试通过。独立测试Gateway上的Agent管理页、语音客户端（含合成历史音频）、图片客户端验证通过，0 JS异常。Agent＋语音组合回归验证情绪标记不进入正文/SSE文本且合成沿用情绪。未重启真实Gateway/Breeze，真实模型/搜索未实测，AOCI仍未对齐；源码整合不等于上线。


## 2026-10-26 记忆层补齐

已部署本地 BGE 中文 CPU 语义检索、结构化事实更新与争议记录、固定与保留期、有来源的阶段摘要、预算及独立后台。迁移至 0013_memory_projection_compat，现有聊天/媒体保留。依据和验收边界见 [开发记录](../development/2026-10-26-memory-completion.md)。源码保留原分支未提交改动，没有混入旧快照分支。

最终验收：记忆专项 96 passed / 1 skipped；整套 313 passed / 3 skipped / 1 failed，失败为既有通话会话接口与测试不一致（缺 call_id），详见开发记录。真实历史待提炼已清零；真实今日摘要 32 个来源、写入 2 条；语义与独立后台运行中，HTTPS 200。
