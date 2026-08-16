# TraceHarness Py：面向插件化与多 Agent 演进的 Python Harness 实施计划

**文档版本：** v2.0\
**制定日期：** 2026 年 8 月 16 日\
**建议语言：** Python 3.12+\
**第一阶段周期：** 8 周，完成可用于实习求职的单 Agent 版本\
**完整演进周期：** 约 16 周，完成插件、多 Agent 与差异化能力\
**项目暂定名：** TraceHarness Py\
**Python 包名建议：** `traceh`

---

# 一、执行摘要

本项目不应被定位成：

> 用 Python 完整翻译 DeepSeek Harness 的 TypeScript 源码。

更有价值的定位是：

> **一个事件溯源、插件可组合、多 Agent 可演进、能够验证真实执行结果的 Python Agent Runtime。**

第一阶段仍然实现一个完整但克制的 Coding Agent：

```text
用户输入任务
    ↓
模型读取代码
    ↓
调用工具修改文件
    ↓
执行测试
    ↓
记录完整运行轨迹
    ↓
通过外部测试验证任务结果
    ↓
支持恢复、重放、检查和评测
```

但从第一天开始，系统就应保证后续能够无痛增加：

```text
第三方工具插件
模型 Provider 插件
Prompt 与上下文插件
权限和审批插件
上下文压缩
长期记忆
MCP 工具
子 Agent
多 Agent 协作
Workflow
远程沙箱
Web UI
任务调度
OpenTelemetry
```

这些功能不能通过不断修改 `AgentLoop` 来实现。

本方案的核心原则是：

> **核心协议不可插拔，能力实现可插拔，策略可以组合，Agent 编排位于 Loop 之外。**

对应到各模块：

```text
Kernel       负责组合、作用域、生命周期和资源所有权
Session      负责保存事实
AgentLoop    负责最小控制流
Service      负责提供能力
Plugin       负责注册能力、策略和扩展
Supervisor   负责管理 Agent
Workflow     负责组织多个 Agent
Invariant    负责证明协议没有被破坏
```

DeepSeek Harness 当前将模型适配器、工具注册表、Session 和 Agent Loop 都组合为插件，并要求新行为附着在明确扩展点上；其 Session Log 是模型上下文、恢复和重放的共同来源。TraceHarness 会借鉴这些思想，但不会照搬 Cordis，也不会让所有东西都能随意替换。

---

# 二、我们与 DeepSeek Harness 的关系

## 2.1 借鉴的部分

本项目重点借鉴：

1. Append-only Session Event Log；
2. Session、Turn、Step 生命周期；
3. 模型可见内容必须能够从日志重建；
4. Service Definition、Provider、Consumer 的能力边界；
5. Tool Runtime 的统一执行流水线；
6. Agent Scope 与注册隔离；
7. 可逆的插件注册和资源清理；
8. 子 Agent 生命周期所有权；
9. 可执行的不变量；
10. 真实世界验证，而不是相信模型自述。

DeepSeek Harness 的工具流水线已经明确区分准入策略、单调 Guard、执行包装、后处理和最终观察；这说明工具不能仅仅被建模成一个函数表。

## 2.2 不照搬的部分

TraceHarness 不直接复刻：

- Cordis；
- 数量庞大的 npm Package；
- 所有组件均可动态替换的设计；
- 完整的 Web Client；
- Typert；
- HMR；
- Code Mode；
- 动态生成 Cordis 插件；
- DeepSeek Harness 当前的 API 和命名。

我们会采用更适合个人 Python 项目的策略：

```text
DeepSeek Harness：
Everything is a Plugin

TraceHarness：
Everything is Composable
Not Everything is Replaceable
```

也就是说：

| 内容                  | 是否允许插件替换 |
| ------------------- | -------- |
| Event Envelope 基本语义 | 否        |
| Session 序号和写入规则     | 否        |
| 生命周期闭合规则            | 否        |
| Scope 与资源所有权语义      | 否        |
| Hook 分发模式           | 否        |
| LLM Provider        | 是        |
| Tool                | 是        |
| Prompt Section      | 是        |
| Tool Policy         | 是        |
| Context Compactor   | 是        |
| Persistence Backend | 是        |
| Agent Provider      | 是        |
| Workflow Engine     | 是        |
| UI 和协议适配器           | 是        |

这能避免插件系统反过来破坏 Harness 最关键的正确性。

---

# 三、项目最终定位

推荐项目简介：

> **TraceHarness Py is an event-sourced, extensible Python runtime for building traceable, recoverable and multi-agent coding systems.**

中文描述：

> TraceHarness Py 是一个基于事件溯源的 Python Agent Runtime，支持可重建模型请求、统一工具治理、崩溃恢复、插件组合、Agent 作用域、多 Agent 生命周期管理和真实结果验证。

它应同时具备三种身份。

## 3.1 可使用的产品

用户能够实际运行：

```bash
traceh run ./project "修复登录 Bug，并运行测试"

traceh resume <session-id>

traceh inspect <session-id>

traceh replay <session-id>

traceh eval ./benchmarks/coding-basic
```

后续版本还支持：

```bash
traceh plugins list
traceh plugins doctor
traceh plugins enable git-tools

traceh agents tree
traceh agents send <agent-id> "继续检查数据库层"

traceh workflow run reviewer-swarm.yml
```

## 3.2 可解释的系统设计项目

项目能够回答：

- 为什么 `messages` 不能作为唯一状态？
- 如何重建模型实际收到的请求？
- 插件如何接入而不修改 Loop？
- 插件卸载时如何确保没有后台任务泄漏？
- 多 Agent 的父子关系、历史关系和通信关系为什么不能混在一起？
- Agent 写文件后崩溃，恢复时如何避免重复执行？
- 多个 Agent 同时修改工作区时如何避免互相覆盖？
- 如何验证 Agent 真的完成了任务？

## 3.3 可以持续演进的开源框架

第一版不是一次性 Demo。

它应有明确的稳定层：

```text
traceh.api
traceh.kernel
traceh.runtime
```

未来的扩展只依赖公开 API：

```text
traceh-plugin-openai
traceh-plugin-anthropic
traceh-plugin-mcp
traceh-plugin-git
traceh-plugin-docker
traceh-plugin-subagent
traceh-plugin-otel
```

---

# 四、设计哲学

## 4.1 一个事实只能有一个权威来源

禁止同时存在：

```text
runtime.state
session.state
agent.status
state_machine.state
ui_state
```

并且所有人都声称自己是真相。

建议：

```text
长期事实       → Event Log
当前运行投影   → State Projector
模型可见历史   → Surface Projector
副作用状态     → Effect Ledger
UI 状态        → Read Model
实时指标       → Telemetry
```

状态可以被缓存，但缓存必须能够从权威来源重建。

DeepSeek Harness 的 Agent Scope 设计也强调“每个独立事实只使用一个权威机制”，避免同时维护多套 reservation、sentinel、snapshot 和保护表。

## 4.2 Loop 只拥有不可替代的控制流

Loop 负责：

```text
领取输入
打开 Turn
打开 Step
组装请求
调用模型
执行工具
决定继续或结束
闭合 Step 和 Turn
```

Loop 不负责：

```text
模型重试策略
模型路由策略
上下文压缩
长期记忆
权限审批
沙箱实现
子 Agent
工作流
Telemetry
UI
插件发现
插件卸载
评测
```

## 4.3 插件不能直接修改 Loop 的内部变量

错误方式：

```python
runtime.current_messages.append(...)
runtime.should_continue = True
runtime.tools["bash"] = plugin_tool
```

正确方式：

```python
ctx.prompts.register(...)
ctx.tools.register(...)
ctx.tool_policies.register(...)
ctx.continuation.register_rule(...)
ctx.projectors.register(...)
```

插件接入的不是 Loop 内部，而是 Loop 调用的稳定 Service。

## 4.4 取消和销毁是两个阶段

```text
Can
cel：
请求当前工作停止

Dispose：
禁止新工作
请求已有工作停止
等待子任务退出
清理监听器
释放资源
从 Registry 移除
```

Dispose 返回时必须达到 quiescence，即系统真正安静下来。DeepSeek Harness 的防御性规则同样强调，清理不能只发出 kill 或 abort，还必须等待子工作退出。

## 4.5 所有模型可见内容都必须有来源

只要某段内容进入模型请求，就必须能回答：

```text
谁生成了它？
何时生成？
由哪个插件生成？
使用什么配置？
它对应哪些原始事件？
恢复时能否重新构造？
```

不能出现：

```python
messages.append(
    {"role": "system", "content": some_runtime_only_string}
)
```

但日志中完全没有它的来源。

---

# 五、总体架构

```text
┌──────────────────────────────────────────────────────┐
│                    Applications                      │
│ CLI / REST API / Web UI / SDK / ACP / Automation     │
└──────────────────────────┬───────────────────────────┘
                           │
┌──────────────────────────▼───────────────────────────┐
│              Composition & Plugin Plane              │
│ Plugin Manager / Profiles / Presets / Config / Scope │
└──────────────────────────┬───────────────────────────┘
                           │
┌──────────────────────────▼───────────────────────────┐
│                 Agent Control Plane                  │
│ AgentSupervisor / Inbox / Activation / Workflow      │
│ Budget / Ownership / Workspace Coordination          │
└──────────────────────────┬───────────────────────────┘
                           │
┌──────────────────────────▼───────────────────────────┐
│                   Agent Runtime                      │
│ AgentLoop / PromptRuntime / LlmRuntime / ToolRuntime  │
│ ContextRuntime / ContinuationRuntime / Verification  │
└──────────────────────────┬───────────────────────────┘
                           │
┌──────────────────────────▼───────────────────────────┐
│                    State Plane                       │
│ Session Log / Effect Ledger / Projectors / Recovery  │
│ Request Snapshot / Composition Snapshot              │
└──────────────────────────┬───────────────────────────┘
                           │
┌──────────────────────────▼───────────────────────────┐
│                  TraceHarness Kernel                 │
│ Service Registry / Scope / Typed Hooks / Lifespan    │
│ Owned Tasks / Activation Transaction / Invariants    │
└──────────────────────────┬───────────────────────────┘
                           │
┌──────────────────────────▼───────────────────────────┐
│                 Infrastructure Plane                 │
│ JSONL / SQLite / Local FS / Docker / Remote Sandbox  │
│ Model APIs / Credential Store / OpenTelemetry        │
└──────────────────────────────────────────────────────┘
```

