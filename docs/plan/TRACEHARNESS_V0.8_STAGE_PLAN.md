# TraceHarness v0.8 冻结阶段计划

> 状态：**已于 2026-08-29 冻结阶段目标、顺序与边界；F0–F4 原实现已提交并通过既有停止点。F5 曾对当时候选完成全局独立审查与 `2496 passed, 7 skipped` 全量，但发布前真实体验随后直接替换了 F4 TUI presentation，旧 Release Stop C 与全量只保留为历史证据。replacement 审查 Finding、Product requester START 前权限、Plugin ActivationSet eager-cleanup 死锁和终态任务→新 Proposal 身份交接都已在原 owner 根修。2026-08-31 的重复真实失败又由 SQLite + exact request replay 定位为 OpenAI-compatible Tool arguments 使用非 JSON 双三引号 multiline；现有 Provider 边界已按 ADR-0038 增加受冻结 Tool schema 约束的唯一 lexical normalization，并严格拒绝 `NaN`/正负 `Infinity`。TUI 失败投影同时绑定 exact Agent/Session/create-request/message 与实际 open Turn，只接受唯一 runtime error 和 `reason=failed` terminal；100–109 列现统一使用单栏，110 列才进入 52 列 facts 双栏。浅色 D1–D7 布局已直接替换旧 presentation，没有兼容分支。修复后已从全新目录亲自走通一次真实 `qwen-plus` TUI Proposal → START → auto/single → Review → Approval → bare Promotion，推广后的 4 项 unittest 通过且 Session/Budget/Workspace 全收敛。Provider、TUI、失败证据与跨 owner 复审曾清零 P0/P1/P2，最新相邻 owner 回归 `251 passed`；Textual gate 焦点交接也已在原 presentation owner 根修。随后同一 TUI 又原地补齐 fresh `Ctrl+T` 角色对话、fresh `Ctrl+P` 完整身份与 Review evidence，没有旧路径或兼容层。此前 `2555 passed, 7 skipped`、退出码 0、耗时 `3078.80s (51:18)` 的完整全量因此只保留为该补齐前的历史证据。用户继续体验发现批准完成后左栏没有宿主结果反馈的 P2，现已在同一 adapter 消费 typed `ProductCommandResult.advance` 根修，UI 提示不写 Session/SQLite；旧逻辑反例精确失败、恢复后转绿。当前 `54` 项 TUI、`215` 项相邻回归、`2575` 项 collect-only 已通过；前一轮独立短复审为 `P0=0/P1=0/P2=0`，本次小修尚待短复审。复审与用户确认后才运行唯一最终全量，之后才可进入 clean-input 打包、双形态离线安装、完整真实 Provider 网格、tag 与 release。**
>
> 冻结基线：已发布 `v0.7.1`，HEAD
> `194f44fe84ecb9adb85fc1d48d182d364bb94f45`。
>
> 本文是 v0.8 的唯一阶段计划。冻结只批准本文范围与阶段顺序；除明确标为已实现的 F0–F4 外，不表示
> 后续能力已经存在，也不授权
> commit、push、tag、release、真实 Provider、外部网络或秘密读取。每个阶段开工前仍须重新核对
> HEAD、两份上下文、ADR、源码与测试；真实代码始终高于本计划。

## 1. 单一产品目标

v0.8 的单一目标是：**把已能完成 ProductTask 的 v0.7.1 主线，变成一个更可靠、可长期使用、
对人可观察的本地产品。**

实现顺序固定为：

```text
终端异常安全 + Attempt/Budget 准入事实先自洽
  -> SQLite 成为唯一生产 EventStore
  -> 同 Provider/同模型的有界重试
  -> UI 无关 Chat 驱动与只读观察
  -> 可选 Textual TUI
  -> 独立审查、真实验收与发布
```

这不是四条平行产品线。SQLite 解决本地事实存储；retry 解决瞬时 Provider 故障；Chat 驱动和
TUI 只改善同一 ProductTask 的交互。任何一层都不得另造 Runtime、Workflow、ProductTask 状态、
Benchmark Runner 或事实源。

## 2. 冻结前审查结论

### 2.1 已由 v0.7.1 解决，不再列为 v0.8 工作

上一轮独立审查所依据的是 v0.7.0。以下问题已在 v0.7.1 的真实源码、反例和发布门禁中处理：

- ProductTask 的真正启动授权已移到独立宿主 Console `START` 手势，模型确认 Tool 不再拥有启动权；
- `AgentLoop` 的 Attempt/Step/Turn 取消收尾已改为一个 owned finalizer，重复取消不能提前返回；
- L4 目标 venv 在 Python 3.13 上使用明确的 `venv` scheme；
- Windows 嵌套 worktree 身份已缩短为 `ws-<完整 SHA-256>`，没有截断摘要或 fallback。

这些事实进入 v0.8 相邻回归，但不得重复实现第二次。

### 2.2 当前结构不一致与 v0.8 新增合同

F0 开工时的 ADR 与正式上下文已经把 Attempt 定义为“一次真实 Provider 调用”，不是本计划新发明的
严格化。基线公开路径是：`AgentLoop` 先写 `request/snapshot` 和 `model/attempt-start`，随后才调用
`BudgetedLlmRuntime.invoke()`；而 Budget runtime 会在内部检查余额、预留，并可能把
`max_output_tokens` 替换成更小的最终值。因此当前实现与现行合同之间已经可能出现：

- Budget 准入失败，但 Session 已经声称一次 Model Attempt 开始；
- durable request snapshot 只能证明 Composition/Surface 组装出的请求，不能证明最终交给 Provider 的
  dispatch request。

第一项不是 SQLite 或 retry 才新产生的问题。F0 已先用公开反例固定错误结果，再在拥有该不变量的既有
边界修复。第二项是 ADR-0034 已接受的组装请求证据边界，F0 为 retry 新增“每个
Attempt 必须绑定同一最终 dispatch request，并各自拥有 reservation”的合同。现有缺陷与新增合同必须
分开验证，不能一起藏进 Provider retry 循环。

另一个当前 P1 与上述状态机无关：`traceh chat` 的最终异常行会把 `str(error)` 原样写入终端，而
OpenAI-compatible adapter 的 HTTP 异常可能包含第三方响应正文。它绕过现有单行清洗漏斗，允许控制字符、
换行或双向格式字符进入与人工 Approval 共用的终端。该问题作为 F0 的第一项窄修复先收敛；Provider
异常进入 durable `model/attempt-end`/`runtime/error` 的 P2 则由 F2 的 typed adapter 边界根治。

冻结审查中提到的“现有 composed snapshot 不是 wire request”和“Attempt 尚未 durable 绑定
reservation”不作为当前 P2：前者是 ADR-0034 已接受但证据不完整的现行合同，后者在当前单
Attempt/Step 下没有可观察错误。二者仍是 F0/F2 必须完成的新合同前置。

## 3. 冻结边界

### 3.1 一条 durable 事实主线

- SQLite 切换后成为唯一生产 EventStore；不保留 JSONL reader、dual write、迁移器、旧字段别名、
  自动导入或“SQLite 打不开就读 JSONL”的 fallback；
- `InMemoryEventStore` 只保留为确定性测试替身；`JsonlEventStore` 从生产与公共导出中删除，旧目录
  只检测并明确拒绝，绝不读取、移动、改写或删除；
- ProductTask、Workflow、Budget、Workspace、Artifact、Promotion 和 Session 仍各自解释自己的
  stream；SQLite 只保存事实，不成为第二领域状态机；
