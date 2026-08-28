# TraceHarness v0.7 总阶段计划与目标效果

状态：v0.7.0 已发布；v0.7.1 维护候选修复中
当前版本：`0.7.1`（未发布）
目标发布版本：`0.7.0`
维护候选目标：`0.7.1`

本文只规定 v0.7 的产品目标、阶段边界、架构约束、验收顺序和最终用户效果。各阶段的协议理由以对应 ADR 为准，当前工程事实以源码、测试和两份项目上下文为准；本文不是新的事实源，也不替代它们。

## 1. v0.7 要解决的问题

v0.6 已经具备进程内多 Agent 的身份、收件、投递、生命周期和模型 Tool，但仍主要是宿主可编程的底层能力。v0.7 的目标是把这些能力接成一条受控 Coding Agent 产品主线：

1. 用户继续在同一个 `traceh chat` 里自然聊天；
2. 普通问答只产生普通 Session，不创建任务；
3. 当对话形成可执行的编码需求时，系统先给出一张有限、可检查的任务提议；
4. 用户确认后，宿主才创建持久 ProductTask；
5. 用户可显式选择 `single`、`multi`，或让严格 Router 在二者中选择；
6. single 与 multi 都复用同一 Budget、Workspace、Artifact、Verification、Approval、Promotion 和 Workflow 主线；
7. 模型可以完成工作、提出候选和报告结果，但不能批准自己的产物，也不能直接推广 Git ref；
8. 人工批准后，宿主用精确摘要和 compare-and-swap 推广；
9. 任务中断、重启或隔天回来后，可以凭 durable task identity 读取真实状态，而不是依赖内存聊天对象；
10. 最后用少量真实任务比较 single/multi，并只把 auto 当作路由决策和路由开销来度量。

这不是“模型任意修改自己”。它是**宿主约束、事实可重放、结果可验证、人工批准后才改变目标**的能力扩展流程。

## 2. 不可偏离的架构原则

### 2.1 事件事实仍是唯一持久事实

- Session、Agent、Budget、Workspace、Artifact、Promotion、Workflow、ProductTask 各自在自己的 append-only stream 中记录本域事实。
- ProductTask 只记录产品状态和对其他域身份的引用，不复制 Workflow 节点状态、Budget 余额、Workspace 路径、Patch bytes 或 Promotion 结果细节。
- 聚合视图必须 fresh replay 各自事实后派生；不得把 `runtime.state`、可变 messages、CLI 内存对象、JSON 缓存或 benchmark 脚本变成第二事实源。
- CAS bytes 仍由摘要寻址，事件只保存不可变 Manifest/identity。

### 2.2 核心 Runtime 保持轻且无产品分支

- `AgentLoop` 继续只负责编排一个 Turn/Step。
- `AgentRuntime` 不保存 Budget balance、Workspace path、ProductTask 状态或 Workflow DAG。
- `ProcessAgentSupervisor` 不变成产品控制器、Workflow engine 或 Git 管理器。
- `PluginManager` 不承担任务路由、审批或推广。
- 新能力优先落在独立域服务和薄宿主 adapter；只有存在真实第二消费者时才引入新抽象。

### 2.3 权力由宿主分离

- Router 只能返回 `single` 或 `multi`，不能生成 DAG、选择 Agent 数量、修改 Budget、路径、Verifier 或 Promotion target。
- Coder 可写 managed Workspace；Parent/Reviewer 只读。权限来自宿主 Profile 槽位，不来自模型输出。
- 模型上下文不含 approval digest、完整 Patch SHA-256、promotion 前后精确 revision。
- Approval 和 Promotion 是宿主操作，不能暴露成模型 Tool，也不能由 Workflow 节点自行批准。

### 2.4 pre-1.0 保持一条清晰主线

- 不兼容已经被当前架构替代的旧 Budget、旧 eval manifest、旧 DTO 或旧 ProductTask schema。
- 不保留 Legacy/V2、别名、双 writer、双 projector、自动迁移或静默猜测。
- 旧数据不自动删除；不支持的历史明确拒绝，并要求使用新的数据目录或显式离线迁移。

### 2.5 所有边界都必须可判定

