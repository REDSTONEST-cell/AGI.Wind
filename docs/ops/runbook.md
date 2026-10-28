# 本地服务端运维

目录：C:/path/to/Nagi。当前是 Windows 本地基础部署，NAS 尚未安装。

浏览器请打开 **http://127.0.0.1:18440/**，这是聊天客户端；依赖状态页在 **http://127.0.0.1:18440/status**。`/health/ready` 是机器健康接口，模型未配置时返回 HTTP 503，不作为浏览器网页入口。

## 聊天客户端

1. 打开 http://127.0.0.1:18440/。**本机回环访问会自动建立 HttpOnly 会话，不需要访问令牌**（`local_auto_login` 默认开启，且同时校验回环来源与 Host 头，防 DNS rebinding）；只有通过非回环地址访问时才回退到客户端密钥登录。
2. 对话页是默认首页：**左侧固定侧栏**分「陪伴 / 她的档案 / 系统」三组——对话、对话归档、灵魂档案、记忆、模型与密钥、连接、外观与账户；顶栏有面包屑、服务状态点、浅深主题切换与刷新。对话页顶部只有当前会话标题、模型入口、日志按钮与 ⋯ 菜单，**不再显示指标条**；下方是一张单会话窗口与输入栏，输入栏右侧只有停止/发送。点「日志」从右侧拉出**终端式系统日志**：深色等宽输出，Gateway stdout/stderr 行（服务端脱敏）与页面活动行混排，支持等级/搜索/详细/复制/跟随/暂停、系统/处理/活动/警告/错误筛选、清空与回到最新；桌面端压缩对话区并排显示，手机端全屏。日志文件来自 `data/gateway.stdout.log` / `gateway.stderr.log`（用启动脚本启动时才会持续写入；服务端同时输出结构化运行日志）。「⋯」提供重命名、归档并开始新对话、永久删除并开始新对话。
3. 旧对话归档后只读；「对话归档」页以表格展示标题、创建时间与查看/恢复/删除操作，删除会永久清除该对话的事件、附件引用和由它产生的记忆来源，无法恢复。
4. 各设置项是独立页面而不是设置弹层：灵魂档案（字段/体积/所属角色指标 + 档案卡 + 表单 + 粘性保存栏，不显示版本号）、记忆（搜索与纠正）、模型与密钥（6 张能力卡 + 提供商列表/详情）、连接（4 格服务指标 + 地址/密钥）、外观与账户（浅色/深色/跟随系统三张主题卡、退出会话、关于）。灵魂档案可上传头像；保存前会提示“旧格式长文本重复进入上下文”。能力卡中对话、识图和文字转语音接入了真实适配器；文字转语音使用本地 Breeze，详情见下文“本地语音回复”。
5. 未配置模型时发送消息会返回 `MODEL_UNCONFIGURED`，不会伪造回答。上下文预算默认总 48 KB、单条输入 12 KB；`CONTEXT_TOO_LONG`/`INPUT_TOO_LONG` 会显示可操作提示并链接到灵魂档案，不再显示“模型没有返回文字”。

## 服务器与客户端分离

同源浏览器访问使用 HttpOnly cookie + CSRF。服务器与客户端分开部署（例如 NAS 跑 Gateway、另一台电脑开客户端）时：

1. 服务端用 `启动服务端(局域网).cmd` 或 `.\scripts\start-local.ps1 -BindHost 0.0.0.0` 启动，监听所有网卡；默认只监听 127.0.0.1。
2. 客户端在登录页或侧栏「连接」填写 `http://服务器IP:18440` 和客户端密钥（默认 `123456`），客户端改用 `Authorization: Bearer <client_key>` 调用 API。
3. 跨域请求要求服务端配置 CORS 白名单：设置 `AGI_ALLOWED_ORIGINS`（逗号分隔）或在 `config/app.local.yaml` 写 `allowed_origins`；同机独立客户端调试可用 `.devtools/standalone-server.mjs`（端口 18441）配合本地配置中的两个开发来源。
4. 默认密钥只用于内网调试。Gateway 绑定非回环地址且仍使用默认密钥时，启动日志会打印警告；正式使用前请修改 `client_key`（配置或 `AGI_CLIENT_KEY` 环境变量）。
5. 客户端地址保存在浏览器 `localStorage`（只存地址，不存密钥）；刷新后会用默认密钥重试，改过密钥需重新输入。

