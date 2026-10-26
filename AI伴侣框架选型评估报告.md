# 自建「像人」的开源 AI 伴侣：框架选型与落地评估报告

> 调研日期：**2026-10-24**（star / License / 最近提交均为当日快照，数据会变）
> 调研方式：3 条并行线（前端框架 / 记忆层 / 语音多模态与部署）+ 主控 GitHub 元数据核验
> 标注约定：**✅ 有官方文档或仓库证据**；**◐ 部分支持/需扩展**；**❌ 不支持**；**〔推断〕** = 我的判断，未逐条实测。
> 附录：[前端框架详评](research/frontends.md) · [记忆层详评](research/memory.md) · [语音/多模态/部署详评](research/voice-stack.md)

---

## 0. 一页结论（先看这里）

**没有单一开源项目能同时满足你的 10 条硬性要求。** 这一点三份调研独立得出同一结论：开源社区把「陪伴外壳」「长期记忆」「语音」「多端」做在了不同项目里，而且**没有一个框架的记忆数据是完全独立于框架本身的**。

所以正确的问题不是「选哪个框架」，而是：

> **把「他」做成一坨可以独立存在的东西（记忆 + 人格 + 编排），框架/客户端只是随时可以换掉的皮。**

三条核心判断：

1. **记忆的真相源必须是「原文 + 结构化字段」，向量只是可丢弃、可重建的索引。**
   这不是洁癖：换 embedding 模型时，全行业几乎都会静默降级（同维度换模型不报错，只是越用越笨——Cognee 官方文档自己承认只比对维度、不比对模型名）。只有把文本捏在自己手里，换模型才是「跑一晚批处理」，而不是「记忆丢失」。
2. **换模型不断人格的关键，是让「人格」不活在模型里。**
   人格 = 你自己的结构化状态（关系阶段、称呼、承诺、禁忌、时间线、情绪基线），每轮动态拼进 prompt；模型只是一个可替换的"嗓子"。用 LiteLLM 把对外的 `companion-chat` 别名指到任意上游，前端零改动。
3. **前端必须可以当消耗品。**
   把你的核心服务包装成一个 **OpenAI 兼容的"模型"**（`POST /v1/chat/completions`），于是 SillyTavern、Open WebUI、LobeChat、AIRI、甚至你自己写的 PWA，全都能直接接上来——界面换了，"他"的记忆和人格仍然在服务器上。

**推荐路线（详见 §7）：**

| 路线 | 组合 | 适合 | 上手 | 长期上限 |
|---|---|---|---|---|
| **A 陪伴优先（最快见效）** | SillyTavern + 自建 pgvector 记忆服务 + OpenAI 兼容网关 + TTS | 现在就想聊、角色扮演深度优先 | 3–7 天 | 中高 |
| **B 平台优先（最省运维）** | Open WebUI 内核（Voice Mode + VRM 头像 + Memory + 13 种向量库） | 多设备/多用户、要现成的语音与界面 | 1–2 天 | 中 |
| **C 自研核心（最符合你的目标）★** | 自研 core-brain（人格+记忆编排，暴露 OpenAI 兼容端点）+ 任意前端 + LiteLLM + vLLM/Pipecat | 要「完整的一生」、记忆独立可迁移 | 2–4 周 | 最高 |

**如果只能先做一件事**：先把「记忆表结构 + 导出格式」定下来（§4.3），它决定了十年后"他"还在不在；外壳随时能换。

---

## 1. 候选与硬性要求对照

### 1.1 基础盘（2026-10-24 快照）

| 项目 | 仓库 | License | Star | 最近提交 | 形态 |
|---|---|---|---|---|---|
| **SillyTavern** | SillyTavern/SillyTavern | **AGPL-3.0** | 34.9k | 2026-10-02 | Node 后端 + Web 前端，**无数据库**，数据全在 `data/` 文件 |
| **RisuAI** | kwaroran/RisuAI | GPL-3.0 | 1.7k | 2026-10-22 | Svelte/Tauri + Docker(hono) 服务端 |
| **Agnai** | agnaistic/agnai | AGPL-3.0 | 0.8k | 2026-06-15（趋缓） | SolidJS + MongoDB 必需，多租户 |
| **Open WebUI** | open-webui/open-webui | **Open WebUI License**（BSD-3 + 品牌条款，非 OSI） | 156.6k | 2026-10-24 | SvelteKit + FastAPI，SQLite/Postgres，多用户 |
| **LobeChat / LobeHub** | lobehub/lobehub | **LobeHub Community License**（Apache-2.0 + 商业限制） | 83.5k | 2026-10-24 | Next.js 全栈，服务端需 Postgres+pgvector |
| **LibreChat** | LibreChat-AI/LibreChat | **MIT** | 46.2k | 2026-10-24 | Express + React，MongoDB 必需 |
| **AnythingLLM** | Mintplex-Labs/anything-llm | **MIT** | 67.5k | 2026-10-24 | Node + React，SQLite + LanceDB 等 9 种向量库 |
| **AIRI** | moeru-ai/airi | **MIT** | 50.7k | 2026-10-24 | Vue3 多端（Web/Electron/Capacitor）+ server-runtime |
| **Open-LLM-VTuber** | Open-LLM-VTuber/Open-LLM-VTuber | MIT（Live2D 示例模型除外） | 14.4k | 2026-05-15（v2 重写中） | Python + Web/Electron 桌面宠物 |
| **Amica** | semperai/amica | MIT（3D 资产除外） | 1.6k | 2026-09-22 | 纯前端 Next.js + Tauri |
| *ChatterUI* | Vali-98/ChatterUI | AGPL-3.0 | 2.8k | 2026-10-05 | Flutter 移动客户端，可连任意 OpenAI 兼容 API |

