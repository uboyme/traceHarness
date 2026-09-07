# TraceHarness v0.9 冻结阶段计划

> 状态：**2026-08-29 冻结产品路线，2026-09-05 纳入 M3 History Evidence；2026-09-07 按 v0.8.0
> 发布基线修订身份、失败前缀、索引、评测与阶段合同；同日完成获授权的 F0-A ADR／设计冻结。
> F0-A/B/C 本轮授权实现与最终限定门禁已收口；F1–F5 未开工，未执行发布级全量或 L2。**
>
> 前置：v0.8.0 已完成发布，源码版本、发布 tag 与记录已核对。F0-A 仅交付设计文档；19 项现有接缝
> 定向测试通过，不代表新协议实现。F0-B 的 59 项新主线已包含在最终 36 文件门禁中：
> `1079 collected / 1076 passed / 3 skipped in 20.22s`；三项跳过均为 Windows 权限／NUL 路径边界。
> F0-C 最终 38 文件 `1104 collected / 1100 passed / 4 skipped in 31.98s`，包含新 History 四文件 81 项。
> 四项 skip 均为 Windows 符号链接权限／NUL 路径边界。本轮未运行全量、L2、构建、联网或真实模型。
>
> 本文冻结 Skill、Memory、M3 压缩历史证据、渐进式披露与 RAG 的单一 Context Input 主线，区分
> 已发布基线、计划合同与尚待验证的实现；版本目标和三个 Release Stop 保持不变。
> 它不表示当前已有这些能力，也不授权 commit、push、tag、release、联网或真实模型运行。

F0-A 的决定与字段合同已冻结为 [ADR-0043](../adr/0043-step-scoped-context-input-and-retrieval.md)、
[ADR-0044](../adr/0044-host-owned-project-scope-and-memory-authority.md) 和
[F0 设计合同](TRACEHARNESS_V0.9_F0_DESIGN_CONTRACT.md)。本计划维护阶段与验收依赖，详细字段以该合同
为唯一规范；F0 整体仍须 F0-B/C 真实主线验证通过。

## 1. 单一产品目标

v0.9 的统一目标是：**让宿主持有的长期上下文，通过渐进式披露和可审计检索，安全、准确、可重放地
进入每次 Model Request。**

这条主线新增两类 canonical 内容，并复用一类已经存在的 canonical 证据：

- **Skill**：已启用 trusted plugin 贡献的、可选择的工作方法与只读参考资源；
- **Workspace Memory**：由宿主批准、对当前 Workspace 长期有效的事实；
- **History Evidence**：M3 从未删除的当前 Session 原始 Surface 事件，以及 format-2
  `surface/replace` 已绑定的来源链。它不是新 Memory 或第三事实源，只回答“当时模型看见过什么”。

三类输入共用“资格过滤 → 检索/精确引用 → 渐进披露 → 上下文预算 → request snapshot”基础设施，但不
共用 authority：Plugin/Generation 决定 Skill catalog，Memory stream 决定哪些 Memory active，Session
EventStore 与 M3 replacement provenance 决定可展开的 History block。检索和展开只读取已有事实，永远不
创造事实、不启用插件、不批准 Memory、不给 Tool 或 Promotion 授权。历史证据只证明过去，不证明当前
Workspace、ProductTask 或测试状态。

建议阶段顺序：

```text
Context Input 合同
  + History Evidence identity / 时效 / Step-scope 合同
  -> typed Skill contribution
  -> Skill 检索与渐进披露
  -> Workspace Memory authority
  -> Memory 检索、历史证据按需披露与统一上下文编排
  -> 治理 UI、冻结评测与发布
```

Skill 与 Memory 保留独立审查停止点。History Evidence 没有新的 active 状态机，只是已有 Session 事件的
只读、可验证投影。三者可以属于同一个 v0.9 产品主题，但不能为了同一个版本号把不同 authority 揉成一个
万能 `context` blob，也不能用 feature flag 长期保留半套双合同。

## 2. 冻结修订结论

### 2.1 渐进式披露与 RAG 都属于 v0.9

上一稿把 embeddings、RAG 和 semantic reranker 全部延期，只做 pin/tag。修订后：

- 渐进式披露是 Skill 与 Memory 的共同必要合同，不是 UI 优化；
- SQLite FTS/BM25 作为首个通用 lexical retrieval lane 纳入 v0.9；
- exact/symbol lane 与 FTS 是基础能力；
- 本地 embedding 和本地 reranker 是 v0.9 内的**可选后续 lane**，只有显式配置、可复核 receipt、
  无网络依赖和独立质量门禁后才启用；
- 不引入外部向量数据库、RAG 服务或云端 embedding API。个人本地产品没有必要为它们增加运维面。

### 2.2 RAG 不是事实源或权限源

RAG/FTS/vector/reranker 的输出都是 derived candidates。权威答案始终是：

- Skill：当前 leased Generation 的 immutable Skill catalog；
- Memory：同一个 EventStore 中 fresh replay 得到的 approved active Memory；
- 最终注入：本次 Step 的 `ContextInputSnapshot` 所记录的有序精确 bytes。

索引可删除重建，排名可因明确的算法版本变化而变化；历史 Request 不得重跑检索、读取最新 Memory、
重新加载当前 Wheel 或用新的 reranker 改写过去。

### 2.3 Skill 选择不等于 Plugin reload

上一稿曾建议 selection 变化复用 Generation publish/Drain 和 Session plugin migration，这会把两个不同
生命周期绑在一起，现明确否决：

- plugin enable/reload/disable 改变可用 catalog，继续由 Activation/Generation/Lease/Drain owner 管；
- 用户选择或一次 Step 的 retrieval 只改变**未来请求的上下文输入**；不 publish Generation、不 drain
  Activation、不触发 plugin migration；
- 正在运行的 Step 使用 admission 时冻结的 Lease 与 ContextInputSnapshot，不受随后选择变化影响；
- selection 事实必须 durable 且可审计，但不能变成第二 Plugin Identity。

### 2.4 Skill 选择不授予 Tool

v0.9 第一版 Skill contribution 只包含 metadata、prompt/reference sections 和受限只读资源。选择 Skill
不会安装/启用插件，也不会让插件另行贡献的 Tool、Policy、Provider 或 Verifier 生效。插件能力仍只能
在现有 Plugin Activation/Composition 主线显式装配；Skill 文本无论写什么都不是 Tool grant。

Skill contribution 也不得复用现有 `PluginContext.register_prompt`：那个入口属于 Composition system prompt
装配，一旦注册就会让所有请求无条件看到正文，绕过 selection、retrieval、渐进披露和 Context Budget。
Skill 使用现有 Activation transaction 内的独立 typed candidate catalog；只有当前 Step 被宿主选中并检索
到的 bytes 才能进入请求专用参考上下文。

### 2.5 M3 原始历史可达，但不在 Prompt 中常驻

M3 的 `surface/replace` 只改变当前 Surface，原始 `user/message`、`assistant/message` 与合法
`tool/result` 仍在同一个 Session EventStore。v0.9 利用这条既有 provenance 提供按需 History Evidence，
但明确区分“永久可审计”和“每轮都喂给模型”：

- 默认明确选空；显式配置才给有界目录或 M3 摘要，另配置 History reader 后，原文只有用户或模型引用宿主已经披露的 exact `block_id`/cursor 才进入一个
  Step 的 Context Input；模型不能提交任意 seq 范围或获得 EventStore handle；
- 多次压缩时由宿主递归展开 replacement 来源链，每层 fresh 校验 Session identity、source seq/digest、
  闭合 Turn 边界与允许披露的 Surface 事件类型；无法证明完整来源时 fail closed；
- History request 若以只读 Tool 形态暴露，普通 `tool/result` 只返回小型 receipt；原始历史 bytes 由下一
  Step 的 Context Input event/request snapshot 冻结，不作为普通 Surface 消息遗留到后续 Turn；
