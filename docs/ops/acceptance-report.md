# 本地基础版本验收记录

2026-10-25，Asia/Shanghai。完整方案未完成，本记录不替代 A01—A20 全链路验收。

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
| A08 | 无出处/AI 伪用户事实隔离 | 实际提炼模型和调度 |
| A09 | 原图/合成观察/引用归档 | 真实视觉模型 |
| A10 | 未实施 | TTS 合成、解码/GPU |
| A11 | 待素材与实施 | 微调权重/清单/用户试听 |
| A12 | 打断 epoch/幂等控制测试 | 真音频/口型/时延 |
| A13 | 并发单任务/共享人格 | 两设备真实播放回执 |
| A14 | Live2D 原件保留 | SDK/未知动作回落 |
| A15 | 文字失败保留 partial | TTS/OOM 降级 |
| A16 | 账号/会话/媒体/检索隔离 | 正式客户端验证 |
| A17 | 未实施 | 训练/在线互斥与回滚 |
| A18 | 配置默认关闭 | 主动调度/安静时段 |
| A19 | 未实施 | ASR/原音频/转写修正 |
| A20 | 基础 fsync/hash 链、旧包防复活、缺账本隔离 | 外部副本物理清理、异盘账本/备份轮换 |

## 复核

按交接串行实施，作者自查，未做独立人员审计。6 项新增失败用例捕获供应商路由、媒体限额、缺账本激活、媒体 ID 冲突及 HTTP 异常脱敏，修复后完整测试通过。无真实厂商/GPU/语音/客户端/用户试听结果。

未初始化 Git 或提交旧资产；源码/锁文件/文件清单用于本轮代码恢复。任务状态、取舍和待条件见 progress.md。