- 身份、owner、store object、Session、message、Turn、Workspace、Artifact、Review、Approval 和 revision 必须跨边界精确核对。
- 取消、失败、cleanup 和 may-have-committed 必须在公开调用返回前收敛，且不遮蔽任何独立失败。
- 并发测试用 Event/Gate 固定窗口，不用 `sleep()` 猜时序。
- 通用实现不得出现示例任务、测试夹具、特定模型、某台机器路径或某个插件名称的隐藏默认值。

### 2.6 明确不声称的安全能力

- v0.7 不是 OS sandbox；Verifier 和 Agent 仍以宿主用户权限运行。
- v0.7 不提供跨进程 Activation/Workspace/Promotion lease。
- v0.7 不自动接管硬崩溃留下的 stale claim，不提供通用自动重试。
- Read-only 是 Tool/host policy，不是文件系统强制隔离。

## 3. 阶段总览

| 阶段 | 结果 | 当前状态 |
|---|---|---|
| D0 | 收紧公共接缝、权限和依赖方向，不创造新能力 | 已完成并提交 |
| A | 用单一 append-only 层级 Budget Ledger 替换 v0.6 的记录式 Budget | 已完成并提交 |
| B | 在既有 owned boundary 强制模型、Step、Tool、wall、process 与 child Budget | 已完成并提交 |
| C | 为 managed Agent 提供 commit-pinned Git worktree 和 Catalog | 已完成并提交 |
| D1 | 把完整 Git candidate tree 冻结为 immutable Patch Artifact + CAS | 已完成并提交 |
| D2 | 固定 Verification、Review、人工 Approval 和 bare Git ref CAS Promotion | 已完成并提交 |
| E | 用固定 Typed Workflow 组合公共服务，不引入通用 DSL 或第二调度器 | 已完成并提交 |
| F0 | 冻结统一 Chat 产品合同、DTO、事件、Profile、Assembly 和架构守卫 | 已完成并提交 |
| F1 | 实现 ProductTask append-only 事实层、严格 replay、幂等写入与派生视图 | 已完成，独立审查与最终门禁通过 |
| F2 | 实现严格 Router、Profile Registry 和 Product Assembly | 已完成；独立审查与最终全量通过 |
| F3 | 接入统一 Chat 产品控制面并完成真实端到端验收 | 已提交；独立复审与最终全量通过 |
| F4 | 重构现有 `traceh eval`，完成 single/multi/auto 的小规模真实度量 | 已完成并提交；独立复审清零 P0/P1/P2，唯一一次最终全量通过 |
| F5 | v0.7 RC、安全/打包门禁、版本、tag 与 GitHub Release | 已完成；第七轮 18/18 可度量、16/18 成功且 DNS/TLS failure 为 0；安全扫描、最终复审、唯一一次最终全量、最终提交的干净打包/归档审计与离线安装通过；annotated tag 与 GitHub Release 已发布 |

## 4. 已完成基础阶段的固定效果

### D0 — 公共接缝

- Toolset 依赖真实 Supervisor Protocol，而不是具体实现或 `object`。
- 权限每次 fresh replay durable Directory。
- child 创建必须经过宿主显式 `ChildProvisioningPolicy`。
- 不提前实现 Budget、Workspace、Workflow 或产品入口。

### A/B — 层级 Budget 与执行门

- 只有一条 `budgets:ledger` 和一个 projector，没有 mutable balance cache。
- root grant、child reservation、usage reservation/settlement 和 account close 都是事实。
- managed create、模型、Step、Tool、wall 和 process slot 复用现有 owned boundary。
- 无 trusted tokenizer 时保守 hold，不伪造精确 Token 证据。
- 旧 Budget schema 明确拒绝，不兼容、不双读写。

### C — Managed Git Workspace

- Catalog 是 Workspace 生命周期唯一事实源。
- worktree 固定到精确 commit，并核对 `.git` marker 与 registry 中的精确 admin directory。
- dirty、unsafe 或 identity 不一致时 quarantine；Agent dispose 不自动删除证据。
- Workspace wrapper 的新增异步尾部必须纳入 close 收敛。

### D1/D2 — Patch、验证、批准与推广