- 每个历史 block 带 `observed_through_seq`、来源时间/identity 与 digest。F0-C 严格固定
  `workspace_observation=null`、`freshness=unknown`：现有 Workspace base_revision 没有与历史 ToolResult
  的执行时状态绑定。真实 matched/stale 由 F3/F4 接入可核对宿主版本事实后再验证，不能伪造当前验证；
- 当前 fresh Product/Workflow/Promotion/Workspace 事实优先于历史证据。History Evidence 只回答过去，
  当前状态问题仍由对应 owner fresh 读取，代码或测试是否仍成立则针对当前 revision 重新验证；
- 一次披露只影响被授权的当前 Step。后续 Step 若仍需要，必须重新选择并重新冻结；不得靠 widget cache、
  mutable Session state、普通 Surface replay 或超长 `tool/result` 让原文永久驻留。

首版只允许当前 Session 的压缩历史块。跨 Session 的稳定结论走 Workspace Memory；跨 Session 原始聊天搜索
会扩大 scope、隐私与预算边界，不在 v0.9 默认范围。

## 3. Authority 与生命周期矩阵

| 对象 | canonical owner | 允许变化的入口 | 不是它的 owner |
|---|---|---|---|
| Plugin installed metadata | 现有 metadata-only discovery | 安装环境显式变化 | Skill selection / 模型 |
| Enabled plugin / Activation | PluginManager + ActivationSet | 宿主显式 enable/reload | RAG / Memory service |
| Skill catalog | leased Composition Generation | 成功发布新的 Generation | selection stream / FTS |
| Skill eligibility/selection | host context policy + durable selection facts | 用户/Workspace 宿主动作 | PluginManager / 模型文字 |
| Memory proposal | Memory domain | 模型或人可提出 bounded candidate | PromptAssembler |
| Active Memory | Memory service + append-only stream | 宿主对 exact proposal digest 批准/替代/撤销 | 模型 / plugin / retriever |
| Session History Evidence | Session EventStore + M3 replacement provenance | 当前 Session exact block 的宿主只读展开 | 模型 / Memory / retrieval index |
| Retrieval index | derived index owner | 从 canonical Skill/Memory 重建 | EventStore 事实源 |
| Candidate ranking | host retrieval policy | 显式算法/config/version | Memory authority / Tool policy |
| 最终注入 bytes | ContextInputSnapshot | 每个进入请求构建的 Step 冻结一次；失败前缀见 4.4 | 当前数据库 latest state |

跨边界时必须验证两侧 identity 和 owner，不能因为字段都叫 `skill_id` 或 `memory_id` 就默认属于同一事实。

## 4. 共同核心：Context Input 合同

### 4.1 `ContextInputSnapshot` 的可观察语义

准确类型名实现时可调整，但每个 Step 必须在 Provider 调用前冻结一个结构化上下文 receipt，至少覆盖：

- 实际来源的 scope binding、Workspace/Session/Turn/Step identity，以及同一 Lease 的持久内容绑定（见 4.5）；
  Memory 输入必须证明项目 binding；仅当前 Session 的 History 不据此声称拥有项目 Memory 读取资格；
- canonical query 的来源身份和 digest；
- 每个 Skill/Memory/History block 的 kind、id、version/digest、provenance、scope 与 disclosure tier；
- **实际注入的有序精确 bytes**，而不只是“当时选了哪些 id”；
- retrieval policy/version、启用的 lane 与各自 config digest；
- 被资格过滤、去重、预算排除或未命中的结构化原因；
- Skill、Memory 与 History Evidence 各自配额、总 Context Budget、canonical UTF-8 字节计量和剩余量；
  Token 计量只有显式配置可验证的 counter/model identity 才能报告，缺失记 unavailable，不用字节伪造 token；
- 最终 block 顺序和整个 Context Input digest。

注入点在 v0.9-F0 冻结为一条明确主线：

1. `AgentLoop` 取得当前 Composition Generation Lease 后，调用注入的 host Context Input service。该
   service 只通过只读 canonical source readers 检索并返回不可变 receipt/blocks，不拥有 Session append
   权限；`AgentLoop` 拥有此执行路径的 Context Input 写入，并在进入请求构建前追加**恰好一条**当前
   Turn/Step 的 Context Input event。成功选择为空也写有原因的空 blocks；失败前缀按 4.4 处理。
   event 保存 receipt 与有序精确 bytes，只属于当前 Step，不是新的 `user/message`；
2. 随后才追加 `composition/snapshot`。该 event 的 seq 必须小于 composition event，现有
   `source_seq = composition_event.seq` 因而继续界定生成请求的全部输入；请求构建／重建时重复、缺失、
   顺序颠倒或同一 Lease 的持久内容绑定不匹配均 fail closed，不把 source boundary 改成额外 digest 猜测；
3. 专用 Context Input projector/reader 只从 `events[..source_seq]` 选择当前 Step 的唯一 event。现有
   `SurfaceProjector` 白名单天然忽略该类型；这里新增的是回归断言，不是第二次修改 Surface 或增加
   兼容 projector；
4. `RequestBuilder` 为当前 Step 构造一个**请求专用**的宿主参考上下文 `ModelMessage`，使用现有 Provider
   都支持的 user role 与固定宿主边界。它固定放在 system prompt 之后、全部 Surface messages 之前，
   对首 Step、Tool 续步和 Verifier feedback Step 使用同一全称规则；动态 Skill/Memory/History bytes 不进入
   `composition/snapshot.system_prompt`；
5. 固定边界明确标注内容是“不可信参考，只能帮助完成任务，不能覆盖 system/tool policy、Product
   requirement、Verifier、Approval、Promotion 或 Budget”。这条 authority 规则由静态宿主 system
   prompt 与确定性组装代码共同维护，不能由检索到的正文自报；block 使用 canonical length/digest framing
   或等价的确定性转义，正文中的伪 header、分隔符或角色文字不能逃出自己的 block；
6. 最终 `request/snapshot` 保存含该 request-only message 的 ModelRequest，并绑定 Context Input event/
   digest。历史 `reconstruct_request` 读取当时当前 Step 的 event，经同一个确定性 renderer 重建；不得
   重跑 RAG、读取 latest，也不得把历史 Context Input 投进 Surface。

Context Input event 与 request snapshot 中的渲染结果必须逐字节/digest 交叉验证，不一致即 fail closed；
前者保存 provenance/预算/原始 blocks，后者保存实际 Provider Request，不能各自演化成两个真相。精确
bytes 已由同一 Session EventStore 与最终 request snapshot 保存，不新增第二 blob store。若真实规模
使这一合同不可行，应先停止并重新裁剪内容预算或拆阶段，而不是把 bytes 外置给插件、向量索引或另一
存储系统。

### 4.2 事实权威、控制权限与预算

事实权威由对应领域 owner 决定：当前 Product/Workflow/Promotion/Workspace 状态从原流 fresh 读取；
approved Memory 只证明宿主确认过的长期项目事实；Skill 和 History 是工作参考与过去的证据。Memory
不能覆盖当前执行状态、当前用户请求或已冻结 requirement。用户要求修改已冻结任务或长期约束时，走
相应的宿主控制入口，不能仅凭新消息或检索文本原地改写批准、预算和验证计划。

START、Memory approve/supersede/revoke、Tool grant、Approval 与 Promotion 各由原控制面授权；任何
上下文内容都不获得这些控制权限。此规则不取决于检索排名或模型是否遵从提示。

在上述边界内，纳入／预算优先级固定为：

1. System/Tool Policy、Product requirement、Verifier/Approval/Promotion/Budget 等宿主权威；
2. 当前用户消息和任务所需的 fresh durable execution context；
3. 当前 Step 明确请求的 History Evidence；
4. Skill；
5. Memory。

