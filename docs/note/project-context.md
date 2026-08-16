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
- 本文与通俗版使用相同的一级编号，便于逐章核对。

## 1. 当前项目状态

| 项目 | 当前事实 |
|---|---|
| 包名 | `traceharness-py` |
| Python 包 | `traceh` |
| 当前版本 | `0.3.0` 维护线，另有 `.env` 支持等 Unreleased 改进 |
| 成熟度 | Educational alpha；可运行、可测试，公共 API 尚未承诺生产稳定性 |
| Python | `>=3.12`；CI 覆盖 Ubuntu 3.12/3.13 与 Windows 3.12 |
| 运行时依赖 | Python 标准库，无第三方运行时依赖 |
| 开发依赖 | pytest、pytest-asyncio、ruff |
| 当前 Agent 模型 | 单进程、单 Session 同时最多一个活跃 Turn |
| 持久化 | 本地 Append-only JSONL Session Stream 与 Effect Stream |
| 模型接入 | 确定性 Scripted Provider；非流式 OpenAI-Compatible `/chat/completions` Provider |
| Coding Tools | `list_files`、`read_file`、`search_text`、`apply_patch`、`shell` |
| 完成判定 | 可选外部 `CompletionVerifier`；默认实现为命令退出码验证 |
| CLI 形态 | `traceh chat` 提供同一 Session 内的连续多轮行式交互；其余命令仍是一次执行一个 Turn。不是流式 TUI |
| 事件写入互斥 | JSONL Stream 在 POSIX 与 Windows 上均有操作系统级跨进程文件锁 |
| 当前自动化测试 | 114 项，通过后才允许更新本表 |
| 内置 Benchmark | 1 个确定性修复案例 |

当前开发重点是把 v0.3 的可靠性、真实使用体验、可观察性和文档治理做扎实；v0.4+ 插件与多 Agent 仅保留协议边界，不视为已实现能力。

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

- 完整 PluginManager、Entry Point 自动发现和热卸载；
- 活跃的 AgentSupervisor、子 Agent Tool 和 Workflow Engine；
- Git Worktree/Overlay Workspace 分支与合并；
- Docker、远程沙箱或操作系统级安全隔离；
- 分布式 Event Store；
- 完整流式模型输出、重试、Fallback 与限流中间件；
- 类似 Codex/Claude Code 的富交互终端界面：`traceh chat` 只是行式多轮提示符，没有流式输出、Tool Call 时间线和执行前审批。

## 3. 仓库目录与职责

```text
traceharness/
├── AGENTS.md                         跨 Coding Agent 的仓库开发规则
├── CLAUDE.md                         Claude Code 薄入口，导入 AGENTS.md
├── src/traceh/
│   ├── api/                          公共协议、不可变 DTO 和扩展边界
│   ├── concurrency.py                不可取消 Worker 的收敛等待
│   ├── cli/                          命令解析、.env 加载、交互式 chat 循环和终端编码
│   ├── evaluation/                   确定性 Benchmark Runner
│   ├── inspector/                    Session 文本、Replay 和静态 HTML 检查
│   ├── kernel/                       Scope、Activation、Hook、Lifespan、Owned Tasks
│   ├── llm/                          Provider 协议实现、注册表和调用边界
│   ├── runtime/                      AgentRuntime、AgentLoop、请求、Continuation、Verifier
│   ├── session/                      EventStore、跨进程文件锁、投影、恢复、压缩和不变量
│   └── tools/                        Tool Registry、Schema、Policy、Middleware、子进程收敛与内置工具
├── tests/                            单元、契约、恢复、取消、跨进程、端到端和 Benchmark 测试
├── examples/                         无 Key 的确定性 Demo 夹具
├── benchmarks/                       独立复制 Workspace 的评估案例
├── docs/
│   ├── note/                         当前项目正式版与通俗版上下文
│   ├── adr/                          已接受设计决定及原因
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

默认装配入口是 [`build_default_runtime()`](../../src/traceh/runtime/agent_runtime.py)。它创建或接受以下替换点：

- `EventStore`；
- `LlmProvider`；
- `PromptAssembler`；
- `Tool`、`ToolPolicy`、`ToolMiddleware`；
- `CompletionVerifier`；
- `ContinuationRuntime`。

```mermaid
flowchart TD
    CLI["CLI / SDK / Evaluator"] --> AR["AgentRuntime"]
    AR --> AL["AgentLoop"]
    AL --> CR["CompositionRuntime"]
    AL --> RB["RequestBuilder"]
    AL --> LR["LlmRuntime + Provider"]
    AL --> TR["ToolRuntime"]
    AL --> CV["CompletionVerifier"]
    AL --> SS["SessionService"]
    RB --> SP["SurfaceProjector"]
    TR --> SS
    SS --> ES["EventStore"]
    ES --> JL["JSONL Session / Effect Streams"]
    REC["Recovery / Inspector / Invariants / Compaction"] --> SS
