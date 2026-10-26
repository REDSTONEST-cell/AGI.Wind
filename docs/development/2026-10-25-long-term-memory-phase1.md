# 长期记忆阶段一：能力审计与最小可靠闭环

日期：2026-10-25。依据：桌面交接 `04`（当前工程）、`05`（记忆要求）、`06`（详细设计）。本轮实现的是**阶段一**，不是完整长期记忆。

## 1. 接手时能力审计

| 能力 | 接手时状态 | 证据/问题 |
|---|---|---|
| 原始经历事务归档 | 已存在 | `ArchiveService.accept` 同事务写 requests/generations/events/outbox；删除账本与墓碑已存在 |
| 幂等请求与恢复任务 | 已存在 | outbox + `Worker` 租约/重试/fencing；但 worker 只处理 `generation.requested` |
| 记忆表结构 | 已存在 | memory_items/source_refs/memory_relations/retrieval_chunks/embedding_spaces/quarantine（0003） |
| 中文关键词检索 | 已存在 | 二元组分词 + ILIKE + `simple` 全文 + RRF；仅 chunk 两路融合 |
| 结构化记忆召回 | 部分 | 只按 memory content 的 ILIKE 命中；不能按原始事件措辞命中，缺少来源文本路径 |
| 上下文预算 | 已存在 | 人格+输入+最多 12 条/3000 字节记忆；发请求前复核 memory_revision |
| 候选来源校验 | 部分 | 能查 event、scope、偏移与 quote_hash；但要求 content 逐字出现在引文中，只能抽取原句 |
| 纠正 | 已存在 | 写 correction 事件、旧记忆 superseded、新记忆 + supersedes 关系、memory_revision+1、失效在途生成 |
| 提炼适配器 | 只有占位 | `Extractor` 使用自由 JSON 数组提示，无严格 schema、无 quote 解析、无上限校验；生产未实例化 |
| 提炼触发 | 未接入 | `archive.accept` 只入队生成；`jobs/handlers.extraction_due` 未被调用 |
| 提炼 worker | 未接入 | 非生成事件直接抛 `HANDLER_UNCONFIGURED` |
| 任务幂等/游标 | 未实现 | 无已处理事件游标、无 source digest/任务去重，重试会重复提炼 |
| 提交前复核 | 未实现 | 无租约再验证、无来源内容哈希复核、无 cursor 竞态检查 |
| 提取后版本更新 | 未实现 | 新记忆写入不递增 memory_revision，不失效运行时投影 |
| 记忆列表/详情/任务状态 API | 未实现 | 只有 search 和 corrections |
| 时间有效期/冲突/版本历史 | 未实现 | memory_items 无 valid_from/to、replaced_by、change 表 |
| 记忆管理页面 | 未实现 | `#/memory` 只有搜索与纠正 |

## 2. 本轮交付

### 2.1 迁移 0009

- `companions.memory_extract_cursor_at` / `memory_extract_cursor_id`：按 `(recorded_at, id)` 的提炼游标。
- `events_companion_recorded_idx`：按角色和时间扫描新增事件的索引。
- 兼容旧库：新库由历史迁移的实时 metadata 直接带列，0009 用 inspector 只补缺失列；已验证“删列 + 版本回退到 0008 后 upgrade”可恢复。

### 2.2 严格提炼协议

- 模型只能返回 `{"schema_version":1,"candidates":[...]}`，候选最多 8 条，content ≤500 字，每条 1–8 个 quote。
- kind 为受控集合；epistemic_type 为既有枚举；未知字段/未知 kind 直接被 schema 拒绝。
- 每条 quote 必须是所给事件原文的连续片段；服务端在提交事务内唯一定位并计算 offsets/quote_hash。找不到、出现多次、来源已删/已变都隔离。
- `user_asserted` 只能来自 user 事件；`observed` 只能来自 vision_observation；`assistant_generated` 只能来自 assistant 事件；虚构标记强制为 fictional。
- JSON/schema 错误最多一次修复调用；仍失败则任务按 `EXTRACTION_INVALID` 重试，不截取“看起来像 JSON”的子串，不写任何记忆。
- 超时映射为 `EXTRACTION_TIMEOUT`；单次调用输入/输出字符数记入任务 payload。