- D1 捕获 staged/unstaged/untracked/deleted/binary/mode 的完整 candidate tree。
- Manifest 派生身份由 replay 重算；CAS 路径逐层拒绝 link/Junction/reparse escape。
- Git 子进程清除继承的 `GIT_*`，候选 checkout 必须与被审计 tree 字节一致。
- D2 在临时 clone 中应用精确 Artifact，运行固定 Verifier，生成 immutable Review。
- 人交回 exact approval digest 后，宿主只向受管 bare 仓库执行 ref compare-and-swap。
- 取消、主失败和 scratch cleanup 失败都完整保留。

### E — 固定 Typed Workflow

- 只有 `AgentTask`、`Map`、`Join`、`Verification`、`Approval` 五类固定节点。
- 定义只带宿主 binding id，不带 Prompt、路径或自由策略。
- 节点、message、child、review 等身份由 run/node/key 派生并由 replay 重算。
- 重入读取事实，不重复外部副作用。
- Approval 是可继续的唯一人工屏障；其他 open node fail closed。
- Workflow 组合公共服务，不替代 Supervisor，不直接 Promotion。

## 5. F0/F1 — 产品合同与事实层

### F0 已冻结

- `RequestedTaskMode` 有 `single`、`multi`、`auto`；执行态的 `ResolvedTaskMode` 只有 `single`、`multi`。
- ProductTask 有自己的 durable identity 和九种精确事件；proposal 本身是确认前的临时值，不是事实。
- 用户确认必须点名 proposal，是不同于 origin 的消息；origin/confirmation 的 claim 都必须由真实 durable Turn start 锚定，同一 Session 还必须通过核心生命周期不变量，最后由持久 seq 证明 confirmation accepted 发生在 Proposal Turn 的合法 `turn/end` 之后。
- v0.7.1 明确修正：上述 Session 事实只证明后续用户消息的身份与顺序，不证明自然语言授权；交互式 Chat 还必须取得绑定当前精确 task 的宿主终端 `START`，模型 Tool 只能请求该提示。
- `ProductPreflightBinding` 冻结 Profile、角色装配、Router、仓库、base revision、Verifier，以及 Promotion target 的仓库身份、精确 ref 与 revision。
- `ProductAssemblyReceipt` 冻结 resolved mode、Workflow definition 和 preflight binding。
- single 与 multi 都是固定 Workflow profile；single 不是绕过安全门的旧主线。
- durable status 与 derived view status 分类型，`interrupted` 不写事件。

### F1 当前交付

- `product-task:<task_id>` 是 ProductTask 唯一 durable stream。
- parser 校验 stream、连续 seq、schema、事件类型、精确 key、迁移和跨事件取值。
- writer 使用 canonical payload 幂等、stream CAS、owned task 和 may-have-committed 三态对账。
- opening 的 payload、确认规则与 Session 证据共享一次规范化；每条 Product 事件也只解析一次，投影与幂等对账只读系统拥有的内建值。
- Session 与 Workflow reader 必须和 ProductTask writer 共享同一 EventStore 对象；`open_task` 只认通过共享 `CoreInvariantChecker` 的 Session 历史，origin 与 confirmation 都必须是 `source="user"`、`target="new_turn"` 的消息、被精确 claim 且其 Turn 有 durable start；一个 Turn 只能归属一条 claimed message，确认不能借用别的消息启动过的 Turn。确认的 accepted seq 还必须晚于 Proposal Turn 的合法 durable end。Session payload 先脱离成系统拥有的内建值，普通 Store 读取错误统一 fail closed，调用方控制的 `BaseException` 不被改写。
- 所有 preflight/receipt 字段在首次 append 前验证，畸形对象不能先污染 stream 再让 replay 失败。
- `view()` fresh 读取 ProductTask、Workflow status 和 process ownership，派生 `unreconciled`、`resumable` 或 `interrupted`。
- F1 只记账，不 import 或驱动 Router、Workflow execution、Promotion、CLI、Provider 或 Plugin。

## 6. F2 — Router、Profile Registry 与 Product Assembly

F2 只负责“把已经确认的 ProductTask 装配成一个精确、可执行但尚未执行的宿主计划”。

### F2 当前交付

