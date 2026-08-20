# TraceHarness Py 项目上下文（正式版）

> 本文维护当前仓库的工程事实和全局视野，不是开发日志。
>
> 维护顺序：先核对真实代码和测试，再更新本文，最后同步通俗版 [`project-context-plain-zh.md`](project-context-plain-zh.md)。

## 0. 文档契约

### 0.1 用途

本文供开发者和 Coding Agent 在开始任务前恢复项目全局上下文，回答：

- 当前版本实际实现了什么；
- 稳定架构边界在哪里；
- 运行时事实怎样持久化、重建、验证和恢复；
- 修改某一层可能影响哪些层；
- 当前已知限制和验证基线是什么。

### 0.2 事实优先级

发生冲突时以当前源码、测试和配置为最高事实源；本文其次；通俗版由本文派生。`CHANGELOG.md`、Roadmap、ADR 和历史实施计划各自保留版本历史、未来计划或设计原因，不替代当前状态核查。

### 0.3 更新原则

- 持续改写成最新状态，不在文末堆积流水账。
- 代码行为、目录职责、状态、配置、验证结果或架构流程变化时更新对应章节。
- Mermaid 图表达当前流程；历史案例必须明确标注为案例。
- 不记录真实 `.env`、Key、Token、本机隐私路径或秘密输出。
- 本文与通俗版使用相同的一级编号（当前为 0–19），便于逐章核对。

## 1. 当前项目状态

| 项目 | 当前事实 |
|---|---|
| 包名 | `traceharness-py` |
| Python 包 | `traceh` |
| 当前版本 | `0.4.0`。唯一事实源是 [`src/traceh/version.py`](../../src/traceh/version.py) 的 `__version__`；`pyproject.toml` 用 `[tool.setuptools.dynamic]` 读取同一属性，因此 Wheel metadata 与被导入的包不可能不一致 |
| 成熟度 | Educational alpha；可运行、可测试，公共 API 尚未承诺生产稳定性 |
| Python | `>=3.12`；CI 覆盖 Ubuntu 3.12/3.13 与 Windows 3.12 |
| 运行时依赖 | `packaging>=24.0,<27`——v0.4 引入的**第一个**第三方运行时依赖，用于 PEP 440 解析（见 1.1）。其余仍只用标准库 |
| 开发依赖 | pytest、pytest-asyncio、ruff |
| 当前 Agent 模型 | 单进程、单 Session 同时最多一个活跃 Turn |
| 持久化 | 本地 Append-only JSONL Session Stream 与 Effect Stream |
| 模型接入 | 确定性 Scripted Provider；非流式 OpenAI-Compatible `/chat/completions` Provider |
| Coding Tools | `list_files`、`read_file`、`search_text`、`apply_patch`、`shell`；插件可增加更多 |
| 插件系统 | **已实现**：`traceh.plugins` Entry Point 发现、显式启用、事务式激活，Stage A Generation/Lease/Drain、Stage B Generation-owned `PluginActivationSet`，以及 Stage C `traceh chat` 内的 `/plugins` 组合切换与 Session 显式迁移授权（见 19 节）。D1/D2 已把四层 Service 与程序化 Tool/Prompt/Policy 装配接入主线；D3 又让 application 插件通过 `PluginContext` 提供 Provider、Policy、Middleware 和命名 Verifier。插件 setup 仍只在 application scope、trusted、进程内运行，不能自行选择子层；EventStore 仍不是插件贡献面 |
| 完成判定 | 可选外部 `CompletionVerifier`；默认实现为命令退出码验证 |
| CLI 形态 | `traceh chat` 提供同一 Session 内的连续多轮行式交互，Turn 运行期间实时打印 Step/Tool Timeline 与 Activity Heartbeat（`--no-timeline`、`--heartbeat-seconds` 可调），首次 Ctrl+C 只取消当前 Turn 并保留 Session；空闲提示符支持 `/plugins`、`/plugins reload`、`/plugins use ...` 和 `--none` 的异步组合切换，不创建 Turn。其余命令仍是一次执行一个 Turn。不是流式 TUI。新增 `traceh plugins list/inspect/doctor` |
| 事件写入互斥 | JSONL Stream 在 POSIX 与 Windows 上均有操作系统级跨进程文件锁 |
| 当前自动化测试 | `1088` collected；全量门禁 `1087 passed, 1 skipped`；D1/D2 覆盖四层装配，D3 覆盖插件 Provider/Policy/Middleware/Verifier 的显式选择、注册时贡献身份冻结、公开 ActivationSet 交接复核及交接失败回滚、普通异常与直接 `BaseException` 的失败保真、跨 CLI/env 优先级、事务冲突/回滚、ActivationSet 身份守卫、旧式 ActivationSet 替换兼容、真实模型/工具/验证主线、旧 Lease 隔离和恢复命令保真；Stage C/D0 控制面与恢复契约继续全绿 |
| 内置 Benchmark | 1 个确定性修复案例 |

当前版本仍为 0.4.0。Stage C 已在 Stage B 基础上把用户控制面接入默认 Runtime 主线；D0 把候选替换、Session 身份迁移、共享 Gate 和在途控制面收敛归 [`PluginCompositionCoordinator`](../../src/traceh/runtime/plugin_composition.py)。D1/D2 把四层 Service 与 Tool/Prompt/Policy 宿主装配交给既有 ActivationSet→Generation→Lease→Snapshot 路径。D3 继续沿用同一事务和所有权边界：插件 Provider、Policy、Middleware 与命名 Verifier 会先在私有候选中校验，再作为一个 ActivationSet 进入 Generation；Provider/Verifier 必须由宿主显式选择，不能因“插件已安装/启用”而偷偷替换。Verifier 现在也处在 Step Lease 内，保证模型、工具和验证来自同一代。它仍不是完整 scoped plugin activation，EventStore 也仍由 Runtime/Session 固定持有。没有运行中 pip install/uninstall、强制 module reload、文件 watcher、isolated、多 Agent、Workflow、MCP、TUI 或流式输出；D3 完成也不等于 v0.5 发布。

### 1.1 为什么引入 `packaging`

Plugin Manifest 的 `requires_traceh`、插件之间的依赖版本区间、以及插件 Distribution 声明的 `traceharness-py` 依赖，三者都是 PEP 440 文本，并且都位于**信任边界**上：解析结果决定一段第三方代码是否被 import 并执行。自己实现一个不完整的 PEP 440 解析器，等于用一个未经验证的比较器守门。因此保留 `packaging` 并在 `pyproject.toml` 显式声明。

必须同时更新的一条旧事实：**“运行时只依赖标准库”从 v0.4 起不再成立**，README、通俗版和本表都已改写。离线 Wheel 验收的 wheelhouse 因此必须同时包含 `packaging` 的 Wheel，见 15.4。

### 1.2 版本为什么必须只有一个来源

`traceh.core` 的 `PluginIdentity` 会写进**每一条持久化的 Composition Snapshot**。如果版本散落在多处，两个来自同一次构建的 Runtime 可能给同一种 Step 写下不同的核心版本——这正是 Composition Snapshot 存在的意义所要排除的情况。

因此以下全部派生自 `traceh.version.__version__`：`pyproject.toml` 的项目版本（dynamic attr）、`traceh.__version__`、`CORE_PLUGIN_IDENTITY`、`TRACEH_PLUGIN_API_VERSION`、`PluginManifest.requires_traceh` 默认值、Generation Runtime 的默认 plugins、`AgentRuntime` 的默认 plugins、CLI 描述。[`tests/test_version_contract.py`](../../tests/test_version_contract.py) 断言 `importlib.metadata.version("traceharness-py")` 与被导入的 `__version__` 相等，并断言默认兼容范围确实包含当前版本。

## 2. 系统目标与边界

### 2.1 当前目标

TraceHarness 是可重建、可审计的 Coding Agent Runtime。它把模型决策、工具调用、现实副作用和外部验证拆成明确边界，并把关键事实持久化为事件。

核心不变量：

1. Session Stream 是运行语义的持久化事实源；
2. Effect Stream 是外部副作用事实的独立账本；
3. State、Surface 和 Model Request 均可从持久化事件重建；
4. 一个 Step 使用一份冻结的 Composition；
5. 每个 Tool Call 最终应有配对的 Tool Result；
6. 不确定的写入或进程副作用不能在恢复时盲目重放；
7. 模型声称完成不等于完成，配置 Verifier 时必须有外部证据。

### 2.2 当前不属于系统的能力

- 面向用户的插件**组合切换**：`traceh chat` 空闲提示符提供 `/plugins`、`/plugins reload`、`/plugins use ID...` 和 `/plugins use --none`。它只重新发现当前进程可见的 Entry Point 并重做 setup/conflict/health，不安装或卸载 Wheel，不强制 `importlib.reload()`，也不承诺从磁盘重新导入 Python 源码；
- **isolated（跨进程）插件**：Manifest 可以声明 `trust_mode="isolated"`，激活会**明确拒绝**它，而不是降级成 trusted；
- 插件提供 `EventStore`：D3 已开放 Provider、Policy、Middleware 与命名 Verifier，但 EventStore 仍固定在 Runtime/Session 生命周期，不能跟随 Step Generation 热替换；
- 插件在 workspace / preset / agent 层执行 setup，或自行声明分层 Tool/Prompt/Policy；D2 只开放宿主装配代码传入的 binding，D3 的新贡献仍属于 application setup，不把 `allowed_scopes` 变成新的激活入口；
- 活跃的 AgentSupervisor、子 Agent Tool 和 Workflow Engine；
- MCP 接入；
- Git Worktree/Overlay Workspace 分支与合并；
- Docker、远程沙箱或操作系统级安全隔离；
- 分布式 Event Store；
- 完整流式模型输出、重试、Fallback 与限流中间件；
- 类似 Codex/Claude Code 的富交互终端界面：`traceh chat` 已有实时 Tool Timeline 与 Activity Heartbeat（见 13.6、13.7），但仍是行式提示符，没有 token 流式输出、Spinner、颜色、可交互 TUI 和执行前审批。

## 3. 仓库目录与职责

```text
traceharness/
├── AGENTS.md                         跨 Coding Agent 的仓库开发规则
├── CLAUDE.md                         Claude Code 薄入口，导入 AGENTS.md
├── src/traceh/
│   ├── version.py                    版本、Distribution 名、核心 plugin id 与默认兼容范围的唯一来源
│   ├── api/                          公共协议、冻结 DTO 和扩展边界（含 `prompts.py`、`plugins.py`）
│   ├── concurrency.py                不可取消 Worker 的收敛等待
│   ├── cli/                          命令解析、.env 加载、交互式 chat 循环、Timeline 投影、Activity Heartbeat、Shell 命令渲染、插件 CLI 投影和终端编码
│   ├── evaluation/                   确定性 Benchmark Runner
│   ├── inspector/                    Session 文本、Replay 和静态 HTML 检查
│   ├── kernel/                       四层 Service 与 Composition Overlay、显式覆盖诊断、Activation、Hook、Lifespan、Owned Tasks
│   ├── llm/                          Provider 协议实现、注册表和调用边界
│   ├── plugins/                      Entry Point 发现、显式启用解析、事务式 PluginManager、Generation-owned ActivationSet Builder
│   ├── runtime/                      AgentRuntime 门面、PluginCompositionCoordinator 控制面、AgentLoop、Generation Composition/Lease、请求、Continuation、Verifier
│   ├── session/                      EventStore、进程内 Event Feed、跨进程文件锁、投影、恢复、压缩、不变量和插件身份事实重建
│   └── tools/                        Tool Registry、Schema、Policy、Middleware、子进程收敛与内置工具
├── tests/                            单元、契约、恢复、取消、跨进程、插件、打包和端到端测试
├── examples/                         无 Key 的确定性 Demo 夹具
│   └── plugins/                      可独立构建安装的示例插件 Distribution
├── benchmarks/                       独立复制 Workspace 的评估案例
├── docs/
│   ├── note/                         当前项目正式版与通俗版上下文
│   ├── adr/                          已接受设计决定及原因（含 0007 插件事务激活）
│   ├── plugins.md                    插件作者与运维契约
│   └── *.md                          专题设计、协议、恢复、测试和演进说明
├── .github/workflows/ci.yml          Ubuntu 3.12/3.13 与 Windows 3.12 编译、测试和 doctor
├── pyproject.toml                    包元数据、依赖、pytest 与 ruff 配置
├── README.md                         安装和使用入口
├── ROADMAP.md                        未来版本计划，不代表当前已实现
├── VALIDATION.md                     v0.3 发布时点验证快照
└── CHANGELOG.md                      已发布与 Unreleased 变化
```

`docs/TraceHarness Py：面向插件化与多 Agent 演进的 Python Harness 实施计划.md` 是长篇实施计划；它不是当前代码事实源。

## 4. 运行时装配与依赖方向

装配有两个入口，位于 [`runtime/agent_runtime.py`](../../src/traceh/runtime/agent_runtime.py)：

| 入口 | 用途 |
|---|---|
| `build_default_runtime()` | 同步、无插件；通过空的 Generation-owned `PluginActivationSet` 进入同一 Generation/Lease 主线，不发现、不 import 插件 |
| `build_default_runtime_async()` | 异步。`enabled_plugins` 为空时使用空 ActivationSet；非空时在私有候选注册表中完成激活事务，再由初始 Generation 接管 |

两者共享 `_prepare_default_runtime()` 与 `_finish_default_runtime()`。拆成两段的原因很具体：插件必须在**注册表已创建、Composition 尚未围绕它们冻结**的那一刻贡献内容，而拆分让这一点成立，同时不需要把装配代码写两遍。

它们创建或接受以下替换点：

- `EventStore`；
- `LlmProvider`；
- `PromptAssembler`；
- `Tool`、`ToolPolicy`、`ToolMiddleware`；
- `CompletionVerifier`；
- `ContinuationRuntime`；
- `ScopedServiceBinding`（Application / Workspace / Preset / Agent 的程序化 Service 装配；覆盖必须显式）；
- `ScopedToolBinding`、`ScopedPromptBinding`、`ScopedPolicyBinding`（同样四层的程序化模型/执行能力装配；覆盖必须显式）；
- `PluginDiscovery`、`enabled_plugins`、`plugin_configs`（仅异步入口）。

```mermaid
flowchart TD
    CLI["CLI / SDK / Evaluator"] --> AR["AgentRuntime"]
    AR --> AL["AgentLoop"]
    AL --> CR["GenerationCompositionRuntime / Lease"]
    CR --> SC["Application → Workspace → Preset → Agent Service Scope"]
    CR --> CO["四层 Tool / Prompt / Policy Overlay → 单一有效 Composition"]
    AL --> RB["RequestBuilder"]
    AL --> LR["LlmRuntime + Provider"]
    AL --> TR["ToolRuntime"]
    AL --> CV["CompletionVerifier"]
    AL --> SS["SessionService"]
    RB --> SP["SurfaceProjector"]
    TR --> SS
    SS --> PS["PublishingEventStore"]
    PS --> ES["EventStore"]
    ES --> JL["JSONL Session / Effect Streams"]
    PS -. "内层 append 按请求的 Durability 正常返回后发布（不等于 fsync）" .-> FEED["SessionEventFeed（进程内、只读订阅）"]
    FEED -. "只读观察" .-> UI["CLI Timeline"]
    REC["Recovery / Inspector / Invariants / Compaction"] --> SS
```

插件在这张图上的位置见下：

```mermaid
flowchart TD
    SEL["显式插件 ids"] --> PB["PluginGenerationBuilder"]
    PB --> CAND["私有候选 Tool / Prompt / Service 注册表"]
    CAND --> ACT["PluginActivationSet"]
    ACT --> GEN["CompositionGeneration"]
    GEN --> CR2["GenerationCompositionRuntime.publish"]
    CR2 --> AL2["AgentLoop：只调用 lease()"]
    ACT -. "旧 Lease 归零后逆序 dispose" .-> DRAIN["Composition Drain"]
    AL2 -. "snapshot plugins" .-> SNAP["composition/snapshot"]
    CHAT["traceh chat /plugins"] --> PC["PluginCompositionCoordinator"]
    AR2["AgentRuntime Turn admission"] --> PC
    PC --> MIG["候选替换 / Session migration Gate"]
    MIG --> AUTH["composition/migration-authorized（身份变化时）"]
    AUTH --> CR2
```

依赖规则：

- `AgentLoop` 只编排生命周期，不导入具体工具、JSONL 文件或厂商 HTTP 逻辑；也**不导入 CLI、Console、颜色或 Timeline 文案**：Timeline 是订阅 Feed 的界面层投影，主循环不知道它存在；
- **`AgentLoop` 同样不知道 `PluginManager` 存在**。插件位于装配层，它的 Tool、Prompt 和 Service 进入既有的 `ToolRegistry`、`PromptAssembler` 和 `ServiceRegistry`，因此**没有** `PluginToolRuntime`，也**没有** `PluginAgentLoop`。`agent_runtime.py` 里对 `PluginManager` 的 import 是函数内局部 import，正是为了让这条边界在依赖图上也成立；
- `AgentRuntime` 是对外门面和默认依赖装配点，并继续拥有活跃 Turn 表、Turn admission 的最终线性化检查和唯一总关闭 Task。D0 把候选 prepare/publish/rollback、Session durable identity 校验与迁移、共享 Gate、replacement/admission 在途任务收敛集中到 `PluginCompositionCoordinator`；协调器只通过窄回调读取 Runtime 是否关闭、是否有活跃 Turn 和 current Generation 的外部插件身份，不拥有第二份可变身份事实。Stage B 的内部 `replace_plugin_composition()` 与 Stage C 的 Chat `/plugins` 控制面仍复用同一条候选→Generation→publish 主线，身份变化仍在共享 Gate 内追加 append-only 授权事件；插件 Tool/Prompt/Service、Activation 与 Owned Task 由对应 Generation 的 ActivationSet 持有；SessionService、EventStore、核心 Provider、内置 Tool 和基础配置是 borrowed core，不能被插件 cleanup 关闭。`dispose()` 先收敛 Turn，再收敛控制面在途任务，再 Drain 所有 Generation，最后仅清理 application-level legacy 资源；
- Provider 与 Tool 通过公共协议进入 Runtime；
- Projector 和 Inspector 只消费事件，不反向修改历史事实；
- 未来多 Agent 控制面应构建在单 Agent Runtime 之上，而不是塞入 `AgentLoop`。

## 5. Session / Turn / Step 生命周期

### 5.1 概念

| 概念 | 当前语义 |
|---|---|
| Session | 与一个已解析 Workspace 绑定的长期事件历史，可包含多个 Turn |
| Inbox Message | 一次用户输入；先 accepted，再 claimed 到一个 Turn |
| Turn | 一次用户唤醒后的完整工作轮次 |
| Step | 一次冻结 Composition、构建请求、调用模型、可选执行工具的决策周期 |
| Model Attempt | 一次 Provider 调用；为未来重试/Fallback 保留独立边界 |
| Tool Invocation | 模型响应中的一个工具请求及其准入、Effect 和 Result |

### 5.2 正常执行

```mermaid
sequenceDiagram
    participant U as User
    participant AR as AgentRuntime
    participant AL as AgentLoop
    participant ES as EventStore
    participant M as Model Provider
    participant T as ToolRuntime
    participant V as Verifier

    U->>AR: run 或 resume instruction
    AR->>AL: run_turn
    AL->>ES: inbox/accepted, inbox/claimed, turn/start
    loop 每个 Step
        AL->>ES: step/start, composition/snapshot, request/snapshot
        AL->>ES: model/attempt-start
        AL->>M: ModelRequest
        M-->>AL: ModelResponse
        AL->>ES: assistant/chunk, assistant/message, model/attempt-end
        alt 有 Tool Calls
            AL->>T: execute_batch
            T->>ES: tool/* 与 effect/*
        else 无 Tool Calls
            AL->>V: verify workspace
            V-->>AL: verification result
            AL->>ES: verification/result
        end
        AL->>ES: step/end
    end
    AL->>ES: turn/end
```

### 5.3 并发与取消

- `AgentRuntime` 用内存锁和 `_active` 表保证同一 Session 同时只有一个活跃 Turn；这是单进程保证。事件写入层（6.5）已有跨进程锁，但“同一 Session 只跑一个 Turn”仍未跨进程强制：两个进程同时 run 同一 Session 时，事件文件不会损坏，结果是事件交错或 `SessionService.append_event()` 抛出 `ConcurrencyConflict`，而不是被 Runtime 提前拒绝。
- Turn admission 与 Stage C Session 迁移共用一把 Composition Gate：Turn 在 Gate 内完成 durable 身份校验并登记 active Turn；迁移在同一 Gate 内确认全局没有 active Turn、准备候选、执行授权 CAS 和 publish。迁移持 Gate 时，新的 Turn 只能等待，不能出现“检查时空闲、下一瞬间 Turn 已开始”的窗口。Gate 不是持久化事实；真正的 Session 身份仍来自事件。
- `cancel()` 先追加 `runtime/cancel-requested`，再取消 Task。`JsonlEventStore` 的取消语义见 6.6：被取消的 Store 操作不会留下仍在后台写入的线程。
- `AgentLoop` 在取消/异常时追加 Attempt、Step、Turn 的终止或错误事件；ToolRuntime 尽量补齐未完成调用的 Tool Result。
- `dispose()` 取消并等待当前 Runtime 持有的活跃 Turn，然后 Drain Composition，最后卸载插件；完整语义见 5.5。Shell Tool 在取消时先 terminate，超时后 kill 并等待进程退出。

### 5.5 `AgentRuntime.dispose()` 的收敛语义

整个关闭过程位于**一个内部 Task**（`_shutdown()`）里，而不是 `dispose()` 自己的协程帧里。这条放置方式修复的是一个真实缺陷：关闭逻辑内联时，调用方在活跃 Turn 仍在收敛期间被取消，会在**到达 `PluginManager.dispose()` 之前**就逃出去；而 `_disposed` 已经置位，于是此后每一次 `dispose()` 都立即返回——插件从此再也不会被卸载，而且没有任何地方报告这件事。

现在的规则：

| 情形 | 行为 |
|---|---|
| 首次调用 | 置 `_disposed = True`（立刻拒绝新 Turn），创建唯一的 `traceh-runtime-dispose` Task，通过 `shield` 等待它 |
| 等待期间被取消 | 用 [`await_worker_convergence()`](../../src/traceh/concurrency.py) 吸收取消并继续等待**同一个** Task；第二、三次取消同样不能提前放行；收敛完成后重新抛出**原始** `CancelledError` |
| 再次调用 | `await` **同一个**已完成 Task，因此复用同一个真实结果，关闭不会跑第二遍 |
| 关闭本身失败 | 该 Task 以异常完成，后续 `dispose()` 会再次抛出同一个异常，**不会**静默伪装成功 |

工作属于 Task 而不属于等待它的人，所以调用方的取消永远碰不到关闭本身。顺序是：先取消并 `gather` 全部活跃 Turn，再取消并等待在途插件候选/迁移及其 rollback，也等待已经进入 Gate 但还未登记的 Turn admission；随后让 Composition Runtime Drain 所有 retired/current Generation。Drain 会由各代 ActivationSet 逆序卸载插件 Activation、取消并等待 Owned Task、撤销 Service/Tool/Prompt 注册，最后才清理仍属 application-level 的 legacy 构建器/发现器资源（19.8）。默认 Stage B/C Runtime 不让 PluginManager 和 Generation 同时拥有同一份插件 cleanup。

### 5.4 一个 Session 中的多个 Turn

`run`/`resume` 各产生一个 Turn；`traceh chat`（见 13.4）在同一 Session 中按用户输入连续产生多个 Turn。无论谁驱动，Turn 的语义完全一致：每个 Turn 都通过 `AgentRuntime.run_existing()` 进入 `AgentLoop`，历史来自事件日志投影，调用方不持有第二份对话状态。

```mermaid
flowchart LR
    IN["用户输入一行"] --> RE["AgentRuntime.run_existing()"]
    RE --> TURN["AgentLoop：一个完整 Turn"]
    TURN --> EV["Session Stream 追加事件"]
    EV --> SUR["SurfaceProjector 重建模型可见历史"]
    SUR --> RE
    TURN --> OUT["打印最终文本与摘要"]
    OUT --> IN
```

## 6. 事件模型与持久化

### 6.1 Event Envelope

事件由 [`PendingEvent` 与 `EventEnvelope`](../../src/traceh/api/events.py) 表示。落盘后包含 Stream ID、单调 `seq`、Event ID、类型、时间、数据以及可选 correlation、causation、actor、composition revision 等元数据。

`EventEnvelope` 的不可变性有明确边界，不能被描述成“事件是递归不可变对象”：

- `@dataclass(frozen=True, slots=True)` **只**禁止重新赋值顶层字段，例如 `event.data = {...}` 会抛 `FrozenInstanceError`；
- `data` 仍然是普通的 `JsonValue` 图：其中的嵌套 `dict`、`list` 都是标准可变容器，`event.data["nested"]["value"] = ...`、`event.data["items"].append(...)` 在语言层面完全合法；
- 当前**不**引入 `FrozenDict`/`FrozenList` 或新的公共 JSON 类型系统。

因此“事件历史不会被改写”是一条**所有权契约**，而不是语言保证。这条契约由**具体边界**承担，不是自动生效的：

- Store 边界是 `EventStore.append()` 与 `read()`，它们返回的 Envelope 归调用方所有；
- Envelope 只是普通对象，框架**不会**自动隔离两个消费者：同一个 `EventEnvelope` 被交给两个消费者时，它们共享同一份可变 payload；
- 因此任何把一个事件分发给多个接收方的组件，都必须为每个接收方单独 detach。本版本确实存在这种扇出：`SessionEventFeed`（6.7）为**每个** Subscriber 单独 detach 一份。

