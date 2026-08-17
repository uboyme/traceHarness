# TraceHarness Py v0.3

TraceHarness Py 是一个基于事件溯源、可以重建运行过程的 Python Runtime，用来构建可追踪的 Coding Agent。v0.3 的基础实现刻意保持在可以理解的规模，同时通过稳定边界为未来的插件、分层 Composition、子 Agent 和 Workflow 留出扩展空间。

> 当前状态：Educational alpha。项目已经能够运行并经过测试，但公共 API 尚未承诺可稳定用于第三方生产环境。

## 已经包含什么

- Append-only JSONL Session Stream 与 Effect Stream；
- Session / Turn / Step / Model Attempt / Tool Invocation 生命周期事件；
- 薄异步 Agent Loop，把 Prompt、模型、工具和 Continuation 行为委托给独立服务；
- 冻结的 Composition Snapshot，以及可以独立重建的模型请求；
- 确定性的 Scripted Provider 和 OpenAI-Compatible Provider；
- 统一 Tool Runtime，支持 Schema 校验、单调 Policy、超时、读并发、写入/进程排他 Barrier 和结构化结果；
- 五个 Coding Tools：`list_files`、`read_file`、`search_text`、`apply_patch`、`shell`；
- Workspace 路径边界和子进程环境变量清洗；
- Event/EventStore 所有权契约：调用方修改 `append()` 或 `read()` 返回事件的嵌套内容，不会反向改写已经保存的历史；
- 进程内 Session Event Feed：`EventStore` 接受事件之后（按调用方请求的 `Durability`）按真实 `seq` 顺序发布给订阅者，每个订阅者拿到独立副本；消费者接口只能订阅、不能发布；它不新增任何持久化事实，也不提升崩溃持久性，不是事实源；
- `traceh chat` 的实时 Tool Timeline：Turn 运行期间即时显示 Step、模型调用、工具生命周期和验证结果，可用 `--no-timeline` 关闭；
- Activity Heartbeat：模型或工具长时间未结束时，按 `--heartbeat-seconds`（默认 10 秒）打印等待时长，完成行附带实测耗时；
- 可收敛的 Ctrl+C：有任务在跑时首次 Ctrl+C 只取消当前 Turn 并保留 Session，取消生命周期会完整显示在 Timeline 上；
- 启动即打印的恢复命令：Banner 直接给出含解析后 `--data-dir` 的可复制命令，硬中断也能从屏幕历史找回 Session；
- Effect Intent / Dispatch / Outcome 记录，用于判断崩溃时间窗中的副作用；
- Append-only 崩溃恢复：闭合孤立的 Model Attempt、Tool Call、Step 和 Turn，不盲目重放结果不明的副作用；
- 通过可选外部命令 Verifier 实现 Evidence-Driven Completion；
- Request 重建检查、协议不变量、Replay、手动 Surface 压缩和静态 HTML Inspector；
- 确定性的 Benchmark Runner 和无需 API Key 的 Demo；
- 为未来可逆插件激活、类型化 Hook、层级 Scope 和 Owned Task 收敛准备的 Kernel 原语。

## 不安装直接运行

需要 Python 3.12 或更高版本。

```bash
cd traceharness-py-v0.3
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

**恢复它当时的行为**是另一件事，需要 Provider、Model 等配置——见下面的[Ctrl+C 与找回 Session](#ctrlc-与找回-session)。Chat 启动时就会把完整命令打印出来，直接复制即可。

继续会话前会先执行崩溃恢复。只有确实修复了未闭合状态时才会打印一行简短的 `recovered: ...`；在你主动输入前不会创建 Turn，也不会替你注入隐藏指令。

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
  traceh chat --session-id <id> --data-dir <绝对路径> --provider <p> --model <m> [--max-steps N] [--script <绝对路径>] [--base-url <url>] [--api-key-env NAME] [--env-file <绝对路径>]
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

`seq` 1-3 是 `session/created`、`inbox/accepted`、`inbox/claimed`：它们**确实已持久化**，只是 Timeline 不显示，所以第一条可见事件通常是 `turn/start`。TraceHarness 刻意不重新编号——真实 `seq` 才能在 `traceh inspect` 和 JSONL 中查回。因此启动时会打印一次说明：

```text
Timeline shows selected persisted events.
Numbers shown as [event N] are Event Log seq values; they may start above 1 or skip where internal events are hidden.
```

Timeline 只显示生命周期与结果：Turn/Step 起止、模型调用与答复、工具的 requested/started/succeeded/failed、验证结果、运行时错误、取消请求和恢复。它**默认不显示** Prompt、请求快照、Composition 快照、模型原文、文件内容、完整 Patch、完整命令输出，遇到未知事件类型也不会打印原始 payload。

输出侧按不可信内容处理：所有来自事件 payload 的字符串都会先清洗（去除 ESC 与其他控制/格式字符、折叠为严格一行、统一限长），因此模型返回的工具名或任意异常类型无法伪造额外的 Timeline 行，也无法发出终端控制序列。`shell` 执行的命令**默认完全不显示**（只显示工具名与调用 ID），因为命令行最容易带上凭据，而关键词扫描无法覆盖所有秘密形态；`runtime/error` 只显示错误类型，不显示消息与 traceback。可显示的读取类工具路径仍会做凭据形态检查，命中即整段不显示；未知工具只显示工具名与调用 ID。

Timeline 是纯界面投影：它不进入模型可见历史，不改变 Request Fingerprint，也不写入任何事件。它只在当前进程内可见——另一个进程写同一份 JSONL 时不会实时出现在这里。

当前 Chat 仍是行式提示符，不是流式 TUI：没有 Token Streaming、执行前审批，也不能在 Turn 运行期间继续输入。

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

源码在运行时只依赖 Python 标准库。项目使用 `setuptools` 作为构建后端，正常的 `pip` Build Isolation 会自动安装它。

## 运行内置 Benchmark

```bash
PYTHONPATH=src python -m traceh.cli.main eval benchmarks/basic \
  --output /tmp/traceh-eval