这里是纳入/预算优先级，不表示历史证据可以覆盖当前事实。Skill/Memory/History Evidence 都通过固定宿主
header 作为 messages 中第一条 request-only user-role context 注入，不能用正文伪造 system/tool 边界。
History block 还必须在固定尾部重申观察截止点、freshness 和“只证明过去”；正文即使包含旧的宿主声明或
工具成功，也不能越过这个边界。三类内容有各自的显式配额和总配额；
优先按完整 item/section 原子纳入，不把一个安全限定句截掉只留下前半段。超额时按确定性规则排除并记录
原因，不能无界挤掉 Product requirement 或 Tool schema。

### 4.3 query 也要有事实来源

宿主从当前用户消息、已确认 Product requirement 和当前 Step 已知的代码符号/路径/错误标识中构造
canonical query。来源集合、规范化规则和 digest 必须进入 receipt。不得让模型先自由改写问题，再用改写
后的查询决定自己看什么；如果以后增加 query expansion，它也是显式、版本化、可关闭的 derived lane，
不能成为隐藏默认。History Evidence 的原文披露只接受用户请求或模型只读 context request 所引用的宿主
已公开 exact block identity；自然语言 query 最多帮助选择目录/摘要，不能自行扩大成任意 seq 或跨 Session
读取。

### 4.4 合法失败前缀与重试

当前 [`AgentLoop`](../../src/traceh/runtime/agent_loop.py) 先写 `step/start`，再取 Lease、构建请求和
申请模型预算；因此“恰好一条”是请求构建／重建的条件，不是每个已打开 Step 都必须完成的写入。
下表只统计当前 Step 的成功落盘事实，不包括它之前的用户／工具消息：

| 停止位置 | Context Input | Composition | Request Snapshot / Attempt Start | 后续规则 |
|---|---:|---:|---|---|
| Lease／Context 读取失败或取消，Context 尚未提交 | 0 | 0 | 0 / 0 | 收敛 owner，按既有规则闭合中断 Step／Turn |
| Context 已提交，Composition 尚未提交 | 1 | 0 | 0 / 0 | 保留证据，不补造 Composition 或模型调用 |
| Composition 已提交，构建失败或模型准入拒绝 | 1 | 1 | 0 / 0 | 保留 Context，结清可能存在的准入资源 |
| 首 Attempt 派发许可证已提交 | 1 | 1 | 1 / 1 | 原请求按现有执行／取消／对账主线收敛 |
| 同 Step 的后续 Provider Attempt | 同一条 | 同一条 | 同一条 snapshot / 新 ordinal start | 复用 exact Context、source_seq 和冻结请求，不重跑检索 |

append 取消或失败可能已提交时，先收敛 owned worker，再按精确身份和 payload fresh 对账为
True/False/unknown；unknown 不能猜成 0、重新写一份 Context 或派发 Provider。Recovery 只处理已发生的
事实并闭合合法前缀，不重新检索、不补造 Context/Attempt，也不消费旧 Turn 的 History 请求。
F0-A 已在设计合同 §2 冻结 Context／Request 协议切换与旧 Session 拒绝入口；不为旧格式静默合成空 Context。

### 4.5 Lease 身份与持久内容绑定

当前 [`CompositionGeneration`](../../src/traceh/runtime/composition_runtime.py) 的 `generation_id`
是进程内生命周期编号，不写持久 Composition 或 Request fingerprint。继续保留这一边界：

- 运行期用 exact Lease 校验 Provider、Tool 与 Skill 资源 owner；不能用 digest 相等代替资源所有权；
- durable Context 绑定同 Step 的 composition revision、plugin provenance 与 immutable Skill catalog
  内容 digest；它们来自同一 Lease，不能重新读取 current Generation；
- ADR-0043 已决定由 Composition 持久化有界 catalog descriptor manifest 与其 digest，并纳入 revision；
  Context 只交叉绑定与核对成员。当前源码尚无这些字段，不得把进程序号当作跨进程 catalog identity；
- 历史重建核对当时的关联与精确 bytes，不加载当前插件或 catalog。同内容的不同运行期 Generation
  不应仅因生命周期编号不同而改变模型请求 fingerprint。

## 5. 通用检索与渐进披露管线

### 5.1 两次宿主过滤

“宿主过滤”具体分为两道：

1. **检索前 eligibility filter**：只让当前 Workspace、active Memory、当前 leased Generation 中仍有效
   的 Skill、兼容版本和宿主允许的内容进入候选语料；History 只列当前 Session、通过 format-2 parser 与
   invariant 校验的 replacement block；跨 scope 内容连向量相似度都不计算；
2. **注入前 verification filter**：对排名结果 fresh 复核 scope/status/generation/version/digest、去重和
   Context Budget；History 还要递归复核 provenance、允许事件类型与 freshness；任何漂移 fail closed 或按
   receipt 记为 unavailable，不能换成 latest。

这两道都由宿主确定性代码执行。每个来源最后一次成功复核是其选择观察边界；之后的变化影响后续
fresh 选择，不声称 Session Context append 与各领域读取是跨流原子事务。模型、Skill 文本、Memory
文本、FTS 分数和 reranker 都不能绕过。

### 5.2 检索 lanes

按阶段启用，所有 lane 都只返回 identity + score/匹配证据：

- exact lane：显式 id、符号、路径片段、错误标识和精确 metadata；
- lexical lane：SQLite FTS/BM25，索引只含通过 eligibility 的 canonical 文本投影；
- semantic lane（可选）：本地 embedding；
- rerank lane（可选）：只对融合后的有界 top candidates 使用本地 reranker；
- deterministic fusion：明确版本的融合/排序规则，稳定 tie-breaker，不依赖数据库偶然行序；
- history disclosure lane：从 M3 replacement 投影有界目录/摘要，只对 exact block identity + cursor 做递归
  原文展开。首版不把全部原始聊天加入 FTS/vector，也不允许检索得分自动注入整块原文。

具体 embedding/reranker 模型、维度、chunking、top-k、权重和融合公式都不得成为 demo 名称或代码隐藏
默认；它们来自显式 config，进入 digest/receipt，并经冻结 benchmark 决定是否值得启用。核心安装在没有
这些 extras 时仍能用 exact + FTS 完成检索。

向量如需持久化，只作为 SQLite 内可重建 derived table；删除后从 canonical facts/resources 重建。不得
增加 Chroma、Pinecone、PostgreSQL/pgvector、独立 JSON cache 或插件私库作为第二事实源。

### 5.3 四级渐进披露

为避免与插件 L2 候选验证混淆，不使用 L0/L1/L2 命名。四级是：

1. **目录**：有界 id/title/type/source/cost，帮助宿主和用户知道有什么；
2. **摘要**：候选的短摘要和匹配理由；
3. **正文片段**：按稳定 section/chunk identity 选中的原文；
4. **关联资源/有界原文**：Skill 只有 descriptor 显式声明、digest 匹配且预算允许的只读资源片段；History
   只有 exact block/cursor、provenance 校验与预算都通过的原始 Surface 消息页。

每次请求只注入需要的级别。目录不等于正文，命中 Skill 名字也不自动加载全部 Wheel；Memory 相关也不
等于把整个 Workspace history 塞进 prompt；命中历史摘要也不自动把整段旧对话塞回 Surface。History 原文
是 request-only、Step-scoped 输入，用完即退；EventStore 永久保留不等于 Prompt 永久驻留。

### 5.4 派生索引与 SQLite owner

当前 [`SqliteEventStore`](../../src/traceh/session/sqlite.py) 严格校验 schema 中只有 `streams` 与
`events`。FTS 不能直接加表后继续使用原开库合同。F0-A 已在设计合同 §7 决定 F2 使用同库 schema 2、
精确 FTS5/shadow 对象与 Store-owned 生命周期；以下是所属阶段的实现义务：

- 同库 schema/version cutover、FTS 虚表及其派生对象的精确集合；未知版本／对象仍拒绝，旧数据按
  pre-1.0 政策明确拒绝，未经授权不迁移、不自动改写或删除；