[`detach_event()`](../../src/traceh/api/events.py) 是可复用的边界 helper：它基于既有的 `to_json_value()` 重建整个 JSON 图，按值携带其余全部元数据（`event_id`、`stream_id`、`seq`、`type`、`schema_version`、`occurred_at`、`causation_id`、`correlation_id`、`actor_id`、`composition_revision`），不经过 JSON 文本编码，因此 Envelope 上的 `UUID` 与 `datetime` 不会退化成字符串。模块内部的 `_detach_json_data()` 只是实现细节，不作为公共 API，也不从包级 `__init__` 导出。

复用 `to_json_value()` 而不是引入通用深复制，意味着“事件 payload 里允许放什么”和“它会被规范化成什么”只有一处定义。必须准确描述这条规则的范围，它**比 `JsonValue` 更宽**：

| payload 中的值 | `to_json_value()` 的处理 |
|---|---|
| JSON 原生 scalar（`None`、`bool`、`int`、`float`、`str`） | 原样透传，不做包装 |
| `Path`、`UUID` | 转成字符串 |
| `datetime` | 转成 ISO 字符串 |
| `Enum` | 递归转换其 `value` |
| dataclass | 转成 dict 后递归转换 |
| 任意 `Mapping` | 转成新的 `dict`，键转 `str` |
| 除 `str`、`bytes`、`bytearray` 之外的 `Sequence`（例如 `tuple`） | 转成新的 `list` |
| `set`、`bytes`、`bytearray`、任意普通对象 | 抛 `TypeError` |

也就是说，`Path` 与 `tuple` 并不是 `JsonValue`，但它们**被规范化而不是被拒绝**（`tuple` 变 `list`，`Path` 变字符串）；只有真正不受支持的值才抛 `TypeError`。不要把这条写成“超出 `JsonValue` 的值一律抛 `TypeError`”。

`to_dict()` 与 `from_dict()` 同样脱离：`to_dict()` 返回的 `data` 是调用方自己的图，修改它不会改写原 `EventEnvelope`；`from_dict()` 也不与传入 `raw` 的嵌套容器共享引用（旧实现只做顶层浅重建）。`from_dict()` 继续要求 `event.data` 是 JSON 对象，非对象直接报 `event.data must be an object`，不会用 `str()` 之类手段悄悄修正类型。

### 6.2 两类 Stream

| Stream | ID 形式 | 用途 |
|---|---|---|
| Session Stream | `session:<session_id>` | 生命周期、消息、模型、工具结果、验证和恢复事实 |
| Effect Stream | `effects:<session_id>` | 现实副作用的 Intent、Dispatch、Outcome 与 Reconciliation |

两个 Stream 通过 `session_id`、`tool_call_id`、`effect_id`、correlation/causation 等字段关联，但各自有独立序号。

### 6.3 当前事件类型

| 类别 | 事件 |
|---|---|
| Session/Inbox | `session/created`、`inbox/accepted`、`inbox/claimed` |
| Composition control | `composition/migration-authorized`（只记录外部插件身份迁移授权，不进入 Model Surface） |
| Turn/Step | `turn/start`、`turn/end`、`step/start`、`step/end` |
| 消息与请求 | `user/message`、`composition/snapshot`、`request/snapshot` |
| 模型 | `model/attempt-start`、`assistant/chunk`、`assistant/message`、`model/attempt-end` |
| 工具 | `tool/call`、`tool/admitted`、`tool/result` |
| 验证 | `verification/result` |
| Runtime | `runtime/cancel-requested`、`runtime/error`、`runtime/recovered` |
| Surface | `surface/replace` |
| Effect | `effect/intent`、`effect/dispatched`、`effect/outcome`、`effect/reconciled` |

### 6.4 EventStore 保证

`EventStore` 协议提供 append、read、head、list_streams。追加要求调用方传入 `expected_seq`；不匹配时抛出 `ConcurrencyConflict`。

#### Event 所有权契约

这条契约写在 [`EventStore` Protocol](../../src/traceh/session/event_store.py) 及其 `append()`/`read()` 的 docstring 上，而不是只写在某个具体实现里：Store 是可替换后端，替换后端不能改变调用方能对事件做什么。任何实现都必须满足以下可观察语义，`InMemoryEventStore` 与 `JsonlEventStore` 对调用方完全一致：

| 调用方持有的对象 | 修改它之后 |
|---|---|
| 原始 `PendingEvent.data` | Store 历史不变（`materialize()` 已在构造事件时脱离输入） |
| `append()` 返回的 Event | Store 历史不变 |
| `read()` 返回的 Event | Store 历史不变，下一次 `read()` 仍是原始事实 |
| 两次 `read()` 各自的结果 | 彼此独立，互不可见 |
| `to_dict()` 返回的字典 | 原 `EventEnvelope` 不变 |
| 传给 `from_dict()` 的原始字典 | 构造出的 `EventEnvelope` 不变 |

隔离覆盖顶层 `data`、嵌套 `dict`、嵌套 `list` 以及 `list` 内的 `dict`；多个 `PendingEvent` 即使复用同一个嵌套输入对象，落库后的事件之间也不共享可变容器。契约保护的是 **Store 历史不被反向污染**，并不声称调用方拿到的副本本身不可变：调用方完全可以修改自己那份副本，只是改不动账本。

两种实现达到同一契约的方式不同，这是设计差异而不是实现不一致：

- `JsonlEventStore` **不需要 Store 专属的 `detach_event()` 调用**，因为历史在文件里，读写两个方向都已经经过共享的 `EventEnvelope` 序列化边界，本轮无功能性改动。但不能因此写成“完全没有额外复制”：这个共享边界本身仍会重建 payload——`read()` 先 `json.loads()`，再由 `from_dict()` 规范化成全新的图；`append()` 则由 `to_dict()` 在序列化前重建 payload。复制是**通过序列化边界达成**的，不是被省掉了；
- `InMemoryEventStore` 必须显式脱离——它保存自己返回的对象，所以 `append()` 与 `read()` 都通过 `detach_event()` 交出副本，绝不把 `_streams` 中的对象暴露给调用方。

`head()` 不做任何复制，仍只返回序号。复制只发生在 Event API 边界（`materialize`、`to_dict`、`from_dict`、`detach_event`），单次复制的规模是**一个事件的 payload**；但一次 `read()` 返回多个事件时，总成本与它解析并返回的事件 payload 总量相关，不能说“一次 read 的成本只是一个 payload”。`JsonlEventStore.read()` 的 `from_seq` 是**过滤而不是定位**：先解析整条 Stream 再筛选，因此总成本对应整条 Stream——这是 JSONL 既有的全量扫描边界（见 16 节），不是脱离副本引入的新问题，本轮只如实记录，不做性能优化。

刻意不引入缓存：缓存意味着把同一份副本发给多个调用方，会重新制造共享引用。

默认 `JsonlEventStore`：

- 一个事件一行 JSON，文件名由 Stream ID URL 编码；
- 每个 Stream 有进程内 `asyncio.Lock`，作为同一事件循环内的快速路径；
- 每个 Stream 有 `.lock` 文件上的操作系统级排他锁，跨进程有效（见 6.5）；
- 写入可选 `SYNC` 或 `BATCHED` Durability；SYNC flush 后执行 `fsync`；
- `head()` 从文件尾读取最后完整行，成本与末行大小相关，不扫描完整文件；
- 尾部半行可自动截断到最后完整事件；
- 中间坏行、Stream ID 不符或序号不连续会报 `CorruptEventStream`。

### 6.5 跨进程 Stream 锁

[`session/file_lock.py`](../../src/traceh/session/file_lock.py) 提供 `exclusive_file_lock()` 上下文管理器，只使用标准库：

| 平台 | 原语 | 语义 |
|---|---|---|
| POSIX | `fcntl.flock(fd, LOCK_EX)` | 无 timeout 且无取消令牌时在内核阻塞；否则用 `LOCK_NB` 轮询 |
| Windows | `msvcrt.locking(fd, LK_NBLCK, 1)` | 锁定 `.lock` 文件偏移 0 起的 1 字节区间，轮询重试 |

关键语义：

- 锁定前显式 `lseek` 到偏移 0，因为 `msvcrt.locking` 从当前文件指针开始锁定；
- 允许锁定文件尾之后的区间，因此空 `.lock` 文件（每个 Stream 首次创建时的状态）也能锁定，不需要预写字节；
- 轮询从 1ms 退避到 25ms 上限；`JsonlEventStore(lock_timeout=...)` 为 `None` 时无限等待，为数值时超时抛 `FileLockTimeout`；
- 只有真正表示“区间被占用”的错误码（POSIX `EWOULDBLOCK`/`EACCES`，Windows `EACCES`/`EDEADLOCK`）才重试，其他 `OSError` 直接上抛；
- 正常、异常、`ConcurrencyConflict` 和取消路径都在 `finally` 中解锁并关闭描述符；解锁失败被吞掉，因为关闭描述符本身就会释放锁，不能掩盖临界区的原始异常；
- 进程崩溃或被杀死时，操作系统关闭描述符即释放锁，`.lock` 文件残留不会造成永久死锁。

`append()`、`read()`、`head()` 的完整临界区（读取 Stream Head、尾部半行检查与截断、`expected_seq` 校验、写入与 flush/fsync）都在同一把锁内完成，因此两个独立 Python 进程操作同一 Stream 时会排队，而不是同时读到相同 Head 后各写一条相同序号的事件。

`expected_seq` 仍然是必需的：锁只保证临界区互斥，调用方通常先 `head()` 再 `append()`，两次调用之间的 Head 可能已被另一个进程推进，此时第二个写入者会得到明确的 `ConcurrencyConflict`，而不是静默覆盖。

该锁是本机文件锁：跨网络文件系统（NFS、SMB）的行为取决于具体实现，不在当前保证范围内。

### 6.6 EventStore 的取消语义

阻塞的锁等待和文件 I/O 在 Worker 线程上执行，而线程无法被杀死。因此 `_run_locked()` 把取消实现为两段式协作，绝不允许出现“调用方已经收到 `CancelledError`，后台线程稍后拿到锁又继续写 Stream”的脱缰操作：

| 取消发生的时刻 | 行为 | 调用方看到 |
|---|---|---|
| 仍在等待 OS 锁 | 置位取消令牌，等待被 `Event.wait()` 立即唤醒，抛 `FileLockCancelled`，Stream 未被触碰 | `CancelledError`，无任何写入 |
| 已拿到锁但临界区尚未开始 | 进入临界区前重新检查取消令牌，直接放弃 | `CancelledError`，无任何写入 |
| 临界区已在执行 | 该段不可中断，按原子完成语义跑完并 flush/fsync | `CancelledError`，但事件已完整落盘 |

实现要点：

- Worker 通过 `asyncio.shield()` 提交，取消协程不会把它变成脱缰线程；
- 协程捕获 `CancelledError` 后先置位 `cancel`，再显式等待 Worker 收敛，然后才向调用方重新抛出；因此 `append()`/`read()`/`head()` 返回时文件已经不再变化；
- 收敛由共享的 [`await_worker_convergence()`](../../src/traceh/concurrency.py) 承担，它循环 `await asyncio.shield(future)` 直到 `future.done()`：**重复取消（第二次、第三次乃至更多次 `CancelledError`）被吸收后继续等待同一个 Worker**，不能被当作提前退出的出口；这里不使用 `suppress(BaseException)`。同一个函数也被 OpenAI-Compatible Provider 复用（见 8.3）；
- Worker 自身的返回值、`FileLockCancelled`、`ConcurrencyConflict` 或其他异常在收敛结束时被显式取回，不会遗留"future exception was never retrieved"告警；
- `signals.finished` 在锁释放之后置位，且必定早于 Future 完成，因此“调用方拿到 `CancelledError`”蕴含“锁已释放且 Worker 已收敛”；
- `_StreamLockSignals` 的 `waiting`/`cancel`/`finished` 三个 `threading.Event` 让“线程确实开始等锁”和“线程确实已收敛”可被观测，而不是靠时序猜测；
- 由于取消令牌的存在，POSIX 的无限等待从内核阻塞改为可中断轮询：等待仍然无限，但可被 asyncio 取消。

第三行是**提交点边界（may-have-committed）**：取消恰好落在写入过程中时，调用方收到 `CancelledError`，事件却已经提交。这里没有任何自动重试机制，因此不应称为 at-least-once。调用方不能假设“收到取消 = 没有写入”；正确做法是重新读取 Stream，并按 `event_id`、correlation 或业务身份判断该操作是否已经落盘，而不是只看 Head 数值——Head 也可能是别的写入者推进的。

### 6.7 进程内 Event Feed

[`session/event_feed.py`](../../src/traceh/session/event_feed.py) 提供**观察通道**，让界面在 Turn 运行期间就能看到事件，而不必等 Turn 结束再读文件。它比事件日志弱得多，这些边界必须写清楚：

| 维度 | Feed 的事实 |
|---|---|
| 新增持久化事实 | 否。不落盘、不参与恢复、不产生任何新事件类型 |
| 额外的崩溃持久性 | 否。事件在内层 `append()` **按调用方请求的 `Durability` 正常返回之后**才发布。Feed 不把 `BATCHED` 升级成 `SYNC`，也不增加任何自己的保证：一条已发布事件能否在操作系统崩溃后存活，完全由 EventStore 原契约决定 |
| 事实源 | 否。Runtime、`RecoveryService`、Inspector、不变量检查仍只读 `EventStore` |
| 历史 | 否。订阅**不重放**历史；需要历史仍走 `EventStore.read()` |
| 状态 | 否。Feed 不保存投影或缓存，不是第二份 State |
| 跨进程 | 否。另一个进程直接写同一份 JSONL 时，本 Feed 收不到 |
| 可丢失 | 是。append 返回之后、发布之前进程崩溃时，实时观察会漏；该事件的持久性仍然只由它请求的 `Durability` 决定，不会因此变差 |

#### 为什么边界选在 EventStore Decorator

`PublishingEventStore` 是包装任意 `EventStore` 的装饰器，而不是 `SessionService` 里的钩子。理由有两条，都可核查：

1. **后端无关**：包装 `InMemoryEventStore` 与 `JsonlEventStore` 的可观察语义完全相同，换 Store 不改变 Feed 语义；
2. **“Store 已接受”正好在这里成立**：`src/traceh/session/service.py` 中的 `store.append()` 是整个 `src` 树里唯一的 append 调用点，所有写入者（`AgentLoop`、`ToolRuntime`、`RecoveryService`、`CompactionService`、`AgentRuntime.cancel()`）都经过它。因此“发布 Store 从未接受的事件”在这个边界上无法表达，也不需要每个写入者自己记得通知。

`build_default_runtime()` 无条件包装 Store，并把 `SessionEventFeed` 暴露为 `AgentRuntime.events`。无订阅者时代价是每次 append 一把无竞争的锁。

#### 顺序与可见性契约

```mermaid
flowchart LR
    W["写入者：AgentLoop / ToolRuntime / Recovery"] --> SS["SessionService.append_*"]
    SS --> PS["PublishingEventStore.append（每 Stream 一把锁）"]
    PS --> ES["内层 EventStore：按请求的 Durability 追加"]
    ES -- "正常返回后，仍在锁内" --> FEED["SessionEventFeed（私有发布）"]
    ES -- "失败 / 冲突 / 取消：发布 0 条" --> NONE["不发布"]
    FEED --> SUB["每个 Subscriber 一份 detach_event() 副本"]
    SUB --> TL["cli/timeline.py 投影为一行文本"]
    TL --> OUT["traceh chat 实时打印"]
    ES --> TRUTH["事实源：Recovery / Inspector / 不变量只读这里"]
```

契约要点：

- **先被 Store 接受，后发布**：`_publish()` 只在内层 `append()` 正常返回后调用，因此 `ConcurrencyConflict`、序列化失败或任何 append 异常都发布 0 条；取消同样发布 0 条，**包括** may-have-committed 那条路径——Feed 允许漏，日志不允许丢；
- **是“接受”，不是额外持久性**：`durability` 原样透传，因此一次 `Durability.BATCHED` 追加会在 Store 从「已 flush 未 fsync」的写入返回时就被发布，这正是调用方所要求的。发布只表示“Store 已接受”，绝不表示“已 fsync”；`BATCHED` 通知是合法的，对应事件在操作系统崩溃时仍受 `BATCHED` 边界约束；
- **按 seq 顺序发布**：每个 Stream 一把 `asyncio.Lock`，同时覆盖“append + publish”。若只在 append 之后发布，两个并发写入者会自由竞争：Store 已序列化写入，但调用方各自恢复执行，seq 10 的写入者可能被调度器挂起、在 seq 11 之后才发布。把锁跨到发布之上，使“按 seq 顺序”成为结构性质，而不是依赖当前调度巧合。不同 Stream 用不同锁，互不阻塞；
- **一批多条**按 seq 顺序发布；
- **Stream 严格隔离**：Session Stream 的订阅者收不到别的 Session，也收不到 Effect Stream；
- **每 Subscriber 一份**：见下节；
- **发布不 await**：`publish()` 只往无界队列里 `put_nowait`，因此锁很快释放，任何订阅者都无法延长它。

#### 消费者只拿到只读接口

观察者拿到的是 `EventFeed` Protocol，只暴露 `subscribe()` 与 `subscriber_count()`。发布位于 `SessionEventFeed` 的私有 `_publish()`，唯一调用者是同模块的 `PublishingEventStore`。

这条区分是承重的：如果消费者接口上有公共 `publish`，任何拿到 Feed 的代码都能注入一条 Store 从未接受的 Envelope，而 Subscriber 无法把它与真事件区分开——Timeline 会忠实地显示一个从未发生的 Step。把发布移出消费者接口，使“只有 Store 接受的事件才会被发布”成为 **API 形状的性质**，而不是依赖观察者自觉遵守的约定。下划线不是安全沙箱，但它明确了权限边界。

同理，`AgentRuntime.events` 是**必填**构造参数，而且必须是该 Runtime 的 `PublishingEventStore` 实际发布的那一个 Feed 对象。给它一个默认值会交给调用方一个「可以订阅但永远沉默」的对象——接口存在而能力不存在。自定义装配必须显式把两者配对，正如 `build_default_runtime()` 所做的。

#### Event 所有权从 Store 边界扩展到每个 Subscriber

6.4 的所有权契约只覆盖 `append()`/`read()` 交回调用方的对象。Feed 引入了**扇出**，这正是 6.1 预留的那个缺口：把同一个 `EventEnvelope` 交给两个消费者，它们会共享同一份可变 payload。因此 `publish()` 为**每个** Subscriber 单独调用一次 `detach_event()`，而不是每次发布只复制一次。可观察结果：Subscriber A 修改嵌套 payload，不影响 Store 历史、不影响 Subscriber B 已收到的事件、也不影响此后任何 Subscriber 收到的事件。

#### 无界队列的明确取舍

每个 `EventSubscription` 有一个无界 `asyncio.Queue`：

- **不对 Runtime 施加背压**：慢订阅者永远不能拖慢或弄失败一次 Store append；
- **被遗弃或长期不消费的订阅者会占内存**，上限只有该 Session 产生的事件量；
- **Chat 生命周期在每条退出路径上都关闭订阅**（正常、异常、取消、EOF、`/exit`），因此随包发布的消费者不泄漏；
- **未来若改为有界队列，必须定义明确的 overflow 语义**：静默丢事件会让 Timeline 对已经发生的事情说谎。

`EventSubscription.close()` 幂等：它把结束标记排在已发布事件之后，因此“先 close 再 drain”会打完所有已排队事件再结束——这正是 Chat 能承诺“Timeline 出现在最终回答之前”的原因，不依赖轮询或 sleep。对已耗尽的订阅再迭代返回空，不会死等一个已被消费掉的结束标记。

## 7. Composition、Surface 与可重建请求

### 7.1 Composition

每个 Step 通过 `CompositionRuntime.lease()` 获取 `ActiveComposition`。快照包含：

- provider、model；
- 完整 system prompt；
- tool schemas；
- plugin identities；
- policy 和 middleware 名称；
- temperature、max output tokens；
- 基于内容生成的 revision。

`plugin identities` 从 v0.4 起是**真实数据**，不再是占位：它等于 `traceh.core`（版本来自 1.2 的唯一来源）加上本次激活的每个外部插件的真实 `plugin_id` 与 `version`。无插件运行时该列表只有一项。

这条身份必须能被重建，否则 Replay 会正确地报告不一致。因此 [`composition_from_event()`](../../src/traceh/runtime/request_builder.py) 也从事件里重建 `plugins`；此前它固定返回 `()`，于是每个被重建的 Composition 都在声称自己来自一个无插件 Runtime。

当前实现是 Generation-backed `CompositionRuntime`：每个 Runtime 始终只有一个 current Generation；Runtime 初始化时固定主 `ToolRuntime.sessions` 对象，Step 进入 `lease()` 时在 `asyncio.Lock` 保护下原子绑定它，并从同一代取得 Provider、ToolRuntime、Prompt、Plugin Identity、Policy/Middleware 和 Snapshot。`publish()` 在同一把锁内把旧代标为 retired、安装新代；旧 Lease 继续持有旧记录，新 Lease 只能拿新代。候选若引用另一个 `SessionService` 会在这个线性化点前被拒绝，避免 `tool/call` 或 `tool/result` 写入另一份 EventStore。每个 Generation 对象只能被一个 Runtime 认领一次；已 retired/cleaned 的 Generation 不能重新发布。资源 cleanup 由独立的一次性 `CompositionResourceOwner` handle 负责：装配层把同一个 handle 显式绑定到 `LlmRegistry`、`ToolRuntime`、`PromptAssembler` 及 Provider/Tool/Policy/Middleware 组件，冻结 Generation、扁平适配器和兼容性投影只传播这个 handle/binding，不扫描对象图，也不使用全局 `id()` 身份目录。binding 直接写入实例字典或声明过的 slot，并验证写入后的真实状态，因此自定义 `__setattr__` 不能静默吞掉所有权标记；提交若在中途失败，会逐项恢复每个组件原来“字段不存在”或“字段存在且为 `None`”的精确状态，Owner 也保持可重试。无法承载这种可验证 binding 的裸 slotted Provider、Tool、Policy 或 Middleware 不能进入 cleanup-bearing Generation；它们必须先经过可绑定的受控装配，或者只能留在没有 Generation cleanup ownership 的 application-level 路径。这样同一个 slotted 原始能力被放进两个新容器时不会被两个 Owner/Runtime 同时接受。多层 `replace()`、把冻结能力重新放进新注册表、从冻结能力构造 Runtime、以及同一 raw capability 再构造 cleanup Generation，都会在 Runtime 初始化或 publish 的共同认领入口被拒绝；没有资源级引用计数时，cleanup-bearing Generation 必须拥有尚未认领的显式 owner，并且不能和旧 Lease 或旧代共享会被 cleanup 关闭的资源。

Generation identity 是内部生命周期编号，只用于引用计数、retire、cleanup 和诊断，不写入事件或 Request Fingerprint。`CompositionSnapshot.revision` 仍是模型可见内容的 fingerprint；因此两个模型可见内容完全相同的 Generation 可以有不同 identity，但 revision 相同。Generation 构造时还会冻结 Tool 的 name、description、input_schema、effect_kind，并用真正只读、扁平、幂等的适配器把执行委托给原 Tool；嵌套 Schema 也不能改写，连续从冻结 Generation 构造无 cleanup 候选不会递归套适配器。Policy/Middleware 名称在构造时捕获，兼容性检查投影与当前 Generation 分离。Runtime 初始化会先从已经冻结的初始 Generation 构造全部兼容性视图，再一次性认领资源；认领后不再调用调用方可变的 Prompt/Registry 来源，因此第二次读取失败不会留下已认领但无 Runtime 接管的资源。Generation 被 retired 后，仍有 Lease 时绝不 cleanup；最后一个 Lease 释放才创建一次 cleanup Task。`drain()` 等待所有 retired Generation 的 Lease 归零并等待 cleanup 真正完成，cleanup 失败会在所有其他 Generation 都尝试后以有界的结构化 `CompositionDrainError.failures` 报告，并把 Runtime 标为 poisoned、拒绝后续 `publish()`。等待期间重复取消由共享内部 Task 和 `await_worker_convergence()` 吸收，收敛后再抛最初的 `CancelledError`。

Stage B 的插件 cleanup 不再使用上述 capability-wide owner 推断插件所有权：`PluginActivationSet` 显式持有插件 Activation、插件 Tool/Prompt/Service、Owned Task 与 cleanup，SessionService、EventStore、核心 Provider 和内置 Tool 只是 borrowed core。候选只在私有注册表中 setup，成功 publish 后由 Generation 接管；旧 Lease 结束前旧 set 不会被卸载。

Stage A 已进入同步/异步默认 Runtime 主线，Stage B 又把 Generation-owned `PluginActivationSet` 接入启动插件和内部候选替换路径；Stage C 让 `traceh chat` 的 `/plugins` 控制面调用同一套 Builder→ActivationSet→Generation→publish→Drain；D1/D2 把四层 Service 与程序化 Tool/Prompt/Policy 装配压成有效 Composition；D3 再把插件 Provider、Policy、Middleware、Verifier 接入同一候选和 Step Lease。`AgentLoop` 仍不导入 PluginManager、Builder、Scope resolver 或 reload service；它只从 Lease 取得本 Step 的 Provider、ToolRuntime 与 Verifier。用户命令只重新构造当前进程可发现的已安装 Entry Point，不安装/卸载 Wheel、不强制 module reload；因此 v0.4.0 仍不是 v0.5 完成版，插件自行选择子层与 EventStore 插件化仍未开放。