---

# 六、稳定层次

系统需要明确哪些内容已经承诺稳定。

## 6.1 Level 0：内核不变量

最稳定，不能由插件改变：

```text
事件序号单调增长
事件一旦提交不能修改
Turn / Step 必须闭合
Tool Call 与 Result 必须相关联
作用域所有权必须明确
插件卸载必须释放注册
Dispose 必须等待 quiescence
一个 Session 同时只有一个合法写入者
```

## 6.2 Level 1：公开协议

发布后需要兼容：

```text
EventStore Protocol
LlmProvider Protocol
Tool Protocol
Plugin Protocol
AgentHandle Protocol
AgentSupervisor Protocol
WorkspaceProvider Protocol
Hook 类型和分发语义
事件 Envelope
错误分类
```

Python 的 `typing.Protocol` 支持结构化子类型，第三方实现不必继承框架内部基类，非常适合定义 Provider 和插件边界。

## 6.3 Level 2：默认实现

允许替换：

```text
DefaultAgentLoop
JsonlEventStore
SqliteEventStore
LocalWorkspaceProvider
OpenAICompatibleProvider
DefaultToolScheduler
DefaultContinuationRuntime
```

## 6.4 Level 3：插件和应用

变化最快：

```text
Coding Tools
Git Tools
MCP Tools
Prompt Presets
UI
Workflow
Subagent Strategy
Telemetry
```

---

# 七、Python 技术决策

## 7.1 Python 版本

建议：

```toml
requires-python = ">=3.12"
```

CI 测试：

```text
Python 3.12
Python 3.13
Python 3.14
```

## 7.2 核心技术选择

| 场景         | 推荐方案                                  |
| ---------- | ------------------------------------- |
| 异步 Runtime | `asyncio`                             |
| 结构化任务所有权   | `asyncio.TaskGroup`                   |
| 接口协议       | `typing.Protocol`                     |
| 不可变内部 DTO  | `@dataclass(frozen=True, slots=True)` |
| 执行上下文追踪    | `contextvars.ContextVar`              |
| 插件发现       | `importlib.metadata.entry_points()`   |
| 外部配置校验     | Pydantic v2                           |
| 单元测试       | pytest                                |
| 异步测试       | pytest-asyncio                        |
| 属性测试       | Hypothesis                            |
| 类型检查       | Pyright strict 或 mypy strict          |
| Lint 与格式化  | Ruff                                  |
| 第一版持久化     | JSONL                                 |
| 第二个持久化后端   | SQLite                                |

`asyncio.TaskGroup` 提供结构化并发语义，但协程不应吞掉 `CancelledError`，否则会破坏 TaskGroup 和 timeout 的取消行为。

`ContextVar` 原生支持 `asyncio`，适合携带 `trace_id`、`agent_id`、`turn_id` 和 `step_id`；但它不应被当作隐藏的依赖注入容器。Service 仍应通过显式 `RuntimeContext` 传递。

## 7.3 内部不可变，边界再校验

内部领域对象：

```python
from dataclasses import dataclass
from uuid import UUID


@dataclass(frozen=True, slots=True)
class StepRef:
    session_id: UUID
    turn_id: UUID
    step_id: UUID
```

`frozen=True` 可以阻止普通字段赋值，`slots=True` 能限制实例属性集合，适合表达运行快照和事件 Envelope。

推荐规则：

```text
同进程可信边界：
dataclass + Protocol + readonly collections

文件、网络、插件配置边界：
Pydantic 校验并转换为内部对象

Session Log：
只允许 JSON 可序列化数据
```

---

# 八、代码组织

第一阶段不需要发布十几个独立 Wheel，但代码路径必须从第一天就分层。

```text
trace-harness/
├── src/
│   └── traceh/
│       ├── api/
│       │   ├── events.py
│       │   ├── ids.py
│       │   ├── json_types.py
│       │   ├── plugins.py
│       │   ├── services.py
│       │   ├── hooks.py
│       │   ├── agents.py
│       │   ├── tools.py
│       │   └── llm.py
│       │
│       ├── kernel/
│       │   ├── registry.py
│       │   ├── scope.py
│       │   ├── hooks.py
│       │   ├── activation.py
│       │   ├── lifespan.py
│       │   ├── tasks.py
│       │   ├── composition.py
│       │   └── invariants.py
│       │
│       ├── session/
│       │   ├── service.py
│       │   ├── event_store.py
│       │   ├── jsonl.py
│       │   ├── sqlite.py
│       │   ├── surface.py
│       │   ├── projections.py
│       │   ├── recovery.py
│       │   └── effect_ledger.py
│       │
│       ├── runtime/
│       │   ├── agent_loop.py
│       │   ├── agent_runtime.py
│       │   ├── request_builder.py
│       │   ├── continuation.py
│       │   ├── context_runtime.py
│       │   └── verification.py
│       │
│       ├── agents/
│       │   ├── supervisor.py
│       │   ├── activation.py
│       │   ├── inbox.py
│       │   ├── ownership.py
│       │   ├── budget.py
│       │   └── local_provider.py
│       │
│       ├── llm/
│       │   ├── runtime.py
│       │   ├── registry.py
│       │   ├── scripted.py
│       │   ├── replay.py
│       │   └── openai_compatible.py
│       │
│       ├── tools/
│       │   ├── runtime.py
│       │   ├── registry.py
│       │   ├── policy.py
│       │   ├── scheduler.py
│       │   ├── results.py
│       │   └── builtins/
│       │
│       ├── plugins/
│       │   ├── manager.py
│       │   ├── discovery.py
│       │   ├── manifest.py
│       │   ├── dependency_graph.py
│       │   └── testkit.py
│       │
│       ├── workflow/
│       │   ├── engine.py
│       │   ├── models.py
│       │   └── local.py
│       │
│       ├── evaluation/
│       │   ├── runner.py
│       │   ├── verifier.py
│       │   └── report.py
│       │
│       └── cli/
│           ├── run.py
│           ├── resume.py
│           ├── inspect.py
│           ├── replay.py
│           ├── plugins.py
│           ├── agents.py
│           └── eval.py
│
├── examples/
│   ├── hello_plugin/
│   ├── git_tools_plugin/
│   └── reviewer_swarm/
│
├── benchmarks/
├── fixtures/
├── tests/
│   ├── unit/
│   ├── contract/
│   ├── golden_traces/
│   ├── recovery/
│   ├── plugin/
│   ├── multi_agent/
│   └── e2e/
│
├── docs/
├── pyproject.toml
└── README.md
```

## 8.1 强制依赖方向

```text
traceh.api
    ↑
traceh.kernel
    ↑
traceh.session
    ↑
traceh.runtime
    ↑
applications
```

插件只能依赖：

```text
traceh.api
traceh.plugins 的公开 SDK
```

禁止第三方插件导入：

```python
from traceh.runtime.agent_loop import _LoopState
from traceh.kernel.registry import _MutableLayer
```

建议增加架构测试，检查非法 Import。

---

# 九、核心领域模型

## 9.1 生命周期层次

```text
Session
└── Turn
    └── Step
        ├── Model Attempt
        └── Tool Invocation
            └── Effect Attempt
```

### Session

长期、可恢复的 Agent 会话。

### Turn

一次外部唤醒开始，到 Agent 再次自然停止。

### Step

一次逻辑模型请求，以及该回复产生的工具调用。

### Model Attempt

一次实际 Provider 请求。

第一版：

```text
一个 Step 只有一个 Model Attempt
```

后续支持：

```text
请求失败
  ↓
同 Provider 重试
  ↓
切换 Provider
  ↓
仍属于同一个 Step 的多个 Attempt
```

这样未来增加重试和模型回退时，不需要重新定义 Step。

### Tool Invocation

模型请求的一次工具调用。

### Effect Attempt

工具对外部世界进行的一次实际副作用尝试。

---

# 十、事件架构：三条轨道

这是 TraceHarness 相比普通 Agent 框架最重要的差异化设计之一。

## 10.1 Durable Session Stream

记录 Agent 对话和生命周期事实：

```text
session/created

inbox/accepted
inbox/claimed
inbox/discarded

turn/start
turn/end

step/start
step/end

user/message

request/snapshot
composition/snapshot

model/attempt-start
assistant/chunk
assistant/message
model/attempt-end

tool/call
tool/result

surface/replace

agent/recovered
runtime/error
```

## 10.2 Effect Ledger

专门记录副作用：

```text
effect/intent
effect/dispatched
effect/outcome
effect/reconciled
```

它回答：

```text
Agent 想做什么？
副作用是否真正发出？
外部世界是否已经变化？
系统崩溃时处于哪个窗口？
恢复后如何判断是否应该重试？
```

## 10.3 Telemetry Stream

记录：

```text
延迟
Token
CPU
内存
工具耗时
队列长度
插件错误
Trace Span
```

Telemetry 不是事实源。

即使 Telemetry 丢失，也不能影响 Session 恢复。

## 10.4 为什么要分三条轨道

如果所有内容都塞进一个 Event Log：

```text
每个 Token
每个指标
每个 Span
每个工具副作用
每个 UI 状态
```