### 1.2 十条硬性要求逐项打勾

| 项目 | 开源 | 语音可打断 | 会聊天/陪伴 | 长期向量记忆 | 看图 | 模型可换 | **记忆可迁移** | 服务器+多客户端 | 可扩展 |
|---|---|---|---|---|---|---|---|---|---|
| SillyTavern | ✅AGPL | ❌按句朗读 | ✅✅ 角色卡/世界书 | ✅ Vectra | ✅ | ✅ 任意 OpenAI 兼容 | ✅✅ **文件级，打包即迁** | ◐ 单用户无鉴权 | ✅ 扩展/插件 |
| RisuAI | ✅GPL | ◐ | ✅✅ | ◐ 摘要式非向量 | ✅ | ✅ | ◐ 藏在聊天数据里 | ◐ Docker 单用户 | ✅ 插件 |
| Agnai | ✅AGPL | ◐ | ✅ | ✅ pipeline | ✅ | ✅ | ◐ Mongo dump | ✅ 多租户 | ✅ pipeline |
| **Open WebUI** | ◐ 品牌条款 | ✅✅ Realtime 插话 | ✅ | ✅ 条目式 + 13 向量库 | ✅ | ✅ | ◐ DB dump | ✅✅ | ✅✅ MCP/Tools/API |
| LobeChat | ◐ 商业限制 | ◐ | ◐ Agent 化 | ✅ 条目 + pgvector | ✅ | ✅ | ◐ DB dump | ✅ | ✅ 插件/MCP |
| LibreChat | ✅MIT | ◐ | ❌ 工作台 | ❌ **明确非语义记忆** | ✅ | ✅ | ◐ Mongo | ✅ | ✅✅ MCP/Agents |
| AnythingLLM | ✅MIT | ◐ STT 仅浏览器 | ◐ 工作台 | ✅ LanceDB 等 9 种 | ✅ | ✅ | ✅ `storage/` 目录 | ✅ Docker 多用户 | ✅ MCP/Flows |
| AIRI | ✅MIT | ✅✅ 客户端 VAD | ✅✅ Live2D/VRM | ⚠️ **WIP 未完工** | ✅ | ✅ | ⚠️ 设备本地 | ◐ server-runtime 早期 | ◐ 插件 WIP |
| Open-LLM-VTuber | ✅MIT | ✅✅ 免耳机打断 | ✅✅ 桌面宠物 | ❌ **已临时移除** | ✅ | ✅ | ◐ | ◐ 单用户 | ✅ Agent 接口 |
| Amica | ✅MIT | ✅ | ✅ VRM 情绪 | ❌ | ✅ | ✅ | ❌ | ❌ 纯前端 | ✅ 插件 |
| **自研 core-brain ★** | ✅ | ✅（接 Pipecat） | ✅（自己定义） | ✅✅ **完全自主** | ✅ | ✅✅ 网关别名 | ✅✅ **一条命令导出** | ✅✅ | ✅✅ |

**读表要点：**
- 「记忆可迁移」这一列，只有 **SillyTavern（文件）、AnythingLLM（storage 目录）、自研核心** 达到「拷目录/一条命令就是全部」的级别；其余都要「整库 dump + 祈祷框架认自己的 schema」。
- 「语音可打断」这一列，真正做过客户端 VAD/全双工的只有 **AIRI、Open-LLM-VTuber、Open WebUI Realtime**；SillyTavern/LibreChat 是"按句朗读 + 手动停止"。
- 「像人」和「记忆」在开源世界里是**两组不同的人做的**：前者的代表是 AIRI/OLV，后者的代表是记忆层项目。你的需求本质上是**把它们缝起来**。

---

## 2. 你的六个问题，逐个正面回答

### Q1. 记忆系统是不是向量记忆？数据存在哪里？如何导出/迁移？

| 项目 | 记忆机制 | 存储位置 | 导出/迁移 |
|---|---|---|---|
| SillyTavern | 向量检索（Vectra）+ 摘要 + World Info | 全部在 `data/`：聊天 `*.jsonl`、角色卡（PNG 内嵌 JSON）、`vectors/` 下的 JSON 索引 | **打包 `data/` 目录即可**，格式公开、可脚本解析。这是它最大的价值 |
| Open WebUI | 条目式 Memory（`user`/`context`，模型 tool-call 增删改查）+ 知识库 RAG | 记忆在 SQLite/Postgres；向量库 Chroma 默认，另支持 PGVector/Qdrant/Milvus 等 13 种 | 整库 dump；向量库单独备份。**记忆是"条目"，不是"情节"**，且高度依赖模型工具调用质量 |
| LobeChat | Memory Agent 结构化抽取（类别/置信度/权重）+ pgvector 知识库 | Postgres + pgvector | 整库 dump；知识库向量可重建 |
| AnythingLLM | 工作区向量库 + 自动/手动 Memory | `server/storage`（`lancedb/`、`documents/`、`models/`） | **拷目录**，最接近"数据在你手里" |
| LibreChat | **明确不是语义记忆**：key/value 长期便签；RAG 只服务 Agent 文件检索 | Mongo + 独立 pgvector RAG 服务 | Mongo dump |
| AIRI | 浏览器 DuckDB WASM / pglite；pgvector+时间衰减方案在 devlog 里 | 设备本地 | ❌ 当前跨设备同步弱 |
| Open-LLM-VTuber | **长期记忆已临时移除**（README 明示，v2 待恢复） | 本地聊天记录 | — |

