# TraceHarness v1.0 总路线：记忆、隔离、互操作与受控并发

> 状态：讨论结论的执行基线；不是任何阶段的开工授权
>
> 编制日期：2026-09-05
>
> 适用范围：v0.8 收口、v0.9、v0.10、v0.11、v0.12 与 v1.0 RC
>
> 事实源优先级：当前源码/测试/协议 > 两份项目上下文 > 本计划 > 旧 Roadmap 与历史讨论

## 0. 文档合同

本文把已经讨论清楚、但分散在多轮对话中的后续路线收敛为一份可执行总计划。它负责回答：

- v0.9 到 v1.0 各版本分别解决什么问题；
- Workspace Memory、M3 History Evidence、Sandbox、MCP 与动态 Workflow 如何分层；
- 哪些能力复用现有主线，哪些必须新建受控边界；
- 每阶段何时停止、审查和进入下一阶段；
- 到什么程度才可以把 TraceHarness 称为一个闭环的本地、单用户、可审计 Coding Agent 宿主。

本文不覆盖 [`TRACEHARNESS_V0.9_STAGE_PLAN.md`](TRACEHARNESS_V0.9_STAGE_PLAN.md) 中已经冻结的字段级合同。
v0.9 仍以该文档为权威实现规格；本文只规定它在总路线中的位置和与后续版本的边界。

本文也不代表一次性实现全部版本。每个版本都必须重新检查当时真实 HEAD，并由项目所有者明确授权开工。
禁止把多个版本压成一个巨型提交，或为了追求“v1.0 功能齐全”提前绕过当前阶段的 owner、协议和门禁。

## 1. 最终产品目标

v1.0 的目标不是成为通用云平台，而是完成一个严肃的本地单用户 Coding Agent 宿主：

```text
用户意图
  → ProductTask 提议与宿主 START
  → 固定或受控动态 Workflow
  → 隔离的 Agent / Tool / MCP 执行
  → 单一 EventStore 中的 durable 事实
  → Verification
  → Review
  → 人工 Approval
  → Git CAS Promotion
  → Workspace Memory 治理
  → M3 压缩与历史证据按需展开
  → Context Composer 冻结下一次模型输入
  → 可观察、可审计、可重放、可恢复
```

用户应获得的核心体验是：

1. 系统不会因 Session 变长或切换 Session 而突然“忘记项目在做什么”；
2. 模型默认只看到最小、最新、最可信的信息，需要细节时再受控展开；
3. 模型可以规划和并发，但不能自行扩大 Workspace、Tool、预算、网络或审批权限；
4. 本地代码与命令在明确隔离边界中运行，隔离不可用时不会假装安全；
5. 外部工具通过标准 MCP 接入，但仍受 TraceHarness 的生命周期、Policy、Budget 与审计控制；
6. 用户能知道本次请求实际注入了什么、执行了什么、为何得到当前状态。

## 2. 当前真实基线

截至本文编制时，源码已经具备：

- append-only EventStore、Session/Turn/Request snapshot 与重放验证；
- Agent、Composition、Plugin Generation/Lease、Tool、Budget 的独立 owner；
- ProductTask、固定 single/multi Product Workflow、Verification、Review、Approval 与 Promotion；
- Workflow DAG、Map、Join 与 ready-node 并发执行底座；
- 子 Agent 独立 identity、Session 和可选独立 Git worktree；
- M2 ProductTask 任务记忆；
- M3 确定性 Surface compaction 及原始事件保留；
- M4 上下文观察投影的当前候选实现。

同时必须诚实承认当前还没有：

- 跨 Session 的权威 Workspace 长期记忆；
- 覆盖 Skill、Memory、历史证据和任务事实的统一 Context Composer；
- OS 级或等价的真实 Sandbox；`READ_ONLY`、venv、临时目录与 Git worktree 都不是 Sandbox；
- MCP Client；
- Product 层可由模型在能力包络内选择或生成的动态并发 Workflow；
- 通用的崩溃后 in-flight Workflow 冷恢复。

当前 Product 拓扑仍是固定串行链：

```text
single: coder → verification → approval
multi:  parent → reviewer → coder → verification → approval
```

底层“能并发运行 ready nodes”不等于产品已经支持安全的多 coder 并发。后者还需要 Artifact、Merge、冲突、
Verification、取消收敛和 Promotion 等完整合同。