日志会迅速膨胀，而且不同事件的正确性要求完全不同。

三者的语义为：

| 轨道             | 是否权威 | 是否必须恢复 | 是否允许采样 |
| -------------- | ---: | -----: | -----: |
| Session Stream |    是 |      是 |      否 |
| Effect Ledger  |    是 |      是 |      否 |
| Telemetry      |    否 |      否 |      是 |

---

# 十一、Event Envelope

```python
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID


JsonValue = (
    None
    | bool
    | int
    | float
    | str
    | list["JsonValue"]
    | dict[str, "JsonValue"]
)


@dataclass(frozen=True, slots=True)
class EventEnvelope:
    event_id: UUID
    stream_id: str
    seq: int

    type: str
    schema_version: int
    data: dict[str, JsonValue]

    occurred_at: datetime

    causation_id: UUID | None = None
    correlation_id: UUID | None = None

    actor_id: str | None = None
    composition_revision: str | None = None
```

## 11.1 字段语义

### `event_id`

全局唯一身份。

### `stream_id`

例如：

```text
session:<session-id>
effects:<session-id>
workflow:<workflow-run-id>
```

### `seq`

该 Stream 内严格连续或严格递增。

### `causation_id`

哪个事件直接导致了这个事件。

例如：

```text
tool/call event_id
    ↓
effect/intent.causation_id
```

### `correlation_id`

一次跨 Agent、跨工具、跨工作流任务的关联 ID。

### `composition_revision`

记录当前使用的是哪一代插件和能力组合。

---

# 十二、事件版本演进

插件系统上线后，事件格式一定会演进。

从第一天加入：

```text
event.type
event.schema_version
```

## 12.1 Upcaster

读取旧事件时：

```text
v1 payload
   ↓ upcast
v2 payload
   ↓
当前领域对象
```

不原地重写旧日志。

## 12.2 插件事件命名空间

核心事件：

```text
turn/start
tool/call
```

插件事件：

```text
com.example.git/branch-created
traceh.compaction/summary
traceh.subagent/child-started
```

## 12.3 缺失插件时的处理

未知插件事件不能导致整个 Harness 无法启动。

应被加载成：

```python
@dataclass(frozen=True, slots=True)
class OpaquePluginEvent:
    type: str
    schema_version: int
    raw_data: dict[str, JsonValue]
```

核心 Projector 可以忽略它。

需要该插件的专属 Projection 则显示：

```text
Projection unavailable:
plugin traceh.git is not installed
```

## 12.4 损坏隔离

一个损坏 Session 应被标记为：

```text
HEALTHY
RECOVERABLE
QUARANTINED
UNSUPPORTED_VERSION
```

它不能让整个 Workspace 启动失败。

---

# 十三、Session、State 和 Surface

## 13.1 Event Log 是事实

禁止：

```python
agent.messages.append(...)
agent.state = AgentState.RUNNING
```

建议：

```python
await session.append("turn/start", ...)
state = state_projector.project(events)
messages = surface_projector.project(events)
```

DeepSeek Harness 也将 Session 定义为 append-only 的唯一事实源，并从中派生模型历史，而不是单独保存另一份 messages。

## 13.2 State Projector

```python
class AgentStateProjector:
    def project(self, events: Sequence[EventEnvelope]) -> AgentState:
        ...
```

生成：

```text
IDLE
RUNNING
WAITING
CANCELLING
FAILED
DISPOSED
```

注意：

```text
持久化 Session 状态
≠
进程内 Agent Activation 状态
```

## 13.3 Surface Projector

生成模型可见历史：

```text
用户消息
Assistant 最终消息
工具调用
工具结果
摘要替代
注入上下文
```

不包含：

```text
Telemetry
原始 Trace Span
插件安装日志
UI 状态
```

## 13.4 Surface Replacement

压缩时追加：

```text
surface/replace
```

声明：

```text
替代了哪些原始事件
替代内容是什么
由哪个插件生成
使用什么算法和模型
```

原事件永远不删除。

---

# 十四、Request Snapshot 与 Composition Snapshot

DeepSeek Harness 已经记录请求配置、系统提示词和工具 Schema，从而让请求成为日志的函数。

TraceHarness 在此基础上再增加一个 **Composition Snapshot**。

## 14.1 Request Snapshot

```python
@dataclass(frozen=True, slots=True)
class RequestSnapshot:
    provider: str
    model: str

    system_prompt: str | None
    messages: tuple[ModelMessage, ...]
    tools: tuple[ToolSchema, ...]

    temperature: float | None
    max_output_tokens: int | None

    credential_ref: str | None
```

日志里只能保存 `credential_ref`，绝不能保存真实 API Key。

## 14.2 Composition Snapshot

```python
@dataclass(frozen=True, slots=True)
class CompositionSnapshot:
    revision: str

    plugins: tuple[PluginIdentity, ...]
    services: tuple[ServiceIdentity, ...]
    tools: tuple[ToolIdentity, ...]
    prompt_sections: tuple[PromptSectionIdentity, ...]
    policies: tuple[PolicyIdentity, ...]
    projectors: tuple[ProjectorIdentity, ...]
```

它记录：

```text
本 Step 使用了哪些插件
每个插件的版本
哪些工具可见
使用哪些权限策略
使用哪个模型 Adapter
使用哪一代配置
```

## 14.3 Request Fingerprint

对规范化请求生成：

```text
SHA-256(
    canonical_json(request_snapshot)
    + composition_revision
)
```

用于：

- 重建一致性测试；
- Replay；
- 评测对比；
- 插件升级前后差异分析；
- Bug 报告去重。

这比只保存模型和消息更适合插件化系统。

---

# 十五、TraceHarness Kernel

Kernel 是整个项目最需要稳定的部分。

它只负责五件事：

```text
Service Registry
Scope
Typed Hook Dispatcher
Plugin Activation Lifecycle
Owned Task Lifecycle
```

Kernel 不知道：

```text
Prompt 是什么
LLM 是什么
Tool 是什么
Agent 是什么
Coding Agent 是什么
```

---

# 十六、Service Protocol

```python
from dataclasses import dataclass
from typing import Generic, Protocol, TypeVar


T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class ServiceKey(Generic[T]):
    name: str
    api_major: int


class Registration(Protocol):
    async def dispose(self) -> None:
        ...


class PluginContext(Protocol):
    def require(self, key: ServiceKey[T]) -> T:
        ...

    def provide(
        self,
        key: ServiceKey[T],
        service: T,
        *,
        replace: bool = False,
    ) -> Registration:
        ...

    def add_cleanup(
        self,
        callback: "AsyncCleanup",
    ) -> Registration:
        ...

    def spawn_owned(
        self,
        coroutine: "CoroutineLike",
        *,
        name: str,
    ) -> "OwnedTask":
        ...
```

## 16.1 ServiceKey 必须带 API Major

```python
LLM_RUNTIME_V1 = ServiceKey[LlmRuntime](
    name="traceh.llm",
    api_major=1,
)
```

未来出现重大不兼容时：

```text
traceh.llm@1
traceh.llm@2
```

可以短期并存，而不是让所有插件同时升级。

## 16.2 服务调用与事件的选择

使用规则：

```text
明确调用某个能力：
Service Method

观察已发生的事情：
Notification Hook

拦截或变换一次操作：
Transform / Gate / Around Hook

记录需要恢复的事实：
Durable Event
```

不要所有通信都使用 EventBus。

---

# 十七、Scope 设计

## 17.1 Scope 层次

```text
Application Scope
    ↓
Workspace Scope
    ↓
Preset Scope
    ↓
Agent Scope
    ↓
Step Composition Snapshot
```

### Application Scope

全局默认 Provider、Telemetry、持久化。

### Workspace Scope

工作区工具、安全策略、仓库指令。

### Preset Scope

Coder、Reviewer、Researcher 等 Agent 预设。

### Agent Scope

单个 Agent 的临时覆盖和专属资源。

### Step Snapshot

Step 开始时冻结的有效能力视图。

## 17.2 解析规则

```text
距离 Agent 最近的层优先
同一层重复注册默认报错
覆盖必须显式声明 replace=True
覆盖必须满足 API Major
```

## 17.3 子 Agent 不自动继承父 Agent Scope

假设：

```text
Application
└── Workspace
    └── Coder Preset
        └── Parent Agent
            └── Parent Agent 临时注册了 write_database
```

创建 Reviewer Child 时，不应该得到：

```text
Parent Agent 的 write_database
```

Child 的组合应为：

```text
Application
└── Workspace
    └── Reviewer Preset
        └── Child Agent
```

父 Agent 对子 Agent 的生命周期所有权，不等于能力继承。

DeepSeek Harness 的 Agent Scope 同样强调 Agent 之间的局部注册隔离，父 Agent 的局部注册不会仅因父子生命周期关系自动进入 Child 的视图。

## 17.4 Step 级冻结

插件可能在运行中启用、禁用或升级。

不能让一个 Step 出现：

```text
组装 Prompt 时看到 Tool v1
执行 Tool 时使用 Tool v2
结果渲染时插件已卸载
```

因此 Step 开始时获取：

```python
async with composition.lease(agent.scope) as snapshot:
    ...
```

该 Step 内始终使用同一代能力。

插件更新只影响下一个 Step。

---

# 十八、Typed Hook 系统

不要实现一个万能的：

```python
event_bus.emit("anything", mutable_object)
```

Hook 的分发语义必须是公开协议。

## 18.1 五种 Hook 模式

| 模式              | 用途                 | 返回值 | 错误策略        |
| --------------- | ------------------ | --: | ----------- |
| `NOTIFY`        | 观察事实               |   无 | 隔离并报告       |
| `TRANSFORM`     | 顺序生成新值             |   有 | 失败当前操作      |
| `GATE`          | Allow / Ask / Deny |   有 | 默认安全失败      |
| `AROUND`        | 包装执行               |   有 | 失败当前操作      |
| `SERIAL_ACTION` | 顺序执行动作             |  可选 | 按 Hook 合约决定 |

