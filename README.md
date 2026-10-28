# AGI.Wind

本地优先的 AI 伴侣 / 桌面陪伴系统。后端是 Python + FastAPI，前端是仓库内的单页 Web 客户端；数据默认保存在本机 PostgreSQL，语音、语音识别、记忆和 QQ 都是可选增强，不配置也能正常文字聊天。

> 当前以本地开发和本机运行为主；服务器部署是可选步骤，不会自动同步。

## 当前状态

- 本地 Gateway / Web：`http://127.0.0.1:18440/`
- 默认监听：`127.0.0.1`；局域网访问使用 `启动服务端(局域网).cmd`
- Python：`>=3.12,<3.13`
- 数据目录：`data/`（运行时生成，默认不进入 Git）
- 完整 Python 测试套件通过；真实模型、GPU 和第三方服务测试按环境跳过

## 功能

### 对话与人格

- 单活跃对话 + 历史归档；可把归档对话恢复为当前对话
- 人格档案版本化：角色设定、关系、承诺、头像、用户头像
- 流式回复、原生推理日志、系统日志抽屉
- 动态分段：回复条数由内容决定，简单回应可以只有一条，复杂内容可以四条五条；不固定三段
- 删除语义明确：归档页删除为永久删除，会清理事件、附件引用和对应记忆来源

### 记忆与上下文

- 主人私有记忆与群聊记忆池隔离
- 关键词 / 结构化 / 语义召回可选；无来源记忆保留但不伪造来源
- 记忆生命周期、重要度、访问强化、待提炼游标
- 上下文只强化实际选中的记忆；检索本身不改变记忆

### 语音

- 本地 Breeze-TTS-2 文字转语音；支持保存和切换克隆音色
- 回复可自动带语音，也可在用户明确要求时强制生成
- 语音分段自动排队播放，历史记录可回放
- 浏览器麦克风语音输入；实时通话支持持续识别、流式播放和打断
- TTS / ASR 服务不可用时只影响语音，文字聊天继续工作

### 图片、表情包与视觉

- 输入框选图、粘贴图片、纯图片发送，最多 4 张
- 图片回合：观察 → 回答 → 二次思考 → 真实收藏 / 模仿
- 表情包库：上传、改名、标签、隐藏 / 恢复、删除
- 识图模型可独立配置

### 主动与独处

- 回复后独处思考（默认关闭，可配置延迟、轮数、每日上限）
- 主动消息受总开关、安静时段、最小间隔和每日上限约束
- 独处产生的思考只进入日志和档案，不自动伪装成用户消息

### QQ / OneBot

- 支持 QQ 私聊与群聊
- 群聊有发言人标记、消息去抖、群聊注意力与跟进策略
- 需要外部 OneBot 实现（例如 SnowLuma）；本仓库不包含 QQ 客户端
- 群聊不会继承主人的私密关系、承诺或私聊内容；群聊不注入环境感知

### 环境感知（Ambient）

默认关闭，需在 Web 设置页明确开启。

- 周期采样当前前台窗口、进程和空闲时间
- 可选截图，仅截当前前台窗口，不截整块桌面
- 默认排除密码管理器、银行、支付、验证码、无痕 / 隐私窗口等
- 按小时限制截图数量，按保留期自动清理
- 观察数据只存在本机数据库；采样器本身不调用模型
- 只有主人私有的 Web 对话会注入简短环境摘要；群聊和 QQ 私聊不注入

## 目录结构

```text
app/
  src/agi_wind/
    api/            FastAPI 应用与 Web 客户端
    agent/          有界模型 / 工具循环
    ambient/        环境感知采样、模型、设置
    archive/        归档、事件、删除账本
    core/           配置、类型、密钥、错误
    delivery/       生成、分段、语音投递、主动 / 独处思考
    identity/       人格与用户档案
    integrations/   QQ / OneBot 等集成
    memory/         记忆、检索、上下文、生命周期
    providers/      模型提供方适配
    voice/          TTS、ASR、实时通话
  migrations/       数据库迁移
  tests/            Python 测试
config/             示例配置（*.local.* 不提交）
docs/               开发记录、运维、验收文档
scripts/            本地启动、停止、状态和验证脚本
data/               运行时数据（不提交）
```