- Store 拥有连接、写入任务和关闭收敛；宿主 index service 通过受控接口维护 derived 内容，不把 SQL
  连接或 EventStore writer 暴露给插件、模型、Context Input reader；
- 索引绑定 canonical source identity/head/digest、作用范围、tokenizer 与排序版本；缺失、过期或损坏
  的已知索引如何被识别、由哪个显式宿主入口重建，以及不可用时的拒绝／receipt 行为。不得放宽任意
  schema 校验来实现“可删除重建”，也不得让索引恢复修改 canonical events；
- 查询 eligibility、同库 writer 竞争、取消、部分重建、backup/restore 与 fresh-open 使用同一套规则；
  optional semantic extras 不得改变核心事实源，重建期间不把半成品当有效候选。

先以 exact+FTS 建立离线基线；可选本地模型只有达到 12.2 的质量与资源门槛后才进入验收范围。

## 6. 明确不做

v0.9 不实现：

- 自动安装、升级、启用任意互联网/本地目录中的 Skill；
- 未审计社区 Skill 的可执行代码或把 `isolated` 静默降级为 trusted；
- Skill selection 授予 Tool、Provider、Policy、Verifier 或 Service；
- 模型自行批准/撤销 Memory，或从每轮聊天自动激活“模型觉得重要”的内容；
- 用户级全局 Memory、跨 Workspace 默认检索、多人共享、云同步或账号系统；
- 外部向量数据库、联网 embedding/reranker、RAG SaaS；
- 用 Memory 替代 Session Event、projection checkpoint 或源码；
- 让 Skill/Memory 改写 requirement、VerificationPlan、evaluator、Approval、Promotion、Budget；
- 第二 PluginManager、第二 PromptAssembler、第二 EventStore、第二 eval 命令或 Benchmark Runner；
- 把所有压缩历史自动注入每轮请求，允许模型任意读取 EventStore seq，或默认跨 Session 搜索原始聊天；
- 把展开的历史原文作为普通 Surface `tool/result` 长期携带，或把旧 revision 的工具结果当成当前验证；
- OS sandbox。若需求变成运行不受信社区内容，先停止并另立 sandbox hardening 阶段。

## 7. v0.9-F0：Context Input、检索与 authority 决策

F0 分为设计冻结和获授权后的最小主线原型。2026-09-07 已完成 F0-A 的 ADR 与设计合同，下列决定均在
设计合同中有对应定义；它不是原型通过记录，也不等于 F0 整体已完成：

- `ContextInputSnapshot` 与现有 Composition/Request Snapshot/Fingerprint 的唯一绑定；
- Context Input event 必须先于当前 `composition/snapshot`，并由现有 `source_seq` 包含；专用
  projector/reader、request-only message 与 `SurfaceProjector`/`composition.system_prompt` 保持隔离；
- Skill eligibility/selection 的 durable identity、scope 与写入 owner；
- Memory proposal/approval/supersede/revoke 的状态机和来源证据；
- query source、exact/FTS lane、filter、budget 和 progressive disclosure 的确定性合同；
- M3 History block 的稳定 identity、嵌套 replacement 递归展开、允许事件白名单、freshness 与分页合同；
- 模型只读 History context request、最小 Tool receipt 和下一 Step request-only bytes 的唯一接线；原文不得
  进入普通 Surface，当前状态不得由旧历史推断；
- derived index 的 schema/version/rebuild 规则，以及 optional local model config/receipt；
- RequestBuilder 在 authority tier 之间的顺序、固定不可信内容边界和 atomic item policy；
- UI、模型 proposal Tool 与真正宿主 writer 的权限分离；
- 跨 Session 项目 scope 的唯一宿主 binding、requester／managed child 继承、重绑定拒绝，以及 Memory
  事实槽和 exact predecessor CAS（10.1）；
- 4.4 的合法失败前缀、4.5 的 Lease／durable catalog 身份区分、11.2 的 History 请求失效规则；
- 5.4 的 SQLite schema／重建／关闭／备份合同，以及 12.2 的评测单位、计量、阈值与 seeding 顺序。

### 7.1 F0 实施批次与定向原型（A/B/C 已完成）

| 批次 | 产出与 owner | 停止条件 |
|---|---|---|
| F0-A 设计冻结（已完成） | ADR-0043/0044 与 F0 设计合同：身份／状态、事件绑定、失败前缀、schema、评测和 owner 验收 | 现有接缝定向 19 项通过；只作为设计依据 |
| F0-B 最小请求主线（已完成） | 现有 AgentLoop／Session／RequestBuilder 中的 Context 冻结、确定性 renderer 与 reconstruction；现有 TUI 统计适配 | 最终限定 36 文件 `1079 collected / 1076 passed / 3 skipped`，含新主线 59 项；两轮有界审查及 TUI 复审无 P0/P1，不宣称 F0 整体完成 |
| F0-C History 接缝（已完成） | format-2 有界展开、逐页 cursor、typed 用户请求与 Tool receipt 到同 Turn 紧邻后继 Step；协议显式切换 | 最终 38 文件 1100 passed / 4 skipped，含新 History 81 项；两分区无 P0/P1，七组反向证据；freshness 只为 unknown，原文不遗留 |

F0-B 只接一条可继续演进的生产请求主线：用已存在的 Session 历史作为证据来源，尚未实现的 Skill／Memory
catalog 必须为空；receipt 明确记录 Session 范围，不虚构尚未建立的项目 binding 或 Memory 读取资格。
不为原型创建另一 Runtime、假 Memory authority 或私有 pending 缓存。F0-C 验证同一主线
的历史披露边界，不提前做完整治理 UI。

F0-C 的详细字段以设计合同 §2–5 为准：Session 唯一切到 `context_protocol=2`，拒绝 F0-B 的 1；
outer Context format 仍为 1，policy 为 `f0-c-context-policy-v1`，config 原七项加 nullable `history`。
HistoryReadPolicy 恰七项显式资源／分页／请求上限；只支持关闭、目录／摘要参考、目录／摘要加 reader，
没有 raw-only 模式。renderer 唯一为 `context-json-v2`；默认仍注入一条空 wrapper 的 request-only
user message，先于全部 Surface。Request 十字段、Composition 空 catalog/digest 与物理 schema 1 保留。
不双读、不迁移，不提前实现通用检索、Skill/Memory、项目绑定或实时 Git 观察。

`session/history.py` 是纯 reader，`api/history.py` 定义 typed DTO，`session/history_requests.py`
集中处理请求资格／目标派生。Tool `request_history_page` 为普通 PURE_READ，仅把 receipt 放在事件
`data["data"]["history_receipt"]`；`include_default_tools=False` 不自动授予它。用户请求经显式
`TurnInput.history_requests` tuple/ChatDriver 透传，由原 SessionService 在首 Step user/message 后
owned/CAS 批量追加 `history/requested`，source/text 不赋权。不新增 writer、Lease 或 pending 状态机。
原文页先于自动目录／摘要占用原子预算；section/chunk 同按闭合 Turn 分页，超大 Turn 整页拒绝并
保留稳定 slot，不造 accepted receipt。只披露首 cursor 和当前页的 next_cursor，不给全页目录。
本 Session 已披露但后来被更宽 replacement 隐藏的旧 block 仍可按精确身份请求。

F0-B 三个新模块共 59 项包含在其最终 36 文件的 `1076 passed, 3 skipped` 中；证据集中维护在正式上下文
15.1 和设计合同 §11，不重复相加不同测试集合。现有 TUI 通过同一纯请求重建，分别统计 reference、
Product 与 conversation，未新增 F5 治理 UI。F0-C 最终证据见设计合同 §12：1104 collected、1100 passed、
4 skipped；新 History 81 项已包含，不复用或另加 F0-B 计数。
以下是 F0 必须证明的行为：

- 同一 identity、观察边界、canonical facts/query/config 两次得到字节一致的 ContextInputSnapshot；不同 Step 的整体 digest 不要求相同；
- host Context Input service 只能读取 canonical sources 并返回不可变 snapshot；临时授予它 Session append
  能力或让它直接写 selection/Memory/context event 时，owner 边界测试必须变红；