## 启动、停止、状态

双击根目录的中文 CMD 文件，或在本目录 PowerShell 执行：

```powershell
.\scripts\start-local.ps1                 # 仅本机，127.0.0.1:18440
.\scripts\start-local.ps1 -BindHost 0.0.0.0   # 局域网
.\scripts\status-local.ps1
.\scripts\stop-local.ps1 -StopDatabase
```

Gateway 默认只监听 127.0.0.1:18440；PostgreSQL 只监听 127.0.0.1:55432。不注册 Windows 服务，不修改防火墙，不接管其他数据库。未设置开机自启，电脑重启后运行启动脚本。

启动复用 app/.venv、.devtools/pgsql 和固定开发角色。`start-local.ps1` 只有在 `/health/live` 真实响应时才复用已有 Gateway；PID 存在但进程卡死时会先停止已验证的项目进程再重启。`stop-local.ps1` 先走带认证的正常退出，15 秒未退出时只强制停止已验证的项目进程；Gateway 未退出时保留数据库，不结束未知进程。

## 文件

| 路径 | 内容 |
|---|---|
| app/src/nagi | 业务源码 |
| app/.venv | 专属 Python 3.12 环境 |
| app/uv.lock、pnpm-lock.yaml | 直接/传递依赖锁 |
| .devtools/pgsql | 便携 PostgreSQL 17.6 |
| data/dev-postgres | 数据库 cluster；运行中直接复制不等于有效备份 |
| data/local-db.json | 含秘密的本机 DB 连接配置，不提交 |
| data/access-token.txt | 本地 API 访问凭据，不输出到聊天/普通日志 |
| data/secrets.json | 客户端设置页保存的服务商密钥，仅服务端读取，不进入 YAML/索引/浏览器 |
| data/settings.json | 客户端设置页保存的服务商与模型选择（不含密钥），重启后生效 |
| data/gateway-host.txt | 上次启动的监听地址，状态脚本据此提示局域网地址 |
| data/local-identity.json | 固定账号/用户/角色/会话 ID，明确为开发角色 |
| data/assets | 内容寻址媒体 |
| data/governance/deletions.ndjson | 独立删除账本，不能被旧备份覆盖；缺失时拒绝开放角色 |
| config/app.local.yaml | 本地配置，含 voice 段；排除提交 |
| config/voice.local.json | Breeze 语音启动路径/端口；排除提交 |
| data/voice.pid | 受管 Breeze 进程 PID |
| data/voice.*.log | Breeze stdout/stderr（首次隐藏启动时创建） |
| data/voice | 参考音频、试听/测试 WAV；不进入归档包 |
| C:\path\to\Models\Breeze-TTS-2\voices\yuwan.breeze | 克隆音色文件，在项目外，需单独备份 |
| data/gateway.*.log | 运行日志，不打印秘密和完整 prompt |
| data/local-smoke/report.json | 实际 HTTP 验证证据，仅合成数据 |

当前是 Git 仓库（已有 3 次提交）；.gitignore 已排除秘密、数据、环境和原有工具/素材。NAS SSH 密码未写项目文件。

## 模型与能力

用户决定稍后配置模型。config/providers.example.yaml 是结构样例；本机入口 config/app.local.yaml，credential_ref 引用环境变量。未把示例域名/模型当作可用配置。

两种配置方式：① 在客户端「设置 → 模型与密钥」按能力 Tab 填写，服务端把配置写入 `data/settings.json`、密钥写入 `data/secrets.json`（文件权限 600），更新后热加载无需重启；② 环境变量与 `config/app.local.yaml` 仍是基线，环境变量中的同名凭据优先于本地密钥文件。未配置或残留无效配置时，聊天会明确返回 `MODEL_UNCONFIGURED` 或 `PROVIDER_NOT_CONFIGURED`，不伪造回答。