### 2.3 可恢复、幂等的异步任务

```text
generation.completed
  └─ 同事务入队 memory.extract（dedupe_key = memory-extract:<companion>:<generation>）
短事务 A：校验租约 → 读游标与最多 8 条事件（角色/可见性过滤）
事务外：调用提炼模型（不持锁）
短事务 B：再次校验租约 → 复核游标未变 → 复核 memory_revision →
          逐条复核 source 可见性与 content_sha256 → 写入/隔离 →
          推进游标 → 记录 usage → 提交
```

- 崩溃在“数据已提交、worker.finish 未执行”之间时，重试从新游标继续；同批不会重复写。
- 游标被其他任务推进时返回 `EXTRACTION_OUTPACED` 重试，新游标下自然 noop。
- 记忆版本被纠正/删除改变时返回 `STALE_CONTEXT`，不在旧快照上覆盖。
- `Worker.run_job` 对 `LEASE_LOST` 不再尝试 fail 写库（旧实现会在租约丢失时二次抛错）。
- 生成主链路仍只等待归档；提炼失败只影响该 outbox 任务，聊天不回滚。

### 2.4 写入与检索

- 相同 content+kind+epistemic 的 active 记忆只补来源（REINFORCE），不产生同义重复卡。
- 新记忆写入后一次性递增 `memory_revision`、把 runtime binding 置 stale、失效在途生成；与现有纠正/删除语义一致。
- 结构化召回增加“来源事件原文匹配”：即使用户问“以后要叫我什么”，也能命中存成“用户希望被称为小风”的事实。
- 新增只读 API：
  - `GET /api/v1/memory/items?companion_id=&kind=&status=&limit=`
  - `GET /api/v1/memory/items/{memory_id}`（含来源 quote/occurred_at/available）
  - `GET /api/v1/memory/extractions?companion_id=`（状态、尝试次数、错误码、usage、结果计数）
- `/api/v1/capabilities` 与 `/health/ready` 显示 `memory_extraction` 为 `memory_extract` / `dialogue_fallback` / `unconfigured`。

### 2.5 配置默认值

`memory_extraction_enabled=true`、`memory_extract_batch_events=8`、`memory_extract_max_candidates=8`、`memory_extract_timeout_seconds=30`。配置了专用 `memory_extract` 角色时优先使用；未配置时回退当前对话模型；配置了但凭据不可用时不静默回退。

## 3. 验证

- 新增 21 项测试（12 集成 + 8 单元），覆盖：生成完成入队与去重、跨会话结构化召回与 scope 隔离、崩溃后重跑不重复、非法 JSON 两次失败后重试、伪造 event_id、assistant 内容冒充用户事实、模型调用期间来源被删、租约丢失不写、纠正后当前事实替换、列表/详情/任务状态 API、隐藏/恢复与隐藏事实不复活、网关 outbox 循环端到端、协议解析/一次修复/未知 kind/候选上限、TTL/衰减/访问强化/过期维护。
- 全量：**123 passed, 1 skipped**（真实厂商默认跳过；含记忆生命周期 5 项与记忆管理页 hide/restore 测试）。
- 迁移：独立脚本在临时 schema 模拟 0008 旧库，补列与索引成功，版本到 `0009_memory_extraction`。
- 真实模型未在自动测试中调用；阶段一验收只证明控制流程、来源校验与确定性行为，不证明提炼质量。

## 4. 剩余限制（后续阶段）