## 3. 不可破坏的架构原则

### 3.1 单一事实源

- 所有权威生命周期事实继续写入同一个 EventStore；
- Workspace Memory、MCP 调用、Sandbox receipt、Workflow plan 与 join/merge 结果都不能另建可变事实库；
- SQLite FTS、embedding、缓存和 UI snapshot 只能是可重建派生物；
- `runtime.state`、共享 mutable messages、模型自述和插件私有状态不能升级成系统事实。

### 3.2 权限与建议分离

- 模型可以提议 ProductTask、Memory candidate、Workflow plan 或需要的证据；
- 宿主拥有 START、Memory 激活/替代/撤销、能力包络、Tool effect、Approval 与 Promotion；
- Plugin manifest、MCP server annotation、模型输出都不能自行授予权限；
- 只有权限扩张才需要新增审批；包络内规划不应被多余人工确认拖慢。

### 3.3 冻结后执行

模型实际调用 Provider 前，本次 request 的组成、工具 schema、Skill/Memory/History 注入、输出上限与相关 identity
必须冻结并可由 `source_seq` 重建。Sandbox、MCP 与动态 Workflow 也必须先冻结可验证的输入或 plan，再发生外部
副作用，不能先执行后补账。

### 3.4 渐进披露

- 默认输入是短、新、确定的信息；
- 先给索引、摘要和存在性，再按问题展开最小证据块；
- M3 摘要是非权威 Surface 压缩，不替代 Workspace Memory；
- 展开 M3 原始历史必须有来源范围、freshness、预算和 request-only 生命周期；
- 旧工具结果可以作为历史证据，但必须标明“当时结果”，不得冒充当前 Workspace 状态。

### 3.5 隔离和失败必须真实

- 没有经过验证的隔离就不得使用 “sandboxed” 文案；
- 声明要求 Sandbox 的执行，在后端不可用时必须拒绝，不能静默降级为普通子进程；
- 取消返回前必须收敛进程树、Tool/MCP 调用、Plugin Lease 与 Workflow children；
- unknown commit、部分成功、超时和 crash 必须有明确对账路径，不能被“失败”一词掩盖。

### 3.6 不重复造轮子

- MCP 使用公开协议，不在 Plugin 内发明一套近似 MCP 的私有互操作协议；
- Plugin 是内部扩展与生命周期载体，MCP 是外部能力互操作边界，两者职责不同；
- 动态 Workflow 复用现有 DAG/Map/Join/并发 scheduler，不建立第二套编排引擎；
- Sandbox 是宿主核心安全能力，不伪装成普通业务 Plugin。

## 4. 版本列车与依赖

```mermaid
flowchart LR
    V08[v0.8 收口<br/>M3 + M4 联合门禁] --> V09[v0.9<br/>Context / Skill / Memory / History Evidence]
    V09 --> V10[v0.10<br/>Sandbox 与隔离执行]
    V10 --> V11[v0.11<br/>官方 MCP Client Plugin]
    V11 --> V12[v0.12<br/>受控动态并发 Workflow]
    V12 --> RC[v1.0 RC<br/>冻结、集成、安全与发布]
```

| 版本 | 主题 | 必须建立的主能力 | 明确不做 |
|---|---|---|---|
| v0.8 收口 | 当前候选闭环 | M3+M4 联合验证、独立审查、发布 | 新增 v0.9+ 功能 |
| v0.9 | 长期上下文与记忆 | Context Composer、typed Skill、Workspace Memory、History Evidence | Sandbox、MCP、动态 Workflow |
| v0.10 | 隔离执行 | 真实 Sandbox、资源/网络/文件边界、收敛证据 | MCP 产品能力、自由 DAG |
| v0.11 | 标准互操作 | 独立官方 MCP Client Plugin，纳入既有 Tool/Skill/资源边界 | MCP Server、多租户市场 |
| v0.12 | 规划自由、权限受控 | 模板选择、受限 DAG、多 Agent 并发与确定性汇合 | 任意代码 Workflow、无界自治 |
| v1.0 | 稳定发布 | 公共边界冻结、联合质量/安全/安装/迁移政策 | 新功能扩张 |

依赖顺序是有意义的：MCP 本地 server 与多 Agent 并发都会扩大副作用面，因此先有 Sandbox；动态 Workflow
需要调用稳定的内部 Tool 与外部 MCP 能力，因此放在 MCP Client 之后。若真实实现证明某个依赖不成立，必须先
形成新 ADR 和重新批准版本边界，不能在实现中暗改顺序。