## 18.2 NOTIFY

用于：

```text
UI
日志
Telemetry
调试器
```

一个订阅者抛异常不能阻止其他订阅者。

## 18.3 TRANSFORM

输入不可原地修改：

```python
new_request = await hooks.transform(
    REQUEST_ASSEMBLED,
    old_request,
)
```

每个 Handler 返回新对象。

## 18.4 GATE

用于：

```text
工具权限
路径政策
预算
沙箱政策
```

聚合规则：

```text
Hard Deny 永远不能被后续插件重新 Allow
Ask 高于普通 Allow
所有插件 Defer 时使用默认策略
```

## 18.5 AROUND

用于：

```text
模型重试
模型 Fallback
工具超时
工具指标
缓存
```

```python
async def middleware(invocation, call_next):
    return await call_next(invocation)
```

Kernel 必须检查：

```text
call_next 最多调用一次
Middleware 超时
Cancellation 正确传播
```

---

# 十九、插件不能随意添加 Hook

插件只能注册到框架预定义的 Hook。

建议第一版公开以下 Hook：

```text
agent.input_admit
prompt.assemble
model.invoke
model.chunk
tool.authorize
tool.invoke
tool.finalize
step.completed
turn.stopping
agent.status
```

新增公共 Hook 需要 ADR，因为它会成为长期兼容承诺。

---

# 二十、AgentLoop 设计

## 20.1 主循环伪代码

```python
class DefaultAgentLoop:
    async def run_turn(
        self,
        agent: AgentHandle,
        first_message: InboxMessage,
    ) -> TurnResult:
        turn = await self.sessions.begin_turn(agent.id)

        try:
            pending = (first_message,)

            while True:
                admission = await self.input_runtime.admit(
                    agent=agent,
                    turn=turn,
                    messages=pending,
                )

                if admission.rejected:
                    return await self.sessions.end_turn(
                        turn,
                        reason=admission.reason,
                    )

                step = await self.sessions.begin_step(turn)

                async with self.composition.lease(
                    agent.scope,
                ) as composition:
                    await self.sessions.append_messages(
                        step,
                        admission.messages,
                    )

                    context = await self.context_runtime.prepare(
                        step=step,
                        composition=composition,
                    )

                    request = await self.request_builder.build(
                        step=step,
                        context=context,
                        composition=composition,
                    )

                    response = await self.llm.invoke(
                        request=request,
                        step=step,
                    )

                    tool_results = await self.tools.execute_batch(
                        calls=response.tool_calls,
                        step=step,
                        composition=composition,
                    )

                outcome = await self.sessions.end_step(
                    step=step,
                    response=response,
                    tool_results=tool_results,
                )

                directive = await self.continuation.decide(
                    agent=agent,
                    outcome=outcome,
                )

                match directive:
                    case Continue(next_messages):
                        pending = next_messages
                    case Finish(reason):
                        return await self.sessions.end_turn(
                            turn,
                            reason=reason,
                        )
                    case Suspend(waiting_for):
                        return await self.sessions.suspend_turn(
                            turn,
                            waiting_for=waiting_for,
                        )
                    case Fail(error):
                        raise error

        except BaseException as error:
            await self.sessions.fail_turn(turn, error)
            raise
```

## 20.2 这个 Loop 为什么可以长期稳定

它不知道：

```text
上下文压缩怎么做
工具审批怎么做
模型失败后怎么切换
子 Agent 怎么创建
Verifier 怎么执行
工作流怎么调度
插件怎么安装
```

它只调用稳定的 Service。

## 20.3 未来功能如何接入

| 功能               | 接入方式                             | 是否修改 Loop |
| ---------------- | -------------------------------- | --------: |
| 新模型              | 注册 `LlmProvider`                 |         否 |
| 模型重试             | `model.invoke` Around Middleware |         否 |
| 模型 Fallback      | LLM Router Plugin                |         否 |
| 新工具              | Tool Registry                    |         否 |
| 权限审批             | Tool Gate Plugin                 |         否 |
| 沙箱               | Workspace / Subprocess Provider  |         否 |
| 上下文压缩            | Context Policy Plugin            |         否 |
| 长期记忆             | Projector + Prompt Section       |         否 |
| MCP              | MCP Tool Provider Plugin         |         否 |
| 子 Agent          | Subagent Tool + AgentSupervisor  |         否 |
| 多 Agent Workflow | WorkflowEngine                   |         否 |
| 自动验证             | Continuation Rule + Verifier     |         否 |
| UI               | 订阅 Session Event                 |         否 |
| Telemetry        | NOTIFY Hook                      |         否 |
| 定时任务             | Scheduler 调用 AgentSupervisor     |         否 |

只有当“Session、Turn、Step 的基本控制语义”发生变化时，才应修改 Loop。

---

# 二十一、Continuation Runtime

不要在 Loop 中不断增加：

```python
if has_tool_calls:
    ...
elif has_pending_approval:
    ...
elif should_compact:
    ...
elif verifier_failed:
    ...
elif child_running:
    ...
```

统一为：

```python
class ContinuationRuntime(Protocol):
    async def decide(
        self,
        *,
        agent: AgentHandle,
        outcome: StepOutcome,
    ) -> LoopDirective:
        ...
```

插件注册 `ContinuationRule`：

```text
Pending Tool Result Rule
Injected Input Rule
Budget Rule
Verification Rule
Goal Rule
Max Steps Rule
Default Finish Rule
```

聚合器拥有确定性优先级。

建议优先级：

```text
Fail
  >
Suspend
  >
Hard Stop
  >
Must Continue
  >
Default Finish
```

插件不能自行写 `runtime.should_continue`。

---

# 二十二、Tool Runtime

## 22.1 Tool 接口

```python
class Tool(Protocol):
    name: str
    description: str
    input_schema: dict[str, JsonValue]

    effect_kind: "EffectKind"

    async def execute(
        self,
        arguments: dict[str, JsonValue],
        context: "ToolExecutionContext",
    ) -> "ToolOutput":
        ...
```

## 22.2 Effect Kind

```text
PURE_READ
WORKSPACE_READ
WORKSPACE_WRITE
PROCESS
NETWORK_WRITE
EXTERNAL_TRANSACTION
```

Effect Kind 会影响：

```text
权限
调度
重试
恢复
审批
证据要求
```

## 22.3 执行流水线

```text
查找 Tool
   ↓
解析参数
   ↓
校验 Schema
   ↓
规范化路径和资源声明
   ↓
Policy Gate
   ↓
Monotonic Guards
   ↓
预算预留
   ↓
写入 effect/intent
   ↓
获得资源锁
   ↓
Around Middleware
   ↓
Tool.execute()
   ↓
捕获独立结果维度
   ↓
写入 effect/outcome
   ↓
Result Transform
   ↓
输出裁剪与脱敏
   ↓
写入 tool/result
   ↓
释放资源和预算
```

## 22.4 独立结果维度

不要只返回：

```python
success: bool
```

应返回：

```python
@dataclass(frozen=True, slots=True)
class ProcessOutcome:
    exit_code: int | None
    signal: str | None
    timed_out: bool
    cancelled: bool
    stdout: str
    stderr: str
```

一个进程可能：

```text
发生超时
同时捕获信号
最后仍退出 0
```

这些事实必须分别表达。DeepSeek Harness 的防御性规则也明确要求正交结果独立报告。

---

# 二十三、资源声明与并发

第一版可以使用：

```text
read 工具并行
write / process 工具排他
```

后续升级为 Resource Claim：

```python
@dataclass(frozen=True, slots=True)
class ResourceClaim:
    namespace: str
    key: str
    mode: Literal["read", "write"]
```

例子：

```text
read_file("a.py")
→ workspace-file:a.py / read

apply_patch("a.py")
→ workspace-file:a.py / write

pytest
→ workspace / read
→ process-slot / write

git checkout
→ workspace / write
```

Tool Scheduler 根据 Claim 决定并发。

以后多 Agent 也复用同一个 Resource Coordinator，不需要再实现第二套锁系统。

---

# 二十四、我们自己的创新一：Effect Ledger

普通 Harness 通常只有：

```text
tool/call
tool/result
```

但这无法准确表达：

```text
工具已开始执行
副作用已发生
结果还没写入日志
进程突然崩溃
```

## 24.1 Effect Intent

执行副作用前，先持久化：

```python
@dataclass(frozen=True, slots=True)
class EffectIntent:
    effect_id: UUID
    tool_call_id: UUID

    kind: EffectKind
    operation_fingerprint: str
    idempotency_key: str | None

    preconditions: dict[str, JsonValue]
    resources: tuple[ResourceClaim, ...]
```

## 24.2 Effect Outcome

```python
@dataclass(frozen=True, slots=True)
class EffectOutcome:
    effect_id: UUID

    status: Literal[
        "succeeded",
        "failed",
        "cancelled",
        "unknown",
    ]

    evidence_refs: tuple[str, ...]
    error: ErrorRecord | None
```

## 24.3 恢复规则

| Effect        | 恢复策略                     |
| ------------- | ------------------------ |
| 纯读取           | 安全重试                     |
| 有幂等键的 API 写入  | 查询幂等键结果                  |
| 文件修改          | 比对文件 Hash、Patch 和前置版本    |
| Git Commit    | 查询 Commit ID             |
| Shell Process | 默认状态未知                   |
| 外部交易          | 必须由专用 Reconciler 判断      |
| 无法判断          | 标记 `UNKNOWN_AFTER_CRASH` |

## 24.4 Reconciler

插件可以注册：

