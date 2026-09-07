# ADR-0043: Step-scoped Context Input、Skill 与可重建检索

- 状态：F0-A 设计已接受；F0-B 最小请求主线已接入，验收进度见设计合同。F0-C 与 F1–F5 尚未实现。
- 日期：2026-09-07。
- 范围：v0.9-F0-A；F0-B/C、F1–F5 继续按阶段开工，不由本文提前授权。
- 关系：扩展 ADR-0008 的内容身份与 ADR-0042 的历史来源合同；不改写其历史实现记录。
- 规范字段与验收清单：[`F0 设计合同`](../plan/TRACEHARNESS_V0.9_F0_DESIGN_CONTRACT.md)。

## 背景与设计冻结时的接缝

当前 [`AgentLoop`](../../src/traceh/runtime/agent_loop.py) 在 `step/start` 后取得 Composition Lease，
追加 `composition/snapshot`，由 [`RequestBuilder`](../../src/traceh/runtime/request_builder.py)
投影 Surface，再通过 Session dispatch permit 冻结 composed/dispatch request 与 Attempt。
[`RuntimeComposition.snapshot()`](../../src/traceh/kernel/composition.py) 的 revision 当前不含 Skill
catalog；[`CompositionGeneration`](../../src/traceh/runtime/composition_runtime.py) 的运行期编号
故意不进入持久请求。M3 原始事件尚在，但模型没有通用历史展开入口。

直接把检索内容塞进 system prompt、普通 Tool result 或可变 messages，会分别绕过选择边界、让旧原文
常驻 Surface，或使过去的请求无法重建。仅保存命中 id 也不能证明插件更新、Memory 撤销后当时的 bytes。

## 决定

### 1. 只有一条 Context Input 写入与请求渲染主线

宿主 Context Input service 接受当前 Step identity、同一 exact Lease 暴露的只读 catalog、canonical
source readers 和显式 policy，返回不可变 snapshot；它没有 Session append、Memory writer、Plugin
publish 或 Tool grant 权限。`AgentLoop` 仍只编排和拥有事件写入，不实现检索或领域审批。

顺序固定为 `step/start → Lease → context/input → composition/snapshot → RequestBuilder → model
admission → Session dispatch permit`。成功零命中也追加空 Context。Context 是该 Step 的冻结输入证据，
不是 Surface 消息或当前 Memory 的另一份权威。`source_seq` 仍等于该 Step Composition event 的 seq。

新增只读 Context reader 与确定性 renderer，由在线构建、离线 reconstruction、snapshot verification
共用；不复制 SurfaceProjector。reader 必须绑定当前 Session/Turn/Step 和精确 Composition event，不能
仅按 revision 向前找到另一 Step 的同内容快照。

三类参考内容合成一条 request-only user-role `ModelMessage`，置于全部 Surface messages 之前；
Composition 的静态 system prompt 保持独立。正文通过 canonical JSON string framing 转义并携带原文
UTF-8 length/digest；伪 header、控制字符和角色文字只能是字符串内容，不能创建第二条消息。即使 blocks
为空也使用同一 renderer 和固定宿主边界。request snapshot 保留实际 composed 与 dispatch request，
并绑定精确 Context seq/digest；核对失败即拒绝。这里只证明结构与控制权限，不能声称模型必定遵从。

事实、控制和预算分别判断：原 Product/Workflow/Promotion/Workspace owner 证明当前状态；批准 Memory
只证明已确认项目事实；Skill/History 是参考。START、Approve、Tool grant 等仍走原控制面。预算依次
保护宿主权威、当前输入与 fresh execution context、明确请求的 History、Skill、Memory；按原子片段
纳入，不截断安全限定句。固定 wrapper 也计入 Context 预算。

### 2. 内容身份持久化，资源身份留在 exact Lease