## 5. v0.8 收口：当前阶段的唯一工作

### 5.1 范围

当前工作树中的 M3 与 M4 必须作为一个集成候选收口：

1. 独立审查验证 Claude 报告中的实现和测试证据；
2. 清零符合仓库 Finding 准入规则的 P0/P1；
3. 使用安装了 TUI extra 的指定解释器运行最终无筛选全量，避免模块级 `importorskip` 静默跳过 TUI；
4. 完成 clean-input Wheel/sdist/source ZIP、core/`[tui]` 离线安装与计划中授权的真实 Provider 验收；
5. 同步正式/通俗上下文、验证记录、README 与 CHANGELOG；
6. 在用户授权后形成单一集成提交、tag 和 release。

### 5.2 停止条件

- M3/M4 的 projector、协议、UI 只读边界和 request snapshot 关系有可重复证据；
- P0/P1 清零；
- 最终全量从头到尾绿色，不能用 `--lf` 或缓存冒充；
- 未授权前不 commit、push、tag 或 release；
- v0.8 未发布前，不开始 v0.9 实现。

## 6. v0.9：统一上下文与项目级记忆

v0.9 的字段、事件、owner、阶段与 release stop 以
[`TRACEHARNESS_V0.9_STAGE_PLAN.md`](TRACEHARNESS_V0.9_STAGE_PLAN.md) 为准。总路线只冻结以下产品边界。

### 6.1 统一 Context Composer

建立唯一 request-scoped Context Input 主线，按明确优先级组装：

```text
宿主 system/policy
  > 当前 Workspace active Memory
  > 当前 ProductTask / Workflow / Promotion 权威事实
  > 当前用户消息与最近原文 Surface
  > M3 非权威摘要
  > 按需展开的历史/执行证据
  > Skill 关联资源
```

“优先级更高”表示冲突解释权更高，不表示所有内容都必须塞入 Prompt。最终注入的 exact bytes、来源、版本、
freshness、预算和排除原因进入可重建的 Context Input snapshot。

### 6.2 Workspace Memory

- 以 append-only proposed/activated/superseded/revoked 事实表达，不维护可变 `workspace_memory.json`；
- 模型可以提出 candidate，只有宿主/用户能激活、替代或撤销；
- 适合长期目标、稳定约束、架构决定、术语、里程碑和已批准路线；
- 临时进度、模型自评、某次工具输出和未确认推断不进入权威长期记忆；
- 多个 ProductTask 的完成事实保留在各自记录中，Workspace Memory 只提炼跨任务仍有效的项目事实与阶段关系。

### 6.3 Skill 与检索

- Skill 是现有 trusted Plugin Generation/Lease 上的 typed contribution，不是另一套插件系统；
- exact + SQLite FTS 是核心离线路径；本地 embedding/reranker 只能显式启用且仍为可重建派生索引；
- 检索遵守 Workspace、Generation、authority、freshness 与 Context Budget 两道宿主过滤；
- Skill 选择不等于启用 Plugin，更不等于授予 Tool。

### 6.4 M3 History Evidence

- 原始事件从未因 compaction 删除，因此可以按 format-2 provenance 精确重建被压缩历史；
- 模型默认只看到摘要和可用证据索引，需要时请求最小原始 block；
- 展开内容仅进入当前 request/Step，不回写普通 Surface，不自动升级成 Memory；
- 每块标明 source range、digest、bytes、freshness，并明确“历史工具结果不证明当前状态”；
- 对当前问题真正需要最新状态时，应重新读取/验证，而不是盲信旧工具结果。

### 6.5 v0.9 完成体验

新 Session 开始后，模型能准确知道项目长期目标、仍有效约束、当前阶段和相关历史任务；面对细节问题时，
先发现“存在证据”，再展开必要的旧对话或执行记录。用户可以检查模型本次真正收到的 Context Input，而不是
根据模型自述猜测。

## 7. v0.10：Sandbox 与隔离执行

### 7.1 产品目标

把“在受控 Workspace 中运行代码和命令”从 Policy 声明升级为可证明的执行边界。Sandbox 是 Host/Core
capability：Tool、Product Agent、受隔离 Plugin 和本地 MCP server 都可复用同一能力，但任何 Plugin 都不能拥有
或绕过它。