- `PublishingEventStore` 仍只在底层 append 成功后发送低延迟提示。Feed 无 replay、不是事实源，
  UI 不能从 Feed payload 直接更新权威状态。

### 3.2 Attempt 是一次真实 Provider dispatch

- 一个 Provider 网络调用恰好对应一个 Model Attempt；一个 Step 可以包含多个 Attempt；
- Budget 拒绝发生在 dispatch 之前时，不得伪造 `model/attempt-start`；
- 现有 `request/snapshot` 对 Composition/Surface 请求及其 fingerprint 的重建语义继续保留；current
  schema 另行保存最终 dispatch request 及其 fingerprint，二者不能用一个含糊的“request”字段互相覆盖；
- 每次 Attempt 有独立 identity、ordinal、start/end、Provider outcome、usage quality、Budget
  reservation/charge/settlement，并绑定同一个 dispatch fingerprint；失败 Attempt 的成本不会因后续
  成功而消失；
- 所有 retry Attempt 使用同一个 Provider、model、Composition Lease 和**同一份最终 provider-bound
  request bytes**；retry 不缩短输出上限、不改 prompt、不增删 Tool、不切换模型；
- 若余额不足以再次预留同一请求，停止 retry 并 fail closed，不能为继续重试偷偷缩小请求；
- recovery 只收敛已经 durable start 的 Attempt；重启后不自动恢复 retry、不重新 dispatch。

### 3.3 UI 不获得权限

- Line CLI 与 TUI 都适配同一个 UI-neutral Chat 驱动和 ProductTaskControlPlane；
- v0.7.1 的独立宿主 `START`、人工 Approval 和 Promotion digest 绑定保持不变；
- 观察 API 必须纯读，不执行 reconciliation、不补事件、不启动 Workflow、不持有后台 owner；
- TUI widget、内存列表和模型文字都不是状态；重启只从 durable facts 恢复；
- UI 只能调用既有宿主动作，不能自动 approve/promote，也不能把 evaluator、Verifier 或 Budget
  authority 放入模型上下文。

### 3.4 资源所有权

- `build_default_runtime` 及内部 runtime factory 不再隐式创建 EventStore，store 参数成为必填依赖；
- store-open/close 的真实生产 owner 必须按调用路径完整盘点，不能再概括成“两个 root”：
  CLI composition boundary 覆盖 chat/run/resume/replay/inspect/recover/sessions/compact 等实际入口，
  Evaluation 的每个 attempt 独立拥有一份实验 store，Evolution `plugin compare` 的每个 comparison case
  也独立拥有自己的 store；
- CLI 内部提取一个显式 store 作用域供各子命令复用；Evaluation attempt 与 comparison case 使用同一
  store-open/close 合同，但各自保留资源作用域。Product host 由这些入口装配，不另造事实源；
- Runtime 与各领域 service 只借用注入的 store，不关闭它；不为了测试把 `close()` 塞进通用
  `EventStore` protocol，也不增加无操作 ownership fallback；
- 备份是宿主操作，不是调用方复制一个看起来像数据库的文件。WAL 模式可能同时存在 `-wal`/`-shm`；
  一致备份必须使用 SQLite backup API 或等价的受支持事务边界输出到全新目标，并记录 schema/version
  receipt；
- close、backup、append/read 的并发准入和取消必须由同一 owner 定义。重复取消不能留下 detached
  thread、connection、transaction 或 backup worker。

## 4. 明确不做

v0.8 不实现：

- Provider/model fallback、自动降级、跨 Step 重试或 Provider SDK 隐式重试；
- JSONL 迁移、兼容读取、dual schema、projection checkpoint 或 event upcaster；
- Agent cold recovery、stale claim takeover、跨进程 Activation/Workspace/Promotion lease；
- token streaming、Web UI、移动端、完整历史 Dashboard；
- PostgreSQL/MySQL、远程数据库、云同步或多用户权限；
- Skill、长期 Memory、RAG 或向量数据库；
- OS sandbox、容器或插件进程隔离；
- 第二 Chat backend、第二 Event bus、第二 Eval/Benchmark 命令或 Runner。

Sandbox 仍是独立 hardening 主题：只有将来要运行不受信 Skill、插件或命令时才成为前置。v0.8
可以记录 subprocess/filesystem/network 威胁边界，但不得提前实现半个沙箱。

## 5. v0.8-F0：当前 P1 收敛、Attempt 准入与请求快照

### 5.1 先关闭终端异常注入

第一项实现必须保持窄而完整：所有 `traceh chat` 异常展示都复用现有 terminal-safe 单行清洗漏斗，
对内容做控制/格式/分隔字符惰性化和有界截断；异常类型也按同一规则处理，不能只清洗 message。
Provider 返回含换行、ANSI/OSC、双向覆盖、超长正文和形似 Approval 文本的确定性 HTTP 错误时，公开
Chat 只能输出一条有界惰性错误行，不能伪造任务状态或后续提示。临时移除这条清洗，反例必须真实恢复
终端注入；修复不改 Provider retry、Budget、Session schema 或 Approval 权限。

### 5.2 冻结两阶段 Model Runtime 边界

实现必须在 ADR 或正式设计决定中冻结以下语义；准确方法名由真实源码决定，但不能继续用一个
`invoke()` 同时隐藏 Budget 准入、最终请求变换和 Provider 副作用：

```text
prepared
  -> admitted(final dispatch request + attempt reservation)
  -> composed/dispatch request evidence frozen
  -> attempt-start durable
  -> dispatched
  -> outcome-recorded
  -> budget-settled
  -> step-closed
```

- `prepared` 是 RequestBuilder 根据 Composition/Surface 得到的 composed request，还不是 Attempt；
- Budget owner 执行 admission，返回最终 dispatch request 与本次 Attempt 的 reservation handle；首次
  admission 可以按现行 Budget 规则确定输出上限，后续 retry 只能完整预留已冻结请求，不能再次缩小；
- `AgentLoop` 仍是唯一 Session Event writer；首次 admission 后只写一条同时含 composed 与 dispatch
  证据的 current-schema snapshot，再写本次 `model/attempt-start`；后续 admission 引用同一 snapshot/
  dispatch fingerprint，只新增自己的 Attempt start。snapshot/start 写入失败必须通过本次 reservation
  handle 补偿；
- `dispatched` 的线性化证据是 `model/attempt-start` durable 后才允许调用 Provider；若 start 结果
  unknown，不得调用 Provider；
- dispatch 只消费已经 admitted 的 concrete handle，不再次 reserve、不修改请求；handle 必须绑定当前
  Composition Lease 解析出的同一个 Provider 对象与 host Attempt，Budget 只挂接 accounting lifecycle，
  不拥有可覆写的 Provider dispatch；Provider outcome 返回后由同一 Budget owner charge/settle；
- `outcome-recorded` 保存稳定 typed outcome 和 usage quality，不持久化 raw response body、任意异常正文、
  traceback、秘密或本机路径；
- settlement 结束前不能把本次 Attempt 当成完成。取消/失败组合仍保留原错误和 cleanup 错误。

这是一条两阶段 public runtime 协议，不是让 Budget service 直接写 Session Event，也不是新增第二
AgentLoop。`request/snapshot`、`model/attempt-start` 与 Budget reservation identity 采用一套 forward-only
current schema；F1 切换 SQLite 时旧 JSONL Session 明确拒绝，不写 upcaster、别名或兼容 reader。