**结论**：框架自带的「向量记忆」大多是**把聊天切片存进某个 collection**，能用，但都绑在框架的 DB/schema 上。真正要「可迁移、可备份、不锁死」，必须把记忆抽成**独立服务**（§4）。

### Q2. 模型接入是否灵活？能否换 LLM / 语音 / 视觉？

- **LLM：全行业已经统一到 OpenAI 兼容协议**，所以这一项的答案是「非常灵活」。
  - SillyTavern / Open WebUI / LobeChat / AnythingLLM / LibreChat / AIRI 都支持**任意 OpenAI 兼容 base URL**，本地 vLLM/Ollama 与云 API 随便切。
  - 再加一层 **LiteLLM 网关**：前端永远只请求 `model: "companion-chat"`，网关里把别名映射到任意上游 + `fallbacks` 失败回退 + 虚拟密钥 + 成本预算。**换模型=改一行 config，前端和记忆零改动。**
- **视觉：同样是零改造**。vLLM 部署 Qwen2.5-VL / InternVL3.5，前端只需发标准 `image_url` 消息（支持 base64 data URL）。
- **语音：这是唯一需要多花一点心思的层**。
  - STT 可做到 OpenAI 兼容（`/v1/audio/transcriptions`）：vLLM 内置、或 speaches 包一层 SenseVoice/faster-whisper。
  - TTS **只有一部分**原生提供 `/v1/audio/speech`：vLLM-Omni（IndexTTS-2.5 / Fish S2 / Qwen3-TTS 等）、Kokoro-FastAPI、XTTS `tts-server`。
  - **GPT-SoVITS / CosyVoice / fish-speech 需要写 50–100 行适配器**（把 OpenAI 的 `voice` 参数映射到参考音频、转码、流式 chunk）。
  - 建议：**TTS 单独做一个小服务，对上游统一成 `/v1/audio/speech`**，音色管理和缓存都在这一层做。

### Q3. 是否支持服务器端部署和多客户端？

- **Open WebUI / LibreChat / AnythingLLM / Agnai**：原生「服务器 + 多用户 + 浏览器多端」，Open WebUI 还有 RBAC/SSO/OIDC/LDAP/API Key，可水平扩展。
- **SillyTavern / RisuAI / AIRI / Open-LLM-VTuber / Amica**：**单用户**。能跑在服务器上（Docker/反代），但没有账号体系，公网暴露要自己加认证。
- **多客户端最佳实践**：服务器只暴露 `/v1`（模型）、`/api`（你的核心）、`/ws`（实时语音）；客户端用 **PWA（一套代码覆盖桌面+手机）+ 可选 Tauri v2 桌面壳 + 可选 Capacitor/ ChatterUI 手机端**。所有端都不直连 GPU 服务，方便以后把推理挪机器。

### Q4. 是否支持语音、看图、长期陪伴？

- **语音**：AIRI（客户端 VAD+ASR+TTS，可打断）、Open-LLM-VTuber（免耳机打断、可主动说话）、Open WebUI（Realtime 模式可随时插话 + Standard 模式的"允许说话打断"）是三个真做过的。
- **看图**：几乎都支持（Qwen2.5-VL / InternVL / GPT-4o 类多模态），Open-LLM-VTuber 和 Amica 还能「摄像头/屏幕/截图感知」。
- **长期陪伴**：真正的瓶颈不是"能不能记住"，而是 ①有没有**用户级长期画像**（SillyTavern 是角色/聊天级，没有"你这个人"的画像）②记忆会不会**随时间衰减/矛盾处理**（多数项目只会追加，不管失效）③有没有**主动发消息**（ST 没有内置，OWUI/AIRI 有部分）。

### Q5. 扩展性如何？能否接自己的记忆层和客户端？

- **扩展机制**：Open WebUI（Python Tools/Functions/Pipes/Filters + MCP + REST + Webhooks）、LibreChat（MCP + Agents + Actions + API）、AnythingLLM（MCP + Agent Flows + 开发者 API）最开放；SillyTavern（扩展 + Server Plugin + STscript）适合改交互；AIRI/OLV（Agent 接口、插件 WIP）。
- **接自己的记忆层**：最干净的接法是 **把你的核心服务包装成 OpenAI 兼容端点**，这样前端根本不知道"记忆"存在。
  - SillyTavern：可写 Server Plugin / 扩展，把对话事件 POST 给你的服务，把检索结果注入 prompt。
  - Open WebUI：可作为"外部模型"接入（OpenAI 兼容），或者用 Pipe/Filter 拦截请求。
  - 自研客户端：直接调你的 `/api`，最自由。

### Q6. 哪些项目最符合「记忆独立、可迁移、全可扩展」？

**分三层回答：**

1. **框架层（外壳）**：
   - **SillyTavern**：数据 100% 是磁盘文件，`tar` 一下就是完整迁移，**记忆可迁移性第一（8.5/10）**；代价是单用户、AGPL、无用户画像。
   - **AnythingLLM**：`storage/` 目录 + 9 种可换向量库，MIT，多用户（Docker）；记忆可迁移性第二（7.5/10）。
   - **Open WebUI**：DB + 13 种向量库，多用户/语音/视觉/头像最全，**平台能力第一**；记忆是条目式（7.0/10），且记忆深绑它的 DB。