### 7.2 S0：威胁模型与后端可行性冻结

开工前必须用真实实验冻结：

- 支持的 OS/backend、内核/系统依赖与最低版本；
- 文件读写、symlink/junction/reparse point、设备路径与父目录逃逸威胁；
- secret/env/credential 继承边界；
- 网络默认拒绝与显式 allowlist 的可执行语义；
- CPU、memory、process count、wall time 与输出上限；
- 子进程树、后台进程、取消、宿主 crash 和 orphan 收敛；
- Git worktree、临时目录和 package cache 的挂载/复制语义。

Windows Job Object 只能解决部分进程/资源生命周期，不能单独被称为文件系统 Sandbox。若某平台无法满足冻结
合同，应明确标为 unsupported 或降低可宣传能力；不得用“尽力限制”冒充强隔离。

### 7.3 S1：核心 Sandbox 合同

建立宿主拥有的不可变执行请求和 receipt，至少包含：

- sandbox/backend identity 与 version；
- workspace root、只读/可写 mounts 与禁止路径；
- argv、cwd、受控 env identity、network policy；
- resource/time/output limits；
- caller/Agent/Session/Tool identity 与 Budget reservation；
- started/finished/cancelled/timed-out/unknown-convergence 结果；
- stdout/stderr 的有界、遮蔽、digest 与持久证据关系。

receipt 写入同一 EventStore。OS 临时对象和进程句柄属于执行 owner，不成为第二事实源。

### 7.4 S2：Tool 与 Product 执行接入

- effectful Tool 统一通过 Sandbox executor；
- `shell`、测试、构建和用户代码不再有绕过隔离的默认路径；
- 现有 Tool Policy、Budget、Session request snapshot 与 Sandbox request identity 互相绑定；
- cancel/timeout 返回前验证进程树收敛，重复取消幂等；
- output flood、fork bomb、路径逃逸、网络逃逸、secret 泄漏和 unknown commit 有确定性反例；
- `READ_ONLY` 继续表示访问政策，不被重命名成 Sandbox。

### 7.5 S3：受隔离外部进程与 Plugin 路径

- trusted in-process Plugin 仍是明确支持模式；
- 需要隔离的 Plugin/local server 使用独立进程与 Sandbox，不把不可信代码加载进宿主；
- Plugin Generation/Lease/Drain 继续拥有生命周期；Sandbox 只拥有该次执行资源；
- activation rollback、drain、host shutdown 与进程树收敛必须互相对账。

### 7.6 S4：观察、验收与发布停止点

Line/TUI 展示实际 backend、网络/文件权限、资源上限、结束原因和 receipt identity，不展示秘密。至少验证：

- 允许的代码编辑/测试正常完成；
- 禁止路径、symlink/reparse escape 与网络访问被真实拒绝；
- timeout/cancel/host shutdown 后无逃逸进程；
- backend 缺失时 fail closed；
- Windows 与其他宣称支持的平台分别有真实验证，不以 mock 代替平台能力。

## 8. v0.11：官方 MCP Client Plugin

### 8.1 定位

MCP 解决“如何与外部能力按公开协议互操作”，Plugin 解决“这项扩展在 TraceHarness 中如何安装、激活、租用、
排空和审计”。因此实现一个独立官方 `traceh-plugin-mcp`，由 TraceHarness 作为 MCP Client；不把 MCP 协议复制成
TraceHarness 私有 Tool 协议，也不把 MCP 逻辑硬焊进 Core。

### 8.2 C0：协议与 transport 冻结

实现时以当时 MCP 官方规范为准，先冻结：

- 支持的协议版本与 transport；
- initialization/capability negotiation、identity 和生命周期；
- Tool、Resource、Prompt 的受支持子集；
- cancellation、timeout、progress、error 与 reconnect 语义；
- 本地 server 与远程 server 的信任、认证、secret 和网络边界。

本文不提前写死易变化的 transport 细节。任何不支持能力都必须显式拒绝，不能猜测降级。

### 8.3 C1：Plugin 生命周期与配置

- MCP Client 作为现有 Activation/Generation/Lease/Drain 下的 typed Plugin contribution；
- server 配置、协议能力和 schema identity 在 Generation publish 前验证；
- Lease 持有期间 server/client 资源可用，drain 等待在途调用收敛；
- secret 由宿主受控注入，不进入 Prompt、事件正文、日志或模型生成配置；
- 本地不可信 server 进程必须走 v0.10 Sandbox；远程 server 受网络 allowlist 与认证政策控制。