### 7.2 Surface

`SurfaceProjector` 只把以下事件投影为模型消息：

- `user/message` → user；
- `assistant/message` → assistant，保留 tool calls；
- `tool/result` → tool；
- `surface/replace` → 替换指定旧 Surface 事件的摘要消息。

原始事件仍保留。多次 Replacement 通过 source seq 遮蔽旧视图，而不是删除历史。

### 7.3 Request Snapshot 与 Fingerprint

`RequestBuilder` 使用“截至 Composition Event 的 Surface + 同一 Lease 提供的 Composition Snapshot”生成 `ModelRequest`，持久化完整 Request、`source_seq`、composition revision 和稳定 fingerprint。内部 Generation identity 不进入 Request Fingerprint；因此请求重建、Replay 和 `verify_request_snapshots()` 继续依据持久化的内容 revision 工作。

```mermaid
flowchart LR
    EV["Session Events through source_seq"] --> SU["SurfaceProjector"]
    SU --> MSG["Model-visible Messages"]
    CO["Composition Snapshot"] --> REQ["ModelRequest"]
    MSG --> REQ
    REQ --> FP["Canonical JSON Fingerprint"]
    FP --> SNAP["request/snapshot"]
    SNAP --> REBUILD["Replay 时独立重建并比较"]
```

`verify_request_snapshots()` 能重新定位对应 Composition、重建当时的 Turn/Step metadata 和 Request，并报告 fingerprint 不一致。

## 8. 模型层

### 8.1 公共边界

`LlmProvider.complete(ModelRequest) -> ModelResponse` 是 Provider 协议；`LlmRegistry` 按名称注册；`LlmRuntime.invoke()` 是主循环与 Provider 之间的调用边界。

当前 `LlmRuntime` 等 Provider 完成后，只把完整文本作为一个 delta 交给 `assistant/chunk` 回调。协议上有 Chunk 事件，但当前网络适配并非真正 token streaming。

### 8.2 Scripted Provider

- 从内存响应序列或 JSON 文件读取确定性响应；
- 保存收到的 Request，便于测试；
- 默认脚本用完后抛 `ScriptExhaustedError`；
- 用于 Demo、单元测试、端到端测试和 Benchmark，不需要 API Key。

### 8.3 OpenAI-Compatible Provider

- 使用标准库 `urllib.request`；
- 发送 `POST <base_url>/chat/completions`；
- 请求为 `stream: false`，支持 messages、function tools、temperature、max tokens；
- API Key 来自显式参数或命名环境变量，以 Bearer Header 发送；
- 解析第一个 choice、文本、tool calls、finish reason 和 token usage；
- HTTP/URL/响应结构错误转换为 `ProviderHttpError`。

当前没有流式读取、自动重试、Fallback、并发限流或厂商专属协议适配。
取消语义：`urllib` 请求一旦发出就无法中止，因此 `complete()` 用 `asyncio.shield` 保护 Worker，取消时先用共享的 `await_worker_convergence()` 等它收敛再重新抛出原 `CancelledError`，重复取消不会提前返回。这样不会出现"Chat 已经宣布 interrupted、后台 HTTP Worker 仍在运行"的情况。代价必须说清楚：这是收敛而不是立即中止网络请求，最坏情况下要等到 `timeout_seconds`（默认 120 秒）到期。

## 9. Tool Runtime 与内置工具

### 9.1 执行管线

```mermaid
flowchart LR
    CALL["Tool Call"] --> LOOKUP["Registry Lookup"]
    LOOKUP --> SCHEMA["Schema Validation"]
    SCHEMA --> POLICY["Monotonic Policies"]
    POLICY --> ADMIT["tool/admitted"]
    ADMIT --> INTENT["effect/intent"]
    INTENT --> DISPATCH["effect/dispatched"]
    DISPATCH --> SCHEDULE["Read Parallelism / Write Barrier"]
    SCHEDULE --> MW["Middleware Chain"]
    MW --> EXEC["Tool.execute"]
    EXEC --> OUTCOME["effect/outcome"]
    OUTCOME --> RESULT["tool/result"]
```

执行事实：

- 同一模型响应中的 Tool Call ID 必须非空且唯一；
- 所有 `tool/call` 先写入 Session Stream；
- 未知工具或参数错误直接产生结构化失败 Result，不会产生 Effect；
- Policy 中任何 DENY 都不可被后续 ALLOW 覆盖；至少一个 Policy ALLOW 才准入；
- 连续的 PURE_READ/WORKSPACE_READ 调用使用 `asyncio.gather`；写入、进程和其他副作用形成顺序 Barrier；
- Middleware 的 `call_next()` 每层最多调用一次；
- Tool Runtime 有整体超时和最大输出字符限制；
- Outcome 记录证据、结构化数据、截断状态或错误；
- 取消时尽量依据已有 Outcome 补齐 Result，否则记录 `aborted_before_dispatch`。

### 9.2 Effect Kind

`PURE_READ`、`WORKSPACE_READ` 被认为并发安全且可安全重试；`WORKSPACE_WRITE`、`PROCESS`、`NETWORK_WRITE`、`EXTERNAL_TRANSACTION` 默认不并发且不可盲目重试。

### 9.3 内置工具

| 工具 | Effect Kind | 输入与行为 | 关键限制 |
|---|---|---|---|
| `list_files` | WORKSPACE_READ | 列出相对文件；可设 `max_files` | 跳过常见缓存、Git、虚拟环境和依赖目录 |
| `read_file` | WORKSPACE_READ | 读取 UTF-8 文件 | 真实路径必须位于 Workspace 内 |
| `search_text` | WORKSPACE_READ | 子串或正则搜索，可限路径和结果数 | 跳过二进制/非 UTF-8 及常见忽略目录 |
| `apply_patch` | WORKSPACE_WRITE | 精确旧文本替换或显式创建新文件 | 校验替换次数；临时文件 + fsync + 原子 replace；不是 unified diff parser |
| `shell` | PROCESS | `shlex.split` 后用 `create_subprocess_exec` 执行 | 不使用 `shell=True`；清洗秘密环境变量；超时/取消收敛子进程 |

所有路径读写通过 `resolve_workspace_path()` 解析后检查 Workspace 边界。默认 `DangerousShellPolicy` 屏蔽一组明显危险的可执行文件名，但它只是 Guardrail，不是安全沙箱。

### 9.4 插件 Tool 走同一条管线

被启用插件注册的 Tool 进入的是**同一个** `ToolRegistry`，因此 9.1 的整条管线对它逐字适用：Registry 查找、Schema 校验、单调 Policy、`tool/admitted`、`effect/intent`、按 Effect Kind 的并发或 Barrier 调度、Middleware、执行、`effect/outcome`、`tool/result`。没有 `PluginToolRuntime`，插件 Tool 在事件日志里与内置 Tool 无法区分——这正是目的。

插件 Tool 名与内置 Tool 冲突时，激活在发布**之前**被拒绝（见 19.4），因此不存在“插件悄悄顶替 `read_file`”这种情况。

## 10. Continuation 与证据驱动完成

`DefaultContinuationRuntime` 按以下顺序决定继续或结束：

1. 达到 `max_steps` → `max_steps_exceeded`；
2. 模型响应包含 Tool Calls → 下一 Step；
3. 已运行 Verifier 且失败，失败次数仍在重试预算内 → 把结构化验证摘要作为新 user message 注入下一 Step；
4. Verifier 持续失败超预算 → `verification_failed`；
5. 无 Tool Calls 且无失败证据 → `completed`。

`CommandVerifier` 使用参数拆分和 `create_subprocess_exec` 在 Workspace 中运行独立命令，使用清洗后的环境，收集退出码、stdout、stderr；退出码 0 为通过。AgentLoop 把结果写为 `verification/result`。

Verifier 是可选的：未配置时，无 Tool Call 的最终模型响应可以结束 Turn，此时 `verification_passed` 为 `None`，不能把它解释成“外部验证已通过”。

### 10.1 Verifier 的子进程与输出所有权

`CommandVerifier` 在 Workspace 中运行真实命令。取消或超时时必须把这个子进程带走，否则它会在调用方已经认为该 Turn 结束之后继续改动 Workspace；同时，在**会返回结果的路径**上，它已经产生的输出是证据，不能在收尾过程中丢掉。这两件事由同一个所有权模型解决。

**输出只有一个归属**：子进程的 stdout/stderr 不接管道，而是直接写进本进程持有的临时文件（[`capture_output()`](../../src/traceh/tools/process_control.py)）。因此：

- 三条路径共用**同一套捕获机制**，因此不存在“哪条路径拿到的是另一份输出”的问题；子进程 flush 过的内容在超时被察觉之前就已经被捕获、对本进程可见；
- 读取是普通文件读，不会阻塞在孙进程仍持有的管道上，也可以重复读而不丢字节；
- 不存在"取消第一次 `communicate()` 再调第二次"的动作，也就不存在第一次已读入缓冲区的输出被丢弃的问题。

**子进程收敛**：超时或取消时先调用 `converge_process()` —— terminate → 有界等待（默认 2 秒）→ kill → 等待退出，返回前直接子进程必定已退出。等待吸收取消并记录“曾经被取消过”，因此收尾过程中到达的取消不会提前放行，而是压到收敛之后再重新抛出 `CancelledError`。

**三条路径各自做什么**（这一点必须精确，不能笼统地说“输出总会被保留”）：

| 路径 | 直接子进程 | 输出 |
|---|---|---|
| 正常完成 | `await process.wait()` 自然返回 | 读取捕获文件，装入 `VerificationResult`；AgentLoop 据此追加 `verification/result` |
| 超时 | 先 `converge_process()` 收敛，再读 | 读取捕获文件；完整文本进入 `VerificationResult.stdout`/`stderr`，尾部进入 summary，并经 `DefaultContinuationRuntime` 注入下一 Step |
| 取消 | 只做 `converge_process()` 收敛，保证子进程不逃逸 | **不读取、不返回 `VerificationResult`、不追加 `verification/result`**；随后临时文件关闭，捕获内容随之丢弃 |

换句话说：取消路径的承诺只有“子进程一定不会逃逸”，没有任何输出证据的承诺。只有调用方追加到 Session/Effect Event Log 的事件才是持久化事实（Verifier 路径是 `verification/result`，Tool 路径是 `effect/outcome` 与 `tool/result`，按路径适用）；临时文件里的字节只是本次调用期间的捕获。

**ShellTool 与两类超时的边界**：`ShellTool` 使用同一套捕获与收敛机制，但它归 `ToolRuntime` 调度，因此存在两个不同的超时：

| 超时来源 | 触发方式 | 上报内容 |
|---|---|---|
| Tool 自身（`shell` 的 `timeout` 参数） | `ShellTool` 收敛子进程后主动 `raise TimeoutError(content)`，content 含实际命令、`exit_code`、`timed_out=true` 与已捕获的 stdout/stderr | `ToolRuntime` 在嵌套边界上把它重贴为 `ToolReportedTimeout`，据此写 `effect/outcome`（`reported_by=tool`）与 `tool/result`，**保留工具自己的文本与时长**，并按 `max_output_chars` 截断 |
| Runtime 预算（`ToolRuntime.timeout_seconds`） | `asyncio.timeout()` 到期 | 保持通用语义：`Tool timed out after <预算>s`，`effect/outcome` 标 `reported_by=runtime`；子进程仍由 `ShellTool` 的取消路径收敛 |

两者靠**嵌套异常边界**区分，不靠错误文本匹配：工具自己抛出的 `TimeoutError` 在内层被立即重贴为领域异常 `ToolReportedTimeout`，因此外层负责 Runtime 预算的 `except TimeoutError` 不可能再吞掉它。修复前两者共用一个 `except TimeoutError`，结果是 Shell 的 stdout/stderr 从 `effect/outcome` 与 `tool/result` 中消失，且内部超时被误报成 Runtime 预算时长。

**本地资源不需要私有 API 收尾**：改用临时文件后不存在 stdout/stderr 管道子传输，`await process.wait()` 返回时 subprocess transport 已自行关闭。因此不访问 `process._transport`，也不需要任何手动关闭步骤。实测（Windows，孙进程仍持有继承句柄）：返回时 `transport_closed=True`、`open_pipe_transports=0`，独立解释器跑完后事件循环关闭时 stderr 没有 `unclosed transport` 或 `Event loop is closed`。

**明确不管理孙进程**：子进程派生的孙进程会继承捕获文件句柄，可能在直接子进程退出后继续运行、继续往文件里写。本模块保证的是**直接子进程**已退出。至于输出会不会被保留，取决于调用方走的是哪条路径，只有这两条会读取捕获：**Verifier 正常完成**、以及**组件自己拥有的超时**（`CommandVerifier` 自身超时、`ShellTool` 自身超时）。**直接取消**与 **`ToolRuntime` 预算先到期**都不读取捕获——后者会取消工具，工具走取消路径，最终只产生 Runtime 通用超时结果。对孙进程不作任何承诺；由于输出走文件而不是管道，孙进程的存在不会拖住收尾。


`sanitized_environment()` 的两项平台修正：

- 保留 `SYSTEMROOT`、`WINDIR`、`COMSPEC`、`PATHEXT` 等 Windows 必需变量。缺少它们时子进程在 `import asyncio` 阶段就以 WinError 10106 失败，`python -m pytest` 这类最普通的 Verifier 命令在 Windows 上根本无法启动；
- 设置 `PYTHONUTF8=1` 与 `PYTHONIOENCODING=utf-8`。父进程按 UTF-8 解码捕获到的字节，而 Windows 上的 Python 子进程默认会用系统代码页（中文环境为 CP936）输出，中文会整段变成 U+FFFD。这条只对 **Python 子进程**成立；非 Python 的原生工具仍然遵循系统代码页，其输出可能依旧是乱码。

这些变量描述机器而不是用户，且仍然经过 KEY/TOKEN/SECRET 等敏感名过滤。
## 11. 崩溃恢复与生命周期收敛

`RecoveryService.recover()` 按固定顺序追加事件，使修复后的流与健康流读起来顺序一致：

1. 读取 Session 与 Effect Streams；
2. 找出没有 `model/attempt-end` 的 `model/attempt-start`，按证据补写 Attempt End（见 11.1）；
3. 找出没有 `tool/result` 的 `tool/call`；
4. 有持久化 Outcome/Reconciled 时据此合成 Result；
5. 没有可确认 Outcome 时标记 `unknown_after_crash`，有 Intent 则追加 `effect/reconciled`；
6. 对未闭合 Step/Turn 追加 `reason=interrupted` 的结束事件；
7. 发生改变时追加 `runtime/recovered`。

```mermaid
flowchart TD
    READ["读取两个 Stream"] --> ATT{"Attempt 未闭合？"}
    ATT -- "有匹配的 assistant/message" --> ASUC["attempt-end: succeeded"]
    ATT -- "无消息或仅有 chunk" --> AUNK["attempt-end: unknown_after_crash"]
    ATT -- "没有" --> CALL{"孤立 Tool Call？"}
    ASUC --> CALL
    AUNK --> CALL
    CALL -- "有 Outcome" --> SYN["合成 tool/result"]
    CALL -- "结果不明" --> UNK["unknown_after_crash，不重放"]
    CALL -- "没有" --> LIFE{"Step / Turn 未闭合？"}
    SYN --> LIFE
    UNK --> LIFE
    LIFE -- "是" --> CLOSE["追加 interrupted end events"]
    LIFE -- "否" --> ANY{"本次是否修复过任何东西？"}
    CLOSE --> ANY
    ANY -- "是" --> REC["追加 runtime/recovered"]
    ANY -- "否" --> DONE["无变更，不追加任何事件"]
```

只要 Attempt、Tool Result 或生命周期中有任意一项被修复，就追加一条 `runtime/recovered`；三项都没有修复时不追加任何事件，`changed` 为 `false`。

### 11.1 Model Attempt 收敛

Attempt 已开始不代表模型答复过，因此状态由持久化证据决定：

| 持久化证据 | 恢复状态 | 关键字段 |
|---|---|---|
| 至少存在一条合格 `assistant/message` | `succeeded` | `recovered=true`、`recovered_from=assistant/message` |
| 没有合格消息，或只有 `assistant/chunk` | `unknown_after_crash` | `recovered=true`、`error_type=RecoveredAfterCrash`、`recovered_from=none`、`partial_chunks` |

一条事件成为某次 Attempt 的合格证据，必须同时满足三条（`partial_chunks` 使用同一套筛选）：

1. `attempt_id` 与 Start 相同；
2. `turn_id`、`step_id` 与 Start 相同，按值比较，`1` 不会匹配 `"1"`；
3. `seq` 晚于 Start —— Start 之前写下的事件描述的不是这次调用。

只要存在任意一条合格消息即判为 `succeeded`，因此"先有错作用域消息、后有正确消息"能被正确识别。

`attempt_id` 只有在是非空、非纯空白字符串时才算有效身份。`None`、数字、布尔值、`""` 和纯空白一律视为缺失：Start 被跳过、在 `notes` 中说明、不写 Attempt End，绝不通过 `str()` 造出名为 `"None"` 的 Attempt。

两种情况共同的硬约束：

- 绝不重新调用 Provider；
- 绝不伪造 `usage` 与 `finish_reason`（从未观测到）；
- 绝不把 Chunk 拼成 `assistant/message`，Chunk 只作为审计证据保留；
- 已有 `assistant/message` 只作证据，不重复追加；
- 已闭合的 Attempt 不被修改，因此重复 recover 幂等；
- 多个未闭合 Attempt 按 Start 事件原始顺序确定性收敛；
- 恢复生成的 End 继承 Start 的 `correlation_id` 与 `composition_revision`，`causation_id` 指向 Start 的 `event_id`；
- `attempt_id` 缺失的 Start 无法关联，跳过并在报告中说明，由不变量报告问题而不是伪造身份。

`RecoveryReport` 与 `runtime/recovered` 都包含 `closed_model_attempts` 计数。

旧版本写下的 Session 可能已经关闭 Step/Turn 却遗留未闭合 Attempt。Append-only 不允许插入历史位置，因此恢复器在既有 `step/end`/`turn/end` 之后追加 Attempt End，`CoreInvariantChecker` 也据此在整条流范围内判断配对，使这类历史 Session 重新变为不变量干净。

这条豁免有明确门槛：晚于所属 Step 关闭才出现的 Attempt End，只有同时带 `recovered=true` 且 `causation_id` 等于对应 Start 的 `event_id` 时才被接受；普通的迟到 End、或指向别的事件的 `recovered=true` End，仍然违反 `attempt-end-inside-step`。

`resume` 总是先 recover，再在同一 Session 追加一个新 Turn，并提醒模型重新检查 Workspace 和恢复结果后再重复副作用。

## 12. 投影、压缩、Inspector、Replay 与 Evaluation

### 12.1 State 与不变量

- `StateProjector` 推导 Session 状态、当前开放 Turn/Step、完成数量和最后序号；
- `CoreInvariantChecker` 检查序号、生命周期嵌套、Model Attempt 身份/配对/真实作用域、Tool Call/Result、Effect 与 Composition 等协议关系；检查按事件流中真正开放的 Turn/Step 判断，不采信 payload 自报的作用域；
- 投影和检查不修改事件。

### 12.2 手动 Surface 压缩

`CompactionService.replace_through()` 要求非空摘要和合法边界，收集边界内模型可见事件序号，追加 `surface/replace`。当前没有自动摘要器；CLI 调用者负责提供摘要文本。

### 12.3 Inspector 与 Replay

- `inspect` 输出 Workspace、状态、事件数、Turn/Step 数、不变量和请求重建违规，可选事件明细；
- `inspect --html` 生成包含 Session/Effect 事件和摘要的静态 HTML；
- `replay` 输出 Surface，即模型可见消息，并执行 Request Reconstruction 检查；
- `sessions` 列出持久化 Session；
- `recover` 只恢复，不调用模型。

### 12.4 Benchmark

`BenchmarkRunner` 发现 `*/case.json`，为每个案例复制独立 Workspace，使用 Scripted Provider 和真实 Tool Runtime/Verifier，生成：

- 案例 Workspace；
- `.traceh` Session/Effect JSONL；
- `report.json`；
- `report.md`。

成功条件是最终外部验证通过且不变量无违规。当前只有 `fix_addition` 一个确定性案例，不能代表复杂真实 Coding 任务质量。

## 13. CLI、配置与日常运行

### 13.1 命令

| 命令 | 当前用途 |
|---|---|
| `traceh run` | 创建 Session 并运行一个 Turn |
| `traceh chat` | 在一个 Session 中连续多轮交互（见 13.4） |
| `traceh resume` | 恢复 Session 后追加一个新 Turn |
| `traceh recover` | 只执行恢复，不调用模型 |
| `traceh inspect` | 状态、不变量、请求重建和事件检查，可导出 HTML |
| `traceh replay` | 重放模型 Surface 并检查请求重建 |
| `traceh compact` | 手动追加 Surface Replacement |
| `traceh sessions` | 列出 Session |
| `traceh eval` | 运行 Benchmark 目录 |
| `traceh plugins list` | 列出已安装插件的元数据，**不 import 任何插件** |
| `traceh plugins inspect <id>` | 同上，针对单个插件；未知或有问题时退出码 6 |
| `traceh plugins doctor [ids...]` | import、setup、health check 后**立即 dispose**；失败时退出码 7 |
| `traceh doctor` | 检查 Python、数据目录和非秘密 Provider 配置状态 |

`run`、`chat`、`resume` 接受 `--plugin`（可重复）。`recover`、`inspect`、`replay`、`compact`、`sessions` 使用同步的 `build_default_runtime()`、不启用插件，因此也**不接受** `--plugin`——提供该参数会是误导。`plugins` 子命令本身同理。

除 `chat` 外的命令都是 run-to-completion：接收一次任务，执行到 Turn 结束，打印最终文本和摘要。`chat` 增加了同一 Session 内的连续输入循环，以及 Turn 运行期间的实时 Step/Tool Timeline（13.6）；但它仍是行式提示符：没有 token 流式输出、执行前审批，也不能在 Turn 运行期间继续输入。`run`/`resume` 本轮**没有**接 Timeline。

### 13.2 `.env` 与配置优先级

默认读取当前工作目录 `.env`，也可用 `--env-file` 指定。优先级：

```text
显式 CLI 参数 > 已存在的进程环境变量 > .env 文件 > 内置默认值
```

支持：

- `TRACEH_PROVIDER`；
- `TRACEH_BASE_URL`；
- `TRACEH_MODEL`；
- `TRACEH_API_KEY_ENV`；
- `TRACEH_DATA_DIR`；
- `TRACEH_MAX_STEPS`；
- `TRACEH_VERIFY_COMMAND`；
- `TRACEH_PLUGIN_VERIFIER`（必须同时显式启用插件，且与命令 Verifier 互斥）。

`.env` 不覆盖已有进程环境变量；OpenAI-Compatible 模式必须显式提供 Base URL 和 Model，不内置某个厂商作为隐藏默认。真实 `.env` 被 Git 忽略，`.env.example` 只包含占位值。

### 13.3 默认 Runtime 参数

| 参数 | 默认值 |
|---|---:|
| max steps | 20 |
| tool timeout | 60 秒 |
| max tool output | 24,000 字符 |
| verification timeout | 60 秒 |
| max verification retries | 1 |
| data dir | `.traceh` |
| provider/model | `scripted` / `scripted-model` |

### 13.4 `traceh chat` 交互循环

Chat 是 v0.3 的交互式 MVP，由 [`cli/chat.py`](../../src/traceh/cli/chat.py) 与 [`cli/console.py`](../../src/traceh/cli/console.py) 实现，`cli/main.py` 只负责解析参数与装配 Runtime。

启动方式二选一，必须恰好提供一个，否则报使用错误（退出码 2，无 traceback）：

| 形式 | 行为 |
|---|---|
| `traceh chat <workspace>` | 校验 Workspace，创建新 Session，打印 session_id/workspace/provider/model，进入循环 |
| `traceh chat --session-id <id>` | 从事件日志读取原 Workspace，先执行 `RecoveryService.recover()`，仅当 `changed=true` 时打印一行恢复摘要，不创建 Turn、不注入任何隐藏指令 |

循环语义：

- 每条普通输入调用 `AgentRuntime.run_existing()`，在同一 Session 中新建一个 Turn；模型历史由事件日志投影，Chat 层不维护第二份 messages；
- 每轮打印 `assistant> <最终文本>` 和 `[reason=... steps=... tokens=... verification=...]`；
- `reason` 非 `completed` 时照常打印并返回提示符，不销毁 Session；
- Turn 抛异常时 AgentLoop 已写入 `runtime/error` 并闭合生命周期，Chat 只打印 `error: <类型>: <消息>`，不打印 traceback，继续等待下一条输入；
- 内部命令只在整行匹配时生效：`/help`、`/session`、`/plugins`、`/plugins reload`、`/plugins use ID [ID ...]`、`/plugins use --none`、`/exit`、`/quit`；空行忽略；未知插件命令不回显用户输入；这些命令都不产生 `user/message`、Turn 或模型请求。插件切换命令在异步控制面完整等待候选 setup/conflict/health、迁移授权、publish 和失败 rollback 后才返回提示符；
- EOF 等同 `/exit`，退出码 0；
- Ctrl+C 的完整语义见 13.8。要点：有活跃 Turn 时**首次** Ctrl+C 只取消该 Turn 并回到 `you>`，Session 保留；停在提示符上的空闲 Ctrl+C 才离开 Chat 并从进程内部返回 130（宿主最终显示什么仍由 Shell 决定，因此不承诺"PowerShell 一定看到 130"）；收敛期间重复 Ctrl+C 不能提前放行，收敛完成后以 130 离开；硬中断（Windows Ctrl+Break、控制台关闭）由操作系统直接终止进程，Python 处理器不会运行，实测退出码为 `3221225786`（`0xC000013A`），没有收敛提示行，只能依赖启动时就已打印的恢复信息与崩溃恢复；
- `runtime.dispose()` 在 `finally` 中执行，覆盖 Python 能够处理的所有退出路径（`/exit`、`/quit`、EOF、`KeyboardInterrupt`、取消、异常）。被操作系统直接终止的硬中断不在此列：那条路径上没有任何 Python 代码运行，靠的是崩溃恢复。