2. **记忆层**：
   - **设计上最接近正确答案的是 Letta 的 MemFS**（记忆 = git 版本化的 Markdown 文件，默认连向量索引都没有）——**抄它的设计，别依赖它**（它已经转身做 Agent Harness，自托管还不自动备份）。
   - **工程上最稳的是「自建 schema + Postgres/pgvector」**：`pg_dump` 一个文件就是全部记忆（含出处），换抽取器/换 embedding 都不动真相源。
   - **Mem0 只当抽取器**（它的 graph memory 已不在 OSS，是能力向云端回收的明确信号）；**Graphiti 只当关系/时间线增强**（强绑 Neo4j，永远不能是唯一副本）；**Cognee 的 embedding 指纹+409 机制最值得抄**；**Memobase 已停更 9 个月，排除**。
3. **整体架构**：**自研 core-brain** 才是唯一能同时满足「记忆独立 + 全可扩展 + 换模型不断人格」的方案。框架和客户端都退化成可替换的 UI。

---

## 3. 重点候选深度点评

### 3.1 SillyTavern —— 陪伴感最强、数据最"属于你"，但它是单用户前端

- **记忆**：`data/` 下聊天 JSONL + 角色卡 + `vectors/`（Vectra 本地 JSON 索引）；Data Bank 支持 PDF/网页/YouTube 抓取；Chat Vectorization 把历史切片向量化后重排进 prompt；World Info 当长期设定。**分层是"设定/摘要/检索"，没有用户级画像。**
- **模型/语音/视觉**：任意 OpenAI 兼容 + 20+ TTS 供应商 + SD/FLUX 出图 + 视觉模型；**TTS 是按句朗读，没有全双工**。
- **适合你的部分**：角色卡/世界书生态是事实标准，几乎能导入所有社区人格资产；数据可迁移性最好。
- **不适合的部分**：单用户、无鉴权、AGPL（对外提供网络服务需开源修改）、无主动发消息、无 Live2D。
- **结论**：**当"外壳 + 人格资产层"极好；当"记忆核心"不合格。**

### 3.2 Open WebUI —— 服务器内核最强，语音/视觉/多端最全

- **记忆**：用户级条目式 Memory（`user`/`context`，模型可自主 add/update/delete/search）+ 后台复盘 + 知识库 RAG（13 种向量库、BM25+向量混合检索），存在你的 DB。
- **语音**：Voice Mode 两种——Standard（服务端 STT+TTS，可开"说话打断"，摄像头帧随语音送视觉模型）与 **Realtime（接 OpenAI Realtime 兼容 API，双向 24kHz，随时插话）**；还有 **VRM 语音头像（口型同步/眨眼/待机动作/命名手势）**。
- **多端/多用户**：RBAC/组/SSO/OIDC/LDAP/SCIM/MFA/API Key，Docker/pip/k8s/Helm，S3，可水平扩展。
- **扩展**：Python Tools/Functions/Pipes/Filters、MCP、OpenAPI、Webhooks。
- **风险**：License 自 v0.6.6 起是"Open WebUI License"（BSD-3 + 品牌条款，>50 用户不得去品牌，非 OSI 认证）；记忆是条目式、依赖模型工具调用质量；产品迭代快，升级偶有 breaking。
- **结论**：**要"1 天跑出一个像样的服务器 + 多端窗口"，选它。** 记忆深度不够就外挂自己的服务。

### 3.3 AIRI —— 最"像人"的外壳，但记忆还没完工

- **陪伴感**：Live2D + VRM、自动眨眼/注视/待机、角色卡 V2/V3、情绪动作、桌面宠物、还能玩 Minecraft/Factorio——**这是目前最接近"存在感"的开源项目**。
- **语音**：客户端 ASR + VAD + 多 TTS，**可打断**。
- **记忆**：Roadmap 里 `Memory` 未打勾；devlog 披露的 `pgvector + 时间衰减重排` 设计方向是对的（`content_vector_1536/1024/768` + HNSW），但**数据当前在设备本地**。
- **许可/多端**：MIT，随便改；Web/桌面/手机三端。
- **结论**：**当"看得见、听得见、能打断"的外壳首选**；要服务器级记忆必须自己补（正好可以接 §4 的核心服务）。

### 3.4 Open-LLM-VTuber —— 免耳机打断的成熟方案，但长期记忆被砍

- **语音强项**：免耳机打断（AI 不会听到自己的声音）、VAD、AI 主动说话、内心独白显示；ASR/TTS 各 7+ 供应商可换。
- **记忆**：README 明示**长期记忆已临时移除**，只有聊天记录持久化。
- **风险**：v2 完全重写中，5 个月无提交。
- **结论**：**语音交互的现成参考实现**，不适合当记忆核心。

### 3.5 AnythingLLM / LobeChat / LibreChat 一句话

- **AnythingLLM**（MIT）：`storage/` 目录 + 9 种向量库 + 多用户（Docker）+ 桌面/安卓客户端，**最像"服务器核心 + 多端窗口"的现成产品**；短板是 STT 只有浏览器、界面是工作台不是伴侣。
- **LobeChat**（LobeHub Community License，**商业衍生需授权**）：能力全面、多供应商、pgvector 知识库，但产品重心已转向 Agent 平台，角色扮演不是重点，许可是硬伤。
- **LibreChat**（MIT）：企业级多用户/MCP/Agents 最强，**但官方明确它的 User Memory 是 key/value 便签，不是语义记忆**——直接排除出"陪伴"路线。