### 8.4 C2：MCP Tools

- MCP Tool 映射为既有 Tool catalog 中的受控 Tool；
- MCP annotation 只提供描述信息，实际 effect/read/write/network 分类由宿主 policy 决定；
- 调用必须经过 ToolRuntime、Budget、Approval（若需要）、Sandbox/network policy、遮蔽和 EventStore 审计；
- request/result schema、server identity、Generation/Lease、Agent/Session/Turn 与调用 receipt 必须可核对；
- duplicate/retry/unknown commit 不能导致不可见的双重副作用。

### 8.5 C3：Resources、Prompts 与 Skill 边界

- MCP Resource 默认是按需读取的外部资源，不自动常驻聊天或 Workspace Memory；
- MCP Prompt 作为不可信内容模板，不成为 system authority；
- 适合长期目录化的能力可以贡献 typed Skill metadata，但仍受 v0.9 selection、budget 和两道宿主过滤；
- 外部内容必须经过 safe display/prompt boundary，不能利用标记闭合、角色伪装或 Prompt injection 提权；
- 所有内容都要带 server/source/freshness，最新读取与历史 snapshot 不混淆。

### 8.6 C4：治理、体验和发布停止点

用户能查看每个 server 的来源、权限、连接状态、暴露能力、最近调用和失败原因，并能停用/排空 Generation。
至少验证恶意 schema、超大输出、断线、重复响应、取消、secret 泄漏、server 重启与 drain。v0.11 不实现
MCP Server 模式，也不承诺第三方插件市场。

## 9. v0.12：受控动态并发 Workflow

### 9.1 产品目标

实现“规划自由，权限受控”：宿主先冻结能力包络，模型只能在包络内选择模板、分解任务和组织 DAG；Workspace、
角色、Tool、预算、并发、网络、安全尾部或审批要求的任何扩张都必须由宿主拒绝或重新审批。

### 9.2 能力包络

至少冻结：

- ProductTask/Workspace/Git base identity；
- 可用角色、每角色 Provider/model policy 与 Tool/MCP 集；
- max nodes、depth、fanout、concurrency、attempts、duration 和 Budget；
- 允许的 AgentTask/Map/Join/Verification/Approval 节点与边类型；
- Artifact 格式、worktree 策略、merge/conflict owner；
- 不可绕过的 Verification、post-code Review、人工 Approval 与 Promotion 安全尾部；
- cancel/failure/partial success/replan 的收敛规则。

### 9.3 W0：Workflow Plan 协议

- 模型输出受限、typed、canonical plan，不输出任意 Python 或可执行 DSL；
- 宿主 validator 检查 DAG 无环、identity、owner、资源、权限和安全尾部；
- validated/frozen plan 进入同一 EventStore，执行器只消费冻结版本；
- plan 文案不是权限，未知 node/edge/field 明确拒绝；
- 当前通用 in-flight crash recovery 不应被文案暗示为已完成，若本阶段要支持必须单列状态与对账设计。

### 9.4 W1：先选择受控模板

第一步只允许模型在宿主批准模板中选择并填写参数，例如：

```text
single coder
parallel inspect → one coder
module map → join findings → one coder
parallel independent coders → deterministic integration
```

这一步先验证 routing、参数绑定、包络内自由和权限扩张拒绝，不立即开放任意 DAG。

### 9.5 W2：受限 DAG 与真正并发

- 复用现有 ready-node scheduler、Map、Join 和 process/concurrency slots；
- 每个子 Agent 拥有独立 identity、Session、Context Input 与 Budget；
- 不共享 mutable messages；协作通过 durable、有界、typed report/message/artifact；
- 同一 Workspace 的写者默认隔离到独立 worktree 或只读分析环境；
- sibling cancel、fail-fast/continue、parent shutdown 与 Budget exhaustion 必须确定性收敛；
- 并发测试使用 Gate/Event/锁，不使用任意 sleep 猜时序。

### 9.6 W3：Artifact、Join 与 Merge

多 coder 完整性取决于汇合而非“同时启动多个模型”：