```python
class EffectReconciler(Protocol):
    async def reconcile(
        self,
        intent: EffectIntent,
        world: WorldProbe,
    ) -> EffectOutcome:
        ...
```

这是 TraceHarness 最适合在面试中展示的差异化能力。

---

# 二十五、我们自己的创新二：Evidence-Driven Completion

Agent 说：

> 已经修复，测试通过。

不代表真的完成。

## 25.1 Completion Contract

```python
@dataclass(frozen=True, slots=True)
class CompletionContract:
    required_checks: tuple[VerificationCheck, ...]
    allowed_files: tuple[str, ...]
    forbidden_files: tuple[str, ...]
```

## 25.2 Evidence Bundle

```python
@dataclass(frozen=True, slots=True)
class EvidenceBundle:
    test_runs: tuple[TestRunEvidence, ...]
    file_hashes: tuple[FileHashEvidence, ...]
    patches: tuple[PatchEvidence, ...]
    commands: tuple[CommandEvidence, ...]
```

## 25.3 Verification Rule

Agent 准备 Finish 时：

```text
模型给出最终回答
   ↓
ContinuationRuntime 准备 Finish
   ↓
CompletionVerifier 执行
   ↓
验证通过 → Finish
验证失败 → 注入结构化反馈 → Continue
预算耗尽 → Finish(unverified)
```

Verifier 不是写进 Loop 的特殊分支，而是 Continuation Runtime 的一条规则。

---

# 二十六、我们自己的创新三：Composition Provenance

每个 Step 都记录：

```text
插件版本
服务 Provider
工具 Schema
Prompt Section
权限策略
模型 Adapter
配置 Revision
```

因此可以回答：

```text
为什么昨天能执行 bash，今天不能？
哪个插件改变了 Prompt？
哪个权限策略拒绝了工具？
升级插件后成功率为什么下降？
```

Inspector 可展示：

```text
Step 12
├── Model: openai-compatible:gpt-x
├── Prompt sections:
│   ├── harness.identity@1
│   ├── coding.workspace@2
│   └── plugin.git.instructions@1
├── Tools:
│   ├── read_file@1
│   ├── apply_patch@2
│   └── shell@1
└── Policies:
    ├── workspace-boundary@1
    ├── shell-approval@3
    └── budget-policy@1
```

---

# 二十七、插件系统

## 27.1 插件发现

Python 标准打包体系的 Entry Point 允许已安装 Distribution 声明可被应用发现的组件，运行时可以通过 `importlib.metadata.entry_points()` 查询。

插件的 `pyproject.toml`：

```toml
[project]
name = "traceh-plugin-git"
version = "0.1.0"
dependencies = [
  "traceh>=0.4,<1.0"
]

[project.entry-points."traceh.plugins"]
git = "traceh_plugin_git.plugin:plugin"
```

## 27.2 Plugin Protocol

```python
from collections.abc import Mapping
from typing import Protocol


class Plugin(Protocol):
    manifest: "PluginManifest"

    async def setup(
        self,
        ctx: PluginContext,
        config: Mapping[str, JsonValue],
    ) -> None:
        ...
```

## 27.3 Plugin Manifest

```python
@dataclass(frozen=True, slots=True)
class PluginManifest:
    plugin_id: str
    version: str

    requires_traceh: str
    requires_plugins: tuple[PluginDependency, ...]
    optional_plugins: tuple[PluginDependency, ...]

    allowed_scopes: tuple[str, ...]
    trust_mode: Literal["trusted", "isolated"]

    provides: tuple[str, ...]
    event_types: tuple[EventTypeDeclaration, ...]
```

## 27.4 不在 Entry Point 中直接执行插件

发现过程：

```text
读取 Entry Point Metadata
    ↓
加载 Manifest
    ↓
检查 TraceHarness API 版本
    ↓
解析依赖图
    ↓
验证配置
    ↓
再导入并执行 setup()
```

---

# 二十八、插件激活事务

插件加载不能一边注册一边向整个 Runtime 暴露。

正确流程：

```text
1. 创建私有 Activation Scope
2. 检查依赖
3. 验证配置
4. 调用 setup()
5. 收集 Service、Hook、Tool 和 Cleanup
6. 运行冲突检查
7. 运行插件自检
8. 原子发布新 Composition Generation
9. 对外可见
```

任何一步失败：

```text
反向释放所有已注册 Effect
取消 Owned Tasks
调用 Cleanup
不发布半完成状态
```

DeepSeek Harness 的 Agent Scope 也采用“Setup 完成后再发布”的原则，避免其他组件看到半初始化 Agent。

---

# 二十九、插件卸载和热更新

## 29.1 生命周期

```text
DISCOVE
RED
VALIDATING
STARTING
ACTIVE
DRAINING
STOPPING
STOPPED
FAILED
```

## 29.2 卸载流程

```text
标记 DRAINING
   ↓
新 Step 不再获得该插件
   ↓
已有 Step 继续持有旧 Generation Lease
   ↓
等待所有 Lease 释放
   ↓
停止 Owned Tasks
   ↓
等待任务退出
   ↓
反向执行 Cleanup
   ↓
移除注册
```

## 29.3 热更新

```text
Plugin v1 ACTIVE
    ↓
Plugin v2 在私有 Scope Setup
    ↓
v2 校验通过
    ↓
Composition Generation 原子切换
    ↓
新 Step 使用 v2
旧 Step 继续使用 v1
    ↓
v1 Drain 并卸载
```

第一版不必实现热更新，但必须使用 Generation Snapshot，以免未来需要重写所有 Registry。

---

# 三十、插件信任边界

## 30.1 In-Process Plugin

```text
性能高
类型调用简单
可访问宿主 Python 进程
不能被视为安全沙箱
```

必须明确：

> Python 进程内插件一旦被导入，就有能力访问文件、环境变量和网络。Scope 和权限系统只用于架构治理，不能形成真正的安全隔离。

## 30.2 Isolated Plugin

后续支持：

```text
独立 Python 进程
JSON-RPC / MessagePack RPC
显式 Capability Token
受控环境变量
受控文件系统
超时和进程回收
```

Plugin Manifest 声明：

```text
trust_mode = isolated
```

插件管理器选择 Process Plugin Host。

---

# 三十一、插件示例

```python
from traceh.api import PluginManifest
from traceh.sdk import tool


@tool(
    name="git_status",
    description="Read the repository status.",
    effect_kind="WORKSPACE_READ",
)
async def git_status(arguments, context):
    shell = context.services.require("traceh.shell", api_major=1)
    result = await shell.run(
        ["git", "status", "--short"],
        cwd=context.workspace.root,
    )
    return {
        "content": result.stdout,
        "exit_code": result.exit_code,
    }


class GitPlugin:
    manifest = PluginManifest(
        plugin_id="traceh.git",
        version="0.1.0",
        requires_traceh=">=0.4,<1.0",
        requires_plugins=(),
        optional_plugins=(),
        allowed_scopes=("workspace", "agent"),
        trust_mode="trusted",
        provides=("tool:git_status",),
        event_types=(),
    )

    async def setup(self, ctx, config):
        ctx.tools.register(git_status)
```

AgentLoop 不需要出现：

```python
if git_plugin_enabled:
    ...
```

---

# 三十二、多 Agent 总体设计

多 Agent 不是：

```python
await asyncio.gather(
    agent.run(...),
    agent.run(...),
)
```

真正需要解决：

```text
身份
Session
Inbox
生命周期
所有权
权限
预算
工作区冲突
消息顺序
取消
恢复
父子关系
结果归属
```

---

# 三十三、AgentSupervisor

```python
class AgentSupervisor(Protocol):
    async def create(
        self,
        spec: "AgentSpec",
    ) -> "AgentHandle":
        ...

    async def resume(
        self,
        session_id: str,
    ) -> "AgentHandle":
        ...

    async def send(
        self,
        agent_id: str,
        message: "AgentMessage",
        *,
        target: "MessageTarget",
        wakeup: bool,
    ) -> "MessageReceipt":
        ...

    async def interrupt(
        self,
        agent_id: str,
        reason: str,
    ) -> None:
        ...

    async def wait_idle(
        self,
        agent_id: str,
    ) -> None:
        ...

    async def dispose(
        self,
        agent_id: str,
    ) -> None:
        ...
```

## 33.1 Agent 的两个存在形态

```text
Durable Agent
=
Session + Header + Inbox History

Live Agent
=
Agent Activation + AgentHandle + Tasks
```

一个 Agent 可以：

```text
Session 存在
但当前没有 Live Activation
```

发送新消息时冷恢复。

DeepSeek Harness 的可继续子 Agent 也将持久化 Child Session 和进程内 Activation 分开，Activation 可以承载多个 FIFO Turn。

---

# 三十四、一个 Agent 只有一个 Inbox

禁止：

```text
普通消息队列
子 Agent 消息队列
Workflow 消息队列
Scheduler 消息队列
UI 消息队列
```

统一：

```text
Agent Inbox
```

消息带来源：

```python
@dataclass(frozen=True, slots=True)
class AgentMessage:
    message_id: str
    content: tuple[ContentBlock, ...]

    source: MessageSource
    correlation_id: str | None
    causation_id: str | None
```

消息来源可能是：

```text
USER
PARENT_AGENT
CHILD_AGENT
WORKFLOW
SCHEDULER
SYSTEM
PLUGIN
```

DeepSeek Harness 的子 Agent 同样坚持 Agent Inbox 是唯一队列，每个 accepted follow-up 都按 FIFO 进入普通 Turn。

---

# 三十五、消息接受不等于任务完成

```python
receipt = await supervisor.send(...)
```

`receipt` 只表示：

```text
消息已被持久化接受
```

不表示：

```text
Agent 已完成
该消息对应某个最终回答
下一次 Turn 一定只由该消息引起
```