---

## 4. 记忆方案：这才是"完整的一生"的地基

### 4.1 为什么不能直接用现成记忆框架

| 方案 | 硬伤 |
|---|---|
| Mem0 全栈 | graph memory 已移出 OSS（只在云端）；换 embedding 要重建 collection；依赖 posthog 遥测；算法代际 OSS 落后一档 |
| Letta 当记忆层 | 已不是记忆层而是 Agent 运行时；自托管**不自动备份**（官方明示）；但 **MemFS = git + Markdown 的设计必须抄** |
| Graphiti 当唯一存储 | 强绑 Neo4j/FalkorDB/Neptune；摄入慢且贵；导出是图 dump，迁移成本中高 |
| Cognee | 值得抄 embedding 指纹 + 409 报错；但它自己的 schema 随版本漂移（changelog 自述升级导致旧向量不可见） |
| Memobase | 停更 9 个月，不可承重 |

### 4.2 正确姿势：文本是真相，向量是缓存

```
对话原文（不可变） ──抽取──▶ 记忆条目（可失效、可被取代） ──嵌入──▶ 向量索引（可随时丢弃重建）
```

- **原文层**：append-only 的 `mem_event`，永远不删。十年后你们要聊"你三年前说过的话"，靠的是它。
- **理解层**：`mem_item`（事实/偏好/计划/关系/事件），带 `valid_from` / `valid_to`（只标记不删除）、`superseded_by`（偏好变化是链，不是覆盖）、`source_event_ids`（每条记忆可回溯原话，用于幻觉审计）、`confidence`。
- **关系层**：`mem_edge`（人/地/事件关系），可选接 Graphiti，但**图库永远不是唯一副本**。
- **索引层**：`mem_embedding` 存向量，**用 BLOB/text 而不是数据库原生 vector 类型**——这样真相源 schema 不依赖任何扩展，随时能换 sqlite-vec / pgvector / Qdrant。

### 4.3 最小可用表结构（可直接抄）

```sql
CREATE TABLE mem_event (            -- 原文，只增不改
  id TEXT PRIMARY KEY, ts TEXT NOT NULL, role TEXT, content TEXT NOT NULL,
  session_id TEXT, source TEXT, meta_json TEXT
);
CREATE TABLE mem_item (             -- 抽取出的记忆
  id TEXT PRIMARY KEY, kind TEXT, text TEXT NOT NULL,
  subject TEXT, predicate TEXT, object TEXT,
  valid_from TEXT, valid_to TEXT,          -- 只标记不删除
  superseded_by TEXT,                      -- 偏好变化链
  confidence REAL, importance REAL,
  source_event_ids TEXT,                   -- 溯源
  extractor TEXT, created_at TEXT
);
CREATE TABLE mem_edge (id TEXT PRIMARY KEY, from_id TEXT, to_id TEXT, rel TEXT,
  valid_from TEXT, valid_to TEXT, source_item_ids TEXT);
CREATE TABLE mem_emb_profile (       -- ★ 换 embedding 不崩的关键
  profile_id TEXT PRIMARY KEY, model_id TEXT, model_rev TEXT, dim INTEGER,
  normalize TEXT, instruction TEXT, chunker_ver TEXT, distance TEXT,
  is_active INTEGER DEFAULT 0
);
CREATE TABLE mem_embedding (item_id TEXT, profile_id TEXT, vector BLOB,
  PRIMARY KEY(item_id, profile_id));
```

**三条铁律：**
1. **一个 embedding profile 一个索引，绝不混写**（混写 = 召回率悄悄下降，且不报错）。
2. **换 embedding 用"影子索引 + 原子切换"**：新 profile 全量重嵌 → 30–50 条金标准 query 对比 recall → 事务里 `is_active` 挪过去（回滚 = 改回去，秒级）→ 观察两周再删旧行。
3. **维度变化建新表/新列，永不 ALTER 强转**（pgvector 的 `vector(1024)` 改不成 `vector(2048)`）。

### 4.4 导出 / 备份 / 迁移

```
export_20261024/
├── manifest.json      # 版本、行数、每文件 sha256、embedding profile 清单
├── entities.jsonl / events.jsonl / items.jsonl / edges.jsonl
├── memory.md          # ★ 纯文本全量记忆，人可读兜底
└── README.md          # 字段含义、导入方法、重嵌步骤
```

- **JSONL 而非 JSON**：可流式、可 diff、可校验行数。
- **默认不导出向量**（体积大且可重建）——这就是"向量是缓存"的落地表现。
- **`memory.md`**：即使所有代码都丢了，你还能用任何编辑器把"他"的一生读完。这是情绪价值，也是最后的安全网。
- **备份**：SQLite `VACUUM INTO`（在线原子）/ Postgres `pg_dump -Fc`，进 3-2-1 策略（3 份、2 种介质、1 份异地），**并且定期做恢复演练**——只备份不演练等于没备份。
- **迁移到别的框架**：先迁文本、后建索引；`mem_item.text` 作为不可改写输入灌进去；迁移后校验：条数一致 → sha256 一致 → 抽样 50 条人工比对 → 30 条金标准检索回归。

