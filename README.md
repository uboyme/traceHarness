# TraceHarness Py v0.8.0 release candidate

TraceHarness Py 是一个基于事件溯源、可以重建运行过程的 Python Runtime，用来构建可追踪的 Coding Agent。v0.4 引入事务式插件系统；v0.5 完成 Generation/Lease/Drain、四层宿主装配与执行能力插件化；v0.6 发布 L1–L4 受控能力演进控制面和进程内多 Agent 主线；v0.7 把层级 Budget、managed Git Workspace、immutable Patch、固定 Verification/Review、人工 Approval、bare ref CAS Promotion、Typed Workflow、ProductTask Chat 与唯一 `traceh eval` Benchmark 接入同一条宿主主线；v0.8 再把唯一生产 EventStore 切到 SQLite，加入同 Provider/同模型/同冻结请求的有界 retry、UI-neutral Chat Driver、纯读 Product observation 与可选 Textual TUI。`AgentLoop`、`AgentRuntime`、`ProcessAgentSupervisor` 和 `PluginManager` 仍保持原有职责边界。

> 当前状态：Educational alpha。项目已经能够运行并经过测试，但公共 API 尚未承诺可稳定用于第三方生产环境。

## 已经包含什么

- Append-only SQLite EventStore：一个 current-schema 数据库保存 Session、Effect 与各控制面 Stream；
- Session / Turn / Step / Model Attempt / Tool Invocation 生命周期事件；
- 薄异步 Agent Loop，把 Prompt、模型、工具和 Continuation 行为委托给独立服务；
- 冻结的 Composition Snapshot，以及可以独立重建的模型请求；
- 确定性的 Scripted Provider 和 OpenAI-Compatible Provider；
- typed、无秘密的 Provider failure 分类，以及宿主显式限制的同 Provider/同模型/同冻结请求 retry；
- 统一 Tool Runtime，支持 Schema 校验、单调 Policy、超时、读并发、写入/进程排他 Barrier 和结构化结果；
- 五个 Coding Tools：`list_files`、`read_file`、`search_text`、`apply_patch`、`shell`；
- Workspace 路径边界和子进程环境变量清洗；
- Event/EventStore 所有权契约：调用方修改 `append()` 或 `read()` 返回事件的嵌套内容，不会反向改写已经保存的历史；
- 进程内 Session Event Feed：`EventStore` 的唯一 `SYNC` append 正常返回之后按真实 `seq` 顺序发布给订阅者，每个订阅者拿到独立副本；消费者接口只能订阅、不能发布；它不新增任何持久化事实，也不提升 SQLite commit 的平台保证，不是事实源；
- `traceh chat` 的实时 Tool Timeline：Turn 运行期间即时显示 Step、模型调用、工具生命周期和验证结果，可用 `--no-timeline` 关闭；
- Activity Heartbeat：模型或工具长时间未结束时，按 `--heartbeat-seconds`（默认 10 秒）打印等待时长，完成行附带实测耗时；
- 可选 Textual TUI：`traceh chat --tui` 复用同一 Session、ChatDriver 与 Product control/observation，把临时宿主操作、durable facts 与模型自述分层显示，只呈现当前合法且需输入确认的 Product 闸门，并提供 fresh、只读的分角色任务对话和完整身份视图；
- 同 Session ProductTask 任务记忆：每轮 requester 请求前从同一 EventStore fresh 投影一条原子 format-7 收据，以 system 当前事实加 user 历史参考携带 focus 在内最多六项任务及准确总数/省略数；对需要解释执行结果的当前 focus 只加入最小执行摘要，详细证据按需读取。它不是第二份 Product 状态、Workspace Memory、RAG 或新控制权限；
- Product 请求者权限隔离：启用 `--product-config` 后，START 前的 Chat 只拥有 workspace 读取、Proposal/Confirmation 与只读 `read_product_task_evidence` Tool；后者只能读取由 durable origin/confirmation 证明属于同一 requester Session 的精确 task id。`apply_patch`/`shell` 不进入工具表，声明为写入、进程或外部事务的插件 Tool 也被单调 Policy 拒绝。真正代码副作用只在 START 后由 Product coder 的 managed Workspace owner 执行；不启用 Product 配置的普通 Coding Chat 保持原工具面；
- 可收敛的 Ctrl+C：有任务在跑时首次 Ctrl+C 只取消当前 Turn 并保留 Session，取消生命周期会完整显示在 Timeline 上；
- 启动即打印的恢复命令：Banner 直接给出含解析后 `--data-dir` 的可复制命令，硬中断也能从屏幕历史找回 Session；
- Effect Intent / Dispatch / Outcome 记录，用于判断崩溃时间窗中的副作用；
- Append-only 崩溃恢复：闭合孤立的 Model Attempt、Tool Call、Step 和 Turn，不盲目重放结果不明的副作用；
- 通过可选外部命令 Verifier 实现 Evidence-Driven Completion；
- 可选 Textual TUI 的 Context 透明度：顶部一行状态条显示模型可见历史字节数、压缩阈值（仅在启用时）、durable 压缩与失败次数和任务目录计数；`Ctrl+X` 打开只读详情页，区分“当前投影”与“最近一次冻结请求”。显示的是 canonical UTF-8 字节，不是 token，也不显示模型 context-window 百分比（没有可信 tokenizer 与canonical 输入上限）；
- Request 重建检查、协议不变量、Replay、手动与宿主自动 Surface 压缩，以及静态 HTML Inspector。自动压缩默认关闭且没有内置数值，启用需一次性显式给出开关与三个阈值；触发指标是 canonical UTF-8 字节数，不是 token；
- 确定性的 Benchmark Runner 和无需 API Key 的 Demo；
- **插件系统（v0.4 新增、v0.5 完成 Generation 化）**：`traceh.plugins` Entry Point 发现、显式启用、事务式激活；插件的 Tool、Prompt Section、Service、Provider、Policy、Middleware 和命名 Verifier 进入既有主线，没有独立的插件 Tool Runtime 或插件 AgentLoop；
- **Plugin Creator Skill（v0.6 L1）**：以独立 Wheel 提供候选编写 Prompt 和 `PURE_READ` 指南；只在专用 Workspace 生成未验证源码，不自动 build/test/install/enable；
- **候选验证（v0.6 L2）**：`traceh plugins validate` 用显式可信核心 `HEAD` 的版本、两套 venv 和 13 道宿主管控门禁验证 L1 候选；执行后复核审计字节，报告与 Wheel 按目录事务提交，失败不发布 Wheel，通过才给出精确产物与 SHA-256；
- **能力对比（v0.6 L3）**：`traceh plugins compare` 复用精确 L2 产物和其记录的核心提交，在两套同构环境运行宿主固定任务；只给出 `improved/regressed/mixed/no-change`，不批准、不安装、不晋升；
- **人工批准与回滚（v0.6 L4）**：`traceh plugins promote` 先生成不改环境的中文证据/风险卡和一次性摘要，只有带回同一摘要的第二次调用才能把精确 Wheel 安装到显式目标 Python；`rollback` 按推广 ID 恢复上一份精确 Wheel 或卸载首版；
- **多 Agent 控制面（v0.6）**：`traceh.agents` 从同一 EventStore 重建 identity 与 FIFO acceptance，`traceh.supervision` 记录 claim/terminal、维持每 Agent/Session 至多一个进程内 Activation，并按 durable `owner_agent_id` child-first 收敛子树；`SupervisorToolset` 提供 `spawn_agent`、`send_agent_message`、`wait_agent`、`collect_agent_artifact`、`stop_agent`，但只有宿主显式装配后模型才能看见；
- **`traceh plugins list/inspect/doctor`**：`list`/`inspect` 只读取元数据，不 import 任何插件、不创建 Session、不调用模型；
- 类型化 Hook、Application → Workspace → Preset → Agent Service Scope 与 Tool/Prompt/Policy Overlay、可逆 Activation 和 Owned Task 收敛等 Kernel 原语；四层装配结果跟随 Generation/Step Lease 冻结。

## 不安装直接运行

需要 Python 3.12 或更高版本。

```bash
cd traceHarness
export PYTHONPATH="$PWD/src"
python -m traceh.cli.main doctor
pytest
```

## 在同一 Session 中连续对话

`traceh chat` 会保持一个 Session，并从终端逐轮读取输入：

```powershell
traceh chat .
```

启动后会打印 Session ID、Workspace、Provider 和 Model，然后停在 `you>` 提示符。你输入的每一行都会在同一个 Session 中创建一个新 Turn。模型历史来自 Event Log 投影，而不是另一份内存聊天记录，因此之后使用 `inspect` 和 `replay` 可以看到整段对话。

通过 Session ID 继续以前的会话；Workspace 会从 Event Log 读取，不需要重新输入：

```powershell
traceh chat --session-id <session-id> --data-dir "<data-dir>"
```

**定位 Session** 需要两样东西：`--session-id` 和存放事件的 `--data-dir`（默认 `.traceh`，位置相对于当时的工作目录）。只有当你就在原来的工作目录、且当时用的是默认 data 目录时，才可以省略 `--data-dir`。

v0.8-F1 是 pre-1.0 的单主线切换：生产事件现在位于 `<data-dir>/events/events.sqlite3`。发现 v0.7 的
`.jsonl`/`.lock` 痕迹时会稳定拒绝并要求选择新 data dir；不会读取、迁移、删除旧证据，也没有 SQLite
失败后回退 JSONL 的路径。一致备份/恢复只能使用宿主持有的 SQLite backup/restore API，并且目标必须
事先不存在。打开现有数据库时会先用 immutable read-only connection 证明全部持久 schema authority；
只有证明属于 TraceHarness 后才允许 SQLite 恢复 hot journal、验证完整历史并启用 WAL。未通过该 schema
authority probe 的数据库不会先被改写 journal mode/bytes，也不会丢失 rollback journal 证据；已经证明
属于本产品的库则明确授权 SQLite crash recovery，恢复后 integrity/history 仍可能继续 fail closed。