attempt-scoped reservation 会取代 ADR-0027 §5 由 Step-scoped reservation 承担的反重复派发互锁，
因此 F0 必须新增 ADR 或明确修订该决定：Budget reservation 只持有/结算本次 Attempt 的费用，不能单独
成为 dispatch 权限；真正的派发许可由现有 Step 执行 owner 在 Session stream 上成功追加
`model/attempt-start` 的 CAS 线性化点取得。只有同一 live owner 在上一 Attempt 已 durable 结束且被
typed policy 判定可重试时，才可为下一 ordinal 申请新 reservation；recovery 只闭合 open Attempt/Step，
不创建 retry 权限。若当前所有权无法证明这一点，F0 停止，不以 attempt UUID 代替 owner。

### 5.3 Attempt 与 snapshot 的绑定

`request/snapshot` 同时保留两种不可混同的证据：

- **composed request/fingerprint**：继续供现有 `verify_request_snapshots` 从 Composition/Surface 重建；
- **dispatch request/fingerprint**：保存 Budget admission 后真正允许发送的 provider/model/参数/消息/
  Tool schema，并供 Provider dispatch 与 retry 绑定。

每个 Attempt 至少能从 durable facts 证明：

- 它属于哪个 Turn/Step、ordinal 是多少；
- 它引用哪条 request snapshot（seq 或稳定 identity）及 dispatch fingerprint；
- Provider/model 与 snapshot/Composition 一致；
- reservation identity 由 attempt identity/ordinal 派生并与 Attempt 一致，同一 Step 的不同 Attempt
  不能复用 reservation；
- start/end 一一对应、ordinal 连续，终态互斥；
- 多 Attempt 的 provider-bound request bytes 完全相同。

`attempt_id` 与 reservation handle 由 runtime owner 显式传递，不塞进 `ModelRequest.metadata`，避免改变
composed 或 dispatch fingerprint。旧的 `agent/session/turn/step` reservation 派生规则必须同步切换，
否则第二次 Attempt 会与第一次冲突；不保留旧规则 fallback。

### 5.4 F0 必测与反向验证

- Budget 为 0 或不足时 Provider 调用数为 0，且没有虚假的 Attempt start；
- Budget 将输出上限裁成最终值时，composed snapshot 仍可由 RequestBuilder 重建，dispatch snapshot 与
  Provider 实收请求逐字段一致；
- 同一 Step 两次 admission 得到两个不同 reservation identity，而两个 Attempt 的 dispatch fingerprint
  完全相同；临时恢复旧 reservation 派生规则时第二次预留必须稳定冲突；
- 两个执行 owner 对同一 Step 并发 admission 时，只能一个 `model/attempt-start` CAS 获得派发许可；
  失败 owner 返回前释放自己的 reservation，Provider 调用总数为 1。临时移除 Session CAS/Step owner
  守卫时必须真实出现两次外部调用；
- 已 durable start 的 Attempt 在进程死亡后只被 recovery 保守闭合，新 owner 不自动创建下一 ordinal；
- snapshot append、attempt-start append、reserve 和 settlement 在 success/failure/cancel/unknown 窗口
  都收敛，不泄漏 reservation 或后台 append；
- 篡改 snapshot identity、attempt ordinal、Provider/model 或 reservation binding 时 replay fail closed；
- 公开注入 Runtime 返回“声明 request 正确但替换 Provider”的普通 admission，或返回在 dispatch 时改写
  request 的 Admission 子类时，两个 Provider 调用数都必须为 0、不得写 snapshot/start；bounded Budget
  已预留时还必须 RELEASED；
- 临时恢复当前“先写 snapshot/start，再由 Budget 裁请求”的顺序，新反例必须真实复现错误结果；恢复
  正确实现后再过相邻回归。

### 5.5 F0 停止点

终端窄修复和 Attempt/Budget 两阶段协议可分成两个可审查提交，但都属于 F0。独立审查必须先清零
F0 的 P0/P1；该停止点不要求立刻发布一个版本，但未清零前不得实现 SQLite 切换或 retry。

### 5.6 实现结果（2026-08-29）

F0 已按 [ADR-0035](../adr/0035-two-stage-model-admission-and-session-dispatch-permit.md) 落地：

- Chat 的最终异常类型与正文分别经过现有 bounded single-line sanitizer，不能用换行、控制序列、
  双向格式字符或超长正文伪造 Product/Approval 行；
- `LlmRuntime.admit()` 冻结最终请求，`LlmAdmission.dispatch()` 才能跨 Provider 边界；bounded Token
  admission 只创建 PENDING Attempt reservation，败选或未取得 Session permit 时由同一 handle 释放；
- `AgentLoop` 只接受 concrete `LlmAdmission`，并在写 Session 事实前核对它绑定 active Composition 的同一
  Provider 对象与 host Attempt；真实 `Provider.complete()` 固定由基类使用该 request 调用，Budget 由
  accounting hook 包围 START/settle/abort，不再通过 Admission 子类接管 dispatch；
- 首个 current-schema `request/snapshot` 同时保存可重建 composed request 和 exact dispatch request，
  并与 `model/attempt-start` 在一个 Session CAS batch 中持久化；每个 start/end 重复 ordinal、snapshot
  seq、dispatch fingerprint 与 reservation identity；
- Attempt id 每次 admission 独立生成，reservation 由完整 Attempt identity 派生；Budget 不再抢先成为
  dispatch lock，Session CAS 是唯一派发许可；
- live failure/cancellation 会 fresh read 并按 Attempt → Step → Turn 收敛，commit-return unknown 不调用
  Provider；cold recovery 仍只闭合既有 open Attempt，不创建下一 ordinal。

确定性公开反例已覆盖零 Token、Budget 裁剪、双 owner 并发、append 前取消、commit-return unknown、
证据篡改、恢复、Provider swapping 与 dispatch-time request rewrite。后两条在修复前都真实完成 Turn；
修复后均在 Session 写入前以 `model-admission-binding-mismatch` 拒绝，Provider 调用为 0，bounded hold
收敛为 RELEASED。其余反向验证把 reservation 临时退回 Step-scoped 后第二次 admission 稳定冲突；把
Session owner/CAS 守卫移除后同一 Step 真实调用 Provider 两次；把顺序退回“先记 Attempt、后做
Budget”后零 Token 路径重新留下虚假 start 并触发 durable Budget evidence mismatch。保护均已恢复。
上述结论记录的是 F0 提交时的边界；F1 的 SQLite 破坏式切换见下一节，retry、driver/TUI 和 v0.9
能力仍未改变。

独立复审清零 P0/P1 后执行最终全量。第一次全量在 53% 处发现唯一确定性遗漏：
`test_real_turn_keeps_one_generation_during_publish_and_rebuilds_requests` 仍读取已被 current schema 删除的
`request/snapshot.provider/model/request` 顶层旧字段。没有为它恢复别名或 compatibility reader；测试改为
同时核对唯一新格式的 `composed_request` 与 `dispatch_request`，定向通过。修复后的确认全量收集口径为
`2426`，进度到 100%、退出码 0，只有 5 个既有 skip 标记。全程未联网、未调用真实 Provider/API、未读
`.env`，也未另跑 Wheel/L2-L4；该次 F0 门禁时 F1-F5 尚未开始。

## 6. v0.8-F1：SQLite 唯一生产 EventStore