- 当前 History source 在 snapshot 后变化，不改变历史 request reconstruction；Skill／Memory 的真实
  变化在 F1/F2、F3/F4 按相同合同接入后验证，不用替身提前声称这些 owner 已成立；
- 连续两个 Step 分别检索不同内容时，第二个 request 只含自己的 Context Input；Surface replay 不含任一
  Context Input event，历史 reconstruction 仍逐字节相同；
- 上述连续 Step 至少一个必须是 Tool Call 后没有新 `user/message` 的真实续步；Context message 在两次
  request 中都位于全部 Surface messages 之前，不能落到 Tool result 后面；
- 模型请求一个已披露 History block 时，普通 Tool result 只出现有界 receipt，下一 Step 的 Context Input
  才包含精确原文；Turn 结束后的 Surface 与再下一 Step 均不自动携带该原文；
- 一层和多层 `surface/replace` 都能按原逻辑顺序、分页边界与 digest 重建允许披露的原始 Surface 消息；
  篡改 source、循环/向未来引用、非闭合边界、隐藏事件类型或跨 Session block 均在注入前拒绝；
- F0-C 原文与目录／摘要的 workspace_observation 均为 null、freshness 均为 unknown；伪造 matched/stale
  稳定拒绝，宿主文案和 receipt 不宣称当前测试或代码状态。同／不同／缺失真实 revision 的三态验收
  移至 F3/F4 的来源 owner 接入阶段；不能用构造 fixture 冒称当前主线已有版本观察；
- 未披露 cursor、越过超大 Turn 的重编号、跨 Session block 拒绝；合法披露后被新 replacement 隐藏的
  旧 block 仍可读取。Tool grant 与 typed 用户入口分别验证，source="user" 或正文不能伪造权限；
- 篡改 Context Input event、request snapshot 或二者 binding/digest 任一侧时 reconstruction fail closed；
- 进入 build/reconstruction 的同一 Step 出现零条/两条 Context Input event、event 晚于 composition，
  或持久内容绑定不一致时稳定拒绝；4.4 的零 Context／无 Request 合法失败前缀仍可检查和恢复；
- Context append 前后取消、may-have-committed、Composition 失败、Budget 拒绝不补造模型调用；同 Step
  的后续 Attempt 不重新检索，保持同一 Context/source_seq；
- History receipt accepted 后触发 max_steps、取消或恢复时，后续 Turn 不消费这份请求、不包含对应原文；
- block 正文包含伪 system/user header、边界标记、控制字符或嵌套引用时，renderer 仍保持同一 block
  identity/bytes，不能生成额外 ModelMessage 或改变后续真实用户消息；
- prompt 超额时 protected authority 不被 Skill/Memory/History Evidence 挤掉；
- secrets/approval/evaluator 形态扫描和明确拒绝规则不依赖模型自觉。

F0-A 同时登记后续 owner 的必测项：F1/F2 验证 retired Skill、Lease/resource identity 与 selection；
F2 验证 exact/FTS 排序和 schema/rebuild；F3 验证跨项目 scope、事实槽竞争、revoke/supersede；F3/F4 接入
真实 Workspace identity/revision 与历史 ToolResult 的持久绑定后验证 freshness 三态，F4 验证三类输入
共同过滤、预算和时效；F5 验证治理与真实 attempt seeding。它们仍是所属阶段门禁，不要求 F0 提前实现。

### 7.2 F0 退出条件

ADR-0043 已覆盖共同 Context Input/Retrieval/Skill/History，ADR-0044 独立解释项目绑定与 Memory authority；
详细字段只在 F0 设计合同维护。设计、代码原型和执行证据分别标状态；只有 F0-B/F0-C 的
获授权主线验证证明 source boundary、重建、失败前缀及 History 失效正确后才可进入 F1。不能用文档审阅
代替原型通过，也不能因后续 owner 尚未实现就在 F0 堆齐全部 v0.9 功能。

## 8. v0.9-F1：现有插件主线上的 typed Skill contribution

### 8.1 实现范围

- 在公共 Plugin SDK 增加最小 Skill descriptor/contribution；准确名字由实现决定；
- contribution 只含稳定 metadata、prompt/reference section、资源 digest、贡献 plugin identity 与兼容
  条件，不含 Tool grant；
- setup 写入候选私有 registry，经现有 conflict/health/receipt 后随 ActivationSet 原子发布；
- candidate registry 是独立 typed contribution surface，不调用 `register_prompt`、不写
  Composition system prompt，也不在未选择时向 Request 注入正文；
- Generation 捕获 immutable Skill catalog，Lease 期间 reload 不改变 catalog bytes；
- discovery 仍 metadata-only、不 import 未启用插件。若安全元数据不足，UI 显示“启用后可见”，不偷 import；
- Activation transaction 负责验证 resource descriptor、相对路径、root containment 与 digest；成功发布后，
  Composition Generation 拥有不可变 descriptor/只读 root handle，Lease 保证读取期间不被 drain cleanup
  抽走，最终仍由现有 Generation owner 收敛；
- resource 读取只通过 leased contribution 接口，路径不能逃出已验证的 Wheel/resource root；不得让
  retriever、Skill selector 或插件私有缓存成为第二 resource owner。

### 8.2 必测与反向验证

- installed-but-disabled Skill 不进入 catalog/request；
- setup/health/conflict/receipt 失败不发布半个 Skill；
- 两插件同 id 冲突 fail closed，不 last-write-wins；
- resource bytes/digest、descriptor/plugin identity 不一致时 candidate activation 失败；
- old Lease 看旧 catalog，new Lease 看新 catalog，cleanup 不抽走在用资源；
- resource read 与 reload/drain 并发时，旧 Lease 读到旧 digest bytes，新 Lease 读新 Generation；所有
  Lease 释放前 root 不关闭，释放后 cleanup 恰好一次；
- 选择 Skill 不启用插件、不发布 Generation，也不获得该插件另行贡献的 Tool；
- enabled 但未选择/未命中的 Skill，其正文在 `composition/snapshot.system_prompt`、Surface messages 和
  最终 request messages 中均不存在；临时改成 `register_prompt` 时该反例必须稳定泄漏并变红；
- 临时移除 catalog receipt、resource digest 或 no-tool-grant 守卫，公开路径按根因变红。

## 9. v0.9-F2：Skill 选择、检索与渐进披露

### 9.1 选择与 retrieval

- 用户/Workspace host policy 决定哪些 enabled Skill eligible；模型不能把自己喜欢的 catalog 项激活；
- durable selection 只影响未来 Step，幂等/CAS/may-have-committed 复用项目现有规则；
- 每个 Step 在 leased catalog 上先 hard filter，再走 exact + FTS，最后按四级披露和预算冻结 blocks；
- optional semantic/reranker 只有在后续 benchmark 证明增益且显式启用时加入；
- 同一 Step 内 selection、plugin reload 或资源变化不改变已经冻结的 bytes；
- 不提供能越过 selection/filter 的万能 `load_skill` Tool。

### 9.2 必测

- 安装多个 Skill 只选择/命中少数时，其余正文和资源不进入 request；
- 相同选择幂等，不同选择产生不同 selection/context digest，但不改变 Plugin Identity；
- 当前 Step 保持原 Lease/snapshot，后续 Step 才看到选择或 reload 变化；
- 前一 Step 已注入的 Skill 不因 Surface replay 自动进入后一 Step；只有后一 Step 自己的检索结果可见；
- prompt 不足时按 item/section 原子排除并记录原因；
- retired Skill、resource missing/digest drift、跨插件伪引用均 fail closed；
- exact/FTS 结果在相同 SQLite schema/query/config 下确定排序。

### 9.3 Release Stop A

F1/F2 独立审查 Plugin/Generation/selection/Request 边界。P0/P1 清零后才进入 Memory authority；无需为
停止点反复跑全量或提前发布，但不能用未来 Memory 测试掩盖 Skill lifecycle 缺陷。

