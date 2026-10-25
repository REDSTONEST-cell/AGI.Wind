# AGI.Wind 实施进度

计划：docs/superpowers/plans/2026-10-25-agi-wind-companion-plan.md
更新：2026-10-25，Asia/Shanghai。

## 当前阶段

用户最新顺序：**先 Windows 本地部署服务端并测试，测试完成后再部署 NAS**。当前交付本地服务端基础版本；完整 T01—T15 尚未完成。

- Gateway：127.0.0.1:18440，app/.venv 独立环境。
- 浏览器入口为 http://127.0.0.1:18440/（HTTP 200 状态页）；/health/ready 是可能返回 503 的机器健康接口，不能作为网页入口。
- PostgreSQL 17.6：127.0.0.1:55432，data/dev-postgres；业务库 agi_wind_local，测试库 agi_wind_test。不注册系统服务。
- 角色明确标记为开发测试；无正式人格、厂商凭据或声音素材。
- 本地开发显式使用 direct_vendor，尚无聊天模型。Letta 未接入，不自动回退、不标为可用。
- readiness 为 degraded/HTTP 503：DB/媒体可用，聊天/视觉/语音未配置，向量暂为 keyword_only。
- NAS 只做 SSH 环境读取，未上传/安装/启动本项目；SSH 已关闭，密码未写项目文件。

## 已通过检查

- 62 项 Python 测试通过；数据库集成使用真实 PostgreSQL 的随机独立临时 schema，测试后清理，没有 SQLite 替代。
- 网页入口回归：模型未配置时主页仍返回 200 HTML，底层健康接口继续返回真实 503；在实际浏览器确认状态展示与刷新按钮。截图 output/local-service-homepage.png。
- Python→JSON Schema→TypeScript 协议生成/漂移检查、生成 TS 严格编译通过。
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
| T04 | 增量切片、中文关键词/精确检索、预算、纠错 | BGE CPU 实测、pgvector、混合向量检索与空间迁移；不能标完成 |
| T05 | 鉴权 HTTP、文字 SSE/控制流、厂商与视觉离线测试 | 真实厂商、兼容端点和完整客户端接口 |
| T06 | 待实施 | Letta bridge、SDK 锁版与能力探针 |
| T07 | outbox 抢占/租约/重试、来源候选校验 | 提炼触发/费用限制/自传整理未接常驻 worker；目前 worker 只处理生成 |
| T08 | 基础导出/空库恢复、clone 来源映射、关键词重建、删除账本 | pg_dump/轮换、replace、声音/头像包、并行向量迁移、真实异盘账本 |
| T09—T14 | 待实施 | ASR/TTS、播放/SDK/Live2D、训练工具链、EverOS、主动互动 |
| T15 | 本地部署与基础 HTTP 演练 | 全链路 A01—A20、性能和 NAS 部署 |

## 决策记录

- Ruling: 非 Git 目录原地建立 app，不创建工作树或纳入旧大工具/资产。成本：无提交级历史，使用源码文件/进度/源码包恢复。
- Ruling: docs/ops/progress.md 为执行账本，跳过依赖 Git/Bash 的 SDD 脚本。成本：无脚本生成的提交范围证据。
- Ruling: 优先可验证数据与本地 API 基础，未完成项逐项记录。成本：此版本不是完整 AI 伴侣。
- Ruling: 本地开发显式 direct_vendor，不自动 fallback，Letta 后续接入。成本：Letta 行为无实测。
- Ruling: 自动事实只接受可逐字验证的抽取，无出处/不受原文支持的改写隔离。成本：记忆写入较保守，推断流程后续补齐。
- Ruling: 暂用字节预算上界和关键词检索，不伪称向量语义检索。成本：同义表达召回有限。
- Ruling: 不开放覆盖恢复；缺最新账本的角色不可查询。成本：恢复需要明确模式/账本输入。
- Ruling: 用户最新要求本地优先，NAS 不作写入，不停止既有 GPU 服务。
- Final review: 按交接串行实施，作者自查；新增 6 项失败用例，修复错误供应商路由、正确路由解析、图像独立限额、缺账本误激活、导入媒体冲突及 HTTP 数据库异常脱敏；全部 RED→GREEN，完整 61/61。未做独立人员审计。

## 下一任务与待输入

1. 继续本地补 BGE/pgvector、Letta、提炼调度和语音；当前基础服务不能替代完整方案验收。
2. 用户后续配置厂商/模型后，实测一条文字和一张合成图片；不在聊天收取或回显 API 密钥。
3. 正式客户端名称/源码、角色设定、用户声音素材和独立备份目录仍待提供。
4. 本地完整链路通过后再部署 NAS。NAS 当时 8188 MiB 显存已用约 7391 MiB，Bonsai 占大部分；不能视为 TTS 可用余量。
