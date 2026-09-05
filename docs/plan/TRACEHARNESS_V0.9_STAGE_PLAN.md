# TraceHarness v0.9 冻结阶段计划

> 状态：**已于 2026-08-29 冻结为 v0.9 路线；2026-09-05 补入 M3 压缩历史证据的按需披露合同；
> 尚未实现，也未获开工授权。**
>
> 前置：只有 v0.8 按当时真实源码完成、独立审查和发布后，才允许重新核对并批准本计划。
>
> 本文冻结 Skill、Memory、M3 压缩历史证据、渐进式披露与 RAG 的单一 Context Input 主线，防止
> v0.8 实施期间范围漂移。
> 它不表示当前已有这些能力，也不授权 commit、push、tag、release、联网或真实模型运行。

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

- 默认只给有界目录与 M3 摘要；原文只有用户或模型引用宿主已经披露的 exact `block_id` 后才进入一个
  Step 的 Context Input；模型不能提交任意 seq 范围或获得 EventStore handle；
- 多次压缩时由宿主递归展开 replacement 来源链，每层 fresh 校验 Session identity、source seq/digest、
  闭合 Turn 边界与允许披露的 Surface 事件类型；无法证明完整来源时 fail closed；
- History request 若以只读 Tool 形态暴露，普通 `tool/result` 只返回小型 receipt；原始历史 bytes 由下一
  Step 的 Context Input event/request snapshot 冻结，不作为普通 Surface 消息遗留到后续 Turn；
- 每个历史 block 带 `observed_through_seq`、来源时间/identity、可得时的 Workspace revision、freshness
  (`matched`/`stale`/`unknown`) 与 digest。`matched` 只表示所有已记录的相关 identity 仍匹配，不把环境依赖
  或非确定工具结果升级成当前验证；缺失当前身份时必须显示 unknown，不能把旧工具成功冒充当前验证；
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
| 最终注入 bytes | ContextInputSnapshot | 每个 Step admission 冻结一次 | 当前数据库 latest state |

跨边界时必须验证两侧 identity 和 owner，不能因为字段都叫 `skill_id` 或 `memory_id` 就默认属于同一事实。

## 4. 共同核心：Context Input 合同

### 4.1 `ContextInputSnapshot` 的可观察语义

准确类型名实现时可调整，但每个 Step 必须在 Provider 调用前冻结一个结构化上下文 receipt，至少覆盖：

- 当前 Workspace/Session/Step identity 与 leased Generation identity；
- canonical query 的来源身份和 digest；
- 每个 Skill/Memory/History block 的 kind、id、version/digest、provenance、scope 与 disclosure tier；
- **实际注入的有序精确 bytes**，而不只是“当时选了哪些 id”；
- retrieval policy/version、启用的 lane 与各自 config digest；
- 被资格过滤、去重、预算排除或未命中的结构化原因；
- Skill、Memory 与 History Evidence 各自配额、总 Context Budget、实际字符/Token 计量和剩余量；
- 最终 block 顺序和整个 Context Input digest。

注入点在 v0.9-F0 冻结为一条明确主线：

1. `AgentLoop` 取得当前 Composition Generation Lease 后，调用注入的 host Context Input service。该
   service 只通过只读 canonical source readers 检索并返回不可变 receipt/blocks，不拥有 Session append
   权限；`AgentLoop` 仍是唯一 Session event writer，并追加**恰好一条**当前 Turn/Step 的 Context Input
   event。它保存 receipt 与有序精确 bytes，只属于当前 Step，不是新的 `user/message`；
2. 随后才追加 `composition/snapshot`。该 event 的 seq 必须小于 composition event，现有
   `source_seq = composition_event.seq` 因而继续界定生成请求的全部输入；重复、缺失、顺序颠倒或
   Generation identity 不匹配均 fail closed，不把 source boundary 改成额外 digest 猜测；
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

### 4.2 authority 优先级与预算

上下文优先级固定为：

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

## 5. 通用检索与渐进披露管线

### 5.1 两次宿主过滤

“宿主过滤”具体分为两道：