Turn 通过 `asyncio.shield` 提交，因此中断到达时 Runtime 仍持有该 Turn，可以走正常取消路径收敛，而不是留下脱缰任务。

Chat 控制面命令：

| 命令 | 语义 |
|---|---|
| `/plugins` | 显示当前 Composition Generation 真正启用的外部 `plugin_id==version`；无插件显示 `none` |
| `/plugins reload` | 用当前外部 id 重新 discovery/setup/conflict/health 并发布新 Generation；身份不变，不追加迁移授权 |
| `/plugins use ID [ID ...]` | 用显式已安装 Entry Point 插件集合迁移当前 Session；身份变化时追加 `composition/migration-authorized` |
| `/plugins use --none` | 切换到只含 `traceh.core` 的 Composition |

这些命令不是代码级 module reload，也不执行 `pip install`/`uninstall`。目标身份变化时，Runtime 在全局 Composition Gate 内确认没有活跃 Turn，候选完全健康后以 Session head CAS 追加授权，再 publish；授权已落盘但 publish 失败时保持 Session fail-closed，不能继续用旧 Generation。身份计算、`source_seq` 和 from/to 校验由 [`session/plugin_identity.py`](../../src/traceh/session/plugin_identity.py) 共享给 Runtime 与 InvariantChecker。

### 13.5 终端编码策略

`configure_stdio()` 把 stdin/stdout/stderr 统一配置为 UTF-8 且 `errors="replace"`，不依赖 `chcp 65001`；不支持 `reconfigure` 的流（测试中的 `StringIO`）安全降级并在报告中标明。

输入侧规则：

- 行首 U+FEFF 属于流而非消息，被剥离。Windows PowerShell 5.1 的 `Out-File -Encoding utf8` 会写入 BOM；PowerShell 7 的 `utf8` 默认无 BOM，需要时用 `utf8BOM`；
- 中文等非 ASCII 内容原样进入 `user/message`，只去除首尾空白；
- 若行内出现 U+FFFD，说明原字符在解码时已经丢失：调用模型前拒绝该行、打印提示、不写 `user/message`、不猜测原文。

### 13.6 Chat 实时 Timeline

Timeline 由两部分组成，职责分开：

| 模块 | 职责 |
|---|---|
| [`session/event_feed.py`](../../src/traceh/session/event_feed.py) | 通用、后端无关的进程内 Feed（6.7），不含任何终端文案 |
| [`cli/timeline.py`](../../src/traceh/cli/timeline.py) | 纯展示投影：`EventEnvelope` → 一行文本或 `None`，不打印、不写入、不改事件 |

`cli/chat.py` 把两者接起来：Turn 开始前订阅 Session Stream，Turn 期间由一个独立 Task 逐行打印，Turn 结束后先 `close()` 再等该 Task 排空，然后才打印最终回答。

行为事实：

- 默认开启；启动参数 `--no-timeline` 关闭。它是**启动参数**，不是 Chat 内部命令，`/help` 里如此说明；
- 每行格式为 `[event <seq>] <文本>`，其中 `<seq>` 是 Session Stream 里**真实的持久化序号**，不是 CLI 生成的行号；被隐藏的事件仍占用序号，所以行号通常不连续，这本身就是证据；
- 真实输出示例（`read_file` 一步 + 收尾一步）：

```text
[event 4] Turn started
[event 5] Step 1 started
[event 9] Model scripted/m called
[event 11] Model responded
[event 12] Tool read_file requested hello.txt
[event 13] Tool read_file started
[event 14] Tool read_file succeeded
[event 15] Step 1 completed
[event 16] Step 2 started
[event 23] Step 2 completed
[event 24] Turn ended (completed)
assistant> Done reading.
[reason=completed steps=2 tokens=0 verification=None]
```

- 显示的事件类型：`turn/start`、`turn/end`、`step/start`、`step/end`、`model/attempt-start`、`model/attempt-end`、`tool/call`、`tool/admitted`、`tool/result`、`verification/result`、`runtime/error`、`runtime/cancel-requested`、`runtime/recovered`；
- **默认不显示**：`composition/snapshot`、`request/snapshot`、`assistant/*`、`user/message`、`inbox/*`、`session/created`、`surface/replace`、Effect 事件，以及任何未知类型。未知类型渲染为空而不是打印原始 payload——会把不认识的东西一律打印的界面，正是秘密泄漏到终端的方式；
- **每个 payload 字符串都被当作不可信输入**。`tool_name` 来自模型响应，`error_type` 来自任意异常，路径来自工具参数。原样插值时，一个换行会伪造出一整行 Timeline，一个 ESC 字节会变成真正的终端控制序列。因此所有 payload 文本（`tool_name`、`tool_call_id`、`provider`、`model`、`reason`、`status`、`error_type`、可显示路径）都必须经过统一的 `sanitize()`，且 `payload_text()` 是处理器读取字段的唯一入口（`cli/activity.py` 的 Heartbeat 复用同一个入口）——这样就不存在“某个字段忘了过滤”的可能：
  - Unicode 分类为 `Cc`（含 ESC、CR、LF、退格）、`Cf`（含双向文本覆写）、`Cs`、`Co` 的字符统一替换为空格：ESC 序列失去 ESC 后剩下的括号文本是惰性的，换行再也无法伪造行，双向覆写再也不能重排用户看到的内容；
  - 随后折叠空白，结果**严格是一行**；
  - 统一长度上限 `MAX_DETAIL_CHARS`，超出则截断加省略号，长值无法把真实信息挤出屏幕。
- **`shell` 的 `command` 默认完全不显示**。命令行是最可能出现凭据的地方，而没有任何关键词扫描能可靠识别所有秘密形态；“扫几个词然后把其余原样打印”只是在等一个不常见的 Token 格式出现。因此 Shell 调用只显示工具名与 call id，对它执行什么一概不显示——这是无条件规则，不会被“看起来无害”的命令绕过。
- Tool 参数摘要因此只剩已知读取类工具的白名单参数（`list_files`/`read_file`/`search_text`/`apply_patch` 的 `path`）；这些值仍要经过凭据形态检查（关键词加上 `sk-`、`ghp_`、`xox?-`、URL basic auth 等形状），命中即**整段不显示**（部分遮蔽的秘密仍然是泄漏）。该检查是路径这一受限取值范围上的兜底，不是通用秘密探测器——这正是 `shell` 采取「不显示」而不是「扫描后显示」的原因。未知工具只显示工具名与 call id。
- payload 缺字段或类型不对时降级为较短的一行，绝不抛异常终止 Chat；
- **`runtime/error` 只显示 `error_type`**，不显示 `message`，也不显示 traceback。异常消息是任意文本：Provider 错误可能引用请求内容，认证失败可能引用它尝试过的凭据。Chat 自己那行 `error: <类型>: <消息>` 来自它捕获到的异常，是既有行为；Timeline 不再把同一段潜在秘密复制一遍。
- 已知残余边界（如实记录）：注入文本中的方括号内容会作为**该行内部的惰性文本**保留，例如 `tool_name` 里的 `[event 999]` 仍会出现在同一行中。保证的是“无法产生第二行”“行首始终是真实事件号”，而不是“无法出现形似标记的字符”；为工具名转义所有方括号会损失可读性，收益不足。
- `/help`、`/session`、空行、未知斜杠命令都不产生事件，因此也不产生 Timeline 行；
- 继续旧 Session 时只显示订阅之后的新事件，不重刷历史；恢复摘要行为不变；
- Turn 失败时：先排空已发布的 Timeline（含 `Runtime error` 那行），再打印原有 `error: <类型>: <消息>`，Chat 继续下一轮；
- `/exit`、`/quit`、EOF、Ctrl+C、异常和取消都会关闭订阅，不留订阅、Task 或队列引用；
- 输出为普通文本，不引入任何终端/UI 第三方库（项目唯一的运行时依赖 `packaging` 只用于 PEP 440 解析，见 1.1），遵循既有 UTF-8 终端策略（13.5）。

Timeline 是纯界面：它不进入 Model Surface，不改变 Request Fingerprint，也不写任何事件。

Timeline 每行还可带完成耗时，例如 `[event 11] Model responded (23.4s)`。该耗时由 13.7 的 Activity Tracker 用单调时钟测量，不是从 payload 读出、也不是由事件时间戳相减得到，因此它是显示注解而不是对持久化数据的断言。

### 13.7 Activity Heartbeat（等待提示）

纯事件驱动的 Timeline 恰好在用户最需要反馈时安静下来：`model/attempt-start` 与 `model/attempt-end` 之间没有事件，因此"Provider 很慢"和"程序卡死"在屏幕上无法区分。[`cli/activity.py`](../../src/traceh/cli/activity.py) 补上这段沉默。

```text
[event 9] Model openai-compatible/qwen-plus called
[waiting 10s] Model openai-compatible/qwen-plus is still working
[waiting 20s] Model openai-compatible/qwen-plus is still working
[event 11] Model responded (23.4s)
```

事实来源与边界：

- **只消费既有事件**：`model/attempt-start` 开始跟踪 `attempt_id`，`model/attempt-end` 停止；`tool/admitted` 开始跟踪 `tool_call_id`，`tool/result` 停止。**不修改 `AgentLoop`**，**不新增 heartbeat 事件类型**；
- **不是持久化事实**：不写 Event Log，不参与 Recovery / Replay / Surface / Request Fingerprint，不进入模型历史，也**不使用 `[event N]` 前缀**——那个前缀专属于真实 `seq`。前缀是 `[waiting <秒>s]`；
- **按身份独立跟踪**：`ToolRuntime` 会并发执行只读工具，因此按 `attempt_id`/`tool_call_id` 分别计时；单一"当前活动"槽位会只报告其中一个而丢掉其余；
- **无法识别身份就不跟踪**：`attempt_id`/`tool_call_id` 缺失或不是字符串时直接忽略。这类活动永远无法被配对结束，跟踪它等于永久泄漏一条等待提示；
- **显示内容严格受限**：只有清洗后的 Provider/Model、清洗后的 Tool Name、Tool Call ID 和已等待秒数。**不显示** Shell command、Tool arguments、Prompt、文件内容、Patch、stdout/stderr、Key 或异常 message。所有文本走 13.6 的同一个 `payload_text()`/`sanitize()`，因此注入无法伪造额外行或发出控制序列（同一条残余边界：形似标记的惰性文本仍可能留在该行内部）；
- **措辞按可证明的事实分开**，不许多说：
  - Model Attempt 的结束事件在 Provider 返回后立即追加，中间没有任何批处理，因此可以诚实地说 `is still working`；
  - Tool 则**不能**说"仍在运行"。`ToolRuntime` 对 parallel-safe 组使用 `asyncio.gather`，整组完成之后才追加各条 `tool/result`，因此从事件流上看，一个**已经执行完**的工具和一个**仍在执行**的工具完全无法区分。能证明的只有"尚未持久化结果"，所以行文就是 `has not reported completion`；
  - 同理，完成耗时的定义必须精确：Model 是 `model/attempt-start` → `model/attempt-end`，Tool 是 `tool/admitted` → **持久化的** `tool/result`。对 gather 组里的工具，这个耗时会长于它自身的执行时间；
- **报告的是跨过的阈值**而不是原始耗时，所以慢事件循环下仍然输出 `20s` 而不是 `20.3s`，同一阈值只报一次。

时间语义：

- 用**单调时钟**计算等待时长（`time.monotonic`）。墙钟会在系统时间被调整时跳变甚至倒退，导致等待时长胡说八道或整段不再触发；
- **按每个 Activity 自己的下一个阈值调度唤醒**，而不是自己固定滴答。固定滴答会把 Heartbeat 的相位锁在 Turn 的启动时刻而不是被观察的工作上：间隔 10 秒、工具在 t=10.1 启动时，t=20 那次唤醒只看到 9.9 秒于是保持沉默，第一条提示要到 t=30 才出现——距离用户开始等待已近 20 秒，而这正是本功能要覆盖的那段时间。`ActivityTracker.seconds_until_next_wait()` 返回最早到期的延迟，因此无论 Activity 在什么相位启动，第一条提示都出现在**它自己**启动后一个 interval 附近。没有 Activity 时按一个 interval 重新检查，这同时也让它与期间启动的 Activity 重新对上相位；
- 已经到期的阈值不再先 sleep 而是直接输出。这不会忙等：输出一行就会把该 Activity 推过该阈值；
- `Clock`（`monotonic` + `sleep`）是可注入边界，因此测试可以确定性地推进 10 秒、20 秒，而不是真的等 10 秒或靠 sleep 猜时序。测试用的 `ManualClock` **必须记录并遵守 deadline**：一个"任何 advance 都放行全部 sleeper"的夹具会让 0.1 秒和 10 秒的等待无法区分，这正是上述相位缺陷能通过一整套看起来很全的测试的原因，因此夹具自身的契约也有测试；
- 活动结束后不再产生任何输出，无论此后过去多久。

配置：

| 形式 | 行为 |
|---|---|
| 默认 | 10 秒 |
| `--heartbeat-seconds 0` | 关闭 Heartbeat，**保留**普通 Timeline |
| `--no-timeline` | 同时关闭 Timeline、Heartbeat 和 13.8 的序号说明 |
| 负数 / NaN / Infinity | 明确报 `CliConfigurationError`，不静默钳制 |

它是**启动参数**，不是 Chat 内部命令；`/help` 与 README 都如此说明。

**当前覆盖范围的边界：Verifier 仍然静默。** Heartbeat 只能跟踪 Model Attempt 与已准入的 Tool，因为只有这两类有明确的"开始"事件。`CommandVerifier` 没有"开始"事件——协议里只有 `verification/result`，它在验证命令**结束之后**才追加。因此一个跑很久的验证命令（例如整套 `python -m pytest`）在屏幕上依旧完全安静。本轮**刻意不**用"模型没有 Tool Call、所以大概要开始验证了"这类 UI 侧推测去猜它是否启动：那是把界面猜测当成事实。是否新增 `verification/start` 协议事件属于事件协议变更，留给后续独立设计。当前只输出普通文本行，没有 Spinner、颜色、`\r` 原地刷新或 TUI。

### 13.8 Ctrl+C 生命周期与恢复信息

#### 首次 Ctrl+C 只取消当前 Turn

修复前的顺序有一个实质缺陷：`_run_turn()` 在 Runtime 真正追加 `runtime/cancel-requested`、`model/attempt-end (cancelled)`、`step/end`、`turn/end` **之前**就关闭了 Timeline 订阅，因此整段取消收敛过程被发布给了"没有人"，用户只看到输出突然停止。

现在的顺序是：

```mermaid
flowchart TD
    CTRLC["用户按下 Ctrl+C（asyncio.run 取消主任务）"] --> KEEP["Timeline 订阅保持开放"]
    KEEP --> CANCEL["runtime.cancel()：追加 runtime/cancel-requested 并取消 Turn"]
    CANCEL --> CONV["等模型、工具与子进程完整收敛"]
    CONV --> EVENTS["Timeline 消费 cancellation 与生命周期结束事件"]
    EVENTS --> DRAIN["Drain Timeline 与 Heartbeat"]
    DRAIN --> NOTICE["打印 Turn interrupted. This session is still open."]
    NOTICE --> PROMPT["回到 you>，同一 Session 继续"]
```

实际输出：

```text
[event 31] Cancellation requested
[event 32] Model attempt cancelled
[event 33] Step 2 ended (cancelled)
[event 34] Turn ended (cancelled)
Turn interrupted. This session is still open.
you>
```

语义要点：

- 第一次 Ctrl+C **只取消当前 Turn**，不结束 Chat、不新建 Session、不自动注入"继续任务"消息。下一个新 Turn 由用户的下一条输入创建；
- Python 3.11+ 的 `asyncio.run` 把 SIGINT 实现为取消主任务，因此这条路径在代码里就是 `CancelledError`；处理完后显式调用 `Task.uncancel()` 清除取消状态，否则下一个 `await` 会把用户刚保住的 Session 直接中断；
- 若 Turn 恰好在中断与 cancel 之间正常结束，则照常打印它真实的结果，而不是谎称被中断。

#### 空闲 Ctrl+C

停在 `you>` 且没有活跃 Turn 时，Ctrl+C 直接离开 Chat，从进程内部返回既有的 `130`，并再次打印完整恢复信息。

#### 收敛期间重复 Ctrl+C

收敛过程委托给共享的 [`await_worker_convergence()`](../../src/traceh/concurrency.py)，它吸收后续取消并继续等待**同一个** Future，因此第二次、第三次 Ctrl+C 都不能提前放行：模型 Worker、Shell/Verifier 子进程和 Timeline Printer 都不会脱缰。收敛完成之后才承认第二次意图——以 `130` 离开。

#### 硬中断边界（不做虚假承诺）

Ctrl+Break、关闭控制台或被操作系统直接终止时，**没有任何 Python 代码会运行**，因此上述收敛与提示都不会发生。那条路径只能依赖启动时已经打印在屏幕上的 Session 信息加崩溃恢复（11 节），这也正是恢复信息在**启动时**而不是只在退出时打印的原因。

#### 恢复信息提前可见，且按目标 Shell 安全渲染

Banner、`/session`、`/exit`、`/quit`、EOF、空闲中断与重复中断退出都会打印可直接复制的命令：

```text
resume later (PowerShell):
  traceh chat --session-id <id> --data-dir <绝对 data_dir> --provider <p> --model <m> [--max-steps N] [--script <绝对路径>] [--base-url <url>] [--api-key-env NAME] [--env-file <绝对路径>]
  traceh sessions --data-dir <绝对 data_dir>
  note: this restores the session and its non-secret settings; it is not a complete configuration snapshot.
```

##### 命令按 Token 构造，再由指定 Shell 渲染

这段文字会被粘进 Shell，因此它是**不可信文本变成 Shell 语法**的地方。只在含空格时加双引号是不够的：PowerShell 在引号之外把 `&`、`;`、`|`、`$(...)`、反引号当作语法，一个未引用的值可以结束当前命令并开始另一条。

[`cli/command_line.py`](../../src/traceh/cli/command_line.py) 把这件事变成不可表达：

- 调用方只组装 **argv token 列表**，从不自己拼命令文本；渲染只发生一次、在一个地方、针对一个具名 Shell；
- **每个 Shell 一套引用规则，绝不共用**。PowerShell 用单引号字面量（内部单引号按 PowerShell 自己的规则写成两个），POSIX 用标准库 `shlex.quote`。Windows 上输出标注为 PowerShell，其余平台标注为 POSIX shell；
- 只有 `Literal`（本仓库自己写死的程序名、子命令名、参数名）且字符集可证明安全时才裸输出。这不是为了美观：**PowerShell 把语句开头的带引号字符串解析为表达式**，`'traceh' 'chat'` 只会打印单词而不执行任何东西，加引号的命令名会得到一条静默什么都不做的命令。标错 `Literal` 的值仍会退化为加引号，而不是退化为注入；
- 含控制字符或换行的值**拒绝渲染**而不是转义：换行会产生第二条命令行，不应该指望任何引用规则去挡它。

"什么算这种字符"由 [`cli/text_safety.py`](../../src/traceh/cli/text_safety.py) 一处定义，命令渲染、`escape_for_display()`、Base URL 检查与 Timeline 的 `sanitize()` 全部读它。这条集中化不是整洁性问题而是修了一个真缺陷：各处原本各写一份"控制字符"，都只判断 Unicode `C*` 类别，于是**都漏掉了 `U+2028 LINE SEPARATOR`（`Zl`）与 `U+2029 PARAGRAPH SEPARATOR`（`Zp`）**。它们对 `str.splitlines()` 以及大量编辑器、日志查看器和渲染器都是换行，实测：

```text
escape_for_display("x<U+2028>note: forged").splitlines()  ->  ["x", "note: forged"]
is_renderable("x<U+2028>note: forged")                    ->  True
```

现集合为 `Cc`、`Cf`、`Cs`、`Co`、`Zl`、`Zp`：命令渲染拒绝它们，`escape_for_display()` 显示为 `\u2028`/`\u2029`，Timeline 的 `sanitize()` 把它们替换为空格。

此时的 fallback 也必须自洽：**它显示的每个派生值都经过 `escape_for_display()` 转义**（`\n`、`\r`、`\x1b`、`\u202e` 等以可见写法呈现，并限长）。否则会出现最荒谬的情况——正是那个"无法安全显示"的值，在解释"无法安全显示"的那段文字里又打出了第二行终端输出。实测旧实现即如此。用户仍能看到转义后的 session_id、data 目录和"为什么没有生成命令"。

##### 定位 Session 与恢复运行行为是两件事

- **定位**需要 `--session-id` 与解析后的绝对 `--data-dir`：Store 在 data 目录之下，用过自定义 `--data-dir` 或换了工作目录的会话，只靠 `session_id` 打不开；
- **恢复行为**需要 provider、model 等，因为它们可能来自原工作目录的 `.env`。只带前两项的命令会在新目录重新解析配置，把会话**静默切换到另一个模型**——已确定性复现：原会话 `model=custom-model`，在另一个 cwd 执行旧版命令后 `model` 解析为 `None` 并回落到默认 `scripted-model`。

##### 它不是完整配置快照，并且明说

命令自带一行 `not a complete configuration snapshot`。两类值**不原样回显**：

| 值 | 处理 |
|---|---|
| `--verify-command` | 任意 Shell 文本，无法既展示又证明其中没有凭据，因此一律省略。**只有当本次生效的 Verifier 确实来自这次加载的 env-file 时**才提示由该文件恢复；否则打印 `Verifier command omitted from the displayed resume command; re-supply it manually.`。命名插件 Verifier 不含 Shell 文本，因此按普通安全 token 直接写为 `--plugin-verifier`，并同时保留对应 `--plugin` |
| Base URL | 用 `urllib.parse` 做**结构检查**：内嵌 username/password，或带 query/fragment 时不显示并说明原因。对任意 query 一律 withhold，是为了不必判断哪个参数名敏感。解析本身也可能抛 `ValueError`（`https://[bad` 只在检查 userinfo 时才报 `Invalid IPv6 URL`），因此解析与 userinfo 访问都在 `try` 内：解析失败同样是**不显示 + 说明原因**，绝不把原值或 traceback 摆到用户面前 |

##### Verifier 的来源必须按"哪个值真正生效"判断

"env-file 里含 `TRACEH_VERIFY_COMMAND`"**不等于**"env-file 能恢复 Verifier"。优先级是：显式 `--verify-command` > 已存在的进程环境变量 > env-file。因此：

| 情形 | 生效值 | 能否声称由 env-file 恢复 |
|---|---|---|
| env-file 有该键，且**没有**显式参数与已存在的进程变量 | env-file 的值 | 可以 |
| env-file 有该键，但传了 `--verify-command` | 显式值 | **不可以**，必须提示手动重新提供 |
| 该变量在启动前已存在于进程环境 | 进程环境的值（`.env` 不覆盖已有变量，因此不会进入 `applied_keys`） | 不可以 |

这个判断只有参数解析阶段掌握全部信息，因此在 `_configure_from_environment()` 中计算，并以**布尔值** `verifier_from_env_file` 传给显示层。Verifier 的**文本本身不进入** `ResumeEnvironment`：该 dataclass 没有能装它的字段，因此它也不会出现在 repr、恢复命令或任何日志行里。

必须准确描述这条规则的能力：它是**结构规则，不是通用秘密探测器**，无法判断一个看起来普通的路径段本身是不是凭据。因此本文不使用"秘密永不打印"这类绝对措辞，而是给出可验证的具体规则。

##### 非法环境变量名在配置阶段就失败

`--api-key-env` 与 `TRACEH_API_KEY_ENV` 的取值必须是合法环境变量名（字母/数字/下划线，不以数字开头），校验发生在**创建 Runtime 与 Session 之前**，非法值抛 `CliConfigurationError`。

以前的行为是：接受它、Provider 拿它去查、恢复命令再静默省略——于是下一次运行悄悄退回 `OPENAI_API_KEY`。校验规则由 [`cli/env_file.py`](../../src/traceh/cli/env_file.py) 的 `validate_env_var_name()` 与 `.env` 解析共用，因此只有一处定义。规则**不因 Provider 而异**：`scripted` 运行时忽略 Key，并不能让一个查不到的名字变成合法配置，否则同一份配置换成 `openai-compatible` 就会失败。**错误信息完全不回显被拒绝的值。** 只做转义是不够的：转义防的是控制字符，防不了一个可打印的秘密。这个设置最常见的写错方式恰恰是**把 Key 本身粘到了变量名的位置**，因此非法值正是最不能打印的东西。也不显示任何可用于推断的派生信息——长度、前后缀、哈希都不显示。消息只说明设置名与合法格式，这已经足够修好它。同理，`.env` 解析遇到非法左侧变量名时也只报行号，不回显该文本：左侧同样可能是被粘错位置的 Key 或带控制字符的内容。