```

依赖规则：

- `AgentLoop` 只编排生命周期，不导入具体工具、JSONL 文件或厂商 HTTP 逻辑；
- `AgentRuntime` 是对外门面和默认依赖装配点；
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
- `cancel()` 先追加 `runtime/cancel-requested`，再取消 Task。`JsonlEventStore` 的取消语义见 6.6：被取消的 Store 操作不会留下仍在后台写入的线程。
- `AgentLoop` 在取消/异常时追加 Attempt、Step、Turn 的终止或错误事件；ToolRuntime 尽量补齐未完成调用的 Tool Result。
- `dispose()` 取消并等待当前 Runtime 持有的活跃 Turn；Shell Tool 在取消时先 terminate，超时后 kill 并等待进程退出。

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

当前实现是 `StaticCompositionRuntime`：每个 Step 都生成一致来源的快照，但 Lease 协议已经把对象生命周期与主循环分开。完整 generation、drain 和热更新尚未实现。

### 7.2 Surface

`SurfaceProjector` 只把以下事件投影为模型消息：

- `user/message` → user；
- `assistant/message` → assistant，保留 tool calls；
- `tool/result` → tool；
- `surface/replace` → 替换指定旧 Surface 事件的摘要消息。

原始事件仍保留。多次 Replacement 通过 source seq 遮蔽旧视图，而不是删除历史。

### 7.3 Request Snapshot 与 Fingerprint

`RequestBuilder` 使用“截至 Composition Event 的 Surface + 当前 Composition”生成 `ModelRequest`，持久化完整 Request、`source_seq`、composition revision 和稳定 fingerprint。

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
| `traceh doctor` | 检查 Python、数据目录和非秘密 Provider 配置状态 |

除 `chat` 外的命令都是 run-to-completion：接收一次任务，执行到 Turn 结束，打印最终文本和摘要。`chat` 增加了同一 Session 内的连续输入循环，但仍是行式提示符：没有 token 流式输出、实时 Tool Call 时间线、执行前审批，也不能在 Turn 运行期间继续输入。

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
- `TRACEH_VERIFY_COMMAND`。

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
- 内部命令只在整行匹配时生效：`/help`、`/session`、`/exit`、`/quit`；空行忽略；未知斜杠命令给出提示；以上都不产生 `user/message` 或 Turn；
- EOF 等同 `/exit`，退出码 0；
- Ctrl+C 的行为取决于宿主 Shell 与 Python 的信号处理，文档不做绝对承诺：
  - 宿主把 Ctrl+C 变成 `KeyboardInterrupt` 时（Linux/macOS 终端、Windows PowerShell 常见情况），Chat 通过既有取消语义收敛当前 Turn（`AgentRuntime.cancel()`），打印可恢复的 session_id，然后从进程内部以 130 返回。宿主最终显示的退出码仍由 Shell 决定，因此不承诺"PowerShell 一定看到 130"；
  - 硬中断（Windows Ctrl+Break、控制台关闭）由操作系统直接终止进程，Python 处理器不会运行：实测退出码为 `3221225786`（`0xC000013A`），没有收敛提示行。此时依赖崩溃恢复：启动时打印的 `session_id=` 就是入口，`traceh chat --session-id <id>` 或 `traceh recover` 会把残留生命周期闭合；
- `runtime.dispose()` 在 `finally` 中执行，覆盖 Python 能够处理的所有退出路径（`/exit`、`/quit`、EOF、`KeyboardInterrupt`、取消、异常）。被操作系统直接终止的硬中断不在此列：那条路径上没有任何 Python 代码运行，靠的是崩溃恢复。

Turn 通过 `asyncio.shield` 提交，因此中断到达时 Runtime 仍持有该 Turn，可以走正常取消路径收敛，而不是留下脱缰任务。

### 13.5 终端编码策略

`configure_stdio()` 把 stdin/stdout/stderr 统一配置为 UTF-8 且 `errors="replace"`，不依赖 `chcp 65001`；不支持 `reconfigure` 的流（测试中的 `StringIO`）安全降级并在报告中标明。

输入侧规则：

- 行首 U+FEFF 属于流而非消息，被剥离。Windows PowerShell 5.1 的 `Out-File -Encoding utf8` 会写入 BOM；PowerShell 7 的 `utf8` 默认无 BOM，需要时用 `utf8BOM`；
- 中文等非 ASCII 内容原样进入 `user/message`，只去除首尾空白；
- 若行内出现 U+FFFD，说明原字符在解码时已经丢失：调用模型前拒绝该行、打印提示、不写 `user/message`、不猜测原文。

## 14. 已有扩展边界与未来接口

当前代码中存在但尚未形成完整产品能力的边界：

| 未来方向 | 已有协议/原语 | 当前缺失实现 |
|---|---|---|
| 插件 | `PluginManifest`、`Plugin`、`PluginContext` | Entry Point Discovery、PluginManager、依赖解析、健康检查 |
| 可逆生命周期 | `Activation`、`Lifespan`、`OwnedTaskSet` | 与真实 PluginManager 的完整集成 |
| 服务与 Scope | `ServiceKey`、`ServiceRegistry`、`Scope` | Application/Workspace/Preset/Agent 完整层级装配 |
| Composition Generation | `CompositionSnapshot`、`CompositionRuntime.lease()` | 多代发布、引用计数、Drain、卸载 |
| 多 Agent | `AgentSpec`、`AgentHandle`、`AgentSupervisor` Protocol、Budget DTO | 活跃 Supervisor、Inbox、子 Agent Tools、冷恢复 |
| Workspace 分支 | `WorkspaceProvider`、Snapshot、PatchArtifact、MergeResult | Git Worktree/Overlay 实现和协调 |
| Workflow | 可复用单 Agent Runtime 边界 | Workflow Engine、Map/Join/Approval 节点 |

这些类型证明接入方向，但不得在文档或对外说明中表述为“已实现插件系统/多 Agent”。

## 15. 测试与验证基线

### 15.1 本地标准检查

```powershell
python -m compileall -q src tests
python -m pytest -q
```

当前测试套件共 114 项，覆盖：

- EventStore expected-seq、尾部恢复和读取；
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
- 未来 Plugin/Agent/Workspace Protocol 可构造性。

跨进程测试通过 `tests/cross_process_worker.py` 启动真实独立解释器，用握手文件而不是长 sleep 同步；它们在临界区内制造确定性重叠窗口，因此去掉 OS 锁后会稳定失败。该 Worker 文件不以 `test_` 开头，pytest 不会收集它。

### 15.2 CI

GitHub Actions 在 push 和 pull request 上运行两个 Job：

| Job | 平台 | Python | 步骤 |
|---|---|---|---|
| `test` | `ubuntu-latest` | 3.12、3.13 矩阵 | 可编辑安装、compileall、pytest、`traceh doctor` |
| `test-windows` | `windows-latest` | 3.12 | 同上 |

Windows Job 是为跨进程文件锁新增的最小覆盖：该平台走 `msvcrt` 而不是 `fcntl`，必须在真实 Windows Runner 上执行。

### 15.3 发布快照与当前测试的区别

`VALIDATION.md` 保存最初 v0.3 发布时的 24 项测试、覆盖率、Demo、Wheel 和干净安装验证。此后 `.env` 功能把测试增加到 31 项，跨进程文件锁与取消语义再增加 12 项到 43 项，Model Attempt 恢复与不变量再增加 27 项到 70 项，`traceh chat` 再增加 24 项到 94 项，取消收敛与子进程编码加固再增加 12 项到 106 项，输出所有权与本地资源收敛再增加 3 项到 109 项，超时证据入下一 Step 与测试清理再增加 3 项到 112 项，Tool 与 Runtime 两类超时的边界再增加 2 项，当前共 114 项。不要把发布时点数字误认为当前测试总数，也不要未经重新运行就改写历史验证结果。

## 16. 已知限制与风险

| 领域 | 当前限制/风险 | 完善方向 |
|---|---|---|
| Stream 锁边界 | 已有 `fcntl`/`msvcrt` 跨进程锁，但只是同机 Advisory Lock：绕过 `JsonlEventStore` 直接写文件不受约束，网络文件系统行为未验证 | 需要更强隔离时改用 SQLite 或独立 Store |
| Session 级并发 | 事件写入跨进程安全，但“同一 Session 只跑一个 Turn”仍只在单进程内强制 | 跨进程 Session Lease 或 Runtime 级占用标记 |
| 子进程输出磁盘占用 | 捕获用的临时文件当前没有大小上限，失控命令可以写满临时目录；上层 Tool Result 截断只影响读出后的文本，不会减少读取前已经占用的磁盘 | 需要时在捕获层增加大小上限并在超限时截断 |
| 临时文件删除延后 | 孙进程继承捕获句柄时，Windows 会把临时文件的删除推迟到最后一个句柄关闭 | 与“不管理孙进程”是同一条边界，必要时由外部清理 |
| 取消的提交点边界 | 取消恰好落在写入过程中时，调用方收到 `CancelledError` 但事件已提交（6.6）；无自动重试，因此不是 at-least-once | 调用方重新读取 Stream，按 `event_id`/correlation/业务身份判断是否已落盘 |
| Model Attempt 证据上限 | 未闭合 Attempt 已按证据补 End（11.1），但 `unknown_after_crash` 只说明“无法证明”，且丢失的 `usage`/`finish_reason` 无法找回 | 需要精确计费时在 Provider 边界先落盘用量 |
| CLI 体验 | `chat` 已支持会话内连续输入，但仍无 token 流式输出、实时 Tool Call 时间线、执行前审批，也不能在 Turn 运行期间输入 | 在不破坏 Runtime 边界下扩展 Surface/UI 层 |
| 中断退出码 | 退出码由宿主 Shell 和 Python 信号处理决定；硬中断（Ctrl+Break/关闭控制台）实测为 `3221225786`，不会运行收敛代码 | 依赖启动时打印的 session_id 与崩溃恢复，不承诺统一退出码 |
| 模型调用中断 | 取消 OpenAI-Compatible 请求时会等待 HTTP Worker 收敛，最坏等到 `timeout_seconds` | 需要立即中止时改用可中断的 HTTP 客户端 |
| Shell 安全 | Policy 是黑名单 Guardrail，不是沙箱 | 容器/远程 Sandbox、能力审批 |
| OpenAI Provider | 非流式、无重试/Fallback/限流 | 在 LlmRuntime/Provider 边界扩展 |
| JSONL 扩展性 | read 仍全量扫描；非分布式 | Checkpoint、SQLite 或其他 EventStore |
| Patch 能力 | 精确文本替换，不解析 unified diff | 增加独立工具实现，不改变 Tool Runtime |
| Benchmark | 仅一个确定性简单案例 | 增加真实 Provider、失败恢复、复杂仓库案例 |
| 自动压缩 | 只有手动 Replacement | 未来 Context/Compaction Plugin |
| API 稳定性 | Alpha，协议可能演进 | v1.0 前建立兼容策略和 Upcaster |

## 17. 变更影响矩阵

| 修改区域 | 必查代码/测试 | 必须同步的本文章节 |
|---|---|---|
| Agent Loop / Continuation | `runtime/agent_loop.py`、`continuation.py`、E2E/取消测试 | 4、5、10、11、15、16 |
| Event/Store/Session | `api/events.py`、`session/*`、event/invariant/recovery 测试 | 5、6、7、11、12、15、16 |
| Provider/Request | `api/llm.py`、`llm/*`、`request_builder.py` | 7、8、13、15、16 |
| Tool/Policy/Middleware | `api/tools.py`、`tools/*` | 6、9、11、15、16 |
| CLI/.env | `cli/*`、`.env.example`、README、CLI tests | 1、3、13、15 |
| Verifier/Eval | `verification.py`、`evaluation/*` | 10、12、15、16 |
| Plugin/Multi-Agent Protocol | `api/plugins.py`、`agents.py`、`workspaces.py`、`kernel/*` | 2、3、4、14、15、16 |
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