如果调用方拥有完整的一次性子 Agent 生命周期，应使用单独 API：

```python
report = await supervisor.run_isolated(
    AgentRunSpec(...)
)
```

`AgentRunReport` 可以包含：

```text
开始和结束边界
最终输出
Evidence
Token
Tool Calls
结束原因
```

不要把 `send()` 变成一个语义含糊的等待结果接口。

---

# 三十六、多 Agent 的四种关系必须分开

不能只设计一个：

```python
parent_agent_id
```

## 36.1 Lifecycle Ownership

谁负责取消和释放 Child。

```text
owner_agent_id
```

## 36.2 History Lineage

Child Session 是否从某个 Session Fork。

```text
forked_from_session_id
fork_boundary_seq
```

## 36.3 Communication

谁给谁发送消息。

```text
sender_agent_id
receiver_agent_id
```

## 36.4 Workspace Relationship

多个 Agent 是否共享工作区。

```text
shared_readonly
isolated_branch
shared_write_lease
external_workspace
```

这四种关系不能相互推导。

---

# 三十七、多 Agent 的 Scope

创建 Child：

```python
AgentSpec(
    preset="reviewer",
    workspace=parent.workspace.snapshot(),
    owner_agent_id=parent.id,
    capability_grants=(
        "read_file",
        "search_text",
        "run_tests",
    ),
)
```

Child 得到：

```text
Application Scope
Workspace Scope
Reviewer Preset Scope
Child Agent Scope
```

而不是：

```text
Parent Agent 整个 Scope 的浅拷贝
```

父 Agent 只能显式 Grant 某些能力。

---

# 三十八、我们自己的创新四：Budget Tree

多 Agent 最容易出现：

```text
无限创建 Child
每个 Child 又创建 Child
Token 和工具调用指数增长
```

设计：

```python
@dataclass(frozen=True, slots=True)
class Budget:
    max_tokens: int
    max_steps: int
    max_tool_calls: int
    max_wall_seconds: float
    max_children: int
    max_depth: int
    max_processes: int
```

父 Agent 创建 Child 时分配预算：

```text
Parent 剩余 100,000 Token
  ↓
Child A 分配 20,000
Child B 分配 30,000
Parent 保留 50,000
```

预算使用流程：

```text
reserve
  ↓
execute
  ↓
commit actual usage
或
release reservation
```

预算事件：

```text
budget/allocated
budget/reserved
budget/committed
budget/released
budget/exhausted
```

这样多 Agent 行为可以被审计和恢复。

---

# 三十九、我们自己的创新五：Workspace Branching

多个 Coding Agent 直接共享同一个可写目录会产生：

```text
文件覆盖
未提交修改互相污染
测试结果无法归因
恢复困难
```

建议通过 `WorkspaceProvider` 抽象：

```python
class WorkspaceProvider(Protocol):
    async def snapshot(self, workspace_id: str) -> WorkspaceSnapshot:
        ...

    async def branch(
        self,
        snapshot: WorkspaceSnapshot,
        *,
        owner_agent_id: str,
    ) -> WorkspaceHandle:
        ...

    async def diff(
        self,
        workspace: WorkspaceHandle,
    ) -> PatchArtifact:
        ...

    async def merge(
        self,
        target: WorkspaceHandle,
        patch: PatchArtifact,
    ) -> MergeResult:
        ...
```

## 39.1 多 Agent 写入策略

```text
Researcher：
共享只读 Snapshot

Coder Child：
独立 Overlay / Git Worktree

Reviewer：
读取 Coder 的 Patch Artifact

Parent：
审批并 Merge Patch
```

Child 返回：

```text
Patch Artifact
Test Evidence
Summary
Risk Report
```

而不是直接悄悄修改 Parent 的工作区。

---

# 四十、子 Agent 如何接入 Loop

子 Agent 不属于 AgentLoop 的特殊语法。

实现一个 Tool Plugin：

```text
spawn_agent
send_agent_message
wait_agent
stop_agent
collect_agent_artifact
```

Tool 内调用：

```python
supervisor = context.services.require(AGENT_SUPERVISOR_V1)
child = await supervisor.create(spec)
```

因此：

```text
AgentLoop
   ↓ Tool Call
ToolRuntime
   ↓
Subagent Tool Plugin
   ↓
AgentSupervisor
   ↓
Child Agent
```

Loop 不知道自己启动了子 Agent。

---

# 四十一、Workflow Engine

Workflow 也不能塞进 AgentLoop。

```python
class WorkflowEngine(Protocol):
    async def start(
        self,
        definition: WorkflowDefinition,
        input: JsonValue,
    ) -> WorkflowRun:
        ...

    async def resume(
        self,
        run_id: str,
    ) -> WorkflowRun:
        ...
```

## 41.1 后续节点类型

```text
AgentTask
ToolTask
ApprovalTask
Map
Join
Condition
ArtifactMerge
Verification
```

## 41.2 Workflow 的位置

```text
WorkflowEngine
    ↓
AgentSupervisor
ToolRuntime
ApprovalService
ArtifactStore
```

Workflow 调用公开 Agent API，不访问 Loop 内部。

## 41.3 多 Agent 模式只是 Workflow 模板

```text
Manager–Worker
Parallel Research
Reviewer–Coder
Debate
Planner–Executor
Map–Reduce
```

不需要每种模式都增加一套 Runtime。

---

# 四十二、Persistence

## 42.1 EventStore Protocol

```python
class EventStore(Protocol):
    async def append(
        self,
        stream_id: str,
        *,
        expected_seq: int,
        events: tuple[PendingEvent, ...],
        durability: "Durability",
    ) -> "AppendReceipt":
        ...

    async def read(
        self,
        stream_id: str,
        *,
        from_seq: int = 0,
    ) -> tuple[EventEnvelope, ...]:
        ...

    async def list_streams(
        self,
        query: "StreamQuery",
    ) -> tuple["StreamSummary", ...]:
        ...
```

## 42.2 Expected Sequence

```text
当前最后 seq = 48
调用 append(expected_seq=48)
```

如果另一个 Writer 已经写到 49：

```text
抛出 ConcurrencyConflict
```

防止一个 Session 被多个 Runtime 同时写入。

## 42.3 Durability

```python
class Durability(Enum):
    SYNC = "sync"
    BATCHED = "batched"
```

建议：

| 事件              | Durability     |
| --------------- | -------------- |
| Inbox Accepted  | SYNC           |
| Effect Intent   | SYNC           |
| Effect Outcome  | SYNC           |
| Turn End        | SYNC           |
| Step End        | SYNC           |
| Assistant Chunk | BATCHED        |
| Telemetry       | 不进入 EventStore |

在报告 Agent Idle 前执行 Flush Barrier。

## 42.4 第一版 JSONL

实现：

```text
一个 Session 一个文件
一行一个 Envelope
尾部半行可截断
完整行不重写
写入使用文件锁
```

## 42.5 第二版 SQLite

表：

```text
sessions
events
stream_heads
plugin_state
projection_checkpoints
```

JSONL 和 SQLite 必须共享同一套 EventStore Contract Tests。

DeepSeek Harness 同样将 SessionPersistence 作为抽象能力，并提供 JSONL 和 SQLite 等后端，使上层继续使用同一套 SessionEvent 语义。

---

# 四十三、崩溃恢复

## 43.1 恢复步骤

```text
加载 Session Stream
   ↓
检查格式和序号
   ↓
运行 Core Invariants
   ↓
识别未闭合 Turn
   ↓
识别未闭合 Step
   ↓
识别未完成 Model Attempt
   ↓
识别孤立 Tool Call
   ↓
查询 Effect Ledger
   ↓
调用 Reconciler
   ↓
追加恢复事件
   ↓
重新建立 Projection
   ↓
恢复为可继续 Agent
```

## 43.2 不删除已发生事件

崩溃后：

```text
真实发生的事件继续保留
```

恢复只追加：

```text
tool/result(status=unknown_after_crash)
step/end(reason=interrupted)
turn/end(reason=interrupted)
effect/reconciled
```

DeepSeek Harness 的持久化恢复同样选择保留崩溃前已提交的 Turn 内容，并通过合成闭合事件修复开放生命周期，而不是截断所有真实事件。

---

# 四十四、不变量系统

不要只写一个中央函数检查所有内容。

每个拥有协议的模块注册自己的 Invariant。

```python
class Invariant(Protocol):
    name: str
    owner: str

    async def check(
        self,
        trace: TraceView,
    ) -> tuple[InvariantViolation, ...]:
        ...
```

## 44.1 Core Invariants

```text
Event seq 单调增长
Turn / Step 正确嵌套
Step 不跨 Turn
Tool Result 对应同 Step Tool Call
Effect Outcome 对应 Effect Intent
Session 只有一个 Writer
Request Snapshot 可重建
Composition Snapshot 可解析
已结束 Step 不接受新事件
```

## 44.2 Plugin Invariants

例如 Git Plugin：

```text
git/commit-created 必须对应 effect/outcome
记录的 commit hash 格式合法
Patch Artifact 必须存在
```

## 44.3 Multi-Agent Invariants

```text
Child Ownership 无环
Depth 不超过预算
Disposed Agent 没有 Live Tasks
一个 Child 最多一个 Activation
Message Receipt 最多 Claim 一次
Child Session 与 Workspace 关系合法
```

DeepSeek Harness 当前也让 Session、Agent、LLM、Tool、Compaction 和 Subagent 等模块分别注册其拥有的不变量，而不是由中央检查器猜测全部协议。

---

# 四十五、配置和 Preset

## 45.1 配置层次

```text
框架默认值
   ↓
用户全局配置
   ↓
Workspace 配置
   ↓
Profile
   ↓
Agent Preset
   ↓
单 Agent Override
   ↓
一次运行 Override
```

## 45.2 示例