必须同时说明这条规则的**能力边界**：校验判断的是"它是不是一个可用的变量名"，无法区分"一个恰好长得像标识符的 Key"。`ghp_...`、`AKIA...` 这类值是合法标识符，会被接受，并作为配置的变量名出现在恢复命令里。把所有形似凭据的标识符一律拒绝会误伤 `GH_TOKEN` 这类正常名字，因此诚实的说法是：**这里校验的是形状，不是意图**。

API Key 的**值**不被读取也不被打印，命令里只出现其**环境变量名**：

- 由附带的 env-file 提供时（`env_file_supplies` 命中）措辞是"可从该 env-file 或 Shell 获取"；
- 否则提示需要在新 Shell 中设置；
- `provider=scripted` 时**不打印** `--api-key-env`，也不提示 `OPENAI_API_KEY`——对一次 Scripted 运行这是误导性指令。

显式用过 `--script` 时携带其解析后的绝对路径，并附明确说明：Scripted Provider 的响应游标**不跨进程持久化**，重新加载同一文件会从第一条响应重新开始。省略它会静默换成内置占位 Provider，因此必须携带。

忘记 `session_id` 时用 `traceh sessions --data-dir <data-dir>` 列出候选。

### 13.9 为什么第一条 Timeline 是 `[event 4]`

`seq` 1-3 是 `session/created`、`inbox/accepted`、`inbox/claimed`——它们**确实被持久化了**，只是 Timeline 不显示，所以第一条可见事件通常是 `turn/start`，即 `[event 4]`。

这里刻意**不重新编号、不引入假的显示序号**：真实 `seq` 才是审计与 JSONL 回查能力，把 4 显示成 1 会毁掉"这个号能在事件日志里查到"这个唯一有价值的性质。

因此 Timeline 开启时，在启动阶段打印一次非事件说明：

```text
Timeline shows selected persisted events.
Numbers shown as [event N] are Event Log seq values; they may start above 1 or skip where internal events are hidden.
```

- 只打印一次；
- 措辞刻意**不以** `[event ...]` 开头——以方括号开头的行是 Timeline 行，一条模仿它的说明会同时误导读者和日志过滤；
- `--no-timeline` 时不打印；
- 继续旧 Session 时同样打印，而且那里更需要：第一条新事件的序号可能是 40 或 400，前面屏幕上什么都没有。


## 14. 已有扩展边界与未来接口

当前代码中存在但尚未形成完整产品能力的边界：

| 方向 | 已有协议/原语 | 当前状态 |
|---|---|---|
| 插件（Provider / Tool / Prompt / Policy / Middleware / Verifier / Service） | `PluginManifest`、`Plugin`、`PluginContext`、`PluginManager` | **已实现 application-scope、trusted、进程内贡献**，见 19 节；Provider/Verifier 必须由宿主显式选择 |
| 可逆生命周期 | `Activation`、`Lifespan`、`OwnedTaskSet` | **已实现**并被 PluginManager 真正使用，含取消收敛。`OwnedTaskSet` 是**生命周期所有权，不是后台任务监督器**，见 19.12 |
| 插件提供 Provider / Policy / Middleware / Verifier | `register_provider()`、`register_policy()`、`register_middleware()`、`register_verifier()` | D3 已接入私有候选 → ActivationSet → Generation → Step Lease 主线；setup 后冻结贡献入口和注册时名称，冲突在 health 前失败并保留归因，ActivationSet 与 Generation 做对象身份守卫，Provider/Verifier 无显式选择时不改变行为 |
| EventStore 替换 | `EventStore` | 仍只能在 Runtime 构造时直接注入；不能由可热替换的 `PluginContext` 提供，因为 SessionService/Event Log 是进程级持久化事实源 |
| 服务与 Scope | `ServiceKey`、`ServiceRegistry`、`ServiceView`、`ScopeKind`、`ScopedServiceBinding`、`ScopeChain` | D1 已实现四层 Service 解析并接入默认 Runtime、插件候选 Generation 与 Step Lease；同层再次绑定必须显式 `replace=True`，跨层覆盖同样要求严格布尔值 `True` 且 API Major 相同，失败装配不污染调用方 Registry，发布后的公开视图只读 |
| Tool / Prompt / Policy Overlay | `ScopedToolBinding`、`ScopedPromptBinding`、`ScopedPolicyBinding`、`CompositionOverlayPlan` | D2 已按固定四层顺序解析同名能力；同层重复与跨层覆盖都要求严格布尔 `replace=True`，失败只发生在私有 fork；插件 application Tool/Prompt 晚贡献会在 health 前重新校验。解析结果进入已有 ToolRegistry、PromptAssembler、ToolRuntime Policy tuple 与 Composition Snapshot，不产生第二套 Runtime |
| Composition Generation 与用户切换 | `CompositionGeneration`、`GenerationCompositionRuntime`、`CompositionRuntime.lease()`、`PluginActivationSet`、`PluginGenerationBuilder`、`composition/migration-authorized` | Stage A/B 生命周期、Stage C Chat `/plugins` 控制面、D1/D2 Scope 装配和 D3 执行能力贡献均进入默认主线；用户只能切换当前进程可发现的已安装组合，仍没有 Wheel/module 级热替换或子层插件 setup |
| isolated 插件 | `PluginManifest.trust_mode` | 可声明，激活**明确拒绝**；无进程边界、无序列化契约、无崩溃子进程失败模型 |
| 多 Agent | `AgentSpec`、`AgentHandle`、`AgentSupervisor` Protocol、Budget DTO | 活跃 Supervisor、Inbox、子 Agent Tools、冷恢复均缺失 |
| Workspace 分支 | `WorkspaceProvider`、Snapshot、PatchArtifact、MergeResult | Git Worktree/Overlay 实现和协调缺失 |
| Workflow | 可复用单 Agent Runtime 边界 | Workflow Engine、Map/Join/Approval 节点缺失 |

标记为“协议存在但未实现”的行，不得在文档或对外说明中表述为已实现能力。反过来，插件系统本身现在**是**已实现能力，旧文档中“没有完整 PluginManager”的说法已经过时并被本轮改写。

## 15. 测试与验证基线

### 15.1 本地标准检查

```powershell
python -m compileall -q src tests
python -m pytest -o addopts='' -q
python -m ruff check src tests
```

带 `slow` 标记的打包验收会构建 Wheel 并创建虚拟环境；需要跳过时用 `-m "not slow"`。

当前测试套件收集 `1088` 项，完整门禁为 `1087 passed, 1 skipped`。D1/D2 原有 Scope/Overlay、Policy 身份、Request 重建与跨 Runtime 隔离契约继续全绿。D3 新增 [`tests/test_plugin_extended_contributions.py`](../../tests/test_plugin_extended_contributions.py)：真实 AgentLoop 使用显式选中的插件 Provider；缺失 Provider/Verifier 和 Provider/Policy/Middleware 冲突在 health 前以固定 code 失败并完整 rollback；setup 结束后贡献入口关闭，health 不能再补注册 Policy/Middleware 绕过冲突检查；Tool、Provider、Policy、Middleware 的注册时名称会被单独保存，health 改写原对象名称会以 `plugin-contribution-identity-changed` 拒绝并回滚，Registry 撤销也只认注册时键；公开 `prepare_activation_set()` 返回后即使调用方让出事件循环，Generation 交接仍会按 transfer receipt 复核 Registry 成员、对象身份与固定名称，后台 Owned Task 的晚到改名会在 claim 前拒绝，Snapshot schema 与 ToolRuntime 查找键不能在同一代分裂；若 receipt 或 Scope 校验在 ActivationSet 构造时失败，Builder 会在所有权尚未交给调用方时 dispose 临时 Manager，取消并等待 Owned Task、恰好一次执行 cleanup，重复取消不能打穿收敛等待，同时保留原始交接错误与 cleanup 失败；两者都是普通 `Exception` 时对外仍是 `ExceptionGroup`，原始失败是 `KeyboardInterrupt`、`SystemExit` 或其他直接 `BaseException` 时则由 `BaseExceptionGroup` 保真，不能再被“分组类型不支持”产生的新 `TypeError` 遮蔽；Policy Overlay 冲突保留责任 `plugin_id`；ActivationSet 提供 LLM Registry 时，缺失所选 Provider 与对象身份不一致同样拒绝；插件 Policy/Middleware 真实经过 ToolRuntime 且名称进入 Composition Snapshot；命名 Verifier 只有显式选择才运行并追加 `verification/result`；Verifier 被 Gate 卡住时发布新 Generation，旧 Step 仍使用旧 Verifier，旧 Activation 直到 Lease 释放才 cleanup；取消 setup 会反向 dispose 四类新 Registration。旧式自定义 ActivationSet 没有 D3 `llms` 属性时，替换路径仍借用协调器已有核心 Registry；一旦候选显式提供 LLM Registry，就不能回退绕过身份守卫。CLI 测试还钉住自定义 Provider 必须同时显式启用插件和提供 Model、`--plugin-verifier`/`TRACEH_PLUGIN_VERIFIER` 必须显式启用插件、与 `--verify-command` 互斥，并在恢复命令中和插件 id 一起保真。若命令行显式选择一种 Verifier，它会覆盖较低优先级环境变量中的另一种；两种选择都来自命令行或都来自环境变量时仍明确报冲突。D3 原有三项反向验证继续保留；公开候选交接复核也做过反向验证：临时移除守卫时确定性测试稳定出现 `DID NOT RAISE`，恢复后才继续完整门禁；临时恢复“activate 后直接 transfer”的旧路径时，交接失败与 cleanup 失败测试会收到裸 `ValueError`，证明临时 Manager 的回滚保护不可省略；临时把 `BaseExceptionGroup` 换回 `ExceptionGroup` 时，直接 `BaseException` 反例稳定失败于 `TypeError: Cannot nest BaseExceptions in an ExceptionGroup`，恢复正确容器后交接四项契约与全量门禁重新通过。

- EventStore expected-seq、尾部恢复和读取；
- EventStore 所有权契约（[`tests/test_event_store_contract.py`](../../tests/test_event_store_contract.py)，核心用例对 `InMemoryEventStore` 与 `JsonlEventStore` 参数化）：修改原始 `PendingEvent` 输入、修改 `append()` 返回值、修改 `read()` 返回值都不改写 Store 历史；两次 `read()` 不共享可变图；复用同一嵌套输入的多个事件互不影响；`to_dict()` 与 `from_dict()` 双向脱离；`from_dict()` 仍拒绝非对象 payload；`detach_event()` 保留全部元数据并在真实 Store 往返后仍是 `UUID`/`datetime` 而非字符串；`detach_event()` 对真正不受支持的值（`set`、任意对象）抛 `TypeError`，但对受支持的框架类型是**规范化而不是拒绝**（`Path` → 字符串、`tuple` → `list`，含嵌套与 `list` 内的 `tuple`），对 scalar 不做包装；两个 Store 并排跑同一组修改后观察到的历史必须逐字相同；`expected_seq`、`ConcurrencyConflict`、`head()` 与被拒绝写入后的流状态不因复制边界而改变。用例一律真实修改嵌套结构再重新读取，不满足于断言两个对象不是同一个；
- 进程内 Event Feed 契约（[`tests/test_event_feed.py`](../../tests/test_event_feed.py)，全部用例对 `InMemoryEventStore` 与 `JsonlEventStore` 参数化）：append 成功后才发布；`ConcurrencyConflict` 发布 0 条且 Head 不变；一批多条按 seq 顺序；三个真实竞争写入者（读 Head → append → 冲突重试）下发布顺序必须等于 Store 中的 seq 顺序；两个 Subscriber 都收到同一批逻辑事件；两个 Subscriber 的嵌套 payload 不共享；Subscriber 修改不污染 Store；先前 Subscriber 的修改不影响后来者；close 后不再投递且订阅计数归零；重复 close 安全；close 前已排队事件仍可 drain（这正是"Timeline 先于回答"的机制）；对已耗尽订阅再迭代返回空而不是死锁；完全不消费的订阅者不阻塞 20 次连续 append 且事件确实排队未丢；Session 与 Effect Stream 严格隔离；抛异常的消费者在自己的 Task 里失败、不影响该次 Store append，也不影响后续 append；发布不产生任何新事件类型；订阅不重放历史；装饰器完整代理 `read`/`head`/`list_streams`；
- Feed 只读接口与连线：消费者接口上不存在任何公共 publish 方法，无法从公开观察面注入伪 Envelope（伪造的 Envelope 既到不了 Subscriber 也进不了日志）；`Durability` 由 Spy Store 证明原样透传，`BATCHED` 不被偷偷升级为 `SYNC` 且 `BATCHED` 追加照样会被发布；`AgentRuntime.events` 是必填参数（用签名断言）、且与 `PublishingEventStore` 发布目标是同一个对象；经 `runtime.sessions` 写入后 `runtime.events` 的订阅者确实收到事件；
- Timeline 终端安全：10 种恶意 payload 值（`\n`、`\r`、`\x1b[2J`、`\b`、`\a`、`\0`、`\u202e`、`\u200b`，即换行、回车、清屏 ESC、退格、响铃、NUL、双向覆写、零宽字符，另加 ANSI 颜色序列与 500 字符超长值）× 13 个被插值字段（`tool_name`、`tool_call_id`、`provider`、`model`、`reason`、`status`、`error_type` 等）全部参数化，断言每行严格一行、无 `Cc`/`Cf`/`Cs`/`Co` 字符残留、无 ESC、长度有界；形如整行的伪造 `tool_name` 无法产生第二行且行首仍是真实事件号；6 种凭据形态的 Shell 命令（`sk-proj-`/`ghp_`/`xoxb-`/URL basic auth/环境变量赋值/连接串，全部为明确标注的 FAKE/FIXTURE 夹具）一律不显示，无害命令同样不显示；`runtime/error` 的 message 与 traceback 均不显示；`sanitize()` 幂等有界且不破坏正常中文；端到端一轮里模型选择的恶意工具名不会伪造 Console 行；
- Timeline Drain 收敛：Gated Printer 自己点亮 `entered` 并阻塞，Drain 连续被取消 3 次、每次都让事件循环真正调度后断言 Drain **仍未结束**且 Printer 仍未结束，释放后 Drain 才重新抛出 `CancelledError`，并断言 Printer 已 done、订阅计数归零、无遗留 `traceh-chat-timeline` Task；Drain 必定先关闭订阅（否则真实 Printer 永不结束）；Renderer 主动抛异常时两轮 Turn 仍完成、两条最终回答都打印、Chat 继续、订阅与 Task 均清理，且事件日志未因观察者失败而出现 `runtime/error`；
- Activity Heartbeat、Ctrl+C 生命周期与序号说明（[`tests/test_cli_activity.py`](../../tests/test_cli_activity.py)，全部用可注入的 `ManualClock` 推进时间，无真实等待）：每个跨过的阈值只报一次且 9.9s 不触发；结束时返回并显示实测耗时；并发两个 `tool_call_id` 各自计时（`ToolRuntime` 的 gather 组只有在整组完成后才追加各条 `tool/result`，因此“其一先完成”只能由 Tracker 层按事件序列驱动验证，不能声称真实 Feed 能观察到逐个完成）；缺少 `attempt_id`/`tool_call_id` 或类型不对时完全不跟踪；Heartbeat 绝不显示 arguments（含 `shell` 的 command 与假 Key 夹具）；恶意 `tool_name`/call id 无法伪造额外行或发出 ESC；`0` 关闭 Heartbeat 但保留 Timeline；`--no-timeline` 同时关闭 Timeline、Heartbeat 与序号说明；负数/NaN/±Infinity 明确报错；`--heartbeat-seconds` 解析默认值；默认 Clock 确实是 `time.monotonic` 而非墙钟；快速 Turn 不产生任何 waiting 行；活动结束后再推进 100 秒也不再输出；Heartbeat 期间事件总数不变且无 heartbeat 类事件、不变量为 0；Console 抛异常与 Turn 失败都不留 Heartbeat/Timeline Task；
- Heartbeat 相位与夹具保真：`seconds_until_next_wait()` 从 Activity 自身起点计算（t=10.1 启动的活动在 t=20.0 仍未到期、t=20.1 到期，报完后下一次到期推进一个 interval），多个活动取最早到期者；端到端让模型调用先占住 10.1 秒使工具**刻意错相位**启动，断言工具在自身 9.9 秒时仍无提示、10.1 秒时首报，且距其启动不足两个 interval；`ManualClock` **自身的契约也有测试**（sleeper 必须按各自 deadline、按顺序唤醒），因为一个“任何 advance 都放行全部 sleeper”的夹具会让 0.1 秒与 10 秒无法区分，正是这种夹具能让相位缺陷通过一整套测试；
- 恢复命令的 Shell 渲染安全（[`tests/test_cli_resume.py`](../../tests/test_cli_resume.py)）：16 个含 `&`、`;`、`|`、`$()`、`$var`、反引号、单双引号、括号、花括号、`@`、中文路径与尾随空格的取值全部参数化，断言 PowerShell 渲染后可按其自身规则还原回原值、内部单引号确实成双、且整段只是一个带引号字面量；POSIX 渲染用真实 `shlex.split` 往返校验；换行、CR、NUL、ESC 与双向覆写一律拒绝渲染并抛 `UnsafeCommandValue`；命令名作为 `Literal` 不加引号（否则 PowerShell 把它当表达式，命令静默什么都不做），而标错 `Literal` 的不安全值仍会退化为加引号；两个 Shell 的渲染结果必须不同；未知 Shell 名被拒绝；`--verify-command` 在含假 Token 时零回显并给出指定文案（来源判定见下一条）；带 userinfo/query/fragment 的 Base URL 一律不显示并说明原因，普通 URL 与含 `&` 的 URL 都只作为一个带引号 token 出现；data_dir、model、session_id 同时含 `&;|$()` 与引号时不产生第二条命令；含控制字符时完全不打印命令但仍显示 session_id；`provider=scripted` 不打印 `--api-key-env` 也不提及 `OPENAI_API_KEY`，OpenAI-Compatible 才打印且区分"在 Shell 中设置"与"可从 env-file 获取"；`--script` 携带绝对路径并附游标不持久化说明，未使用时不出现；env-file 只在加载过时出现；命令自带"不是完整配置快照"；
- 单行安全的 Unicode 边界：`U+2028`/`U+2029` 在两种 Shell 渲染器上都被拒绝、`escape_for_display()` 显示为 `\u2028`/`\u2029` 且 `splitlines()` 只有一行、`_safe_base_url()` 对含它们的 URL withhold、Timeline 的 13 个可插值字段都无法借它们伪造第二行、fallback 无法被它们拆出伪造的 `note:`/`traceh chat`/`[event ]` 行；两处测试辅助断言都改用 `splitlines()` 加显式分隔符检查，而不是只看 `\n`/`\r` 与 `C*` 类别——旧写法正是让这个缺陷通过整套测试的原因；共享类别集合本身也有测试，防止两处再次漂移；
- 拒绝值零回显：4 种被粘错位置的假凭据（`sk-proj-`、`xoxb-` 及带空格/等号的形状）在 `--api-key-env` 与 `.env` 左侧两条路径上都不出现在错误消息里，且消息不含长度、前 4 位或后 4 位；含 ESC、换行、`U+2028`、`U+2029`、双向覆写的名字，错误消息仍是单行安全文本且不含输入片段；同时**明确钉住能力边界**——形似标识符的 `ghp_...`/`AKIA...` 会被接受，因为校验的是形状而非意图；
- 恢复命令安全检查的健壮性（[`tests/test_cli_resume_safety.py`](../../tests/test_cli_resume_safety.py)）：5 种无法解析的 URL（`https://[bad`、`https://[::1`、`http://[` 等）必须"不显示 + 给出原因"而不是抛异常，且原因不回显原值；7 种应被 withhold 的 URL（userinfo、query、fragment、换行、ESC、双向覆写）各自给出对应原因且不泄漏假密码；无法解析时命令的其余部分照常生成；4 种恶意值 × 3 个字段（session_id / data_dir / model）验证 fallback 里**每个派生值都被转义**、逐行断言无控制字符、无伪造的 `traceh chat`/`note:`/`[event ]` 行、`note:` 恰好一条，且仍能看到定位信息；`escape_for_display()` 惰性、有界且不破坏中文；7 种非法环境变量名（含 `bad;name`、空串、以数字开头、含换行）在 `--api-key-env` 与 `TRACEH_API_KEY_ENV` 两条路径上都抛 `CliConfigurationError`，`scripted` 也不例外，报错信息本身单行无控制字符**且完全不回显取值**，4 种合法自定义名与内置默认值仍然通过；
- Verifier 来源判定：env-file 含 `TRACEH_VERIFY_COMMAND` 但传了显式 `--verify-command` 时，`verifier_from_env_file` 必须为 `False`、提示手动重新提供、且假 Token 夹具零回显；无显式参数且 env-file 的值真正生效时才为 `True` 并提示由该文件恢复，同样零回显；变量已存在于进程环境时（`.env` 不覆盖）也为 `False`；`ResumeEnvironment` 的字段里根本没有能装 Verifier 文本的位置；
- 恢复命令的配置保真：解析打印出的 `traceh chat …` 命令，在**另一个工作目录**且清空 `TRACEH_*` 后重新走一遍配置解析，断言 `provider`/`model` 与原会话一致（旧版在此处会把 `model` 丢成默认值）；只打印 API Key 的**变量名**且输出中不含任何 Key 形态；`.env` 只在确实加载过时才写入命令；没有 `.env` 时命令仍能靠显式 flag 复现配置；
- Ctrl+C 生命周期：中断模型调用时订阅在取消发生的那一刻仍然开放，Console 依次出现 `Cancellation requested`、`Model attempt cancelled`、`Step 1 ended (cancelled)`、`Turn ended (cancelled)`，全部早于 `Turn interrupted` 提示；不变量为 0、开放 Turn/Step 均为 `None`；同一 Session 的第二条输入创建了真正的第二个 Turn；中断工具时 `tool/call` 与 `tool/result` 数量相等（取消路径补齐）；用受 Gate 控制的 `cancel()` 证明连续 3 次取消都无法让收敛提前返回、且 `cancel()` 不会被重复发起；真实场景下第二次 Ctrl+C 在收敛后才离开且无残留；空闲 Ctrl+C 返回 130 并打印含 `session_id` 与解析后 data dir 的恢复命令；
- 恢复信息与事件序号：新建 Session 在任何 Turn 之前就打印 `resume later:`（含空格的 data dir 也正确加引号），继续旧 Session 与 `/session` 各打印一次；`--env-file` 明确不被猜进命令；新 Session 第一条可见事件的 `seq` 确实是 4、被隐藏的三条确实是 `session/created`/`inbox/accepted`/`inbox/claimed`、没有被重编号为 1；说明行不以 `[event N]` 开头；说明只打印一次；继续旧 Session 时不重放历史且最小显示序号大于既有历史长度；
- Timeline 投影与 Chat 实时性（[`tests/test_cli_timeline.py`](../../tests/test_cli_timeline.py)）：**Gate 工具在 `execute()` 里点亮 `entered` 并阻塞，测试据此在 Turn 尚未结束时断言 Console 已出现 requested/started 行、且尚无 succeeded 与 `assistant>`，释放后再断言 succeeded 与最终回答，并断言两者的输出顺序**；每行携带真实持久化 seq 且渲染出的 seq 全部能在事件日志里找到、且刻意断言序号不连续（证明不是 CLI 行号）；`step/end` 复用 `step/start` 的编号，缺少 start 时仍能渲染；Tool 生命周期与失败 `error_type`；两种 Verification 结果；`runtime/*` 与 `runtime/recovered`；10 类噪声/未知事件一律渲染为空（含塞入假 Key 的 request payload）；11 组缺字段/错类型 payload 不抛异常；shell 摘要单行限长；命中凭据特征时整段不显示；未知工具只显示名与 call id；渲染不修改事件；`--no-timeline` 完全静默但最终回答与摘要不变；继续旧 Session 时最小显示序号大于既有历史长度（不重刷）；失败 Turn 保留 Timeline 且 Chat 可继续；内部命令与空行不产生 Timeline 行；正常结束与被取消后订阅计数归零且无遗留 Timeline Task；整轮 Timeline 输出不含 Prompt marker、文件内容与请求结构；`--no-timeline` 的解析默认值；
- 跨进程 Stream 锁：两个独立 Python 进程并发追加、`expected_seq` 竞争、跨进程尾部半行修复、持锁期间阻塞、崩溃后可再取锁、异常路径解锁；
- EventStore 取消语义：等锁期间取消 `append`/`head`/`read` 时 Worker 线程先收敛再抛 `CancelledError`、被取消的 append 绝不落盘、临界区内取消按原子完成收尾、连续多次取消也无法打断收敛；
- Session/Surface/Compaction/Invariant；
- AgentLoop 端到端工具循环和 Verification；
- Tool Schema、Policy、Middleware、失败、超时和并发 Barrier；
- Workspace 越界与精确 Patch；
- 取消、子进程收敛和崩溃恢复；
- 取消时的资源收敛：Verifier 与 Shell 子进程在调用方返回前必定已退出（用子进程持有的 OS 锁判定存活，不靠等待猜测）、超时清理过程中再次取消仍不放行、进入收敛后逐次取消调用方始终不返回、`sanitized_environment()` 仍能启动 Python 子进程、OpenAI-Compatible Worker 在本地 gated HTTP Server 下先收敛再抛 `CancelledError`；
- 输出所有权与本地资源：超时结果必须包含子进程超时前已 flush 的 stdout/stderr（用 marker 文件证明输出动作确已完成）、超时 summary 经 `DefaultContinuationRuntime.decide()` 注入后模型确实能看到这两段输出、summary 的尾部界限与普通结果一致、独立解释器跑完一次超时后事件循环关闭时 stderr 无 `Event loop is closed`/`unclosed transport`/`Exception ignored` 且无遗留 Task、测试用的 PID 清理只停自己记录的进程；
- 两类超时的边界（经真实 `ToolRuntime.execute_batch()`，不是直接调用 `ShellTool.execute()`）：Tool 内部超时时 `ToolRunResult`、`effect/outcome`、`tool/result` 三处都保留 stdout/stderr 且不误报 Runtime 预算时长；Runtime 预算先到期时仍走通用超时语义并完成子进程收敛；
- 子进程中文输出：Python 子进程的原始字节可严格按 UTF-8 解码并与原文完全一致（不使用 `errors="replace"` 掩盖）；
- 内置默认 Scripted Provider 可连续应答多轮，显式 `--script` 仍在耗尽时报错；
- Model Attempt 恢复：崩后仅有 Start、仅有 Chunk、已有匹配 `assistant/message`、跨 Step 与跨 Turn 消息不算证据、错作用域消息在前正确消息在后、Start 之前的消息不算证据、Chunk 按作用域与 seq 计数、`attempt_id` 为 `None`/数字/纯空白时跳过、重复 recover 幂等、旧版本已闭合 Step/Turn 的 Append-only 修复、多 Attempt 按序收敛；
- Model Attempt 不变量：End 无 Start、重复 Start/End、payload 作用域不符、无法使用的 `attempt_id`（`None`/数字/布尔/空串/空白）、Start 不在真正开放的 Turn/Step 内、普通 End 迟于 Step 关闭、缺 `causation_id` 的迟到 `recovered` End、同 Step 双开、已闭合 Step 缺 End，以及正常配对、运行中 Attempt 与合法 Append-only 修复不误报；
- Scripted/OpenAI-Compatible Provider；
- `.env` 解析、优先级、秘密不打印和测试隔离；
- `traceh chat`：参数互斥校验与 `.env` 继承、单 Session 连续两轮、第二轮 Request 能看到第一轮 Surface、已有 Session 先恢复且不自动建 Turn、干净 Session 不写 `runtime/recovered`、内部命令与空行不产生 Turn、含斜杠的自然语言不被误判、Turn 失败后可继续、EOF 与 dispose、运行中 Turn 被中断后收敛、中文往返、BOM 剥离、U+FFFD 拒绝、终端编码降级；
- Kernel Scope、Activation、Hooks、Lifespan、Owned Tasks；
- Inspector、Request Reconstruction 和 Benchmark；
- 未来 Agent/Workspace Protocol 可构造性；
- **版本契约**（[`tests/test_version_contract.py`](../../tests/test_version_contract.py)）：`importlib.metadata.version("traceharness-py")` 必须等于被导入的 `traceh.__version__`；`pyproject.toml` 不得写死字面版本且必须声明 dynamic attr；`CORE_PLUGIN_IDENTITY`、`TRACEH_PLUGIN_API_VERSION`、`installed_traceh_version()` 三者一致；默认 `requires_traceh` 范围必须包含当前版本；同步与异步两个装配入口报告同一个核心版本；Composition Snapshot 里的核心版本等于该唯一来源；
- **插件发现**（`test_plugin_discovery.py`）：discovery 绝不调用 `EntryPoint.load()`；`to_dict()` 明示 manifest 未被读取；非法 Entry Point 名、缺失/非法 Distribution 元数据、不可读或非法 requirement、缺失/重复/不兼容的 `traceharness-py` 依赖各自报对应问题码；重复 Entry Point 名把**每一个**声明者都标为失败；其他 group 被忽略；元数据 Provider 抛异常时返回合成记录且不泄漏异常文本；排序确定；
- **显式启用**（`test_plugin_selection.py`、`test_cli_plugin_selection.py`）：默认不启用任何插件；`TRACEH_PLUGINS` 逗号分隔并去空白；任一 `--plugin` 整体替换环境变量；空、非法（含大写、空格、控制字符、ESC、双向覆写、超长）、重复 id 全部拒绝；被拒绝的假凭据取值零回显且不泄漏长度/前后缀；错误消息单行无控制字符；`run`/`chat`/`resume` 解析结果一致；`plugins` 与只读命令不暴露该参数；CLI 报使用错误而非 traceback；
- **Manifest 校验**（`test_plugin_manifest.py`）：非 Manifest 值、非法/不匹配/保留 `plugin_id`、非 PEP 440 版本、非法或不兼容 `requires_traceh`、非法依赖项与依赖版本、重复依赖、required 与 optional 冲突、非 tuple 依赖列表、空/未知/重复 scope、缺少 application scope、非法 trust mode、`isolated` 明确拒绝、非法与重复 `provides`；并断言**一次返回全部失败**而不是第一条；
- **激活事务**（`test_plugin_manager.py`，58 项）：只 import 已启用插件、空选择完全不碰 discovery、未安装/元数据有问题的插件不被 import；import 失败与 setup 失败都不泄漏插件异常文本；类与工厂两种 Entry Point 目标；缺 `setup` 被拒；依赖先于依赖者 setup、未启用的必需依赖失败、依赖版本不兼容失败、核心依赖按唯一版本判定、缺失可选依赖只是 notice 而已启用但不兼容是失败、依赖环在任何 setup 之前被发现、`provides` 冲突、独立插件顺序确定；Tool/Prompt/Service 真正进入既有主线且 setup 期间核心注册表看不到 staged 内容；dispose 按反向依赖顺序移除全部贡献、单个 cleanup 失败不阻止其余；setup 失败逆序回滚且什么都不发布、Owned Task 被取消；与核心 Tool/Prompt/Service 冲突各自报对应码并完整回滚；**冲突插件的 `health_check` 从未被调用**；health 返回 `False`、抛异常、零参数签名、缺省四种情况；health 在全部 setup 之后运行；配置深拷贝隔离、缺键无默认报错、依赖提供的 Service 可被 `require()`；`spawn_owned` 拒绝非协程且任务名不使用插件提供的文本；非法 Tool 名与 Prompt section id 被拒；激活只能一次、状态表正确、身份含真实版本、重复 dispose 安全；
- **取消语义**（`test_plugin_cancellation.py`，11 项，全部用显式 Event 门控而非 sleep）：setup 阻塞期间的**纯取消**抛原始 `CancelledError` 而**不是** `PluginActivationError`；取消后 Tool/Prompt/Service 全部回滚、cleanup 全部执行、Owned Task 已收敛；health check 阻塞期间取消同理；回滚期间连续取消 3–4 次都不能让调用方提前返回（每次都真正让事件循环运行并断言仍未结束）；dispose 期间重复取消同样收敛；若收敛后的 cleanup 真失败，则以脱敏 `PluginDisposeError` 报告而不是让取消遮蔽失败，且其余 Activation 继续回滚；不产生任何 never-retrieved task exception（用事件循环异常处理器捕获断言为空）；纯取消**不**被记入插件失败状态；真实 setup 失败仍报 `plugin-setup-failed`；
- **插件与 Runtime 主线**（`test_plugin_runtime.py`）：无插件时 Prompt 与 Tool 集合同步于同步装配入口；插件**已安装但未启用**时默认 Runtime 完全不变且 `setup` 从未被调用；启用后 Tool Schema 与 Prompt Section 确实进入模型可见面；模型真正调用插件 Tool，`tool/call` 与 `tool/result` 数量相等、`effect/intent` 与 `effect/outcome` 数量相等、不变量 0 项、Request 重建违规 0 项；Composition Snapshot 含真实插件身份且 `composition_from_event()` 能重建它；Session 记录外部插件身份、无插件时为空列表；插件集合相同可继续、丢插件/加插件/改版本三种情况都拒绝；v0.4 之前无该键的 Session 视为无插件可继续；畸形 metadata 被拒；保留键不可由调用方提供；Runtime dispose Drain 后才卸载插件、幂等、且先收敛 Turn 再进入 Composition cleanup；
- **插件 CLI**（`test_cli_plugins.py`，36 项）：`list`/`inspect` 的 human 与 JSON 输出、排序、空集合、退出码；10 种恶意元数据值（换行、回车、清屏与颜色 ESC、退格、响铃、双向覆写、行/段分隔符、超长）断言输出严格单行、无 ESC、无 `Cf` 残留、长度有界；`list`/`inspect` 绝不 import 插件；`doctor` 完成 setup 与 health 后立即 dispose、默认覆盖全部已发现插件、失败时退出码 7 且不泄漏插件异常文本、报告未安装插件、报告可选依赖 notice、human 输出同样安全、使用一次性注册表因此不污染真实 Runtime；断言 `llm_used` 与 `session_created` 均为 false；
- **只读 CLI 命令**（`test_cli_read_only_commands.py`）：`sessions`/`inspect`/`inspect --html`/`replay`/`recover`/`compact` 经 `main()` 真实执行并断言不变量与重建违规为 0；同时钉住它们不接受 `--plugin`。这组用例是本轮补上的覆盖缺口——一个被漏掉的 import 让这些命令全部无法运行，而当时没有任何测试会发现；
- **真实打包验收**（`test_plugin_wheel_e2e.py`，标记 `slow`，见 15.4）；
- **Owned Task 异常所有权**（[`tests/test_owned_task_ownership.py`](../../tests/test_owned_task_ownership.py)，13 项，全部安装真实事件循环 exception handler 并强制 `gc.collect()`，而不是读 stderr）：任务在 dispose **之前**自行抛错并完成时，dispose 前后都不得出现 `never retrieved`；关闭时仍在运行的任务由 `gather` 覆盖；**成功完成**与**被取消**的任务都不被误报；四种结局（成功、取消、自行失败、关闭期间失败）都不产生 `never retrieved`；取回即止——异常对象**不保留**：所有者身上没有 `failures` 属性，一百次失败后所有者状态不增长（钉住最小语义与“所有权而非监督”这条边界）；失败的后台任务**不会**让 `cancel_and_wait()` 抛错、不会阻止后续 spawn；`active_count` 忽略已完成任务；关闭后 spawn 被拒且不留下未 await 的协程告警；
- **Runtime 关闭收敛**（[`tests/test_runtime_dispose.py`](../../tests/test_runtime_dispose.py)，11 项，活跃 Turn 与插件 cleanup 全部用 `asyncio.Event` 门控，不用 sleep 猜时序）：核心用例使用**确定性取消门闩**——`GatedCancellationProvider` 在收到 shutdown 的取消后点亮 `cancellation_entered` 并继续停驻、吸收第二、三次取消；测试**等待该门闩**之后才取消 `dispose()`，release 之前断言 dispose 未完成、插件 cleanup 未运行、Turn 未结束，release 之后才允许收敛并重新抛出原始 `CancelledError`（反向验证：改回内联写法，此用例报 `plugins were stranded by the cancellation`）。每次显式 `cancel()` 后的单个 `sleep(0)` 只负责投递已请求的取消信号，不是到达缺陷窗口的证据——窗口证据全部来自 Event；回滚期间连续 3 次取消都不能让调用方提前返回；被取消的 dispose 之后再次 dispose 复用同一次关闭而不是重跑；活跃 Turn 必定先于插件 cleanup 收敛；`dispose()` 一开始就拒绝新 Turn；重复与并发 dispose 只执行一次关闭；**关闭失败时后续 dispose 再次抛出同一异常**而不是伪装成功；无插件路径同样幂等且能收敛运行中的 Turn；普通 run 的行为不变；
- **Session 插件身份**（[`tests/test_session_plugin_identity.py`](../../tests/test_session_plugin_identity.py)，36 项，全部用真实 Session 与真实事件日志）：6 组 PEP 440 等价版本（`1.0`↔`1.0.0`、`1.0`↔`1.0.0.0`、`2.0`↔`2.0.0` 等）创建的 Session 可以继续并真正跑完一个 Turn；6 组真实差异（`1.0` vs `1.0.1`、`1.1`、`2.0`、`1.0.post1` 等）仍然被拒绝；不匹配消息保留 Session 记录的原始版本文本；4 种无法解析的版本仍报 `malformed`；重复 id 仍被拒；保留键在 6 种取值（`[]`、`None`、完全相同的列表、别的插件、字符串、字典）下**一律按出现即拒绝**，无插件 Runtime 上同样如此；被拒绝时 Session 根本没有被创建；其余用户 metadata（含嵌套结构）照常保存，无论有没有插件；**缺键与显式 `null` 是两种事实**：经 `SessionService` 直接写入、`traceh_plugins` 键真正缺席的 Session 按 v0.3 无插件 Session 继续并跑完 Turn；同一路径写入显式 `None` 的 Session 在 `verify_session_plugins` 与 `run_existing` 上都报 `malformed`，插件 Runtime 上同样如此；`[]` 是 Runtime 自己写的合法无插件记录，仍然通过；
- **`traceh run` 的 dispose 保护与 `.env` 隔离**（[`tests/test_cli_run_dispose.py`](../../tests/test_cli_run_dispose.py)，14 项）：Workspace 缺失、Store 失败、保留键被拒三种 `create_session` 失败路径都断言 `dispose()` **确实被调用**（用包装真实 Runtime 的 Spy，不是断言副作用）；失败时不会打印一个并不存在的 `session_id=`；正常完成与 Turn 抛异常两条既有路径继续 dispose；正常 run 的输出行、`session_id` 先于结果的顺序、退出码 0 与 `max_steps_exceeded` 的退出码 2 全部不变。**这组用例真正不读取开发者的 `.env`**：autouse fixture 把工作目录移到 `tmp_path`——`--env-file` 默认是相对路径 `.env`，把仓库根目录从可达范围里移走比让每个测试记得传参更可靠，也不依赖 fake `_runtime` 挡网络；`drive_run` 强制使用测试专属的不存在 env-file 路径，并**断言 `EnvLoadReport.loaded is False`**；另有 5 项专门验证隔离本身：仓库 `.env` 不在工作目录、默认参数解析结果为 `loaded=False`、Provider/Base URL/Model/Key 全是内置默认、真实 `_runtime()` 不经过任何 monkeypatch 就能构建出 Scripted Provider、而测试目录内的显式 env-file 仍然生效（隔离没有弄坏功能）。反向验证：去掉 chdir 后 4 项隔离测试立即变红，且失败内容正是仓库 `.env` 提供的 `openai-compatible`。