模型角色：`dialogue`（计划/对话，已接入）、`vision`（识图，已接入）、`tts`（本地 Breeze，已接入）、`embedding`、`asr`、`rerank`（仅保存配置，适配器未接入，客户端标注“待接入”），以及内部角色 `memory_extract`/`memory_reflect`。`local` 是嵌入角色的内置关键词检索占位，不需要密钥。

本地开发显式使用 direct_vendor；Letta 未接入，不自动回退。切换供应商会重新解析对应客户端，未配置的供应商明确拒绝。当前为开发人格，正式人格与声音待用户指定。

上下文预算：`context_budget_bytes` 默认 48000、`input_budget_bytes` 默认 12000，均按 UTF-8 字节估算；可用环境变量 `AGI_CONTEXT_BUDGET_BYTES`/`AGI_INPUT_BUDGET_BYTES` 调整。人格档案与单条输入分别计入预算，超限返回可读的 `CONTEXT_TOO_LONG`/`INPUT_TOO_LONG`。

`GET /health/live` 只证明存活。`GET /health/ready` 在 DB/媒体/对话模型可用时返回 200；语音会额外显示 `unconfigured` / `unavailable` / `available`，但语音不可用不会让文字聊天变成 degraded；向量仍为关键词检索。

## 已开放 API

`/api/v1` 需要 `Authorization: Bearer <本地凭据>` 或同源浏览器会话 cookie；健康检查无需认证。凭据不能放长期 URL 查询串。

- client/launch：Bearer 换一次性登录票据；client/session：票据换 HttpOnly+SameSite=Strict cookie（响应体只给 CSRF token），GET 复用/ DELETE 退出；client/key-session：客户端密钥换同源 cookie 会话（每 IP 60 秒最多 5 次失败）；client/bootstrap：仅回环自动登录。cookie 会话的写操作必须同时带 `Origin` 与 `X-AGI-CSRF`。
- conversations：GET `?companion_id=&state=active|archived|hidden|all`（默认 active）；POST 归档现有 active 后创建新 active；PATCH 改 `title`/`state`，恢复旧对话会自动归档当前 active。归档会话的 events 只读，chat 请求返回 `CONVERSATION_NOT_ACTIVE`。
- capabilities、companions、events/{id}。
- settings/providers、settings/models/{role}：服务商与模型配置，GET 只回脱敏状态；settings/providers/{id}/test 由服务端用已存密钥探测端点；`role` 支持 dialogue/vision/embedding/asr/tts/rerank（及内部记忆角色）。
- system-logs：读取 Gateway stdout/stderr 尾部，支持 `tail` 与 `stdout_offset`/`stderr_offset` 增量；需要认证，响应前会脱敏已知凭据、数据库密码与 Bearer/赋值形态敏感串。
- chat/requests：先事务落盘再 202，相同 request_id 复用任务。
- generations/{id}、generations/{id}/events：状态/SSE，Last-Event-ID 为整数 sequence。
- generations/{id}/interrupt：command_id 幂等打断。
- conversations/{id}/live、conversations/{id}/events：控制流/分页历史。
- assets 上传和按 ID 授权下载。
- companions/{id}/persona：读取和 expected_version 更新；companions/{id}/model 修改未来请求模型。
- memory/search、memory/corrections：关键词检索和有来源纠错；memory/items 与 memory/items/{id}：当前记忆列表/来源详情，PATCH 支持 hidden/active；memory/extractions：提炼任务状态、尝试次数、错误码与 usage。
- archive/exports：同步基础导出及授权下载。
- DELETE events/{id}：删除事件及同轮派生输出，写账本并清理未共享媒体。

语音回复 API 已开放：`GET/PATCH /api/v1/voice/settings`、`GET /api/v1/voices`、`POST /api/v1/voice/preview`、`GET /api/v1/generations/{id}/speech`。SDK/兼容入口/导入任务 API 还未全部开放，以 progress.md 为准。