```toml
[traceh]
profile = "coding"

[plugins]
enabled = [
  "traceh.openai",
  "traceh.coding-tools",
  "traceh.jsonl",
  "traceh.verification"
]

[presets.coder]
model = "openai-compatible:coder"
tools = [
  "list_files",
  "read_file",
  "search_text",
  "apply_patch",
  "shell"
]

[presets.coder.budget]
max_steps = 30
max_tool_calls = 80
max_children = 2
max_depth = 2

[presets.reviewer]
model = "openai-compatible:reviewer"
tools = [
  "read_file",
  "search_text",
  "run_tests"
]
```

## 45.3 Secret

配置中只保存：

```toml
credential = "credentials://openai/default"
```

真实值由：

```python
CredentialProvider.resolve(ref)
```

在每次模型请求时读取。

---

# 四十六、第一版功能范围

前八周仍然保持克制。

## 46.1 必须实现

```text
Session / Turn / Step
Append-only JSONL
State Projector
Surface Projector
Request Snapshot
Composition Snapshot 基础版
Scripted LLM
OpenAI-Compatible LLM
AgentLoop
Tool Runtime
五个 Coding Tools
Workspace 安全
Effect Intent / Outcome 基础版
Cancel / Dispose
Crash Recovery
Replay
Inspector
Evaluation
```

## 46.2 第一版不实现

```text
第三方 Entry Point 自动发现
热重载
完整多 Agent
Workflow DSL
远程插件进程
Web UI
Docker Provider
MCP
长期记忆
自动上下文压缩
分布式 Worker
```

但这些功能对应的 Protocol、Scope 和事件位置必须已经明确。

---

# 四十七、第一版内置工具

| Tool          | 类型               |
| ------------- | ---------------- |
| `list_files`  | WORKSPACE\_READ  |
| `read_file`   | WORKSPACE\_READ  |
| `search_text` | WORKSPACE\_READ  |
| `apply_patch` | WORKSPACE\_WRITE |
| `shell`       | PROCESS          |

## 47.1 Workspace 安全

必须实现：

```text
所有路径 realpath
禁止逃逸 Workspace
禁止符号链接穿透
Shell 固定 cwd
Shell 超时
输出大小限制
子进程环境变量清洗
危险命令审批
取消后不启动新 Tool
Dispose 等待子进程退出
```

---

# 四十八、演进表

| 第一版简单实现              | 稳定抽象                      | 后续复杂实现                     |
| -------------------- | ------------------------- | -------------------------- |
| `InMemoryEventStore` | `EventStore`              | JSONL / SQLite / Remote    |
| 静态加载内置插件             | `PluginManager.install()` | Entry Point Discovery      |
| 单层 Registry          | `ScopeRegistry`           | Workspace / Preset / Agent |
| 一个 Agent             | `AgentSupervisor`         | 多 Agent Activation         |
| 本地目录                 | `WorkspaceProvider`       | Worktree / Docker / E2B    |
| 简单 Barrier           | `ResourceCoordinator`     | 资源声明调度                     |
| 单 Provider           | `LlmRuntime`              | Router / Fallback / Replay |
| 固定 Prompt            | `PromptRuntime`           | 插件 Prompt Sections         |
| Tool Call / Result   | `EffectLedger`            | Reconciler                 |
| 最终文本                 | `CompletionVerifier`      | Evidence Gate              |
| 静态 CLI               | Session Event Feed        | Web / SDK / ACP            |
| Scripted Model       | `LlmProvider`             | 真实模型和 Mock Server          |

第一版可以简单，但调用方从第一天只依赖右侧的稳定抽象。

---

# 四十九、十六周实施路线

## 里程碑概览

```text
v0.1  可重建的单 Agent Loop
v0.2  Coding Agent 与 Tool Runtime
v0.3  恢复、评测和 Inspector
v0.4  Plugin SDK 与 Entry Point
v0.5  Scope、Generation 和可逆卸载
v0.6  AgentSupervisor 与子 Agent
v0.7  Budget、Workspace Branch 和 Workflow
v1.0  稳定 API、兼容策略和完整文档
```

## 第 1 周：API 与 Kernel 骨架

完成：

```text
traceh.api
EventEnvelope
ServiceKey
Protocol
Registration
Lifespan
OwnedTask
Scope 基础类型
```

验收：

- API 模块不依赖 Runtime；
- 一个 Demo Service 可以注册、解析、释放；
- Cleanup 按逆序执行；
- Setup 失败能回滚。

## 第 2 周：Session Event Store

完成：

```text
InMemoryEventStore
JSONL EventStore
Expected Sequence
Event Codec
State Projector
Core Invariants
```

验收：

- 重启后重建相同状态；
- seq 冲突被拒绝；
- 尾部半行可以恢复；
- 一个损坏 Session 不影响其他 Session。

## 第 3 周：Surface 与 Request

完成：

```text
SurfaceProjector
PromptRuntime
RequestBuilder
RequestSnapshot
CompositionSnapshot
Request Fingerprint
Scripted LLM
```

验收：

```text
运行时发送的 Request
==
从日志独立重建的 Request
```

## 第 4 周：AgentLoop

完成：

```text
Inbox
Session / Turn / Step
DefaultAgentLoop
Cancel
Dispose
LoopDirective
ContinuationRuntime
```

验收：

- 无工具 Turn；
- 多 Step Turn；
- Cancel 后日志闭合；
- Dispose 后无后台 Task。

## 第 5 周：Tool Runtime

完成：

```text
Tool Registry
参数校验
Policy Gate
Monotonic Guard
Around Middleware
Tool Result
Effect Intent / Outcome
```

验收：

- Deny 不可被覆盖；
- 超时产生结构化结果；
- Tool Call 与 Result 配对；
- 未派发 Tool 在取消后得到明确结果。

## 第 6 周：Coding Agent

完成五个工具和安全边界。

验收：

- 能完成至少两个 Fixture；
- 任务成功由测试决定；
- 禁止访问 Workspace 外文件；
- Shell 不继承敏感环境变量。

## 第 7 周：恢复与评测

完成：

```text
Crash Recovery
Effect Reconciliation 基础版
Benchmark Runner
External Verifier
Metrics Report
```

测试故障点：

```text
Request 写入前
模型流式中
Tool Call 后、Effect Intent 前
Effect Intent 后、执行前
执行后、Outcome 前
Outcome 后、Tool Result 前
```

## 第 8 周：v0.3 求职版发布

完成：

```text
CLI
Inspector
静态 HTML Trace
README
架构文档
恢复文档
演示视频
GitHub Release
```

此时项目已经可以放进简历并开始投递。

## 第 9 周：Plugin Manifest 和 Discovery

完成：

```text
Plugin Protocol
Manifest
依赖图
版本检查
Entry Point Discovery
Plugin Doctor
```

验收：

```bash
pip install traceh-plugin-git
traceh plugins list
```

无需修改 TraceHarness 源码即可发现插件。

## 第 10 周：Activation Transaction

完成：

```text
私有 Setup Scope
原子 Publish
失败 Rollback
Owned Tasks
Cleanup
DRAINING
```

验收：

- Setup 中途失败不留下注册；
- 插件异常不能破坏 Kernel；
- 卸载后无 Listener 和 Task 泄漏。

## 第 11 周：Scope 和 Composition Generation

完成：

```text
Application / Workspace / Preset / Agent Scope
Generation Snapshot
Step Lease
显式 Override
```

验收：

- 两个 Agent 可以看到不同 Tool；
- Child 不继承 Parent Agent Scope；
- 插件更新不改变进行中的 Step。

## 第 12 周：AgentSupervisor

完成：

```text
Agent Registry
Activation
Cold Resume
One Inbox
Ownership Graph
Child-first Dispose
```

验收：

- Agent 可创建、停止和恢复；
- 一个 Session 最多一个 Live Activation；
- Parent Dispose 会等待 Child；
- 消息按 FIFO Claim。

## 第 13 周：Subagent Plugin

完成：

```text
spawn_agent
send_agent_message
wait_agent
collect_artifact
stop_agent
```

验收：

- Parent 可以委派只读 Review；
- Child 有独立 Session；
- Child 能冷恢复；
- Parent 取消不会留下孤儿进程。

## 第 14 周：Budget 与 Workspace Branch

完成：

```text
Budget Tree
Workspace Snapshot
Child Branch
Patch Artifact
Merge
Resource Claims
```

验收：

- Child 不能超出分配预算；
- 多个 Coder 不直接共享可写目录；
- Parent 可以审查后 Merge Patch。

## 第 15 周：Workflow 与 Evidence Gate

完成：

```text
AgentTask
Parallel Map
Join
Verification
Reviewer–Coder Workflow
```

验收：

- 一个 Workflow 启动两个 Reviewer；
- 汇总结果交给 Coder；
- Coder 生成 Patch；
- Verifier 通过后结束。

## 第 16 周：v1.0

完成：

```text
API 稳定声明
Plugin Author Guide
Compatibility Matrix
Migration Guide
Contract Test Kit
完整 Release
```

---

# 五十、测试策略

## 50.1 Unit Tests

```text
Event Codec
Projector
Scope Resolution
Registry
Policy Aggregation
Budget
Resource Claims
```

## 50.2 Golden Trace Tests

Scripted Model 产生固定轨迹：

```text
输入
模型 Chunk
Tool Call
Tool Result
最终消息
```

比较完整 Event Stream。

## 50.3 Contract Tests

每个实现必须通过：

```text
EventStoreContract
LlmProviderContract
ToolContract
WorkspaceProviderContract
AgentProviderContract
PluginLifecycleContract
```

## 50.4 Property Tests

使用 Hypothesis 生成：

```text
随机 Turn / Step 事件
随机取消点
随机插件安装顺序
随机 Agent Ownership Graph
```