跨进程测试通过 `tests/cross_process_worker.py` 启动真实独立解释器，用握手文件而不是长 sleep 同步；它们在临界区内制造确定性重叠窗口，因此去掉 OS 锁后会稳定失败。该 Worker 文件不以 `test_` 开头，pytest 不会收集它。同理，`tests/plugin_fixtures.py` 与 `tests/plugin_e2e_driver.py` 也不以 `test_` 开头。

### 15.4 真实 Wheel / Entry Point 验收

插件套件其余部分都注入假的 `entry_points` provider。这足以确定性地驱动 Manager，但它**证明不了打包**：无法说明一个声明 `traceharness-py>=0.4,<1.0` 的独立 Distribution 真的能与本次构建共存，也无法说明 `importlib.metadata` 找得到它。

因此 [`tests/test_plugin_wheel_e2e.py`](../../tests/test_plugin_wheel_e2e.py) 做真实验收：

1. 用 `pip wheel --no-deps` 构建 TraceHarness Wheel 与示例插件 Wheel；
2. 用 `pip download` 把 `packaging` 放进同一个 wheelhouse——它现在是真实运行时依赖，离线安装必须能找到它；
3. `python -m venv` 创建全新虚拟环境；
4. `pip install --no-index --find-links <wheelhouse>` **离线**安装三者；
5. 用该 venv 的解释器运行 [`tests/plugin_e2e_driver.py`](../../tests/plugin_e2e_driver.py)，它只能 import 这些 Wheel 装出来的东西。

Driver 断言的事实（不需要任何 API Key，不调用真实模型，由 Scripted Provider 驱动）：真实 `importlib.metadata` Entry Point 被发现且值正确；discovery 无问题码；`plugins list/inspect/doctor` 全部返回 0；未启用插件时默认 Runtime 的 Tool 集合与 Prompt 完全不变；启用后模型可见 Tool Schema 与 Prompt Section；模型真正调用插件 Tool；`tool/call`↔`tool/result` 与 `effect/intent`↔`effect/outcome` 严格配对；Composition Snapshot 含 `traceh.core` 与插件真实身份；Session metadata 记录插件身份；不变量违规 0 项；Request 重建违规 0 项；dispose 后插件 Tool 消失。

获取 `packaging` Wheel 这一步可能需要网络或已预热的 pip 缓存；不可用时该用例明确 skip 并说明原因，**安装本身**始终是 `--no-index` 的离线安装。

### 15.2 CI

GitHub Actions 在 push 和 pull request 上运行两个 Job：

| Job | 平台 | Python | 步骤 |
|---|---|---|---|
| `test` | `ubuntu-latest` | 3.12、3.13 矩阵 | 可编辑安装、compileall、pytest、`traceh doctor` |
| `test-windows` | `windows-latest` | 3.12 | 同上 |

Windows Job 是为跨进程文件锁新增的最小覆盖：该平台走 `msvcrt` 而不是 `fcntl`，必须在真实 Windows Runner 上执行。

`tests/` 不是 Python package；共享测试夹具（例如 `plugin_fixtures.py`）按 pytest 加入测试目录后的顶层模块导入，不使用 `tests.plugin_fixtures`。后者可能在开发机上偶然可用，却会在干净检出中被环境里的同名 `tests` package 遮蔽并导致收集失败。CI 与本地发布门禁都必须能在只含 Git 跟踪文件的干净检出中完成收集和全量测试。

### 15.3 发布快照与当前测试的区别

`VALIDATION.md` 保存最初 v0.3 发布时的 24 项测试、覆盖率、Demo、Wheel 和干净安装验证。历史 v0.4 基线为 910 项（909 通过、1 项按平台跳过）；Stage A 后为 960/959/1，Stage B 为 980/979/1，Stage C 为 999/998/1，D0 为 1003/1002/1，D1 为 1029/1028/1，D2 为 1053/1052/1；D3 当前真实收集 1088 项，完整门禁为 1087 passed、1 skipped。不要把发布时点数字误认为当前测试总数，也不要未经重新运行就改写历史验证结果。

## 16. 已知限制与风险

| 领域 | 当前限制/风险 | 完善方向 |
|---|---|---|
| Stream 锁边界 | 已有 `fcntl`/`msvcrt` 跨进程锁，但只是同机 Advisory Lock：绕过 `JsonlEventStore` 直接写文件不受约束，网络文件系统行为未验证 | 需要更强隔离时改用 SQLite 或独立 Store |
| Session 级并发 | 事件写入跨进程安全，但“同一 Session 只跑一个 Turn”仍只在单进程内强制 | 跨进程 Session Lease 或 Runtime 级占用标记 |
| 子进程输出磁盘占用 | 捕获用的临时文件当前没有大小上限，失控命令可以写满临时目录；上层 Tool Result 截断只影响读出后的文本，不会减少读取前已经占用的磁盘 | 需要时在捕获层增加大小上限并在超限时截断 |
| 临时文件删除延后 | 孙进程继承捕获句柄时，Windows 会把临时文件的删除推迟到最后一个句柄关闭 | 与“不管理孙进程”是同一条边界，必要时由外部清理 |
| Event payload 可变性 | Store 历史已由脱离副本保护（6.4），但 `EventEnvelope.data` 本身仍是普通可变 JSON 图：拿到副本的代码可以随意修改自己那一份，语言层面不阻止。契约由具体边界承担而非自动生效：同一个 Envelope 被交给两个消费者时，框架不会自动隔离它们 | 需要更强保证时才考虑不可变 JSON 容器类型，代价是公共 API 与全部 `event.data` 读取点都要改。已有的扇出（`SessionEventFeed`）按契约为每个 Subscriber 单独 detach；任何新增分发点必须同样处理 |
| Event 复制成本 | 复制只在 Event API 边界发生，单次规模等于一个事件 payload；一次 `read()` 的总成本与它解析并返回的 payload 总量相关。`InMemoryEventStore.read()` 为每个返回事件重建一次 JSON 图；JSONL 侧则由共享序列化边界重建（`from_dict()` 读、`to_dict()` 写），且 `read()` 的 `from_seq` 是过滤而非定位，仍解析整条 Stream | 属于正确性的必要代价；刻意不加缓存，因为缓存会重新引入共享引用。全量扫描是 JSONL 既有边界（见本表 JSONL 扩展性一行），需要时应换 Store 或加 Checkpoint，而不是回退到共享引用 |
| 取消的提交点边界 | 取消恰好落在写入过程中时，调用方收到 `CancelledError` 但事件已提交（6.6）；无自动重试，因此不是 at-least-once | 调用方重新读取 Stream，按 `event_id`/correlation/业务身份判断是否已落盘 |
| Model Attempt 证据上限 | 未闭合 Attempt 已按证据补 End（11.1），但 `unknown_after_crash` 只说明“无法证明”，且丢失的 `usage`/`finish_reason` 无法找回 | 需要精确计费时在 Provider 边界先落盘用量 |
| CLI 体验 | `chat` 已支持会话内连续输入、实时 Timeline（13.6）、Activity Heartbeat（13.7）与可收敛的 Ctrl+C（13.8），但仍无 token 流式输出、Spinner、颜色、执行前审批，也不能在 Turn 运行期间输入；`run`/`resume` 尚未接 Timeline | 在不破坏 Runtime 边界下扩展 Surface/UI 层，复用同一 Feed 与 Formatter |
| 变量名校验只看形状 | `--api-key-env` 校验的是"是不是可用的变量名"，无法识别一个恰好合法的标识符其实是被粘错位置的 Key（`ghp_...`、`AKIA...` 会被接受，并出现在恢复命令里）。拒绝所有形似凭据的标识符会误伤 `GH_TOKEN` 这类正常名字 | 这是形状校验而非意图识别；错误路径已做到零回显，合法路径无法再进一步 |
| Base URL 检查的能力边界 | 只做结构检查（userinfo/query/fragment）与解析失败保护，不是通用秘密探测器：无法判断一个普通路径段本身是不是凭据 | 需要更强保证时应由用户自己保管配置，而不是让显示层猜 |
| 恢复命令不是配置快照 | `--verify-command` 一律不回显（任意 Shell 文本，无法证明其中没有凭据）；命名插件 Verifier 会作为安全 token 保留，但 Event Log 只持久化验证结果、不把其选择名当作 Session 兼容身份；Base URL 也只按结构规则（userinfo/query/fragment）withhold，不是通用秘密探测器 | 需要完整重建时由用户自己保管原始配置或 env-file，不要手工删改恢复命令中的 `--plugin-verifier`；不要把"秘密永不打印"写成绝对承诺 |
| Scripted 游标不持久化 | 恢复命令携带 `--script` 绝对路径，但 Scripted Provider 的响应游标不跨进程保存，重新加载会从第一条响应开始 | 需要精确续跑脚本时应把游标也落盘，属于 Provider 层设计 |
| Verifier 无等待提示 | Heartbeat 只覆盖 Model Attempt 与已准入 Tool；`CommandVerifier` 没有“开始”事件（协议只有结束时的 `verification/result`），因此慢验证命令在屏幕上仍然完全安静 | 需要覆盖就要新增 `verification/start` 一类协议事件，属于事件协议变更，另行设计；本轮明确拒绝用 UI 侧推测去猜它是否启动 |
| 并发 Tool 完成时刻不可观察 | `ToolRuntime` 对 parallel-safe 组用 `gather`，整组完成才追加各条 `tool/result`。因此等待提示只能说“尚未报告完成”，完成耗时也是 `tool/admitted` → 持久化 `tool/result`，对组内工具会长于其自身执行时间 | 若要精确到单个工具，需要改 `ToolRuntime` 的事件排序；本轮不做，因为那会动到工具执行的持久化语义 |
| Heartbeat 是瞬时显示 | 它不是事件，不落盘、不可回查：日志里永远看不到"当时等了多久"。完成耗时只出现在屏幕上，不进入 payload | 需要可审计的时延就应在 Provider/Tool 边界落盘用量与耗时，而不是把 UI 状态写进日志 |
| 硬中断仍无收敛 | Ctrl+Break、关闭控制台或被操作系统终止时没有任何 Python 代码运行，13.8 的收敛与提示都不发生 | 依赖启动时即打印的恢复信息与崩溃恢复；不承诺统一退出码 |
| Timeline 注入的残余边界 | 所有 payload 文本已统一 `sanitize()`：控制/格式字符被中和、严格一行、长度有界，因此无法伪造第二行、无法发出终端控制序列。但形似结构标记的**惰性文本**仍会留在该行内（例如 `tool_name` 里的 `[event 999]`） | 保证是“不产生第二行、行首为真实事件号”；如需更强，代价是为每个字段加转义或引号，可读性下降 |
| Feed 只在同进程可见 | `SessionEventFeed` 是进程内通道：另一个进程直接写同一份 JSONL 时本进程收不到，因此没有跨进程实时观察能力 | 需要时才考虑文件 Tail 或独立通知机制；本版本刻意不做 |
| Feed 无背压/溢出策略 | 队列无界：慢订阅者不会拖慢 Runtime，但被遗弃的订阅者会占内存，上限是该 Session 的事件量。Chat 在每条退出路径关闭订阅，因此随包消费者不泄漏 | 若改为有界队列，必须先定义明确 overflow 语义；静默丢事件会让 Timeline 说谎 |
| Feed 可丢失，不是证据 | 内层 append 已正常返回、尚未发布时进程崩溃，Feed 通知会漏；Event Log 能否在操作系统崩溃后保留，仍由该次请求的 `Durability` 决定。Feed 不重放历史、不持久化 Offset | 恢复与审计继续只读 `EventStore`；需要历史用 `read()` |
| 中断退出码 | 退出码由宿主 Shell 和 Python 信号处理决定；硬中断（Ctrl+Break/关闭控制台）实测为 `3221225786`，不会运行收敛代码 | 依赖启动时打印的 session_id 与崩溃恢复，不承诺统一退出码 |
| 模型调用中断 | 取消 OpenAI-Compatible 请求时会等待 HTTP Worker 收敛，最坏等到 `timeout_seconds` | 需要立即中止时改用可中断的 HTTP 客户端 |
| Shell 安全 | Policy 是黑名单 Guardrail，不是沙箱 | 容器/远程 Sandbox、能力审批 |
| OpenAI Provider | 非流式、无重试/Fallback/限流 | 在 LlmRuntime/Provider 边界扩展 |
| JSONL 扩展性 | read 仍全量扫描；非分布式 | Checkpoint、SQLite 或其他 EventStore |
| Patch 能力 | 精确文本替换，不解析 unified diff | 增加独立工具实现，不改变 Tool Runtime |
| Benchmark | 仅一个确定性简单案例 | 增加真实 Provider、失败恢复、复杂仓库案例 |
| 自动压缩 | 只有手动 Replacement | 未来 Context/Compaction Plugin |
| 插件切换的代码边界 | Stage C 的 `traceh chat` 已有 `/plugins`、`/plugins reload`、`/plugins use ...` 和 `--none`；它只重做当前进程可发现的 Entry Point 激活，不重新导入已在 `sys.modules` 中的模块，也不安装/卸载 Wheel | 后续若需要动态安装、module reload 或文件监听，必须另设安全与所有权设计 |
| 插件不是沙箱 | v0.4 只有 trusted、进程内插件。`isolated` 可声明但被明确拒绝。一个被启用的插件与 Harness 同进程、同权限运行，能做任何 Python 能做的事 | 真正的隔离需要进程边界、每次 context 调用的序列化契约与子进程崩溃失败模型；在此之前，“启用插件”等于“信任其作者” |
| 插件贡献面仍有生命周期边界 | D3 已能提供 Provider、Policy、Middleware 和命名 Verifier，但全部是 application setup、trusted、进程内且 Generation-owned；EventStore 仍不能由插件提供 | EventStore 必须先有独立于 Step Generation 的进程级固定插件所有权，不能把账本跟着 `/plugins` 切换 |
| Scope Overlay 仍不是 scoped plugin activation | D1/D2 已解析程序化 Service、Tool、Prompt、Policy binding，并把模型可见结果纳入既有 Generation/Snapshot；D3 的插件 Policy 仍来自 application setup。插件 Manifest 仍要求 application scope，Workspace/Preset/Agent 不能各自运行 setup。当前单 Runtime 只有一条 Agent 层装配，不等于已有 AgentSupervisor | 子层插件生命周期与多 Agent Scope 所有权留给后续明确设计；不得把程序化 binding 或 application 插件贡献误称为插件已能自行选择 scope |
| Session 插件身份与迁移 | 当前身份由共享事件解析器按 `session/created`、合法 `composition/snapshot` 和 `composition/migration-authorized` 顺序重建；身份变化必须在全局 Gate 内以 `source_seq`/Session head CAS 追加授权。授权已落盘而 publish 失败时 fail-closed；不会自动迁移所有 Session。版本按 PEP 440 等价判定 | 仍没有 Session 自动迁移、批量迁移或跨进程迁移协调；每次授权仍由用户命令显式触发，Generation identity 不持久化 |
| 后台任务失败不被上报为运行结果 | `OwnedTaskSet` 取回插件后台任务的异常（因此不会再出现 `Task exception was never retrieved`）但**不保留**它——早期版本把每个失败对象存进一个无界列表，而该列表没有任何主线消费者；每个异常都持有 traceback，进而持有每一帧的局部变量，为无人读的数据保留不受信任的插件状态是一种内存泄漏兼泄漏面。它也不重启任务、不把失败升级成 Runtime 故障。一个插件的后台任务静默死掉时，Turn 仍会照常完成 | 需要观测语义时必须先有真实主线消费者，且采用有界、结构化、脱敏的记录，不能保留原始异常与 traceback；需要监督语义（重启、退避、上报）时另行设计并明确授权，见 19.12 |
| 依赖 `packaging` | 运行时不再只依赖标准库；离线安装必须自行准备该 Wheel | 这是守信任边界的必要代价，见 1.1 |
| API 稳定性 | Alpha，协议可能演进 | v1.0 前建立兼容策略和 Upcaster |