## 10. v0.9-F3：Workspace Memory append-only authority

### 10.1 状态与 scope

在同一个 EventStore/SQLite 中新增一个独立 Memory 领域和唯一 projector/service。ADR-0044 与设计合同
§6 已冻结 exact 事件与 CAS 规则，语义区分：

```text
proposed -> active -> superseded | revoked
```

- proposal 不是 active；模型只能提交 bounded candidate，不能批准；
- host approval 必须绑定 exact proposal digest、source evidence 和当前 Workspace scope；
- Workspace Memory 的 scope 是宿主持久绑定的长期项目范围，当前 Session／managed Workspace 必须
  通过该 binding 解析，不能由模型/plugin payload 自报，也不直接等同于一个临时 worktree id；
- supersede/revoke 追加新事实，不更新/删除旧 row；
- active view fresh replay、detached、fail closed，不缓存第二份 mutable truth；
- source 可指向人工输入、Session/ProductTask 等 durable evidence；未经证实的模型猜测不能冒充用户事实。

当前 [`SessionService.create_session`](../../src/traceh/session/service.py) 只保存解析后的目录；
[`workspace_identity`](../../src/traceh/workspaces/service.py) 按 provision operation/request 生成
单个 managed worktree 身份。它们不是现成的长期项目 identity。F0 必须选定同一 EventStore 中 scope
binding 的唯一宿主写入／读取 owner；F3 把它与 Memory authority 接通，并证明：

- requester、后续 Session、派生 Agent/worktree 的关联都来自明确的宿主配置／动作及 durable binding；
  路径相同、`source_id` 同名或模型 payload 相同不能单独证明同项目；
- child 只能通过宿主已批准的父任务／项目关系继承 scope；缺失、歧义或跨项目绑定在读取和批准前拒绝；
- 项目移动、重新绑定或 source mapping 变化的接受／拒绝条件必须明确，不静默继承旧目录的 Memory；
- binding 是项目归属事实，不是第二 Workspace lifecycle、全局用户记忆或对外部资源的权限授予。

Memory 的每个 `fact_slot` 由宿主确认，字段规则已由 F0-A 冻结。同一 scope、同一槽至多一个 active；
approve 绑定当前槽为空或精确前任，supersede/revoke 绑定 exact predecessor identity/digest 和观察到的
stream head，经 CAS 追加原子事实。竞争者重新读取后明确失败，不能 last-write-wins 或激活两个版本。
不同自然语言事实间的语义冲突交宿主审核，不承诺 FTS／模型自动证明所有 active 文本互不矛盾。

### 10.2 内容边界

只接受短小、稳定、用户可理解的 Workspace 事实。明确拒绝秘密、`.env`/Key、临时任务进度、模型自评、
Approval/Promotion authority、Budget 内部凭据、Verifier/evaluator 冻结输入和整段大文件副本。拒绝规则必须
由 host 执行；“提示模型别记”不是控制。

首版允许的语义类别至少覆盖：长期目标、持续约束、项目术语、已确认架构决策、已完成的重要里程碑、
当前获批阶段和已经批准的后续路线。它们仍是 bounded facts，不是项目管理数据库；“正在修改某文件”“某次
测试目前跑到哪里”“某个 ProductTask 正在等待 Approval”等瞬时执行状态继续由原 Session/Product/Workflow
owner 回答。阶段、路线或决定变化时追加 supersede/revoke，不原地改写，也不让两个互相冲突的版本同时 active。
所谓“长期用户偏好”只允许当前 Workspace scope，不能自动升级成用户级全局偏好。

### 10.3 必测与反向验证

- proposal 未经 exact digest 宿主批准，后续 Step 不可见；
- 同 request id 不同内容、错误 source/scope/digest、重复 active identity fail closed；同事实槽不同
  proposal 竞争 active、过期 predecessor/head 的替代或撤销均拒绝；
- 跨 Workspace approval/proposal/source 引用在 append 前零写入拒绝；
- 同项目两个 Session 和合法派生 worktree 读取同一 active Memory；同名 source／同路径但没有正确
  binding 的另一项目不能读取或批准；scope 创建／绑定中途失败不留下可被误认的授权；
- revoke/supersede 后新 Step 不再选旧版本，历史 snapshot/reconstruction 保留；
- unknown schema/event/order、篡改 source、敌意 payload fail closed；
- append cancel/unknown 三态对账不重复激活，close 前 owned worker 收敛；
- plugin/Skill/model 没有 writer/EventStore 句柄；
- 临时放宽 model approval、scope 自报或 mutable active flag，反例真实越权后恢复。

### 10.4 Release Stop B

Memory authority 独立 P0/P1 审查清零后，才允许把它交给 RAG。检索质量不能补救错误 authority。

## 11. v0.9-F4：Memory 检索、History Evidence 与统一 Context Orchestration

### 11.1 统一但不混同

Skill 和 Memory 在这一层共同实现：query receipt、两道 filter、exact/FTS、可选 semantic/reranker、
deterministic fusion、渐进披露、分项/总预算和 ContextInputSnapshot。History Evidence 复用同一个预算、
framing 与 snapshot，但只走当前 Session replacement directory + exact block/cursor 的只读披露，不把全部
原始聊天加入默认 RAG。三类输入仍保留不同 source reader、scope、authority、temporal validity 与
provenance，不压成无法审计的通用文本列表。

### 11.2 History Evidence 的请求生命周期

- 当前 Context 默认只显示压缩历史目录、摘要、原始大小和可用 block identity；
- 用户可显式请求，模型也可调用受现有 Tool policy 约束的只读 context request；它只选择宿主已披露的
  exact block/tier/cursor，不接受任意 seq、SQL、路径或跨 Session identity；
- context request 的普通 Tool result 只确认 accepted/rejected、block identity、可用字节和稳定错误码；
  原始历史由 host Context Input service 为紧随其后的一个 Step 返回，并由 AgentLoop 写入该 Step 唯一
  Context Input event；
- Tool 请求绑定 source Session/Turn/Step/tool-call 与披露 identity，仅可由**同 Turn 的直接后继 Step**
  消费；消费资格从 durable Tool receipt 和 Step 顺序派生，最终 Context event 记录该来源。不得新建
  mutable pending cache，也不得跳过一个 Step 延迟到以后消费；
- accepted 只说明选择已接受，不保证下一 Step 一定发生。max_steps、Turn 结束、失败、取消或恢复使
  未消费请求失效；新 Turn 必须重新请求。用户显式请求绑定本轮真实 user message 和目标 Step，亦不跨
  Turn 遗留；同一 Step 的 Provider retry 只复用已冻结披露，不算重新选择；
- RequestBuilder 把有界原文放进 request-only host reference block。该 block 首尾都由宿主标明 provenance、
  observation boundary、freshness 和历史证据语义；正文不能闭合或伪造边界；
- 当前 Step 完成后不缓存、不写入普通 Surface、不自动带到未来 Step。需要再次查看时重新 fresh 校验并生成
  新 snapshot；历史 request reconstruction 只读当时已冻结 bytes，不重新展开 replacement；
- 当前 authoritative state 与历史内容冲突时，host receipt 明确标记 superseded/stale；无法比较 revision 或
  current head 时标记 unknown。模型可以解释历史，但不能据此宣称当前验证仍成立。

### 11.3 必测