> 实现状态：stdlib `SqliteEventStore` 已成为唯一生产 backend；Runtime factory 改为显式借用 Store，CLI、
> Evaluation attempt 与 Evolution comparison case 各自拥有 open/close；旧 JSONL、mixed/schema/link/
> corruption 均 fail closed，backup/restore、跨进程 CAS、异流 busy/有界等待和取消/关闭收敛已由确定性
> 测试覆盖。设计决定见 [ADR-0036](../adr/0036-single-production-sqlite-event-store.md)。本阶段未实现 retry、
> driver/TUI 或 v0.9 能力，也未运行计划留到 Release Stop B 的完整 pytest。

本阶段门禁事实：`collect-only = 2446`；SQLite/EventStore/Feed 直接组 `100 passed, 2 skipped`，
Session/Runtime/Agent `608 passed`，插件组合 `184 passed`。跨域大组首次 `692 passed, 2 failed, 2 skipped`，
两项仅为 F1 合法修改的 `AgentLoop`/`AgentRuntime` 保护摘要仍钉在 F0，更新两个命名摘要后对应文件
`90 passed`；CLI + comparison 首次 `537 passed, 1 failed, 1 skipped`，修正“内存 seed、SQLite replay”
夹具后聚焦组 `38 passed`。Release Stop A 首轮复审发现额外持久 trigger 可让 append 返回成功却删除事实，
以及未知 DELETE-mode 数据库会在拒绝前被切成 WAL；exact schema gate 现在核对全部持久对象与规范化 DDL，
schema/history 也先于 WAL 配置验证。两条公开反例分别反向重现 `returned=1, head=1, replay=0` 和拒绝后
数据库 SHA 改变。第二轮又发现普通读写验证连接会先自动恢复 unknown hot rollback journal；现在既有库
先用 `mode=ro&immutable=1` 只证明 frozen exact schema，只有证明通过后才授权普通连接恢复、完整验证并
启用 WAL。真实崩溃反例证明 unknown 主库与 journal bytes 全部不变，同时 current-schema hot journal 仍
能恢复和 fresh replay。移除 authority probe 会稳定重现数据库 SHA 改变。保护恢复后的 review-fix 相邻组为
`155 passed, 2 skipped`；连同既有六类保护共九项均逐项反向验证并恢复。compileall、修改范围 Ruff、diff
与文档 QA 通过。未运行完整 pytest、联网、真实 Provider、Wheel/L2-L4，也未读取 `.env`。Release Stop A
最终复审已清零 P0/P1，F1 到此结束并允许下一步实现 F2；完整全量仍留到 Release Stop B。

### 6.1 schema 与线性化合同

使用 Python stdlib `sqlite3`，由唯一 store-open/ownership 边界创建。至少冻结：

- `build_default_runtime` 及内部 factory 缺少显式 store 时稳定失败；CLI 与
  `evaluation/attempt.py`、`evolution/comparison_probe.py` 分别负责自己创建 store 的作用域、关闭顺序和
  错误传播；
- 单个数据目录只有一个 current schema version；unknown/older/newer 均稳定拒绝；
- stream identity 与 seq 由数据库唯一约束和事务共同保证；
- `append(expected_seq)` 在同一事务内检查 head、写入整组事件并提交，跨进程并发只能一个成功；
- `read(from_seq)`、`head()`、`list_streams(prefix)` 的排序、过滤和 detached ownership 与当前
  `EventStore` 合同一致；
- payload 用唯一 canonical JSON 边界编码；非法 row、seq gap、重复 seq、非法 Envelope 不被修补；
- busy/timeout 只产生稳定 Store 错误，不在内部无限 retry，也不把 lock contention 当成 CAS 成功。

SQL 表名、索引、journal mode、connection/thread 模型和 busy 数值由最小原型与确定性测试决定，不能在
计划里写成未经证明的默认。

### 6.2 单库写入串行化、busy 与 durability

JSONL 当前按 stream 分锁，而单个 SQLite 数据库的 writer 会跨 stream 串行化；这是 F1 的主要并发合同，
不能只测“同一 stream 的 expected seq CAS”。普通的同进程多 stream 竞争由 store 内部做**有界等待与
串行化**，不得直接变成 Agent/Workflow 失败；超过明确上限才返回稳定 Store availability error。
Provider retry 层永远不处理 Store busy，调用方也不得无限重试或把第二次 append 猜成安全。具体
busy timeout、connection/thread 模型和是否需要进程内 writer gate 由最小原型决定，成为显式、文档化、
可测试的 store 配置，不从某台机器或测试时序偷取默认。

`Durability.BATCHED` 同时作为普通合同清理删除：所有当前 caller（包括每次 Attempt 仅出现一次的
`assistant/chunk`）统一走唯一 append/commit 合同；`PublishingEventStore` 只在底层提交成功后发布提示。
当前 Provider 没有真实 token streaming，这项清理不是 F1 的主要破坏风险；未来真正 streaming 必须另行
设计可证明的批处理协议。ADR 仍须说明 SQLite commit、WAL 与平台断电保证的边界，不能把普通测试无法
证明的 crash durability 写成事实。

### 6.3 legacy 与备份

- 新目录创建 current SQLite；已存在 current SQLite 正常打开；
- 发现旧 JSONL 痕迹但无合法 SQLite，稳定提示用户选择新 data dir；
- JSONL 与 SQLite 混合目录、数据库 schema 不一致、数据库路径被链接替换均 fail closed；
- legacy 文件零读取、零移动、零删除、零自动转换；
- 备份只通过宿主持有的一致性 backup 操作写入一个事先不存在的目标；不宣传 raw copy 或“永远只有
  一个物理文件”。恢复必须先验证 schema/version 与完整性，不能覆盖活跃数据目录。

### 6.4 F1 必测与反向验证

- 两个进程/独立实例对同一 stream、同一 expected seq 竞争，只提交一组连续事件；
- 单进程 N 个 Session/领域 stream 与 Feed 订阅者并发写入时，普通竞争全部有界串行化、各流顺序正确、
  Feed 只在提交后提示且没有非确定性 Store error；测试使用 Gate/Barrier，不用 `sleep()` 猜时序；
- 两个进程写不同 stream 时，锁等待上限、成功/timeout 结果和错误 owner 与 ADR 一致；临时把 busy
  配置恢复为立即失败时，真实多角色 ProductTask/等价公开并发反例必须变红；
- commit 已发生后调用方被取消，调用方返回前 worker 收敛，fresh replay 能判定 may-have-committed；
- commit 未发生、已发生和无法判断三种结果不坍缩；
- caller 修改 append/read 返回 payload 不改变历史或另一读者；
- prefix 枚举不泄露其他 stream，结果确定排序；
- legacy/mixed/version/link 拒绝不会产生空白新数据库掩盖旧历史；
- chat/run/resume/replay/inspect/recover/sessions/compact、`evaluation/attempt.py` 与公开
  `traceh plugin compare` 共用同一 store-open/close 规则；
- owned/borrowed close、重复 close、close 期间取消、backup 与 writer 竞争全部收敛；
- 临时移除 DB unique/CAS、legacy gate、schema gate、取消重读和 ownership close 中的关键保护，公开
  反例按根因变红后恢复。

### 6.5 Release Stop A

F0+F1 完成后做一次独立 P0/P1 审查和 SQLite 真实多进程/备份验证。只有这条事实层清零，才能在其上
增加 retry；停止点不自动意味着 tag 或公开版本。F1 使用 SQLite/Session/Product/Evaluation/CLI 的广泛
定向与相邻回归证明当前 owner，不在本停止点重复全量；F1 与 F2 的跨域集成全量统一放在 Release Stop B。

