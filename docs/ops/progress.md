# AGI.Wind 实施进度

计划：docs/superpowers/plans/2026-10-25-agi-wind-companion-plan.md
更新：2026-10-25，Asia/Shanghai。

## 当前阶段

用户最新顺序：**先 Windows 本地部署服务端并测试，测试完成后再部署 NAS**。当前交付本地服务端基础版本与可用的陪伴客户端；完整 T01—T15 尚未完成。

- Gateway：127.0.0.1:18440，app/.venv 独立环境；`--host`/`-BindHost` 可切到 0.0.0.0 供局域网客户端连接。
- 参考客户端：http://127.0.0.1:18440/（单会话聊天 + 设置 + 归档），状态页在 /status；同源用 HttpOnly+SameSite=Strict 会话 cookie + CSRF，独立客户端用 Bearer 客户端密钥 + CORS 白名单。
- PostgreSQL 17.6：127.0.0.1:55432，data/dev-postgres；业务库 agi_wind_local，测试库 agi_wind_test。不注册系统服务。
- 角色明确标记为开发测试；无正式人格、厂商凭据或声音素材。
- 本地开发显式使用 direct_vendor，已配置 DeepSeek 对话模型；Letta 未接入，不自动回退、不标为可用。
- readiness 为 ready：DB/媒体/对话模型/本地 Breeze 语音可用；向量仍为 keyword_only。
- NAS 只做 SSH 环境读取，未上传/安装/启动本项目；SSH 已关闭，密码未写项目文件。
- 开发文档：接手至 2026-10-25 的实际开发记录见 [2026-10-25-development-record.md](../development/2026-10-25-development-record.md)；长期记忆阶段一的能力审计与实现见 [2026-10-25-long-term-memory-phase1.md](../development/2026-10-25-long-term-memory-phase1.md)；下一功能“思维链 + 图片收发 + 表情包库”的设计与实施顺序见 [2026-10-25-reasoning-images-stickers-plan.md](../superpowers/plans/2026-10-25-reasoning-images-stickers-plan.md)。
- 长期记忆阶段一已接入：聊天完成后异步提炼、来源引文校验、崩溃/重试幂等、游标与租约复核、当前事实检索与纠正；没有时间有效期/冲突状态/记忆管理页面，不能称为完整长期记忆。

## 本轮新增（2026-10-25，客户端 v5）

- **单会话 + 归档**：`conversations.state`（active/archived/hidden）；每个角色最多一个 active，创建新对话时自动归档旧对话；恢复旧对话会自动归档当前 active；归档会话事件只读，聊天请求返回 `CONVERSATION_NOT_ACTIVE`。迁移 `0007_conversation_lifecycle` 为旧库补列并把多余 active 归档，归档格式版本升到 2（旧 v1 包仍可导入）。
- **页面内会话菜单**：侧栏只显示当前对话卡片，「⋯」菜单提供重命名与归档并开始新对话；重命名、隐藏、删除确认全部用页面内 modal，不再依赖浏览器 `prompt/confirm`。
- **对话归档页**：查看只读历史、恢复为当前对话、永久删除；删除会清除事件、附件引用与由它产生的记忆来源，记录不可恢复。
- **模型六能力 Tab**：计划/对话、语音转文字、文字转语音、嵌入、重排序、识图；提供商源列表 + 详情面板（Base URL、模型 ID、API Key、测试、删除）。`ModelRoles` 增加 asr/tts/rerank，provider capabilities 扩展；只有 dialogue/vision 已接入，其余显示“待接入”。
- **服务器/客户端分离**：`client_key`（默认 123456）、`host`、`allowed_origins` 设置；`POST /api/v1/client/key-session`（同源 cookie，每 IP 失败限速）；Bearer 同时接受访问令牌与客户端密钥；CORS 仅放行白名单；`serve --host` 与 `启动服务端(局域网).cmd`；连接页只保留服务器地址、密钥、连接/断开与状态。已用独立静态源 :18441 无头验证跨域 Bearer 客户端与刷新后重连。
- **CONTEXT_TOO_LONG 修复**：预算从写死 12000 字节改为 `context_budget_bytes=48000`、`input_budget_bytes=12000`（可配置），人格+输入超限才拒绝；客户端检测旧格式重复长文本并提示删除；失败气泡改为可操作说明 + 跳转灵魂档案，不再显示“模型没有返回文字”误导文案；真实 DeepSeek 对话复测通过。
- **身份显示**：头像与名字在气泡外，右侧用户名来自“怎么称呼你”；未设置称呼时不重复显示“我”。
- **UI v6 控制台式重做**：固定 234px 侧栏（陪伴 / 她的档案 / 系统三组）、76px 顶栏（面包屑/服务状态/主题/刷新）、七个独立页面与 `#/chat` 哈希路由；页面标题 + 指标条（归档/连接/档案）+ 卡片/表格/状态胶囊/粘性保存栏；对话页保持单一窗口并移除指标行与输入栏旁说明文字；灵魂档案移除版本显示；新增右侧**终端式系统日志**（Gateway stdout/stderr 实时增量 + 页面活动混排，深色等宽、跟随/暂停、筛选、清空、桌面并排/移动全屏）；头像上传改为原生文件输入覆盖层，避免程序化 click 被浏览器拦截；浅色/深色/跟随系统主题；1024 与 390 响应式。视觉参考 qq-bridge 控制台（MIT，仅重建设计令牌与布局，未复制其页面/脚本）。新增 `GET /api/v1/system-logs`（脱敏增量读取）与 Gateway 结构化运行日志。
- 测试：82 项通过（新增会话生命周期、客户端密钥/限速/CORS、六能力角色、长人格预算、系统日志脱敏与增量偏移）；`scripts/smoke-client.py` 增加单活跃、key-session、capabilities 校验；新增 `.devtools/verify-v5.mjs`、`verify-standalone.mjs`、`verify-ui-v6.mjs`、`verify-avatar-v6.mjs`。