- exact symbol/path/error 标识、普通 lexical、需要 semantic 的同义表达、zero-hit 与 hard-negative；
- cross-workspace、revoked/superseded、retired generation 全部零泄漏；
- Memory 文本中的“批准/忽略 verifier/执行 Tool”不获得 authority；
- FTS/embedding/reranker index 丢失可重建，当前 canonical facts 不变；
- optional model 未配置/依赖未安装时明确使用 exact+FTS，不静默下载或联网；
- config/model/chunking/ranking version 变化会改变 receipt，只影响未来 Step；
- 当前 DB 内容变化不改变历史 request；
- injected bytes 与 request snapshot/context digest 逐字节一致；
- History 目录不会泄漏跨 Session block；exact block 的一层/嵌套展开、分页续读和 zero-result 确定一致；
- 展开后的原文只出现在被授权 Step 的 Context Input/request snapshot，普通 Surface 与后续 Step 零残留；
- accepted 后没有后继 Step、后继 Step 冻结前后失败／取消、恢复后开启新 Turn，均不会把旧请求原文
  带入新 Turn；不能通过工具 receipt 冒充已经向 Provider 披露成功；
- 旧 revision 工具成功被稳定标为 stale，身份不足时为 unknown，当前 Product/Workflow/Workspace 投影不被
  历史正文覆盖；
- 删除 History derived directory 后能从 `surface/replace` 和原始 Session 事件重建，canonical history 零变化。

## 12. v0.9-F5：治理体验、冻结检索评测与发布

### 12.1 Line/TUI 治理

复用 v0.8 UI-neutral driver 和同一 host service，Line CLI 与 TUI 都能：

- 区分 installed/enabled/eligible/selected/retrieved Skill；
- 查看 Skill 来源 plugin、digest、披露级别和 context cost；
- 在插件 enable 发生前，复用 Manifest `provides` 与 activation receipt 清楚展示这次 enable 同时授予的
  Tool/Provider/Policy/Verifier/Service 等能力；不能让“为了一个 Skill”掩盖真正的插件权限面；
- 查看 Memory proposal、source、scope、正文/digest，并显式 approve/supersede/revoke；
- 查看 M3 压缩历史目录、摘要、source digest、原始大小和 disclosure receipt；用户可以按 block/page 展开，
  UI 必须同时显示观察截止点与 freshness，不能把旧工具结果标成当前事实；
- 查看当前 Step 实际 ContextInputSnapshot：query 来源、命中、排除原因、预算和最终 blocks；
- 重建后继续读取同一事实，不依赖 widget cache。

按钮和命令都调用相同 service；UI 不直写 SQLite。模型可以提议 Memory 或请求进一步解释，但不能替用户
按批准按钮、改变 eligibility 或越过 context budget。

### 12.2 检索质量评测只复用现有 `traceh eval`

本阶段以 v0.8-F1 已完成、CLI 与 `evaluation/attempt.py` 已统一使用 SQLite store-open/ownership 边界为
硬前置；若该前置没有在当时真实 HEAD 上成立，F5 不得用评测扩展顺手补数据库主线。

不得新增 `traceh rag-eval`、第二 Runner 或模型自评。若 manifest 需要扩展，进行一次唯一 schema cutover，
同步 shipped benchmark 并明确拒绝旧 schema，不保留双 reader。

冻结语料的唯一装载 owner 是现有 `evaluation/attempt.py::run_attempt` 主线。当前代码先构造 Product
host，再在 `_prepare()` 创建 requester Session；不能直接在 host 前插 seeding 就声称已有合法 scope。
F0-A 已冻结以下顺序，F5 在同一 attempt owner 内实现：创建 SQLite store 和受控 Runtime → 创建 requester
Session／宿主项目 scope binding → 按 exact-key manifest/digest 使用生产 Plugin/Memory service 完成
seeding → 构造 Product host → 通过同一 Session 发起请求。tool-free requester 使用 attempt source
repository 作为 workspace，由宿主 resolver 核对真实项目关联，不从独立 `rw` 目录伪造 scope。
后续 coder Session/worktree 由生产宿主绑定
继承同一项目 scope；manifest/source id 不能自报授权。各创建、绑定、seed 和 host 装配失败／取消都由
既有 attempt 的关闭主线收敛，不引入第二个 prepare Runner。所有相对资源都必须解析在 manifest root 内；完整 corpus
不复制进 source repository 或 coder writable Workspace。人工 relevance judgments 与 evaluator 规则只由
Runner 持有，不进入 store 中模型可检索的 canonical 内容；只有正常检索选中的 reference blocks 会按
Context Input 合同进入 ModelRequest。不得在 `run_attempt` 外增加语料准备 Runner、旁路 registry 或第二
EventStore。

检索评测作为既有 ProductTask attempt 的**宿主冻结 evaluator**：

- benchmark manifest、完整 frozen corpus 和人工 relevance judgments 不进入 coder writable Workspace 或
  模型上下文；query 只通过各 arm 相同的正常用户/requirement 输入与 receipt 生效，不作为额外 evaluator
  指令偷塞给模型；
- 每个 attempt 的 frozen input identity/digest、host seeding receipt 与 Workspace scope 可复核；少一项、
  多一项、digest 漂移、越出 manifest root 或 seeding 发生在 Product host 构造后均拒绝；
- runner 从 `ContextInputSnapshot` 读取真正注入的 ids/tiers/bytes，不能从模型文字猜“它用到了什么”；
- 同条件比较 exact+FTS 与显式启用的 semantic/reranker policy，除 policy 外 query/corpus/scope/budget 均相同；
- 至少包含 exact/symbol、lexical、semantic、zero-hit、cross-workspace、revoked/superseded、
  retired-generation 和 hard-negative；
- 指标至少报告 Recall@K、MRR、context precision、zero-hit accuracy、scope-isolation violations，以及
  unavailable/unproven；
- n=1 明确标注 single observation，不声称统计显著；模型不能批准 evaluator 或自评“更聪明”。

指标清单还必须有可执行的通过标准。F0-A 已冻结评测规格，F5 在看 candidate 结果前冻结具体语料、配置和
阈值，不能跑完后调整题目或成功线：

- 以唯一 Step 的 ContextInputSnapshot 为注入观测单位，同 Step Provider retry 不重复计样；按角色、
  query 与 attempt 分层汇总，明确失败、unavailable、unproven 的分母，不能只统计成功请求；
- K、候选排序与实际注入的区别、tier/section/chunk 到 relevance judgment 的映射、重复命中处理和
  zero-hit 定义均进入 evaluator digest；
- 覆盖中文、英文和代码符号／路径／错误标识混合输入，以及换 Session 后询问项目目标／阶段的用户旅程；
  当前 Session History 披露另按 exact identity/provenance 验收，不能扩大为跨 Session 原文检索；
- exact+FTS 先有冻结 baseline。scope-isolation violations 必须为 0，其他 Recall/MRR/precision/zero-hit
  的最低值、容许退化和资源上限须显式给出；阈值缺失时只报告测量，不声称通过质量门禁；
- 记录 Context 字节成本、检索延迟、索引规模／重建成本及明确的环境和语料规模。本地 semantic/reranker
  只有在同条件比较达到预定增益、关键类别不越过退化线且成本不超限后才可启用；未证明收益时保持可选
  lane 关闭，不妨碍达标的 exact+FTS 核心路径交付。

冻结选择是保留该评测，而不是默认延期；但若实现时无法由现有 `run_attempt` 在上述边界内装载语料，
评测整体延后，不以交付压力为理由新增平行 Runner。核心 deterministic retrieval tests 始终是实现门禁。

### 12.3 Release Stop C 与最终门禁

1. compileall；
2. Context snapshot/reconstruction、Plugin Skill lifecycle、selection、Memory authority、History Evidence
   展开/Step-scope/freshness、retrieval/index、Composition/RequestBuilder、Line/TUI 定向测试；
3. Plugin Activation/Generation/Lease/Drain、Session/Request、Budget、Product/Workflow/Review/Approval/
   Promotion/Evaluation 相邻回归；