1. **检索前 eligibility filter**：只让当前 Workspace、active Memory、当前 leased Generation 中仍有效
   的 Skill、兼容版本和宿主允许的内容进入候选语料；History 只列当前 Session、通过 format-2 parser 与
   invariant 校验的 replacement block；跨 scope 内容连向量相似度都不计算；
2. **注入前 verification filter**：对排名结果 fresh 复核 scope/status/generation/version/digest、去重和
   Context Budget；History 还要递归复核 provenance、允许事件类型与 freshness；任何漂移 fail closed 或按
   receipt 记为 unavailable，不能换成 latest。

这两道都由宿主确定性代码执行。模型、Skill 文本、Memory 文本、FTS 分数和 reranker 都不能绕过。

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

进入代码前至少冻结：

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
- UI、模型 proposal Tool 与真正宿主 writer 的权限分离。

### 7.1 F0 必测原型

- 同一 canonical facts/query/config 两次得到字节一致的 ContextInputSnapshot；
- host Context Input service 只能读取 canonical sources 并返回不可变 snapshot；临时授予它 Session append
  能力或让它直接写 selection/Memory/context event 时，owner 边界测试必须变红；
- 当前 Memory/Skill 或 History source 在 snapshot 后变化，不改变历史 request reconstruction；
- 连续两个 Step 分别检索不同内容时，第二个 request 只含自己的 Context Input；Surface replay 不含任一
  Context Input event，历史 reconstruction 仍逐字节相同；
- 上述连续 Step 至少一个必须是 Tool Call 后没有新 `user/message` 的真实续步；Context message 在两次
  request 中都位于全部 Surface messages 之前，不能落到 Tool result 后面；
- 模型请求一个已披露 History block 时，普通 Tool result 只出现有界 receipt，下一 Step 的 Context Input
  才包含精确原文；Turn 结束后的 Surface 与再下一 Step 均不自动携带该原文；
- 一层和多层 `surface/replace` 都能按原逻辑顺序、分页边界与 digest 重建允许披露的原始 Surface 消息；
  篡改 source、循环/向未来引用、非闭合边界、隐藏事件类型或跨 Session block 均在注入前拒绝；
- 同一旧工具结果分别绑定相同、不同和缺失 Workspace revision 时，freshness 稳定为 matched、stale、unknown；
  stale/unknown 文案和结构化 receipt 均不得宣称当前测试或当前代码状态；
- 篡改 Context Input event、request snapshot 或二者 binding/digest 任一侧时 reconstruction fail closed；
- 同一 Step 出现零条/两条 Context Input event、event 晚于 composition snapshot，或 event 的 Generation
  identity 与随后的 composition 不一致时，build/reconstruction 都稳定拒绝；
- block 正文包含伪 system/user header、边界标记、控制字符或嵌套引用时，renderer 仍保持同一 block
  identity/bytes，不能生成额外 ModelMessage 或改变后续真实用户消息；
- 跨 Workspace、revoked Memory、retired Generation Skill 和 digest 漂移均不能进入候选；
- 检索索引全部删除后可重建，canonical history 零变化；
- prompt 超额时 protected authority 不被 Skill/Memory/History Evidence 挤掉；
- secrets/approval/evaluator 形态扫描和明确拒绝规则不依赖模型自觉。

### 7.2 F0 退出条件

需要一份覆盖共同 Context Input/Retrieval 的 ADR；Skill lifecycle 与 Memory authority 若理由独立，可各有
一份 ADR。不得为普通 SQL 表或类拆分滥建 ADR。未能证明历史 reconstruction 不重跑 RAG 时不得进入 F1。

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

在同一个 EventStore/SQLite 中新增一个独立 Memory 领域和唯一 projector/service。精确事件名由 ADR
冻结，语义至少区分：

```text
proposed -> active -> superseded | revoked
```

- proposal 不是 active；模型只能提交 bounded candidate，不能批准；
- host approval 必须绑定 exact proposal digest、source evidence 和当前 Workspace scope；
- Workspace scope 从当前 Session/Workspace durable facts 由宿主推导，不能由模型/plugin payload 自报；
- supersede/revoke 追加新事实，不更新/删除旧 row；
- active view fresh replay、detached、fail closed，不缓存第二份 mutable truth；
- source 可指向人工输入、Session/ProductTask 等 durable evidence；未经证实的模型猜测不能冒充用户事实。

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
- 同 request id 不同内容、错误 source/scope/digest、重复 active identity fail closed；
- 跨 Workspace approval/proposal/source 引用在 append 前零写入拒绝；
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

