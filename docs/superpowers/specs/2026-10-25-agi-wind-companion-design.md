# AGI.Wind 自托管 AI 伴侣：开发设计

版本：1.1-draft ｜ 日期：2026-10-25 ｜ 使用者：接手开发的 Codex

本文是工程设计，不是部署完成报告。用户确定的要求见 §1；其余技术选型是本方案的默认建议。执行顺序见同目录上级 plans 下的 2026-10-25-agi-wind-companion-plan.md。

## 1. 用户要求与范围

### 1.1 必须实现

| ID | 要求 | 完成判据 |
|---|---|---|
| R01 | 聊天使用厂商大语言模型 | 可配置服务地址、模型、凭据引用、采样与输出参数 |
| R02 | 看图使用厂商视觉模型 | 可单独配置；原图、提问、视觉结果及来源一起归档 |
| R03 | 语音本地部署 | 正常语音链路不依赖云端 ASR/TTS |
| R04 | 训练自己的声音 | 提供数据准备、微调、试听、注册、回滚流程；交付可加载的自训练权重 |
| R05 | 服务器 RTX 4060 8GB 优先用于语音 | 日常 GPU 只优先承担 TTS；训练与在线服务有明确切换机制 |
| R06 | 可选本地向量模型 | 首版 CPU 运行；可在实测有余量后迁到 GPU |
| R07 | 长期记忆最重要，能积累完整经历 | 原始记录持久化；支持跨会话、跨模型召回；摘要不替代原件 |
| R08 | 换模型保持角色与记忆连续 | companion_id、人格版本、关系、事件、承诺均不因模型切换重建 |
| R09 | 记忆独立、可迁移 | 不依赖 Letta/EverOS 私有目录即可导出、恢复、重建索引 |
| R10 | 可扩展并接已有客户端 | 稳定项目 API；客户端、推理供应商、记忆引擎均经适配器接入 |
| R11 | Live2D 展示角色 | 客户端加载模型；语音口型、表情、动作、打断同步 |
| R12 | 像持续存在的陪伴角色 | 跨会话保留共同经历和关系；具备一致称呼、表达风格、承诺和主动互动机制 |

“人格连续”指身份、记忆与约定持续存在，不承诺不同厂商模型的表达习惯完全相同。“完整经历”指完整保存被系统接收/生成并标明状态的记录，不承诺任何一次检索能无遗漏找回所有细节。

### 1.2 当前已知工程状态

- 工作目录：C:/Users/wind/Desktop/AGI.Wind，当前不是 Git 仓库。
- 已有 AI伴侣框架选型评估报告.md 与 research/；它们是研究资料，不是用户逐项确认的技术规范。
- tools/ayatsuri_2d 与 tools/ayatsuri2d-portable 是已有编辑工具，不能当作聊天客户端。
- output/live2d/WindChibi 与 output/live2d/viewer 是已有角色资产及动态预览；预览入口包含 PIXI/Live2D 加载和说话演示，尚未证明有聊天/语音后端接入。
- 用户的正式客户端名称、源码位置、认证方式、音频与 Live2D 接口未确定。
- 服务器 OS、CPU、内存、磁盘、CUDA/驱动、现有 GPU 占用和访问方式未核实；不能从旧任务推断部署目标。
- 没有在目标机器验证 GPT-SoVITS、Letta、EverOS 的完整组合。
- 保留上述已有目录，不移动、不覆盖、不将其整体引入新的业务源码树。

### 1.3 首版边界

- 首版服务一个账号、一个主角色；数据库从第一天保留 account_id、user_id、companion_id。
- 支持多个客户端连接同一身份；同一角色的回答任务串行执行，避免同时生成互相冲突的状态。
- 先完成文字、记忆、厂商视觉、本地语音、Live2D，再补后台整理与主动陪伴。
- 不在 8GB GPU 上部署本地聊天大模型或本地视觉大模型。
- 不从零训练语音基础模型；“训练自己的声音”落实为已有底模上的声音微调。
- 不把角色美术、复杂绑定制作作为后端开发的前置条件；复用现有资产验证，缺失动作使用中性退化。
- 不在首版引入 Kubernetes、Redis、Neo4j、独立 Qdrant 或完整 LiteLLM 网关。需要时通过接口扩展。

## 2. 方案选择

### 2.1 默认方案

项目网关 + 项目独立数据层 + Letta 运行时适配器 + 本地 GPT-SoVITS + 本地 ASR + 现有客户端适配/最小参考客户端。

EverOS 在后期作为可替换的记忆整理与检索增强器接入，不成为原始人生档案的唯一存储。其接入是开发任务，不能宣称 Letta + EverOS 原生即插即用。

### 2.2 三条路线及取舍

| 路线 | 优点 | 代价 | 本方案决定 |
|---|---|---|---|
| 直接使用 Letta 内置记忆 | 较少自建工作，角色持续存在 | 原始资产、事件状态、开放导出受运行时结构影响 | 不用它作为唯一档案；保留为运行时投影 |
| N.E.K.O/Open-LLM-VTuber 全栈改造 | 语音、角色展示已有基础 | 核心记忆与客户端耦合，需跟随上游接口 | 仅作为可选外壳/参考，不替换本项目 API |
| 项目独立档案 + 可替换适配器 | 可审计、跨框架迁移，适合长期积累 | 需要实现网关、数据层和适配测试 | 默认采用；Letta 优先承担角色运行 |

这里的独立数据层是一个小型业务服务，不是重新实现通用 Agent 框架。首版只包含归档、人格、记忆、模型调用、语音与角色同步。

### 2.3 已核对的上游事实