## 7. v0.8-F2：同 Provider/同模型的 bounded retry

**当前实施状态（2026-08-29）**：本节代码、确定性失败/取消矩阵、报告字段和
[ADR-0037](../adr/0037-typed-provider-failures-and-bounded-model-retry.md) 已实现。retry 位于既有
`AgentLoop` 的 Step 内，以后续 Model Attempt ordinal 复用 ordinal one 冻结的 exact provider-bound
request；没有 Provider 内部重试、第二 Runner 或 fallback。四项要求的反向验证已经分别真实复现
认证/协议/未知失败风暴、后续调用逃出 Budget reservation、结算取消绕过生命周期 owner、以及第二次
调用请求漂移，随后均恢复保护。Release Stop B 最终复审已经清零 P0/P1/P2，F1+F2 完整集成门禁也已
通过；F2 到此结束，阶段顺序允许下一步进入 F3，但本节没有提前实现 F3。

Release Stop B 首轮审查发现非法 Session 历史仍可能取得下一张 dispatch permit。修复集中在唯一 owner：
`SessionService.start_model_attempt()` 在同一 Stream lock 内、CAS 前复用完整 `CoreInvariantChecker`；已有
任何核心不变量失败都按 ownership conflict 拒绝，不折叠、修补或删除证据。确定性反例在 retry wait
期间追加第二条 canonical Attempt end；旧逻辑真实发出 ordinal 2，新逻辑保持 Provider 调用 1 次。另一条
P2 是 Benchmark E2E 的旧 Usage 断言，现改为同时证明 Provider Token unavailable 与 Ledger 保守结算，
没有改生产计量语义。短复审最终确认 P0/P1/P2 全零，允许进入完整集成门禁。

实施自审又用公开边界固定三个同范围反例：极大但合法的 Attempt ordinal 不能让指数退避先溢出，choice
缺少 `message` 与显式 `usage: null` 不能被当成空成功或“usage 缺席”。旧实现分别真实得到
`OverflowError` 和两次 `DID NOT RAISE ProviderFailure`；修复集中在 policy 算术与 adapter 严格解析 owner。

实施侧停止点门禁已完成：F2/CLI 定向组 `416 passed`，Runtime/Session/Budget/取消/CLI 相邻组
`474 passed, 1 skipped`，Product/Workflow/Promotion/Benchmark 跨域组除两处阶段授权文件的旧摘要 pin
外为 `504 passed, 1 skipped`，更新具名 pin 后对应 `2 passed`，受影响的本地 Git Benchmark E2E
`6 passed`；全仓 `2478 collected`。compileall、改动范围 Ruff、diff、反硬编码/新增秘密值与文档 QA
通过。这些结果只允许进入 Release Stop B 独立审查，不等价于该停止点已经清零。

首轮审查修复后的不重叠门禁为 `90 + 7 + 32 + 131 = 260 passed`，依次覆盖 Retry/Session/Budget/
Recovery、受影响真实本地 Git Benchmark E2E、Provider/Runtime/架构，以及 CLI Chat/Product 合同与
Benchmark 报告；全仓 `2479 collected`。compileall、本次修改 Python 文件 Ruff、生产修复反示例硬编码
扫描和 diff 通过。这些在当时仍严格等待短复审清零 P0/P1，没有冒充随后运行的完整全量。

短复审清零后，完整门禁从全新仓库外短 `basetemp` 执行全部 `2479` 项。第一次运行得到
`2471 passed, 7 skipped, 1 failed`；唯一失败是 CLI Activity 测试仍期待 F2 已禁止暴露的 raw
`RuntimeError` 文本，而生产已正确输出安全的 `ProviderFailure: provider-failure-unclassified`，heartbeat
生命周期断言本身通过。只同步该旧测试预期后，目标与既有 CLI 清洗反例 `3 passed`；再从另一个全新短
目录完整重跑，最终 **`2472 passed, 7 skipped`、退出码 0**，最慢 L2 隔离验证 `1097.00s`。没有使用
`--lf`、测试筛选或缓存结果冒充最终全量；Release Stop B 到此关闭。

### 7.1 typed、清洗后的失败分类

Provider adapter 必须把 transport/HTTP 结果映射成仓库自有、稳定、无秘密的失败类别；host retry
policy 只消费类别，不解析异常字符串。至少区分：

- 明确不可重试：认证/权限、无效请求、上下文或输出参数、配置、协议/严格响应解析错误；
- 候选可重试：临时 DNS、connect/read timeout、TLS 提前 EOF、response 前断连、明确批准的
  408/429/部分 5xx；
- 未识别：默认不可重试。

清洗责任在 Provider adapter：typed Provider exception 的公开 message 不能包含 raw HTTP response body、
headers、底层 `URLError` 正文、秘密或本机路径；`model/attempt-end` 只记录稳定 code/category。现有
`runtime/error` 对非 Provider 故障保留有界 traceback 的诊断合同不在本阶段全面删除，但 Provider 故障
进入该 traceback 时也只能暴露已经清洗的 typed exception。

具体状态表、最大 Attempt 数、总 retry elapsed、单次 delay、`Retry-After` cap 和 backoff/jitter 必须
来自显式 host policy；测试使用注入 clock/scheduler，不能 sleep 猜时序。Provider SDK 自带 retry 必须
显式关闭或证明不存在，避免双层重试。

### 7.2 Budget、请求和计量

- 第一次 Attempt 的 provider-bound request 在 F0 冻结；后续 Attempt 原样复用；
- 每次 Attempt 重新为同一请求独立 reserve/start/charge/settle；失败和成功全部计入累计 Budget；
- usage unknown 时沿用现有保守 settlement，不把未知填 0；
- backoff 不计 Provider active elapsed，但计入 Step/wall；报告分列 Attempt count、retry wait、Provider
  active、failure category 与最终结果；
- Budget 不足、retry count/elapsed 用尽或取消已经提出时，不得再 dispatch；
- `traceh eval` 的 Product success 定义不变。新网格只能说明当前条件下的可靠性，不能把 retry 后成功
  解释为模型质量提升，也不能与旧网格声称统计显著。

### 7.3 F2 必测与反向验证

- 第一次断连、第二次成功：同一 Step 两个 Attempt、两份 reservation/settlement、一个最终响应；
- 认证、bad request、Router strict parse、未知异常均一次即停；
- 取消发生在 delay、reserve、start append、Provider invoke、end append 和 settlement 各窗口时，
  Provider 调用数都不多一次；
- 第一次 unknown usage 后，余额不足以完整预留相同请求时不缩请求、不重试；
- 重启只关闭 open Attempt，不自动继续 retry；
- raw body/headers/异常正文、凭据和本机路径不进入 Event/CLI/report；
- 极大合法 ordinal 的退避计算仍有限且不溢出；缺失 response message 与显式 null Usage 是 protocol
  failure；
- 临时移除不可重试 gate、attempt-scoped reservation、no-next-attempt 取消 gate 和 frozen request guard，
  分别真实复现认证风暴、费用覆盖、取消后额外付费调用和请求漂移。
- 在 retry wait 期间追加重复 canonical Attempt end，下一次 Session permit 必须 fail closed；临时移除完整
  history gate 时 Provider 必须真实多调用一次，不能只证明 checker 另处会报错。