- `traceh.product` 新增 `router.py`（严格 `TaskRoutingParser` 实现与由 `ProductRouterProfile` 定界的 owned `ProductModeRouter`）、`registry.py`（唯一 `ProductProfileRegistry` 与三个派生 assembly digest）、`topology.py`（single/multi 两种固定 Workflow 与派生身份）、`assembly.py`（`ProductAssemblyService.preflight`/`assemble`）。
- Router 只认恰好一个 JSON 对象、键集恰好 `mode`/`reason`；未知 mode、多余键、超长、畸形 JSON 与多份答案一律稳定失败，不重试、不猜文本；超时与响应上限只来自显式 Profile，live Router 的实际 assembly 身份还必须匹配 fresh preflight；`reason_display` 只展示，任何生产分支不读它。
- 注册表没有默认 profile；解析器交回的 assembly 必须匹配被问的槽位，写权限来自 `ProductRole.workspace_access`，Router 不持 Tool——两条不变量当场强制。
- 预检每次重新解析：source 精确 commit、VerificationPlan digest、Promotion target fingerprint/精确 ref/expected revision 全部来自真实 resolver 结果；drift 是拒绝，不是重绑。
- 装配用 F1 的 writer 写唯一 `product/task-routed`（operation id 派生，重试是同一次写入），并从真正会跑的 Workflow definition 算 `workflow_definition_hash`；本阶段不写 `product/task-started`。

必须完成：

1. 严格 Router：输入是有界任务摘要，输出经 `TaskRoutingParser` 解析为有限枚举；超时、超长、格式错误或额外字段立即失败，不用自由文本猜测，也不自动重试。
2. Router 是纯决策边界：不持有 Supervisor、Workflow、Promotion、Workspace、Registry 写句柄或 Tool。
3. explicit `single`/`multi` 不调用 Router；`auto` 只在宿主预检确认两种装配都可用后调用。
4. Profile Registry：由显式 id 解析 provider/model、三角色 preset/capability/budget、router、source、verification plan 和 promotion target；缺失或歧义失败。
5. Product Assembly：核对 store object、profile digest、role assembly、repository fingerprint、base/expected revision、verification plan 和 target，产出精确 `ProductAssemblyReceipt`。
6. single profile 固定为一个 `AgentTask` 后接 Verification/Approval；multi profile 固定为 parent/reviewer/coder 协作拓扑后接同一安全尾部。不得让 Router 或 task JSON 携带节点、边或扇出。
7. 本阶段不执行 Workflow、不改 Chat、不调用真实模型、不批准、不推广。

完成标志：任何 ProductTask 在执行前都能得到唯一 receipt，或以稳定错误拒绝；不存在隐式默认、自由 DAG 或第二 Registry。

## 7. F3 — 统一 Chat 产品控制面与真实验收

F3 把现有行式 `traceh chat`、F1 ProductTask、F2 Assembly、Stage E Workflow 和 D2 Promotion 接到同一宿主控制面。

产品行为：

1. 普通聊天继续原样运行；没有 proposal/人类确认就没有 ProductTask。
2. 模型只能提出结构化 proposal；用户明确指定时可附带 single/multi/auto，否则使用 Profile 默认值。宿主用固定面板显示模式、来源、Profile、仓库、安全边界和确认后将使用的唯一 prospective task id。
3. 用户在后续 Turn 表达意图时，宿主仍记录该 message/Turn 作为身份与顺序证据；模型的确认 Tool 只能请求一次绑定当前精确 task 的宿主提示，只有终端用户再输入固定 `START` 才创建 ProductTask。宿主命令仍属于同一控制面，不能形成另一条任务主线。
4. explicit single/multi 或 auto resolved mode 经 F2 装配后启动现有 Workflow。
5. 到 Approval 屏障时，宿主渲染 Review evidence；模型上下文不获得 secret approval/promotion values。
6. 人工批准后，由产品控制器显式调用 Promotion；Workflow 自己不推广。
7. `inspect`、`approve`、`reject`、`cancel`、`abandon` 等宿主操作都按 task identity fresh replay，不依赖当时的聊天内存。
8. `interrupted` 不伪装成可恢复；只有 Stage E 已证明的 Approval 屏障可继续。
9. `product/task-opened` 以后、Workflow 启动以前的普通 Router/装配失败必须先复用现有 owner 清理，再写失败终态；清理失败不能伪造收敛。

真实验收至少覆盖：