- 时间有效期、主体/属性规范、冲突/候选状态、变更历史表、旧版本回溯：阶段二。
- 用户记忆管理页面（列表/过滤/详情/来源跳转/隐藏/删除/候选确认）：阶段二。
- 每日费用上限、真实厂商单价显示、按日预算暂停：尚未实现；目前只把调用次数与输入/输出字符数写入任务 payload 并打日志。
- 批量策略是“每轮完成即入队，最多 8 条事件”；高频连续对话会在短时间内产生多次模型调用，后续可加合并窗口。
- `ContextBuilder` 每次仍调用 `index_events` 扫描全量可见事件；数据量增长需要增量水印或索引游标。
- 关键词/结构化检索质量未跑中文质量集；embedding、混合检索、摘要/自传整理未开始。
- 没有独立 worker 进程；提炼只在 Gateway 的 outbox 循环内运行。
- 旧 `MemoryWriter.validate_and_store` 仍保留给历史测试/手工调用，要求 content 逐字出现在引文；生产新链路不再使用它。

## 5. 运行方式

```powershell
cd app
.\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider
```

要启用提炼：在客户端“模型与密钥”配置对话模型即可（未单独配置 `memory_extract` 时会复用对话模型）；也可显式把 `memory_extract` 角色绑定到更便宜的模型。关闭自动提炼可在 `config/app.local.yaml` 设 `memory_extraction_enabled: false` 并重启 Gateway。

## 6. 回滚

`0009` 降级只删除两个游标列和事件扫描索引，不删除 memory_items/source_refs；已写记忆保留。若只想停止新的提炼，先关闭 `memory_extraction_enabled` 或停用模型角色，再决定是否迁移。

## 7. 追加：记忆管理页面（2026-10-25）

用户反馈记忆页只有搜索框、没有可操作按钮。本轮补上阶段二的第一块用户界面，不改变阶段一协议：

- **列表**：进入 `#/memory` 自动读取；支持搜索（记忆正文或来源原文）、类型筛选、状态筛选（有效/已隐藏/已替代/争议/全部）、刷新。每条显示类型、认知性质、状态、来源数、时间，以及查看详情/纠正/隐藏/恢复按钮。
- **详情**：右侧固定面板显示完整正文、类型/性质/状态/置信度/创建时间/ID，以及每条来源的 quote、事件 ID、发生时间、是否仍可访问。没有可用来源时不能纠正。
- **纠正**：来源改为下拉选择（只列服务端返回的可用来源，显示引文），不再要求用户手填事件 UUID；提交后旧记忆 superseded，新记忆带来源与 supersedes 关系。
- **隐藏/恢复**：新增 `PATCH /api/v1/memory/items/{memory_id}` 与页面按钮；隐藏停止召回但不删除原始对话，恢复重新参与召回。隐藏、已替代、争议的同内容事实不会因为再次提炼而复活（写入器返回 ignored）。
- **提炼任务**：页面展示最近任务的状态、尝试次数、事件数、写入/忽略/隔离计数和错误码；失败任务按既有 outbox 退避重试。
- **立即提炼**：记忆页“立即提炼一批”按钮调用 `POST /api/v1/memory/extractions`，不依赖下一轮聊天即可排队处理最多 8 条历史事件，适合首次查看或补齐旧档案；每次点击排队一批，费用按实际模型调用计。
- **检索边界**：页面明确标注当前是中文关键词 + 来源原文匹配，向量语义检索尚未接入。
- **验证**：后端完整 `123 passed, 1 skipped`；`node --check` 通过；新增 `scripts/verify-memory-client.mjs` 以无头 Chromium + mock 记忆 API 验证列表、详情、纠正弹层、隐藏、筛选、立即提炼、召回测试、维护与重要度，0 JS 异常；截图 `.devtools/memory-client-1440.png`、`memory-client-hidden.png`。真实业务库未写入合成记忆。

仍未完成：候选确认/驳回、隐藏条目独立分页、删除源对话入口、时间有效期/冲突语义、变更历史、独立 worker 与每日费用上限。

## 8. 借鉴 LivingMemory 的记忆生命周期（2026-10-25）