### 7.4 Release Stop B

F2 先用确定性 Provider stub 完成 retry/error/cancel 全矩阵并独立审查。若用户另行授权，只做一次有界的
真实 Provider smoke 来证明 adapter 接线，不跑完整 Benchmark 网格，也不把 smoke 当发布或质量证据。
真实运行使用新输出目录、不补跑失败项、不 fallback、不覆盖历史报告、不打开/打印/记录 Key。清零
P0/P1 后运行一次覆盖 F1 SQLite 与 F2 retry 的集成全量，并附 `--durations=30`；通过后才进入 UI 重构。
唯一完整发布网格留到 F5。

## 8. v0.8-F3：UI-neutral Chat 驱动与只读 Product observation

**当前实施状态（2026-08-30）**：已按本节实现。首轮独立审查发现 initial observation
fresh read 失败后遗留 subscription/watcher 的 P1，以及公开 host 默认 Feed 未连接 Store 的 P2；当前均已
在 owner 根修并完成确定性反例与反向验证，独立短复审确认 P0/P1/P2 全零。Line CLI 已迁移到共享 Driver/typed update；
Product observation 只 fresh read 并保留 ProductTask/Workflow 双状态，Feed 只作 dirty hint。F3 没有
实现 Textual、第二 UI 命令、第二状态机或任何 F4/F5 能力。

### 8.1 驱动边界

- 外部 adapter 异步提交输入并消费 typed update；driver 不直接读取 stdin，也不渲染终端文本；
- Line CLI 先迁移为第一个 adapter，普通 Chat、Product Proposal、v0.7.1 宿主 `START`、`/task`
  命令、Approval 和 Ctrl+C 语义保持一致；
- Product command 解析保持纯函数；所有写动作调用既有 control-plane owner；
- observation 只 fresh read Product/Workflow/Directory/Artifact/Promotion，不调用带 reconciliation 的
  `inspect/approve` 路径，不 append，不持有业务 worker；
- ProductTask 与 Workflow 是两条事实流。纯读 view 必须并列保留两者状态：例如 Workflow 已到
  awaiting approval、而 ProductTask 仍是 started 时，明确显示“Workflow 已等待批准，ProductTask 尚未
  对账”，不能假装两者一致，也不能等 heartbeat 写一条 Product 事实才让用户看见 Approval；
- 用户真正执行 inspect/approve/reject/cancel 等动作时仍进入现有 control-plane reconciliation owner，
  observation 不替它推进状态；
- 现有 `ActivityTracker` 的事件派生逻辑重构为 driver 的唯一 typed ephemeral activity 来源，Line CLI 与
  TUI 都消费它；不得让 TUI 按相同事件另建第二份 in-flight 推断。它仍不持久化、不参与 recovery。

### 8.2 Feed 只是“该刷新了”的提示

不能只写“replay + Feed + `(stream_id, seq)` 去重”。正确观察握手至少是：

1. 先订阅当前已知的精确 stream；
2. 再 fresh read durable heads 与投影；
3. 若读取发现新的相关 stream，订阅它们并重新读取，直到观察集合稳定；
4. Feed 事件只把 view 标成 dirty，随后重新读取事实，不直接信任 payload；
5. 每次用户动作后、周期 heartbeat、进入 Approval 前和 terminal 前都强制 durable refresh。

实现还必须保证 handshake 部分建立失败时由 observation owner 回滚已经创建的全部 subscription/watcher；
Product host 只能接受与同一 `PublishingEventStore` 精确绑定的显式 Feed，不能提供断开的默认 Feed，也不能
把任意 Feed 与 Store 配成一对后假装具备实时通知。

这样事件发生在“订阅之前”、Feed 丢失、adapter 暂停或 TUI 重启都不会改变最终 view。不得新增 wildcard
全局 Event bus 或 replayable Feed。

### 8.3 F3 必测与反向验证

- 没有 stdin 输入时，进度/取消仍由 event loop 处理；
- 高频 observation 前后全部相关 stream head 不变；
- 相同录制输入经旧 Line 行为合同和新 driver 得到相同 durable Product 事实；
- 在 subscribe/read 各竞态窗口注入事件，最终 view 不漏 terminal/approval；
- Workflow 已 awaiting approval、ProductTask 仍 started 且没有任何用户动作的稳态下，纯读 view 必须
  持续显示两条状态及分歧，所有相关 stream head 保持不变；随后执行真实 Approval 动作才由原 owner 对账；
- 丢弃全部 Feed 通知后，heartbeat/action/final refresh 仍恢复相同 view；
- Line/TUI 对同一事件与 monotonic clock 得到同一 typed activity update，移除共享 tracker 后重复实现的
  差异反例必须变红；
- UI 关闭时调用原 owner 的 cancel/close，不遗弃 ProductTask；
- initial fresh read 失败或取消时，全部初始 subscription/watcher 在公开调用返回前归零；
- host 拒绝普通 Store、缺失 Feed 或与 Publishing Store 身份不一致的 Feed，正确绑定后 dirty 通知可达；
- 临时去掉 subscribe-before-read、周期 refresh 或 observation 纯读守卫，对应公开用例真实漏状态或产生
  非法写入。

## 9. v0.8-F4：可选 Textual TUI

> 实现状态（2026-08-31）：`traceh chat --tui` 仍接入同一 Driver、Session open/recovery、Product host 与
> durable observation；Textual 仍是 optional extra。发布前体验确认旧 fixed-button TUI 缺少即时反馈、
> 事实年龄和可见关闭过程，因此已在同一路径原地替换，没有旧 TUI compatibility。新版定向反例与真实
> local-Git Product Pilot 已通过。replacement 独立审查发现的两项 P1 与两项 P2 已按现有 owner 修复并
> 完成确定性反向验证并经短复审清零；随后 Product Chat START 前权限根修已通过真实 Git/Line/Textual
> 反例。后续多轮真实体验的 Router/coder terminal 停滞由 native-thread traceback 统一定位为
> `PluginActivationSet` 在非重入 ownership lock 内启动 eager cleanup Task 导致 event-loop 自锁。当前只在
> 该插件生命周期 owner 内把同步 `disposing` 认领与锁外 cleanup Task 启动分开，原 Router/Supervisor/
> Product/Workflow cleanup 主线已恢复，并用 eager-task Runtime 反例、SQLite 主线和 Textual Pilot 覆盖。
> 后续用户体验又发现旧终态 task observation 遮蔽下一项已确认 Proposal/START；现已在唯一 App observation
> owner 按 exact task id 收敛旧 observer 并切换新 pending task，反向验证可稳定恢复“新标题 + 旧失败 +
> 无 START”。当前又按 D1–D7 原地换成唯一浅色布局：右栏顶部摘要/底部闸门、固定宽事实表、确认区不
> 隐藏摘要、短对话底锚和短 `模型 ·` 标记；100–109 列使用单栏，失败面板从 exact Workflow message
> 所属 Turn 显示稳定叶子错误。一次真实 `qwen-plus` TUI ProductTask 已从 Proposal 走到 bare Promotion。
> Provider/TUI/失败证据与跨 owner 最终复审均已清零，最新相邻 owner 回归 `251 passed`；不得拿
> 替换前的 F5 全量直接进入打包/发布。其后的产出可见性工作又在同一个 adapter 原地实现 fresh
> `Ctrl+T` 角色对话、fresh `Ctrl+P` 完整身份，并恢复 Review evidence；没有旧 TUI 或兼容分支。这批改动
> 发生在 `2555 passed, 7 skipped` 全量之后，因此该数字现在也只作历史证据。用户体验又发现批准完成后
> 左栏没有宿主反馈的 P2，现已在同一 adapter 根修并反向验证。当前 `54` 项 TUI、`215` 项相邻回归、
> `2575` 项 collect-only 已通过；前一轮独立短复审为 `P0=0/P1=0/P2=0`，本次小修尚待短复审。复审与
> 用户确认后再运行唯一最终全量。