```

每个案例都会生成 `report.json`、`report.md`、复制后的 Workspace 和持久化 Trace。只有外部 Verifier 通过且协议不变量保持干净，案例才算成功。

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

使用 `--env-file path/to/file.env` 可以选择其他配置文件。支持的 Runtime 配置包括 `TRACEH_PROVIDER`、`TRACEH_BASE_URL`、`TRACEH_MODEL`、`TRACEH_API_KEY_ENV`、`TRACEH_DATA_DIR`、`TRACEH_MAX_STEPS` 和 `TRACEH_VERIFY_COMMAND`。`openai-compatible` Provider 必须显式配置 Base URL 和 Model；TraceHarness 不会静默选择某个厂商地址或模型。

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

v0.3 Adapter 使用 `/chat/completions` 和非流式 HTTP。Event Protocol 已经把 Model Attempt 分成独立边界，因此未来可以增加流式、重试和 Provider Fallback，而不改变 Step 语义。

取消 Turn 无法中止已经发出的 `urllib` 请求。Provider 不会把 Worker 丢在后台，而是先等待请求收敛，再重新抛出取消，因此 CLI 宣布 Turn 结束后不会还有后台 Worker 继续与 Endpoint 通信。最坏情况下需要等到 Provider Timeout 到期；这是等待收敛，不是立即中止。

## 在 Python 代码中使用

```python
from pathlib import Path

from traceh.llm.scripted import ScriptedLlmProvider
from traceh.api.llm import ModelResponse
from traceh.runtime.agent_runtime import RuntimeConfig, build_default_runtime

provider = ScriptedLlmProvider((ModelResponse(content="done"),))
runtime = build_default_runtime(
    RuntimeConfig(
        data_dir=Path(".traceh"),
        provider="scripted",
        model="demo",
    ),
    provider=provider,
)

result = await runtime.run(Path("./workspace"), "Inspect the project")
await runtime.dispose()
```

`build_default_runtime()` 还可以接收自定义 `EventStore`、`ContinuationRuntime`、额外的 `Tool`、Policy、Prompt Assembler 和 Verifier。需要更深度的定制时，可以向 Agent Loop 提供其他 `CompositionRuntime`，让一个 Step Lease 一套自洽的 Provider、Tool Set 和 Snapshot Generation。

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

### 已经存在的扩展边界

| 未来能力 | 当前已有边界 |
|---|---|
| 新 Model Provider | `LlmProvider` + `LlmRegistry` |
| 新 Tool | `Tool` + `ToolRegistry` |
| Tool 授权 | `ToolPolicy` |
| Prompt 扩展 | `PromptSection` + `PromptAssembler` |
| 自定义完成行为 | `ContinuationRuntime` |
| 自定义验证 | `CompletionVerifier` |
| 新持久化后端 | `EventStore` |
| 可观测性插件 | 类型化 NOTIFY Hooks；进程内 `SessionEventFeed` 订阅 |
| 可逆插件装配 | `Activation`、`Lifespan`、`OwnedTaskSet` |
| Step 安全的插件/模型/工具 Generation | `CompositionRuntime.lease()` |
| Agent 专属能力 | 层级 `Scope` |
| 子 Agent | 未来构建在 `AgentLoop` 之上的 `AgentSupervisor` |
| 多 Agent Workflow | 未来调用 `AgentSupervisor` 的 Workflow 层 |

延伸阅读：[架构说明](docs/architecture.md)和[插件演进说明](docs/plugin-evolution.md)。

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
src/traceh/api          稳定协议、冻结 DTO 和扩展边界
src/traceh/kernel       Scope、Activation、Hooks、可逆生命周期
src/traceh/session      EventStore、Projector、Recovery、不变量
src/traceh/runtime      AgentLoop、Composition Lease、Request、Continuation、Verification
src/traceh/llm          Provider Registry 和 Adapter
src/traceh/tools        Policy、调度、Effect 和内置 Coding Tools
src/traceh/inspector    文本 Replay 和静态 HTML Trace
src/traceh/evaluation   确定性 Benchmark Runner
tests                   契约、恢复、取消和端到端测试
```