## 已通过检查

- **长期记忆阶段一（2026-10-25）**：完整 pytest `123 passed, 1 skipped`（新增 21 项记忆测试）；0009/0010 迁移在临时 schema 模拟旧库升级成功；不调用真实厂商，提炼质量控制流程与来源校验已验证，提炼质量本身未评测。
- **本地语音回复（2026-10-25）**：完整 pytest `153 passed, 2 skipped`；协议生成物与 Python schema 无漂移；真实 Breeze 合成/真实聊天语音、无头 Chromium 设置面板与开关均已验证；语音复用 `speech_segments`，无数据库迁移。

- 82 项 Python 测试通过；数据库集成使用真实 PostgreSQL 的随机独立临时 schema，测试后清理，没有 SQLite 替代。
- 本机自动登录：回环来源且 Host 为 127.0.0.1/localhost 时自动签发 HttpOnly 会话，无需粘贴令牌；非回环（NAS/反代）要求客户端密钥，且用 Host 校验防 DNS rebinding。无头 Chromium 新 profile 实测直接进入聊天页、令牌框不可见、发消息得到回复。
- 客户端验收：单会话/归档/恢复/隐藏状态机、客户端密钥（cookie 与 Bearer）、失败限速、CORS 白名单、六能力角色与长人格预算均有后端测试；`scripts/smoke-client.py` 对运行中 Gateway 实测通过（bootstrap、key-session、单活跃、capabilities=2、伴侣/人格/设置、重命名、CSRF、退出）。
- 无头 Chromium 实测 v5：页面只有一段进行中的对话，⋯ 菜单、重命名 modal、归档列表/只读回看、六个模型 Tab、连接页服务器地址与密钥、灵魂档案体积提示均正常；0 JS 报错。截图见 `.devtools/v5-*.png`。
- 无头 Chromium 实测 v6：1440、1024、390 三档布局；七个页面路由与面包屑、活跃导航、四格指标、会话菜单/重命名 modal、归档表格与只读回看、删除操作、刷新按钮、六个能力卡、连接指标、浅色/深色/跟随系统主题、模拟 SSE 回复、右侧日志抽屉（桌面/移动、筛选计数、错误 0）均通过；0 JS 报错。截图见 `.devtools/v6-*.png`。
- 无头 Chromium 实测独立客户端：从 :18441 静态源用 `http://127.0.0.1:18440` + 默认密钥连接，CORS 预检、Bearer 请求、跨域头像 blob、刷新后自动重连均通过；截图见 `.devtools/v5-standalone-*.png`。
- 修复 `CONTEXT_TOO_LONG`：默认预算 48000 字节（可配置），真实 DeepSeek 对话复测完成并返回流式回答；失败提示改为可操作文案并跳转灵魂档案。
- 修复客户端流式不显示的根因：SSE 帧数据是完整 StreamEvent 信封，文本在 `data.payload.text`，旧代码读取 `data.text` 导致增量全部丢弃，只能在刷新后从历史看到回答。
- 网页入口回归：客户端主页与 /status 状态页均 HTTP 200；模型已配置时 `/health/ready` 返回 200 ready。
- 协议生成/漂移检查、生成 TS 严格编译通过（历史记录，未在本轮重跑）。
- 本机实际 HTTP：存活、鉴权、事务接收、重复请求复用任务、未配置模型的明确失败、关键词检索、导出/校验通过。
- Gateway + PostgreSQL 正常停止、重新启动、保留原始记录/人格的演练通过；CMD 所用 Windows PowerShell 5.1 实测。
- 测试中导出到第二个空 PostgreSQL schema：身份 ID、原文内容哈希、媒体字节一致，不依赖 Letta 私有目录。
- 最新账本阻止旧包/克隆恢复已删源记录；缺账本只恢复为 recovery_quarantine；未共享媒体清理/重试垃圾回收/访问隔离已覆盖。
- 原有 tools/research/Live2D 保留。详情与证据见 acceptance-report.md。

## 各任务实际状态