- 同一真实模型完成一次 normal chat 不创建任务；
- 一次 single：确认 → Workspace → Agent → Artifact → Verification → Approval → Promotion；
- 一次 multi：parent/coder/reviewer 协作后走同一安全尾部；
- 一次 auto：严格解析为 single 或 multi，并记录路由证据；
- 一次用户拒绝或取消，不移动目标 ref；
- 一次进程重启后凭 task id 读取 awaiting approval，并由人继续；
- 结束时 process slot 为零、Budget reservation 全部 terminal、没有遗留 live Activation，worktree/CAS/Review 状态与事件一致；
- 推广目标是一次性本地 bare 仓库，不接触真实远端。

F3 验收通过后才进入性能度量；一次真实模型运行不能证明权限不可越界，否定性质仍由确定性架构测试证明。

## 8. F4 — 复用并重构 `traceh eval`

不得新建第二套 benchmark 命令。F4 重构现有 `traceh eval`，并明确拒绝旧 manifest，不留兼容层。

度量原则：

- 使用少量、通用、彼此不同的真实编码任务，不针对某个示例插件或已知实现调参。
- Benchmark/evaluator 由宿主冻结，候选执行方不能修改题目、评分器、Gate 或批准自己的结果。
- single 与 multi 使用相同模型族、任务、目标仓库、Verifier、Budget 规则和 Promotion 规则。
- 每种模式至少重复少量次数；若成本限制只能 `n=1`，报告必须写“单次观测”，不得宣称统计显著。
- multi 的人工等待时间单列；可用程序化即时批准测 active latency，但必须明确标注，不能把人离开电脑的时间当执行性能。
- auto 不是第三个质量 arm；只报告 resolved mode、严格解析是否成功、路由 Token 和路由耗时，再归入实际 single/multi 结果。

所有指标必须来自持久事实或宿主边界的单调时钟：

| 指标 | 来源/口径 |
|---|---|
| 是否成功 | Workflow terminal + Verification + Promotion receipt |
| routing tokens | Router Session/Usage 与 ProductTask routing identity |
| execution tokens | 任务关联的 Agent Session Usage 与 Budget Ledger |
| active elapsed | 排除 approval wait 的阶段单调时钟区间 |
| approval wait | awaiting-approval 到宿主决定的独立区间 |
| wall elapsed | 从确认到最终 terminal 的总区间 |
| steps / tool calls | Session durable Turn/Step/Effect facts |
| multi 累计工作时长 | 所有参与 Agent 的 durable Turn 区间求和 |
| 预算结果 | Ledger reservation/charge/settlement facts |

报告只描述观察结果和取舍，不让模型自评“变好了”。

### F4 当前交付

- `traceh.evaluation` 是与 `traceh.cli` 平级的第二个 composition root：它调用同一个 `build_product_chat_host()`、驱动同一个 `ProductTaskControlPlane`，没有第二个任务状态机、调度器、Workflow 或“成功”的第二个定义。为此 `ProductChatHost` 公开 `control`，Product host 配置 schema 的共享一半收敛到 `parse_product_host_settings()`。
- `benchmark.json` 是 schema 1 + 精确键集；它不能命名仓库、推广目标、provider、model、节点、边、Agent 数量或 approval digest。每次 attempt 的源仓库与一次性本地 bare target 由 Runner 从任务 `initial/` 树自建，因此“不接触真实远端”是结构性质。provider/model 来自 `--provider`/`--model`。
- 需求与模式来自 manifest 并直接交给控制面；一个宿主冻结、无 Tool 的 requester Provider 只负责产生 `product/task-opened` 需要的真实 Session 证据。
- 指标全部落在上表口径上；推不出来的报告为 `unavailable` 而不是 0，`UsageQuality.UNKNOWN` 会让该 Session 的 Token 总数变成 unavailable。Session 报告的 Token 与 Ledger 结算的 Token 并列呈现，不互相冒充。
- 质量聚合按 resolved mode 分组，auto 只单列路由结果与开销；`n=1` 标注 `single observation`；每个任务跨 arm 的 requirement/profile/source revision/verifier 摘要被核对并在分歧时点名。
- 失败与取消通过同一控制面收敛既有 owner 再写终态；证据目录不删。退出码回答“度量是否完成”。
- 旧 `*/case.json` 布局稳定拒绝（`benchmark-legacy-manifest-rejected`），不读取其内容、不升级、不删除用户旧数据。
- 设计决定记录在 [`docs/adr/0033-product-task-benchmark-as-the-single-eval-path.md`](../adr/0033-product-task-benchmark-as-the-single-eval-path.md)，工程事实见两份上下文的 20.30 / 20.24。