- 每个 coder 产出 immutable patch/artifact identity 和验证证据；
- Join 只读取已冻结的 child outputs，不读共享可变目录猜结果；
- merge 顺序与算法确定，冲突有唯一 owner，不能 last-writer-wins；
- 合并后必须在集成 Workspace 上重新 Verification；
- 增加独立 post-code Agent Review，再进入人工 Approval 和 Git CAS Promotion；
- 任一 child 成功不等于 ProductTask 成功，最终 terminal 由 Product owner 根据完整安全尾部写入。

### 9.7 W4：有限 replanning、观察与发布停止点

允许模型根据 durable 中间结果在剩余包络内提出一次或有界次数的 plan revision；每次 revision 都重新验证、
冻结和记录，不能原地修改执行中 DAG。Line/TUI 展示 plan、并发 children、各自 Context/Tool/Artifact、Join、
Budget 和收敛状态。

至少通过：并发度上限、跨 worktree 隔离、冲突 merge、child crash、cancel race、Budget exhaustion、恶意 plan、
安全尾部绕过、Promotion CAS 冲突和 deterministic replay 反例。

## 10. v1.0 RC：冻结与最终发布

v1.0 RC 不再增加功能，只做整合、清理、兼容边界与发布证据。

### 10.1 稳定边界

- 明确 `traceh.api`、`traceh.sdk`、Plugin、MCP Plugin、事件协议与 CLI 的稳定/实验范围；
- 为每个持久协议决定“支持迁移”还是“pre-1.0 数据目录明确拒绝”，不默认承诺万能 upcaster；
- 删除已经被主线替代的别名、双 parser、fallback 和 feature island；
- 发布第三方 Plugin/MCP contract test kit 与最小示例，但示例值不能成为隐藏默认；
- 冻结版本、包 metadata、extras、Python/OS/backend 支持矩阵和迁移指南。

### 10.2 联合安全与质量门禁

- Memory/Context/History source boundary 与 Prompt injection；
- Sandbox path/network/secret/resource/process-tree 隔离；
- Plugin/MCP activation、Generation/Lease/Drain、取消与 unknown commit；
- 动态 Workflow identity、并发、Artifact/Join/Merge、Verification/Review/Approval/Promotion；
- EventStore replay/invariants、Budget reconciliation、Session ownership 与 shutdown；
- TUI/Line 可用性、窄屏、颜色/遮蔽、错误诚实显示和上下文透明度；
- performance/durations、长 Session、多任务、多 Agent 和 output flood；
- clean-input Wheel/sdist/source ZIP、core/各 extras 离线安装、支持平台真实 Sandbox 和授权真实 Provider/MCP 验收。

最终独立审查清零 P0/P1 后，运行一次从头开始的最终全量；确定性失败修复后必须重新跑到绿色。然后才可在
用户授权下 commit/tag/release。

## 11. 用户验收旅程

### 11.1 长项目不失忆

用户在多个 Session、多个 ProductTask 后询问“我们为什么选择这个架构、现在在哪个阶段”。模型从 active
Workspace Memory 得到最小权威答案，能列出相关任务/证据索引；需要细节时只展开对应历史 block，不把整个旧
聊天塞回 Context。

### 11.2 历史证据不冒充现状

模型展开一段旧工具结果时，界面和 request 都标明当时 seq/freshness。若用户问“现在测试是否仍通过”，模型
必须走当前读取或 Verification，而不能把旧的 `exit=0` 当作新事实。

### 11.3 安全接入外部 MCP 工具

用户启用一个官方 MCP Client Plugin 配置。宿主展示 server 与权限；模型发现 Tool 后仍需通过现有 Tool Policy、
Budget、网络和 Sandbox。server 声称自己“只读”不会覆盖宿主的 effect 分类。

### 11.4 多 Agent 并发修改

模型在允许的模板/包络内把互相独立模块分给两个 coder。每个 coder 使用独立 Session/worktree；Join 检查
artifacts，确定性合并，集成测试与 post-code Review 通过后才出现人工 Approval。取消时所有 children 和进程树
收敛，ProductTask 不会假完成。

### 11.5 用户可解释本次模型输入

用户能查看最近冻结请求中的 system、Memory、Product facts、Surface、History Evidence、Skill 与 Tool schema
分别占多少、来自哪里、为何命中或被排除；该视图只读同一 EventStore，不产生新的 Prompt 事实。

## 12. 阶段门禁与交付节奏

每个版本遵循相同节奏：