| 任务 | 已实现/通过 | 未完成 |
|---|---|---|
| T01 | 配置/DTO/共享协议/精确依赖锁 | 后续 adapter 的专用 fixtures 随模块补充 |
| T02 | 档案、幂等事务、媒体、scope | NAS 耐久性与磁盘边界另测 |
| T03 | 稳定身份、人格历史、模型绑定、关系/承诺读取 | 正式人格与投影 UI |
| T04 | 增量切片、中文关键词/精确检索、预算、纠错；阶段一增加来源原文匹配与列表/详情 API | BGE CPU 实测、pgvector、混合向量检索与空间迁移；时间有效期/冲突/记忆页面未实现，不能标完成 |
| T05 | 鉴权 HTTP、文字 SSE/控制流、厂商与视觉离线测试 | 真实厂商、兼容端点和完整客户端接口 |
| T06 | 待实施 | Letta bridge、SDK 锁版与能力探针 |
| T07 | outbox 抢占/租约/重试、来源候选校验；阶段一接入提炼触发、游标幂等、严格 quote 校验与 gateway worker 分发 | 每日费用上限、独立 worker 常驻、摘要/自传整理未实现；worker 仍只随 Gateway 运行 |
| T08 | 基础导出/空库恢复、clone 来源映射、关键词重建、删除账本 | pg_dump/轮换、replace、声音/头像包、并行向量迁移、真实异盘账本 |
| T09—T14 | 参考客户端文字链路已交付（T11 部分）：单会话、归档、六能力模型设置、服务器/客户端分离、SSE 增量、打断、人格/记忆界面、服务端密钥与模型配置（掩码回显、热加载、端点探测）；本地 Breeze TTS、语音开关/概率/情绪与音频播放已接入 | ASR 麦克风、Live2D 驱动、声音训练、EverOS、主动互动；独立客户端的安装包/离线包尚未提供 |
| T15 | 本地部署与基础 HTTP 演练、客户端冒烟通过 | 全链路 A01—A20、性能和 NAS 部署 |

## 决策记录

- Ruling: 非 Git 目录原地建立 app，不创建工作树或纳入旧大工具/资产。成本：无提交级历史，使用源码文件/进度/源码包恢复。
- Ruling: docs/ops/progress.md 为执行账本，跳过依赖 Git/Bash 的 SDD 脚本。成本：无脚本生成的提交范围证据。
- Ruling: 优先可验证数据与本地 API 基础，未完成项逐项记录。成本：此版本不是完整 AI 伴侣。
- Ruling: 本地开发显式 direct_vendor，不自动 fallback，Letta 后续接入。成本：Letta 行为无实测。
- Ruling: 自动事实只接受可逐字验证的抽取，无出处/不受原文支持的改写隔离。成本：记忆写入较保守，推断流程后续补齐。
- Ruling: 暂用字节预算上界和关键词检索，不伪称向量语义检索。成本：同义表达召回有限。
- Ruling: 不开放覆盖恢复；缺最新账本的角色不可查询。成本：恢复需要明确模式/账本输入。
- Ruling: 用户最新要求本地优先，NAS 不作写入，不停止既有 GPU 服务。
- Ruling: 浏览器鉴权采用“Bearer→一次性票据→HttpOnly+SameSite=Strict cookie+CSRF”的桥接，长期令牌不落 localStorage；参考实现参照 LibreChat（MIT）的会话模式，不复制代码。成本：浏览器会话存进程内，Gateway 重启需重新登录。
- Ruling: 本机回环访问自动建立浏览器会话（`local_auto_login`，默认开），令牌输入只在非回环访问时作为兜底；同时校验来源地址与 Host 头，避免恶意站点借 DNS rebinding 拿到本机会话。成本：Gateway 若暴露到非回环网络，必须保持令牌登录。
- Ruling: 参考客户端按 AstrBot（AGPL-v3 + EULA）的信息架构与视觉语言重做：左侧导航五页、白卡片/圆角/紫罗兰强调色、深色模式、会话筛选与重命名；仅借鉴设计，不复制其代码，也不引入其 Vuetify/AGPL 依赖。
- Ruling: 2026-10-25 UI v6 改为参考 qq-bridge 控制台（MIT）的视觉语言：蓝色强调、234px 分组侧栏、76px 顶栏、页面标题/指标/卡片/表格/状态胶囊/粘性保存栏、浅深与跟随系统主题、哈希路由。仅重建设计令牌与布局，不复制其单文件控制台 DOM/JS；AGI.Wind 保持陪伴客户端而非十页管理台。成本：需要维护更大的 CSS/路由代码。
- Ruling: 客户端为无构建步骤的原生 ES module 页面，由 Gateway 同源托管；对照调研后不引入 Next/Svelte/Tauri 框架。成本：无组件生态，后续复杂交互需自行维护。
- Ruling: 服务商密钥只存服务端 `data/secrets.json`（600，Git 忽略），GET 仅回 configured/last4；配置写 `data/settings.json` 并热加载 ProviderRegistry。成本：本地文件为明文存储，NAS 部署前需改加密/系统钥匙串。
- Ruling: 会话模型采用“单活跃 + 旧对话只读归档”，每个角色最多一个 active；删除=仅从界面隐藏，归档记录永不硬删除，恢复旧对话会自动归档当前 active。成本：不保留多会话并行能力，切换靠归档/恢复。
- Ruling: 服务器与客户端分离采用“客户端密钥”：同源走 key-session cookie+CSRF，跨域走每请求 Bearer + CORS 白名单；密钥默认 123456 仅为内网调试，绑定非回环时启动日志警告。成本：局域网明文 HTTP 下密钥可被嗅探，正式部署需 HTTPS/改密钥。
- Ruling: 模型设置按能力 Tab 展示，未接适配器的 asr/tts/rerank/embedding 只保存配置并标注“待接入”，不伪造可用。成本：用户可先录入端点，但语音/向量功能仍不可用。
- Ruling: 上下文预算改为可配置字节上界（默认 48000/12000），不为长人格自动截断或摘要；超限时明确报错并引导精简档案。成本：极长人格仍需用户手工删减。
- Final review: 按交接串行实施，作者自查；新增 6 项失败用例，修复错误供应商路由、正确路由解析、图像独立限额、缺账本误激活、导入媒体冲突及 HTTP 数据库异常脱敏；全部 RED→GREEN，完整 61/61。未做独立人员审计。

