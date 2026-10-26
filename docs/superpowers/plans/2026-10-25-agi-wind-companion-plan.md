# AGI.Wind Companion Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox syntax for tracking. 本交接默认由接手 Codex 串行执行，不默认启动多 agent；用户另行指定时再调整。

**Goal:** 交付记忆独立可迁移、使用厂商聊天/视觉、本地自训练声音与 Live2D 的自托管 AI 伴侣。

**Architecture:** 项目网关和 PostgreSQL 保存权威人格、原始经历、媒体引用与派生记忆。Letta 是首选运行时适配器，GPT-SoVITS 是本地语音候选，EverOS 后期可选增强；任何组件不能成为唯一人生档案。

**Tech Stack:** Python 3.12 / FastAPI / Pydantic / SQLAlchemy / Alembic / PostgreSQL 17 + pgvector / TypeScript SDK / GPT-SoVITS 独立环境 / SenseVoiceSmall CPU / BGE-small-zh-v1.5 CPU。

**Spec:** ../specs/2026-10-25-agi-wind-companion-design.md。该设计与本计划必须一起交接，入口为项目根目录 Codex开发交接.md。

## Global Constraints

- 用户聊天模型和看图模型使用厂商 API，语音模型本地运行。
- 服务器 GPU 为用户声明的 RTX 4060 8GB；实际目标、驱动与现有占用必须先核实。
- 语音优先；Embedding、ASR 默认 CPU；不新增本地聊天/视觉大模型。
- companion_id 不随模型、音色、名字、会话和客户端变化。
- 项目原始记录/人格/媒体为权威数据；向量和运行时历史是派生数据。
- 不覆盖现有 tools/、research/、output/live2d/ 和选型报告。
- 真实客户端、服务器连接信息、厂商凭据、正式人格和声音素材尚未提供。
- 先用 fake adapters 完成业务与数据测试；缺凭据不阻止独立开发。
- 文档给出的模型/协议能力要在固定上游版本验证，不以 latest 作为锁定版本。
- 不向聊天、终端输出或测试快照打印凭据、完整私人对话和 base64 媒体。
- 不写全局 AGENTS.md，不改无关服务，不关闭系统防火墙，不停止未知占用进程。
- 有 Git 才提交；当前目录不是 Git 仓库。若开始实现，先明确新源码根与仓库边界；不要把现有大模型/工具/私密数据提交进去。

## Review Focus

1. 一个 request_id 重试、重连和双设备观察，不得产生双份原始记录/付费生成；T02/T05/T10。
2. 用户纠错、AI 自述、虚构剧情和原始经历不能混为事实；T04/T07。
3. 导出时存在正在生成、待提炼、未完成媒体写入或删除墓碑，恢复不能丢记录/复活删除内容；T08。
4. 取消时旧 TTS 仍在计算、网络乱序和客户端重复播放，旧 epoch 不得再响；T09/T10/T11。
5. Embedding 同维度换模型、Letta 私有历史丢失或 EverOS 不可用，角色和原始记忆仍可恢复；T04/T06/T08/T13。

## 0. 文件与实施边界

建议新建业务源码根 app/，不把当前工作目录整个清空或重建。若发现已有正式业务代码，复用其结构，并把映射记录在 docs/ops/environment.md。

~~~text
app/
  pyproject.toml
  uv.lock
  src/agi_wind/
    core/{types,events,errors,settings}.py
    archive/{models,repository,service,assets,exports,imports,backup}.py
    identity/{models,service,projection}.py
    memory/{models,embedding,chunking,keyword,retrieval,context,extraction,corrections}.py
    runtime/{protocol,letta,direct_vendor}.py
    providers/{protocol,openai_compatible,vision,registry}.py
    speech/{protocol,asr,gpt_sovits,voices,scheduler,segmentation,training}.py
    delivery/{models,service,stream,playback}.py
    avatar/{profiles,cues}.py
    proactive/{models,scheduler}.py
    jobs/{models,outbox,worker,handlers}.py
    api/{app,auth,chat,memory,assets,identity,audio,voice_ws,archive,jobs}.py
  migrations/versions/
  tests/{unit,integration,contract,e2e,fixtures}/
packages/
  contracts/                 JSON Schema 与 TS 类型生成输入/产物
  client-sdk/src/            HTTP/SSE/WS、音频/头像控制器
services/
  letta-bridge/              固定版本 Node/TS 运行时桥
  speech/                   语音环境/容器定义，不存模型权重
  everos/                   可选增强器配置与适配部署
clients/
  reference/                正式客户端无法接入时才实现最小页面
config/
  app.example.yaml
  providers.example.yaml
  voices.example.json