- Letta 当前活跃运行时位于 letta-ai/letta-code；自托管使用 local backend 的 App Server。不要照抄旧 Python V1 服务端教程。[Letta 仓库](https://github.com/letta-ai/letta)、[自托管](https://docs.letta.com/self-hosting)。
- App Server 提供 SDK/WebSocket 及兼容接口，支持文本/图片；身份、会话和模型配置不是同一概念。接入前锁版本并验证会话行为。[接口文档](https://docs.letta.com/self-hosting/app-server/quickstart)。
- MemFS 为 Markdown/Git 记忆；默认不含向量索引。本项目不能因接入 Letta 就跳过本地检索与迁移工作。[MemFS](https://docs.letta.com/concepts/memfs)。
- EverOS 的 Markdown 与索引分层适合借鉴，但其提炼内容不等于完整原始对话；本项目仍独立保存原始事件和媒体。[存储说明](https://github.com/EverMind-AI/EverOS/blob/main/docs/how-memory-works.md)。
- GPT-SoVITS 提供微调和本地 API；不同版本的请求字段、流式与显存需求有差异。固定代码提交、权重版本后做契约测试。[仓库](https://github.com/RVC-Boss/GPT-SoVITS)、[API 源码](https://github.com/RVC-Boss/GPT-SoVITS/blob/main/api_v2.py)。

上游资料只用于确认能力；下文出现的 /api/v1 等为本项目设计接口，不是上游现成端点。执行日重新核对并将版本写入 docs/ops/versions.md，禁止自动追随 latest。

## 3. 部署与模块

### 3.1 技术栈

| 部分 | 默认技术 | 说明 |
|---|---|---|
| 业务网关与记忆服务 | Python 3.12、FastAPI、Pydantic、SQLAlchemy 2、Alembic、httpx | 一个服务，按模块拆分 |
| 核心数据库 | PostgreSQL 17 + pgvector + pg_trgm | 不额外部署向量数据库 |
| 后台任务 | PostgreSQL outbox/jobs + 独立 Python worker | 崩溃可恢复，不依赖进程内临时队列 |
| Letta 适配 | TypeScript + App Server/Agent SDK；Node 版本遵循锁定版要求 | 隔离 SDK 变更与内部协议 |
| TTS | GPT-SoVITS v2Pro 系列作为首轮候选 | 由 8GB 实测选定具体版本；独立 Python/torch 环境 |
| ASR | SenseVoiceSmall，默认 CPU | 通过统一适配器返回最终转写；不假设其原生提供增量流式转写 |
| Embedding | BAAI/bge-small-zh-v1.5，CPU | 初始 384 维；读取模型实际 tokenizer/限制后切片 |
| 中文关键词检索 | Python 分词 + PostgreSQL simple 全文索引，精确字符串/pg_trgm 补充 | 不把默认英文分词当作中文检索 |
| 参考客户端 | 仅在已有客户端不能接入时增加 TypeScript/Vite 最小页面 | 不重做完整聊天产品；复用已有 Live2D 预览资产 |
| 部署 | Docker Compose（Linux 默认候选）；GPU 模型服务隔离 | 原生部署在 Windows/容器 GPU 不可用时作为适配路径 |

各组件独立锁依赖。不要把 GPT-SoVITS 的 torch/numpy 环境和业务服务强行合并。

### 3.2 逻辑拓扑

~~~text
已有客户端 / 最小参考客户端
  ├─ 麦克风、回声消除、播放队列、Live2D 渲染
  └─ HTTPS + SSE/WS
          ↓
AGI.Wind Gateway
  ├─ 身份/会话/幂等/生成控制
  ├─ 人格与记忆上下文构建
  ├─ RuntimeAdapter → Letta bridge → 厂商聊天模型
  ├─ VisionAdapter → 厂商视觉模型
  ├─ SpeechAdapter → ASR CPU / GPT-SoVITS GPU
  ├─ MemoryService → PostgreSQL + pgvector
  └─ 媒体访问与播放回执
          ↓
PostgreSQL 事务 outbox → Worker
  ├─ 提取事实/事件、生成摘要与日记、增量向量化
  ├─ 可选 EverOS adapter
  └─ 备份/导出/索引重建任务

项目 data/：媒体原件、声音包、训练记录、Live2D 资产、运行时投影
外部 API 凭据：只在服务器侧配置，不下发客户端
~~~

### 3.3 数据所有权

| 内容 | 权威来源 | 其他副本的地位 |
|---|---|---|
| 原始对话/请求/生成/播放状态 | 项目 PostgreSQL | 客户端缓存、Letta 历史均是可重建副本 |
| 图片/录音/声音权重/角色素材 | 项目管理的内容寻址文件与清单 | 外部 URL 或框架缓存不能当唯一原件 |
| 人格核心、称呼、关系、承诺 | 项目 persona_versions / relationships / commitments | Letta persona.md 是单向投影 |
| 提炼记忆与引用 | 项目 memory_items + source_refs | EverOS 结果先视作候选，再验证入库 |
| 向量、检索缓存、摘要提示缓存 | 可重建派生数据 | 失效可重算，不能代替原件 |
| runtime agent/conversation ID | runtime_bindings 映射 | 不允许作为全局角色 ID |

禁止双主写入：项目人格修改必须先入项目数据库，再投影到 Letta；不能让 Letta 自行修改核心人格后不回传。普通模型推断只可提出 memory/persona proposal，经来源校验后入库。用户显式修改可直接建立新版本。

## 4. 身份、配置与运行时边界

### 4.1 身份约定

- account_id：本系统账号。认证后由服务端确定，不能信任请求体中的同名字段。
- user_id：与角色建立关系的人类身份；首版每个账号一个。
- companion_id：角色终身 ID，采用 UUID；修改姓名、模型、声音、客户端不改变。
- conversation_id：一次聊天线；多个会话共享该角色的人格和授权关系记忆。
- device_id：客户端实例；只用于连接与播放归属，不决定记忆身份。
- request_id：一次用户发送的幂等 ID，由客户端生成 UUID。
- generation_id：一次回答任务；重连不新建，用户重试生成才新建。
- event_id：原始档案事件 ID。
- segment_id：一个可播放语音片段，属于特定 generation_id 与 epoch。
- persona_version：每次核心人格/称呼/边界变化递增，采用乐观锁。

首版按 companion_id 串行回答，第二个输入可立即归档，但回答排队。主动消息也走同一队列。并发读历史不受限。不得在数据库长事务内等待厂商流式响应。

### 4.2 模型角色

~~~yaml
providers:
  vendor_chat:
    protocol: openai_compatible
    base_url: https://vendor.invalid/v1
    credential_ref: vendor_chat_key
    capabilities: [text, streaming]
  vendor_vision:
    protocol: vendor_native
    adapter: IMPLEMENT_FROM_ACTUAL_PROVIDER_DOCS
    credential_ref: vendor_vision_key
    capabilities: [text, image]
models:
  dialogue: {provider: vendor_chat, model: CONFIGURE_ACTUAL_MODEL}
  vision: {provider: vendor_vision, model: CONFIGURE_ACTUAL_MODEL}
  memory_extract: {provider: vendor_chat, model: CONFIGURE_ACTUAL_MODEL}
  memory_reflect: {provider: vendor_chat, model: CONFIGURE_ACTUAL_MODEL}
  embedding:
    provider: local
    model: BAAI/bge-small-zh-v1.5
    device: cpu
runtime:
  preferred: letta
  backend: local
  fallback: direct_vendor
  fallback_policy: explicit_admin_selection
speech:
  asr: {provider: sensevoice, device: cpu}
  tts: {provider: gpt_sovits, device: cuda:0, voice_id: wind-default}
~~~

此示例为配置结构，不是可直接运行的最终配置。协议适配器必须按实际厂商文档实现。credential_ref 指向服务端秘密存储/环境变量，配置导出不能包含值。

模型参数包括 temperature、top_p、max_output_tokens、reasoning 配置、超时、支持的输入模态。只有供应商声明并经契约测试支持的参数才透传；不支持的字段返回明确错误或显示“不支持”，不静默伪装有效。

### 4.3 RuntimeAdapter 项目契约

~~~python
class RuntimeAdapter(Protocol):
    async def probe(self) -> RuntimeCapabilities: ...
    async def ensure_binding(self, scope: Scope) -> RuntimeBinding: ...
    async def apply_persona(self, binding: RuntimeBinding, snapshot: PersonaSnapshot) -> None: ...
    async def configure_model(self, binding: RuntimeBinding, model: ModelRef) -> AppliedModel: ...
    def stream(self, request: RuntimeRequest) -> AsyncIterator[RuntimeDelta]: ...
    async def cancel(self, generation_id: UUID) -> CancelResult: ...
    async def export_diagnostics(self, companion_id: UUID) -> RuntimeDiagnostics: ...
~~~

Scope、请求/返回字段见 §12。项目 API 不向客户端暴露 Letta 私有 ID。RuntimeRequest 必须携带 memory_revision；bridge 在开始生成前确认人格与记忆投影版本一致。

Letta bridge 必须验证：创建/恢复同一 agent；指定 conversation；更新模型；每轮注入项目上下文；受控记忆工具；流式文本；中断；local 状态目录；人格投影完成版本。真实 SDK 命令只记录在对应 adapter 中，不散落到业务层。

使用 SDK/WebSocket 为默认集成；兼容接口只作客户端兼容/探针。若使用 Responses 接口，按上游要求处理稳定会话标识；不得假设 previous_response_id 有效。[会话注意事项](https://docs.letta.com/self-hosting/app-server/quickstart)。

Letta 自带长期记忆只保留人格投影和受控上下文指针；独立存储仍由项目负责。验证“Letta 是否重复注入本项目已提供的全部历史”，避免双份历史挤占上下文。

如果当前锁定版无法提供必要注入/权限/取消能力：记录失败证据；可先用 direct_vendor 完成其余模块；生产启用该替代必须在配置和交付状态中明确。不得把 direct_vendor 的结果标为 Letta 集成通过。

## 5. 原始档案与数据库

### 5.1 表结构约定

全部时间保存 UTC timestamptz，展示与相对日期解析默认 Asia/Shanghai。UUID、内容哈希、状态、schema_version 从第一版固定；可选字段允许 null，缺失不靠字符串“unknown”补充。

| 表 | 关键字段与约束 |
|---|---|
| accounts / users | id、账号归属；禁止跨账号读写 |
| companions | id、account_id、name、created_at、active_persona_version、memory_revision、status |
| persona_versions | companion_id、version、core_json、style_json、human_notes、origin、source_refs、created_at；复合唯一 |
| relationships | user_id、companion_id、addressing、shared_boundaries、relationship_notes、version；复合唯一 |
| conversations | id、account_id、companion_id、user_id、title、created_at |
| requests | id=request_id、account_id、conversation_id、input_hash、state、generation_id；唯一请求 ID |
| generations | id、request_id、epoch、runtime、model_ref、persona_version、memory_revision、state、delivery_state、started_at、finished_at、error_code |
| events | id、account_id、companion_id、conversation_id、request_id、generation_id、role、kind、content_json、occurred_at、recorded_at、visibility、content_sha256 |
| assets | id、sha256、relative_path、mime、bytes、kind、created_at、metadata_json；内容哈希唯一 |
| asset_refs | asset_id、event_id、purpose；多对多，防止误删共享文件 |
| memory_items | id、account_id、companion_id、user_id 可空、kind、subject、content、epistemic_type、confidence、importance、status、valid_from/to、created_at、updated_at、revision |
| source_refs | id、memory_id、event_id、source_kind、json_pointer、start/end_offset、quote_sha256；引用可验证 |
| memory_relations | from_memory_id、to_memory_id、relation_type；supports/contradicts/supersedes/related_to |
| commitments | id、companion_id、user_id、content、due_at、status、source_event_id、fulfilled_event_id |
| retrieval_chunks | id、source_type、source_id、content、content_hash、start/end_offset、token_count、search_terms、revision |
| embedding_spaces | id、model_name、revision、weights_hash、tokenizer_hash、dimension、normalize、metric、status |
| memory_embeddings | chunk_id、space_id、content_hash、embedding vector、created_at；chunk_id+space_id 唯一 |
| outbox | id、event_type、aggregate_id、payload、available_at、attempts、state、lease_until、dedupe_key；dedupe_key 唯一 |
| jobs | id、kind、scope、state、progress、checkpoint、attempts、lease_until、last_error_code |
| playback_receipts | generation_id、segment_id、device_id、state、played_ms、received_at；幂等更新 |
| speech_segments | id=segment_id、generation_id、epoch、segment_index、source_event_id、display_text、spoken_text、voice_id/version、asset_id 可空、sample_rate/channels/duration_ms 可空、state、error_code；生成+epoch+序号唯一 |
| avatar_cues | id、generation_id、epoch、segment_id 可空、expression、motion、intensity、duration_ms、sync；永久记录实际发送的角色指令 |
| delivery_commands | command_id、generation_id、operation、result_epoch、result_json、created_at；command_id 唯一，保证打断重试幂等 |
| generation_stream_events | id、generation_id、sequence、type、payload、created_at；短期 SSE 重放，不作唯一永久档案 |
| conversation_live_events | id、conversation_id、sequence、type、payload、created_at；跨设备 delivery 控制与重连，保留至少 24 小时 |
| voice_profiles | id、name、engine、version、bundle_path、manifest_sha256、training_run_id、status |
| avatar_profiles | id、name、model_asset_id、expression_map、motion_map、lip_sync_config、version |
| runtime_bindings | companion_id、runtime、external_agent_id、conversation mapping、projected_persona_version、projected_memory_revision、runtime_version、status |
| external_memory_refs | engine、external_id、memory_id、source_batch_id、scope、sync_status |
| memory_engine_namespaces | companion_id、engine、namespace_id、memory_revision、status；只允许 active namespace 查询 |
| deletion_tombstones | deletion_id、ledger_sequence、scope、target_type、target_ids、deleted_at、policy、ledger_hash；不含删除内容 |
| audits | actor、operation、target_id、before/after_hash、timestamp、reason；不写秘密值 |

memory_items.kind 至少支持 fact、episode、reflection、user_profile、self_journal、relationship、preference。自传和用户事实分开，不能只建立“用户偏好列表”。

events.kind 至少支持 user_input、assistant_output、vision_observation、transcription、system_event、delivery_update、correction。角色 system 的运行事件也必须区分用户体验事件与内部遥测。

### 5.2 写入事务

1. 校验认证、会话归属、输入、附件状态。
2. 在同一事务中写 requests、用户原始 event、generation pending、outbox。
3. 提交成功后才返回 accepted。数据库写失败不先调用付费模型。
4. 相同 request_id + 相同 input_hash 返回原任务；相同 ID 不同输入返回 409。
5. 厂商文本增量每 250ms 或 1KiB 批量写入原始输出与重放事件，提交后再发送对应 SSE；结束/取消前强制落盘。不得先向用户显示已确认文字，再依赖稍后内存队列补档。
6. assistant_output 状态区分 complete、partial、cancelled、failed；生成过但未播放也保留。文本结束发送 text.completed，但整个 generation 先进入 draining。
7. 文字输出落盘后可建立记忆整理任务；generation.completed 只在该轮请求的音频与 avatar 事件全部产出或明确失败后发送。播放完成由 delivery_state/回执单独记录，不等待用户听完才完成生成。
8. 崩溃重启将租约过期的 queued/running/draining 任务按实际情况恢复或置 interrupted；不能把半条回答补标 complete，不能再次扣费重跑而不建立新 generation。

events 原文默认追加保存。用户纠错追加 correction 并更新派生记忆；用户删除走明确的删除流程，同时清理索引和缓存。不得用“原始不可变”拒绝用户删除其数据。

### 5.3 媒体原件

文件保存为 data/assets/<sha256 前两位>/<sha256>.<ext>，元数据和引用存数据库。上传先暂存、检查大小/解码、计算哈希、原子 rename，再提交资产就绪状态。请求只引用已就绪资产。

首版限额：图片单张 10MiB、每请求最多 4 张；语音单次 120 秒/20MiB；WebSocket 音频消息 64KiB 以内。配置可改，但必须有上限与错误响应。

原始音频按用户设置保留，默认开启“保留用于共同经历的语音原件”，训练素材单独目录；不把全部历史聊天自动加入训练集。音频保存用户原件并可生成 16kHz 单声道 PCM 派生文件，二者分别记录哈希。

## 6. 记忆系统

### 6.1 内容与真实性

epistemic_type 枚举：

- user_asserted：用户明确说过，表示“用户这样描述”，不保证客观世界事实。
- observed：由上传媒体或工具获得，必须记录观察来源、模型与时间。
- assistant_generated：AI 当时说过/承诺过；不能自动当作用户事实。
- inferred：模型归纳/推断，始终带来源及置信度。
- fictional：角色扮演、假设、故事设定；默认不进入现实事实检索。

每条自动提炼记忆必须有可回溯 source_refs；验证不到出处则进入 quarantine，不参与核心事实注入。模型生成的自我日记可存在，但必须标为 assistant_generated/inferred，不能伪造没有发生过的共同经历。

### 6.2 时间与修正

- 同时保存事件发生时间 occurred_at 和系统得知时间 recorded_at。
- “昨天”“上周”等相对日期以该条消息原始时间和用户时区解析，记录解析依据；不在未来重放时重新解释。
- “以前喜欢咖啡，现在不喝”产生新状态与 supersedes 关系；旧经历保留。
- 冲突未能确定时，两条都保留并标记 contested，回答说明不确定或向用户核实；不能简单选最近向量命中项当真相。
- 关系、称呼、承诺、纪念日是独立结构，不只靠语义相似度。
- 情绪状态是短期运行状态，设置过期时间；不能把一次生气永久写成人格核心。

### 6.3 切片与索引

- 不改动原文；retrieval_chunks 保存派生片段并引用源记录。
- 按消息/事件语义边界切片，起步目标中文 200—400 字；实际 tokenizer 限制为准，单片不超过锁定模型输入上限减 32 tokens。
- 长段重叠建议 40 字，保存 offset；不能把人物、否定词和发生时间单独切掉后丢失关联。
- Embedding 模型默认 BGE-small-zh-v1.5/CPU；锁权重 revision 和 tokenizer，实际探测维度。初始预期 384 维不是对任意替代模型的假定。[模型卡](https://huggingface.co/BAAI/bge-small-zh-v1.5)。
- 建立 embedding fingerprint，必须包含模型标识、revision/权重哈希、维度、归一化、距离度量和关键预处理版本。同维度不同模型也禁止混查。
- pgvector 使用无定长 vector 列，按 space_id 建立受控维度转换的部分索引；schema 迁移负责创建索引，不能拼接用户提供的 SQL 标识符。
- 首版向量候选数 30、关键词候选数 30、精确人物/日期候选数 20；按 chunk_id 合并，RRF k=60，选最多 12 个记忆片段；同一原事件最多 2 个片段。
- 每轮关系与人格不依赖检索，直接读取；其余记忆按需选择。
- 可选 reranker 默认关闭，避免首版挤占语音资源；接口预留。
- 中文全文索引使用分词后的 simple tsvector；精确称呼、日期及型号使用字符串匹配补充。不能假设纯向量或默认英文全文配置能覆盖中文。

混合检索可以在 PostgreSQL 内实现，无需单独向量库。[pgvector 文档](https://github.com/pgvector/pgvector#hybrid-search)。

### 6.4 Prompt 构建

优先级：系统边界 → 核心人格 → 当前关系/称呼 → 有效承诺 → 近期对话 → 相关记忆 → 当前用户输入/媒体观察。

默认输入目标预算 12,000 tokens；其中检索记忆最多 3,000、人格/关系最多 2,000、近期对话目标 4,000，其余给系统与当前输入。供应商窗口较小则按实际限制缩减；单次超长输入必须明确拒绝或要求分段，不能无声截掉用户内容。

当前输入、关键称呼与有效承诺优先保留；减少历史片段和低相关记忆。记录实际采用的 memory_ids、persona_version、token 估计及模型配置，默认不把完整 prompt 写普通日志。

记忆内容作为带来源的数据块注入；其中的“忽略规则”“调用某地址”等文字不作为系统指令执行。

### 6.5 后台提炼与成长

默认在每 8 个完成的用户轮次或空闲 120 秒后处理增量；两条件合并去重。仅处理上次 checkpoint 之后的内容，不重复总结全人生。

处理顺序：事实/事件候选 → schema 验证 → 引用验证 → 去重/冲突检查 → 入库 → 摘要/自传日记 → 向量任务。抽取失败不影响已归档的聊天；job 保留错误和重试位置。

核心人格中的身份、主要价值、关系边界只能由用户显式设置或接受建议后改变；普通偏好与有来源的经历可自动积累。模型可提出 persona proposal，不能直接覆盖受保护核心。

后台厂商调用设置日 token/费用上限；接近上限延后整理，保留原始队列，不丢记录。默认单 worker、最多一次模型调用并发；不做无意义周期轮询或每分钟重读全部历史。

### 6.6 EverOS 接入

默认在核心归档、原生检索和迁移验收后实现。只通过 MemoryEngineAdapter 接口接入 ingest、flush、search、health、snapshot。

- 以项目事件 ID 和 batch ID 建立本地投递台账，不假设上游幂等。
- EverOS 结果先做 scope、来源、时间与重复验证，再纳入项目候选记忆。
- 无法拿到精确出处时，至少回指完整输入批次并标注批次级来源；不能伪称精确引文。
- 先运行 shadow 模式：只记录命中效果，不参与回复；通过验证后才启用增强检索。
- 停用或升级 EverOS 不删除项目记忆，项目仍能文字聊天和检索。
- 必须实现逻辑 invalidate 与新 namespace 重建；不要求上游提供原生逐条删除，但旧 namespace 失效后绝不参与回复。
- 严格限制文件访问目录，媒体由项目提供已归档的文字描述或内部授权路径，不让外部请求任意指定 file://。

上游 API 使用 /api/v2，具体 schema 以锁定版 OpenAPI/源码为准；本文不把自己的 DTO 冒充上游字段。[EverOS API](https://github.com/EverMind-AI/EverOS/blob/main/docs/api.md)。

### 6.7 纠正、删除与外部副本失效

memory_revision 是项目的权威记忆修订号。用户纠正、永久删除、关键关系变化时，先提交项目变更并递增修订号，再允许新回答构建上下文。

- 每次检索在最终注入前，重新校验来源未删除、记忆 revision 有效、scope 正确。正在运行的提炼/向量任务在写入前检查修订号，过期结果不能使已删内容复活。
- 纠正/删除使活动回答的记忆快照过期时，中断该轮并广播新 epoch，避免它继续输出旧事实；已生成部分保留实际状态。用户下一轮使用新 revision，不在旧 generation 中混入两套人格/事实。
- 修订变化后旧 Letta conversation/context 标为 stale；重新建立上下文，必要时建立新的私有 agent/工作目录，以项目有效档案与人格重建。项目 companion_id 不变。
- 普通纠正保留旧经历，但重建上下文必须带当前状态与修正；永久删除内容不得重放到新上下文。
- EverOS 旧 namespace 立即禁查；后台以有效档案构建新 namespace，完成前使用项目原生检索。不得只追加一条“忘掉旧事”后继续查询旧副本。
- MemoryEngineAdapter 增加 invalidate(scope, memory_revision, tombstones) -> InvalidationReceipt 与 rebuild(scope, memory_revision)。invalidate 至少保证路由隔离；purge 能力单独报告。
- 永久删除同时登记 Letta/EverOS 本地副本清理任务，验证删除或销毁其专属存储。无法细粒度删除时使用专属存储隔离并停止旧实例；不能清理不明共享目录，也不能将“停止查询”冒充“物理清理完成”。
- 恢复后默认创建新 runtime binding 与 namespace，导入的旧私有缓存仅用于诊断，不自动恢复为 active。
- 厂商已经接收的数据按该厂商的数据保留规则处理；本项目不能承诺删除第三方服务器副本。此限制不影响本地后续上下文排除。

## 7. 本地语音与自训练声音

### 7.1 资源分工

- 在线：GPT-SoVITS 一个进程/一个已加载声音包，占用 GPU；ASR、Embedding 默认 CPU。
- TTS 并发上限 1，排队最大 8 个片段；每段合成前检查是否仍属于当前 epoch。
- 每个声音包预热后记录静态占用、首次合成峰值、连续合成峰值，不能只看权重文件大小。
- 在线目标 GPU 峰值不超过总显存的 85%；这是工程余量目标，不是已测结果。
- GPU OOM 后暂停接收新 TTS、释放失败请求资源并重启该专用服务一次；不无限自动重试，不影响文字和记忆服务。
- 训练模式先停止接受新语音生成、等待/取消队列、停止在线 TTS 进程、确认显存释放，再加载训练任务。Embedding/ASR 若改到 GPU 也必须释放。
- 训练完成后退出训练进程、校验权重、恢复原在线版本；新声音只有通过试听和加载测试后才激活。
- 不停止目标服务器上的其他现有服务来“腾显存”；先报告占用，部署方案使用专属进程/目录/端口。

### 7.2 语音模型选择与版本固定

GPT-SoVITS v2Pro 系列为首轮评估候选；准确版本由目标 4060 8GB 的推理与小样本微调探针决定。更新版本可以比较，但不因为数字较新就替换已通过的声音。

部署探针只做：服务加载、3 段中文/中英混合合成、一次小样本训练检查、记录 CUDA 峰值与耗时。明显不适配时调整 batch/精度或版本，禁止反复完整训练撞 OOM。

模型服务依赖独立环境，业务 gateway 只通过网络 API 调用。不要把 HTTP 路由、路径和流式参数硬编码在客户端。

### 7.3 声音包

~~~json
{
  "schema_version": 1,
  "voice_id": "wind-default",
  "version": "voice-0001",
  "engine": "gpt_sovits",
  "engine_revision": "PIN_ACTUAL_COMMIT",
  "model_family": "PIN_AFTER_GPU_PROBE",
  "weights": [
    {"role": "gpt", "path": "weights/voice.ckpt", "sha256": "ACTUAL_SHA256"},
    {"role": "sovits", "path": "weights/voice.pth", "sha256": "ACTUAL_SHA256"}
  ],
  "references": [
    {"id": "neutral", "path": "references/neutral.wav", "text": "实际准确转写", "language": "zh"}
  ],
  "training_run_id": "training-0001",
  "default_parameters": {"speed_factor": 1.0},
  "evaluation": {"status": "pending", "report_path": "evaluation/report.json"}
}
~~~

路径相对声音包根目录；导入拒绝路径穿越和缺失文件；只加载管理员导入并校验的权重。声音版本、参考音频、底模依赖、训练数据清单与超参数全部记录，才能迁移和重训。

对已锁定 API，初始采用 batch_size=1、句级 WAV 输出；参数根据真实版本验证。GPT/SoVITS 权重切换是全局状态变化，必须持有语音服务独占锁，切换期间不处理其他声音请求。[实际 API](https://github.com/RVC-Boss/GPT-SoVITS/blob/main/api_v2.py)。

### 7.4 训练流程

1. 收集用户指定的原始录音和逐字文本，存入独立 training_runs/<id>/raw。不要擅自使用全部私人聊天录音。
2. 产生清单：文件哈希、时长、采样率、语言、文本、来源、训练/验证划分。
3. 检测静音、削波、重复、背景音乐、多说话人；自动问题进入待修正清单，保留原件。
4. 按底模要求切片/重采样；保持标点和发音文本准确，训练/验证不使用同一录音相邻切片造成泄漏。
5. 使用能放入 8GB 的小 batch/精度和底模推荐微调方式，保存实际配置；不承诺所有版本都能在该卡全量微调。
6. 固定 20 条试听文本，包括日常对话、数字、日期、长句、中英混合、轻声/问句；不在训练集中使用这些评估句。
7. 记录相似度主观评分、可懂度、错漏读、杂音、首段延迟、RTF、显存峰值。声音好不好必须由用户试听，不能用“训练完成”替代。
8. 打包新 voice version；保留旧版本，一次操作可回滚。不因声音更新修改 companion_id 或人格。

正式训练依赖用户提供素材；无素材时可完成工具链与样例验证，但 R04 必须标“待素材/待用户试听”，不能标完成。

### 7.5 ASR 与输入音频

默认 SenseVoiceSmall/CPU；使用本地模型缓存，不把音频发到厂商。若 CPU 延迟不达标，再单独比较轻量实现，不抢占 TTS 显存。[官方 CPU 部署](https://github.com/QwenAudio/SenseVoice)。

参考客户端：浏览器采集 → echoCancellation/noiseSuppression → VAD 分段 → 服务端标准化 16kHz mono PCM → 最终转写。用户可在发送前修正转写；原识别结果与修正记录分别保存。

首版支持“按住说话/点击发送”作为可靠路径，VAD 自动发送是增强功能。带扬声器的全双工回声消除需实际设备测试，不能因启用浏览器选项就宣称免耳机打断已验收。

### 7.6 输出音频、流式与打断

首版使用“文本流式 + 句级音频片段播放”，不冒称完整原生端到端流式 TTS。

- 在句末标点切分；首段达到 12 个字符后可在安全短句边界提前发送；默认单段上限 100 个字符，避免切断数字、小数、缩写和代码。
- token 文本完整保存；TTS 仅朗读可说内容，排除内部标签、工具数据和代码块，朗读文本与显示文本分别记录。
- 每段保存 WAV 及真实 sample_rate、channels、duration_ms、voice_version；不要假定所有 GPT-SoVITS 版本输出同一采样率。
- 每段完整元数据写入 speech_segments，表情写 avatar_cues；它们是永久档案，不能只存在会过期的 SSE 重放表。playback_receipts.segment_id 外键引用 speech_segments。
- SSE 发送 audio.segment 元数据与受保护下载路径；客户端下载解码后按 segment_index 播放。
- audio/字幕/表情使用相同 generation_id、epoch、segment_id；客户端按真正开始播放时间触发表情。
- 用户打断时客户端先停止播放并清空待播队列，然后发 interrupt；服务器提升 epoch、取消上游、取消待合成片段。
- 文本结束后，等所有已请求的语音片段 ready/failed/cancelled、所有 avatar.cue 写入并发出，才发送 generation.completed。可选音频失败用 warning 标明，文字结果不丢失。
- generation.completed 后仍可能有音频正在设备播放。此时 interrupt 不改写已完成原文，仍取消 delivery、提升 epoch，并通过 conversation live 流广播 delivery.epoch_changed，使其他已连接设备停止旧播放。
- 无法停止正在执行的 GPU 合成时允许其计算结束，但结果必须丢弃，不再播放；不为了中断一段话直接杀共享模型进程。
- 客户端回传 started/finished/interrupted 与实际播放毫秒。状态区分“模型生成过”“客户端播放过”；不能因服务器返回音频就说用户已经听到。

## 8. 厂商视觉

1. 用户上传图片 → 项目保存原件并校验。
2. VisionAdapter 使用实际支持的方式发送图片，不把内网文件路径当成厂商可访问 URL。
3. 回答为结构化 observation：description、visible_text、entities、uncertainties、model_ref、source_asset_ids。
4. 看不清的细节不生成确定事实；视觉观察只引用原图和当时问题。
5. 当前聊天模型已有视觉能力时可以合并调用；仍保留单独可配置的视觉角色，避免锁死。
6. 不默认每轮重发全部历史图片；优先使用原有文字观察，需要时重新分析指定原图。
7. 不持续上传摄像头/屏幕。此功能后续由用户启用并选择采样频率，首版手动上传即可。

## 9. Live2D 与客户端

### 9.1 首个检查

确认正式客户端是否支持自定义服务地址、认证、自定义会话 ID、SSE/WS、音频片段播放、播放回执、Live2D 参数。只支持普通 /v1/chat/completions 的客户端可以先文字聊天，但不能因此验收 R11。

若不能获取正式客户端源码，交付 packages/client-sdk 的 TS 类型/事件解析器及最小参考页面；不等待未知客户端而停掉后端开发。

### 9.2 资产与渲染

优先复用 output/live2d/WindChibi；先测试真实 .model3.json、.moc3、纹理、参数及授权来源。已有 Ayatsuri2D 用于编辑资产，不充当聊天运行时。

Live2D 默认在客户端 GPU 渲染，不占服务器用于 TTS 的 CUDA 显存。开发服务与正式服务均使用 HTTPS 或 localhost 的安全上下文以支持麦克风。

口型用本地播放音频 RMS 驱动 ParamMouthOpenY，初始 attack 40ms、release 100ms、clamp 0..1；参数可配置，不假设每个模型都采用同名参数。模型缺嘴参数则关闭口型并报告能力。[Live2D 口型说明](https://docs.live2d.com/en/cubism-sdk-manual/lipsync/)。

表情枚举：neutral、happy、sad、surprised、thinking、concerned。每个 avatar profile 映射到实际 expression ID；未知或缺失映射回 neutral。动作仅使用 allowlist：idle、nod、shake_head、wave；缺失动作不报整轮错误。

不要让厂商模型直接输出任意 Live2D 文件路径/JS。模型可提出规范化情绪标签，由项目解析验证后生成 avatar.cue；TTS 不朗读这些标签。

### 9.3 项目 AvatarCue

~~~json
{
  "generation_id": "UUID",
  "epoch": 1,
  "segment_id": "UUID",
  "expression": "happy",
  "intensity": 0.45,
  "motion": "nod",
  "duration_ms": 1500,
  "sync": "segment_playback_start"
}
~~~

客户端只接受当前 generation/epoch；播放结束嘴巴回 0，表情按配置回 neutral。用户打断、断线、切角色、窗口关闭都要停止口型和待播动画。

Open-LLM-VTuber 可作为另一个 client adapter 的实现参考，但不能因为已有外壳就把核心数据迁入它。若复用源代码/SDK/模型，记录各自许可证与来源。[参考](https://docs.llmvtuber.com/docs/user-guide/live2d/)。

## 10. 陪伴行为

- 人格核心、称呼、沟通习惯和关系配置每轮生效，不依赖模型自己“想起来”。
- 记住共同经历、未完成承诺和用户明确表达的偏好；回答优先引用相关经历，避免每轮机械复述记忆。
- 自传日记记录 AI 当天参与的对话、承诺与反思，并区别真实交互和虚构内容。
- 主动聊天默认关闭，避免部署时自行打扰；交付可配置开关、安静时段、频率上限。
- 用户开启后：默认每日最多 2 次，安静时段 23:00—09:00（Asia/Shanghai），冷却至少 4 小时；用户正在输入/已有待答/离线则不打断，过期候选直接作废。
- 触发来源：用户显式设置的纪念日/提醒、已授权跟进的承诺、长时间未互动后的问候；不编造“刚刚看见你做了什么”。
- 主动消息走普通归档、语音和状态机，不绕过幂等与成本上限。
- 首版不接外部消息平台主动发信；仅在本项目客户端显示。用户需要 QQ/Telegram 等时另写适配器。

## 11. API 与事件协议

### 11.1 鉴权与错误

统一 /api/v1；认证主体从 Bearer token/受保护会话取得。首版单账号，首次启动生成访问凭据并允许轮换，不把固定密钥写前端源码。浏览器 WS 使用同站 HttpOnly 会话或短期 WS ticket；秘密不得放长期 URL 查询串。

错误结构：

~~~json
{"error":{"code":"REQUEST_CONFLICT","message":"同一请求 ID 对应不同内容","retryable":false,"request_id":"UUID"}}
~~~

状态码：400 输入错误；401 未认证；403 无权限；404 不存在；409 冲突；413 超限；422 schema/不支持参数；429 忙/预算；503 依赖不可用。普通错误不返回原始上游响应、秘密或文件绝对路径。

### 11.2 HTTP

| 方法与路径 | 作用 |
|---|---|
| GET /health/live | 进程存活；不调用厂商 |
| GET /health/ready | DB、资产目录与必要 adapter 状态；分别报告可用/退化 |
| GET /api/v1/capabilities | 协议版本、模态、运行时、ASR/TTS/Live2D 能力，不泄露凭据 |
| POST /api/v1/assets | multipart 上传；返回 asset_id、sha256、mime、bytes |
| GET /api/v1/assets/{id} | 校验账号归属后读取；支持音频 Range 请求 |
| POST /api/v1/conversations | 创建会话，传 companion_id；返回 conversation_id |
| POST /api/v1/chat/requests | 接受输入、附件、request_id；返回 generation_id 与 events_url |
| GET /api/v1/generations/{id}/events | SSE，可用 Last-Event-ID 恢复 |
| POST /api/v1/generations/{id}/interrupt | body 带 command_id，幂等打断；返回 generation_state、delivery_state、epoch |
| GET /api/v1/conversations/{id}/live | 会话控制 SSE，广播 accepted 与 delivery.epoch_changed，支持重连 |
| POST /api/v1/playback/receipts | 幂等播放回执 |
| GET /api/v1/conversations/{id}/events | 带 cursor 分页原始历史 |
| GET /api/v1/companions/{id}/persona | 当前人格与版本 |
| PATCH /api/v1/companions/{id}/persona | expected_version + 变更；冲突 409 |
| POST /api/v1/companions/{id}/model | 修改未来请求的模型绑定；当前轮保持原模型 |
| POST /api/v1/memory/search | 带授权 scope、query、time_range，返回命中与来源 |
| POST /api/v1/memory/corrections | 指定记忆/事件及纠正文字；追加 correction |
| POST /api/v1/archive/exports | 创建完整/指定角色导出任务 |
| POST /api/v1/archive/imports | 校验后导入新命名空间，默认不覆盖现有身份 |
| POST /api/v1/index/rebuilds | 构建指定 embedding_space 的新索引任务 |
| GET /api/v1/jobs/{id} | 任务状态和断点 |
| GET /api/v1/voices | 可用声音与版本 |
| POST /api/v1/voices/imports | 管理员导入声音包 |
| POST /api/v1/voices/{id}/activate | 排空旧队列后激活已验证版本 |
| PATCH /api/v1/companions/{id}/proactive | 主动聊天开关、安静时段、上限 |
| GET /v1/models | 兼容客户端可见的项目模型别名 |
| POST /v1/chat/completions | 兼容文字/图片入口，映射到相同项目状态机 |
| POST /v1/audio/transcriptions | 兼容 ASR 入口 |
| POST /v1/audio/speech | 兼容 TTS 入口；输入 voice_id，不接受任意权重路径 |

兼容端点不等于原生 API 的全部能力。没有稳定 chat key 的外部客户端，每次请求只能作为独立会话；必须显示限制，禁止靠完整历史文本相似度猜同一会话。支持 X-AGI-Conversation-ID 和 X-Request-ID；缺幂等 ID 时由服务器分配，但不承诺客户端自动重试去重。

### 11.3 请求与事件

~~~json
{
  "request_id":"UUID",
  "conversation_id":"UUID",
  "input":{"text":"还记得上次一起看的那张照片吗？","asset_ids":[]},
  "output":{"text":true,"audio":true,"avatar":true},
  "device_id":"browser-main"
}
~~~

accepted 响应包含 request_id、generation_id、epoch、events_url；客户端收到后订阅。SSE 的 id 行使用该流的整数 sequence，Last-Event-ID 按同一流解析；payload.event_id 仍是 UUID。序号单调递增，重放事件保留至少 24 小时，存于 generation_stream_events 表；这张表与 events 的原始档案职责不同。conversation live 流有自己的 sequence，不与 generation 流混用。

~~~json
{
  "protocol_version":"1.0",
  "event_id":"UUID",
  "sequence":12,
  "type":"audio.segment",
  "generation_id":"UUID",
  "epoch":1,
  "timestamp":"2026-10-25T10:00:00Z",
  "payload":{
    "segment_id":"UUID",
    "segment_index":0,
    "text":"记得，那是你给我看过的海边照片。",
    "asset_id":"UUID",
    "audio_url":"/api/v1/assets/UUID",
    "sample_rate":32000,
    "channels":1,
    "duration_ms":2600,
    "voice_id":"wind-default",
    "voice_version":"voice-0001"
  }
}
~~~

sample_rate 示例仅展示字段；必须返回真实值。事件类型固定：

accepted、memory.context、text.delta、text.segment、text.completed、audio.segment、avatar.cue、generation.completed、generation.cancelled、generation.failed、warning。

memory.context 只给授权用户显示命中摘要/来源，不输出内部系统提示。文字 completed 不等于 generation completed。generation terminal 前必须排空要发送的音频/角色输出，terminal 后迟到的输出丢弃。

会话 live 流独立于 generation 流，发送 generation.accepted、delivery.epoch_changed、persona.changed、memory.invalidated 等控制事件。客户端保持订阅该流，即使 generation 已结束，也能接到其他设备发出的停止播放命令。控制事件不受“terminal 后丢弃输出”规则限制。

同一 command_id 重试返回原结果；已经取消且无新活动的 generation 不再次提升 epoch。恢复连接前先读取当前 epoch/delivery_state，再决定是否恢复未播片段，默认不自动重播已确认完成的片段。

### 11.4 输入语音 WebSocket

/api/v1/voice/ws：连接后发送 authenticated start，字段包含 conversation_id、request_id、input_format=pcm_s16le、sample_rate=16000、channels=1；然后二进制 PCM，每帧 20—100ms；客户端发送 end 后得到 transcript.final，再由客户端确认/自动发送为普通 chat request。

首版不发送 transcript.partial，除非 ASR adapter 真正支持且契约测试通过。超时 30 秒无音频发送 warning，120 秒达到上限自动结束并转写；断线不自动提交半句为用户最终输入。

## 12. 代码边界与共享 DTO

~~~text
agi_wind/
  core/
    types.py          Scope、ModelRef、ContentPart、SourceRef、RuntimeRequest
    events.py         StreamEvent、AudioSegment、AvatarCue
    errors.py         统一错误码
  archive/            原始事件、媒体、导出/恢复
  identity/           人格、关系、runtime binding
  memory/             提取、冲突、检索、embedding、上下文
  runtime/            项目 RuntimeAdapter 与 Letta/direct 实现
  providers/          厂商文字/视觉协议适配
  speech/             TTS/ASR 适配、声音包、队列和取消
  delivery/           generation 状态机、SSE、播放回执
  avatar/             情绪标签校验与 cue 生成
  proactive/          主动聊天计划与限额
  jobs/               outbox、worker、重试与租约
  api/                HTTP、WS、鉴权
~~~

关键 DTO：

| 类型 | 必需字段 |
|---|---|
| Scope | account_id、user_id、companion_id、conversation_id |
| ModelRef | provider_id、model_id、parameters、config_version |
| PersonaSnapshot | companion_id、version、core、style、relationship、commitments |
| SourceRef | event_id、json_pointer、start_offset/end_offset 可空、quote_hash 可空 |
| RuntimeRequest | scope、generation_id、epoch、memory_revision、model_ref、persona_snapshot、recent_events、memories、current_input、vision_observations |
| RuntimeDelta | generation_id、epoch、kind(text/tool/end/error)、payload |
| MemoryHit | memory_id/chunk_id、content、score、epistemic_type、source_refs、time_range |
| AudioSegment | generation_id、epoch、segment_id、segment_index、text、voice_id/version、asset_id、sample_rate、channels、duration_ms |
| RuntimeCapabilities | text、image、streaming、cancel、model_switch、context_injection、controlled_tools、local_state |

Pydantic JSON Schema 与 TS 类型从同一规范生成；不在 Python 与 TS 各手写一套会悄悄漂移的契约。

## 13. 导出、迁移、备份和删除

### 13.1 开放档案包

~~~text
archive/
  manifest.json
  identities.jsonl
  personas.jsonl
  relationships.jsonl
  conversations.jsonl
  requests.jsonl
  generations.jsonl
  events.jsonl
  memories.jsonl
  source_refs.jsonl
  memory_relations.jsonl
  commitments.jsonl
  assets.jsonl
  asset_refs.jsonl
  playback_receipts.jsonl
  speech_segments.jsonl
  avatar_cues.jsonl
  deletion_tombstones.jsonl
  voice_profiles.jsonl
  avatar_profiles.jsonl
  provenance/runtime_versions.json
  provenance/model_configs.redacted.json
  provenance/deletion-ledger-snapshot.ndjson
  voices/...
  avatars/...
  assets/<sha256-prefix>/...
  optional/runtime_snapshot/...
  optional/everos_snapshot/...
~~~

manifest 含 archive_format_version=1、应用 schema_version、export_id、scope、UTC 时间、consistent_cut、各文件 bytes/record_count/sha256、依赖版本、是否包含索引/声音素材/待完成任务、redaction 标记。哈希覆盖实际文件字节，不重新序列化后计算。

人格、原文、事实、关系、来源、媒体原件、声音与 avatar profile 都要导出。speech_segments 与 avatar_cues 保留朗读文本、片段顺序、声音版本、媒体和回执关联。短期 SSE 表无需完整迁移，但不得承担任何唯一永久信息。

API keys、会话 token、数据库密码不导出。声音训练原始数据可选导出，但清单必须明确缺失项及其对重训的影响。

### 13.2 一致性和恢复

- 导出先建立角色范围维护屏障：阻止新任务入队、等待/取消当前 generation、将增量落盘、等待记忆 worker 的写租约结束，再确定快照。
- 使用数据库一致性读快照，资产采用内容寻址不可变对象；复制清单所引用的就绪文件。导出期间允许读，不允许角色状态写入。
- 导出结束释放屏障；超时失败必须释放，不能让角色永久锁住。
- 运行中的任务导出为 interrupted/pending 元数据，恢复后不自动重放付费模型调用或播放旧音频。
- 导入先做 schema、哈希、引用完整性与空间要求检查；解包拒绝越界路径/符号链接跳出。
- 默认导入空数据库/新 namespace；不能以 upsert 无声合并同名角色。冲突需明确选择 clone、replace 或 cancel；replace 先有恢复点。
- 原 ID 保留于迁移模式；克隆模式生成新 ID 并重映射全部外键，保留 origin_id。
- 先恢复原文与人格，再恢复记忆/媒体/声音，最后重建关键词与向量索引，最后创建新的 runtime binding。
- 不要求恢复 Letta 私有 agent ID 才能恢复该角色；可从项目人格与历史生成新绑定。

### 13.3 Embedding 迁移

建立新 embedding_space，后台分批处理内容，记录 chunk_id + content_hash + checkpoint。构建期间旧索引继续服务，新内容分别入两套空间；新空间追平后做检索回归并原子切 active_space。

同维度换模型仍是新空间。失败不切 active_space；切换后保留旧索引至用户指定保留期。删除所有向量后，原始记录、人格和非向量检索仍可用。

### 13.4 备份

独立于开放导出另做数据库备份 + 资产/声音/角色文件快照。目标位置由用户配置，默认不上传第三方。备份可加密，密钥不和备份明文放同目录。

常规备份必须复用一致性切点机制：取得写入维护屏障，排空/中止生成，冻结 worker 提交，建立数据库快照并确定就绪文件清单。完成数据库 dump 后可释放写屏障，但为清单文件保留 asset pin，直到文件复制与校验完成；GC/永久删除须等待 pin 释放或使备份任务明确失败，不能悄悄删掉正在备份的文件。

备份 manifest 同时记录 DB snapshot、文件清单与删除账本 checkpoint；任一部分缺失或校验失败即为无效备份，不能进入成功列表。备份恢复与开放包导入共享最新删除账本检查。

首版每日一次、保留最近 7 份日备份与 4 份周备份；这只轮换备份副本，不删除原始经历。容量不足先告警/暂停新媒体上传，不静默清空旧人生。真正长期使用前至少完成一次异地/其他磁盘恢复演练。

### 13.5 删除

区分“从回复中隐藏”“纠正”“永久删除”。隐藏只影响检索；纠正保留历史；永久删除需清理源记录、派生记忆、向量、缓存和不再被引用的资产，记录无内容的删除审计。

删除墓碑有独立于应用数据库恢复的权威账本：data/governance/deletions.ndjson，逐条追加、fsync、序号与前项 hash 链；只存删除 ID、原始 scope/实体 ID、时间、策略，不存被删内容。它不在“恢复旧数据库”时被旧版本覆盖。

删除意图先写账本，再同步 deletion_tombstones 和清理任务；崩溃后 readiness 前重放账本。账本另存到用户配置的备份控制目录，不能只混在轮换中的旧快照里；跨磁盘灾难恢复保证要等独立副本同步成功才标通过。

恢复旧备份时必须读取当前部署或独立控制目录的最新账本，按原 ID/克隆映射应用后再开放查询。导出包包含截至导出时的账本快照；它不能知道导出之后发生的删除。搬到新机器恢复旧包时，需同时提供最新账本；无法获得最新账本时，恢复报告明确“不保证后续删除已应用”，默认不将该恢复提升为正式 active 角色。

备份中的删除传播策略必须说明：旧备份按保留期过期，恢复前强制重放墓碑。克隆先在源 origin_id 上应用墓碑，再分配新 ID，不能通过克隆绕过删除。用户明确要求彻底清除时再处理备份副本，不擅自清理。

## 14. 故障、运维与资源

### 14.1 降级矩阵

| 故障 | 应有行为 |
|---|---|
| 数据库写失败 | 不确认 accepted，不调用厂商，提示重试 |
| Letta 不可用 | 报 runtime unavailable；只有已明确配置 direct_vendor 时才切换 |
| 聊天 API 失败 | 保留用户输入和错误状态；不自动重复计费生成 |
| 视觉 API 不可用 | 保留原图；说明无法分析，不编造图片内容 |
| Embedding 不可用 | 精确/关键词/时间检索继续；记录退化 |
| EverOS 不可用 | 项目原生记忆服务继续；后台投递稍后重试 |
| TTS/OOM | 文字继续、记忆继续；明确音频失败，不替换成云音色 |
| ASR 失败 | 用户可改用文字；录音保留为失败转写待重试 |
| Live2D 加载失败 | 聊天/声音继续；静态占位，不让整轮报错 |
| 网络重连 | 使用相同 generation_id 与 sequence 订阅；不再次发送原请求 |
| 磁盘不足 | 不接受不能持久化的新媒体；文本按剩余阈值有序拒绝，不能先回答后丢档 |

### 14.2 默认端口建议

443：唯一对客户端入口；8000：gateway；4500：Letta；9880：GPT-SoVITS；8001：ASR adapter；5432：PostgreSQL。执行前检查冲突，可以整体改配置，禁止杀掉占用端口的未知服务。

内部服务默认仅 localhost/Compose 内网，管理员训练界面不直接公开。TLS、认证、资产 scope 校验、文件路径白名单属于首版必要实现。

Letta 运行在专用工作目录，只开放本项目需要的受控工具；不为了陪伴功能给它宿主机任意 shell、浏览器、凭据目录权限。

### 14.3 指标与日志

记录 request_id/generation_id、adapter 版本、状态、耗时、token/费用估计、job backlog、GPU allocated/reserved/峰值、音频 RTF、播放延迟、索引版本。

普通日志不含 API key、音色训练原始文本、完整对话、原图 base64 或系统 prompt。调试日志按用户选择临时开启并有过期机制。

健康检查不调用付费 API，不反复加载模型，不周期占用 GPU。支持 readiness 退化字段，区分进程监听、模型可用、业务完整请求通过。

## 15. 验收

### 15.1 必须通过

| ID | 场景 | 预期 |
|---|---|---|
| A01 | 服务器重启后续聊 | 同一 companion_id、人名/称呼/承诺/共同经历仍在 |
| A02 | 模型 A 切到模型 B | 不新建角色；能引用有来源的既有经历，模型参数生效可查 |
| A03 | 同维度不同 Embedding | 拒绝混用；新空间重建后可切换 |
| A04 | 删除全部向量再重建 | 原始记录数量/哈希与人格版本不变 |
| A05 | 开放包导入空环境 | 不用原 Letta 数据目录也能恢复身份、原文、媒体、来源与声音包 |
| A06 | 请求重复发送/断线重连 | 一条用户记录、一个 generation；不重复模型调用或播放 |
| A07 | 用户纠正偏好 | 当前事实更新，过去发生的事件仍可追溯 |
| A08 | AI 编造一句用户经历 | 不自动成为 user_asserted；缺来源的候选被隔离 |
| A09 | 上传图片后更换聊天模型 | 原图和视觉观察保留，仍可定位来源 |
| A10 | 本地声音包合成 | 真实生成可解码音频；正常 TTS 不调用云服务 |
| A11 | 自训练声音 | 有训练清单/权重/配置/评估句，用户试听确认后标通过 |
| A12 | 播放中打断 | 旧 epoch 音频不再播放，口型归零，新输入可继续 |
| A13 | 两设备同角色 | 共享记忆，输入按序生成；设备回执各自独立 |
| A14 | Live2D 动作缺失 | 回落 neutral/idle，文本和音频不受影响 |
| A15 | TTS 崩溃/显存不足 | 文字与档案可用，不无声丢事件 |
| A16 | 跨用户/角色请求 | 未授权来源和资产不可读，检索 scope 不串 |
| A17 | 训练与在线切换 | 资源互斥，训练结束恢复原声音，旧包可回滚 |
| A18 | 主动互动关闭/安静时段 | 不发消息；开启后遵守次数、冷却和队列 |
| A19 | 原始语音与转写修正 | 原录音、ASR 输出和用户修正均可区分 |
| A20 | 删除与备份恢复 | 墓碑生效，已删除记忆不因重建或恢复复活 |

### 15.2 性能目标，必须实测后报告

- 本地非向量查询在 10,000 条记忆基准上 p95 < 500ms，目标机器实测。
- 预热后 30 个中文字的 TTS 首段目标 < 2 秒、持续 RTF < 1；未达成则报告真实值和可选优化，不伪称实时。
- 客户端收到打断后本地播放停止目标 < 200ms；服务器上游取消另行计时。
- Live2D 参考客户端目标 30 FPS 以上，客户端硬件/模型复杂度写入报告。
- 总回复延迟分列：上传、ASR、检索、厂商首 token、首句、TTS、下载/解码、首次播放；不能只报告厂商速度。
- 8GB 机器 TTS 连续 20 段无 OOM；测试期间记录 GPU 其他进程与实际余量。

性能目标不是现有证据。API/数据完整性测试先用 fake adapters；真实厂商、GPU、用户试听只做必要验收，不重复无效压测。

## 16. 交付物

1. 可运行服务与客户端接入 SDK；现有客户端适配或最小参考页面。
2. 已锁定的依赖、上游提交、模型权重哈希、许可证清单。
3. 独立原始档案与人格 schema、数据库迁移、开放导出/恢复工具。
4. 本地 ASR/TTS 服务、声音训练工具链和声音包管理。
5. Live2D 资产映射、口型与播放同步，保留原有素材。
6. CPU Embedding 与混合检索，索引升级/回滚命令。
7. 运行/停止/训练切换/备份/恢复操作说明。
8. docs/ops/acceptance-report.md：每条 Axx 的证据、实际环境、状态和未完成项。

交付状态统一使用：已实现、自动测试通过、目标机器验证通过、用户试听/客户端验收通过、待外部条件。不能只写“已完成”掩盖缺少素材、API 凭据或目标硬件验证。

## 17. 执行前核对项

| 缺失信息 | 当前默认处理 | 何时需要用户信息 |
|---|---|---|
| 正式客户端名称/源码 | 做能力检查，先交付 SDK 与参考客户端 | 接入正式客户端之前 |
| 服务器 OS/SSH/路径 | 先本地 fake adapter 开发，目标部署单独核实 | 安装/启动目标服务之前 |
| 厂商与模型凭据 | 配置 schema + fake contract 测试；不猜测、不读取无关密钥 | 真实 API 验收之前 |
| 声音素材 | 完成工具链；不冒充已有专属声音 | 真实微调之前 |
| 正式角色核心设定 | 使用标明“开发测试”的临时人格，不写入正式角色 | 正式角色创建前 |
| 原音频保留/备份位置 | 提供可配设置，原始数据不自动删除 | 长期实际使用前 |
| 主动聊天偏好 | 默认关闭 | 用户启用时 |

这些信息不阻止独立模块开发。执行方应一次集中提出确实影响下一步的缺失项，不反复要求已确认的权限，也不以信息缺失为由跳过可完成的本地工作。