## 下一任务与待输入

### v7 接手实施（2026-10-25）

- 执行依据：`2026-10-25-reasoning-images-stickers-plan.md` v7，沿用已批准设计；使用当前 checkout 的 `codex/v7-visual-stickers` 分支，保留全部接手前未提交改动。
- 基线：在 app/ 执行 `.venv/Scripts/python.exe -m pytest -q -p no:cacheprovider`，82 passed。
- 数据阶段：新增表情包迁移 0008、角色隔离、手动名称保护、档案 v3 和旧 v1/v2 导入、删除引用保护；新增 3 项测试 RED→GREEN。
- 生成阶段：ProviderChunk 分离 reasoning/text、真实 trace/SSE、两阶段图片编排与受限 JSON；新增及相关测试 12 passed；阶段全套 92 passed。
- Ruling: 沿用本 checkout 与既有 progress 账本，不复制/提交他人未提交成果；仅创建特性分支。成本：v6 与 v7 尚共享未提交工作树，交付时须明确变更范围。
- Ruling: v7 的 tool.failed 统一落为 tool.completed(status=failed)，补充 skipped 状态；reflect 本身也记录工具边界。成本：消费方须识别 failed/skipped，不能只看事件名。
- Ruling: 多图观察全部图片，正常 save/mimic 作用于第一张，反思失败兜底收藏全部图片。成本：模型无法在本版逐张选择收藏动作。
- v7 最终实现：图片/GIF、受控两阶段回合、真实 reasoning/tool/sticker SSE 与持久化、库 CRUD、归档 v3/旧包兼容、删除保护和历史图片均完成。最新用户要求的左下角“＋”、剪贴板图片、输入框预览、聊天区/头像放大、移除侧栏品牌、日志过程显示与可拖宽度已完成。
- 用户后续覆盖原计划“思考不显示在日志”：只在页面内存的日志抽屉显示真实思考，服务端 stdout 和 localStorage 仍不写入思考；“处理”筛选可单独查看。
- Final: 独立审查 4 项问题已修复：视觉观察绑定 request/generation 并校验 epoch，删除不残留；失败/取消终结 running 工具；SSE 重试耗尽保留部分正文；历史按原 asset_ids 恢复图片顺序。对应 RED→GREEN，相关 15 passed。
- 最终自动验证：99 passed、1 skipped；协议生成/漂移检查通过；无头 Chromium 三档通过，0 JS 异常。真实厂商测试单独启用：1 passed，仅合成图片与隔离 schema，vision/reply/reflect 均完成，reasoning 629 字符。
- 本地发布：先保存 `data/backups/pre-v7-20261025.dump`（134156 bytes），确认无在途/排队生成后，正常停止 Gateway，应用 0008 并按原地址/端口重启。原工作树保留，未提交/未推送；AOCI 收尾在最终稳定状态执行。
- 后续用户要求已补齐：双方头像分别设置/移除；共享本地裁剪窗口可拖动/缩放，确认后才上传 512×512 PNG，取消不上传；88px 固定预览与异步令牌防止重复叠图。Chromium 用非正方形双色图验证拖动后输出的尺寸、格式和实际中心像素，保存/聊天显示/移除通过。
- 日志补齐每条回复正文和历史轨迹恢复：实时 delta 汇总到 THINK/REPLY 行，TOOL 显示每次调用/结果，刷新页面仍可从档案恢复；思考不写服务器日志或 localStorage。浏览器回归再次通过，0 JS 异常。
- 最新用户要求覆盖 v7 原先双轨迹 UI：思考与工具过程仅在日志栏显示；聊天和归档只渲染正文及图片。保留服务端 trace 与日志恢复，移除气泡轨迹渲染；浏览器覆盖实时/刷新不出现思考且日志仍完整可见。
- 分段回复（用户确认规则）：按 `。！？!?`/换行创建独立助手气泡，去掉句末分隔标点，保留句首问号叹号；逗号顿号不拆，英文句点保留以免损坏文件名/编号/小数，URL和代码内部标点受保护。图片独立为一条；实时、历史、归档共用 splitter，服务端原始事件及日志内容不变。
- 输入框遮挡修复：聊天页占满可视高度，只滚动消息；空状态层与提示条避让输入；中等宽度日志改并排，手机日志底部由 ResizeObserver 测量并留出输入区，长历史/日志开启均验证输入、选图、发送可见且可点击。
- 验证：`node --test scripts/test-reply-segments.mjs` 5 passed；Chromium 三档通过，包含40条长历史、日志打开时命中测试、两段正文+独立图片、刷新一致、断线保留、纯图片后断线显示说明。独立审查的英文句点破坏文件名/编号、图片气泡错误正文仍隐藏两项已 RED→GREEN；当前浏览器已安全刷新并实测输入框未遮挡。
- Ruling: 此次分段为浏览器显示层规则，原文不拆成多个数据库事件。理由：保留幂等/上下文/日志的完整回复，实时与历史统一投影。成本：其他客户端需复用同一显示规则才能看到相同分段。

### 表情包修复（2026-10-25）