为 Composition 新增有界 `skill_catalog` descriptor manifest 与其派生 `skill_catalog_digest`，
共同纳入 `RuntimeComposition.snapshot()` 的 revision payload。digest 从同一 leased Generation 的 descriptor 得到，descriptor
涵盖 plugin provenance、Skill/version、section/resource identity 与内容 digest。空 catalog 也有
canonical digest。descriptor manifest 是持久 catalog 成员证明的唯一承载，digest 必须可由它重算；
不保存全部参考正文，只有实际选中的 bytes 进入 Context。历史重建核对成员关系，不加载当前 Wheel。

Context 的 `composition_revision` 与 `skill_catalog_digest` 是对 Composition 的关联引用，必须与
随后同 Step event 相等，不是独立 catalog registry。Skill block 另保留 exact 来源与 bytes。新 catalog
进入 revision 是本 ADR 的明确协议决定；ADR-0008 的“生命周期编号不持久化”仍成立。同内容、不同
进程或 Generation 生命周期编号，不能仅因此改变请求 fingerprint。

F1 在现有 Activation transaction 中注册 typed Skill candidate；成功发布后属于 Generation，资源
访问与 cleanup 复用 ActivationSet/Lease/Drain。selection 不调用 `register_prompt`、不 publish、不
drain、不启用插件、不授 Tool。F2 以同一 EventStore 的 Session-scoped selection stream 记录 exact
catalog/Skill 选择；没有合法记录就没有显式选择。项目级推荐是可选的宿主 policy 输入，不借 selection
建立项目权限。retired catalog 的旧选择不得静默改绑新版本，重选前记为 unavailable。

### 3. 冻结失败前缀，而不强迫所有 Step 都产生请求

进入构建／重建才要求唯一 Context。Lease 或读取失败可留下零 Context；Context 已提交后可以没有
Composition；Composition 后构建失败／预算拒绝可以没有 Request/Attempt。它们是合法终止前缀。
`request/snapshot` 仍与首个 `model/attempt-start` 在现有许可证主线内一起提交。

可能已提交的 append 先收敛 owned worker，再按精确 event identity/payload fresh 对账为
True/False/unknown。unknown 不重写、不派发、不伪称成功。后续 Provider retry 复用同 Step 的 Context、
Composition、source_seq、composed/dispatch bytes；只增加既有 Attempt ordinal。恢复只处理已经发生的
事实、收敛合法前缀，不重新检索、不补造 Context，也不续用旧 Turn 的历史请求。

### 4. History 是有期限的读取，不是新的 Memory

只支持当前 Session 的 format-2 replacement。宿主从已披露目录生成 exact block identity；模型只能
引用该 identity 和有界 page cursor，不能给 seq 范围、文件路径或另一 Session。递归展开复用 M3
`surface_prefix()` 和严格 parser，核对每层 source identity/digest、闭合 Turn 和允许消息类型。

原文只在目标 Step 的 Context 中出现。模型 Tool result 只返回有界 receipt，绑定原调用、来源 Step、
同 Turn 的直接后继 Step 位置。accepted 表示请求已登记，不保证后继 Step 存在；该位置因 max_steps、
失败、取消或 Turn 结束没有发生时直接失效。后继 Step 已开始但在冻结前失败，也不能由再下一 Step
接管。消费／过期从 Session 事实派生，不增 pending 缓存或第二状态机。

用户显式历史请求通过宿主 typed input 绑定当前真实用户消息和该 Turn 首 Step，同样只消费一次。
普通自然语言 query 只能筛目录，不能自行扩大原文读取资格。历史中的 Tool success 仅证明过去；
freshness 按已记录的 Workspace identity/revision 与当前宿主证据比较，缺失为 unknown、不同为 stale，
全部相关记录匹配才是 matched，仍不等于当前代码通过验证。

### 5. exact + FTS 共用 Store，但没有新的事实源

F2 在同一个 `events.sqlite3` 进行物理 schema 1 → 2 的破坏式切换，精确增加 derived index 对象。
不另建 sidecar 数据库，不向插件或 Context reader 暴露连接。Store 拥有 SQL、线程任务、事务、busy、
关闭和取消收敛；index service 只经受控入口维护可重建内容。F0-B 的 Session 协议变化不提前创建 FTS。