## 17. 变更影响矩阵

| 修改区域 | 必查代码/测试 | 必须同步的本文章节 |
|---|---|---|
| Agent Loop / Continuation | `runtime/agent_loop.py`、`continuation.py`、E2E/取消测试 | 4、5、10、11、15、16 |
| Event/Store/Session | `api/events.py`、`session/*`、event/invariant/recovery 测试 | 5、6、7、11、12、15、16 |
| Event 所有权 / Store 返回值 | `api/events.py`（`detach_event`、`to_dict`、`from_dict`、`materialize`）、`session/event_store.py`、`session/jsonl.py`、`tests/test_event_store_contract.py` | 6.1、6.4、15、16 |
| Event Feed / 发布顺序 | `session/event_feed.py`、`runtime/agent_runtime.py` 的装配、`tests/test_event_feed.py` | 4、6.1、6.4、6.7、15、16 |
| Timeline / Chat 输出 | `cli/timeline.py`、`cli/chat.py`、`cli/main.py`、`tests/test_cli_timeline.py`、`tests/test_cli_chat.py`、README | 1、3、13.4、13.6、15、16 |
| Heartbeat / 等待提示 | `cli/activity.py`、`cli/chat.py` 的显示装配、`cli/main.py`、`tests/test_cli_activity.py` | 1、3、13.6、13.7、15、16 |
| Ctrl+C / 恢复信息 | `cli/chat.py`（`_interrupt_turn`、`_write_resume_block`、空闲中断）、`runtime/agent_runtime.py` 的 `cancel()`、取消与活动测试 | 5.3、11、13.8、15、16 |
| Provider/Request | `api/llm.py`、`llm/*`、`request_builder.py` | 7、8、13、15、16 |
| Tool/Policy/Middleware | `api/tools.py`、`tools/*` | 6、9、11、15、16 |
| CLI/.env | `cli/*`、`.env.example`、README、CLI tests | 1、3、13、15 |
| Verifier/Eval | `verification.py`、`evaluation/*` | 10、12、15、16 |
| 插件发现/启用/激活 | `plugins/*`、`api/plugins.py`、`api/prompts.py`、`kernel/activation.py`、`kernel/tasks.py`、`runtime/agent_runtime.py`、`tests/test_plugin_*.py` | 1、2、3、4、7.1、13、14、15、16、19 |
| Runtime 关闭 / dispose | `runtime/agent_runtime.py`（`_shutdown`、`dispose`）、`plugins/manager.py` 的 `dispose`、`tests/test_runtime_dispose.py` | 5.3、5.5、15、16、19.8 |
| 插件组合控制面 / Session 迁移 | `runtime/plugin_composition.py`、`runtime/agent_runtime.py` 门面、`tests/test_plugin_composition_coordinator.py`、Stage B/C 控制面测试 | 4、5.3、14、15、16、19.7–19.9 |
| Composition Generation / Lease / Drain | `runtime/composition_runtime.py`、`runtime/agent_loop.py`（仅 lease 调用）、`runtime/agent_runtime.py` 工厂与 dispose、`tests/test_composition_generations.py`、插件 Runtime 顺序测试 | 4、5.5、7.1、7.3、14、15、16、19.7、19.8 |
| Service Scope / Overlay | `api/services.py`、`kernel/registry.py`、`kernel/scope.py`、`plugins/manager.py` 的候选 Scope、默认 Runtime 工厂、`tests/test_scope_overlays.py` | 1、2、3、4、14、15、16、19.7、19.13 |
| Tool / Prompt / Policy Overlay | `kernel/composition_overlays.py`、`runtime/prompt.py`、`plugins/manager.py` 的候选解析、默认 Runtime 工厂、`tests/test_composition_scope_overlays.py` | 1、2、3、4、7.1、14、15、16、19.7、19.14 |
| Owned Task 所有权 | `kernel/tasks.py`、`kernel/activation.py`、`tests/test_owned_task_ownership.py` | 14、15、16、19.12 |
| Session 插件身份比较 | `session/plugin_identity.py`（持久化身份重建与 PEP 440 比较）、`runtime/plugin_composition.py`（校验、迁移和 CAS）、`runtime/agent_runtime.py`（`create_session` 与公开门面）、`tests/test_session_plugin_identity.py`、Stage C/D0 控制面测试 | 15、16、19.9 |
| CLI 命令的资源保护 | `cli/main.py` 的各 handler、`tests/test_cli_run_dispose.py`、`tests/test_cli_read_only_commands.py` | 13.1、15、16 |
| 插件 CLI | `cli/plugins.py`、`cli/main.py`、`tests/test_cli_plugins.py`、`tests/test_cli_plugin_selection.py`、README、`docs/plugins.md` | 13.1、15、19.10 |
| 版本 | `version.py`、`pyproject.toml`、`tests/test_version_contract.py`、CHANGELOG | 1、1.2、15、19 |
| 运行时依赖 | `pyproject.toml`、README、打包验收 | 1、1.1、15.4、16 |
| Composition 插件身份 | `composition_runtime.py`、`request_builder.py`、`session/service.py`、插件运行时测试 | 7.1、7.3、12、15、19.9 |
| Multi-Agent/Workspace Protocol | `api/agents.py`、`api/workspaces.py`、`kernel/*` | 2、3、4、14、15、16 |
| 开发流程/目录 | `AGENTS.md`、`CLAUDE.md`、CI、pyproject | 0、1、3、15、18 |

## 18. 当前维护流程

```mermaid
flowchart LR
    START["开始开发"] --> READ["完整读取正式版与通俗版"]
    READ --> CHECK["检查 Git 与真实代码"]
    CHECK --> CHANGE["实现最小完整修改"]
    CHANGE --> VERIFY["编译、测试、定向验证"]
    VERIFY --> FORMAL["先更新正式版当前事实"]
    FORMAL --> PLAIN["逐章同步通俗版"]
    PLAIN --> DOCQA["链接、Mermaid、秘密和一致性检查"]
    DOCQA --> GIT["代码、测试、两版文档同一提交"]
```

维护完成标准：

- 当前代码与测试通过；
- 本文所有受影响事实已更新，过时事实已删除；
- 通俗版相同编号章节已同步，且没有引入额外能力；
- README/CHANGELOG/ADR/专题文档按各自职责更新；
- 示例只作为示例，秘密未进入 Git；
- 最终交付说明变更、验证、文档同步和剩余边界。

## 19. 插件系统（v0.4 / Stage A–D3）

作者与运维契约见 [`docs/plugins.md`](../plugins.md)，v0.4 事务原因见 [ADR-0007](../adr/0007-transactional-plugin-activation.md)，Stage B 所有权决定见 [ADR-0009](../adr/0009-generation-owned-plugin-activation-set.md)，Stage C Session 迁移决定见 [ADR-0010](../adr/0010-session-plugin-composition-migration.md)，D0 控制面所有权拆分见 [ADR-0011](../adr/0011-plugin-composition-control-plane-coordinator.md)。本节记录工程事实。

### 19.1 模块职责

| 模块 | 职责 |
|---|---|
| [`plugins/discovery.py`](../../src/traceh/plugins/discovery.py) | 读取 `traceh.plugins` Entry Point 组的 Distribution 元数据，**不 import 插件** |
| [`plugins/selection.py`](../../src/traceh/plugins/selection.py) | 解析并校验显式启用列表，发生在发现与 import 之前 |
| [`plugins/errors.py`](../../src/traceh/plugins/errors.py) | 结构化 `PluginFailure` 与异常层次 |
| [`plugins/manager.py`](../../src/traceh/plugins/manager.py) | Manifest 校验、依赖解析、事务式激活，以及 `PluginActivationSet` / `PluginGenerationBuilder` 的候选所有权转交 |
| [`cli/plugins.py`](../../src/traceh/cli/plugins.py) | `list`/`inspect`/`doctor` 的安全投影 |
| [`api/plugins.py`](../../src/traceh/api/plugins.py) | `PluginManifest`、`PluginContext`、`Plugin`、`CORE_PLUGIN_IDENTITY` |
| [`api/prompts.py`](../../src/traceh/api/prompts.py) | `PromptSection`，从 `runtime/prompt.py` 移出，使 SDK 不必导入装配层 |

### 19.2 Discovery 只读元数据

`PluginDiscovery.discover()` 通过 `importlib.metadata` 读取 Entry Point 与 Distribution 元数据，**从不调用 `EntryPoint.load()`**。这条分离是一个安全性质而不是性能优化：它使“列出这台机器上装了哪些插件”本身不成为一次代码执行。

每条记录报告的问题码：`invalid-entry-point-name`、`distribution-metadata-missing`、`distribution-version-invalid`、`distribution-requirements-missing`、`distribution-requirement-invalid`、`traceh-dependency-missing`、`traceh-dependency-duplicate`、`traceh-distribution-incompatible`、`duplicate-entry-point`、`entry-point-metadata-error`。

两个刻意的选择：

- 同一个 Entry Point 名被两个 Distribution 声明时，**所有声明者都被标为失败**，而不是按安装顺序静默选一个；
- 全局元数据 Provider 本身抛异常时，返回一条合成记录，绝不把它的异常文本或 traceback 交给 CLI。

比较用的“已安装 TraceHarness 版本”取自 `traceh.version.__version__` 而不是 `importlib.metadata`：真正要承载插件的是被 import 的那份代码，而 `pyproject.toml` 又从同一属性派生版本，因此二者是构造性一致而非巧合一致。

### 19.3 显式启用

安装**不等于**启用。启用来自 `--plugin`（可重复）或 `TRACEH_PLUGINS`（逗号分隔）。命令行上任何一次 `--plugin` 都会**整体替换**环境变量值，而不是追加，因此一条命令行总能完全决定本次运行的插件集合。`run`、`chat`、`resume` 在 `_configure_from_environment()` 中共用同一次解析。

校验发生在发现与 import 之前，因此非法 id 永远到不了第三方代码。被拒绝的取值**完全不回显**：这个设置最常见的写错方式是把 Token 粘到了插件 id 的位置。

### 19.4 激活事务的四个阶段

```mermaid
flowchart TD
    SEL["显式选择"] --> VAL["校验 id"]
    VAL --> DISC["元数据发现（不 import）"]
    DISC --> LOAD["只 import 已启用插件"]
    LOAD --> MAN["逐字段校验 Manifest"]
    MAN --> DEP["依赖解析 + 确定性拓扑排序"]
    DEP --> P1["阶段 1：setup() 写入私有 staged registries"]
    P1 --> P2["阶段 2：完整冲突检查"]
    P2 --> P3["阶段 3：health check"]
    P3 --> P4["阶段 4：原子发布进既有主线"]
    P4 --> OK["装配完成；身份写入 Composition"]
    P1 -. "失败或取消" .-> RB["逆序回滚全部 Activation"]
    P2 -. "冲突" .-> RB
    P3 -. "失败或取消" .-> RB
    P4 -. "失败" .-> RB
    RB --> ERR["PluginActivationError / PluginDisposeError / 原始 CancelledError"]
```

**为什么冲突检查必须早于 health check。** 一个 Tool 与内置 Tool 同名的插件无论 health check 说什么都会被拒绝。先跑 health check 只是给一段已知注定失败的第三方代码额外一次执行、占用时间或访问网络的机会；而冲突完全由 Manager 已经持有的数据判定，先问插件不会得到任何新信息。候选实现的顺序是反的，本轮已修正，并由 `test_conflicting_plugin_health_check_is_never_called` 钉住（反向验证：把顺序换回去，该用例立即失败）。

**为什么 setup 必须写进私有 staged registries。** 一个在 `setup()` 中途失败的插件，此前已经注册过的内容如果直接进了实时注册表，失败的激活就会留下一个任何配置都无法描述的状态。私有暂存使“全部成功之前什么都不可见”成为结构性质。插件之间的同名冲突因此在 setup 阶段就由共享的 staged registry 抛出，表现为后一个插件 `plugin-setup-failed`。

**为什么发布必须原子。** 一个 Step 冻结一份 Composition。若逐个发布，某个 Step 可能由半发布的插件集合组成，而 Composition Snapshot 会描述一个从未连贯存在过的配置。

### 19.5 取消不是失败

候选实现用 `except BaseException` 捕获 `CancelledError` 并重写成 `PluginActivationError`/`plugin-setup-failed`，于是启动期按 Ctrl+C 会被报告成“插件配置有问题”。更糟的是回滚把重复取消当作停止展开的理由，第二次 Ctrl+C 可能让尚未回滚到的 Activation 就此滞留。

现在的语义：

| 取消发生的位置 | 行为 |
|---|---|
| setup 阻塞期间 | 停止继续 setup/health/publish，逆序回滚全部 Activation，重新抛出原始 `CancelledError` |
| health check 阻塞期间 | 同上 |
| publish 期间 | 同上 |
| 回滚期间重复取消 | 被吸收；`Activation.dispose()` 已在重新抛出前收敛，因此记录取消意图并**继续展开其余 Activation** |

当 rollback 全部成功时，调用方拿到原始 `CancelledError`，并且保证：全部 staged 注册已撤销、全部 Owned Task 已取消并等待完成、全部 cleanup 已执行、状态表中**没有**把纯取消记成插件失败。若 rollback cleanup 真失败，取消不能把失败改写成成功收尾：其余 Activation 仍继续逆序回滚，最终以只含仓库固定文案的 `PluginDisposeError` 报告；原始插件异常正文与 traceback 不保留、不显示。收敛复用既有的 [`await_worker_convergence()`](../../src/traceh/concurrency.py)（见 6.6、8.3、13.8）：重复取消是意愿声明，不是逃生出口，也不是遮蔽 cleanup 失败的理由。

真实失败仍然是失败：`test_a_genuine_setup_failure_is_still_reported_as_a_failure` 防止这条修复把真错误变成静默取消。反向验证：去掉专门的 `CancelledError` 分支后，原有 10 项取消测试中有 6 项立即变红，且报出的正是 `PluginActivationError: Plugin setup failed`；本轮新增的第 11 项又证明，恢复“忽略 rollback failure”的旧逻辑时会错误地只抛 `CancelledError`。

### 19.6 依赖与 Manifest

- `requires_plugins` 的目标必须**也被显式启用**，仅安装不够，否则 `required-plugin-missing`。插件不能替运维启用它的依赖；
- `optional_plugins` 缺失是 notice；**已启用但版本不兼容**是失败，不是可以耸肩略过的缺席；
- 依赖环报 `plugin-dependency-cycle`，且此时任何插件的 `setup()` 都还没跑过；
- 两个插件声明同一个 `provides` 能力报 `provides-conflict`；
- 排序用最小堆做拓扑排序，所以同一组插件的 setup 顺序永远相同，复现是真的可复现；
- `traceh.core` 是保留 id，任何外部插件声明它都报 `plugin-id-reserved`；
- Manifest 校验**一次返回全部失败**而不是第一条，作者修一次就能看到全部问题。

### 19.7 Generation 主线、ActivationSet 与仍未实现的用户热更新

Stage A 已把 Generation-backed Composition Runtime 接入两个默认工厂；Stage B 又把 Generation-owned `PluginActivationSet` 接入无插件、启动插件和内部候选替换路径。`PluginGenerationBuilder` 为每次候选创建独立的 Tool、Prompt、Service 注册表视图，`PluginManager` 在这些私有注册表中完成 discovery、依赖排序、Manifest 校验、setup、冲突检查和 health check；成功后只把一次性 Activation 所有权转交给 ActivationSet。`activate()` 成功到 `PluginActivationSet` 构造成功属于同一个事务：receipt 或 Scope 校验在交接构造中失败时，调用方尚未拿到候选，临时 Manager 仍是唯一 cleanup owner，Builder 必须先完整 dispose 它再返回错误。候选构造或 publish 失败都会立即逆序 rollback，current Generation 不变。`AgentLoop` 继续只依赖 `CompositionRuntime.lease()`，不导入 PluginManager、Builder 或 reload service。

一个 Generation 是一组在构造时捕获的不可变运行能力引用：LLM Registry/Provider、Provider 名称、Model、Prompt sections、Tool schemas/ToolRuntime、Plugin Identity、Policy/Middleware 和模型参数一起绑定。Tool 的 name、description、input_schema、effect_kind 会进入真正只读、扁平且幂等的适配器，公开属性没有赋值或删除入口，嵌套 Schema 也被冻结，执行仍委托给捕获的 Tool；Provider、Policy 和 Middleware 的模型可见名称也由冻结适配器捕获，因此 Snapshot 不会重新读取活对象。Generation identity 的一次性发布状态与资源 cleanup ownership 是两件独立的事：Stage A 的 capability-wide `CompositionResourceOwner` 仍由显式装配使用；Stage B 的插件 cleanup 绑定到一次性 ActivationSet，而不是绑定到共享 core 能力。SessionService、EventStore、核心 Provider、内置 Tool 和基础配置是 borrowed core；插件 Activation、插件 Tool、Prompt Section、Service、Owned Task 和 cleanup callback 是 generation-owned。插件 ActivationSet 不能被两个 Generation 或两个 Runtime 接收，也不能被 PluginManager 留作第二个 cleanup owner。

候选插件身份可以随 Generation 变化，但 Generation 的 ToolRuntime 必须使用 Runtime 固定的 `SessionService`；这保证 Session Event Log 仍是唯一事实源。Stage A 没有资源级引用计数，因此能力-wide cleanup-bearing Generation 仍必须使用尚未被其他 Generation 认领的独占 raw 能力。发布在内部 `asyncio.Lock` 的线性化点完成；旧记录进入 retired，新 Lease 只能取得新记录，已有 Lease 保存旧代完整的 Provider、Prompt、ToolRuntime、Policy/Middleware、Service 和 Snapshot。旧代只有在 Lease 归零后才启动一次 ActivationSet cleanup；cleanup 先取消并等待 Owned Task，再按依赖逆序撤销 Service、Tool 和 Prompt 注册。ActivationSet 的 cleanup 失败会以有界、终端安全的结构化摘要报告，但不会跳过其他插件或其他 Generation；Runtime 标记为 poisoned 并拒绝后续 publish。`drain()` 等待所有 retired 记录的 Lease 与 cleanup 都收敛，重复取消只能在同一内部收敛任务完成后重新抛出最初的 `CancelledError`。

binding 的事务边界不是普通 `setattr()`：实现直接操作真实实例字典或类声明的 slot，并在提交后验证读取到的是同一个 binding。这样即使能力对象自定义 `__setattr__` 且静默忽略字段，也不能绕过资源所有权。提交过程保存每个组件的原始属性状态；后续组件失败时，已写入组件会精确恢复为“原本不存在”或“原本存在且值为 `None`”，不会把内部 Registry 破坏成缺少属性的半初始化对象。Runtime 构造同样先从冻结后的初始 Generation 建好兼容性 LLM/Tool/Prompt 视图，再认领 Owner；认领之后没有第二次调用 raw Prompt/Registry 的失败窗口。

Generation identity 只服务内部生命周期，`CompositionSnapshot.revision` 仍是模型可见内容 fingerprint；同内容的不同 Generation 因此可以复用相同 revision，不会把内部编号塞进 Request Fingerprint。`AgentRuntime.dispose()` 的默认顺序是活跃 Turn → Composition Drain（由各代 ActivationSet 负责插件 cleanup）→ 仍属 application-level 的构建器/发现器资源；默认路径没有 PluginManager 的第二个插件 cleanup owner，旧 v0.4 自定义装配才会在 Drain 后清理可选的 legacy PluginManager。

Stage B 提供 `AgentRuntime.replace_plugin_composition()` 这一装配层内部 API；Stage C 又提供 `AgentRuntime.migrate_session_plugin_composition()` / `reload_plugin_composition()`，由 `traceh chat` 的四条 `/plugins` 命令调用，但没有第二套 PluginManager、ToolRuntime 或 Registry。`/plugins reload` 重建当前外部 id；`/plugins use` 需要显式目标 id，身份变化时先写授权事件；`/plugins use --none` 只保留 `traceh.core`。命令只在空闲提示符执行，不创建 Turn。插件 id/version 输出经过受控身份验证；第三方 setup/health/cleanup 异常不直接打印。

D0 不改变上述协议，只重新划清控制面所有权。[`runtime/plugin_composition.py`](../../src/traceh/runtime/plugin_composition.py) 的 `PluginCompositionCoordinator` 现在持有候选替换串行锁、共享 admission/migration Gate、replacement/admission 在途 Task 集合、Session durable identity 校验以及 migration CAS/may-have-committed/fail-closed 流程；[`runtime/agent_runtime.py`](../../src/traceh/runtime/agent_runtime.py) 保留公开方法的薄委托、活跃 Turn 表、最终 Turn admission 线性化点和总关闭 Task。公开门面之间原有的动态分派同样保留：`reload_plugin_composition()` 读取门面的 `enabled_plugin_ids` 并调用门面的 `migrate_session_plugin_composition()`，协调器没有另设 reload 捷径，因此继承、替换或审计公开迁移入口不会被抽取绕过。协调器通过窄回调查询“Runtime 是否关闭”“是否有活跃 Turn”“current Generation 的外部身份”，不保存第二份可变插件身份，也不执行 Turn。`AgentLoop` 完全未修改，继续只依赖 `CompositionRuntime.lease()`。D0 当时是进入 Scope 工作前的结构检查点；D1/D2 的 Service 与 Composition Scope 随后接入 Builder/ActivationSet/Generation，没有把控制面复杂度塞回门面。