## 导出、恢复、重建

以下从项目根执行：

```powershell
.\app\.venv\Scripts\python.exe -m nagi.cli archive export --output data\exports\manual.zip
.\app\.venv\Scripts\python.exe -m nagi.cli archive verify data\exports\manual.zip
.\app\.venv\Scripts\python.exe -m nagi.cli index rebuild
.\app\.venv\Scripts\python.exe -m nagi.cli archive import data\exports\manual.zip --mode clone --latest-ledger data\governance
```

只有确认账本为最新权威版本时才提供 `--latest-ledger`；未提供、文件缺失时只能隔离恢复，不能查询角色。默认新命名空间拒绝 ID 冲突；覆盖恢复尚未开放。当前 index rebuild 仅重建关键词切片，原始事件/人格不变；pgvector 向量未实现。

开放包不含 API secrets 和 Letta 私有历史。声音/头像包、原生数据库备份/轮换和异盘恢复仍待后续任务；基础包通过不等于完整迁移验收。

## 开发验证

```powershell
cd app
..\app\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider
cd ..
.\app\.venv\Scripts\python.exe scripts\smoke-local.py
.\app\.venv\Scripts\python.exe scripts\smoke-client.py
.\app\.venv\Scripts\python.exe scripts\smoke-voice.py
node scripts\verify-voice-client.mjs
node .devtools\verify-v5.mjs
node .devtools\verify-standalone.mjs
node scripts/verify-memory-client.mjs
```

数据库集成测试使用 agi_wind_test 中随机临时 schema，不触碰业务库。`smoke-local` 写一条合成经历并验证基础链路；`smoke-client` 验证浏览器会话/CSRF、单活跃会话、客户端密钥、静态客户端、人格/会话/设置读取与退出，其中聊天请求使用当前已配置模型（未配置时明确返回 `MODEL_UNCONFIGURED`）。两个无头脚本分别验证同源页面与跨域独立客户端；跨域验证需要先运行 `.devtools/standalone-server.mjs` 和本地 CORS 开发来源。

真实厂商与语音由显式开关的 live test/冒烟脚本单独验证（`AGI_RUN_VISUAL_LIVE=1`、`AGI_RUN_VOICE_LIVE=1`、`scripts/smoke-voice.py`）；默认自动测试不调用真实厂商或 GPU。向量与 NAS 部署仍不属于自动验证范围。

源码恢复包不含 PostgreSQL、依赖环境、数据和秘密。新电脑需 uv sync 安装依赖，并从 versions.md 的官方来源取得便携 PostgreSQL 包；setup-dev-postgres.py 仅初始化专属本地实例。

## v7 图片回合与表情包