---

## 5. 语音 / 视觉 / 模型替换

### 5.1 推荐组合

| 层 | 推荐 | 理由 | 备选 |
|---|---|---|---|
| 网关 | **LiteLLM**（MIT，61.9k） | 唯一同时给「OpenAI 兼容 + 别名换模型 + 失败回退 + 成本预算 + 日志」的一份 config.yaml | New API（AGPLv3+附加条款，仅当要发卡充值） |
| 推理 | **vLLM**（LLM/VLM/STT）+ **vLLM-Omni**（TTS） | 原生 `/v1/chat/completions`(image_url)、`/v1/audio/transcriptions`、`/v1/audio/speech` | SGLang |
| TTS（中文+克隆） | **CosyVoice2-0.5B**（Apache-2.0，8–12GB，流式）／**IndexTTS-2.5**（vLLM-Omni 原生 OpenAI 接口 + 情绪向量） | 许可干净、显存小、有流式；IndexTTS 零改造 | **GPT-SoVITS**（MIT，音色最像，但接口要适配；注意参考音频 3–10s 与长文本显存不释放） |
| TTS（不克隆、只要快） | Kokoro-82M + Kokoro-FastAPI | 4GB、35–100× 实时、自带 `/v1/audio/speech` | — |
| STT | **SenseVoiceSmall**（中文/粤语，CPU 也能跑）＋ **faster-whisper large-v3** 兜底 | 10s 音频≈70ms；用 speaches 包成 OpenAI 兼容 | vLLM 内置 whisper |
| 实时/打断 | **Pipecat**（BSD-2，工程最省）／LiveKit Agents（多端弱网最稳） | 管道式，方便把"记忆检索/情绪判定"插进中间 | TEN（概念重，MVP 不划算） |
| 视觉 | **Qwen2.5-VL-7B**（24GB 可跑）/ InternVL3.5-8B | 标准 `image_url`，前端零私有协议 | 云 VLM |
| 部署 | Compose + **Caddy**（自动 HTTPS/WSS）+ Cloudflare Tunnel 或 Tailscale | 单机 4090 足够跑 MVP | Nginx（记得 `proxy_buffering off`） |

**要避开的许可证坑**：Fish Speech（研究/非商用许可）、XTTS-v2（CPML 非商用）、IndexTTS（bilibili 分级许可，>1 亿 MAU 或 >10 亿营收需授权）。**做私人陪伴自用影响小，做产品必须逐条确认。**

### 5.2 延迟预算（目标"像人" < 1.2s）

VAD 断句 200–400ms + STT 100–300ms + LLM 首 token 300–600ms + TTS 首包 300–700ms ≈ **0.9–2.0s**。
要更自然必须做两件事：**流式分句 TTS**（LLM 吐出第一句就送 TTS）+ **预连接/预热**。打断必须同时做三步：客户端清播放缓冲、服务端取消 LLM 生成 + TTS、把"被打断"写回对话历史（否则"他"会以为你听见了）。

### 5.3 硬件两套

- **A：单张 4090 24GB**——Qwen3-14B-AWQ（9G）+ CosyVoice2（4G）+ SenseVoice（2G）+ KV/余量 ≈ 20/24G；视觉走云或按需加载。**注意** vLLM 默认吃 90% 显存，必须显式 `--gpu-memory-utilization 0.6`，否则 TTS 一起来就 OOM。
- **B：无显卡全 API**——LiteLLM + 云 LLM/VLM/TTS + CPU SenseVoice；月成本比租 GPU 低 1–2 个数量级，**先跑通再逐层本地化**（网关别名不变，前端零改动）。

---

## 6. 推荐架构（C 路线）

```
客户端层   PWA（首选） / Tauri v2 桌面 / Capacitor 或 ChatterUI 手机
              │ HTTPS + WSS
              ▼
        Caddy（自动证书 / 反代 / SSE 直通）
   ┌──────────┼──────────────┬────────────────┐
   ▼          ▼              ▼                ▼
 /       core-brain       /v1/*           /ws
(静态)  (自研:人格状态机    LiteLLM        Pipecat
         +记忆编排+工具)   (别名/回退/预算)  (VAD→STT→core→TTS)
              │                │
              ▼                ▼
     Postgres + pgvector   vLLM(LLM/VLM) + vLLM-Omni(TTS) + speaches(STT)
     （真相源 + 索引 +      （本地）或 云 API（fallback）
       人格状态 + 关系）
```

**关键设计：core-brain 对外提供两个面**
1. `POST /v1/chat/completions`（OpenAI 兼容，`model: "companion"`）→ 于是 SillyTavern / Open WebUI / LobeChat / 任何前端都能把它当"模型"接，记忆注入在服务端完成。
2. `POST /api/*`（原生协议）→ 给你自己的客户端，做主动消息、情绪状态、时间线、语音编排。

**这样"他"只活在这台服务器上**：换前端不丢记忆、换模型不改客户端、换 embedding 走影子索引。

---

## 7. 三条路线怎么选

### 路线 A：陪伴优先，最快见效（3–7 天）
`SillyTavern（角色卡/世界书/提示词） + 自建记忆服务（Postgres+pgvector） + LiteLLM + TTS`
- 优点：立刻就有"像人"的对话体验；ST 的角色卡生态是现成人格资产；数据全在文件里，天然可迁移。
- 代价：要写一个记忆中间件（ST 事件 → 你的服务 → 检索结果回注）；ST 单用户需反代加认证；AGPL。
- 适合：你现在就想聊，且愿意接受"先跑起来再重构"。