- **库页重复卡片**：`navigate()` 在设置 hash 后不再额外 render，避免 hashchange 触发第二次页面加载；`loadStickers()` 增加加载令牌并在响应时重新清空网格，丢弃过期响应。验证：Chromium 中先离开再点回表情包页，卡片仍只有 1 张。
- **文本回合可发送已收藏表情包**：此前只有图片回合的反思阶段会调用 `send_sticker`，纯文字请求“发给我”不会进入图片编排。现在文字回合在主回复完成后，仅在库非空且用户有表情包/发图线索（或少量自然触发）时执行一次受限二次选择，只允许返回库内 UUID；命中后走原有 `sticker.sent`、附件落库、使用次数和 `generation.completed` 流程。图片回合仍走原两阶段编排，不重复发送。
- **自动命名与标签**：图片回合反思提示要求 `save.tags` 为 2-5 个可检索中文短标签；模型漏填时用观察资料再生成一次，失败才用“表情”兜底。手动上传的图片通过 `POST /api/v1/stickers` 自动调用已配置视觉模型命名/打标签；新增 `POST /api/v1/stickers/{id}/autotag` 供已有未标签条目补打。当前库内旧图已补为“困困摸鱼 / 摸鱼、困倦、二次元、流口水”。
- **不支持文件不再静默**：聊天图片与表情包上传在前端先按 PNG/JPEG/WebP/GIF 校验，不支持的组合直接 toast 文件名；服务端仍保留 `UNSUPPORTED_MEDIA`/`INVALID_MEDIA` 作为最终防线，上传失败会显示可读错误。
- 验证：完整后端 `102 passed, 1 skipped`（新增文本发送 2 项、自动标签 1 项）；`node --test scripts/test-reply-segments.mjs` 5 passed；`node scripts/verify-visual-client.mjs` Chromium 三档通过、0 JS 异常；本地真实视觉自动标签调用成功，Gateway 重启后 live/ready。
- **消息出现动画**：实时发送的用户消息与助手分段气泡使用 0.34s 淡入上滑动画；分段按流式到达顺序逐条出现，新增气泡间隔约 700ms，避免“瞬间几条”；历史/归档加载不播放动画，避免长历史整屏抖动。`prefers-reduced-motion` 下禁用延迟与动画。
- **日志升级**：终端抽屉增加 INFO/WARN/ERR 等级、系统/处理/活动/警告/错误筛选、搜索、详细字段开关、复制、可见/总数与错误/警告计数；客户端活动日志携带 conversation/generation/首字耗时/耗时/SSE 事件数/模型等结构化字段；`memory.context` 增加 memory_count、memory_ids、token_estimate 与 model；Gateway 对写请求记录 method/path/status/耗时（不含 body）。抽屉背景改为伴侣头像模糊水印，头部增加头像徽章，头像上传/移除/保存/加载时同步更新。
- **空回复恢复**：SSE 事件流提前结束但未收到终态时自动续传最多 3 次；完成仍无正文时按 generation_id 从会话历史补回归档正文并渲染，日志记录“已从档案恢复回复正文”及诊断字段；本地模拟流提前断开测试通过。

### 本地语音回复（2026-10-25）

- **交互**：对话输入框底部独立「语音」开关（按设备保存）+ 概率浮层；关闭按服务端概率（默认 30%），打开后每条有正文的回复都带一条语音。语音是独立助手气泡，支持播放/暂停、时长、排队/合成/失败/取消状态；历史/归档可回放，不自动播放。
- **服务端**：新增 `voice/` 包（Breeze HTTP engine、情绪映射、文本清洗、VoiceService）和 `speech_segments` 复用；**无数据库迁移**。正文完成后先发 `speech.queued` 并立即 `generation.completed`，后台合成不阻塞输入框；音频 WAV 存内容寻址资产，事件 content/attachments 记录 voice 状态、情绪、时长和 asset 引用。
- **API**：`GET/PATCH /api/v1/voice/settings`、`GET /api/v1/voices`、`POST /api/v1/voice/preview`、`GET /api/v1/generations/{id}/speech`；`/health/ready` 的 `tts` 为 unconfigured/unavailable/available，`/capabilities` 的 `tts` 仅在真实可达时为 true。`output.voice` 支持 auto/always/off，旧 `output.audio=true` 兼容为 always。
- **情绪**：system prompt 要求她第一行自报隐藏情绪标记 `[情绪:xxx]`；`EmotionTagStream` 在流式开头缓存并剥离标记，正文/归档不出现标签；标记缺失时用受限语气导演兜底，再失败回退平静。服务端把枚举固定映射为 Breeze instruction。正文清洗 Markdown/代码/URL/表情并按 400 字断句。
- **启动**：`scripts/start-voice.ps1` 读取 `config/voice.local.json`，复用健康的 `yuwan` 进程或隐藏启动 Breeze，日志 `data/voice.*.log`，PID `data/voice.pid`；`start-local` 自动调用且失败不影响 Gateway，并改为用 `/health/live` 校验已有 Gateway（卡死的旧进程不再被误判为运行中）；`stop-local` 新增 `-KeepVoice`、等待退出并在超时后只强制停止已验证项目进程；`status-local` 显示 Voice ready/unavailable 与音色。
- **客户端**：新增 `audio.js` 自定义播放器；`createReplyTurn.voice()` 生成语音气泡，历史/归档音频附件分流；`sdk/api.js` 增加 voice 方法；TTS 设置页改成 Breeze 面板；模块版本提升到 `v=20261026a`。
- **验证**：完整 pytest `153 passed, 2 skipped`（新增语音 text/emotion/engine/API/reply/interrupt/stale 测试；默认跳过真实 GPU/vendor live test）。`pnpm` 等价 `node scripts/generate-contracts.mjs` + `scripts/check-contracts.mjs` 通过。真实 Breeze 5.36s 音频总耗时 3.80s、RTF 0.71、首包 434ms；旧导演链路真实聊天生成 12.96s 语音；新自报标记链路 live test 通过（source=model_tag、5.76s、正文无标记）。`scripts/verify-voice-client.mjs` 无头 Chromium 验证开关、概率浮层、TTS 面板、yuwan 音色、真实历史语音气泡（0:12）与 0 JS 异常。
- **边界**：一回复一语音、非逐句流式；超过 400 字读节选；不做 ASR/麦克风/全双工/Live2D 口型；`.breeze` 音色文件和参考音频不进入归档包，迁移 NAS 需单独备份；Breeze 权重研究/非商业用途。