- 输入框左下角“＋”选 PNG/JPEG/WebP/GIF，或在输入框 Ctrl+V 粘贴图片；最多 4 张，上传中不可发送，允许纯图片。预览可移除，发送后进入消息与历史。
- 流程：真实视觉观察 → 对话模型流式思考/正文 → 第二次调用输出受限 JSON → 真实收藏/发送。反思失败或超时保留正文并兜底收藏，不模仿。任何失败/取消都收敛工具状态。
- 表情包页面 `#/stickers`；API 为 `GET/POST /api/v1/stickers`、`POST /api/v1/stickers/{id}/autotag`、`PATCH/DELETE /api/v1/stickers/{id}`，复用认证、CSRF 与账号/角色权限。手动上传会先用视觉模型生成名称与 2-5 个标签；已有未标签条目可点“自动标签”补打。后台上传/收藏也会带标签，便于后续按标签选择。图库条目删除不删除已有聊天图片；永久删除消息按删除账本清理观察、派生记录及非共享资产。
- 纯文字回合在她已有表情包时也能发图：用户明确要“表情包/发给我”等会触发一次受限库选择；普通聊天只保留很低概率的自然使用，不会每轮发送。图片回合仍走观察/回答/反思的完整两阶段。
- SSE 新增 reasoning.delta/completed、tool.started/completed、sticker.sent；`tool.completed.status` 包含 completed/failed/skipped，不能仅看事件名判断成功。`text.completed` 后仍需等 `generation.completed`，图片动作发生在二次思考之后。
- 日志抽屉的“处理”筛选显示真实思考与工具；思考仅在当前页面内存显示，不进入 localStorage 或 Gateway stdout。拖日志左边界调整宽度，方向键微调、双击恢复；宽度单独保存在 localStorage。
- 日志还显示每条回复正文；刷新后由历史 trace/text 恢复，不把思考另存到浏览器持久存储。
- 双方头像在「灵魂档案」分别设置，「基本档案」中的“你的头像”对应用户。选图进入本地裁剪，拖动/缩放后确认才上传 512×512 PNG，取消不上传；点“保存档案”后生效，也可移除。头像信息随 persona core 导出，原始素材仍走已授权 assets。
- 网页回复按 `。！？!?` 与换行分条，句末分隔标点不显示，句首 `？!` 等保留；逗号顿号不拆，文件名/编号中的英文句点保留。代码块/行内代码/URL内部标点不拆；图片独立气泡，历史和归档规则相同。原始档案和模型上下文不拆分。
- 聊天页只滚动消息区，输入区固定在可视范围；日志打开时也避让输入区。提示条位于顶部。前端修改后强刷即可，无需数据库迁移。
- 档案格式 v3 / schema `0008_reasoning_stickers`，兼容 v1/v2；迁移仅新增 stickers 表。发布前数据库备份保存在 `data/backups/pre-v7-20261025.dump`，未提交 Git。降级应用可保留 stickers 表，不删用户资产。

验证（根目录）：

```powershell
node scripts/verify-visual-client.mjs
node --test scripts/test-reply-segments.mjs
pnpm contracts:check
# 真实厂商仅在明确设置开关时调用，使用合成数据与隔离 schema：
$env:AGI_RUN_VISUAL_LIVE='1'
cd app
.\.venv\Scripts\python.exe -m pytest tests/integration/test_visual_live.py -q -s -p no:cacheprovider
Remove-Item Env:AGI_RUN_VISUAL_LIVE
```

Chromium 验证拦截全部业务写请求，检查上传/粘贴/纯图片、双轨迹、附件实际解码、历史、表情包 CRUD/lightbox、断线保留正文、头像/聊天区与日志拖动；不更改真实对话。截图在 `.devtools/v7-*.png`。

## 本地语音回复（Breeze TTS）

2026-10-25 接入。鱼丸可以在文字回复后发送一条语音消息，使用本机 Breeze-TTS-2（Q8_0 + Vulkan）的 `yuwan` 克隆音色。**不重新训练、不重新下载模型**。

### 交互

- 对话输入框底部左侧有独立「语音」开关：
  - 关闭：按服务端概率随机带语音（默认 30%，可在旁边的「语音 xx%」浮层或设置页调整）。
  - 打开：每条有正文的回复都带一条语音。
  - 开关按浏览器/设备保存在 `localStorage`；概率是服务端角色行为设置，所有客户端共享。
- 语音是一条独立助手气泡，位于文字气泡之后；支持播放/暂停、时长、排队/合成/失败/取消状态。文字仍然保留。
- 新语音准备好后会尝试自动播放；浏览器拦截时显示「点击播放」。历史/归档不自动播放。
- 「模型与密钥 → 文字转语音」是本地 Breeze 面板：启用、服务地址、音色、概率、单段最大字数、试听文本/情绪、保存。该页不再使用通用 API Key 提供商表单。

### 启动与停止

本地语音默认由 `scripts/start-local.ps1` 自动启动，不会弹出新 CMD 窗口：

```powershell
.\scripts\start-local.ps1              # 启动/复用网关与语音
.\scripts\status-local.ps1             # 显示 Voice: ready/unavailable 与 Voice id
.\scripts\stop-local.ps1 -KeepVoice    # 停止网关但保留语音进程
.\scripts\stop-local.ps1               # 停止网关与 data/voice.pid 管理的语音进程
```