冻结语料的唯一装载 owner 是现有 `evaluation/attempt.py::run_attempt` 主线：每个 attempt 在创建自己的
SQLite store 之后、构造 Product Chat host 之前，按 exact-key manifest 和内容 digest 使用生产
Plugin/Memory service
装载本 attempt 的宿主冻结 Skill/Memory 输入。所有相对资源都必须解析在 manifest root 内；完整 corpus
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

冻结选择是保留该评测，而不是默认延期；但若实现时无法由现有 `run_attempt` 在上述边界内装载语料，
评测整体延后，不以交付压力为理由新增平行 Runner。核心 deterministic retrieval tests 始终是实现门禁。

### 12.3 Release Stop C 与最终门禁

1. compileall；
2. Context snapshot/reconstruction、Plugin Skill lifecycle、selection、Memory authority、History Evidence
   展开/Step-scope/freshness、retrieval/index、Composition/RequestBuilder、Line/TUI 定向测试；
3. Plugin Activation/Generation/Lease/Drain、Session/Request、Budget、Product/Workflow/Review/Approval/
   Promotion/Evaluation 相邻回归；
4. SQLite derived index rebuild、fresh data dir、无 semantic extras 与带本地 extras 的离线安装；
5. collect-only、Ruff、diff-check、链接/围栏/章节 QA、secrets/本机路径/archive scan；
6. 每个 release stop 的 Finding 已关闭后做最终独立 P0/P1 审查；
7. 清零后只运行一次最终全量；
8. clean-input Wheel/sdist/source ZIP、外部示例 Skill Wheel、无 TUI/semantic extras 的 core 安装；
9. 授权真实模型 acceptance 只证明 receipt 注入、权限未扩大和生命周期闭合，不让模型自评质量。

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

- 最终独立计划审查已经结束；2026-09-05 经项目所有者确认的唯一范围修订是把 M3 History Evidence
  纳入同一 Context Input 主线。本文保持 Skill + Memory + History Evidence 同一 v0.9 主题和三个 release
  stop，不因范围观感拆成两套注入主线；
- v0.9 只有在 v0.8 完成并发布后才能按当时真实 HEAD 重新核对。重新核对用于发现代码事实变化，
  不是默认推翻已经冻结的产品目标；任何实现仍需项目所有者明确开工授权；
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
| 0 | v0.8 前置 | M3+M4 联合门禁、提交、发布，以发布 HEAD 重新核对本文 | v0.8 未发布则 v0.9 不开工 |
| 1 | F0 | ContextInputSnapshot、authority/budget、query/filter、History identity/Step-scope/freshness、重放 ADR | exact bytes 可重建，History 原文不进入后续 Surface |
| 2 | F1 | 现有 Plugin Generation/Lease 上的 typed Skill catalog 与只读资源 owner | 半发布、identity/digest、reload/drain 反例全绿 |
| 3 | F2 | Skill durable selection、exact+FTS、四级披露、可选本地 semantic/reranker 接口 | Release Stop A：Plugin/selection/Request P0/P1 清零 |
| 4 | F3 | Workspace Memory proposal/active/superseded/revoked append-only authority | Release Stop B：Memory authority P0/P1 清零 |
| 5 | F4 | Skill+Memory 统一检索编排；M3 History exact block 按需展开；统一预算和 snapshot | scope、时效、重放、索引重建与 context-rot 反例全绿 |
| 6 | F5 | Line/TUI 治理、上下文/历史披露透明度、冻结检索评测、打包与发布 | Release Stop C、最终独立审查、唯一最终全量和发布门禁 |

最终产品行为应同时满足：默认上下文短而新，Workspace 长期事实可治理，Skill 按需披露，M3 原文随时可
追溯但不会长期驻留；用户和模型都能知道“有什么、为什么命中、实际注入了什么、它在什么时刻有效”。