4. SQLite derived index rebuild、fresh data dir、核心离线安装；只有纳入该版本交付的本地 extras 才要求对应安装与质量门禁；
5. collect-only、Ruff、diff-check、链接/围栏/章节 QA、secrets/本机路径/archive scan；
6. 每个 release stop 的 Finding 已关闭后做最终独立 P0/P1 审查；
7. 清零后在明确的最终检查点运行一次从头开始的无筛选全量；确定性失败修复后重跑到绿色属于同一检查点，不能用 `--lf` 代替；
8. clean-input Wheel/sdist/source ZIP、外部示例 Skill Wheel、无 TUI/semantic extras 的 core 安装；
9. 授权真实模型 acceptance 只证明 receipt 注入、权限未扩大和生命周期闭合，不让模型自评质量。

### 12.4 门禁触发与本轮限制

| 工作阶段 | 允许／要求的验证 | 不自动触发 |
|---|---|---|
| F0-A 文档工作 | 文档 QA；按本轮授权对现有接缝做精确 nodeid collect-only／定向测试 | 整仓全量、L2、compileall、构建和联网；不把基线通过称新功能通过 |
| 获授权的 F0-B/F0-C、F1–F4 实现批次 | compileall、受影响 owner 的正向／反例／失败取消和必要反向验证、相邻回归、collect-only、修改范围 Ruff／diff-check | 整仓全量、真实 L2-L4、Wheel、包索引和真实 Provider |
| Release Stop A/B | 对所属 owner 独立审查、修复后定向确认 | 不因到达停止点就重跑全量或 L2 |
| 明确获授权的集成／F5 发布检查点 | 12.3 的最终全量及属于交付范围的长门禁 | 不以“计划已批准”推导真实服务或重复长门禁授权 |

实施批次开始前按当时 HEAD 写出具体测试文件／nodeid 与相邻 owner，检查选中项是否包含 `slow`／构建／
联网。当前 `test_real_candidate_validation_runs_every_l2_gate` 属 `slow` 且会被无筛选全量选中；不能
把整份候选验证文件或整仓 pytest 当作普通快速回归。最终检查点应明确全量包含哪些长门禁，避免另跑
独立 L2 后又无意在全量内重复。确需独立 L2 的发布证据时，必须明确记录原因与授权。
用户禁止全量／L2 时保持禁止，报告相应门禁未运行；不得把有筛选结果称为最终无筛选全量。

## 13. 完成定义与拆版规则

v0.9 完成必须证明：

- Skill 是现有 trusted Plugin/Generation 生命周期的 typed catalog，不是第二插件系统；
- Skill selection 只影响未来 ContextInput，不 publish/drain Generation、不授予 Tool；
- Memory 是同一 EventStore 中 host-approved、Workspace-scoped、append-only facts；
- M3 History Evidence 只从当前 Session 的原始 Surface 事件与 format-2 replacement provenance 重建；它
  request-only、Step-scoped、可分页且带 freshness，不成为 Memory、第二 EventStore 或普通 Surface 常驻内容；
- exact/FTS 与可选本地 semantic/reranker 都只是 derived retrieval，索引可重建；
- 两道 host filter、渐进披露和 Context Budget 阻止 scope/authority 泄漏与无界 prompt；
- 历史 Step 从 `source_seq` 内唯一 Context Input event 的精确 injected bytes 重建，不重跑 RAG、不读
  latest；首 Step 与 Tool/Verifier 续步使用同一 message 位置；
- Skill 关联资源由 Composition Generation/Lease 拥有并随 drain 收敛，不产生 retriever 私有资源生命周期；
- Line/TUI 共用同一治理 service，`traceh eval` 仍是唯一评测入口；
- trusted-only 边界被诚实陈述，没有把进程内 Plugin 宣传为 sandboxed。

冻结方案保留同一个 v0.9 产品主题、分三次独立停止审查；但下列任一条件成立时必须重新由项目所有者
决定拆版，而不是留下 feature flag/双 schema：

1. Skill typed contribution 需要绕过现有 Activation/Generation owner；
2. selection 无法在不触发 plugin migration 的情况下绑定未来 request；
3. Memory authority 无法独立于 retrieval 正确成立；
4. Context Input event 无法在当前 Composition Lease 内先于 `composition/snapshot` 落盘，或无法由现有
   `source_seq` 完整界定；
5. 专用 projector/reader 无法从当时 event 重建 exact bytes，必须污染 Surface/system prompt、重跑 RAG
   或读取 latest；
6. History Evidence 无法在不允许任意 seq、跨 Session 读取、永久 Surface 驻留或把旧结果冒充当前事实的
   前提下实现；
7. optional local semantic lane 需要联网、外部数据库或隐藏模型默认；
8. 真实需求扩大成运行未审计社区 Skill，必须先完成独立 sandbox hardening。

## 14. 冻结后的重新批准与停止规则

- 2026-09-07 按已发布 v0.8.0 完成只读可行性核对后，经项目所有者授权修订本计划。此次澄清项目 scope、
  Lease／catalog identity、失败前缀、History 失效、FTS owner、评测和门禁；不改变 Skill + Memory +
  History Evidence 同一 v0.9 主题、三个 release stop 或唯一注入主线；
- v0.8 发布前置已满足，F0-A ADR／设计合同与 F0-B 最小请求主线／限定门禁已完成。最终 36 文件
  `1076 passed, 3 skipped`，其中包括 59 项新主线；F0-C 随后完成最终 38 文件 `1100 passed, 4 skipped`，
  含新 History 81 项。F0-A/B/C 授权实现收口，F1 未开工，不声称发布或后续能力已验证；
- F0 必须先证明 Context event/source boundary、Tool 续步位置、历史 request reconstruction，以及 M3
  History 原文不会常驻 Surface；F1/F2 必须先证明 Generation/Lease/resource owner；F3 必须先证明 Memory
  authority。后阶段不得掩盖前阶段 owner 缺口；
- retrieval quality 继续由唯一 `traceh eval` 度量，语料由现有 attempt runner 装载。若该 owner 在真实
  实现中不可行，延期整个质量评测，不新增第二 Runner；
- trusted-only Plugin 是本版本接受的边界。sandbox 仍是独立 hardening；不得为了声称支持不可信 Skill
  在 v0.9 内实现半套隔离或静默降级；
- 如果准确类名、SQL 表或排名公式需要调整，可以选择更轻实现；改变 canonical owner、事实源、权限、
  source boundary、唯一 Runner 或无联网/外部向量库合同，必须重新由项目所有者批准。

## 15. 完整执行总表

| 顺序 | 阶段 | 主要产出 | 阶段停止条件 |
|---:|---|---|---|
| 0 | v0.8 前置（已完成） | 已发布 v0.8.0；按发布基线重新核对并修订计划 | 不为重新核对重复 v0.8 全量／L2；功能开工另行授权 |
| 1 | F0-A（已完成）→ F0-B（已完成）→ F0-C（已完成） | 唯一 Context 主线、当前 Session 原文分页及 typed/Tool 请求均已接入 | 38 文件 1100 passed / 4 skipped、两分区无 P0/P1；本轮实现收口，F1 未开工，不代替发布级全量 |
| 2 | F1 | 现有 Plugin Generation/Lease 上的 typed Skill catalog 与只读资源 owner | 半发布、identity/digest、reload/drain 反例全绿 |
| 3 | F2 | Skill durable selection、exact+FTS、四级披露、可选本地 semantic/reranker 接口 | Release Stop A：Plugin/selection/Request P0/P1 清零 |
| 4 | F3 | Workspace Memory proposal/active/superseded/revoked append-only authority | Release Stop B：Memory authority P0/P1 清零 |
| 5 | F4 | Skill+Memory 统一检索编排；M3 History exact block 按需展开；统一预算和 snapshot | scope、时效、重放、索引重建与 context-rot 反例全绿 |
| 6 | F5 | Line/TUI 治理、上下文/历史披露透明度、冻结检索评测、打包与发布 | Release Stop C、最终独立审查、唯一最终全量和发布门禁 |

最终产品行为应同时满足：默认上下文短而新，Workspace 长期事实可治理，Skill 按需披露，M3 原文随时可
追溯但不会长期驻留；用户和模型都能知道“有什么、为什么命中、实际注入了什么、它在什么时刻有效”。