- `scripts/start-voice.ps1` 读取 `config/voice.local.json`（不提交，包含 `binary/model/voices_dir/host/port/chunk_first/chunk_max/webui`）。缺少配置、路径或模型时只打印 `[语音]` 警告，Gateway 仍正常启动。
- 已有健康实例会被复用；新启动时日志写入 `data/voice.stdout.log` / `data/voice.stderr.log`，PID 写入 `data/voice.pid`。
- 当前实测启动参数：`breeze-server.exe <model> --host 127.0.0.1 --port 8137 --webui --voices-dir <voices> --chunk-first 4 --chunk-max 40`。
- `stop-local.ps1` 只停止 PID 文件指向、且进程名和模型路径匹配的 `breeze-server.exe`；不会杀未知进程。
- 只改 Gateway 前端/后端代码时不需要重启 Breeze；改语音启动参数需要先 `stop-local.ps1` 再启动。

### 服务端配置

`config/app.local.yaml`（不提交）：

```yaml
voice:
  enabled: true
  base_url: http://127.0.0.1:8137
  voice_id: yuwan
  voice_version: "1"
  probability: 0.3
  max_chars: 400
  timeout_seconds: 180
```

客户端「文字转语音」保存会写入 `data/settings.json` 的 `voice` 段并热生效，不需要重启。`VoiceSettings.enabled` 在代码中默认 `false`，防止测试或未部署环境误调 GPU；本机显式打开。

### 数据流

1. 客户端发送 `output.voice = auto|always|off`。`always` 强制，`auto` 按服务端概率，`off` 不发；旧 `output.audio=true` 兼容为 `always`。
2. 正文流式输出完成后，Gateway 在生成流中发 `speech.queued`，写入 `speech_segments`（复用 0004 已建表，**无数据库迁移**），然后立即 `generation.completed`，输入框不被语音阻塞。
3. 正文第一行由她自己写隐藏情绪标记 `[情绪:开心]`；Gateway 在流式输出中先缓存开头，解析后从正文和归档中剥离。只有标记缺失/无法解析时，才调用一次受限的「语气导演」JSON 兜底；仍失败则回退 `平静`。
4. 文本清洗（Markdown/代码/URL/表情/情绪标签）后，按 400 字断句截断；调用 Breeze `POST /v1/audio/speech`，把 PCM 包成 WAV，存入内容寻址资产。
5. 事件 content 的 `voice` 更新为 `ready`，`attachments` 追加 `kind:"audio"`，`asset_refs` 记录输出引用；客户端轮询 `GET /api/v1/generations/{id}/speech` 播放。
6. 历史/归档通过同一个附件读取音频；删除对话时现有删除账本会清理语音段和未共享音频资产。

### 情绪提示词

- 对话 system prompt 增加“情绪标记与表达”段：第一行必须是 `[情绪:开心]`（可选开心/平静/难过/担心/撒娇/生气/惊讶/温柔），标记后换行写正文；标记不会展示给用户。除标记外不用括号写动作/情绪。
- 服务端 `EmotionTagStream` 在流式开头缓存少量字符，解析标记后只转发可见正文；正文和归档均不含标记。标记缺失时用受限语气导演兜底。
- 无论自报还是兜底，TTS `instruction` 都由服务端固定映射，模型不能注入任意指令。映射示例：开心→“带笑意、语速稍快、声音明亮”；难过→“低落、放慢、带一点鼻音”；撒娇→“软糯、尾音拖长”。
- 正文中不会出现 `[情绪:开心]` 之类的可见标签；流式分段不受影响。

### API