索引绑定 scope、source head/content digest、leased catalog digest、tokenizer/ranker/config digest。
正常开库不偷偷重建，已知 manifest／派生行逻辑缺失、过期或不一致记为 unavailable；表／shadow／DDL
缺失或物理损坏仍开库拒绝。显式宿主 rebuild 以有界事务原子发布，
读者不能消费半成品。未知 schema 对象和 canonical event 损坏仍拒绝，不能以“索引可重建”放宽。
backup/restore 必须通过 Store 的一致快照边界处理同库全部对象，恢复后重新核对来源，不能裸拷 WAL
中的主文件或把索引恢复写成 Memory 事件。

eligibility 在评分和 top-K 前确定。禁止直接用跨项目 FTS 全库 `bm25()` 统计再过滤结果：其他项目的
文档数/词频也会影响排名。FTS 提供受作用域约束的 lexical 命中，宿主仅对 eligible corpus 的派生词频
计算版本化 BM25；候选上限前先完成资格过滤，超资源界限报告 unavailable，不返回看似完整的半次检索。
exact、lexical 与可选 semantic 的融合、排序 tie-break、中文分词方案和配额见字段合同。

### 6. 明确切换和拒绝入口

F0-B 为新 Session 冻结 Context 协议标记和新的 Composition/Request exact-key 集；缺标记的旧 Session
在 open/resume/recover/inspect/replay/构建入口明确拒绝，不能补空 Context。EventEnvelope 的版本不被
拿来猜测 Session payload。F2 再执行上述物理 schema cutover；旧数据使用旧发行版或新的数据目录，
本轮不授权迁移、双 reader、自动改写或删除。具体字段及入口见合同 §2。

### 7. 评测仍由唯一 attempt owner 装配

[`evaluation/attempt.py`](../../src/traceh/evaluation/attempt.py) 当前先建 Product host、后在 `_prepare`
建 requester Session。F5 在同一 owner 中调整为 Store/Runtime → Session/宿主 scope binding → 生产
Plugin/Memory seeding → Product host → 原确认与执行流程。tool-free requester 使用该 attempt 的 source
repository 作为 workspace，使项目绑定可经实际 source resolver 核对；不再用独立 `rw` 目录伪造归属。
任何部分失败继续由原 attempt 收敛，不能
加第二 Runner。模型、coder Workspace 不持有完整 corpus、人工 relevance judgments 或 evaluator。

每个唯一 Step 是一次注入观测，retry 不重复。候选 Recall/MRR 与实际注入 precision 分开；失败、
unavailable 和 unproven 进入明确分母。具体 corpus、质量阈值、可选模型和资源限额在 F5 看到 candidate
结果前预注册；缺值只报告测量，不能声称过门禁。跨项目泄漏通过线固定为 0。

## 被否决的方案与代价

- `register_prompt`、普通 Surface Tool result 或 mutable messages 注入：破坏选择、时效或重建。
- 保存 id 后重跑检索、以当前 Wheel 补原文：历史证据随 latest 漂移。
- 持久化 Generation 编号：混淆资源 owner 与可跨进程核对的内容。
- 一个万能 Context/Memory 状态流：混淆领域事实与请求证据；项目 authority 独立见 ADR-0044。
- 第二数据库、无约束 schema 容忍或静默迁移：扩大存储与旧数据生命周期。
- 全库评分后过滤、先跑结果后调阈值：分别污染 scope 排名和评测结论。

代价是 Session 与 SQLite 分阶段明确拒绝旧格式，以及 exact bytes 在来源与请求证据中有受预算约束的
重复保存。重复内容必须交叉验证；当前事实仍回原 owner fresh 读取。F0-B/C 必须证明真实请求链、
失败前缀与 History 时效，F1–F5 才证明各领域实现与质量，文档接受不能替代这些执行证据。
