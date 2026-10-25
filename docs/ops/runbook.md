# 本地服务端运维

目录：C:/Users/wind/Desktop/AGI.Wind。当前是 Windows 本地基础部署，NAS 尚未安装。

浏览器请打开 **http://127.0.0.1:18440/**，这是返回 HTTP 200 的服务状态网页。`/health/ready` 是机器健康接口，模型未配置时返回 HTTP 503，不作为浏览器网页入口。

## 启动、停止、状态

双击根目录的三个中文 CMD 文件，或在本目录 PowerShell 执行：

```powershell
.\scripts\start-local.ps1
.\scripts\status-local.ps1
.\scripts\stop-local.ps1 -StopDatabase
```

Gateway 只监听 127.0.0.1:18440；PostgreSQL 只监听 127.0.0.1:55432。不注册 Windows 服务，不修改防火墙，不接管其他数据库。未设置开机自启，电脑重启后运行启动脚本。

启动复用 app/.venv、.devtools/pgsql 和固定开发角色。停止先走带认证的正常退出，再停专属数据库；Gateway 未退出时保留数据库，不结束未知进程。

## 文件

| 路径 | 内容 |
|---|---|
| app/src/agi_wind | 业务源码 |
| app/.venv | 专属 Python 3.12 环境 |
| app/uv.lock、pnpm-lock.yaml | 直接/传递依赖锁 |
| .devtools/pgsql | 便携 PostgreSQL 17.6 |
| data/dev-postgres | 数据库 cluster；运行中直接复制不等于有效备份 |
| data/local-db.json | 含秘密的本机 DB 连接配置，不提交 |
| data/access-token.txt | 本地 API 访问凭据，不输出到聊天/普通日志 |
| data/local-identity.json | 固定账号/用户/角色/会话 ID，明确为开发角色 |
| data/assets | 内容寻址媒体 |
| data/governance/deletions.ndjson | 独立删除账本，不能被旧备份覆盖；缺失时拒绝开放角色 |
| config/app.local.yaml | 本地配置，尚无厂商/模型，排除提交 |
| data/gateway.*.log | 运行日志，不打印秘密和完整 prompt |
| data/local-smoke/report.json | 实际 HTTP 验证证据，仅合成数据 |

当前不是 Git 仓库；.gitignore 已排除秘密、数据、环境和原有工具/素材。NAS SSH 密码未写项目文件。

## 模型与能力

用户决定稍后配置模型。config/providers.example.yaml 是结构样例；本机入口 config/app.local.yaml，credential_ref 引用环境变量。未把示例域名/模型当作可用配置。配置后重启，实际请求通过后再标聊天可用。

本地开发显式使用 direct_vendor；Letta 未接入，不自动回退。切换供应商会重新解析对应客户端，未配置的供应商明确拒绝。当前为开发人格，正式人格与声音待用户指定。

`GET /health/live` 只证明存活。`GET /health/ready` 当前为 HTTP 503：DB/媒体 available，模型/语音 unconfigured，embedding 为 keyword_only。

## 已开放 API

`/api/v1` 需要 `Authorization: Bearer <本地凭据>`，健康检查无需认证。凭据不能放长期 URL 查询串。

- capabilities、companions、创建 conversations。
- chat/requests：先事务落盘再 202，相同 request_id 复用任务。
- generations/{id}、generations/{id}/events：状态/SSE，Last-Event-ID 为整数 sequence。
- generations/{id}/interrupt：command_id 幂等打断。
- conversations/{id}/live、conversations/{id}/events：控制流/分页历史。
- assets 上传和按 ID 授权下载。
- companions/{id}/persona：读取和 expected_version 更新；companions/{id}/model 修改未来请求模型。
- memory/search、memory/corrections：关键词检索和有来源纠错。
- archive/exports：同步基础导出及授权下载。
- DELETE events/{id}：删除事件及同轮派生输出，写账本并清理未共享媒体。

计划中的语音/SDK/兼容入口/导入任务 API 还未全部开放，以 progress.md 为准。

## 导出、恢复、重建

以下从项目根执行：

```powershell
.\app\.venv\Scripts\python.exe -m agi_wind.cli archive export --output data\exports\manual.zip
.\app\.venv\Scripts\python.exe -m agi_wind.cli archive verify data\exports\manual.zip
.\app\.venv\Scripts\python.exe -m agi_wind.cli index rebuild
.\app\.venv\Scripts\python.exe -m agi_wind.cli archive import data\exports\manual.zip --mode clone --latest-ledger data\governance
```

只有确认账本为最新权威版本时才提供 `--latest-ledger`；未提供、文件缺失时只能隔离恢复，不能查询角色。默认新命名空间拒绝 ID 冲突；覆盖恢复尚未开放。当前 index rebuild 仅重建关键词切片，原始事件/人格不变；pgvector 向量未实现。

开放包不含 API secrets 和 Letta 私有历史。声音/头像包、原生数据库备份/轮换和异盘恢复仍待后续任务；基础包通过不等于完整迁移验收。

## 开发验证

```powershell
cd app
& "$env:USERPROFILE\.local\bin\uv.exe" run pytest -q
cd ..
pnpm contracts:check
pnpm exec tsc --noEmit --strict --target ES2022 --module NodeNext --moduleResolution NodeNext packages/contracts/types.ts
.\app\.venv\Scripts\python.exe scripts\smoke-local.py
```

数据库集成测试使用 agi_wind_test 中随机临时 schema，不触碰业务库。smoke-local 写一条合成经历，并确认未配置模型时 MODEL_UNCONFIGURED；不能把它当作真实模型验收。

源码恢复包不含 PostgreSQL、依赖环境、数据和秘密。新电脑需 uv sync 安装依赖，并从 versions.md 的官方来源取得便携 PostgreSQL 包；setup-dev-postgres.py 仅初始化专属本地实例。

## NAS 后续

按用户要求本地测试后再迁移。只读确认 NAS 为 fnOS、Docker 28.5.2、RTX 4060 8188 MiB；当时其他模型服务占约 7.4 GiB。未停止任何既有服务，未修改 NAS 文件、端口、防火墙。当前没有 TTS 余量实测。