- `GET /api/v1/voice/settings`：`enabled/base_url/voice_id/probability/max_chars/available/status/voices`。
- `PATCH /api/v1/voice/settings`：局部更新并持久化；非法 URL/概率/音色返回 422。
- `GET /api/v1/voices`：代理 Breeze 音色列表；不可达时返回空列表和 `status`，不抛 500。
- `POST /api/v1/voice/preview`：`{"text":"...","emotion":"开心"}`，返回 `audio/wav`，文本最多 200 字。
- `GET /api/v1/generations/{id}/speech`：`state`（none/queued/synthesizing/ready/failed/skipped/cancelled）、`segment_id`、`emotion`、`attachment`、`error_code`。进程重启后遗留的 queued 会变成 `failed / SPEECH_INTERRUPTED`。
- 生成流新增 `speech.queued`；`content.voice` 保存 mode/emotion/emotion_source/instruction/truncated/state；`content.attachments` 保存音频元数据。`emotion_source` 为 `model_tag`（她自己写）或 `director_fallback`（标记缺失兜底）。

### 实测与限制

- 真实 Breeze 试听：`data/voice/preview-live-happy.wav`，200 OK，`audio/wav`，24000 Hz。
- 真实服务合成：5.36 秒音频，总耗时 3.80 秒，RTF 0.71，首包 434 ms。
- 旧导演链路真实聊天已产生 12.96 秒语音（情绪“惊讶”）；新的自报标记链路 live test 通过：`source=model_tag`、5.76 秒语音、正文不含 `[情绪:`，状态 ready，附件与历史可读。
- 一条回复一条语音，不做逐句流式；400 字以上读节选并标记 `truncated`。
- Breeze 权重为研究/非商业用途；用户参考音频与克隆授权由用户确认。
- `.breeze` 音色文件和 `data/voice/*-reference.wav` **不进入 Nagi 归档包**；迁移 NAS 时需单独备份 `C:\path\to\Models\Breeze-TTS-2\voices\yuwan.breeze` 和参考音频。
- 语音服务不可用、超时、409 忙或显存不足时只影响该条语音，文字回复保持完整；`always` 模式会收到 `TTS_UNAVAILABLE` 提示。
- 8 GB 显存下 Breeze 常驻约 5.9 GB；不要同时跑其他大 GPU 任务。NAS 尚未部署。

## 长期记忆（阶段一）

- 聊天完成后，Gateway 在同一事务里登记 `memory.extract` outbox 任务；聊天不等提炼。worker 从角色提炼游标读取最多 8 条新事件，事务外调用提炼模型，再在短事务里复核租约、游标、`memory_revision`、来源可见性与内容哈希后写入或隔离。
- 模型必须返回严格 JSON：`{"schema_version":1,"candidates":[...]}`。候选最多 8 条、content ≤500 字，每条必须带事件原文中的连续 `quote`；服务端负责唯一匹配、计算偏移和哈希。助手内容不能写成用户事实，虚构/故事强制 fictional；无法证明的候选进 quarantine，不阻断聊天。
- 模型角色：优先使用 `memory_extract`；未配置时复用 `dialogue`；配置了但凭据不可用则显示 `unconfigured` 而不是静默换模型。`GET /health/ready` 和 `/api/v1/capabilities` 的 `memory_extraction` 显示 `memory_extract` / `dialogue_fallback` / `unconfigured`。
- 配置项（`config/app.local.yaml`，默认值）：`memory_extraction_enabled: true`、`memory_extract_batch_events: 8`、`memory_extract_max_candidates: 8`、`memory_extract_timeout_seconds: 30`。关闭自动提炼改为 `false` 后重启 Gateway。
- 查询与核对：
  - 侧栏「记忆」页会自动列出记忆：可搜索正文或来源原文，按类型/状态筛选；点条目看详情、来源 quote、事件 ID 与时间；支持纠正、隐藏（停止召回，不删原始对话）和恢复。
  - `GET /api/v1/memory/items?companion_id=...` 当前记忆与来源数；
  - `GET /api/v1/memory/items/{id}` 来源 quote、发生时间和是否仍可访问；
  - `PATCH /api/v1/memory/items/{id}` body `{"status":"hidden"}` 或 `{"status":"active"}` 隐藏/恢复；
  - `GET /api/v1/memory/extractions?companion_id=...` 最近任务状态、尝试次数、错误码和 usage；
  - `POST /api/v1/memory/extractions` body `{"companion_id":"..."}` 立即排队处理一批（最多 8 条历史事件），记忆页“立即提炼一批”使用它；
  - `POST /api/v1/memory/search`、`POST /api/v1/memory/corrections` 沿用原有检索/纠正。