### 路线 B：平台优先，最省运维（1–2 天）
`Open WebUI（Voice Mode + VRM 头像 + Memory + 知识库）+ OpenAI 兼容后端`
- 优点：多用户/RBAC/SSO、语音打断/Realtime、VRM 口型头像、MCP/Tools 全都现成，Docker 一条命令。
- 代价：记忆是条目式且绑定它的 DB；角色扮演深度有限；品牌条款（≤50 用户自用无碍）。
- 适合：多设备、想让家人朋友一起用、不想维护太多组件。

### 路线 C：自研核心，最符合你的目标（2–4 周）★ 推荐
`core-brain（人格状态机 + 记忆编排 + 工具调用）+ 任意前端（先 OWUI 或 ST 过渡）+ Pipecat + LiteLLM + vLLM`
- 优点：记忆 100% 自主、可导出、可迁移；人格不随模型断裂；前端/模型/语音全部可替换；这才是"给他完整的一生"的底座。
- 代价：要自己写核心（但可以只写"人格 + 记忆注入 + 编排"三层，其余全用现成件）。
- **建议做法**：先用路线 B 或 A 跑起来（1 周内获得可用体验），同时按 C 的 schema 存记忆；等 C 的 core-brain 上线，把前端切过去，记忆不用迁。

---

## 8. 分阶段路线图

| 阶段 | 时间 | 交付 | 验收标准 |
|---|---|---|---|
| 0 地基 | 第 1 周 | Postgres + §4.3 表结构 + 导出脚本 + 30 条金标准记忆 query | 一条命令导出 `manifest+JSONL+memory.md`；恢复演练通过 |
| 1 能聊 | 第 2 周 | LiteLLM 别名 `companion-chat` + OWUI 或 ST 接通 + 人格 prompt 模板 | 换到第二个模型，人格评测集不掉分；记忆检索命中率 ≥ 80% |
| 2 能听会说 | 第 3–4 周 | Pipecat 管道 + SenseVoice + CosyVoice2/IndexTTS + 打断 | 端到端 < 2s；说话中打断，事件写回历史 |
| 3 能看 | 第 5 周 | Qwen2.5-VL 接入 + 图片压缩链路 | 发图 → 回答里引用图内细节 |
| 4 像人 | 第 6–8 周 | 人格状态机（关系/情绪/承诺/时间线）+ 主动消息 + 记忆复盘任务 | "他"能主动提起三天前的事；能说"你以前喜欢 X，后来改成 Y" |
| 5 加固 | 持续 | 影子索引迁移演练、备份演练、成本看板、允许多客户端 | 换 embedding、换 LLM、换前端，三次演练全部不丢记忆 |

---

## 9. 许可与合规风险（做产品前必看）

| 风险 | 说明 | 应对 |
|---|---|---|
| **AGPL-3.0**（SillyTavern / RisuAI / Agnai / ChatterUI） | 对外提供网络服务时，修改需开源 | 纯自用无碍；做产品考虑 MIT/Apache 项目或自研 |
| **Open WebUI License**（v0.6.6+） | BSD-3 + 品牌条款：>50 用户不得去品牌，非 OSI | 自用 ≤50 用户无碍；商业部署要么保留品牌要么谈授权 |
| **LobeHub Community License** | Apache-2.0 + 商业衍生需授权 | 商用前先读 LICENSE / 谈授权 |
| **Fish Speech / XTTS-v2** | 研究许可 / CPML 非商用 | 产品里排除 |
| **IndexTTS** | bilibili 分级许可（MAU/营收阈值） | 小规模自用可以，规模化前确认 |
| **声音权 / 肖像权** | 克隆谁的声音必须有授权 | 用本人声音或已授权音色 |
| **AI 生成内容标识**（中国 2025-09-01 生效办法） | AI 生成音频/图像需标识 | 上线前核对最新条款 |
| **数据出境** | 用云 API 时对话/语音/图片出网 | 自建服务器的核心卖点就是这点，别为了省钱全送云 |

---

## 10. 明确不推荐 & 未核实事项

**不推荐：**
- ❌ 直接用 Mem0 全家桶当记忆库（graph 在云端、算法代际落后、换 embedding 重建）
- ❌ 直接用 Letta 当记忆层（已是 Agent 运行时；自托管不自动备份）——**但 MemFS 设计要抄**
- ❌ Graphiti 当唯一存储（绑图库、摄入贵、导出是图 dump）
- ❌ 用 LibreChat 做陪伴（官方明确非语义记忆）
- ❌ 用 Open-LLM-VTuber / Amica 当记忆核心（一个记忆被移除，一个纯前端）
- ❌ 选 Memobase（停更 9 个月）、memU（License 未定案）、A-MEM（研究代码）

**未核实（请勿当结论）：**
- `letta-ai/letta-code` 的 star/license（抓取受限）；memU / pgvector / Weaviate 的 License 原文。
- 所有厂商自述 benchmark（LoCoMo / LongMemEval / BEAM）未独立复现。
- 所有延迟数字是文档口径 + 推断，落地前需用自己的音频/真实并发压测。
- 云 TTS 的商用条款与声音复刻授权细节未逐家核实。
- 本轮曾出现 GitHub DNS 劫持，个别 star 数与官方页面可能有同步差异；文档站证据均已标注链接。