### 长期记忆阶段一（2026-10-25）

依据 `06-长期记忆详细设计与开源借鉴-DeepSeek实施版.md` 与 `05-记忆系统开发要求`，先审计后实现最小可靠闭环；详细缺口表见 [长期记忆阶段一开发记录](../development/2026-10-25-long-term-memory-phase1.md)。

- **迁移 0009**：`companions` 增加 `memory_extract_cursor_at/id` 提炼游标，`events` 增加 `(companion_id, recorded_at, id)` 扫描索引；兼容旧库的补列逻辑已用“删列+回退版本后 upgrade”实测。
- **真实提炼适配器**：严格 `{"schema_version":1,"candidates":[...]}` 协议；候选 ≤8 条、content ≤500 字、每条 1–8 个 quote；quote 必须在给定事件原文中唯一出现，服务端计算 offsets/quote_hash；user_asserted/observed/assistant_generated 分别校验事件角色/类型，虚构强制 fictional；JSON/schema 错误最多一次修复，第二次失败按 `EXTRACTION_INVALID` 重试，不猜写。
- **异步任务闭环**：`generation.completed` 同事务入队 `memory.extract`（按 generation 去重）；短事务读游标、事务外调模型、短事务复验租约/游标/memory_revision/来源可见性与 content_sha256 后提交；崩溃后重试从新游标继续，同批不重复；`LEASE_LOST` 不再触发 fail 的二次写库错误。
- **写入规则**：相同 content+kind+epistemic 只补来源（REINFORCE），不产生重复事实；新有效记忆一次性递增 `memory_revision`、runtime binding 置 stale、失效在途生成。
- **召回**：结构化记忆除自身内容外，还按来源事件原文命中；用户问“以后要叫我什么”可命中存成“用户希望被称为小风”的事实。新增 `GET /api/v1/memory/items`、`GET /api/v1/memory/items/{id}`、`GET /api/v1/memory/extractions`；capabilities/ready 显示提炼模型来源与回退状态。
- **配置**：`memory_extraction_enabled`、`memory_extract_batch_events=8`、`memory_extract_max_candidates=8`、`memory_extract_timeout_seconds=30`；优先用专用 `memory_extract` 角色，未配置时回退对话模型，配置了但凭据不可用则不静默回退。
- **验证**：新增 14 项测试覆盖入队去重、跨会话召回与 scope 隔离、崩溃重跑不重复、非法 JSON 重试、伪造来源、assistant 冒充用户事实、模型调用期间删来源、租约丢失、纠正替换、列表/详情/任务状态、Gateway outbox 端到端、协议解析/修复/候选上限。全量 `116 passed, 1 skipped`。
- **边界**：未实现时间有效期、冲突/候选状态、变更历史、候选确认/驳回、删除源对话入口、每日费用上限、独立 worker；提炼质量未用真实模型/中文质量集评测；`index_events` 仍全量扫描可见事件，数据增长后需增量水印。

### 记忆管理页面（2026-10-25）

用户反馈记忆页只有搜索框、没有任何管理按钮；本轮补阶段二第一块界面，阶段一协议不变。

- **列表**：`#/memory` 自动加载；搜索正文或来源原文，按类型/状态（有效/已隐藏/已替代/争议/全部）筛选，带刷新；每条显示类型、认知性质、状态、来源数、时间与操作按钮。
- **详情**：右侧面板展示正文、置信度、创建时间、记忆 ID，以及每条来源 quote、事件 ID、发生时间和是否仍可访问；无可用来源时禁止纠正。
- **纠正**：来源改为服务端返回的可用来源下拉（显示引文），不再手填事件 UUID；提交后旧记忆转历史，新记忆带来源和 supersedes 关系。
- **隐藏/恢复**：新增 `PATCH /api/v1/memory/items/{id}`；隐藏停止召回但不删原始对话，恢复重新参与。隐藏/已替代/争议的同内容事实不会因再次提炼复活（ignored 计数）。
- **提炼任务**：展示最近任务状态、尝试次数、事件/写入/忽略/隔离计数与错误码；“立即提炼一批”按钮调用 `POST /api/v1/memory/extractions`，不依赖下一轮聊天即可排队处理最多 8 条历史，适合首次查看或补齐旧档案。
- **检索边界**：页面标注当前为中文关键词 + 来源原文匹配，向量语义检索尚未接入。

### 记忆生命周期（借鉴 LivingMemory，2026-10-25）

