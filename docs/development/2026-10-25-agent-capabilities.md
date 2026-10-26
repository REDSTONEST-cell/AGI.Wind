# Agent 能力开发与整合说明

## 2026-10-26 电脑文件工具与审批链路收尾（本机真实模型实测）

本轮在原整合结果上补齐电脑文件工具、审批与管理员重启链路，并完成真实模型端到端验收：

- 审批等待超时现在统一转为 `AppError(TOOL_APPROVAL_TIMEOUT)`，不再把裸 `asyncio.TimeoutError` 交给通用异常分支；`ApprovalQueue` 的超时可注入，便于单元测试。
- 相对路径文件工具会自动创建 `data/agent-workspace` 工作区；工作区内的嵌套路径写入会创建父目录。删除了旧的“只有直接父目录等于工作区才创建”的死代码。
- `scripts/request-agent-admin.ps1` 重写：UAC 提权后不再调用会重跑数据库初始化且强制 `127.0.0.1` 的 `start-local.ps1`，而是读取 `data/gateway-host.txt` / `gateway-port.txt`，保留当前绑定主机（本机当前为 `0.0.0.0` 以同时服务内网 IPv4 和公网 IPv6），只重启 Gateway、保留语音进程，并将结果写入 `data/agent-admin-restart.log`。提权重启会清空浏览器内存会话，刷新页面后本机回环会自动重新登录。
- 新增测试：审批超时类型与清理、空工作区/相对路径创建、嵌套写入；新增 `scripts/accept-agent-files.py` 真实模型验收脚本与 `scripts/verify-agent-approval.mjs` 审批弹窗 Chromium 校验。

验收结果（真实 DeepSeek 对话模型 + 本机 Gateway + 真实文件系统）：

- 审批通过：模型调用 `list_files(C:\Users\wind\Desktop)`，弹窗进入待批准队列后被批准，工具轨迹 `completed`，回复正常带出目录内容。
- 审批拒绝：模型再次请求同一操作，拒绝后本轮仍正常完成，工具轨迹为 `failed / TOOL_APPROVAL_DENIED`，回复自然说明被拒绝。
- 验收使用临时会话并在结束后删除；`file_access_mode`、自动记忆提炼开关和原先的活动会话均已恢复。关闭自动提炼是为了避免合成测试消息进入长期记忆。
- Chromium 审批弹窗（非模态、显示路径、允许/拒绝、0 JS 异常）通过；后端相关测试 14 项通过，全量回归见下方验证段。

## 2026-10-26 命令行执行工具（参考 AstrBot Agent Computer Use）