deploy/
  compose.yaml
  compose.gpu.yaml
scripts/
  environment-report.*
  verify-docs.*
docs/ops/
  environment.md
  versions.md
  progress.md
  runbook.md
  acceptance-report.md
  licenses.md
data/                       gitignored：数据库以外的媒体、模型、声音、训练、运行时工作目录
~~~

测试数据全部合成，不复制用户私人历史。真实 GPU/厂商用例显式标记 live/gpu，默认测试不联网。

## T01：环境清点、配置与共享契约

**Files**

- Create: docs/ops/environment.md、versions.md、progress.md。
- Create: app/pyproject.toml、app/src/agi_wind/core/{types,events,errors,settings}.py。
- Create: packages/contracts/、config/*.example.*。
- Test: app/tests/unit/test_settings.py、test_contracts.py、app/tests/conftest.py。

**Interfaces**

- Produces: Actor(account_id,user_id)、Scope、ModelRef、PersonaSnapshot、ContentPart、SourceRef、RuntimeRequest、RuntimeDelta、MemoryHit、AudioSegment、AvatarCue、StreamEvent。
- Produces: ChatRequest(request_id,conversation_id,input,output,device_id)、AcceptedRequest(request_id,generation_id,epoch,events_url)。
- Produces: load_settings(path) -> Settings；secret 通过 SecretStr/引用处理。
- Test fixtures: actor、seeded_scope、chat_request_factory、fake_clock、fake_provider、fake_runtime；integration db 必须是真实临时 PostgreSQL，而非用 SQLite 代替。

- [ ] 读取设计与当前目录；只读检查正式客户端线索、服务器连接资料及已有资产。不存在的资料写明未提供，不猜 IP/凭据。
- [ ] 建立依赖与数据排除规则，锁 Python/Node/包版本；只安装业务开发所需轻量依赖，GPU 模型安装留 T09。
- [ ] 写配置校验与契约失败用例：未知模型角色拒绝、unsupported 参数可判定、日志序列化不泄露 SecretStr、跨语言 JSON 字段一致。
- [ ] 定义 DTO 与 JSON Schema，生成 TS 类型；协议 1.0，后续新增可选字段不改必需字段语义。
- [ ] 验证：uv run pytest tests/unit/test_settings.py tests/unit/test_contracts.py -q；pnpm contracts:check。预期全部通过、生成类型无漂移。
- [ ] 若 Git 可用，提交本任务源码/测试/文档；若无 Git 且尚未建立仓库，保留改动清单，不执行失败的提交循环。

**交付**：接手者能在无服务器、无 API key 条件下加载配置并验证契约。environment.md 明确当前发现的 Live2D 预览不是已验收聊天客户端。

## T02：权威原始档案、媒体与幂等

**Files**

- Create: app/src/agi_wind/archive/{models,repository,service,assets}.py。
- Create: app/src/agi_wind/delivery/models.py。
- Create: app/src/agi_wind/jobs/models.py。
- Create: app/migrations/versions/0001_archive.py。
- Test: app/tests/integration/test_archive.py、test_assets.py。

**Interfaces**

- Consumes: Actor、ChatRequest、AcceptedRequest。
- Produces: ArchiveService.accept(actor: Actor, request: ChatRequest) -> AcceptedRequest。
- Produces: AssetStore.put(actor: Actor, stream: BinaryIO, declared_mime: str) -> AssetRecord。
- Produces: ArchiveService.append_output(generation_id, delta, final_state=None) -> EventRecord。
- Produces: 事务性 requests/events/generations/outbox 与 asset_refs；后台提炼后续消费 outbox。后续 delivery 迁移补 speech_segments/avatar_cues 与短期重放表，永久信息不得只留 SSE。

- [ ] 写失败测试；代表测试：

~~~python
async def test_duplicate_request_reuses_generation(archive, actor, chat_request_factory):
    request = chat_request_factory(text="记住今天一起看了海")
    first = await archive.accept(actor, request)
    second = await archive.accept(actor, request)
    assert first.generation_id == second.generation_id
    assert await archive.count_user_events(request.request_id) == 1
    assert await archive.count_generation_jobs(request.request_id) == 1
~~~

- [ ] 添加同 ID 不同输入 409、事务回滚无 accepted、跨账号资产 403、重复媒体去重而引用不丢失、上传中断不产生 ready 资产用例。
- [ ] 运行上述 integration 用例确认失败；失败必须来自缺失行为而不是测试环境未启动。
- [ ] 实现迁移、输入哈希、事务写入和内容寻址媒体存储；原件与派生文件分别记录。
- [ ] 验证：uv run pytest tests/integration/test_archive.py tests/integration/test_assets.py -q；alembic upgrade head；在空测试库恢复迁移。
- [ ] 提交本任务；progress.md 记录实现/测试状态，不记录真实密钥。

**交付**：任何回答发生前，用户输入都有可靠归档；客户端重复发送不重复生成。

## T03：稳定身份、人格版本、关系与承诺

**Files**

- Create: app/src/agi_wind/identity/{models,service,projection}.py。
- Create: app/src/agi_wind/api/identity.py。
- Create: app/migrations/versions/0002_identity.py。
- Test: app/tests/integration/test_identity.py。

**Interfaces**

- Produces: IdentityService.snapshot(actor, companion_id) -> PersonaSnapshot。
- Produces: IdentityService.update(actor, companion_id, expected_version: int, patch: dict) -> PersonaSnapshot。
- Produces: IdentityService.bind_model(actor, companion_id, model_ref: ModelRef) -> ModelRef。
- Produces: runtime_bindings；身份永远使用项目 UUID。

- [ ] 写失败测试：修改模型/声音/名称后 companion_id 不变；expected_version 过期返回 409；两个会话读取同一有效关系与承诺。
- [ ] 写 AI persona proposal 不能覆盖受保护核心、用户显式变更生成新版本、旧版本仍可读的用例。
- [ ] 实现人格/关系/承诺表及版本控制；正式人格未提供时使用清楚标明“开发测试”的临时角色。
- [ ] 验证：uv run pytest tests/integration/test_identity.py -q；测试中至少包括第二个账号不能读取该角色。
- [ ] 提交；输出固定角色与跨会话共享状态的接口示例，不把测试人格当作用户正式人格。

## T04：CPU Embedding、中文混合检索与上下文构建

**Files**

- Create: app/src/agi_wind/memory/{models,embedding,chunking,keyword,retrieval,context,corrections}.py。
- Create: app/migrations/versions/0003_memory.py。
- Test: app/tests/unit/test_chunking.py、test_embedding_space.py、test_context_budget.py。
- Test: app/tests/integration/test_retrieval.py、test_corrections.py。

**Interfaces**

- Produces: EmbeddingService.embed(texts: list[str], space_id: str) -> list[list[float]]。
- Produces: MemoryService.search(scope: Scope, query: str, time_range=None) -> list[MemoryHit]。
- Produces: ContextBuilder.build(scope, current_input, model_ref) -> ContextBundle。
- Produces: apply_correction(actor, memory_id, correction_text, source_event_id) -> CorrectionResult。
- ContextBundle 必需字段：memory_revision、persona_snapshot、recent_events、memories、current_input、vision_observations、token_estimate、selected_source_ids。

- [ ] 用 fake embedder 写失败用例：同维度不同 fingerprint 禁止混查；长输入按实际 tokenizer 拆分且 offsets 可回指原文。
- [ ] 准备中文合成数据：人物重名、否定、偏好变化、去年/今年、含精确日期型号、虚构剧情；不得只测“喜欢咖啡”。
- [ ] 实现 BGE CPU provider、空间元数据、中文关键词/精确检索、RRF 去重、scope 过滤。
- [ ] 实现 budget：最多 12 个片段、每事件最多 2 片、检索最多 3,000 tokens；当前输入不得静默截断。
- [ ] 验证“原始向量删除后关键词检索仍可用”“纠错保留旧事件但当前偏好更新”“跨账号/角色结果不串”；最终上下文再次检查 memory_revision 与删除标记，已用过期快照生成的活动回答要中断。
- [ ] 运行：uv run pytest tests/unit/test_chunking.py tests/unit/test_embedding_space.py tests/unit/test_context_budget.py tests/integration/test_retrieval.py tests/integration/test_corrections.py -q。
- [ ] 只进行一次真实 BGE CPU 小批次 smoke，记录模型 hash/维度与运行设备；不做全目录向量化。
- [ ] 提交；此时记忆检索不依赖 Letta 或 EverOS 启动。

## T05：厂商适配、视觉与可重连文字链路

**Files**

- Create: app/src/agi_wind/providers/{protocol,openai_compatible,vision,registry}.py。
- Create: app/src/agi_wind/runtime/{protocol,direct_vendor}.py。
- Create: app/src/agi_wind/delivery/{service,stream,playback}.py。
- Create: app/src/agi_wind/api/{app,auth,chat,assets,memory}.py。
- Create: app/migrations/versions/0004_delivery.py。
- Test: app/tests/contract/test_provider.py、test_sse.py、app/tests/integration/test_generation.py、test_vision.py。

**Interfaces**

- Consumes: ArchiveService、IdentityService、ContextBuilder。
- Produces: GenerationService.submit(actor, request: ChatRequest) -> AcceptedRequest。
- Produces: GenerationService.run(generation_id) -> None。
- Produces: stream_events(actor, generation_id, after_sequence: int) -> AsyncIterator[StreamEvent]。
- Produces: VisionAdapter.observe(scope, asset_ids, question) -> VisionObservation。
- Produces: RuntimeAdapter 的 direct_vendor 实现；先用 fake_provider，真实凭据通过用户提供的安全途径配置。

- [ ] 写失败测试：DB 失败时 vendor_calls=0；SSE 重连序号不重发已经确认事件；生成崩溃保留 partial 而非 complete。
- [ ] 实现项目 chat request → 原文归档 → 检索 → provider stream → 持久化输出 → SSE 的最小链路。每批文字先提交数据库再发送；文本结束为 text.completed，没有音频任务的请求才能此时直接 generation.completed。
- [ ] 迁移加入 speech_segments、avatar_cues、delivery_commands、conversation_live_events；让永久语音/角色状态与短期 SSE 分离。
- [ ] 实现访问控制、media upload、VisionObservation 原件引用；vision 超时不能产生虚假 description。
- [ ] 兼容端点只映射相同业务服务；X-AGI-Conversation-ID 缺失时明确独立会话，不能猜测身份。
- [ ] 验证供应商参数过滤、图像实际载荷转换、API 错误脱敏、HTTP 中断后的 partial 状态。
- [ ] 运行：uv run pytest tests/contract/test_provider.py tests/contract/test_sse.py tests/integration/test_generation.py tests/integration/test_vision.py -q。
- [ ] 如用户已提供厂商与凭据，做一条真实文本与一张非私人测试图片请求；未提供则 live 状态 pending，不编造成功。
- [ ] 提交；交付无声音也可运行的文字/图片业务闭环。

## T06：Letta 运行时桥与人格单向投影

**Files**

- Create: services/letta-bridge/package.json、src/{server,client,capabilities,projection}.ts。
- Create: app/src/agi_wind/runtime/letta.py。
- Create: app/tests/contract/test_letta_bridge.py。
- Create: services/letta-bridge/tests/{identity,context,model-switch,cancel}.test.ts。

**Interfaces**

- Consumes: RuntimeAdapter、PersonaSnapshot、ContextBundle、runtime_bindings。
- Produces: ensure_binding、apply_persona、configure_model、stream、cancel、probe。
- bridge 仅对业务服务提供内部接口，不对客户端暴露 SDK/工作目录。

- [ ] 固定 Letta package/SDK 版本；启动 local backend，确认真实状态目录和私有接口，写 versions.md。
- [ ] 先用 bridge fake client 写协议测试：同 companion 复用 binding、persona_version 落后时先更新、旧输出携带正确 generation/epoch。
- [ ] 实现 SDK 调用并验证上下文只注入一次；禁用/替换不需要的宿主机工具，工作目录限制为该角色投影目录。
- [ ] 真实能力探针必须完成：同 agent 两轮、换模型不换 agent、项目人格变更可见、传入项目记忆、取消、重启恢复。
- [ ] 新建空 Letta 状态目录，从项目档案恢复 binding，证明不是依赖旧私有历史才能续聊。
- [ ] 用纠错/删除递增 memory_revision 的用例验证旧 binding/context 禁用、新上下文从权威档案重建；专属本地副本清理不能靠一条“请忘掉”指令代替。
- [ ] 运行 Python bridge 契约和 TS 测试；真实能力缺失时记录失败项与证据，继续其他模块，不把 direct_vendor 标为 Letta 成功。
- [ ] 提交；只有探针通过才把 preferred runtime 的 readiness 标可用。

## T07：增量提炼、事实来源、自传与后台队列

**Files**

- Create: app/src/agi_wind/memory/extraction.py。
- Create: app/src/agi_wind/jobs/{outbox,worker,handlers}.py。
- Create: app/src/agi_wind/api/jobs.py。
- Test: app/tests/unit/test_extraction.py、app/tests/integration/test_jobs.py、test_memory_provenance.py。

**Interfaces**

- Produces: Extractor.extract(scope, event_batch) -> list[MemoryCandidate]。
- MemoryCandidate：kind、content、epistemic_type、confidence、source_refs、occurred_at、relations。
- Produces: MemoryWriter.validate_and_store(candidate) -> MemoryRecord | QuarantineRecord。
- Produces: Worker.claim(worker_id, lease_seconds=120, batch_size=4) -> list[Job]。
- Produces: Worker.run_job(job_id) -> JobResult。

- [ ] 写失败测试：模型虚构用户生日无出处→quarantine；AI 的“我小时候住海边”不能变用户事实；用户剧情段落→fictional。
- [ ] 写 outbox 崩溃重试/租约过期/双 worker 抢同任务的测试；最多 5 次重试，间隔 5×2^attempt 秒，上限 300 秒并加抖动。
- [ ] 实现 lease 每 30 秒续约、checkpoint、增量批次；不持有 SQL 写事务等待远程模型。
- [ ] 实现 8 轮/空闲 120 秒合并触发；提炼费用到上限只延后任务。
- [ ] 实现事实、episode、relationship、self_journal、reflection 的来源和状态；用户修正会触发旧派生记忆失效/修正。
- [ ] worker 写回前检查 memory_revision/墓碑；提炼过程中用户删除的旧事件不允许重新入库。
- [ ] 运行：uv run pytest tests/unit/test_extraction.py tests/integration/test_jobs.py tests/integration/test_memory_provenance.py -q。
- [ ] 提交；原文不会因为提炼失败而丢失，worker 退出再启动可以续跑。

## T08：开放导出/恢复、向量迁移与备份

**Files**

- Create: app/src/agi_wind/archive/{exports,imports,backup}.py。
- Create: app/src/agi_wind/api/archive.py。
- Create: app/tests/integration/test_archive_roundtrip.py、test_reindex.py、test_delete_restore.py。
- Create: app/tests/fixtures/archive-v1/（仅合成数据）。

**Interfaces**

- Produces: ExportService.create(actor, companion_id, options) -> JobId。
- Produces: ImportService.validate(path) -> ValidationReport。
- Produces: ImportService.restore(path, mode: Literal["new_namespace","clone","replace"]) -> ImportReport。
- Produces: IndexService.rebuild(space_id, from_checkpoint=None) -> JobId。
- Produces: BackupService.create(target_ref) -> BackupManifest。
- Produces: DeletionLedger.append(intent) -> Tombstone；load_latest(control_dir) -> LedgerSnapshot；独立账本不得被旧数据库恢复覆盖。
- CLI：uv run agi-wind archive export/import/verify；uv run agi-wind index rebuild；真实参数通过 --help 固定并写 runbook。

- [ ] 写失败测试：完整 roundtrip 后 events/来源/媒体/人格/声音清单哈希一致；空 Letta 仍能恢复身份。
- [ ] 加入导出时生成中、未提炼队列、取消音频、删除墓碑、共享资产、未就绪上传场景；验证 speech_segments/声音版本/回执关联完整恢复。
- [ ] 实现维护屏障与一致性快照；manifest 和 NDJSON 覆盖设计 §13 所有权威实体。
- [ ] 导入先验证 zip/tar 路径、记录数、SHA256、外键、schema；冲突不自动覆盖。
- [ ] 实现新 embedding_space 并行回填与双写追平、原子切换；同维度不同模型仍新建空间；故障不切索引。
- [ ] 常规备份复用数据库一致性切点与资产 pin，验证并发删除不会形成缺文件的“成功备份”；不把 API secrets 放归档。
- [ ] 实现独立 fsync 删除账本、独立备份副本及启动重放；测试“删除前的旧备份 + 最新账本”恢复后不复活内容，缺最新账本不得悄悄提升为正式角色。
- [ ] 运行：uv run pytest tests/integration/test_archive_roundtrip.py tests/integration/test_reindex.py tests/integration/test_delete_restore.py -q。
- [ ] 执行一次真实“导出→空库导入→禁用向量查询→重建→召回”的本地演练，记录命令、哈希、计数。
- [ ] 提交；数据可迁移验收必须在语音功能扩张之前完成。

## T09：本地 ASR/TTS、声音包和 GPU 资源隔离

**Files**

- Create: app/src/agi_wind/speech/{protocol,asr,gpt_sovits,voices,scheduler}.py。
- Create: app/src/agi_wind/api/audio.py。
- Create: services/speech/、deploy/compose.gpu.yaml。
- Test: app/tests/contract/test_speech_adapters.py、app/tests/integration/test_voice_profiles.py。

**Interfaces**

- Produces: AsrAdapter.transcribe(asset: AssetRecord, language: str | None) -> Transcript。
- Produces: TtsAdapter.synthesize(text: str, voice: VoiceProfile, cancel_token) -> SynthesizedAudio。
- SynthesizedAudio：临时文件/字节流、实际采样率、channels、duration_ms、engine_revision。
- Produces: VoiceRegistry.import_bundle(path) -> VoiceProfile。
- Produces: SpeechScheduler.run_exclusive(job) -> AudioSegment；队列容量 8、GPU 执行并发 1。

- [ ] 用假音频服务写失败测试：上游返回 JSON error 不能当 WAV；未知 voice 拒绝；权重 hash 不匹配/路径越界拒绝；旧 epoch 合成结果不入播放队列。
- [ ] 核实目标 GPU 与驱动、其他服务占用；没有目标访问资料时完成 adapter 与部署配置，gpu/live 标 pending。
- [ ] 在专用目录/环境安装锁定 GPT-SoVITS 和底模；不修改现有模型服务，不打开公网训练面板。
- [ ] 锁定请求 schema；映射项目 voice_id 到服务器声音包路径；不让客户端提交任意 ref_audio_path/权重路径。
- [ ] 实现 CPU SenseVoice 与 CPU Embedding 隔离；获取模型后正常运行不再隐式联网下载。
- [ ] 单次目标探针：加载、中文/数字/中英各一段、解码和真实峰值；显存不足先调整版本/配置，不开大批量循环。
- [ ] 运行 adapter/registry 测试；如硬件已就绪再完成连续 20 段 TTS，用例只跑一次，报告真实 RTF/显存。
- [ ] 提交；训练尚未完成时只标“本地推理通过”，不能标“自训练声音通过”。

## T10：句级音频、语音输入、打断与投递状态

**Files**

- Create: app/src/agi_wind/speech/segmentation.py。
- Modify: app/src/agi_wind/delivery/{service,stream,playback}.py。
- Create: app/src/agi_wind/api/voice_ws.py。
- Test: app/tests/unit/test_segmentation.py、app/tests/integration/test_interrupt.py、test_voice_input.py、test_playback.py。

**Interfaces**

- Produces: SentenceSegmenter.feed(delta: str, final=False) -> list[TextSegment]。
- Produces: SpeechCoordinator.enqueue(generation_id, epoch, segment: TextSegment) -> None。
- Produces: GenerationService.interrupt(actor, generation_id, command_id: UUID) -> CancelResult。
- Produces: PlaybackService.receipt(actor, receipt: PlaybackReceipt) -> None。
- CancelResult：generation_id、generation_state、delivery_state、epoch、upstream_cancelled；重复 command_id 返回原结果，已取消且无新活动不再次增加 epoch。

- [ ] 写失败测试：token 分块刚好切在数字/小数/代码围栏；句段不得吞字、重字或朗读内部情绪标签。
- [ ] 测试乱序 audio.segment、取消后迟到音频、终止事件后迟到文本、重复播放回执；旧 epoch 永不再次可播放。
- [ ] 实现文本流与句级 TTS 并行、真实音频元数据、授权下载及终态 SSE；文字结束先 draining，尾段音频/表情事件完成或明确失败后才 generation.completed。
- [ ] 测试“文字完成但尾句 TTS 未完成”不会丢尾句；已 generation.completed 但设备仍播放时，interrupt 通过独立 conversation live 流让两设备停止。
- [ ] 实现 PCM WS 输入限额、最终转写及发送确认；不伪造 transcript.partial。
- [ ] 实现生成内容/已送达/实际播放三类状态与回执；打断只取消相应 generation，不杀共享服务。
- [ ] 运行：uv run pytest tests/unit/test_segmentation.py tests/integration/test_interrupt.py tests/integration/test_voice_input.py tests/integration/test_playback.py -q。
- [ ] 提交；完成至少一次“说话→ASR→回答→本地声音→打断→继续”的可回放合成事件测试。

## T11：现有客户端接入、Live2D 与参考页面

**Files**

- Create: packages/client-sdk/src/{types,api,sse,voice-input,audio-player,avatar-controller}.ts。
- Create: app/src/agi_wind/avatar/{profiles,cues}.py。
- Create: clients/reference/，仅在现有客户端不能完成接入时。
- Test: packages/client-sdk/tests/{reconnect,audio-player,avatar-controller}.test.ts。
- Test: clients/reference/tests/e2e/companion.spec.ts。

**Interfaces**

- Consumes: 协议 1.0、AudioSegment、AvatarCue。
- Produces: CompanionClient.submit/subscribe/interrupt；AudioPlayer.enqueue/stop/receipt。
- Produces: AvatarController.load(profile)/applyCue(cue)/setMouthLevel(level)/reset()。
- Reference client 只包含连接设置、聊天、录音、播放、Live2D、来源查看，不创建新账号平台/支付/模型市场。

- [ ] 核对正式客户端接口；能接则写 adapter，不能接则 SDK + reference；不把原有 viewer 的“说话预览”当作真实 TTS。
- [ ] 测试重连只订阅原 generation、双设备不误提交、旧 epoch 音频不播、播放回执符合实际位置；generation 流结束后仍监听会话控制流。
- [ ] 加载现有 WindChibi 副本/只读引用，检查实际参数与表情动作，建立 avatar profile；不覆盖原 .moc3/纹理。
- [ ] 口型基于实际 AudioContext 音频能量；attack 40ms、release 100ms 可配置；断线/打断/结束归零。
- [ ] 测试未知动作/表情回 neutral、模型加载失败仍能聊天、标签不被 TTS 朗读。
- [ ] 运行 pnpm --filter client-sdk test 与 reference e2e；用实际浏览器检查麦克风授权、HTTPS 和音频自动播放限制。
- [ ] 目标客户端验收真实语音播放、表情同步与打断；记录客户端/浏览器/设备/模型版本，不以截图证明全链路成功。
- [ ] 提交；正式客户端不可访问则明确“参考客户端通过，正式客户端待接入”。

## T12：声音训练工具链与专属声音交付

**Files**

- Create: app/src/agi_wind/speech/training.py。
- Create: services/speech/training/（数据清单、预处理、微调调用封装、评估、打包）。
- Create: docs/ops/voice-training.md。
- Test: app/tests/unit/test_training_manifest.py、app/tests/integration/test_training_mode.py。

**Interfaces**

- Produces: TrainingService.prepare(dataset_dir, config) -> DatasetManifest。
- Produces: TrainingService.enter_mode() -> ResourceLease；退出必须恢复原在线版本。
- Produces: TrainingService.run(manifest, training_config) -> TrainingRun。
- Produces: TrainingService.package(run_id) -> VoiceBundle。

- [ ] 写失败测试：训练/验证录音泄漏、缺文本、重复素材、恢复线上服务失败有明确状态、训练与在线 GPU 互斥。
- [ ] 实现原件保留、切片/重采样派生、转写校对清单和固定 20 句评估集。
- [ ] 使用用户提供的素材做小样本预检与微调；没有素材时仅交付工具链并标“待素材”，不借别人的预训练声音冒充专属音色。
- [ ] 记录所有底模、工具版本、超参数、checkpoint、数据 hash、实际训练 GPU 峰值。
- [ ] 合成评估句并提供音频给用户试听；激活新声音前保留旧 voice version 可回滚。
- [ ] 运行训练相关测试；训练完成和用户试听通过分别记账。
- [ ] 提交源码/清单模板/说明；原始录音与权重放 data/ 不入 Git。

## T13：EverOS 可选增强与适配验证

**Files**

- Create: app/src/agi_wind/memory/engines/{protocol,everos}.py。
- Create: services/everos/。
- Test: app/tests/contract/test_everos.py、app/tests/integration/test_external_memory.py。

**Interfaces**

- Produces: MemoryEngineAdapter.ingest(batch: EventBatch) -> IngestReceipt。
- Produces: flush(scope)、search(scope, query) -> list[ExternalMemoryCandidate]、health()、snapshot()。
- Produces: invalidate(scope, memory_revision, tombstones) -> InvalidationReceipt；rebuild(scope, memory_revision)；purge 能力单独报告。
- ExternalMemoryCandidate 必须携带 engine、external_id、source_batch_id、scope、content、time_range、provenance_precision。

- [ ] 先固定上游版本，读取其实际 API schema，不把设计里的 DTO 直接当 EverOS 请求。
- [ ] 测试上游重复投递/超时后不确定接收状态、scope 隔离、批次来源映射和禁用后原生记忆仍可用。
- [ ] 实现本地同步台账；幂等无法保证时按外部 ID/内容批次去重，记录不确定状态，不无限重复 add。
- [ ] shadow 模式运行合成评估，不改变真实回复；无精确来源的结果明确批次级来源。
- [ ] 通过后由配置启用增强检索；项目默认检索与原始档案不依赖其可用性。
- [ ] 验证关闭/卸载 EverOS 后，T08 导入与跨会话检索不变。
- [ ] 验证纠错/永久删除后旧 namespace 立即禁查，重建完成前回退原生检索；缓存/本地副本物理清理另有状态与证据。
- [ ] 提交；若收益不明显或运维不合适，可保持 disabled，不能影响核心验收。

## T14：主动陪伴、承诺跟进与持续自传

**Files**

- Create: app/src/agi_wind/proactive/{models,scheduler}.py。
- Modify: app/src/agi_wind/api/identity.py。
- Test: app/tests/unit/test_proactive.py、app/tests/integration/test_companion_continuity.py。

**Interfaces**

- Produces: ProactiveService.update_settings(actor, companion_id, patch) -> Settings。
- Produces: ProactiveService.evaluate(scope, now) -> ProactiveCandidate | None。
- 候选仍通过 GenerationService.submit/run 归档与串行化，不另建身份。

- [ ] 用 fake_clock 测默认关闭、23:00—09:00、每日最多 2 次、冷却 4 小时、正在输入/待答/离线不发送。
- [ ] 实现已授权承诺/纪念日跟进；不自行通过 QQ/邮件等外部平台发信。
- [ ] 测试跨模型、跨会话的称呼、共同经历、未完成承诺和自传连续性；来源不够不能编故事。
- [ ] 验证新消息不重复主动候选；用户关闭后取消待发送候选。
- [ ] 运行：uv run pytest tests/unit/test_proactive.py tests/integration/test_companion_continuity.py -q。
- [ ] 提交；用真实厂商试听/聊天评估表达是否生硬，结果写为主观用户验收，不伪造量化“像人”分数。

## T15：部署、恢复演练与交付

**Files**

- Create: deploy/compose.yaml、app/Dockerfile、services/*/Dockerfile（按适配需要）。
- Create: docs/ops/runbook.md、acceptance-report.md、licenses.md。
- Create: scripts/environment-report.*。
- Test: app/tests/e2e/test_end_to_end.py。