### 9.1 最小产品面

- 仍使用 `traceh chat --tui`，不新增第二 Product Chat 命令；
- Textual 是可选依赖；核心安装、Line CLI、Eval 和离线 Wheel 不依赖它；
- 默认界面明确分开 transient Proposal/START/operation、durable Product/Workflow/Session/Review/
  Promotion facts 与模型自述；模型文字不得成为任务状态；
- 只有一套浅色 presentation：右栏从顶部起排、闸门固定在底部，确认只替换闸门区；事实表列宽与容器
  算术一致，短对话贴近输入框向上生长，模型文字只用暗色斜体 `模型 ·`；不保留旧主题或旧布局；
- 只渲染当前合法闸门；START/Approve/Reject/Cancel 均需 typed confirmation，未知状态组合 fail closed；
- operation 等待时长和每条相关 durable 流的 latest event/age 必须可见，停滞只陈述“无新事实”；
- START caller 在途但 durable Workflow 已 RUNNING 时，正常 typed Cancel 必须仍可达并走原 control owner；
- initial observation 失败必须诚实显示并有界重试；任务年龄不得被无法绑定当前 task 的全局流刷新；
- 一个终态任务之后收到新 Proposal/START request 时，唯一 Product pane 必须按 exact task id 关闭旧 observer
  并切到新 pending task；旧 durable 证据保留为历史，但不得混入新任务或遮蔽其 START；
- Product/Workflow 分歧并列展示；本轮 TUI 不提供 `Ctrl+I`/`Ctrl+R`，只提示既有 Line
  `/task inspect <task-id>`，普通 refresh 不得调用 inspect；
- 默认身份短而可读；`Ctrl+P` 每次打开 fresh observation 后显示完整身份与显式复制，剪贴板失败只把
  选定值写入独立临时文本文件并报告路径；小于 110 列仍折成单栏摘要；
- `Ctrl+T` 每次打开 fresh Product observation，只从 exact Router Agent/Session 与固定 Workflow role
  Agent/Session/create-request binding 发现角色；每条 Session 先过 `CoreInvariantChecker`，再由现有
  `SurfaceProjector` 按角色展示。该视图无 Store scan、缓存、订阅、写入口或实时 tail；Usage 不可靠时明确
  unavailable，shell 参数、tool result 正文和 raw payload 保持遮蔽；
- Review 区显示既有 evidence 的 bounded changed paths、Verifier 摘要与 Patch preview，截断和替换必须
  明示，不能声称提供完整 diff；
- 关闭过程逐 owner 可见，退出前仍按原 lifecycle owner 收敛；
- 首版不做完整历史 Dashboard、Web UI、拖拽 DAG 或模型 token streaming；
- 所有模型文字、Patch preview、路径和失败展示都按不可信内容转义并有界；
- Workflow 包装失败之外，必须沿失败 node → Agent Directory → exact Agent/Session/create-request/message →
  invariant-checked message Turn `runtime/error` 显示可验证的稳定叶子 code/category/type；不得采纳 identity
  conflict 的外来 Agent，也不得让同 Session 后续无关 Turn 覆盖原失败；不显示 raw Provider/exception 内容；
- Approval 前显示 review/target/digest 的安全摘要，按钮仍调用同一 control-plane 幂等操作。

### 9.2 F4 必测

- Line/TUI 对同一输入创建相同 Proposal/Task identity；
- 未执行宿主 `START` 或人工 Approval 时，模型动作和 UI 事件都不能越权；
- 双击、重复、stale digest 使用现有幂等/CAS 保护；
- restart 只靠 durable observation 恢复，不依赖 widget state；
- 未安装 TUI extra 时给出明确提示，不静默切换成不同语义；
- markup、控制字符、超长 Unicode 和二进制摘要不能执行 UI 标记或破坏布局；
- Windows 终端 resize、`Ctrl+T`/`Ctrl+P` 全宽视图、Ctrl+C、EOF 和后台任务关闭走原 owner 收敛。
- START operation 在 observer 尚未返回时也必须立即反馈；并发 refresh 不得让旧 projection 覆盖新 facts；
- START 在途 Cancel 必须通过真实 Product host 到达 durable CANCELLED；初始 observation 错误后周期恢复；
- restored 终态任务之后依次收到另一 task 的 Proposal 与 confirmation，必须先显示新提案、再显示属于该
  task 的 START；临时移除身份交接时反例应真实恢复旧终态污染和 START 消失；
- `Ctrl+T` 在宽/窄屏都必须切到任务对话页；上下键、Enter、Esc 都有可观察效果，打开/翻页/折叠不能
  append 事实，外部 append 后重新打开必须 fresh 可见；
- multi 对话页必须显示 router/parent/reviewer/coder 的精确 Session；Router/Directory 或固定角色
  session/request identity 被替换、Session invariant 失败时必须 fail closed；unknown Usage 不能显示为 0；
- `Ctrl+P` 必须 fresh 读取完整身份，复制失败降级不能写 Product/Session facts；状态栏不得宣传未实现的
  `Ctrl+I`/`Ctrl+R`；
- Review changed paths、Verifier 与 Patch preview 的行数上限和遗漏提示必须有纯 presentation 测试；
- 当 Chat Workspace 与 Product source 是同一真实 Git 仓库时，confirmation 后到宿主 START 前不得出现
  Workspace 写入、进程或外部事务；Product-configured requester 只暴露 read/proposal/confirmation Tool，
  声明为 effectful 的插件 Tool 也必须由同一单调 Policy 拒绝。临时恢复普通 Chat 工具面时，反例必须真实
  弄脏 source 并由原 Workspace gate 报 `workspace-source-invalid`，不能用工具未注册或夹具失败冒充；
- 至少一条确定性 Pilot 使用真实 Product host、auto Router、managed local Git、Verifier 与 Review 到达
  awaiting approval 或 durable terminal。
- Python 3.12 eager task factory 下，插件 ActivationSet cleanup 不得在持有其非重入 ownership lock 时启动；
  真实 Runtime dispose 必须收敛，且同线程重入反例在恢复旧“锁内 create_task”逻辑时确定性失败；
- Router、single coder 与 multi 角色仍通过原 Supervisor/Generation cleanup 主线收敛；生产 SQLite 与
  Textual Product 主线在角色 terminal/failure 后必须继续写出 Workflow/Product terminal 或 Approval，不能
  用移除局部 cleanup、UI 假进度或 Provider 未调用冒充修复。
- OpenAI-compatible Tool arguments 仍以严格 JSON 为主；若允许 multiline 扩展，只能由 exact frozen Tool
  schema 证明顶层字段为 string，并在转换后重新通过严格 object 解析。`NaN`/正负 `Infinity`、未知/
  非字符串/嵌套/表达式等邻近
  malformed 输入必须稳定拒绝，不能用 JSON5、eval、retry/fallback 或模型/任务硬编码换取 demo 成功；