## 快速开始（Windows 本机）

### 1. 准备依赖

- Windows 10 / 11
- Python 3.12
- 本地 PostgreSQL（项目脚本可管理开发用实例）
- 可选：Breeze-TTS、ASR 模型、记忆嵌入模型

安装 Python 依赖：

```powershell
cd app
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
```

详细依赖准备见 [运维说明](docs/ops/runbook.md)。

### 2. 准备配置

```powershell
copy config\app.example.yaml config\app.local.yaml
copy config\providers.example.yaml config\providers.local.yaml
```

- `config/app.local.yaml`：数据目录、监听、主动 / 语音 / 环境感知设置
- `config/providers.local.yaml`：模型提供方和模型 ID；密钥只在服务端保存
- 语音 / ASR / 记忆配置分别参考 `config/voice.example.json`、`config/asr.example.json`、`config/memory.example.json`
- 所有 `*.local.*`、`data/`、证书和密钥都不应提交

### 3. 启动

双击：

- `启动本地服务.cmd`：启动本地 PostgreSQL 和 Gateway（只监听 `127.0.0.1`）
- `启动服务端(局域网).cmd`：监听 `0.0.0.0`，供手机 / 其他电脑访问
- `查看本地状态.cmd`：查看服务、数据库、模型与访问地址
- `停止本地服务.cmd`：停止服务，保留数据

浏览器打开：**http://127.0.0.1:18440/**

本机访问会自动建立 HttpOnly 会话；独立客户端使用 Bearer 密钥，默认开发密钥为 `123456`，正式使用前必须更换。

## 配置与端口

| 组件 | 默认地址 | 说明 |
| --- | --- | --- |
| Gateway / Web | `127.0.0.1:18440` | FastAPI + 静态客户端 |
| PostgreSQL | `127.0.0.1:55432` | 项目本地开发实例 |
| Breeze TTS | `127.0.0.1:8137` | 可选，`scripts/start-voice.ps1` |
| ASR | `127.0.0.1:8139` | 可选，`scripts/start-asr.ps1` |
| 记忆模型 | `127.0.0.1:8141` | 可选，`scripts/start-memory-model.ps1` |

配置入口：

- `config/app.local.yaml`
- `config/voice.local.json`
- `config/asr.local.json`
- `config/memory.local.json`

密钥、QQ token、证书、`data/settings.json` 只在本地；不要提交到 Git。

## 测试

```powershell
cd app
.\.venv\Scripts\python.exe -m pytest tests -q
```

前端 / 客户端脚本可用 Node 单独验证，例如：

```powershell
node scripts/test-reply-segments.mjs
node scripts/test-audio-autoplay.mjs
node scripts/test-log-view.mjs
```

真实模型 / GPU 测试默认跳过，需要在本地配置提供方后显式运行。

## 文档

- [运维说明](docs/ops/runbook.md)
- [环境说明](docs/ops/environment.md)
- [实施进度](docs/ops/progress.md)
- [验收记录](docs/ops/acceptance-report.md)
- [实时通话](docs/ops/realtime-call.md)
- [开发记录](docs/development/2026-10-25-development-record.md)

## 安全与隐私

- 默认只监听回环地址；局域网模式请确认网络可信
- 默认开发密钥 `123456` 必须更换
- 环境感知默认关闭，开启前请阅读设置页说明
- 群聊 / QQ 场景不继承私密记忆，也不注入环境感知
- 不要把 `data/`、`config/*.local.*`、证书、模型密钥或 QQ 凭据提交到仓库

## 第三方组件

TTS、ASR、嵌入模型、PostgreSQL、Caddy 等第三方组件的版本与许可见 [第三方许可](docs/ops/licenses.md) 和 [版本记录](docs/ops/versions.md)。