检查不变量。

## 50.5 Kill-Point Tests

在每个关键 Await 前后注入崩溃。

```python
await killpoints.hit("after_effect_intent")
```

验证恢复。

## 50.6 Plugin Compatibility Test Kit

第三方插件可以运行：

```bash
pytest --traceh-plugin-contract
```

检查：

```text
Manifest
API 版本
Setup / Dispose
Task 泄漏
事件 JSON 化
Hook 错误
Scope 隔离
```

## 50.7 Multi-Agent Deterministic Tests

使用 Scripted Provider 和虚拟时钟。

不使用真实模型来证明并发正确性。

## 50.8 E2E

真实读取：

```text
文件
Git Diff
测试退出码
生成 Artifact
Session 日志
```

不检查模型是否说“完成”。

---

# 五十一、公开 API 兼容策略

## 51.1 Public API

只有以下路径承诺兼容：

```text
traceh.api
traceh.sdk
```

其他路径：

```text
traceh.kernel._internal
traceh.runtime._driver
```

不属于插件 API。

## 51.2 版本规则

```text
0.x：
允许演进，但必须提供 Migration Note

1.x：
Minor 版本只做向后兼容增加
Major 版本允许破坏变更
```

## 51.3 Deprecation

公开 API 删除前：

```text
至少经历两个 Minor Release
产生 DeprecationWarning
文档提供替代 API
```

## 51.4 Event Schema

事件使用独立 `schema_version`，不与 Python 包版本绑定。

## 51.5 Capability Version

```text
traceh.tools@1
traceh.tools@2
```

插件 Manifest 声明兼容范围。

---

# 五十二、必须编写的 ADR

## ADR-001：为什么 Event Log 是 Source of Truth

## ADR-002：为什么区分 Session、Turn、Step 和 Attempt

## ADR-003：为什么 Kernel 不是 Plugin

## ADR-004：Service、Hook 和 Durable Event 的选择标准

## ADR-005：为什么插件 Setup 必须事务化

## ADR-006：为什么 Step 要冻结 Composition Snapshot

## ADR-007：为什么 Tool Call 之外还需要 Effect Ledger

## ADR-008：为什么 Child 不继承 Parent Agent Scope

## ADR-009：为什么 Ownership、Lineage 和 Communication 分离

## ADR-010：为什么 Coding Child 使用独立 Workspace Branch

## ADR-011：为什么 Completion 需要 Evidence

## ADR-012：为什么多 Agent 编排不进入 AgentLoop

这些 ADR 会成为面试时最有价值的说明材料。

---

# 五十三、禁止出现的架构退化

## 53.1 巨型 RuntimeContext

禁止：

```python
class RuntimeContext:
    messages
    tools
    plugins
    state
    session
    child_agents
    current_request
    temporary_cache
    approval
    workflow
```

拆成稳定 Service 和不可变 Step Context。

## 53.2 万能 EventBus

禁止：

```python
emit("before_everything", mutable_dict)
```

必须使用有类型和明确分发模式的 Hook。

## 53.3 Loop 中出现插件名

禁止：

```python
if "compaction" in plugins:
    ...
if "subagent" in plugins:
    ...
```

## 53.4 ContextVar 作为 Service Locator

禁止：

```python
current_runtime.get().tools.execute(...)
```

ContextVar 只携带 Trace Identity。

## 53.5 Child 共享 Parent 可变对象

禁止：

```python
child.context = parent.context
child.messages = parent.messages
```

## 53.6 热更新直接替换 Dict

禁止：

```python
registry["tool"] = new_tool
```

必须通过 Generation Publish 和 Drain。

## 53.7 插件注册无 Disposer

任何注册必须属于一个 Activation，并能被逆向释放。

## 53.8 把进程内插件称为沙箱

它不是。

---

# 五十四、CLI 规划

## v0.3

```bash
traceh run
traceh resume
traceh cancel
traceh sessions list
traceh inspect
traceh replay
traceh compact
traceh eval
traceh doctor
```

## v0.5

```bash
traceh plugins list
traceh plugins inspect
traceh plugins enable
traceh plugins disable
traceh plugins doctor
traceh composition dump
```

## v0.7

```bash
traceh agents list
traceh agents tree
traceh agents send
traceh agents stop
traceh agents inspect

traceh workflow run
traceh workflow inspect
traceh workflow resume
```

---

# 五十五、最终演示场景

## 55.1 单 Agent

```text
输入有 Bug 的项目
   ↓
Agent 读取文件
   ↓
修改代码
   ↓
执行测试
   ↓
Verifier 检查
   ↓
生成 Trace 和 Evidence
```

## 55.2 崩溃恢复

```text
Effect Intent 已提交
   ↓
文件写入完成
   ↓
进程被杀死
   ↓
traceh resume
   ↓
Reconciler 读取文件 Hash
   ↓
确认副作用已发生
   ↓
不重复 apply_patch
```

## 55.3 插件安装

```bash
pip install traceh-plugin-git
traceh plugins enable traceh.git
```

不修改 Runtime 源码，Agent 自动得到 Git Tool 和 Prompt Section。

## 55.4 多 Agent

```text
Parent Planner
   ├── Reviewer A：安全审查
   ├── Reviewer B：测试审查
   └── Coder Child：独立 Workspace Branch
            ↓
       Patch + Test Evidence
            ↓
Parent Review
            ↓
Merge
            ↓
Completion Verifier
```

## 55.5 Composition Inspector

展示每个 Step 使用的：

```text
模型
插件版本
Prompt
Tool
Policy
预算
Workspace
Request Fingerprint
```

---

# 五十六、简历描述模板

```text
TraceHarness Py｜可扩展、多 Agent 的 Python Agent Runtime

独立设计并实现基于事件溯源的 Python Agent Harness，将模型请求、
工具调用、生命周期和副作用统一记录为可恢复的 Session Stream 与
Effect Ledger，支持 Session/Turn/Step、请求重建、崩溃恢复、
轨迹回放和外部结果验证。

设计微内核式插件系统，通过 Protocol、Scope、Typed Hook、
Activation Transaction 和 Composition Snapshot 支持模型、
工具、Prompt、权限策略与持久化后端的独立扩展，并保证插件卸载
达到 Quiescence，不影响正在执行的 Step。

实现 AgentSupervisor、单 Inbox、Budget Tree 和隔离 Workspace
Branch，使多个 Agent 可以并行审查、编码和验证，并通过 Patch
Artifact 与 Evidence Bundle 合并结果，而不共享可变运行状态。

在 [任务数] 个可复现 Coding Benchmark 上取得 [成功率]，
恢复覆盖 [故障点数] 类崩溃窗口，核心协议通过单元、属性、
Golden Trace、Contract、Kill-Point 和 E2E 测试验证。
```

所有方括号数字必须来自实际评测。

---

# 五十七、完成标准

## v0.3 求职版

- [ ] 支持完整 Coding Agent 闭环；
- [ ] Session 使用 Append-only Event Log；
- [ ] Session、Turn、Step 定义明确；
- [ ] 请求能够独立重建；
- [ ] 有 Composition Snapshot；
- [ ] Tool 经过统一 Runtime；
- [ ] 有 Effect Intent 和 Effect Outcome；
- [ ] 支持取消和 Quiescent Dispose；
- [ ] 支持至少四个崩溃窗口恢复；
- [ ] 有外部 Verifier；
- [ ] 有 Benchmark；
- [ ] 有 Trace Inspector；
- [ ] 无 API Key 时能运行 Scripted Demo；
- [ ] 有架构、恢复和评测文档；
- [ ] 有演示视频和正式 Release。

## v1.0 平台版

- [ ] 第三方插件可通过 Entry Point 自动发现；
- [ ] Plugin Setup 是事务化的；
- [ ] Plugin Dispose 无 Task 和 Listener 泄漏；
- [ ] Scope 支持 Workspace、Preset 和 Agent；
- [ ] Step 使用 Composition Generation Lease；
- [ ] 插件更新不改变进行中的 Step；
- [ ] AgentSupervisor 支持冷恢复；
- [ ] 每个 Agent 只有一个 Inbox；
- [ ] Child 不自动继承 Parent Agent Scope；
- [ ] Ownership、Lineage、Communication 分离；
- [ ] 支持 Budget Tree；
- [ ] 支持 Workspace Branch；
- [ ] 支持至少一种多 Agent Workflow；
- [ ] 有 Plugin Contract Test Kit；
- [ ] 有 API 兼容政策；
- [ ] 有完整多 Agent Demo。

---

# 五十八、最终判断

使用 Python 完全可行，而且非常适合这个项目。

但真正决定项目质量的不是语言，而是能否守住以下边界：

```text
Session 是事实
Loop 是控制流
Kernel 是生命周期
Service 是能力
Plugin 是扩展
Supervisor 是 Agent 所有权
Workflow 是编排
Effect Ledger 是副作用事实
Verifier 是完成证据
```

第一版可以只有：

```text
一个 Agent
一个 JSONL Store
一个模型 Provider
五个工具
一个 CLI
```

但它们必须通过稳定 Protocol 组合。

这样后续增加：

```text
插件
多 Agent
上下文压缩
远程执行
审批
工作流
MCP
Web UI
```

时，不需要推翻第一版。

本项目最值得展示的并不是：

> “我用 Python 复刻了 DeepSeek Harness。”

而是：

> “我从大型 Agent Runtime 中提炼了事件溯源、能力边界、作用域和生命周期思想，并进一步设计了 Effect Ledger、Composition Provenance、Evidence Completion、Budget Tree 与 Workspace Branching，最终交付了一套能从单 Agent 平滑演进到多 Agent 的 Python Harness。”

这会比单纯复刻更能证明你具备 Agent Infra、异步系统、插件架构、工程正确性和系统设计能力。