- 至少一条真实 TUI acceptance 从聊天 Proposal 经过 typed START、Router、coder、Verifier/Review、typed
  Approval 到一次性 bare Promotion，并核对 source clean、目标 ref、推广后测试及 Session/Budget/Workspace
  收敛；确定性 Provider 与 Pilot 仍是日常门禁，不能冒充这条真实验收。

### 9.3 Release Stop C

UI/TUI 完成后独立审查 UI 是否获得新 authority；F3/F4 各自只跑 driver、Line/TUI、Product observation、
权限与生命周期的定向和相邻回归。清零 P0/P1 后才进入 F5 的最终全量与打包。

## 10. v0.8-F5：最终验证与发布候选

> 当前实施状态（2026-08-31）：唯一版本源已切到 `0.8.0`；插件版本/范围和作者模板已同步。替换前候选
> 的第 6 步审查和 `2496 passed, 7 skipped` 第 7 步全量已真实完成但现为历史证据。TUI replacement 重新
> 打开 Release Stop C、当前候选全量和后续第 8/9 步；最新浅色布局、叶子失败投影与 ADR-0038 Provider
> 根修已经一次真实 TUI ProductTask 完成路径验证。Provider/TUI/失败证据与跨 owner 最终复审均已清零
> P0/P1/P2，最新相邻 owner 回归 `251 passed`。Textual gate 点击后的焦点交接也已在当前 presentation
> owner 根修并经短复审清零。两次完整红灯及测试同步根因均保留在验证记录，最终完整全量为
> `2555 passed, 7 skipped`、退出码 0、耗时 `3078.80s (51:18)`。随后同一 TUI 的产出可见性代码与测试
> 又发生实质变化，因此这次全量现只作历史证据；当前必须先完成该范围的定向/相邻验证与独立审查，再
> 运行一次新的唯一最终全量。全量绿色前不能进入 clean-input 打包与双形态离线安装，也不得声称资产、
> 真实 Provider 网格、tag 或发布门禁已经通过。

### 10.1 风险分层门禁与 F5 顺序

v0.8 不再把“一个小阶段”机械等同于“一次完整 pytest”。门禁分三层：

| 层级 | 何时运行 | 必须证明什么 |
|---|---|---|
| 阶段门禁 | F1-F4 每次实现和修复 | compileall、当前 owner 的正向/关键反例/失败或取消定向测试、相邻回归、collect-only、修改范围 Ruff、diff 与文档 QA |
| 集成检查点 | F2 独立审查清零 P0/P1 后一次 | F1 SQLite 唯一事实源与 F2 retry/Attempt/Budget 跨域组合；完整 pytest 附 `--durations=30` |
| 发布门禁 | F5 全局独立审查清零 P0/P1 后一次 | 当前提交候选的最终完整 pytest、打包/离线安装、发布验证和另行授权的真实 Provider 网格 |

若 F1-F4 意外改变共享 Runtime/Session/Store schema、Provider 外部副作用或其他不在当前 owner 内的跨域
合同，或者独立审查证明存在真实跨域 P0/P1，可把一次完整 pytest 前移为新的集成检查点；该决定必须在
报告中写清触发原因。已经前移并通过的检查点，不在下一个小阶段无新跨域改动时机械重跑。全量发现
确定性失败后，根修并重新运行到绿色仍是同一检查点的必要确认。并发、取消、进程、SQLite 和真实 Git
用例不默认启用 `pytest-xdist -n auto`；只允许并行已证明资源隔离的子集。Wheel、L2-L4、真实 Provider
及联网门禁也不进入日常阶段门禁，除非当前改动直接拥有该边界。

F5 的具体顺序是：

1. compileall；
2. F0 terminal safety/Attempt/Budget、F1 SQLite、F2 retry、F3 driver、F4 TUI 定向测试；
3. Session/Turn/Step/Attempt、Product/Workflow/Budget/Workspace/Artifact/Promotion/Evaluation 相邻回归；
4. SQLite 同进程多 stream/Feed、跨进程同流 CAS/异流 busy、backup/restore、legacy refusal、所有 fresh
   data dir CLI 子命令，以及 `evaluation/attempt.py` 与 `evolution/comparison_probe.py` 的显式 store
   ownership 验收；
5. collect-only、修改范围 Ruff、`git diff --check`、链接/围栏/章节对应 QA；
6. 每个 release stop 的 Finding 已关闭后，做一次最终独立 P0/P1 审查；
7. 审查清零后运行一次最终全量并记录 `--durations=30`；若它暴露确定性缺陷，修复后必须重新全量确认，
   两次均如实记录，不能把首次红灯抹掉；
8. clean-input Wheel/sdist/source ZIP、archive audit、无 `[tui]` 与带 `[tui]` 的离线安装；
9. 用户另行授权后只运行一次作为发布证据的完整真实 Provider acceptance：fresh SQLite data dir、fresh
   eval output、不重试旧报告、不 fallback、不打开/打印/记录 `.env` Key。F2 的可选 smoke 不与该网格
   合并、不补样本，也不宣称质量结论。

停止点是审查边界，不要求每个小阶段都跑全量或发布版本。除有据可复核的前移触发外，F1/F2 合并一次
集成全量，F5 再运行一次发布候选全量。

### 10.2 文档与完成定义

实现时先更新正式上下文，再同步通俗版；同时更新 README、CHANGELOG、Roadmap、必要 ADR 和验证记录。
不得在 F5 前升级版本、tag、push 或 release。

v0.8 完成必须证明：

- snapshot 同时证明可重建的 composed request 与最终 provider-bound dispatch request，Budget 拒绝不会
  产生虚假 Attempt；
- SQLite 是所有生产入口唯一 EventStore，旧 JSONL 明确拒绝且零迁移/零读取；
- store owner、close、backup、CAS、跨 stream busy、durability 和取消收敛都有公开反例；
- retry 同 Provider/同模型/同请求、有界、逐 Attempt 计费、可取消且无 fallback；
- Line/TUI 复用同一 Product/Workflow/Approval/Promotion 权限主线与唯一 ephemeral activity projection；
- Feed 丢失或 UI 重启后，durable observation 仍得到相同事实；
- 没有第二 Runtime、Supervisor、Workflow、ProductTask 状态、Event bus 或 Benchmark Runner。

## 11. 冻结后的执行与停止规则

- 最终独立计划审查已经结束；不再为本文启动第五轮开放式审查。实现中的新 P0/P1 仍按 `AGENTS.md`
  证据准入处理，但设计偏好、未来扩展或没有公开错误结果的理论对象不能重新打开范围；
- F0 必须显式修订 ADR-0027 的反重复派发归属；F1 至少需要一份覆盖 SQLite schema、store ownership、
  busy/durability/backup/legacy refusal 的 ADR。F3 采用本文已冻结的纯读双状态 view，因此不新增让
  heartbeat 写 Product 事实的 ADR；
- 每个 release stop 只审查已经实现的当前阶段与相邻公开路径，不提前实现或用后续阶段测试掩盖前一层；
- 完整 pytest 按 10.1 的风险分层执行；真实 Provider 完整网格、版本升级、打包、tag、push 与 release
  仍只在 F5 且分别获得所需授权；
- 如果真实源码证明某个准确类名或拆分不可行，可以选择更轻实现，但不得改变本计划的 owner、事实源、
  权限、唯一 Runner、无 compatibility reader 和失败收敛合同；需要改变这些冻结边界时必须由项目所有者
  重新批准，而不是实施者自行扩展。