## CLI 命令

```text
traceh run
traceh chat            # 支持 --no-timeline / --heartbeat-seconds
traceh resume
traceh recover
traceh inspect
traceh replay
traceh compact
traceh sessions
traceh eval
traceh doctor
```

使用 `traceh <command> --help` 查看详细参数。

## v0.3 已知边界

- Plugin Entry Point 自动发现和热替换有意推迟到 v0.4+；Kernel 中已有 Activation/Scope 原语，但尚无第三方 PluginManager；
- 每个进程只有一个活跃 Agent Runtime，尚无 `AgentSupervisor`；
- `traceh chat` 是行式交互：已有实时 Tool Timeline、Activity Heartbeat 和可收敛的 Ctrl+C，但没有 Token Streaming、Spinner、颜色、执行前审批，也不能在 Turn 运行期间输入；`traceh run`/`resume` 尚未接入 Timeline；
- Activity Heartbeat 只是屏幕状态：不写 Event Log、不可事后回查，完成耗时也不进入 payload；需要可审计的时延应在 Provider/Tool 边界落盘；
- 硬中断（`Ctrl+Break`、关闭控制台）没有任何收敛：不打印提示、不闭合生命周期，只能依赖启动时已打印的恢复命令与崩溃恢复；
- Session Event Feed 只在同一进程内可见，没有跨进程实时观察；它的队列无上限，因此不对 Runtime 施加背压，但被遗弃的订阅者会占用内存，且尚无 Overflow 策略；它可丢失、不重放历史、不提升崩溃持久性（`SYNC`/`BATCHED` 语义仍由 `EventStore` 决定），不能当作恢复证据；
- Timeline 已对注入做了清洗，但形似结构标记的惰性文本仍可能出现在该行内部：保证是"不会产生第二行、行首为真实事件号"，不是"不会出现形似标记的字符"；
- JSONL 提供单机写入互斥与乐观并发控制，但不是分布式数据库；
- OpenAI-Compatible Adapter 非流式，且没有 Retry/Fallback Middleware；
- `apply_patch` 执行精确文本替换，不解析 Unified Diff；
- 默认 Shell Policy 是 Guardrail，不是安全沙箱；运行不可信 Agent 时应使用容器或远程 Sandbox；
- Effect Reconciliation 当前是通用实现；Git、远程 API 和事务系统的领域 Reconciler 应由后续插件提供。

## 开发与验证

```bash
PYTHONPATH=src pytest
PYTHONPATH=src python -m compileall -q src
```

当前共有 137 项自动化测试，覆盖 JSONL 恢复、expected-seq 冲突、跨进程锁、EventStore 所有权、Scope、可逆 Activation、Hook 语义、Surface Replacement、Workspace 边界、精确 Patch、Request 重建、端到端 Coding、取消收敛、崩溃恢复和 Benchmark 报告等能力。

## 设计文档

- [当前项目上下文（正式版）](docs/note/project-context.md)
- [当前项目上下文（通俗版）](docs/note/project-context-plain-zh.md)
- [架构说明](docs/architecture.md)
- [事件协议](docs/event-protocol.md)
- [Session Event Feed](docs/event-feed.md)
- [恢复语义](docs/recovery-semantics.md)
- [插件与多 Agent 演进](docs/plugin-evolution.md)
- [测试策略](docs/testing.md)
- [ADR](docs/adr/)

## 项目来源说明

本项目是一个独立的 Python 实现，参考了 DeepSeek Harness 中 Append-only Session、可重建请求、能力扩展边界、分层生命周期和可执行不变量等思想。它没有复制 DeepSeek Harness 的实现，不承诺与其 API 兼容，也不是 DeepSeek 官方项目。

## License

MIT。
