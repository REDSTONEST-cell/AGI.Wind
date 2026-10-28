# 环境核实

2026-10-25。工作目录 C:/path/to/AGI.Wind；原目录不是 Git 仓库。

业务根为 app/；共享协议 packages/contracts/；配置 config/；运行数据 data/，与业务源码分离。tools/、research/、output/live2d/ 原件保留且排除提交。

本机 Windows 11 专业版 x64，约 32 GiB RAM。已有 Python 3.12.14、Node 24.21.0、pnpm 11.7.0 和 uv。业务使用 app/.venv，不改全局 Python 环境。不安装 GPU 模型依赖到业务环境。

现有 Live2D 为 output/live2d/WindChibi，入口 WindChibi.model3.json，ParamMouthOpenY 口型、Idle/Talk 动作。viewer/server.cjs 固定 127.0.0.1:18866，只是模型预览，尚非正式聊天客户端。未找到正式客户端源码。

用户已确认目标为既有 fnOS NAS；厂商和模型由用户后续配置。历史地址/硬件记录须只读复核后才写部署配置。当前未进行 NAS 写入或安装。

正式角色核心设定、声音素材及训练授权目录、备份控制目录尚未提供。测试只使用合成数据；真实厂商/GPU/客户端/试听验收独立记账。

本地 PostgreSQL 已位于 .devtools/pgsql，实例 data/dev-postgres，只监听 127.0.0.1:55432；业务库 agi_wind_local、测试库 agi_wind_test 分离。不注册 Windows 服务。Gateway 已部署在 127.0.0.1:18440。版本/哈希见 versions.md，测试不使用 SQLite 替代。

用户最新要求先本机部署测试，之后再 NAS。已通过 SSH 只读确认：fnOS Linux 6.18.18.c1126-trim，Docker 28.5.2，/vol2 约 124 GiB 可用，RTX 4060 共 8188 MiB，当时已用 7391 MiB，Bonsai 占大部分；127.0.0.1:5432 已有服务。未在 NAS 安装本项目，未停止其他服务。SSH 已关闭，凭据未保存到项目。

本机 RTX 4060 Laptop / 8188 MiB，桌面基线约 1.5–2.3 GB；Breeze-TTS-2 Q8_0 + Vulkan 语音服务常驻约 5.9 GB，剩余余量约 2.2 GB。模型/运行包/音色位于 `C:\path\to\Models\Breeze-TTS-2\`，不进入业务源码；`config/voice.local.json`（不提交）记录 binary/model/voices_dir。语音服务只监听 127.0.0.1:8137，由 Gateway 代理，局域网客户端不直接访问 GPU 服务。