F4 已提交。F5 的真实外部模型验收现有多轮 `18/18 measured` 历史证据；其中第五轮 `15/18` 只证明 ADR-0034 前的旧 Profile。手工 Chat 促成累计 Token Budget/每请求上限根因拆分、可读审批/进度与 Windows 全 CLI UTF-8 后，当前 Profile 的第六轮在显式无代理进程和新仓库外目录再次得到 `18/18 measured`、`15/18 success`，三个失败均为 durable Windows DNS。DNS-only 诊断随后证明 WLAN 的首选 DHCP DNS 直查 UDP `0/50`、TCP `0/10`；换成 `223.5.5.5`/`223.6.6.6` 后，Windows resolver `200/200`、Provider 同路径无 Key 探针 `50/50`。全新第七轮得到 `18/18 measured`、`16/18 success`：single `5/6`、multi `5/6`、auto `6/6`，auto 全部严格解析到 single，DNS/TLS failure 均为 0；余下是一次远端断开和一次累计 Budget fail-closed，全部 Budget/Workspace owner 收敛。独立复审、唯一一次最终全量、F5 安全扫描、最终提交后的干净打包/归档审计和离线安装均已通过；版本事实源与验证记录为 `0.7.0`，annotated tag 与 GitHub Release 已发布。工程事实见两份上下文的 20.31 / 20.25。

## 9. F5 — v0.7 RC 与发布

发布前必须：

1. 独立 Sol 审查各阶段没有 P0/P1；
2. compileall、修改范围 Ruff、文档 QA、`git diff --check` 和完整 pytest 全绿；
3. 真实 single/multi/auto 验收证据可重放；
4. 校验四个核心文件没有产品层增重；
5. 安全扫描确认无真实 Key、本机路径、测试夹具硬编码、模型可见 approval/promotion secret；
6. Wheel 在干净输入中构建并审计内容；
7. 将版本更新为 `0.7.0`，同步 CHANGELOG、README、Roadmap、正式/通俗上下文和验证记录；
8. 创建并推送提交与 `v0.7.0` tag；
9. GitHub Release 附带精确构建资产、摘要和真实验证说明，并设为 Latest。

完成状态：第 3 项由当前 Profile 的第七轮完成。它在修正 WLAN DNS 后从新仓库外目录运行 3 个任务 × `{single,multi,auto}` × 2 次，`18/18 measured`、`16/18 success`；DNS/TLS failure 为 0，两次失败分别是远端主动断开和累计 Budget fail-closed，JSON/Markdown 与资源收敛已复核。第六轮的三个 DNS 失败与全部更早历史报告均保留，不能选择性隐藏。验收发现的 D1/D2 新目录 leaf-entry、Router 模型可见合同、Chat 可读性、Windows UTF-8 与 ADR-0034 Token 两层上限均已按现有 owner 根修。两项独立审查 P1 由 Promotion owner 共享规则保护 inspect/review/approve/promote、F4 collector 与 crash-prefix recovery；相关反例和七类关键保护均反向验证。最终复审 P0/P1/P2 清零，唯一一次完整 pytest 为 2402 通过、5 跳过、退出码 0。第 5 项安全扫描检查了 377 个受控文本文件，没有真实凭据形态、当前机器路径或生产夹具硬编码；第 4 项 protected-core 零 diff 已验证。第 6–9 项全部完成。

### 9.1 v0.7.1 维护修复

发布后的独立审查确定了两个 P1 和一个受支持平台缺陷：模型确认 Tool Call
实际承担了 ProductTask 启动授权；AgentLoop 的两层裸 `shield()` 在重复取消时可
让关闭事实晚于公开调用返回；Python 3.13 的发行版默认 sysconfig scheme 可在
`-I -S` 下把目标 venv 的包目录推到不存在的位置。v0.7.1 只修这三条根因：
宿主对精确 task 再要求固定 `START`，AgentLoop 用单一 owned finalizer 顺序关闭
Attempt/Step/Turn，L4 对目标 venv 显式选择 `venv` scheme 并校验路径归属。
三项均有确定性公开路径反例和反向验证。最终独立复审、唯一一次全量、打包、
提交、tag 与发布尚未执行，不得把本段当作发布完成记录。