- 隐藏、已替代或争议的同内容事实不会被后续提炼重新写成有效记忆；原始事件仍保留在归档中。
- 生命周期（借鉴 LivingMemory 设计）：每条记忆有重要度、类型化 TTL、到期时间、访问次数与衰减方式；被真正注入上下文会强化并延长到期；Gateway 每 6 小时自动把到期记忆置为 `expired`（保留正文与来源），页面“运行维护”可手动触发。记忆页支持按最新/重要度/即将到期排序、查看生命周期、标记重要。
- 召回测试：记忆页输入一句话可查看当前命中、路线（结构化/关键词）、分数、重要度/新鲜度；只读展示，不注入对话，也不会强化记忆。
- 生命周期 API：`PATCH /api/v1/memory/items/{id}` 可带 `status` 或 `importance`；`POST /api/v1/memory/recall-test`；`POST /api/v1/memory/maintenance`。
- 记忆概览/设置：页面顶部指标（有效/隐藏/过期/替代/待提炼事件/任务数）与重要性/类型分布；设置卡可改自动提炼、每批事件数、候选上限、超时、自动维护与间隔；`GET /api/v1/memory/stats`、`GET/PATCH /api/v1/memory/settings`。设置写入 `data/settings.json` 的 `memory` 段，重启后恢复。
- “配置嵌入模型”按钮跳到模型与密钥的嵌入 Tab；当前只保存配置，向量适配器尚未接入，页面显示“向量：未接入”。
- 任务失败会按既有 outbox 退避重试，最多 5 次后 `failed`；原始事件和已成功记忆不受影响。没有独立 worker 进程，任务随 Gateway 运行。
- 阶段一未实现：时间有效期/冲突状态/变更历史、记忆管理页面、隐藏/删除单条记忆、每日费用上限、embedding/混合检索；`ContextBuilder` 仍会全量扫描可见事件做索引，历史很长后需要增量水印。

## NAS 后续环境记录

按用户要求本地测试后再迁移。只读确认 NAS 为 fnOS、Docker 28.5.2、RTX 4060 8188 MiB；当时其他模型服务占约 7.4 GiB。未停止任何既有服务，未修改 NAS 文件、端口、防火墙。当前没有 TTS 余量实测。

## Agent 能力（源码已整合，服务待确认重载）

2026-10-25 增补：检索服务可选 DeepSeek，使用独立保存的 DeepSeek API Key，固定调用官方 POST /v1/tools/search 和 /v1/tools/fetch。搜索返回摘要；模型可按需调用 web_fetch 读取正文。两者共用联网开关、调用次数、超时和结果长度限制。搜索词最多128字符；抓取结果限256KiB传输并按当前结果字符预算截断。不会把密钥发往目标网页。下述“仅摘要”边界适用于 SearXNG/Brave。本次定向26项测试通过，未使用真实密钥调用计费接口。

左侧“系统 → Agent 能力”管理总开关、记忆/表情包/联网工具及调用次数和超时。默认关闭；模型需要支持原生function calling。联网使用自备SearXNG JSON实例或Brave密钥，未配置则不向模型提供联网工具。保存后下一轮生效；关闭开关会阻止在途工具继续执行。密钥不回显，不写settings.json。

结果仅为搜索摘要及来源URL，不代表读取网页全文。没有Shell、本地文件或任意MCP能力。开启Agent时最终正文等待最后一轮模型请求完成，关闭时保留原流式。源码整合与自动回归已完成，真实Gateway尚未重启；需用户确认重载后才加载该页面，不能从源码状态推断已上线。语音情绪标记继续由服务端剥离，Agent最终回复也经过同一处理。