用户要求增加可在电脑上执行命令的 Agent 工具，参考 [AstrBot 的 Agent 沙盒环境](https://docs.astrbot.app/use/astrbot-agent-sandbox.html)（[英文版](https://docs.astrbot.app/en/use/astrbot-agent-sandbox.html)）。AstrBot 给 Agent 提供 `astrbot_execute_shell` / `astrbot_execute_python`，并按成员/管理员矩阵控制“允许执行代码、执行时联网、文件访问范围”；Windows 本机环境不支持进程隔离，因此其本机执行依赖管理员权限与整个环境访问。

本项目是单用户本地陪伴，采用更适合的安全模型：

- 新工具 `run_command`，Windows 通过 `cmd.exe`、其他系统通过 `/bin/sh` 执行一条命令，返回退出码、stdout、stderr。
- 设置新增 `command_access_mode`（`disabled` / `ask` / `full`，默认 `disabled`）、`command_timeout_seconds`（默认 30，1–300 秒）、`command_max_output_chars`（默认 8000，500–16000）。
- `ask` 模式下每次执行都会进入既有审批弹窗（与文件工具共用队列），批准是单次、绑定具体命令与工作目录；拒绝、超时或对话切换都会阻止执行。`full` 模式跳过审批，`disabled` 不向模型暴露工具。
- 默认工作目录为 `data/agent-workspace`，可传相对或绝对 `cwd`；不存在的目录直接报错。命令以网关进程用户权限运行并可联网——Windows 没有本机沙箱，审批是唯一人工闸门，因此默认关闭、并在页面与提示词中明确警告不要执行破坏性、提权、后台常驻或长期等待的命令。
- 工具轨迹只持久化命令、工作目录、退出码和是否截断，不把 stdout/stderr 再复制进档案/日志；完整输出只进入当前模型请求，并受 `max_result_chars` 二次限制。
- 超时会用 `taskkill /F /T`（Windows）或 kill（POSIX）终止整棵进程树；输出解码优先 UTF-8，其次系统 locale/GBK。

真实模型验收（`scripts/accept-agent-command.py`，2026-10-26）：

- 批准：模型调用 `run_command("echo agent-command-ok")`，审批弹窗显示命令/工作目录/超时，批准后退出码 0，回复带出输出；轨迹 `completed` 且未持久化 stdout。
- 拒绝：第二次调用被拒绝后命令未执行，轨迹 `failed / TOOL_APPROVAL_DENIED`，回复正常说明被拒绝。
- 使用临时会话并删除，测试期间关闭自动记忆提炼、结束后恢复；活动会话与 Agent 设置恢复。当前本机 `command_access_mode=ask`（可在 Agent 页面切回关闭或完全允许）。
- 验证：`tests/unit/test_agent_commands.py` 7 项（审批/拒绝/完整模式/超时/输出上限/工作目录/关闭）与设置 API 集成测试通过；设置页与审批弹窗 Chromium 校验通过。

## 2026-10-25 主工作区整合结果

已根据用户转交的交接要求，将 `d22b02f..7c199e2` 的功能差异整合到 `C:\Users\wind\Desktop\AGI.Wind`，没有执行整分支 merge，没有将旧快照覆盖到当前语音版本。现有局域网启动脚本内容相同，保留原文件。Git 原分支与既有未提交成果均保留，未批量暂存或提交。

合并前备份位于 `.devtools/agent-integration-20261025/`：`before/` 保存本轮涉及的原文件，`manifest.json` 记录原始摘要与是否存在，`original-tracked.patch` 保存原有已跟踪文件的未提交差异。`agent-integration.patch` 为功能应用时的差异；不包含之后新增的组合测试与本段记录。备份不含本机密钥或运行数据。

实际代码冲突只有两处：

- `delivery/service.py`：保留 `EmotionTagStream`、流式隐藏情绪标记、语音排队及失败降级，把 Agent 最终回复交给相同的标记过滤流程。
- `api/client/app.js`：保留新版 `audio.js` 导入、`stopAllAudio` 和资源版本参数，追加 Agent 管理页入口。

四个运维文档存在追加位置冲突，保留两边内容。`voice/emotion.py`、语音路由、客户端 `audio.js` 与语音实现文件均未被旧快照替换。设置持久化同时保留 memory、voice、agent 各段。

整合验证：完整后端 **183 passed, 2 skipped，56.38 秒**；两个 skip 为需要显式授权的厂商/语音实测。协议检查通过，Node 分段 **5/5**。Chromium 的 Agent 设置（1440/390）、语音开关/概率/音色/历史音频卡片、图片聊天（1440/1024/390）均通过，0 JS 异常。浏览器运行在独立 18441 测试服务与临时 PostgreSQL schema，语音由合成测试引擎提供；不代表真实 Breeze 重新验收。新增 Agent 工具查询后隐藏情绪标记并生成语音的组合回归通过。

**尚未上线**：未重启 Gateway 或 Breeze，未修改真实 settings.json，未开启 Agent，未调用真实模型或搜索服务。临时测试服务已停止。AOCI 当前仍未对齐；本次最终维护结果保存于 `.devtools/agent-integration-20261025/aoci-maintain.json`，不将已有索引当作最新代码事实。用户确认后再安排服务重载与最小实测。

以下保留独立分支阶段的开发记录，其中“待合并”描述属于整合前历史状态。

用户选择：站内工具＋联网检索。开发分支 `codex/agent-capabilities`，工作区 `C:\Users\wind\.codex\worktrees\agent-capabilities\AGI.Wind`。

原工作区 `C:\Users\wind\Desktop\AGI.Wind` 仍交给 DeepSeek 开发。本分支的 `d22b02f` 是2026-10-25开发中源码的隔离快照，包含先前多人工作，不是本轮新增功能；**不得把快照提交整体合入原分支**。功能提交从该快照之后开始，核心提交 `ac747d8`。

## 已实现

- 复用当前对话模型的原生 function calling，限轮次/调用次数/超时/结果长度；达到调用预算后尝试一次禁止工具的最终回答。
- 工具：search_memory、search_stickers、send_sticker、web_search。作用域来自服务端已受理的生成，不允许模型指定账号或角色。
- 表情包先查再发，每轮最多一张；活动状态、归属、附件引用、计数与SSE在现有事务中更新。Agent回合关闭旧的自动发图，图片观察/收藏流程保留。
- 工具计划不进入聊天正文；模型提供的reasoning与真实工具开始/结果概况进入已有日志/档案轨迹。记忆查询原文只进入当前模型请求，日志仅记录命中数和来源ID，避免产生无法随来源删除的原文副本。取消、epoch、记忆版本失效沿现有路径收敛。
- 左侧系统区新增“Agent 能力”：总开关、逐项工具开关、轮次/次数/超时/结果数量限制、搜索服务配置与密钥管理。
- 设置保存到现有settings.json的agent段；密钥在SecretStore，API不回显；Brave和SearXNG密钥引用分开，切后端不会转发另一后端的密钥。
- 联网支持管理员指定SearXNG JSON端点或固定Brave Search API。禁止重定向，不跟随搜索结果链接。未配置时明确不可用。

## 使用与边界

默认关闭。合并并加载新版Gateway后，在“Agent 能力”开启所需工具；当前对话模型必须支持原生工具调用。SearXNG需自备可用实例并开启JSON输出；Brave需API密钥。没有自动选择公共搜索实例，没有安装新服务，也没有替用户申请密钥。

Agent模式每轮调用采用有响应长度上限的非流式原生请求，最终回复等最后一轮完成再进入既有分段显示；关闭Agent时保留原有流式体验。思考字段只在当前工具循环临时回传给需要它的厂商，不作为未来对话历史记忆重新注入。

联网仅返回搜索摘要和来源URL，尚不抓网页全文。没有任意MCP、命令执行、记忆写入工具或自主定时任务。调用次数与超时限制不等于精确货币预算。此轮没有调用真实厂商或搜索计费接口，端点真实可用性仍需配置后的必要小样例验证。

> 2026-10-26 更新：本地文件访问工具 `list_files / read_file / write_file / download_file` 已接入，受 `file_access_mode`（关闭 / 逐次批准 / 完全访问）约束；逐次批准模式下每次操作都会进入浏览器审批弹窗，拒绝/超时只终止该操作，不终止整轮回复。文件工具仍无 Shell/MCP/命令执行能力。

## 验证

- 隔离快照基线：147 passed, 1 skipped。
- 修复审查问题后的最终完整回归：176 passed, 1 skipped，68.42秒；默认跳过live测试。
- Node分段测试5项通过。
- Chromium管理页：保存、密钥清空/遮蔽、刷新恢复、1440/390宽度、无横向溢出、0 JS异常。接口写入用mock；页面由独立测试Gateway提供，使用专用PostgreSQL schema，无真实用户聊天。
- 独立审查发现来源删除后工具结果副本残留、并发PATCH丢更新；均已写出失败测试复现并修复。Agent设置读改写及配置文件持久化使用同一进程写锁。
- AOCI维护已尝试，返回stopped/blocked、aligned=false，提示需处理存量Observe复核及孤儿/范围阻断；没有提交缺乏证据的语义。用户要求节省额度并加快收尾，本阶段停止额外治理循环，后续整合时处理，不能声称已对齐。

后端验证需要原项目已存在依赖；隔离worktree没有重装依赖。PowerShell示例（不打印数据库连接）：

```powershell
$env:PYTHONPATH='C:\Users\wind\.codex\worktrees\agent-capabilities\AGI.Wind\app\src'
$env:PYTHONDONTWRITEBYTECODE='1'
$env:PYTHONIOENCODING='utf-8'
$env:TMP='C:\Users\wind\Desktop\AGI.Wind\.devtools\agent-temp'
$env:TEMP=$env:TMP
$env:AGI_TEST_DATABASE_URL=(Get-Content 'C:\Users\wind\Desktop\AGI.Wind\data\dev-db.json' -Raw | ConvertFrom-Json).url
Set-Location 'C:\Users\wind\.codex\worktrees\agent-capabilities\AGI.Wind\app'
& 'C:\Users\wind\Desktop\AGI.Wind\app\.venv\Scripts\python.exe' -m pytest -q -p no:cacheprovider
```

## 后续合并

需要明确的DeepSeek完成信号，不能把“文件暂时没变化”当作完成。主工作区仍有大量未提交成果；不reset、不clean、不批量覆盖。

先记录主工作区最终HEAD与文件指纹，在临时整合区对比本分支隔离快照与DeepSeek最终内容，再应用功能提交或三方合并。重点复核core/settings.py、delivery/service.py、api/app.py和客户端app/index/styles；并行语音改动可能触及这些位置。保留DeepSeek的模型/语音/记忆设置，尤其settings.json各段的读写。

合并后重新跑后端、分段与相关客户端回归，再完成目标仓库AOCI维护；确认没有活动生成/训练任务后再安排服务重载。不得为了本分支验收中断原服务或语音任务。