## 10. 每个阶段的交付纪律

- 一个阶段只实现该阶段，不偷跑下一阶段。
- 先根因、身份、所有权、事实源、取消/清理边界，再写代码。
- 新增正向、关键反例、失败/取消测试；关键修复必须反向验证。
- 每轮修复优先跑定向和相邻门禁；独立审查无阻断后只跑一次最终全量，避免重复十分钟级全仓测试。
- 代码、测试、正式上下文、通俗上下文和 CHANGELOG 同步进入同一提交。
- 每阶段提交前交给独立 Sol 做只读审查；P0/P1 清零、最终门禁全绿后才提交。
- 审查统一遵守根目录 [`AGENTS.md`](../../AGENTS.md) 第 8 节：Finding 必须来自当前公开路径并有确定性影响；不把未来能力、设计偏好或无限扩张的敌意对象形态当成阻断项。独立审查清零 P0/P1 后停止扩张并进入一次最终门禁。
- 不因全量测试偶尔通过就忽略确定性反例，也不因基线问题就红灯交付。

## 11. 最终用户体验示例

```text
用户：帮我给这个 Python 项目增加一个安全的配置校验。

TraceHarness：可以。我建议创建一个编码任务：
  模式：auto
  Profile：python-quality
  仓库：已固定到 commit <display id>
  安全边界：候选只进入 managed worktree；检查后仍需你批准才会推广。
  要启动这个精确 task，请在宿主终端输入 START。

用户（聊天消息）：我接受这项提议。

TraceHarness：模型已请求启动，但普通聊天文字不是执行授权。
  Start exact ProductTask task-...? Type START to authorize:

用户：START

TraceHarness：任务 task-... 已创建。Router 选择 multi。
  parent 正在拆分工作，coder 在独立 worktree 修改，reviewer 只读检查。

TraceHarness：候选已通过固定验证，等待你的批准。
  这里展示人可读报告，不把 approval digest 或精确推广参数交给模型。

用户：批准。

TraceHarness：已用 ref compare-and-swap 推广；任务 completed。
```

如果用户只是问“这个错误是什么意思”，系统正常回答，不创建 task。用户显式指定 single 时不调用 Router，但仍经过 Workspace、Artifact、Verification、Approval 和 Promotion。用户第二天回来可凭 task id 读取真实状态；没有可证明的恢复条件时显示 interrupted 并要求人工处理，而不是擅自重跑。

## 12. v0.7 明确不做

- 通用 Workflow DSL、任意 DAG、循环/条件语言；
- Router 自由生成 Agent 数量或拓扑；
- 模型自动批准、自动推广、直接修改目标仓库；
- 硬崩溃后的通用自动恢复、stale claim takeover、跨进程 lease；
- OS/container sandbox；
- 自动生成 Benchmark 后自己评分自己；
- L5 弱点归纳、自动生成插件、自动安装插件的完整自我进化闭环；
- MCP、TUI、流式模型输出等无关产品扩展。

这些能力可以在以后基于 v0.7 的 durable identity、Budget、Workspace、Artifact、Workflow 和人工批准边界继续扩展，但不得以“以后可能需要”为理由提前加重当前核心。

## 13. v0.7 完成后的系统能力

v0.7 发布后，TraceHarness 将从“拥有多 Agent 构件的 Runtime”提升为“可以在统一聊天中创建、执行、检查、批准和推广真实编码任务的受控 Coding Agent”。它同时具备：

- 单 Agent 与固定多 Agent 两条可选择主线；
- 严格且受限的 auto 路由；
- 可执行的层级 Budget；
- 每 Agent managed Git Workspace；
- immutable Patch Artifact；
- 固定 Verifier、人工批准与 Git ref CAS；
- 可重放的 Typed Workflow 与 ProductTask；
- 从 durable evidence 得出的基础 Token/耗时/步骤度量；
- 重启后仍可识别任务及人工屏障的产品身份。

能力变强，但 `AgentLoop`、`AgentRuntime`、Supervisor 和 PluginManager 不成为“什么都懂”的巨型核心。这是 v0.7 最重要的完成标准。