**恢复它当时的行为**是另一件事，需要 Provider、Model 等配置——见下面的[Ctrl+C 与找回 Session](#ctrlc-与找回-session)。Chat 启动时就会把完整命令打印出来，直接复制即可。

继续会话前会先执行崩溃恢复。只有确实修复了未闭合状态时才会打印一行简短的 `recovered: ...`；在你主动输入前不会创建 Turn，也不会替你注入隐藏指令。

### 可选 Textual 界面

核心与 Line Chat 不依赖 Textual。需要双栏交互界面时安装可选 extra：

```powershell
python -m pip install -e ".[tui]"
traceh chat . --tui
```

继续旧会话仍用同一个命令，只增加 adapter 选择：

```powershell
traceh chat --session-id <session-id> --data-dir "<data-dir>" --tui
```

TUI 只有一套当前浅色 presentation，没有旧布局或主题兼容开关。左侧显示从 SQLite Session facts 重建的
对话和同一 `ChatDriver` 的活动：用户行顶格使用默认样式，宿主行顶格使用 teal，模型每一行都以“两个空格、
暗色竖线与暗色斜体”缩进，不作为宿主证据。短对话贴近输入框向上生长，长对话自动滚动。右侧从顶部
按任务头、最近 durable 事实、证据、闸门或终态四组显示进程内 Proposal/START/operation 与
Product/Workflow/Session/Review/Promotion facts，组间恰好三条分隔线；底部只显示当前合法闸门。固定列
事实表显示当前任务可绑定事件的年龄和宿主等待时长。模型确认 Proposal 后只会出现独立的 **START**；
点击后还要完整输入 `START`，并立即看到
“宿主已接受 · 等待返回”。Approve、
Reject、Cancel 也使用精确 typed confirmation，只有当前合法动作才会出现；START 调用尚未返回但 durable
Workflow 已 RUNNING 时，仍可通过同一 Product control 正常 Cancel。Product/Workflow 分歧时批准
入口关闭并提示使用 Line `/task inspect <task-id>`；当前 TUI 不提供 `Ctrl+I` 或 `Ctrl+R`，普通 refresh 不会
偷偷对账。证据区使用“审批 / 改动 / 校验 / 补丁”标题，显示有界 changed paths、Verifier 摘要和最多十二行
Patch preview。TUI 不保存或
接收 approval digest。批准、驳回、取消或放弃成功且 fresh 状态读取完成后，左栏会从现有 typed operation
result 显示一次宿主结果；这不是 Session 消息，不写 SQLite，也不进入模型上下文。`Ctrl+T` 每次打开都
fresh 读取 Product observation，并精确绑定 Router 与固定
Workflow 角色 Session；每条 Session 先过 `CoreInvariantChecker`，再按 canonical seq 单遍组合 user/model
发言与 tool call/result，工具行显示真实 seq 区间，超出一屏时明确报告省略的工具调用与发言数量。该页只读、
无缓存、无实时 tail；unknown/missing Usage 显示 unavailable，shell 参数和工具结果正文保持遮蔽。
`Ctrl+P` 同样 fresh 读取并展示完整身份，支持显式复制；剪贴板失败才写独立临时文本文件并显示
路径。所有模型、Patch、路径和错误文字都按 plain text 转义并
有界，Rich/Textual markup 被禁用。Feed 仍只是进程内 dirty 提示；TUI 还会按宿主单调时钟周期 fresh read
SQLite，因此另一进程更新任务时不会永久停留在旧面板；初次读取失败会诚实显示并周期重试，不会伪装成
“尚无任务”。同一窗口从一个终态任务进入下一项 Proposal 时，TUI 会按精确 task id 关闭旧 observer 并
切换唯一 Product pane；旧失败证据仍留在 SQLite 历史中，但不会再遮住新任务确认后的 START。失败摘要
优先显示失败节点 exact Agent/Session/request/message 所属 Turn 中可验证的稳定叶子 code/category/type，
同时保留 Workflow 包装 code；同一 Session 后续无关 Turn 不能覆盖它，原始 Provider body、header、异常
消息和 traceback 不进入界面。小于 110 列的终端自动折成单栏摘要；`Ctrl+T`/`Ctrl+P` 都打开同一套
全宽只读页面，不保留旧 details panel 或窄屏展开兼容行为。

auto Router 的消息已经有 durable terminal fact 后，responder 会把这个可重放结果交回唯一 ProductTask
writer，并沿原有 `finally` 路径等待 Supervisor dispose Router child；没有把 cleanup 所有权转移给 TUI。
插件 ActivationSet 现在先在 ownership lock 内冻结 `disposing`，释放锁后才启动 cleanup Task，所以 Python
3.12 eager task scheduling 不会在同一线程重入非重入锁并冻结整个事件循环。Router、ProductTask root、
失败/取消和宿主关闭仍沿既有 owner 收敛，没有后台遗弃 Agent、第二套生命周期或 TUI 特例。

传入 `--product-config` 时，左侧请求者 Chat 即使和配置中的 Product source 指向同一个目录，也只能
`list/read/search`、提议/确认，以及按精确 task id 调用纯读 `read_product_task_evidence`；不能在 START 前
直接 `apply_patch` 或运行 `shell`。证据 Tool 只接受由 Product origin/confirmation 与 requester Session
事件共同证明属于当前 Session 的任务；不存在、属于其他 Session 或任一关联证据不可读/损坏时，都返回同一
`product-task-evidence-unavailable` 结果，不成为 task-id 探针。真正写代码的是 START 后固定 Workflow 中
拥有独立 managed Git worktree 的 coder。这是共享 Product Chat Runtime 的权限规则，Line 与 TUI 完全一致，
不靠界面隐藏按钮实现。

同一 Product-configured requester Session 的下一轮模型请求会先从现有 ProductTask 与 Session 事件 fresh
投影一张有界任务目录：当前 focus 在内最多六项，并带准确总数和省略数。当前状态作为宿主 `system` 事实，
历史 source request 作为明确标注的 `user` 参考；后者不是当前指令、canonical requirement 或 START/审批/
推广授权。format 7 只为当前 focus 的 `awaiting_approval`、`completed`、`rejected`、`cancelled` 或 `failed`
状态加入六项宿主验证的执行摘要：Workflow 状态、managed Tool 调用数、changed-path 数、校验结论、
Verifier 数与 Promotion 是否记录；历史任务和其他状态不复制摘要。changed path、Tool 结果元数据、Verifier、
Review 与 Promotion 的详细身份仍不默认占用上下文，模型确有需要时再调用
`read_product_task_evidence`。该 Tool fresh 连接既有 Product/Workflow/Session/Agent/Artifact/Review/
Approval/Promotion/Budget 事实并返回有界 JSON；不暴露原始 Patch、Tool 参数/输出、模型散文或 Product
workspace 路径，也不授予任何控制权限。活动投影只接受 Runtime/Recovery 当前会落盘的七种结果状态；
未知 durable 状态明确拒绝，`pending` 只表示尚未配对结果的在途 Tool 调用。

CLI 为 requester Tool 与 host context/observation 分别构造两份 stateless `ProductReadModels` reader bundle：
它们不是同一组 Python 实例；前者在 Runtime 冻结 Tool 表前绑定原 Store，后者在 Runtime 建立后绑定带
进程内 Feed 的 Store wrapper。CLI 对两次构造传入同一原始 durable log、Profile、CAS、VerificationPlan、
Promotion target 与报告上界，因此当前生产入口按构造保证两者同源同配置；host 不跨实例比较这两份
bundle，只校验传给自己的第二份 bundle 与 host 及其内部 reader chain 一致。所有结果仍从同一 EventStore
fresh 重建；
`product/context-snapshot` 记录模型当时看见的两条精确消息及其重放/身份元数据，
Product/Workflow/Promotion 原流仍是控制权威，普通
Tool 调用/结果仅按既有 Session Tool 生命周期留痕。没有新增 Memory stream、缓存、RAG 或跨 Session 记忆。
旧 format 1–6 不兼容，需使用新的 data 目录，不会自动迁移或回退。设计边界见
[ADR-0041](docs/adr/0041-session-scoped-product-task-evidence-memory.md)。

`Ctrl+C` / `Ctrl+Q` 不会立刻留下一个黑屏等待：右栏会先显示 operation、Driver、observer、Product host
和 Runtime 的实际收敛状态，全部结束后再退出。

首版不做 token streaming、完整历史 Dashboard、拖拽 DAG、执行中并发输入或 TUI 内插件组合管理；插件
选择仍可用启动参数，空闲期 `/plugins` 切换仍属于 Line adapter。没有安装 extra 却使用 `--tui` 时会在
创建 Store/Session 前明确报错，不会静默切回 Line。

以下内部命令只有在整行完全匹配时才生效：

```text
/help     显示可用命令
/session  显示 Session ID、Workspace、Provider 和 Model
/exit     退出 Chat
/quit     退出 Chat
```

`/help` 还会提示 `--no-timeline` 与 `--heartbeat-seconds` 属于启动参数。内部命令、空行和无法识别的斜杠命令都不会创建 Turn，因此也不会产生 Timeline 行。

空行会被忽略。`Ctrl+D`（Windows 上是 `Ctrl+Z` 后按 Enter）等同于 `/exit`。

`Ctrl+C` 的行为取决于宿主 Shell，因此项目不承诺一个统一退出码。当 Shell 把它转换成 Python `KeyboardInterrupt` 时——Linux/macOS 终端或 PowerShell 中通常如此——正在运行的 Turn 会通过正常取消路径收敛，程序打印可继续使用的 Session ID，并从 Python 内部返回 130；Shell 最终显示什么仍由 Shell 决定。硬中断则不同，例如 Windows `Ctrl+Break` 或直接关闭控制台窗口：操作系统会直接终止进程，不运行上述收敛逻辑，Windows 实测退出码为 `3221225786`。

这正是启动时立即打印完整恢复命令的原因——它包含 Session ID、绝对 data 目录和配置参数，直接复制即可；格式见下面的[Ctrl+C 与找回 Session](#ctrlc-与找回-session)。重新进入后程序会先执行崩溃恢复，闭合之前遗留的生命周期，再继续对话。

### 实时 Timeline

Turn 运行期间，Chat 会实时打印它正在做什么，默认开启：

```text
you> 读一下 hello.txt
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

方括号里的数字是 Session Stream 中**真实的持久化事件序号**，不是终端行号。序号通常不连续，因为被隐藏的事件（Prompt 快照、请求快照、模型原文等）同样占用序号。你可以用这个序号在 `traceh inspect` 中定位同一条事件。

需要安静输出时（脚本、CI、日志采集）关闭它：

```powershell
traceh chat . --no-timeline
```

`--no-timeline` 是启动参数，不是 Chat 内部命令；关闭后最终回答和摘要行不变。

### 等待提示（Activity Heartbeat）

模型或工具跑得久时，Timeline 本身会安静下来——`model/attempt-start` 与 `model/attempt-end` 之间没有事件。为了区分"很慢"和"卡死"，会按固定间隔打印等待时长：

```text
[event 9] Model openai-compatible/qwen-plus called
[waiting 10s] Model openai-compatible/qwen-plus is still working
[waiting 20s] Model openai-compatible/qwen-plus is still working
[event 11] Model responded (23.4s)
```

```powershell
traceh chat . --heartbeat-seconds 5   # 每 5 秒提示一次
traceh chat . --heartbeat-seconds 0   # 关闭等待提示，保留 Timeline
```

默认 10 秒；`0` 只关闭等待提示；`--no-timeline` 会同时关闭 Timeline、等待提示和事件序号说明；负数、NaN、Infinity 会报配置错误。它是启动参数，不是 Chat 内部命令。

计时从**每个活动自己的起点**算，不是固定滴答：因此活动在任何时刻启动，第一条提示都出现在它自己开始约一个间隔之后，而不会拖到第二个间隔。

等待提示**不是事件**：不写入 Event Log，不参与恢复与重放，前缀是 `[waiting …]` 而不是 `[event N]`。它只显示清洗后的 Provider/Model、工具名、调用 ID 和已等待秒数——不显示 Shell 命令、工具参数、Prompt、文件内容、Patch 或命令输出。并发工具按调用 ID 分别计时。

措辞按可证明的事实区分：模型调用的结束事件在 Provider 返回后立即写入，所以显示 `is still working`；工具则显示 `has not reported completion`，因为 `ToolRuntime` 对并发只读工具成组执行、整组完成才写入各自的 `tool/result`，从事件上无法区分"已跑完"与"仍在跑"。工具的完成耗时是 `tool/admitted` 到**持久化的** `tool/result`，对组内工具会长于其自身执行时间。

**尚未覆盖**：`CommandVerifier` 没有"开始"事件（协议只有结束时的 `verification/result`），所以一个跑很久的验证命令仍然完全安静。本项目不使用 UI 侧推测去猜它是否启动。

### Ctrl+C 与找回 Session

有任务在运行时，**第一次 Ctrl+C 只取消当前 Turn**，Session 保留，取消过程会完整显示出来，然后回到提示符：

```text
[event 31] Cancellation requested
[event 32] Model attempt cancelled
[event 33] Step 2 ended (cancelled)
[event 34] Turn ended (cancelled)
Turn interrupted. This session is still open.
you>
```

下一个 Turn 由你的下一条输入创建，不会自动注入"继续任务"。停在提示符上按 Ctrl+C 才会离开 Chat（内部返回 130）。收敛过程中再次 Ctrl+C 不会提前放行：模型请求、Shell/Verifier 子进程都会先收敛完。

**硬中断除外**：Windows `Ctrl+Break`、关闭控制台或被操作系统终止时，没有任何 Python 代码会运行，上述收敛与提示都不会发生。这正是恢复命令在**启动时**就打印的原因：

```text
resume later (PowerShell):
  traceh chat --session-id <id> --data-dir <绝对路径> --provider <p> --model <m> [--max-steps N] [--script <绝对路径>] [--base-url <url>] [--api-key-env NAME] [--env-file <绝对路径>] [--model-retry-* <value>]
  traceh sessions --data-dir <绝对路径>
  note: this restores the session and its non-secret settings; it is not a complete configuration snapshot.
```

命令按目标 Shell 渲染并标注：Windows 上是 PowerShell（动态值用单引号，内部单引号按 PowerShell 规则写成两个），其他平台用 POSIX 规则（`shlex`）。程序名和参数名保持不加引号，其余每个取值都由对应 Shell 的规则引用，因此 `&`、`;`、`|`、`$()`、反引号、引号和空格都只会是普通文本，不会拼出第二条命令。含控制字符或换行的值一律拒绝渲染（判定包含 `U+2028`/`U+2029` 这两个 Unicode 行分隔符——它们对 `splitlines()` 与许多查看器同样是换行，只检查 `C*` 类别会放行）——此时只打印 session_id 和 data 目录，**且这两个值也会被转义**（换行显示为 `\n`、ESC 显示为 `\x1b`），并说明未生成命令。否则那个「无法安全显示」的值会在解释它的那几行里再次破坏输出。

这条命令分成两部分，用途不同：

- **定位 Session**：`--session-id` 加解析后的绝对 `--data-dir`。Store 位于 data 目录之下，换过工作目录或用过自定义 `--data-dir` 的会话，只靠 session_id 打不开；
- **恢复运行行为**：`--provider`、`--model` 等。这些值可能来自原工作目录的 `.env`，只带前两项的命令会在新目录重新解析配置，把会话**静默切换到另一个模型**。

**它不是完整配置快照**，命令自己也这么说。有两类值不会原样回显：

- `--verify-command` 是任意 Shell 文本，无法既展示又证明其中没有凭据，因此一律省略。**只有当本次真正生效的验证命令确实来自随命令附带的 `--env-file` 时**，才提示由该文件恢复；配置优先级是「显式参数 > 已有环境变量 > env-file」，所以文件里含有该键并不代表它能恢复当前生效的值。其余情况明确提示 `Verifier command omitted from the displayed resume command; re-supply it manually.`；
- Base URL 使用 `urllib.parse` 做**结构检查**：URL 内嵌用户名/密码，或带 query/fragment 时不显示，并说明原因让你手动补上。畸形地址（如 `https://[bad`）会让解析抛错，这种情况同样只是「不显示 + 说明原因」，不会暴露原值或 traceback。这是结构规则，不是通用秘密探测器——它无法判断一个普通路径段本身是不是凭据。

`--api-key-env` 与 `TRACEH_API_KEY_ENV` 必须是合法环境变量名（字母、数字、下划线，不以数字开头），否则在创建 Session 之前就报配置错误——不再接受后又静默省略、导致下次运行退回 `OPENAI_API_KEY`。该规则与 Provider 无关：`scripted` 忽略 Key 并不使非法名字合法。

**错误信息不回显被拒绝的值**，也不显示其长度或前后缀：这个设置最常见的写错方式就是把 Key 本身粘到了变量名的位置，因此非法值恰恰是最不能打印的东西；`.env` 中非法的左侧名字同样只报行号。需要说明能力边界：校验判断的是**形状**而非意图，一个恰好是合法标识符的 Key（如 `ghp_...`）会被接受，并作为变量名出现在恢复命令中。

API Key 的**值**不被读取也不被打印，命令里只会出现它的**环境变量名**（`--api-key-env NAME`）：由 env-file 提供时提示"可从该 env-file 或 Shell 获取"，否则提示需要在新 Shell 中设置。`provider=scripted` 时不会打印这一项，也不会提示你去设置 `OPENAI_API_KEY`。

显式使用过 `--script` 时会带上其解析后的绝对路径，并提示：Scripted Provider 的响应游标**不跨进程持久化**，重新加载同一文件会从第一条响应重新开始。

Banner、`/session`、`/exit`、`/quit`、EOF 与中断都会显示这段信息。

### 为什么第一条是 `[event 4]`

`seq` 1-3 是 `session/created`、`inbox/accepted`、`inbox/claimed`：它们**确实已持久化**，只是 Timeline 不显示，所以第一条可见事件通常是 `turn/start`。TraceHarness 刻意不重新编号——真实 `seq` 才能在 `traceh inspect` 和 SQLite EventStore 中查回。因此启动时会打印一次说明：

```text
Timeline shows selected persisted events.
Numbers shown as [event N] are Event Log seq values; they may start above 1 or skip where internal events are hidden.
```

Timeline 只显示生命周期与结果：Turn/Step 起止、模型调用与答复、工具的 requested/started/succeeded/failed、验证结果、运行时错误、取消请求和恢复。它**默认不显示** Prompt、请求快照、Composition 快照、模型原文、文件内容、完整 Patch、完整命令输出，遇到未知事件类型也不会打印原始 payload。

输出侧按不可信内容处理：所有来自事件 payload 的字符串都会先清洗（去除 ESC 与其他控制/格式字符、折叠为严格一行、统一限长），因此模型返回的工具名或任意异常类型无法伪造额外的 Timeline 行，也无法发出终端控制序列。`shell` 执行的命令**默认完全不显示**（只显示工具名与调用 ID），因为命令行最容易带上凭据，而关键词扫描无法覆盖所有秘密形态；`runtime/error` 只显示错误类型，不显示消息与 traceback。可显示的读取类工具路径仍会做凭据形态检查，命中即整段不显示；未知工具只显示工具名与调用 ID。

Timeline 是纯界面投影：它不进入模型可见历史，不改变 Request Fingerprint，也不写入任何事件。它只在当前进程内可见——另一个进程写同一个 SQLite 数据库时不会实时出现在这里。

v0.8-F3 已把这条行为拆成 UI-neutral `ChatDriver` 与第一个 Line adapter：Driver 异步接收 Turn 输入并
发出 typed Session/activity/outcome update，不读 stdin、不含终端文案，也不保存第二份 messages。
Product 进度同样 fresh read ProductTask/Workflow 两条状态，Feed 只提示刷新；两条流暂时不一致时如实
并列显示，只有真正的 inspect/approve/reject/cancel 动作才调用原 control-plane owner 对账。观察建立期间
任何 fresh-read 失败都会由 observer 自己回滚全部 subscription/watcher；Product host 只接受与同一
`PublishingEventStore` 精确绑定的显式 Feed，缺失或身份不一致会在装配前拒绝，不能得到永久静默的假实时观察。

默认 Chat 仍是行式提示符；F4 的 `--tui` 是可选最小 Textual adapter。两者都没有 Token Streaming，
也不能在 Turn 运行期间继续输入；TUI 的 START/Approval 是既有 Product 人工边界，不是新增模型权限。

### Windows 中文与其他非 ASCII 文本

Chat 会把 stdin、stdout 和 stderr 配置为 UTF-8，并使用 `errors="replace"`，因此不要求执行 `chcp 65001`。程序会去掉输入开头的 BOM，因为它属于文本流而不是用户消息：Windows PowerShell 5.1 的 `Out-File -Encoding utf8` 会写入 BOM；PowerShell 7 的 `utf8` 默认不带 BOM，使用 `utf8BOM` 才会重新启用。

如果一行中仍出现 U+FFFD，说明原始字符在解码时已经丢失。TraceHarness 会拒绝这一行并给出提示，不写入 `user/message`，也不会猜测原文。请改用 UTF-8 重新发送。

## 运行确定性的 Coding Demo

Demo 初始包含一个错误的 `add()` 实现。Agent 会修改 Workspace，因此运行前先复制一份：

```bash
cp -R examples/demo_bug /tmp/traceh-demo

PYTHONPATH=src python -m traceh.cli.main run \
  /tmp/traceh-demo \
  "Fix the addition bug and run the tests" \
  --script examples/demo_script.json \
  --verify-command "python -m unittest -v" \
  --data-dir /tmp/traceh-data
```

命令会打印一个 `session_id`。可以用下面的命令检查和 Replay：

```bash
PYTHONPATH=src python -m traceh.cli.main inspect <session-id> \
  --data-dir /tmp/traceh-data \
  --html /tmp/traceh-session.html

PYTHONPATH=src python -m traceh.cli.main replay <session-id> \
  --data-dir /tmp/traceh-data
```

## 作为 Python 包安装

```bash
python -m pip install -e ".[dev]"
traceh doctor
```

### 运行时依赖

v0.4 引入了本项目的**第一个运行时第三方依赖**：[`packaging`](https://pypi.org/project/packaging/)（`>=24.0,<27`）。

它被用来解析 PEP 440：Plugin Manifest 的 `requires_traceh` 兼容范围、插件之间的依赖版本区间，以及插件 Distribution 声明的 `traceharness-py` 依赖。这三处都位于信任边界上，自己实现一个不完整的 PEP 440 解析器风险更大，所以选择依赖标准实现。

核心安装除此之外仍然只使用 Python 标准库；`pytest`、`pytest-asyncio`、`ruff` 只是开发依赖。可选
`tui` extra 另行声明 `textual>=8.2.8,<9`，Line/Eval/core import 不依赖它。项目使用 `setuptools` 作为
构建后端，正常的 `pip` Build Isolation 会自动安装它。

> 注意：v0.3 及更早文档中"运行时只依赖标准库"的说法从 v0.4 起不再成立。

### 安装并启用一个插件

安装只是让插件**可被发现**，不等于启用：

```powershell
python -m pip install .\examples\plugins\traceh-example-skill-plugin
traceh plugins list                       # 确认已发现；这一步不 import 插件
traceh plugins doctor traceh.example.skill
```

启用是一个独立、显式的动作：

```powershell
traceh run <workspace> "任务" --plugin traceh.example.skill
$env:TRACEH_PLUGINS = "traceh.example.skill"   # 等价写法
```

命令行上任何一次 `--plugin` 都会**整体替换** `TRACEH_PLUGINS`，而不是追加；`run`、`chat`、`resume` 使用同一套选择规则。完整作者契约见[插件说明](docs/plugins.md)。

仓库还包含一个真正独立发行的 Python Quality 插件。它贡献只读项目检查 Tool、Python 开发提示、环境保护 Policy 和命名 Verifier：

```powershell
python -m pip install .\examples\plugins\traceh-python-quality-plugin
traceh plugins doctor traceh.python.quality
traceh chat <workspace> --plugin traceh.python.quality --plugin-verifier python-tests
```

Verifier 不会猜测试框架。项目需要在根目录 `pyproject.toml` 明确声明测试命令：

```toml
[tool.traceh-python-quality]
test-command = ["python", "-m", "pytest", "-q"]
timeout-seconds = 120
```

如果项目已经存在 `[tool.pytest.ini_options]` 或根目录 `pytest.ini`，插件也可以据此使用当前解释器运行 pytest。完整配置与边界见[插件自己的 README](examples/plugins/traceh-python-quality-plugin/README.md)。

仓库还包含 L1 Plugin Creator Skill。它只负责在**单独的 Candidate Workspace** 中指导模型写出标准外部插件源码，不运行、不安装、也不批准自己的候选：

```powershell
python -m pip install .\examples\plugins\traceh-plugin-creator-skill-plugin
traceh plugins doctor traceh.plugin.creator
traceh chat <candidate-workspace> --plugin traceh.plugin.creator
```

技能要求先明确并确认插件身份、贡献类型、权限和验收条件，然后才写 package metadata、Entry Point、Manifest、实现、测试、README 与标为 `UNVALIDATED (L1 SOURCE ONLY)` 的 `CANDIDATE.md`。完整边界见 [技能插件 README](examples/plugins/traceh-plugin-creator-skill-plugin/README.md) 和 [ADR-0015](docs/adr/0015-source-only-plugin-candidate-authoring-skill.md)。

L1 结束后，由宿主的 L2 命令独立验证候选。它不会创建 Session、调用模型或把候选装进宿主 Python：

```powershell
traceh plugins validate <candidate-workspace> `
  --core-project <trusted-traceh-git-repository> `
  --output <new-evidence-directory> `
  --allow-index
```

离线时把最后一项换成 `--wheelhouse <directory>`。L2 使用可信核心 `HEAD` 的静态版本、两套临时 venv、宿主 metadata/doctor/pytest 门禁和完整核心回归；它拒绝源码 Junction/reparse point 与宿主管控包名，候选执行后还会重审 Wheel 并核对初审 SHA-256。普通失败原子提交无 Wheel 报告，报告提交本身失败则不留下输出目录；13 道门禁全过后才把精确审计字节与报告作为一个目录事务发布。虚拟环境不是 OS 沙箱，陌生候选仍应放进容器或远程 Sandbox。完整决策见 [ADR-0016](docs/adr/0016-independent-plugin-candidate-validation.md)。

L2 通过后，L3 对比**同一个**审计 Wheel；不会重新构建候选：

```powershell
traceh plugins compare <l2-evidence-directory> `
  --core-project <trusted-traceh-git-repository> `
  --suite benchmarks/evolution/python_quality_v1 `
  --output <new-comparison-evidence-directory> `
  --allow-index
```

离线时同样改用 `--wheelhouse <directory>`。Suite 必须位于 L2 报告记录的可信核心提交内；L3 先把核心、候选与全部传递依赖一次性冻结为带 SHA-256 的 Wheel 集，再让 baseline 与 candidate 从同一 Wheel 集离线安装并核对 Distribution receipt，只有 candidate 启用目标插件。传给嵌套 Tool/Verifier pip 的 Wheelhouse 会编码成单个本地 `file://` URI，含空格目录不会被拆开，原始路径、复合值和远端位置不会穿过环境清洗。宿主 Probe 通过真实 Runtime、Session Event Log、Verifier、不变量和请求重建收集证据；正常返回还必须有匹配且闭合的 durable `turn/end`，每个 `composition/snapshot` 也必须记录该臂预期的插件身份。最后只分类，不产生批准或安装权限。内置 Python Quality v1 是三个确定性合同案例，不代表通用 Coding Benchmark 或真实模型效果。完整决策见 [ADR-0017](docs/adr/0017-host-owned-baseline-candidate-comparison.md)。

L3 得到 `improved` 且没有回归后，L4 仍不会自动安装。先对显式目标 Python 生成审阅卡：

```powershell
traceh plugins promote <l2-evidence-directory> <l3-evidence-directory> `
  --target-python <target-venv-python> `
  --registry <promotion-registry> `
  --output <new-review-directory>
```

审阅 `report.md` 后，若接受其中的能力、目标和风险，用新的输出目录交回完整摘要：

```powershell
traceh plugins promote <l2-evidence-directory> <l3-evidence-directory> `
  --target-python <target-venv-python> `
  --registry <promotion-registry> `
  --output <new-promotion-directory> `
  --approve <full-approval-sha256>
```

L4 会把 L3 重建为完整 Case/两臂/汇总/固定 Gate/冻结 Wheel 证据，只有外形的骨架 JSON 不能生成审批。摘要绑定 L2/L3 原始字节、Wheel SHA、Registry、目标解释器身份、完整 Distribution receipt、`site-packages` 内容摘要、规范包所有者和当前托管状态；任何变化都要重新审批。目标的非候选依赖必须与 L3 一致，L4 只用 `--no-index --no-deps` 安装已审计 Wheel，不在批准之后另解一套依赖；`plugins doctor` 返回后还会重新读取包清单并逐字节摘要安装包目录（排除可再生 `__pycache__`），插件 import/health 即使只改文件、不改版本，也会失败并回滚。Review 输出与 Registry 必须在目标 Python 环境之外。推广报告会给出 `promotion_id`，回滚时必须显式写回 Distribution 和该 ID：

```powershell
traceh plugins rollback `
  --target-python <target-venv-python> `
  --registry <promotion-registry> `
  --output <new-rollback-directory> `
  --plugin-id <plugin-id> `
  --distribution <canonical-distribution-name> `
  --current-promotion-id <promotion-id>
```

目标环境旁的固定宿主协调目录只按规范目标环境持有一份跨进程锁和 Owner，不依赖调用进程的 `TEMP`、解释器别名、Registry、plugin id 或 Distribution。因为每条推广状态都保存整份环境 receipt，L4 v1 同一目标环境只允许一条受管 Distribution 链；另一 Distribution 要等当前链完整回滚为未安装、释放 Owner 后才能接手，不能各写一份互相矛盾的完整环境事实。Registry 仍按“目标 + Distribution”用 `stable/installing/rollbacking` 保存当前链的精确产物、收据与回滚目标；硬崩溃留下的未完成状态也只能通过显式回滚收敛。若首次推广恰好死在 Owner/记录已写而首个 `installing` 尚未写入的窗口，rollback 只会在精确记录与目标仍未安装相互印证时重建该前状态，否则 fail-closed。它仍是同一用户权限下的包管理，不是 OS 沙箱，也不会替运行中的 Runtime 自动启用插件。完整决策见 [ADR-0018](docs/adr/0018-human-approved-exact-plugin-promotion.md)。

插件 Provider 与 Verifier 不会因为插件被启用就自动接管。必须显式选择：

```powershell
traceh run <workspace> "任务" --plugin my.plugin.id --provider my.provider --model my-model
traceh run <workspace> "任务" --plugin my.plugin.id --plugin-verifier my.verifier
```

自定义 Provider 必须同时显式启用插件并指定 Model；命名 Verifier 也必须显式启用插件，并与 `--verify-command` 互斥。

### 在运行中的 Chat 切换组合

插件必须已经安装并能被当前进程的 Entry Point discovery 发现。`traceh chat` 回到空闲提示符后，可以使用：

```text
/plugins                 # 显示当前外部插件 id/version
/plugins reload          # 重建并发布当前组合
/plugins use ID [ID ...] # 切换到明确指定的插件
/plugins use --none      # 只保留 traceh.core
```

这些命令不创建 Turn、消息或模型请求。身份发生变化时，Runtime 会先完成候选 ActivationSet 的 setup、冲突检查和 health check，再以 append-only 的 `composition/migration-authorized` 事件授权**当前 Session**；同身份 reload 不追加迁移事件。持久化账本仍有未闭合 Turn/Step 时会在候选构建前拒绝迁移；`/session` 和退出时的恢复命令按 Session 最新 durable 插件身份生成，因此 fail-closed 窗口也不会提示已失效的旧组合。未知命令只显示固定帮助提示，不回显整条输入。它不会执行 `pip install/uninstall`、`importlib.reload()`、文件监听或 Wheel 替换。失败窗口按 may-have-committed 规则对账；授权已落盘但新 Generation 无法发布时，Session 会 fail-closed，而不是偷偷继续旧组合。

## 运行内置 Benchmark

`traceh eval` 是**唯一**的 benchmark 通路。它跑的就是 `traceh chat --product-config` 那条 ProductTask 主线：真实确认、固定 Workflow、managed Git worktree、不可变 Patch Artifact、冻结 Verifier、Review 和 Git ref compare-and-swap 推广。

```powershell
PYTHONPATH=src python -m traceh.cli.main eval benchmarks/product_v1 `
  --output <一个尚不存在的证据目录> `
  --provider openai-compatible --base-url <url> --model <model>
```

- `--output` 必须尚不存在；每次 attempt 在 `attempts/<NNN>/` 下留下自己的源仓库、一次性 bare target、事件流、worktree 和 CAS，运行结束后写 `report.json` 与 `report.md`。失败或取消不会删除任何证据：attempt「干净」的含义是所有 owner 已收敛，而不是证据被删掉。
- Manifest（`benchmarks/product_v1/benchmark.json`，schema 1、精确键集）只能命名 Profile、三个角色槽位与 Budget、Router 上界、任务总 Budget、冻结 VerificationPlan、capture 上限、arms 和 tasks。它**不能**命名仓库、推广目标、provider、model、节点、边、Agent 数量或 approval digest——每次 attempt 的源仓库和一次性本地 bare target 都由 Runner 自己创建，因此这条命令在结构上无法接触真实远端。
- provider/model 来自 `--provider` / `--model`（或 `TRACEH_PROVIDER` / `TRACEH_MODEL`），一次运行的所有 arm 使用同一个模型族，报告会记录它是哪一个。
- 报告按**解析后**的模式聚合：`auto` 的结果计入 Router 实际选择的那个 arm，`auto` 只单独报告路由是否严格解析成功、路由 Token 和路由耗时；它不是第三个质量 arm。
- 只有一次观测的 arm 会在两份报告里标注 `single observation`；聚合只有计数、总和、最小、最大和均值，不声称统计显著性。
- `approval wait` 单独计时并从 `active elapsed` 中扣除；本 Benchmark 使用 `approval_policy: programmatic-immediate`（宿主对自己的一次性本地目标立即批准），两份报告都会写明。普通 Chat 仍然必须由人执行 `/task approve`。
- 无法从持久事实可靠推出的指标报告为 *unavailable*，绝不填 0。`UsageQuality.UNKNOWN` 的用量报告会让该 Session 的 Token 总数变成 unavailable。
- F2 把 model Attempt 数、retry wait、Provider-active 时间、稳定 failure category 与最终模型结果加入同一份 durable 报告；失败 Attempt 的 Usage 只有可信时才计入 Token，否则仍是 unavailable。retry 后成功是可靠性数据，不是模型质量提升。
- 退出码回答的是「度量是否完成」：全部 attempt 可度量且每个任务的实验条件一致时为 `0`，否则为 `4`。某个编码任务失败是数据，不是工具错误。

v0.6 的 `*/case.json` 布局被**明确拒绝**（`benchmark-legacy-manifest-rejected`），不做升级、不留适配层，也不会删除旧数据。完整设计决定见 [ADR-0033](docs/adr/0033-product-task-benchmark-as-the-single-eval-path.md)，Benchmark 自身说明见 [`benchmarks/product_v1/README.md`](benchmarks/product_v1/README.md)。

## 使用 OpenAI-Compatible 接口

TraceHarness 会自动读取当前目录下的 `.env`。进程环境变量优先于 `.env`，显式 CLI 参数又优先于二者。复制模板，并确保生成的 `.env` 始终只保留在本地：

```powershell
Copy-Item .env.example .env
```

如果使用阿里云百炼北京地域，可以这样编辑 `.env`：

```dotenv
TRACEH_PROVIDER=openai-compatible
TRACEH_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
TRACEH_MODEL=qwen-plus
TRACEH_API_KEY_ENV=DASHSCOPE_API_KEY
DASHSCOPE_API_KEY=replace-with-your-api-key
```

确认配置已经加载，同时不会打印秘密：

```powershell
traceh doctor
```

之后无需再传 API 配置参数即可正常运行：

```powershell
traceh run . "Inspect the project and report what should be improved"
```

使用 `--env-file path/to/file.env` 可以选择其他配置文件。支持的 Runtime 配置包括 `TRACEH_PROVIDER`、`TRACEH_BASE_URL`、`TRACEH_MODEL`、`TRACEH_API_KEY_ENV`、`TRACEH_DATA_DIR`、`TRACEH_MAX_STEPS`、`TRACEH_VERIFY_COMMAND`、`TRACEH_PLUGIN_VERIFIER`，以及 `TRACEH_MODEL_RETRY_MAX_ATTEMPTS`、`TRACEH_MODEL_RETRY_MAX_ELAPSED_SECONDS`、`TRACEH_MODEL_RETRY_BASE_DELAY_SECONDS`、`TRACEH_MODEL_RETRY_MAX_DELAY_SECONDS`、`TRACEH_MODEL_RETRY_AFTER_CAP_SECONDS`、`TRACEH_MODEL_RETRY_JITTER_RATIO`。`openai-compatible` Provider 必须显式配置 Base URL 和 Model；TraceHarness 不会静默选择某个厂商地址、模型或插件能力。

仍然支持等价的进程环境变量配置：

```bash
export OPENAI_API_KEY=...

PYTHONPATH=src python -m traceh.cli.main run ./your-project \
  "Investigate the failing tests and fix the smallest root cause" \
  --provider openai-compatible \
  --base-url https://your-endpoint.example/v1 \
  --model your-model \
  --verify-command "python -m pytest -q"
```

OpenAI-compatible Adapter 使用 `/chat/completions` 和非流式 HTTP，本身没有 SDK/内部 retry。v0.8-F2 的宿主策略默认包括第一次在内最多 3 个 Attempt、总 retry window 30 秒、base/max delay 0.5/4 秒、Retry-After cap 8 秒和 jitter 0.2；只候选 DNS、timeout/408、TLS EOF、disconnect、429 与选定 5xx，认证/权限/bad request/config/protocol/unknown 一次即停。每次 retry 都是同一 Step 的新 durable Attempt 与独立 Budget reservation，不换 Provider/model/request。程序化 Runtime 默认 `NO_MODEL_RETRY`，只有 composition root 显式提供 policy 才启用；没有 Provider/model fallback。完整决定见 [ADR-0037](docs/adr/0037-typed-provider-failures-and-bounded-model-retry.md)。

Tool arguments 平常仍必须是严格 JSON。为处理已在真实 OpenAI-compatible Endpoint 上复现的 multiline
字符串形态，标准 JSON 失败后只允许一条受冻结 Tool schema 约束的 lexical extension：顶层字段必须明确
声明为 `type=string`，且只规范化双三引号 multiline token；规范化后的整体仍须由标准 parser 解析为
object。未知字段、非字符串/嵌套字段、表达式、注释、尾逗号和其他 malformed response 继续作为不可重试
protocol failure；没有 `eval`、JSON5、Provider/model fallback 或按模型/Tool/任务硬编码。见
[ADR-0038](docs/adr/0038-schema-gated-multiline-tool-arguments.md)。

取消 Turn 无法中止已经发出的 `urllib` 请求。Provider 不会把 Worker 丢在后台，而是先等待请求收敛，再重新抛出取消，因此 CLI 宣布 Turn 结束后不会还有后台 Worker 继续与 Endpoint 通信。最坏情况下需要等到 Provider Timeout 到期；这是等待收敛，不是立即中止。

## 在 Python 代码中使用

```python
from pathlib import Path

from traceh import ScopeKind, ScopedPromptBinding, ScopedServiceBinding
from traceh.api.llm import ModelResponse
from traceh.api.prompts import PromptSection
from traceh.api.services import ServiceKey
from traceh.llm.scripted import ScriptedLlmProvider
from traceh.runtime.agent_runtime import RuntimeConfig, build_default_runtime

provider = ScriptedLlmProvider((ModelResponse(content="done"),))
runtime = build_default_runtime(
    RuntimeConfig(
        data_dir=Path(".traceh"),
        provider="scripted",
        model="demo",
    ),
    provider=provider,
    # 示例：Agent 层显式覆盖 Application 层的同一 Service 合同。
    service_bindings=(
        ScopedServiceBinding(
            ScopeKind.APPLICATION,
            ServiceKey("example.telemetry", 1),
            object(),
        ),
        ScopedServiceBinding(
            ScopeKind.AGENT,
            ServiceKey("example.telemetry", 1),
            object(),
            replace=True,
        ),
    ),
    # 示例：只让这个 Agent 的模型看到这一段 Prompt。
    prompt_bindings=(
        ScopedPromptBinding(
            ScopeKind.AGENT,
            PromptSection("example.agent", "Answer with concise evidence."),
        ),
    ),
)

result = await runtime.run(Path("./workspace"), "Inspect the project")
await runtime.dispose()
```

`build_default_runtime()` 现在要求调用方显式传入 `EventStore`，Runtime 只借用而不负责关闭；CLI、每个 Evaluation attempt 和每个 Evolution comparison case 各自在 composition root 打开/关闭唯一生产 `SqliteEventStore`。Factory 还可以接收自定义 `ContinuationRuntime`、额外的 `Tool`、Policy、Prompt Assembler、Verifier，以及显式 `ScopedServiceBinding`、`ScopedToolBinding`、`ScopedPromptBinding`、`ScopedPolicyBinding`。同层替换或更近层覆盖祖先都必须写真正的布尔值 `replace=True`（字符串等 truthy 值会被拒绝）；Service 的 API Major 还必须一致。失败的四层装配不会把半成品写进调用方 Registry/Prompt。Tool、Prompt、Policy 会先解析成一份有效 Composition，再由 Generation/Step Lease 和 Snapshot 冻结；没有第二套 Scoped Runtime。

## 架构

```text
CLI / SDK / Evaluator
        |
   AgentRuntime
        |
    AgentLoop                 <- 最小控制流
   /    |     \
Prompt  LLM   ToolRuntime     <- 可替换能力
   \    |     /
 RequestBuilder
        |
Session Stream + Effect Stream
        |
Projectors / Recovery / Inspector / Invariants
```

主控制循环不知道 Tool 的具体实现方式、Provider 怎样调用模型、Verifier 怎样判断成功，也不知道未来如何发现插件。它只协调稳定的服务边界。

### 事实源

- 持久化运行事实：Session Stream；
- 外部副作用事实：Effect Stream；
- Runtime State：由事件投影得到；
- 模型可见历史：由事件投影得到的 Surface；
- Model Request：Surface 与冻结 Composition Snapshot 的函数。

### 扩展边界：哪些已经可用，哪些还只是接口

第三方插件**现在就能**通过 `PluginContext` 提供：

| 能力 | 插件调用 | 进入哪条主线 |
|---|---|---|
| 新 Tool | `context.register_tool()` | 既有 `ToolRegistry` 与 Tool Runtime |
| Prompt 扩展 | `context.register_prompt()` | 既有 `PromptAssembler` |
| 服务 | `await context.provide()` | 既有 `ServiceRegistry` |
| Model Provider | `context.register_provider()` | 候选 `LlmRegistry`；必须显式选择 |
| Tool Policy | `context.register_policy()` | 既有 Tool Runtime admission 链 |
| Tool Middleware | `context.register_middleware()` | 既有 Tool Runtime 执行链 |
| 命名 Verifier | `context.register_verifier()` | 同一 Step Generation Lease；必须显式选择 |
| 可逆清理 | `context.add_cleanup()` | 该插件的 `Activation` |
| 后台任务 | `context.spawn_owned()` | 该插件的 `OwnedTaskSet` |

以下边界**在代码中存在**，但插件还不能提供它们，需要直接装配 Runtime：

| 未来能力 | 当前已有边界 |
|---|---|
| 自定义完成行为 | `ContinuationRuntime` |
| 新持久化后端 | `EventStore`；仍是进程级固定事实源，不能跟随 Step Generation 切换 |
| 可观测性 | 类型化 NOTIFY Hooks；进程内 `SessionEventFeed` 订阅 |
| Step 安全的多代 Generation 与组合切换 | `CompositionRuntime.lease()`、Generation/Lease/Drain、`traceh chat /plugins` 控制面；仍没有 Wheel/module 级热替换 |
| Agent 专属装配 | 四层 `ScopeChain` 与程序化 Tool/Prompt/Policy Overlay 已接入默认 Runtime；插件仍不能在子层 setup |
| 子 Agent | `traceh.supervision.SupervisorToolset` 已提供绑定 owner 的 spawn/send/wait/stop/collect 五个普通 Tool；宿主必须把它们装进 Agent 的 ToolRegistry，并明确解析 preset/workspace |
| 多 Agent Workflow | 未来调用 `ProcessAgentSupervisor` 的 Workflow 层 |

延伸阅读：[插件说明](docs/plugins.md)、[ADR-0007](docs/adr/0007-transactional-plugin-activation.md)、[ADR-0009](docs/adr/0009-generation-owned-plugin-activation-set.md)、[ADR-0010](docs/adr/0010-session-plugin-composition-migration.md)、[ADR-0012](docs/adr/0012-four-layer-service-scope.md)、[ADR-0014](docs/adr/0014-generation-scoped-plugin-execution-capabilities.md)、[架构说明](docs/architecture.md)和[插件演进说明](docs/plugin-evolution.md)。

## 重要设计选择

### 请求可以重建

调用 Provider 前，TraceHarness 会持久化：

- 实际使用的 Provider 和 Model；
- 组装后的完整 System Prompt；
- 模型实际可见的 Tool Schemas；
- Composition Revision；
- Surface 边界序号；
- 完整 Request Snapshot 与 Fingerprint。

`traceh replay` 会根据更早的持久化事件重新构建请求，并报告 Fingerprint 不一致。

### 不盲目重试副作用

Tool Runtime 在派发前写入 `effect/intent`，操作完成后写入 `effect/outcome`。如果进程在中间崩溃，除非持久化证据可以证明结果，否则 Recovery 会把操作标为 `unknown_after_crash`。系统不会仅仅因为缺少 `tool/result`，就重复执行写入或进程操作。

### 完成必须有证据

配置 `--verify-command` 后，最终模型响应必须接受真实 Workspace 上的命令检查。Verifier 失败时，其证据会在 Step 与重试预算范围内反馈给下一 Step。

取消或超时 Verifier 不会让命令留在后台：子进程会先 terminate，必要时再 kill，直到确认退出后调用方才继续。子进程还会使用 `PYTHONUTF8=1` 和 `PYTHONIOENCODING=utf-8`，因此 Windows 上的 Python 子进程会按 UTF-8 输出非 ASCII 文本，而不是跟随系统代码页。非 Python 原生工具仍然遵循控制台代码页。

## 项目目录

```text
src/traceh/version.py   版本与核心身份的唯一事实源
src/traceh/api          稳定协议、冻结 DTO 和扩展边界
src/traceh/kernel       Scope、Activation、Hooks、可逆生命周期
src/traceh/plugins      Entry Point 发现、显式启用、事务式 PluginManager
src/traceh/session      SQLite EventStore、Projector、Recovery、不变量
src/traceh/runtime      AgentLoop、Composition Lease、Request、Continuation、Verification
src/traceh/supervision  Agent Activation、durable claim/outcome、child-first 收敛、运行报告与子 Agent Tool
src/traceh/budgets      单一层级 Budget Ledger 与宿主显式执行门
src/traceh/workspaces   commit-pinned Git worktree 生命周期与 Tool Policy
src/traceh/artifacts    immutable Patch Manifest、Git capture、SHA-256 CAS 与只读报告关联
src/traceh/promotion    固定 Verifier、Review/Approval/Promotion Ledger 与 Git ref compare-and-swap
src/traceh/workflow     固定 Typed DAG：AgentTask/Map/Join/Verification/Approval 与一条编排账
src/traceh/api/product  v0.7-F0 冻结的统一 Chat 产品面合同（纯协议、无 I/O）
src/traceh/product      v0.7-F1–F3 / v0.8-F5 ProductTask 事实、固定装配、fresh activity/memory read models 与可选 Chat 产品控制面
src/traceh/llm          Provider Registry、Adapter、typed failure 与 bounded retry policy
src/traceh/tools        Policy、调度、Effect 和内置 Coding Tools
src/traceh/inspector    文本 Replay 和静态 HTML Trace
src/traceh/evaluation   v0.7-F4 ProductTask Benchmark：manifest、一次性仓库、durable 指标与报告
src/traceh/evolution    L2 验证、L3 对比与 L4 人工批准/推广/回滚控制面
examples/plugins        可独立构建的示例、Python Quality 与 Plugin Creator Distribution
tests                   契约、恢复、取消、插件和端到端测试
```

## CLI 命令

```text
traceh run
traceh chat            # 支持 --no-timeline / --heartbeat-seconds；--product-config 显式启用产品面
                       # --tui 下顶部有一行 Context 状态条，Ctrl+X 打开上下文详情
                       # 自动压缩需一起给出 --auto-compact on --auto-compact-bytes N
                       # --auto-compact-summary-bytes N --auto-compact-keep-turns N
traceh resume
traceh recover
traceh inspect
traceh replay
traceh compact          # 手动 Surface 压缩；--through-seq 必须精确等于某个闭合 Turn 的 turn/end 序号
traceh sessions
traceh eval             # ProductTask Benchmark；--output 必须尚不存在，度量不完整时退出码 4
traceh plugins list     # 只读元数据，不 import 插件
traceh plugins inspect  # 同上，针对单个插件
traceh plugins doctor   # 会 import、setup、health check，随后立即 dispose
traceh plugins validate # Runtime 外构建/审计/测试 L1 候选，失败退出码 8
traceh plugins compare  # 复用精确 L2 产物跑固定宿主对比，失败退出码 9
traceh plugins promote  # 先审阅摘要，再按同一摘要推广精确 Wheel，失败退出码 10
traceh plugins rollback # 按当前推广 ID 恢复上一份精确托管状态，失败退出码 10
traceh doctor
```

`run`、`chat`、`resume` 支持 `--plugin`（可重复）；`inspect`、`replay`、`recover`、`compact`、`sessions` 不启用插件，因此也不接受该参数。

使用 `traceh <command> --help` 查看详细参数。

## v0.8.0 发布候选状态

当前工作树已经进入 v0.8-F5，并把唯一版本事实源切到 `0.8.0`；最新正式发布仍是
`v0.7.1`。F0–F4 原实现和各 Release Stop 已完成，F5 也曾对当时候选完成全局审查与一次
`2496 passed, 7 skipped` 全量；但发布前真实体验随后直接替换了旧 TUI presentation，该历史全量不能
认证当前代码。新版 TUI 已按单一浅色标准直接替换，确定性 Pilot 与真实本地 Git Product 主线已通过；replacement 独立审查发现的
两项 P1 与两项 P2 已按根因修复、反向验证并经短复审清零；后续真实体验发现的 Product Chat START 前
权限问题、Plugin ActivationSet eager-cleanup 死锁和旧终态任务遮蔽下一项 START 的精确身份交接问题也都
已按各自 owner 根修并完成确定性反例/反向验证。其后真实 `qwen-plus` 流程暴露的 multiline Tool arguments
严格 JSON 失败，已在原 Provider 边界以 schema-gated parser 根修；严格 decoder 同时拒绝非 JSON 的
`NaN`/正负 `Infinity`。失败面板也会从 exact Workflow message Turn 投影稳定叶子错误。修复后已亲自从
全新目录走通 Proposal → typed START → auto/single → Review → typed
Approval → bare target Promotion，推广后的 4 个 unittest 通过，Session/Budget/Workspace 全部收敛。
这条定向真实验收不替代 18-attempt 网格。Provider、TUI、失败证据与跨 owner 复审曾清零
P0/P1/P2，最新相邻 owner 回归为 `251 passed`；Textual gate 焦点交接也已在原 presentation owner 根修。
其后的产出可见性工作又在**同一个 TUI** 中增加 fresh `Ctrl+T` 角色对话、fresh `Ctrl+P` 完整身份，并
恢复 Review evidence，没有旧路径或兼容层。此前 `2555 passed, 7 skipped`、退出码 0、耗时
`3078.80s (51:18)` 的完整全量因此只保留为该改动前的历史证据，不能认证当前工作树。用户体验随后发现
批准完成后左栏没有宿主反馈的 P2，已在原 adapter 根修并反向验证。其后的 M1 已完成完整 Patch/任务对话
出口并通过 `65` 项 TUI 与 `56` 项相邻测试、`2638` 项带 Textual 的 collect-only；这些仍只是 M1 检查点。
M2 又把单任务 format-5 投影升级为 format-7 同 Session 有界任务目录、当前 focus 最小执行摘要和按需
`read_product_task_evidence`，未增加 Memory 域或第二事实源；format 6 只保留为历史协议。当前 M2 的
九模块定向组 `172 passed`、完整 TUI 组 `82 passed`、Product F3 E2E `26 passed`、collect-only `2665`
项；仓库外精确候选提交通过真实 L2 后，唯一最终全量为 `2658 passed, 7 skipped`、退出码 0。完整证据见
[`docs/validation-v0.8.0.md`](docs/validation-v0.8.0.md)。clean-input Wheel/sdist/source ZIP、无 `[tui]`/
带 `[tui]` 离线安装、真实 Provider 网格、commit、push、tag 与 GitHub Release 仍未执行；候选尚未发布。

v0.8.0 候选只整合已有主线：SQLite 是唯一生产 EventStore，旧 JSONL 明确拒绝；retry 不换 Provider、
模型或冻结请求，也没有 fallback；Line/TUI 共享同一 Driver、Product control/observation 与人工
START/Approval/Promotion 权限。Plugin Creator 和 Python Quality 独立 Wheel 各升为 `0.2.2`，它们自己
的 Distribution/Manifest 明确覆盖 0.8；Creator 为新候选生成 `traceharness-py>=0.8,<0.9`，核心
validator 没有被放宽。过程证据见 [v0.8.0 验证记录](docs/validation-v0.8.0.md)。

## v0.7.1 维护版与 v0.7 已发布能力

最新已发布版本是 `v0.7.1`。这个维护版只修三条已复现边界：ProductTask 开始前必须由终端用户
对屏幕上精确 task 输入 `START`，模型的确认 Tool Call 只能请求这次宿主提示；
AgentLoop 的取消收尾由一个 owned Task 顺序写完 Attempt/Step/Turn，重复 Ctrl+C
不能让调用方提前返回；L4 在 `-I -S` 下检查目标 venv 时显式使用 `venv`
sysconfig scheme，并拒绝逃出目标前缀的包目录。发布门禁还修正了两个独立示例
插件遗留的 `<0.7` 元数据：Plugin Creator 与 Python Quality `0.2.1` 的 Distribution
依赖和 Manifest 现在都覆盖 0.7，真实 Wheel 可以与 0.7.1 核心离线共存。它没有增加 retry/fallback、
第二个 Workflow/Benchmark、默认 Product Profile 或新的模型权限。

- 插件 setup 只支持 **application scope、trusted、进程内**：`trust_mode="isolated"` 可以在 Manifest 中声明，但会被明确拒绝。D1/D2 的四层能力是宿主程序显式装配的借用型 Service/Tool/Prompt/Policy binding；插件还不能在 Workspace/Preset/Agent 层 setup；
- **切换边界**：空闲 `traceh chat` 支持 `/plugins`、`/plugins reload`、`/plugins use ID...` 和 `/plugins use --none`。它只重做当前进程已经能发现的 Entry Point 激活，不是运行中 pip install/uninstall、Wheel 替换、强制 module reload 或文件 watcher；旧 Generation 仍要等 Lease 归零后才 cleanup；
- 插件现在可以提供 Tool、Prompt Section、Service、`LlmProvider`、`ToolPolicy`、`ToolMiddleware` 和命名 `CompletionVerifier`；Provider/Verifier 必须显式选择。插件仍**不能**提供 `EventStore`，因为账本是 Runtime/Session 的进程级事实源，尚无独立于 Step Generation 的固定所有权；
- 没有 MCP 接入面，也没有通用 Workflow DSL。v0.7-F3 已给现有 `traceh chat` 增加**可选** `--product-config` 产品面：模型可在自然聊天中建议结构化提议/确认，但 v0.7.1 要求终端用户对宿主显示的精确 task 再输入固定 `START`，之后才启动 fixed single/multi/auto Workflow；模型 Tool 不能代替这个动作。启用 Product 配置时，START 前请求者 Chat 只装配 list/read/search 与提议/确认 Tool，并按声明的 EffectKind 拒绝 effectful core/plugin Tool；代码副作用只属于 START 后的 Product coder。`/task inspect|approve|reject|cancel|abandon TASK_ID` 按持久身份 fresh replay；Workflow 自己仍不推广，批准只来自宿主命令。配置必须显式给出 Profile、source、Budget、Verifier、managed root、CAS 与 bare target，没有默认 profile，也不能携带 DAG/Prompt/approval 值；F3 v1 还要求 Product Profile 的 provider/model 与当前 Chat 完全相同，并只支持可直接共享 Provider 对象的内置 Provider，不接受插件 Provider；不传配置时仍是原来的单 Agent Chat；
- Session 记录创建时的插件身份和后续真正使用过的 Composition；插件集合改变后，只有用户在当前空闲 Session 执行 `/plugins use ...` 才会追加 `composition/migration-authorized`，没有授权的旧 Session 仍拒绝继续，其他 Session 不会自动迁移。版本按 PEP 440 等价判定，因此 `1.0` 与 `1.0.0` 不算变化；授权已落盘但 publish 失败时 Session fail-closed；
- 插件的 Owned Task 只有**生命周期所有权**，没有监督器：它们的异常会被取回（因此不会冒 `Task exception was never retrieved`），取回之后**立刻丢弃、不留存**，也不会被重启，更**不会**把后台任务失败升级成 Runtime 故障；
- L1 Plugin Creator 的“专用 Candidate Workspace”和“不执行候选”是流程合同，不是沙箱；它只产出未验证源码；
- L2 可以独立 build/audit/doctor/test 并跑可信核心回归，但两套 venv 仍不是 OS 沙箱，候选代码拥有当前用户权限且只保证直接子进程收敛；L2 也不比较能力好坏、不做人工批准、正式安装或回滚；
- L3 使用精确 L2 Wheel 和可信核心中的固定任务做确定性 baseline/candidate 对比；它仍不是 OS 沙箱或真实模型 Benchmark，也不批准、安装、晋升或回滚插件；
- L4 只接受 `improved` 且零回归的精确 L2/L3 证据，但它仍不是 OS 沙箱或包签名系统；目标依赖必须已经与 L3 receipt 一致，L4 v1 不解析或升级依赖、也不同时管理同一环境中的多条 Distribution 链，不会把推广自动应用到正在运行的 Runtime；
- `ProcessAgentSupervisor` 是**进程内**的：它保证一个 Agent 在自己名下最多一个活实例，并在完整 Acceptance/claim 归属校验和 durable claim 落盘后才运行 Turn；open claim 会阻塞后续 FIFO，关闭按 owner 子树 child-first 收敛在途 create/resume 与 Runtime cleanup。Stage E Tool 只允许操作绑定 owner 的后代，并从账本重建 run report。v0.7-F3 只在显式 Product 配置下从 CLI 外层装配 Supervisor/Budget/Workspace/Artifact/Workflow/Promotion，核心仍不知道产品；F2 的模型 retry 不接管 stale claim，也没有 Workflow/Tool retry、通用 Workflow DSL 或跨进程 lease；
- v0.7-C managed Workspace 只接受宿主 source mapping，固定到精确 Git commit，并双向核对 `.git` marker 与唯一 worktree admin registry entry；dirty/unsafe/unknown worktree 会 quarantine，Agent 停止不会自动删除它。Wrapper 的 `resume()` 后置 Workspace 复核也必须在 `aclose()` 返回前收敛。Read-only 是显式 Tool Policy，不是 OS sandbox；
- v0.7-D1 可由宿主把一个 terminal message 对应的 staged/unstaged/untracked/deleted/binary/mode 状态冻结为完整 candidate tree、binary Patch、SHA-256 CAS bytes 和 append-only Manifest。它使用临时 index 与 Workspace capture gate，raw tree diff 递归到 leaf entry，因此新目录中的普通文件不会被目录容器 mode 误拒绝；Catalog 重算派生身份，CAS 逐层拒绝 reparse 父链，Git 子进程不继承宿主 `GIT_*` 注入；`collect_agent_artifact` 仍只读；
- v0.7-D2 可由宿主把一个不可变 Patch 在临时 clone 中应用到精确 target revision、跑固定 Verifier、记录 immutable Review Report，再凭人工提交的 exact approval digest 用 `git update-ref <ref> <new> <expected-old>` 推广到宿主管理的 **bare** 仓库。集成 diff 同样递归检查普通文件 leaf，仍拒绝 symlink/gitlink mode。Verifier 以同一用户权限运行，是能力与证据边界而不是 OS sandbox；D2 域本身没有 CLI、自动批准、非 bare 目标、跨进程 lease，也没有模型可见的 approve/merge/promote Tool；它由 v0.7-E 的 Verification/Approval 节点作为公共服务调用；
- v0.7-E 可由宿主用固定 Typed DAG 把上述公共服务组合成 AgentTask/Map/Join/Verification/Approval 五类节点。定义只带宿主 binding id，不带 Prompt、路径或策略；Agent/message/review/map child 身份由 run 与 node 派生，重入是重新读取而不是重跑；Approval 是人工屏障，Workflow 自己不能批准；除“干净停在 Approval 屏障”外的任何中断状态都 fail closed。它不是第二个调度器，也没有 CLI、重试策略或跨进程 lease；
- v0.7-F0 只新增公共合同模块 `src/traceh/api/product.py` 与它的合同/架构测试：冻结 ProductTask 的九种事件与精确 key 集**、允许的状态迁移与跨事件取值一致性**、`task-opened` 绑定用户确认的 preflight 与确认身份、single/multi 两条固定拓扑、角色由 Profile 槽位唯一决定因而只有 coder 可写、Approval/Promotion 永远是宿主操作且模型拿不到 approval digest/Patch SHA/精确 revision、`interrupted` 只作为派生视图而不写事件、临时 Proposal 及「确认必须是不同消息并由持久顺序证明发生在 Proposal Turn 结束后」，以及定长 Profile、带 resolved assembly digest 的 preflight binding 与可计算 digest 的 Assembly Receipt。**F0 本身没有实现包**；
- v0.7-F1 新增独立域 `src/traceh/product/`，把 ProductTask 变成真实持久事实：严格 parser 与唯一投影（形状/顺序/跨事件取值三层）、fresh reader（未开启的任务返回 `None`）、九种事件的宿主写入（精确 payload 幂等、用重放得到的 seq 做 CAS、共享三态对账、owned task 取消收敛），以及每次都重新读三处的派生视图（`unreconciled`/`resumable`/`interrupted`）。Session/Workflow reader 必须和 ProductTask writer 共享**同一 EventStore 对象**；开启任务只接受通过共享 `CoreInvariantChecker` 的 Session 历史，origin 与 confirmation 都必须是由真实 durable `turn/start` 锚定到精确 Turn 的 `source="user"` 消息，并要求确认消息的 accepted seq 晚于 Proposal Turn 的合法 durable end。opening 只规范化一次，payload/授权/Session 证据共用同一组内建身份；每条 Product 事件也只解析一次，投影与 operation 对账共用脱离后的 payload，且通过 `str.__str__` 基类读取固定 Unicode 内容，不执行子类状态化 `__str__`。append 正常返回后的结果重读失败会保留 `committed=True`。所有 preflight/receipt 字段在首次 append 前验证，畸形输入不能先污染流再让 replay 失败。**它只记录，不驱动**：不 import Workflow、Promotion、Artifact、Workspace、Supervisor、Runtime、插件、CLI 或 Provider。仍**没有** Router、Chat 接线、默认 Profile/Registry/Assembly、Workflow 执行或 Promotion 调用；
- F1 的 Session acceptance 会先把 `source/content/target` 脱离成内建字符串，且 `target` 必须精确为 `new_turn`；敌意 `str` 子类不能把其他投递方式伪造成确认。普通 Session Store 读取失败统一为不回显后端细节的 `product-session-unreadable`，调用方控制的 `BaseException` 不被吞掉；
- F1 还要求一个 durable Turn 只能归属一条 claimed message，confirmation 不能借用别的消息已经启动过的 Turn。ProductTask 查询身份在 replay 入口只规范化一次，选择 Stream、核对 payload 和构造 Summary 全部使用同一个内建 `task_id`；
- v0.7-F2 在同一个 `src/traceh/product/` 域补上「确认之后、执行之前」的一段：严格 Router（只认恰好一个 `{"mode", "reason"}` JSON 对象，未知 mode/多余键/超长/畸形/多份答案一律稳定失败，不重试、不猜文本；超时与响应上限只来自显式 `ProductRouterProfile`，live Router 的实际 assembly 摘要也必须对上 fresh preflight，`reason_display` 只展示）、唯一 Profile Registry（没有默认 profile，assembly 必须匹配被问的槽位，写权限来自 `ProductRole` 槽位，Router 不持 Tool——当场强制；Budget 范围复用 Ledger 域的唯一合同）、每次 fresh 解析的 preflight（source 精确 commit、VerificationPlan digest、Promotion target fingerprint/**精确 ref**/expected revision 都来自真实 resolver，漂移即拒绝），以及用 F1 writer 写唯一 `product/task-routed` 并从真正会跑的 Workflow definition 算出 `workflow_definition_hash` 的固定 Assembly。single 固定 `coder → verification → approval`，multi 固定 `parent → reviewer → coder →` 同一安全尾部。它**只出计划，不执行**：不写 `product/task-started`、不启动 Workflow、不捕获、不验证、不批准、不推广、不调用真实模型，也没有任何 Chat 接线或产品命令；
- 生产 Router 请求现在也明确写出 parser 已经执行的完整合同：exact keys 与 mode，`reason` 为 `null` 或非空、单行安全且受共享 `MAX_REASON_DISPLAY_CHARS` 约束的展示文字，无首尾空白或附加散文。Parser 仍然严格拒绝，不截断、不重试、不 fallback；
- v0.7-F3 在现有 `traceh chat` 上增加可选产品 host：Proposal 可带用户明确要求的 single/multi/auto，省略才使用 Profile 默认值；宿主把模式、来源和 prospective task id 一起显示。后续用户 Turn 仍提供 durable 身份/顺序证据，但 v0.7.1 明确要求独立终端 `START` 才写第一条持久任务事实，不从自然语言猜授权。开始后立即打印 task id，并沿用可选 Chat heartbeat 从现有 durable Product/Workflow 视图显示进度；到 Approval 或执行 `/task inspect` 时，宿主从固定 Workflow、Agent Directory、Artifact CAS 与 Review 账本只读拼出节点、Session replay、变更路径、有界 Patch 和 Verifier 结果，证据缺失或被改写时明确提示 unavailable 与不可批准，不新增 Product 状态或事实源。固定两种拓扑复用 Supervisor、层级 Budget、managed Workspace、immutable Artifact、Verifier、Workflow 与 Promotion。到 Approval 可以退出进程，重启后按 task id fresh replay 再批准；Router/装配等 Workflow 前普通失败会先释放资源、再写 task failed，终态和 promotion/cleanup 崩溃窗口均可幂等补齐。Review/Patch/approval/promotion 值不进模型上下文；捕获过的 dirty worktree 只有 exact candidate tree 才能在 merged/rejected 后受控删除；
- Product Profile 中 `budget.max_tokens` 是一个 Agent 整个生命周期累计的 input+output 权限，`max_output_tokens` 是每次 provider 请求的输出上限；role 与 Router 必须同时显式提供，两者均进入 Profile digest。旧 shape 不兼容、不默认、不迁移；现有 Budget ledger、Provider、`AgentLoop` 与 `AgentRuntime` 没有 Product 特例，见 [ADR-0034](docs/adr/0034-separate-product-token-budget-and-request-output-limit.md)；
- v0.8-F0 已把模型调用拆成两阶段：Budget admission 先冻结 exact dispatch request 并创建 Attempt-scoped
  PENDING hold，`AgentLoop` 再以 Session CAS 原子写入 composed/dispatch snapshot 与 Attempt start；只有
  CAS 胜者才能 dispatch，败者先释放 hold。胜者的 concrete admission 还必须绑定 Composition 解析出的
  同一个 Provider 对象与 host Attempt；Budget 只能挂接计费生命周期，不能替换真实 dispatch。Budget
  拒绝不再留下虚假 Attempt，start/end 会绑定 ordinal、snapshot、dispatch fingerprint 与 reservation。
  Chat 最终异常行也统一为一条有界 terminal-safe 文本。
  见 [ADR-0035](docs/adr/0035-two-stage-model-admission-and-session-dispatch-permit.md)；
- 人工审批不会只信一份 Review“内部摘要算得通”：持有冻结 VerificationPlan 的 Promotion owner 会在复用 Review、approve 与 promote 前逐项重验 command id/顺序/`argv_digest`、evidence digest 和 passed；`/task inspect` 与 F4 evidence collector 复用同一规则。即使有人同步重算被篡改 Review 的内部 evidence/approval digest 并让各域身份彼此一致，界面和 Benchmark 仍会 fail closed，直接 `/task approve` 也会在 bare ref 改动前拒绝；Promotion 已落盘但 Product terminal 未写的恢复分支也必须先幂等重入 `promote()`，不能只查 ledger 就补成功；
- v0.7 的阶段顺序、不可偏离原则与最终产品效果统一记录在 [v0.7 总阶段计划](docs/plan/TRACEHARNESS_V0.7_STAGE_PLAN.md)；该计划不替代源码、测试、ADR 或两份项目上下文的事实源地位；
- v0.8 与 v0.9 的范围已经分别冻结在 [v0.8 阶段计划](docs/plan/TRACEHARNESS_V0.8_STAGE_PLAN.md) 和
  [v0.9 阶段计划](docs/plan/TRACEHARNESS_V0.9_STAGE_PLAN.md)；v0.8 收口至 v1.0 的版本列车、Sandbox、
  MCP Client 与受控动态并发 Workflow 边界统一记录在
  [v1.0 总路线](docs/plan/TRACEHARNESS_V1.0_MASTER_PLAN.md)。当前 `0.8.0` 候选的 F0–F4 已完成两阶段
  Model admission、SQLite 唯一生产 EventStore，以及 typed Provider failure + 同冻结请求有界 retry；F2
  已在 Release Stop B 独立复审清零 P0/P1/P2，并完成 F1+F2 集成全量；F3 已完成共享 Chat Driver、
  Line adapter、只读 Product observation 与同一 `traceh chat --tui` Textual adapter；旧 F4 停止点曾
  清零，但发布前体验已在原入口直接替换 TUI presentation，没有保留旧布局。新版已通过定向与真实本地
  Product Pilot；replacement 独立审查的两项 P1 与两项 P2、后续 Product Chat START 前权限、Plugin
  eager-cleanup、task identity handoff、Provider multiline、exact-message 失败投影、110 列断点与 gate
  焦点交接均已根修；Provider/TUI/失败证据与跨 owner 最终复审清零 P0/P1/P2，最新相邻 owner 回归
  `251 passed`，其后完整全量 `2555 passed, 7 skipped`；这些结果均早于 fresh 角色对话/完整身份补齐。
  M1 已在唯一 TUI 主线完成完整 Patch 与不截断任务对话出口，M2 已把 requester Product context 升级为
  format-7 同 Session 有界任务目录、当前 focus 最小执行摘要和同 Session 按需证据 Tool；它仍不是 Workspace
  Memory 或第二事实源。M2 独立复审与唯一最终全量已经通过，结果见验证记录；clean-input 资产、双形态
  离线安装、真实 Provider 网格及发布仍未执行，F5 候选尚未发布。v0.9 在 v0.8 发布后重新批准，沿现有
  Plugin/EventStore 主线实现 Skill、Workspace Memory、
  渐进披露和可审计检索。当前 M2 的 Session task history 不是 Workspace Memory；系统仍没有 Skill/Memory、
  OS sandbox、Provider/model fallback 或
  第二 Benchmark Runner；
- `traceh chat` 是行式交互：已有实时 Tool Timeline、Activity Heartbeat、ProductTask durable 进度/审批证据和可收敛的 Ctrl+C，但没有 Token Streaming、Spinner、颜色，也不能在 Turn 运行期间输入；`traceh run`/`resume` 尚未接入 Timeline。所有 CLI 命令在解析和输出前统一尝试切换 UTF-8，Windows 旧代码页不再让合法的持久 Unicode 文本使 `replay`/`inspect` 崩溃；
- Activity Heartbeat 由 UI-neutral Driver 中唯一的 `ActivityTracker` 产生 typed update，Line/TUI 不各自推断；它仍只是屏幕状态，不写 Event Log、不可事后回查，完成耗时也不进入 payload；需要可审计的时延应在 Provider/Tool 边界落盘；
- 硬中断（`Ctrl+Break`、关闭控制台）没有任何收敛：不打印提示、不闭合生命周期，只能依赖启动时已打印的恢复命令与崩溃恢复；
- Session Event Feed 只在同一进程内可见，没有跨进程实时观察；它的队列无上限，因此不对 Runtime 施加背压，但被遗弃的订阅者会占用内存，且尚无 Overflow 策略；它可丢失、不重放历史、不提升唯一 `SYNC` SQLite append 的保证，不能当作恢复证据；
- Timeline 已对注入做了清洗，但形似结构标记的惰性文本仍可能出现在该行内部：保证是"不会产生第二行、行首为真实事件号"，不是"不会出现形似标记的字符"；
- SQLite 只解决本地事实存储：同库 writer 跨 Stream 有界串行化，默认 busy timeout 5 秒；它不是跨主机数据库，打开时 exact schema/history 校验的成本随历史增长；额外持久 trigger/view/index 会被拒绝，未知数据库先经 immutable schema probe，因此在拒绝前不会触发 hot-journal recovery 或切换 WAL；
- OpenAI-Compatible Adapter 非流式，adapter 内没有 retry；宿主只有 F2 同请求 bounded retry，没有 Fallback Middleware；
- `apply_patch` 执行精确文本替换，不解析 Unified Diff；
- 默认 Shell Policy 是 Guardrail，不是安全沙箱；运行不可信 Agent 时应使用容器或远程 Sandbox；
- Effect Reconciliation 当前是通用实现；Git、远程 API 和事务系统的领域 Reconciler 应由后续插件提供。

## 开发与验证

```powershell
python -m compileall -q src tests
python -m pytest -o addopts='' -q
python -m ruff check src tests
```

F5 RC 前三次修复后的 OpenAI-compatible `qwen-plus` 正式网格都完成 `18/18 measured`：第二轮严格成功 `11/18`、含 6 次 durable TLS EOF；经过 Windows 用户级本机代理的第三轮只有 `3/18`、含 14 次 TLS EOF；第四轮以进程级 `NO_PROXY` 绕过代理后为 `13/18`、0 TLS EOF，但还有 2 次 Router reason 拒绝、1 次 Budget exhausted 和 2 次 DNS `getaddrinfo failed`。历史报告均保留，不能选择性覆盖。

第四轮的两次 Router reason 拒绝随后被定位为模型可见提示没有公开既有 256 字符与单行安全限制；该根因已用真实 Chat → Router → ProductTask 反例和反向验证修复，没有放宽 parser。修复后的第五轮从新目录再次完成 `18/18 measured`，严格成功 `15/18`：requested single `6/6`、multi `4/6`、auto `5/6`；auto `6/6` 严格解析且全部归入 single，reason 拒绝归零。其余 3 次均为 coder DNS `getaddrinfo failed`，没有 TLS EOF 或 Verifier failure；全部 Budget/Workspace 收敛。随后手工 Chat 暴露角色累计 Token Budget 与每请求输出上限被混用，现已按 ADR-0034 根因拆开；第五轮因此只证明旧 Profile。

当前 manifest 的第六轮随后在显式无代理的进程环境和新仓库外目录完成：`18/18 measured`、`complete=true`、严格成功 `15/18`；requested single `5/6`、multi `4/6`、auto `6/6`，auto 六次严格解析且全部归入 single。三个失败仍全部是 durable DNS `getaddrinfo failed`（single coder、multi parent、multi coder），没有 TLS EOF、Budget exhaustion、Router 或 Verifier failure；其余 15/15 完成 Review 与 Promotion。52 个 Budget account 全部 terminal，52 个 Workspace 为 51 released + 1 quarantined、`live=0`。JSON/Markdown 逐项一致且无 Key/本机路径。

DNS-only 诊断随后定位到 WLAN 的首选 DHCP DNS：该服务器直查 UDP `0/50`、TCP `0/10`，而备用 DNS UDP `50/50`、TCP `10/10`。将 WLAN DNS 修正为 `223.5.5.5`/`223.6.6.6` 后，Windows system resolver `200/200`，与 Provider 同 Python `urllib`/OpenSSL 且无代理的无 Key 探针为 `50/50`。全新第七轮仍不重试、不 fallback、不覆盖历史报告，得到 `18/18 measured`、`complete=true`、严格成功 `16/18`：requested single `5/6`、multi `5/6`、auto `6/6`，DNS 和 TLS EOF 都为 0。剩余失败分别是一次远端主动断开，以及一次 multi coder 在 durable 使用 126312 tokens 后由累计 Budget fail closed；54 个 Budget account 与 54 个 Workspace 全部收敛，dirty failure 以 2 个 quarantine 留证、`live=0`。当前仍没有生产 retry/fallback/代理特例/SSL 放宽；独立复审、唯一一次最终全量、F5 安全扫描、真实网格、最终提交后的干净打包/归档审计和离线安装均已通过，`v0.7.0` 已正式发布。

本轮发版稳定化定向门禁为 Product `257 passed`、Evaluation `52 passed`、Budget/Workspace/Artifact/Promotion/Workflow `397 passed, 3 skipped`、CLI `521 passed, 1 skipped`，collect-only `2407`。独立审查先发现 frozen-command/Review P1，修复后复审又找到已有 Promotion 的恢复早退；两处都已按 Promotion owner 修复。反向验证分别复现旧 ref 移动、错误 Benchmark 成功与错误 Product `completed`。最终独立复审为 P0/P1/P2 全零，随后唯一一次最终全量得到 `2402 passed, 5 skipped`、退出码 0。

F4 已完成独立复审和唯一一次最终门禁：全仓 2395 收集 / 2390 通过 / 5 跳过，退出码 0，耗时 28:04；F4 定向测试 51 项、Product/架构相邻回归 304 项、Budget/Workspace/Artifact/Promotion/Workflow 相邻回归 325 项通过且 2 项跳过、CLI 回归 519 项通过且 1 项跳过。两轮复审发现的 5 个 P1 和 2 个 P2 均已用确定性公开路径反例修复，其中 6 项完成逐项反向验证；compileall、修改范围 Ruff、文档 QA、`git diff --check` 均通过，四个受保护核心文件零 diff。五个既有 skip 是 Windows 上四处目录 symlink 权限边界和一处路径不能包含 NUL。F3 历史检查点为 2344/2339/5，F2 为 2326/2321/5，F1 为 2253/2248/5，v0.6.0 发布基线仍是 1707/1706/1。仓库外干净 HEAD 克隆已跑通公开 L2 的 13/13 门禁与完整核心回归，并让公开 L3 命令在 Python Quality v1 固定任务中得到 baseline 2/3、candidate 3/3、`improved`、0 regressions、0 不变量/请求重建违规；同一条真实链路随后完成 L4 非变更 review、精确摘要 apply、目标 `plugins list/doctor` 与显式 rollback。v0.6 RC 又使用真实 OpenAI-compatible 模型完成 parent → spawn → send → wait → collect → stop；同一 child Session 随后显式恢复并完成第二个真实 Turn，独立取消路径收敛为 durable `cancelled`。两份 Session 都没有开放 Turn/Step，不变量和请求重建违规均为 0。详见 [v0.6.0 验证记录](docs/validation-v0.6.0.md)。独立 Python Quality 插件另有 17 项契约测试；独立 Plugin Creator Skill 另有 10 项。

其中 74 项来自第三方复审确认的 5 个阻断项的两轮修复：Owned Task 的异常所有权（不再出现 `Task exception was never retrieved`，取回后**不保留**异常对象）、`AgentRuntime.dispose()` 的单任务收敛（取消不再让插件永远卸载不掉）、Session 插件身份按 PEP 440 **对象**比较（`1.0` 与 `1.0.0` 等价，`1.0` 与 `1.0.1` 仍拒绝；键**缺席**是 v0.3 会话，显式 `null` 是损坏数据）、保留 metadata 键 `traceh_plugins` 按**出现**拒绝、以及 `traceh run` 的 `create_session` 纳入 `try/finally`（其测试真正不读取开发者 `.env`）。

其中带 `slow` 标记的 18 项是真实打包验收：它先为 TraceHarness、示例 Skill、Python Quality、Plugin Creator Skill 建立只含声明源码的隔离构建输入，再构建四个 Wheel，并审计成品不得夹带 `.pyc`、旧构建目录或 `.egg-info`；随后把 `packaging` 一并放进离线 wheelhouse，用 `--no-index` 装进全新虚拟环境，再通过真实 Entry Point 验证发现、诊断、Python Quality 的 Tool/Policy/Verifier，以及 Plugin Creator 的 Prompt/只读指南/事件与请求重建主线。跳过它用：

```powershell
python -m pytest -o addopts='' -q -m "not slow"
```

## 设计文档

- [当前项目上下文（正式版）](docs/note/project-context.md)
- [当前项目上下文（通俗版）](docs/note/project-context-plain-zh.md)
- [架构说明](docs/architecture.md)
- [事件协议](docs/event-protocol.md)
- [Session Event Feed](docs/event-feed.md)
- [恢复语义](docs/recovery-semantics.md)
- [插件说明（作者与运维契约）](docs/plugins.md)
- [插件与多 Agent 演进](docs/plugin-evolution.md)
- [测试策略](docs/testing.md)
- [v0.6.0 验证记录](docs/validation-v0.6.0.md)
- [v0.7.0 发布验证记录](docs/validation-v0.7.0.md)
- [v0.7.1 发布验证记录](docs/validation-v0.7.1.md)
- [v0.8.0 发布候选验证记录](docs/validation-v0.8.0.md)
- [v0.8 冻结阶段计划](docs/plan/TRACEHARNESS_V0.8_STAGE_PLAN.md)
- [v0.9 冻结阶段计划](docs/plan/TRACEHARNESS_V0.9_STAGE_PLAN.md)
- [v1.0 总路线：记忆、隔离、互操作与受控并发](docs/plan/TRACEHARNESS_V1.0_MASTER_PLAN.md)
- [ADR](docs/adr/)，其中 [ADR-0007](docs/adr/0007-transactional-plugin-activation.md) 记录 v0.4 插件激活，[ADR-0013](docs/adr/0013-scoped-tool-prompt-policy-overlays.md) 记录 D2 四层 Composition Overlay 的设计原因

## 项目来源说明

本项目是一个独立的 Python 实现，参考了 DeepSeek Harness 中 Append-only Session、可重建请求、能力扩展边界、分层生命周期和可执行不变量等思想。它没有复制 DeepSeek Harness 的实现，不承诺与其 API 兼容，也不是 DeepSeek 官方项目。

具体对照见[插件说明第 10 节](docs/plugins.md#10-relationship-to-deepseek-harness)：该节基于官方仓库 [`deepseek-ai/deepseek-harness`](https://github.com/deepseek-ai/deepseek-harness) 固定 Commit `99f6f02fecdb7dff40c3fbc9470f5907c29f74ca` 的 `docs/architecture.md` 写成。简要结论是：借鉴了 shared context、reversible effects 和 composition 三个思想；**没有**引入 Cordis、TypeScript/Node，也**没有**采用"AgentLoop 本身也是插件、不存在特权内核"的方向——TraceHarness 刻意保留稳定内核与事件事实边界（见 [ADR-003](docs/adr/003-kernel-is-not-a-plugin.md)）。

## License

MIT。