**Interfaces**

- Produces: 启动/停止/状态/训练切换/导出/恢复命令；所有端口和目录可配置。
- Produces: /health/live、/health/ready、/api/v1/capabilities 和验证报告。

- [ ] 在目标机器确认 OS、CPU/RAM/磁盘、GPU 驱动、NVIDIA 容器支持、端口、部署路径与其他服务占用。
- [ ] 使用专属目录与凭据部署；仅暴露 HTTPS 网关，内部模型/数据库/训练 UI 不直接公网开放。
- [ ] 配置健康检查、重启策略、只读模型挂载/可写状态目录、停止清理、日志限额与备份目标。
- [ ] 按设计 A01—A20 执行必要验收；先完成自动测试，再做真实厂商/GPU/客户端/用户试听。已经通过的检查不无故反复跑。
- [ ] 完成一次冷重启续聊、换聊天模型、声音回滚、索引重建、空环境恢复，记录计数和哈希。
- [ ] 实测端到端耗时分段与 GPU 余量；将未达性能目标的真实值写报告。
- [ ] acceptance-report 必须逐项写：自动测试、目标机器验证、用户验收、待条件；附实际命令和脱敏证据位置。
- [ ] 提交源码和文档；不提交 secrets、数据库、训练音频、模型权重。若用户要求发布/部署范围之外的外部操作，另依据该会话授权处理。