参考 [LivingMemory](https://github.com/lxfight-s-Astrbot-Plugins/astrbot_plugin_livingmemory)（AGPL-3.0）公开的“原子/类型化 TTL/衰减/访问强化/整合”设计，**仅借鉴思路、未复制 AGPL 源码**，按本项目 PostgreSQL 独立实现。

- **迁移 0010**：memory_items 增加 importance、access_count、last_accessed_at、last_reinforced_at、ttl_days、expires_at、decay_type、archived_at 与两个索引；旧记忆给 90 天宽限。
- **TTL 与衰减**：经历 7d、计划 2d、事实 180d、关系 90d、偏好 60d、承诺 90d、身份 365d、其他 30d；计划 step、关系 linear、其余 exponential；importance 与强化次数放大 TTL。
- **访问强化**：ContextBuilder 真正选中并注入记忆后 access_count+1、更新 last_accessed_at、expires_at 只延长不缩短；召回测试不强化。
- **维护**：Gateway 启动后每 6 小时自动把到期 active/dormant 置为 expired（保留正文/来源，不物理删除）；页面提供手动“运行维护”。
- **页面**：记忆列表显示重要度/访问次数/到期，支持按最新/重要度/即将到期排序；详情显示生命周期字段并可“标为重要”；新增“召回测试”显示命中路线、分数、kind/importance/freshness/status。
- **API**：`PATCH /api/v1/memory/items/{id}` 支持 status/importance；`POST /api/v1/memory/recall-test`；`POST /api/v1/memory/maintenance`。
- **验证**：完整 pytest `123 passed, 1 skipped`；无头 Chromium 页面回归新增生命周期行、排序、召回测试、维护和重要度，0 JS 异常。用户已手动触发一次真实提炼（8 条事件，0 条候选，任务 completed），证明任务可运行。
- **边界**：向量/BM25/图谱混合检索、Embedding Provider 配置落地、语义聚类整合、Agent 主动记忆工具、TTL 可配置化与真实质量评测均未完成。

### 系统概览与记忆设置（2026-10-25）

参考 LivingMemory Dashboard 的信息架构（AGPL，仅借鉴布局/指标，未复制代码）。

- 记忆页增加指标条（有效/隐藏/过期/替代/待提炼事件/任务数）、重要性 10 档与类型分布、设置卡片。
- 新增 `GET /api/v1/memory/stats`、`GET/PATCH /api/v1/memory/settings`；设置含自动提炼、批量事件数、候选上限、超时、自动维护与间隔，写入 `data/settings.json` 的 memory 段，重启恢复；保存后热生效。
- 维护循环读取设置；关闭维护时每 5 分钟检查一次；向量状态显示“未接入”，可跳模型页嵌入 Tab（适配器仍未接入）。
- 验证：完整 pytest `123 passed, 1 skipped`；无头 Chromium 指标/分布/设置读取保存/向量入口通过，0 JS 异常。
- 边界：仅单角色统计、无图表交互/导出/图谱/提示词管理；向量仍不可用。

下一功能的设计已落到 [思维链/图片/表情包开发计划](../superpowers/plans/2026-10-25-reasoning-images-stickers-plan.md)；以下保留原有待办顺序，开发新功能前先确认该计划的假设与非目标。

1. 客户端下一步：在浏览器里用真实厂商完成一段连续对话并观察记忆/档案更新；再补 Live2D 渲染、语音播放/打断与播放回执（依赖 T09/T10 音频链路）。
2. 归档页的删除已按用户要求升级为物理删除：`DELETE /api/v1/conversations/{id}` 写入删除账本，清除事件、请求/生成、附件引用及派生记忆来源；生成中的对话拒绝删除（409）。
3. 本地 TTS（Breeze `yuwan`）已接入并通过真实试听/聊天验证；ASR、向量、重排序仍未接入，相应能力 Tab 不能当作功能验收。
4. 正式部署 NAS 前：更换默认客户端密钥、启用 HTTPS、把 `allowed_origins` 限定为真实客户端来源，并加密 `data/secrets.json`。
5. 本地完整链路通过后再部署 NAS。NAS 当时 8188 MiB 显存已用约 7391 MiB，Bonsai 占大部分；不能视为 TTS 可用余量。

### Agent 能力（独立分支阶段记录；现已整合源码，未上线）

- 分支 codex/agent-capabilities，隔离工作区位于 C:\Users\wind\.codex\worktrees\agent-capabilities\AGI.Wind；原工作区未部署此功能。
- 默认关闭；开启后复用当前对话模型执行受限工具循环：记忆查询、表情包查询/单张发送、SearXNG或Brave联网检索。
- 新增“Agent 能力”管理页与GET/PATCH /api/v1/agent/settings；工具开关、调用/时间/结果限制与检索配置持久化；密钥分后端保存在SecretStore且不回显。
- 推理与工具仅日志；记忆工具原文只临时提供给模型，持久化日志仅留结果概况/来源ID。并发设置PATCH已加共享写锁。
- Agent回合原生请求暂为非流式，普通聊天保持流式；联网仅搜索摘要，不抓全文；真实厂商和搜索端点未实测。
- 细节及集成边界见 docs/development/2026-10-25-agent-capabilities.md。隔离快照d22b02f包含既有工作，不能整体合并；只整合其后的功能差异。

2026-10-25 整合更新：功能差异已应用主工作区，保留新版语音情绪流、路由、客户端播放和原未提交成果。完整后端183 passed/2 skipped，协议、分段、Agent/语音/图片客户端通过。未重启Gateway/Breeze，未开启真实Agent；AOCI未对齐，真实模型和搜索仍待配置后实测。回退备份与具体冲突见上述开发记录。


## 2026-10-26 记忆层补齐

已部署本地 BGE 中文 CPU 语义检索、结构化事实更新与争议记录、固定与保留期、有来源的阶段摘要、预算及独立后台。迁移至 0013_memory_projection_compat，现有聊天/媒体保留。依据和验收边界见 [开发记录](../development/2026-10-26-memory-completion.md)。源码保留原分支未提交改动，没有混入旧快照分支。

最终验收：记忆专项 96 passed / 1 skipped；整套 313 passed / 3 skipped / 1 failed，失败为既有通话会话接口与测试不一致（缺 call_id），详见开发记录。真实历史待提炼已清零；真实今日摘要 32 个来源、写入 2 条；语义与独立后台运行中，HTTPS 200。


## 2026-10-26 QQ 群聊、连续接话与双记忆池（第一批）

迁移至 0014_qq_channels：新增群通道绑定、外部成员身份、OneBot 发送回执、事件发送者/平台消息 ID，以及记忆池归属与群可见范围字段。一个群对应一个持久窗口，群消息先归档再按 @ / 引用 / 短时追问规则决定是否调用模型；群回复使用 send_group_msg 并保存回执。记忆检索按服务端场景隔离：主人私聊只取主人池（可含主人本人群发言），群窗口只取本群公开的主人事实与本群成员事实，其他群、成员池其他人和主人私密承诺不可见。连接页新增群聊开关、允许群列表、接话模式/有效期/轮次，侧栏新增 QQ 群窗口列表（只读、带发送者）。

验证：完整 pytest **347 passed, 3 skipped, 1 failed**（唯一失败为既有通话 call_id 测试不一致，与本轮无关）；QQ 专项 12 项、记忆场景 3 项、无头 Chromium 群窗口/设置检查通过，0 JS 异常。

边界：真实 QQ 群收发尚未验收；短时接话状态仅进程内；成员池提炼任务、逐群启停、群内成员级停止与双池管理页筛选为下一批。依据与详细边界见 [开发记录](../development/2026-10-26-qq-groups.md)。


## 2026-10-26 QQ 群成员池与双池管理页（第二批）

新增 `memory/members.py` 与服务端成员池写入器：事实主体由被引用事件的发送者身份决定，房间由群绑定决定，多成员共用的候选、主人发言、匿名消息都不能写入成员池；同一成员相反说法进争议与冲突关系，不做静默覆盖。新增 `memory.extract.member` outbox 任务，按群游标读取非主人成员消息、复用预算受限的提炼模型；群回复后立即入队，纯观察消息 60 秒合并入队，每群同时最多一个 pending。记忆页新增“记忆池 / 来源群”筛选，成员记忆显示名片与来源群；`GET /api/v1/integrations/qq/groups` 提供持久群窗口列表。

验证：完整 pytest **351 passed, 3 skipped, 1 failed**（仍为既有通话 call_id 测试不一致）；成员池专项 3 项、QQ 群专项 6 项通过；无头 Chromium 成员池筛选与来源群检查通过，0 JS 异常。边界：真实模型/真实群成员记忆尚未验收；逐群启停、成员级停止、歧义分类、接话线程持久化仍未完成。见 [开发记录](../development/2026-10-26-qq-groups.md)。


## 2026-10-26 QQ 群内权限隔离与逐群启停（第三批）

群场景 Agent 权限独立收紧：即使主人全局开启文件/命令完整权限，群聊也强制禁用本地文件与命令工具，不注册对应工具、不产生本地审批；记忆/表情包/联网仍按全局设置和预算运行。新增 `PATCH /api/v1/integrations/qq/groups/{binding_id}` 逐群启停，`allowed_groups` 为唯一开关来源，群绑定状态同步；连接页新增已接入群窗口列表，可逐群开关并直接打开窗口。

验证：Agent 群权限单元测试 2 项、逐群启停 API 测试 1 项通过；无头 Chromium 实测逐群启停与打开群窗口，关闭后配置恢复，0 JS 异常；完整 pytest **359 passed, 3 skipped, 1 failed**（仍为既有 call_id 测试）。剩余：真实群验收、成员级停止/群内审批/每群费用上限、歧义分类与接话线程持久化。见 [开发记录](../development/2026-10-26-qq-groups.md)。


## 2026-10-26 QQ 多媒体收发、模型自定分段与发送节奏

私聊与已授权群聊支持图片、表情包、语音的接收和回复发送：图片复用 AssetStore/VisionAdapter（缺 URL 时 `get_image`，白名单域名 HTTP→HTTPS，保留 DNS/IP 固定、防重定向与格式校验）；语音用 SnowLuma `get_record_text` 转写并带 `[语音转文字]` 进入记忆隔离，回复最多等本轮后台合成 120 秒，record/base64 发送；表情包附件按 image/base64 发送。有声的段只发语音、不再重复发文字，文字仅作回退。分段改由模型决定：一行一条消息，服务端与客户端同步，不再按标点自动切；QQ 文字按长度延迟 0.6s+0.06s/字（上限 8 秒）发送。Agent 回合的表情包工具改为每轮 30% 概率提供（用户明确要时不受限），避免每轮都发表情包。

验证：QQ 单测 21 项、Agent 55 项、生成集成 3 项、分段 Node 与 Python 单测通过；本机整套 pytest 会挂起，逐文件均通过，未取得整套结果。真实 QQ 语音/图片往返与真实模型识别仍未验收。依据与边界见 [开发记录](../development/2026-10-26-qq-media.md)。