参考结构：[astrbot_plugin_livingmemory](https://github.com/lxfight-s-Astrbot-Plugins/astrbot_plugin_livingmemory)（AGPL-3.0）。该仓库提出的“记忆原子 + 类型化 TTL + 重要性衰减 + 访问强化 + 整合”对本项目有价值，但 **AGPL-3.0 代码不能直接复制进 AGI.Wind**；本轮只借鉴其公开设计思路，按本项目 PostgreSQL schema 独立实现，没有复制源码。

### 8.1 数据与规则

- 迁移 `0010_memory_lifecycle`：`memory_items` 增加 importance、access_count、last_accessed_at、last_reinforced_at、ttl_days、expires_at、decay_type、archived_at 与两个查询索引；旧记忆获得 90 天宽限，不会升级即全部过期。
- 类型化 TTL：经历 7 天、计划 2 天、事实 180 天、关系 90 天、偏好 60 天、承诺 90 天、身份 365 天、其他 30 天；importance 与强化次数放大 TTL。
- 衰减：计划用 step、关系用 linear、其余 exponential；检索与召回测试展示 freshness，按“相关度 + 重要性 + 新鲜度”形成 0-1 分数。
- 访问强化：记忆真正被注入上下文时（ContextBuilder 提交前）access_count+1、last_accessed_at 更新、expires_at 只延长不缩短；召回测试不会强化，避免调试影响排序。
- 维护：启动后每 6 小时自动把到期的 active/dormant 记忆置为 expired（保留正文和来源，不物理删除）；页面也提供手动“运行维护”。
- 用户可在详情把记忆标为重要（importance 0.9），会延长到期时间并参与排序；隐藏/替代/争议记忆不参与召回。

### 8.2 页面与 API

- 记忆页新增排序（最新/重要度/即将到期）、生命周期信息（重要度/访问次数/到期）、详情生命周期字段、“标为重要”“运行维护”。
- 新增“召回测试”卡片：输入一句话返回当前命中、路线（结构化/关键词）、最终分数、kind/importance/freshness/status；只读展示，不注入对话。
- API：`PATCH /api/v1/memory/items/{id}` 支持 status/importance；`POST /api/v1/memory/recall-test`；`POST /api/v1/memory/maintenance`。
- 自动维护随 Gateway 生命周期的后台任务运行。

### 8.3 验证与边界

- 全量 pytest：**123 passed, 1 skipped**；新增 5 项生命周期测试（TTL/衰减/强化/过期不召回/优先级与召回调试），并扩展 API 测试。
- 无头 Chromium 记忆页回归新增生命周期行、排序、召回测试、维护和“标为重要”，0 JS 异常。
- 真实提取链路已由用户手动触发一次（8 条事件，模型返回 0 条候选，任务 completed），证明任务可运行，但不代表记忆质量。
- 未做：向量/BM25/图谱混合检索、Embedding Provider 配置落地、语义聚类整合、Agent 主动记忆工具、TTL 的可配置化与真实质量评测；`dormant` 状态暂未产生。

## 9. 系统概览与记忆设置（2026-10-25）

参考 LivingMemory 的 Dashboard 信息架构（AGPL 项目，仅借鉴布局与指标思路，未复制页面代码）：

- **指标条**：有效、已隐藏、已过期、已替代/争议、待提炼事件、提炼任务数。
- **分布图**：重要性 10 档分布、记忆类型分布；下方保留列表/详情。
- **记忆设置**：自动提炼开关、每批事件数、每批候选上限、提炼超时、自动维护开关、维护间隔；保存后热生效并写入 `data/settings.json` 的 `memory` 段，重启后恢复。
- **向量状态**：设置区显示“向量：未接入”，提供“配置嵌入模型”跳转到模型与密钥的嵌入 Tab；适配器仍未接入，跳转后页面会如实说明。
- **API**：`GET /api/v1/memory/stats`、`GET/PATCH /api/v1/memory/settings`；统计只读，设置写入需认证与 CSRF。
- **验证**：完整 pytest `123 passed, 1 skipped`；无头 Chromium 验证指标、两类分布、设置读取/保存与向量入口，0 JS 异常。
- **边界**：统计基于当前筛选范围/角色；没有跨角色总览、图表交互、导出、提示词管理与图谱；嵌入仍不可用。