## 阶段验收与恢复点

| 阶段 | 包含任务 | 可交付能力 | 进入下一阶段前必须确认 |
|---|---|---|---|
| M0 | T01 | 可验证契约与环境报告 | 数据/源码目录分离 |
| M1 | T02—T05 | 原文归档、人格、记忆、厂商文字/视觉 | 幂等、来源、scope 测试 |
| M2 | T06—T08 | Letta 接入、增量成长、可迁移档案 | 空库恢复、无 Letta 私有目录恢复 |
| M3 | T09—T11 | 本地语音、打断、Live2D | GPU、实际客户端/参考端全链路 |
| M4 | T12 | 自训练声音 | 用户素材、微调权重、试听验收 |
| M5 | T13—T14 | 可选记忆增强、主动陪伴 | 禁用增强不影响核心、主动开关有效 |
| M6 | T15 | 目标部署与运维交付 | A01—A20 证据或明确待条件 |

每阶段结束更新 progress.md，仅列：当前阶段、已通过检查、未通过项、下一任务、所需输入。不要在聊天里粘贴整份设计或重复调研所有框架。

## 接手者执行纪律

- 保留用户最新指令优先级；本计划不是额外授权。
- 不在本任务之外创建新的用户聊天，不自动发送消息给别人。
- 不把“服务启动”“端口监听”“模型下载完成”写成业务验收通过。
- 没有真实客户端/声音素材/厂商凭据时，把相关验收标待条件，继续可独立完成的工作。
- 必须变更架构时，在 docs/ops/decisions.md 写明原因、影响和迁移方式；不能无声换回框架私有记忆。
- 每个任务先验证高风险行为，再实现最小改动；低价值的配置镜像测试不增加。
- 不为通过测试删除数据完整性、幂等、scope 隔离或恢复验收。
- 每次停止提供准确状态：哪些已写文件、哪些运行、哪些需要真实环境验证。