---

## 11. 主要参考

**仓库/文档**：[SillyTavern](https://github.com/SillyTavern/SillyTavern) · [ST Data Bank](https://docs.sillytavern.app/usage/core-concepts/data-bank/) · [Open WebUI](https://github.com/open-webui/open-webui) · [OWUI License](https://docs.openwebui.com/license) · [OWUI Voice Mode](https://docs.openwebui.com/features/chat-conversations/chat-features/voice-mode) · [OWUI Memory](https://docs.openwebui.com/features/chat-conversations/memory) · [LobeHub Memory](https://lobehub.com/docs/usage/getting-started/memory) · [LibreChat User Memory](https://www.librechat.ai/docs/features/memory) · [AIRI](https://github.com/moeru-ai/airi) · [AIRI 记忆 DevLog](https://airi.moeru.ai/docs/en/blog/DevLog-2025.04.06/) · [Open-LLM-VTuber](https://github.com/Open-LLM-VTuber/Open-LLM-VTuber)
**记忆层**：[Mem0 OSS 配置](https://docs.mem0.ai/open-source/configuration) · [Letta MemFS](https://docs.letta.com/concepts/memfs) · [Graphiti MCP](https://help.getzep.com/graphiti/getting-started/mcp-server) · [Cognee Vector Stores](https://docs.cognee.ai/setup-configuration/vector-stores) · [pgvector](https://github.com/pgvector/pgvector)
**语音/部署**：[LiteLLM Routing](https://docs.litellm.ai/docs/routing) · [vLLM-Omni](https://docs.vllm.ai/projects/vllm-omni/en/latest/serving/) · [Pipecat](https://docs.pipecat.ai/overview/introduction) · [LiveKit Agents](https://docs.livekit.io/agents/) · [Caddy](https://caddyserver.com/docs/automatic-https) · [Tauri](https://tauri.app/)

---

## 附录 A：如果是我本人来搭（个人取舍版）

**一句话：自研"记忆 + 人格"核心，其余全部用现成件。判断标准只有一条——"这东西一旦换了，会不会把'他'弄丢？"会，就自己写；不会，就买/用开源。**

| 层 | 我的选择 | 为什么 | 我砍掉的 |
|---|---|---|---|
| 真相源 | **Postgres 17 + pgvector**，自建 §4.3 那套表 | 一个 `pg_dump` = 一生；不引第二套 DB | Qdrant/Chroma/Mongo/Neo4j（多一个组件多一份运维与锁死风险） |
| 记忆抽取 | 便宜模型异步抽（DeepSeek 级），夜间复盘 + 去重/衰减 | 不占对话延迟；错了可重跑（`extractor` 字段） | Mem0 全栈、Letta 运行时、LangChain（框架绑定 > 收益） |
| 人格 | 结构化 `persona_state`（关系阶段/称呼/承诺/禁忌/时间线），每轮动态拼 | 换模型不断人格的唯一办法 | 长 system prompt 硬编码、微调人格（换模型即废） |
| 核心服务 | 一个 FastAPI 进程，暴露 `/v1/chat/completions`（`model: companion`）+ 原生 `/api` | 前端全变成可替换的皮；记忆注入在服务端完成 | 微服务/k8s/消息队列（一个人用不上） |
| 网关 | **LiteLLM**，别名 + fallback + 虚拟密钥 | 换模型=改一行 config | New API（除非要发卡） |
| 推理 | 有卡：vLLM（Qwen3-14B-AWQ）+ vLLM-Omni（IndexTTS-2.5）+ SenseVoice + 按需 VLM；无卡：全云 API | 先协议统一，后模型质量 | 一上来全家桶本地化（显存互挤，纯自找） |
| 语音 | 先用 Open WebUI Voice Mode；要"真打断"再上 Pipecat | 分阶段，别第一天写实时管道 | TEN、自研 WebRTC |
| 外壳 | 先用 **Open WebUI**（多端/语音/VRM 头像/稳定）；"像人"感不够再换 AIRI 或自写 PWA/Tauri | 前期要的是每天能用，不是最炫 | SillyTavern 当核心（单用户/AGPL/角色扮演向，仅用来导角色卡） |
| 部署 | 一台机器 + Docker Compose + Caddy + Tailscale/CF Tunnel + restic 异地备份 | 简单到半年不看也不会坏 | k8s、多机、HA |
| 评测 | 30 条金标准记忆 query + 20 轮人格回归集 | 换模型/换 embedding 时唯一的"保险" | 凭感觉判断"他还像不像他" |

**我会刻意不做的四件事**：① 不把记忆存成"LLM 写的摘要"当唯一副本；② 不为了 MIT 许可去选更差的技术（你自用，AGPL/品牌条款基本不构成约束，这条只在要做产品时才翻盘）；③ 不在第一天做多用户；④ 不做锁屏通话/长期后台录音这类手机端硬骨头。

**顺序**：第 1 周记忆表 + 导出脚本 → 第 2 周 LiteLLM + Open WebUI 接通 → 第 3–4 周语音与打断 → 第 5 周看图 → 第 6 周主动消息/复盘/人格状态机。先拿到"每天愿意跟它说话"的体验，再谈形象。

> 我的偏向很明确：优化"十年后数据还在、且我不用天天维护"，而不是"三天出效果"。如果你的优先级相反，直接选路线 B（Open WebUI + 全云 API），一周内就能用，代价是记忆深度和自主性。