当前身份事实由 [`session/plugin_identity.py`](../../src/traceh/session/plugin_identity.py) 共享计算：初始值来自 `session/created.metadata.traceh_plugins`，合法 `composition/snapshot` 更新到实际 Step 身份，合法 `composition/migration-authorized` 要求 `from_plugins` 等于此前身份且 `source_seq` 等于此前身份事实序号，然后更新到 `to_plugins`。迁移事件只记录外部插件，不写 `traceh.core`、Generation identity 或 Request Fingerprint。候选通过 setup/conflict/health 后才追加授权；append 取消会按稳定 `migration_id` 重读判断是否已落盘。若授权已落盘但 publish 失败，Runtime 不伪造成功，也不继续接受旧 Composition，Session 保持 fail-closed。

本阶段仍没有运行中 pip install/uninstall、强制 `importlib.reload()`、文件 watcher、Workspace/Preset/Agent 层的插件 setup、EventStore 插件贡献、isolated 插件、多 Agent、Workflow、MCP、TUI 或模型流式输出。D2 的 Tool/Prompt/Policy 是宿主程序显式装配；D3 新增的 Provider/Policy/Middleware/Verifier 也仍属于 application setup。Python module 可能仍在 `sys.modules` 中，`/plugins reload` 不是从磁盘重新加载修改后的源码。版本仍为 `0.4.0`，Stage D3 不是 v0.5 发布。

`trust_mode="isolated"` 被**明确拒绝**而不是降级成 trusted：把“请求隔离”当成“允许进程内运行”的许可，等于给了插件比它申请的更高权限。真正的隔离需要进程边界、每次 context 调用的序列化契约和子进程崩溃的失败模型，这些都还不存在。

### 19.8 dispose 顺序

`AgentRuntime.dispose()` 在与 Turn admission 最终检查共享的 `_lock` 线性化点标记关闭，之后先取消并 `gather` 全部活跃 Turn；再调用 `PluginCompositionCoordinator.shutdown_inflight()`，取消并等待所有在途候选替换/迁移及其 rollback，也等待已进入共享 Gate 但尚未完成注册的 Turn admission；随后才调用 Composition Runtime 的 `dispose()`，退休 current Generation 并 Drain 所有 Generation-owned ActivationSet；最后清理 application-level legacy 资源。协调器会取回每个候选 Task 的终态：正常取消不算关闭失败，但候选 rollback 产生的 `PluginDisposeError` 会进入关闭错误集合，因此 `dispose()` 不能在插件资源可能未清干净时报告成功，后续调用也会重放同一关闭失败。默认 Stage B/C 路径没有 PluginManager 的插件 cleanup owner；只有保留 v0.4 兼容性的自定义 legacy 装配才会在 Drain 后调用可选的 `PluginManager.dispose()`。顺序相反会让 Drain 越过仍在 setup/rollback 的候选，或把某个仍有 Lease 的 Generation 正在使用的 Tool、Service 或 Owned Task 抽走。

整个关闭过程位于唯一的内部 Task 中，因此重复调用复用同一结果、重复取消无法提前放行、关闭失败不会被后续调用伪装成成功；完整语义与它修复的缺陷见 5.5。

单个插件 cleanup 失败不阻止其余 cleanup：失败被收集成 `PluginDisposeError`，`Lifespan.close()` 本身也已保证逐条继续。该错误会通过 `AgentRuntime.dispose()` 传播出来，并在后续每一次 `dispose()` 上再次抛出。

### 19.9 Session 插件身份

`AgentRuntime.create_session()` 把初始外部插件身份写进 `session/created` 的 metadata 保留键 `traceh_plugins`。每个实际 Step 的 `composition/snapshot` 也记录完整 PluginIdentity。

**保留键按“是否出现”判定，不看取值。** 只要调用方的 metadata 里出现 `traceh_plugins` 就抛 `ValueError`，无论它是 `[]`、`None`，还是与当前插件身份**完全相同**的列表。早期实现只在取值与预期不同时才拒绝，于是这三种写法都能通过——而这个键记录的是运行时**自己观测到什么**，任何调用方能写进去的值都是一个 Runtime 无法背书的断言。调用方提供的其余 metadata 键照常原样保存。

`verify_session_plugins()` 在 `run_existing()`、`resume()` 和 `chat` 继续旧 Session 时执行，且**早于 recovery**——recovery 会追加事件，向一个用不同 Composition 创建的 Session 追加事件正是要防的事。身份解析集中在 [`session/plugin_identity.py`](../../src/traceh/session/plugin_identity.py)：按 seq 顺序读取 `session/created.metadata.traceh_plugins`、合法 `composition/snapshot` 与合法 `composition/migration-authorized`。迁移授权要求 `from_plugins` 等于此前有效身份、`source_seq` 等于此前身份事实序号，才把有效身份更新为 `to_plugins`。如果 Session 已有最新合法 `composition/snapshot` 的 `plugins` 字段，校验优先使用实际 Step 身份；没有 Snapshot，或是 v0.3 缺少该字段的旧数据，才回退到创建 metadata（随后合法迁移事件仍可显式更新授权身份）。不匹配抛 `SessionPluginMismatchError`，消息同时列出 Session 要求的与当前运行的两组身份，且**保留 Session 当时记录的原始版本文本**而不是改写过的形式。Stage B 的内部替换仍不会自动授权；Stage C 只有用户明确执行 `/plugins use` 才追加本 Session 的 migration authorization，不会自动迁移其他 Session。内部 publish 本身不追加事件；如果只是内部 publish 且新 Generation 尚未执行 Step，进程崩溃后仍可回到最后一条 durable Snapshot。

**缺键与显式 `null` 必须区分，读取端用缺失 sentinel 而不是 `get()`。** `dict.get()` 对“键不存在”和“键被显式记为 `null`”返回同一个 `None`，这是两个不同的事实：键真正缺席的是 v0.4 之前写下的无插件 Session；显式记录的 `null` 不是本 Runtime 任何版本会写下的值，属于损坏数据，必须报 `malformed` 而不是当“无插件”放行。这个 sentinel 现在由共享的 `plugin_identity.py` 持有，Runtime、迁移和不变量检查不各自复制解析规则。

**版本按 PEP 440 语义比较，用的是 `Version` 对象而不是字符串。** 这一条必须写准，因为直觉上的写法是错的：`str(Version("1.0"))` 是 `"1.0"`，`str(Version("1.0.0"))` 是 `"1.0.0"`，所以“先 `Version()` 解析再 `str()` 规范化然后比字符串”**并不会**把两个 PEP 440 等价版本判成相同——早期实现正是这样，于是 `1.0` 与 `1.0.0` 之间会被误判为组合变化并拒绝继续 Session。现在由共享 identity helper 生成 `(plugin_id, Version)` 键：`Version("1.0") == Version("1.0.0")` 为真，而 `Version("1.0") == Version("1.0.1")` 仍为假，因此真正的版本变化照旧被拒绝。无法解析的版本仍然报 `malformed`，等价性没有变成宽容；migration event 的 `from/to/source_seq` 也使用这套对象比较，不能用 `str()` 把坏值修成合法身份。

v0.4 之前写下的 Session 没有这个键，等价于“无插件”，可以正常继续。

`traceh chat` 打印的恢复命令按 Session 最新 durable 插件身份生成，而不是盲目读取当前 Runtime Generation；因此授权已落盘但 publish 失败时，提示仍会带上已授权的目标 `--plugin`，不会给出必然被拒绝的旧组合。若 durable 身份本身无法安全读取，则只打印转义后的定位信息，不打印可能误导的命令。迁移 API 在候选构建前后都会投影 Session 的持久化 Turn/Step 生命周期；存在未闭合 Turn 或 Step 时直接拒绝，不追加授权。

### 19.10 CLI 输出安全

`list`/`inspect`/`doctor` 打印的每一个字符串都来自第三方 Distribution 元数据，因此全部经过 `escape_for_display()`（与 13.8 的恢复命令、13.6 的 Timeline 共用同一套规则）：严格一行、无控制字符、有长度上限。`traceh chat` 的 `/plugins` 只显示经过 Manifest/PEP 440 验证的 `plugin_id==version`，而 `/plugins use`、`reload` 的失败出口只显示仓库固定的一行摘要，不回显无效 id、插件异常正文、路径或配置值；通用未知命令也只显示固定的 `unknown command (try /help)`，不回显整条用户输入。`_safe()` 递归处理整个结构而不是几个预期字段，因此不存在“某个字段忘了清洗”。

`doctor` 使用一次性 `ToolRegistry` 与 `PromptAssembler`，因此它激活的任何东西都到不了真实 Runtime；无论激活成功与否都在 `finally` 中 dispose。插件自身的异常文本从不外泄——所有 message 都由本仓库编写，失败只用固定 `code` 区分。

### 19.11 示例插件

[`examples/plugins/traceh-example-skill-plugin/`](../../examples/plugins/traceh-example-skill-plugin/) 是一个**可独立构建安装**的 Distribution，不是仓库内的测试夹具。它有自己的 `pyproject.toml`、`traceh.plugins` Entry Point、`PluginManifest`、一个 `PromptSection`、一个 `PURE_READ` 无副作用 Tool，以及一份打包进 Wheel 的 `SKILL.md` 资源（通过 `importlib.resources` 读取）。

它明确**不**扫描用户的 Codex/Claude 目录、不读环境变量、不访问网络，也**不**因为被安装就成为默认能力。

### 19.12 `OwnedTaskSet` 是生命周期所有权，不是监督器

插件通过 `PluginContext.spawn_owned()` 创建的后台任务归 [`kernel/tasks.py`](../../src/traceh/kernel/tasks.py) 的 `OwnedTaskSet` 所有。必须准确描述它**拥有什么**：

| 它保证 | 它不做 |
|---|---|
| 关闭时取消并等待全部 owned task 收敛 | 重启失败的任务 |
| 取回每个任务的结果或异常，不留给 GC | 把后台任务失败升级为 Runtime 故障 |
| —— | **保留**异常对象。取回之后立刻丢弃，不建立任何异常列表 |

**为什么“取回异常”需要一个明确的所有者。** 一个在关闭之前就抛异常的任务会自行完成，done callback 把它从集合中移除——于是 `cancel_and_wait()` 永远看不到它，也永远不会取回它的异常。asyncio 随后会在垃圾回收时报 `Task exception was never retrieved`：时机与真正的原因无关，也不归属于任何组件。因此 done callback（`_retire()`）在任务完成的那一刻调用一次 `task.exception()`：被取消的任务直接跳过（取消是预期的关闭结果，而且此时 `task.exception()` 本身会抛 `CancelledError`），正常完成和真正失败的都只到“取回”为止。

**为什么取回之后不留存。** 早期版本把每个失败对象追加进一个 `failures` 列表供“查询”，但该列表**没有任何主线消费者**——它是一份无界、永久增长、永远不会被读的记录。而每个异常对象都持有 traceback，traceback 又持有每一帧的局部变量：为无人读的数据保留不受信任的插件状态，既是内存泄漏，也是一道泄漏面。真正的可观测性必须从“有消费者”开始；在 v0.4 拥有消费者之前，所有权止步于取回。测试明确钉住这一点：所有者身上没有 `failures` 属性，一百次失败后所有者状态不增长。

**为什么不升级成 Runtime 故障。** 把插件的后台任务崩溃翻译成“Runtime 失败”是一个 v0.4 尚未做出的策略决定：它会改变“运行时失败了”这句话的含义，也需要定义重启、退避和上报规则。本轮刻意只做所有权，并在此写明边界，而不是顺手发明一个未经授权的监督器。

### 19.13 D1：四层 Service Scope 主线

D1 没有另造一套“Scoped Runtime”。[`kernel/registry.py`](../../src/traceh/kernel/registry.py) 的 `ServiceRegistry` 仍是 Service 注册唯一主线，只是每层 Registry 现在可以只读穿透一个 parent；[`kernel/scope.py`](../../src/traceh/kernel/scope.py) 用固定的 `ScopeKind` 把它们装成 Application → Workspace → Preset → Agent。默认同步/异步 Runtime 工厂接收显式 `ScopedServiceBinding`，`PluginGenerationBuilder` 为每个候选复制 application Registry，再重新构造后三层。候选 `PluginActivationSet` 持有这一整条 ScopeChain，Generation 构造时捕获其 effective Agent Scope 与 `ServiceView`，Step Lease 随 Provider/Tool/Prompt 一起返回同一代的 Service 视图。公开 `PluginManager.prepare_activation_set()` 会把来源链的 Workspace/Preset/Agent binding blueprint 交给 Builder，不会把既有 child Scope 静默丢掉。application 插件的 Service 会晚于 child binding 发布，所以 Manager 在 Activation 真正生效前重新校验 child override；Workspace/Preset/Agent 不能利用时间差绕过 `replace=True` 或 API Major 检查，失败仍走事务回滚并保留稳定冲突 code 与责任插件身份。

解析规则是协议而不是惯例：同层第二次绑定若未传 `replace=True`，得到 `service-already-bound`；显式传入 `replace=True` 才替换同层旧值。较近层覆盖祖先也必须传 `replace=True`，否则得到 `service-override-requires-replace`；字符串 `"false"`、整数 `1` 或其他 truthy 值都不是覆盖授权，`replace` 必须是严格的 `bool`。显式覆盖若只找到同名但不同 API Major 的能力，得到 `service-override-api-major-mismatch`。插件发布冲突保留同一稳定 code 与责任 `plugin_id`，不会降级成无身份的通用 `plugin-publish-failed`。错误对象同时保留 `code`、目标 key、当前 scope 与既有 scope，调用者不需要匹配整段英文。不同 API Major 在没有声称“替换”时可以并存。`ServiceKey.api_major` 必须是正整数，布尔值不算整数。

装配按固定层级排序，而不是相信调用者传入 binding 的顺序；因此把 Agent binding 写在 Application binding 前面也不能绕过覆盖检查。`ScopeChain.build()` 先在隔离 fork 上预检完整四层，全部成功后才把 Application binding 写进调用方 Registry；后续层失败不会留下“幽灵”Application 值。构造完成后四个 Scope 会封印，公开 `ServiceView` 只有 `resolve/get/require/snapshot`，没有 `provide()`；`AgentRuntime.services` 与 `ActiveComposition.services` 都返回该只读视图，`AgentRuntime.scope` 与 `ActiveComposition.scope` 指向 current/leased Generation 捕获的 Scope。插件内部仍通过 application Registry 的受控 Registration 写入，旧代的 Registration 只有在最后一个旧 Lease 退出后才撤销，所以新 Generation 切换不会原地改写旧 Step 的 Service 视图。

D1 的 Scope 能力是 ActivationSet 的可选扩展，不会反向收窄 D0 的 ownership/cleanup 协议：满足原有 claim/dispose 合同但没有 `scope`/`services` 属性的自定义 ActivationSet 仍可装配，其 Generation/Lease 的 Scope 视图为 `None`；如果自定义对象选择提供 D1 Scope，则 `scope` 与 `services` 必须成对出现并属于同一条链。默认 `PluginActivationSet` 始终提供完整四层视图。

层级方向同样受约束：application 插件 setup 可以读取 application Service，但不能反向读取 workspace/preset/agent 覆盖；最终 Runtime/Step 从 agent 层向上查找。两个 Runtime 用不同 agent binding 时不会共享局部 Registry。`ScopedServiceBinding.value` 当前是由装配调用者持有生命周期的借用能力，Scope 只解析、不自动 dispose；插件通过 Registration 提供的 application Service 才随 ActivationSet/Generation 清理。这里没有把 `PluginManifest.allowed_scopes` 解锁：插件仍按 v0.4 规则只允许 application setup。Service 不直接进入模型请求，所以 Scope identity 不写进 `CompositionSnapshot.revision` 或 Request Fingerprint；D2 真正影响模型可见内容的 Overlay 继续走现有 Generation/Snapshot 主线，见下一节。

### 19.14 D2：Tool、Prompt 与 Policy 的四层程序化 Overlay

D2 沿用 D1 的固定顺序，但没有复制一套“Scoped ToolRuntime”。[`kernel/composition_overlays.py`](../../src/traceh/kernel/composition_overlays.py) 接收 `ScopedToolBinding`、`ScopedPromptBinding` 与 `ScopedPolicyBinding`，先在私有 fork 上按 Application → Workspace → Preset → Agent 排序解析，再产出既有 `ToolRegistry`、`PromptAssembler` 和 Policy tuple。默认同步/异步工厂把这些显式 binding 交给 `PluginGenerationBuilder`；空插件、启动插件和后续 `/plugins` 候选替换都使用同一份不可变 blueprint。解析结果进入 `PluginActivationSet`，随后由既有 `CompositionGeneration` 冻结 Tool schema、Prompt、Policy 名称并生成 Snapshot revision。`AgentLoop`、RequestBuilder、Event Log 与 Replay 没有第二条路径。

Tool、Prompt、Policy 都以稳定名字作为覆盖身份。相同 scope 的第二次绑定若没有 `replace=True`，分别得到 `tool-already-bound`、`prompt-already-bound`、`policy-already-bound`；较近层覆盖祖先而未授权时得到对应的 `*-override-requires-replace`。`replace` 必须是真正的 `bool`，字符串、数字和 `None` 都不能冒充授权。解析按 scope 排序而不信任输入顺序，且只修改 fork；即使 Policy 冲突发生在 Tool/Prompt 已完成候选替换之后，调用方原来的 Registry 与 Prompt 也保持不变。`PromptAssembler.register(..., replace=True)` 的 Registration 会在逆序清理时恢复旧 Section，与 ToolRegistry 的可逆替换语义一致。

插件 Tool/Prompt 仍由 application setup 贡献，因此有一个晚到祖先问题：初次解析 child Overlay 时插件内容尚不存在。Manager 会把 staged application Tool/Prompt 投影到私有候选，**在 health check 之前**再次解析 child Overlay；隐式覆盖因此以稳定 code 和责任 `plugin_id` 失败并回滚，第三方 health 不会获得一次本来就不该发生的执行机会。全部插件真实发布后再解析一次，最终 Tool/Prompt/Policy 三者一起转移到 ActivationSet。后续插件组合替换继续使用 Builder 保存的 child blueprint；协调器构造候选 ToolRuntime 时必须使用 ActivationSet 的 Policy tuple，CompositionGeneration 按长度、顺序和逐项 `is` 对象身份校验二者一致，绝不调用可由第三方重载的 `__eq__`。因此名称相同但 admission 行为不同的 Policy 不能伪装成同一候选能力。

D2 只增加宿主装配能力；D3 才在下一层正式扩宽 `PluginContext`，见下一节。Binding 中的程序化 Tool/Policy 是借用能力，其生命周期仍由装配调用者持有；application 插件资源由对应 ActivationSet 清理。两个 Runtime 可以装配不同的 Agent Tool/Prompt/Policy，真实 Tool admission 与 Request Snapshot 会反映各自结果，但当前还没有 `AgentSupervisor` 去创建和管理两个 Agent。

### 19.15 D3：Provider、Policy、Middleware 与 Verifier 插件贡献

D3 没有给四类能力另建“插件 Runtime”。`PluginContext.register_provider()` 写入候选 `LlmRegistry`，`register_policy()` 与 `register_middleware()` 进入候选 ToolRuntime，`register_verifier(name, verifier)` 写入命名候选；这些 Registration 都归当前 Activation，setup、conflict、health、publish、rollback、最后 Lease cleanup 继续使用同一事务。**setup 是唯一允许改变候选 Composition 的阶段**：全部插件 setup 完成后，Manager 会先关闭每个 Context 的 Provider/Policy/Middleware/Verifier/Tool/Prompt/Service 注册入口，再做冲突检查和 health。health 仍可读取配置与 Service，并可登记 cleanup/Owned Task，但不能补注册执行能力；尝试晚注册会成为有界的 `plugin-health-check-failed` 并走同一回滚，不可能绕过 pre-health 检查。关掉方法本身还不够：Tool、Provider、Policy、Middleware 的名称会在注册时单独捕获，冲突检查、Overlay 归因和选择判断只读这份事务事实；setup 后、每次 health 返回后及带 `await` 的 Service 发布结束后都会校验原对象名称仍一致。任何漂移都以 `plugin-contribution-identity-changed` 拒绝并逆序回滚；Tool/LLM Registration 撤销同样使用注册时键，不会因对象改名清错槽位。`prepare_activation_set()` 是公开的异步交接边界，返回后调用方可能在构造 Generation 前再次 `await`；因此 ActivationSet 在 transfer 时保存不可变 capability receipt，Generation claim 前重新核对候选 Registry 容器、成员对象、固定名称、Prompt、Policy/Middleware、Verifier 与插件身份。交接不是在 `activate()` 返回时完成，而是在 ActivationSet 构造成功后才完成；如果 receipt 自身发现 Registry key 与活对象身份已经分裂，Builder 会 dispose 尚未转移所有权的临时 Manager，取消并等待 Owned Task、逆序 cleanup 每个 Activation，然后重新抛出原始交接错误。清理期间的重复取消不会让调用方提前返回；清理也失败时，两份错误通过 `BaseExceptionGroup` 构造器一起保留：成员全是普通 `Exception` 时 Python 自动派生为 `ExceptionGroup`，而 `KeyboardInterrupt`、`SystemExit` 等直接 `BaseException` 仍能留在 `BaseExceptionGroup` 中，不会被新的分组 `TypeError` 遮蔽。Generation 复核失败则由已经拿到 ActivationSet 的调用方按既有候选 cleanup 协议负责。Tool schema 与 ToolRuntime 查找键都以已经登记的 Registry key 为准，不能出现 Snapshot 宣称新名字而执行表仍只认旧名字。Provider/Policy/Middleware 名字必须满足通用能力名规则，实现必须提供相应的 `complete()`、`check()`、`invoke()`；Verifier 由注册时的显式名字标识并要求 `verify()`。它们不能隐式覆盖宿主同名能力：Provider、Policy、Middleware 分别用 `provider-publish-conflict`、`policy-publish-conflict`、`middleware-publish-conflict` 在 health 前拒绝；同一候选内部的重复注册则在 setup 阶段按既有事务失败并回滚。插件 Policy 与 child Overlay 冲突时也保留稳定 code 和责任 `plugin_id`。

“注册了”不等于“自动接管”。有效 Provider 仍由 `RuntimeConfig.provider` / CLI `--provider` 明确选择；自定义名字只有在同时显式启用至少一个插件时才被 CLI 接受，并且必须明确提供 Model。Verifier 同样由 `verifier_name` / `--plugin-verifier` / `TRACEH_PLUGIN_VERIFIER` 选择；没有选择时，插件 Verifier 不运行，现有直接 Verifier 或 `--verify-command` 语义不变。命名插件 Verifier 与命令 Verifier 互斥，缺失显式目标分别得到 `provider-not-provided` 或 `verifier-not-provided`，并在 health 前回滚，而不是从“唯一看起来像候选”的对象猜默认值。

`PluginActivationSet` 现在随 Tool/Prompt/Service/Policy 一起持有候选 LLM Registry、Middleware tuple 与有效 Verifier。`CompositionGeneration` 对选中 Provider、Policy、Middleware 和 Verifier 都做对象身份守卫；ToolRuntime 不能换成名称相同、行为不同的对象。若 ActivationSet 显式提供 LLM Registry，所选 Provider 必须存在于该 Registry，且必须与 Runtime 使用的对象逐项 `is` 相同；“候选里没有，但另一个 Registry 里恰好有同名 Provider”不是回退条件。为保持 D0 的自定义 ActivationSet 替换合同，只有完全没有 `llms` 属性（或明确为 `None`）的旧式对象，协调器才借用自身已有核心 Registry；显式提供 D3 Registry 的候选绝不会走这个兼容分支。`ActiveComposition` 把 Verifier 带进 Step Lease，AgentLoop 的验证阶段已移动到 `async with compositions.lease(...)` 内：模型响应、ToolRuntime 和验证器由同一个 Generation 冻结。发布新 Generation 时，正在验证的旧 Step 仍用旧 Verifier，旧插件 Activation 要等该 Lease 退出后才 cleanup。Snapshot 已记录 Provider、Policy/Middleware 名称和插件身份；Verifier 不影响发给模型的 Request，因此不新增 Request Fingerprint 字段，其真实结果仍由 `verification/result` 持久化。

EventStore 刻意没有加入 `PluginContext`。它是 SessionService、Recovery、Inspector 和所有事件写入共同借用的**进程级事实源**，而当前插件 ActivationSet 会随 Step Generation retire。若让 `/plugins` 切换卸载 Store 插件，旧 Session 会继续握着已经被 cleanup 的账本实现，或者同一 Runtime 出现两本账。真正开放前必须先设计独立于 Generation 的 process-lifetime/pinned Activation 所有权、Store 构造与关闭顺序、旧 Session 兼容和合同测试；当前仍只能在 Runtime 构造时直接注入 EventStore。这个收窄记录在 [ADR-0014](../adr/0014-generation-scoped-plugin-execution-capabilities.md)。