1. **F0/设计冻结**：按真实 HEAD 明确 owner、identity、协议、线性化点、失败/取消和不做项；
2. **分批实现**：每批只改一个 owner 或一条端到端主线；
3. **定向与相邻回归**：正向、关键反例、失败/取消、反向验证；
4. **Release Stop**：独立审查，只接受符合 `AGENTS.md` 证据门槛的 Finding；
5. **集成检查点**：跨共享事实源/Runtime/外部副作用后运行一次无筛选全量；
6. **发布检查点**：打包、离线安装、支持平台/Provider/MCP 的授权真实验证；
7. **提交与发布**：只有用户明确授权后执行。

日常小批次不重复跑全量。全量只放在计划中的集成或发布点；但一旦最终全量出现确定性失败，修复后重跑到
绿色是同一检查点的一部分，不能省略。

每批报告必须包含：

- 根因与所属 owner；
- 修改文件与协议变化；
- 正向/反例/失败或取消证据；
- 反向验证；
- 两份上下文同步章节；
- 未运行门禁和剩余边界；
- git status，且不把未授权操作写成已完成。

## 13. 明确排除到 v1.0 之后

以下能力不属于本路线的 v1.0 完成条件：

- 多主机分布式 Runtime、集群调度与远程 EventStore；
- 多用户、组织、RBAC、云托管控制面；
- 云端 Workspace Memory、外部向量数据库或默认联网 embedding；
- MCP Server 模式与通用 MCP gateway；
- 任意 Python/脚本 Workflow、无界循环/条件、自修改 DAG；
- 模型自行批准 Memory、Workflow 权限扩张、最终 Approval 或 Promotion；
- 未受控社区 Plugin 的进程内执行；
- 静默 Provider/model fallback；
- OpenTelemetry 产品化、实时 token streaming 与大规模可观测平台；
- 把所有 in-flight crash recovery、跨进程 scheduler 接管或 exactly-once 外部世界写入包装成已解决。

这些能力未来可以重新规划，但不得以“顺手加上”为由进入 v0.9-v1.0 主线。

## 14. 需要在各阶段现场决定的事项

以下问题现在不应猜答案，必须在对应 F0 用真实实现/平台证据决定：

| 决策 | 决定阶段 | 当前约束 |
|---|---|---|
| Sandbox 支持的 OS/backend | v0.10 S0 | 必须真实隔离、不可静默降级 |
| MCP 协议版本与 transport | v0.11 C0 | 以届时官方规范为准 |
| 动态 plan schema 与模板集合 | v0.12 W0/W1 | typed、无任意代码、安全尾部不可绕过 |
| 是否在 v0.12 扩展通用冷恢复 | v0.12 W0 | 不得由 UI 文案提前承诺 |
| pre-1.0 持久数据迁移范围 | v1.0 RC | 逐协议决定迁移或明确拒绝 |
| v1.0 稳定 API 范围 | v1.0 RC | 小而可验证，不冻结内部实现细节 |

## 15. v1.0 完成定义

只有同时满足以下条件，v1.0 才闭环：

- 长期项目事实、任务事实、聊天 Surface、压缩摘要与历史证据各有明确 authority，统一进入可重建 Context Input；
- 模型在多 Session、多 ProductTask 中不会因为缺少宿主事实而表现为系统性失忆；
- effectful 代码执行处于真实、可说明、fail-closed 的 Sandbox 边界；
- 外部能力通过官方 MCP Client Plugin 接入并经过现有 Tool/Policy/Budget/Audit 主线；
- Product 层可以在宿主能力包络内使用受限 DAG 和并发 Agent，同时保持独立 Context、Artifact、Join/Merge、
  Verification、Review、人工 Approval 与 Git CAS Promotion；
- 所有权威结果仍来自唯一 EventStore，索引/UI/缓存均可重建；
- 用户能解释本次模型看到了什么、执行了什么、为何批准或拒绝；
- 文档、代码、测试、协议、打包与真实平台验收一致，最终全量和发布门禁绿色；
- v1.0 没有通过隐藏 fallback、虚假 Sandbox、双事实源或模型自我授权换取“看起来能用”。

达到这里，TraceHarness 可以诚实地定位为：**一个本地单用户、可审计、可重放、具备长期记忆、标准外部工具
互操作、真实执行隔离和受控多 Agent 并发的 Coding Agent 宿主**。它仍不是通用云平台，但已经不是依赖演示路径
才能成立的玩具。
