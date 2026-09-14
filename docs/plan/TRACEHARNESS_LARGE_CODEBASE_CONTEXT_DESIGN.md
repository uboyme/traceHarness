# 大型代码库理解任务的上下文设计（证据与选项）

状态：**设计提案，尚未决策，未实现任何一项**。2026-09-14。

本文回答一个具体问题：**要让主 Agent 在一个单次上下文装不下的代码库上工作，当前架构缺什么。**
它只陈述已核对的现状与实测数据，给出候选方案及各自需要的 ADR 边界，不预设结论，也不包含任何代码改动。

---

## 1. 问题与可测指标

每次模型调用都要重发完整对话，读过的工具结果全部留在其中，因此"读得越多、下一次越贵"。
在一个 6.8 万行的语料上，主 Agent 的上下文单调增长直到 Provider 失败。

可测的成功标准（基线现成，见第 2 节）：

> 主 Agent 能就 6.8 万行语料给出可验证的回答，而**其自身上下文全程不超过约 4 万输入 token**。

当前基线：**89,856 输入 token 后失败**。

---

## 2. 实测证据

三轮真实模型配对对照（single / multi 同题同上限，仅执行策略不同），驱动
`tests/live_dynamic_collaboration/comprehension_comparison.py`，语料 digest `0c4c730d…` 三轮字节相同。

语料：本仓库引擎包 Python 加最早 30 份 ADR，**253 文件 / 2.67 MB / 68,125 行**，
刻意不含任何全项目摘要文档（否则单体读一个文件即可作答，对照失去意义）。
按 `read_file` 的分页上限，完整读完需要 **693 页**。

### 2.1 三轮结果

| 轮次 | 变更 | single | multi | 结论 |
|---|---|---|---|---|
| 1 | 交付物上限 60,000 字符 | 失败 `provider-tool-arguments-json-string-unclosed` | 同左 | 作废 |
| 2 | 上限改 12,000 | 失败（同上，71.9 秒） | 失败 `provider-timeout` 120.28 秒 | 作废 |
| 3 | 上限改 24,000、重试预算 600 秒 | 失败（同上） | 失败 `provider-timeout` 120.28 秒 | 作废 |

三轮的失败**均来自夹具参数与宿主/Provider 边界的相互作用**，不是被测机制本身：
交付物字数上限对 `max_output_tokens=8192` 的单次回复过大、Provider 固定 120 秒 HTTP 超时
（`llm/openai_compatible.py:265`，运行计划无此旋钮，`cli/main.py:538` 构造时不传）。
三轮累计约 754 万 token，对"多助手是否划算"**未取得任何有效读数**。

### 2.2 上下文增长曲线（第 3 轮 single，逐次请求的 exact input tokens）

```
#3  3,343   #10 33,700   #15 61,347   #19 75,403   #27 89,856   #28 失败
```

**全程单调上升，从未下降。** 同期生成速率稳定在约 110 token/秒（#22：5,826 输出 / 53.8 秒；
#26：3,279 / 28.3 秒），即**没有观察到随上下文增大而劣化**；失败是输出截断与请求超时，不是"上下文墙"。

### 2.3 质量侧的正面数据

第 2 轮 single 臂成功写出交付物（单次 `apply_patch`、16,565 字符参数、5,826 输出 token）。
将该文档离线送入固定检查（放开长度一条）：

```
DOC CHARS 16202
ok 63 16 83
```

即 **63 个真实引用文件、18 个包中的 16 个、83 条锚点引用全部核验通过、零条不可解析引用**。
结论：**在此语料上，单个 Agent 的答案实质质量是够的**；瓶颈在上下文与输出边界，不在能力。

### 2.4 委派侧的观察

multi 臂三轮共 **276 次 `read_file`、0 次 `submit_collaboration_plan`**，
计划工具全程在其工具清单中（失败时快照可证）。

**证据边界**：三轮 multi 均**死在提交计划之前**，因此**不能**断言"模型拒绝协作"，
只能陈述"在开放式理解任务上它持续自行阅读，未走到派发步骤"。

---

## 3. 现状核查（逐条对应源码）

### 3.1 读取粒度

`src/traceh/tools/builtins/file_page.py:10-11`：

```
MAX_FILE_PAGE_LINES = 120
MAX_FILE_PAGE_CHARS = 8000
```

每次 `read_file` 最多 120 行 / 8000 字符。这是有意的有界证据设计（每条结果都是 append-only
事件日志中的证据），代价是覆盖同样代码量所需的调用次数与上下文占用同比放大。

### 3.2 工具输出保留：机制已存在，Product 未装配

`src/traceh/session/tool_output.py:15-16` 定义 `read_tool_output` / `search_tool_output`，
`src/traceh/tools/output.py` 实现三个工具（`read_tool_output`、`list_tool_outputs`、
`search_tool_output`）。其语义是：被保留的工具输出在上下文中**只留一个短引用**，
原文留在 Effect 日志里按需取回。

- 检索 episode 宿主**已授予**这三个工具：`evaluation/evaluators/episode_setup.py:28`
  `{"list_tool_outputs", "search_tool_output", "read_tool_output"}`
- Product 路径**一处引用都没有**：`product/runtime.py:82-83` 的工具集为
  `READ_TOOL_IDS = ("list_files", "read_file", "search_text")`、
  `WRITE_TOOL_IDS = (*READ_TOOL_IDS, "apply_patch", "shell")`

**这是本文最重要的一条：卸载上下文所需的机制本仓库已经造好，只是没接到执行任务的那条路上。**

### 3.3 Surface 压缩：接了 chat，未接 Product，且粒度不匹配

- 装配点只有一处：`cli/main.py:616` `compaction=getattr(args, "compaction", None)`，
  由 `--auto-compact*` 旗标或 `TRACEH_AUTO_COMPACT_*` 环境变量驱动，**默认 `None` 即关闭**。
- TUI 是同一 `traceh chat` 命令的 adapter，因此**继承**该装配；TUI 自身无装配代码，
  `tui/context_inspection.py` 等处只是显示压缩状态。
- **TUI 配置表单不暴露任何压缩字段**：`tui/config_forms.py` 中与"压缩"相关的两处
  （`history=压缩历史原文读取`、`history_tier` 的"压缩摘要"）属于上下文检索的披露档位，
  与自动压缩策略无关。因此**仅使用 TUI 的用户没有任何界面可以开启它**。
- Product 路径完全未接：`product/runtime.py` 自建 `RuntimeConfig` 且不传该参数。

**粒度问题（比装配更关键）**：`session/compaction.py:11-12` 明确
"**Only a closed prefix is compactable.** A cut boundary is always the sequence of a `turn/end`
that really closed an open Turn"。即压缩只能替换**已闭合 Turn 的前缀**。
而编码/理解任务的上下文是在**同一个 Turn 内部、跨数十个 Step** 累积的。

> 因此：**即便给 Product 接上现有压缩，也解决不了本文的问题。** 两件事必须分开评估——
> "接不接"是装配问题，"接了管不管用"是粒度问题。

**未查到**任何 ADR 或正式上下文记载"Product 故意不接压缩"的理由（ADR-0042 未涉及该边界）。
因此无法判定这是有意取舍还是遗漏。

### 3.4 委派：当前唯一已接的卸载手段

助手拥有独立 Session 与独立上下文，只交回有界报告，因此**派发是当前架构中唯一能卸掉
Step 粒度上下文压力的机制**。它已完整实现并经真实模型验证（ADR-0077/0078）。

其前提是主方**足够早地**派发。计划阶段的宿主提示已包含相应要求
（`product/collaboration.py` 的 PLAN 视图："Choose how much investigation is needed within the
existing total budget… **Do not do the child's whole assignment just to ask it to repeat your
work.** Implementation starts after the bound child handoff completes."），
但计划前的自我调查**没有任何量化约束**，仅受总预算限制。

---

## 4. 候选方案

按"影响 ÷ 代价"排序。**均未实现**；前两项涉及工具语义或请求投影，需要独立 ADR。

### 方案 A：旧工具结果逐步转为引用（建议优先评估）

把 3.2 的保留机制接入 Product，并新增规则：**超过一定步数的工具结果，在请求投影中替换为引用**，
原文经既有 `read_tool_output` / `search_tool_output` 按需取回。

- **为什么干净**：事件日志一字不改，仍是唯一 append-only 事实源；改变的只是**发给模型的投影**。
  不引入第二事实源、不新增平行状态机——复用既有 owner。
- **预期效果**：上下文在某个水位稳住，而不是单调增长至崩溃。
- **必须由 ADR 决定的边界**：
  1. 替换的触发规则（按步数、按字节、还是按请求压力）；
  2. 模型在第 N 步"还能直接看见第几步的原文"——这是一条新的可见性语义；
  3. 被替换内容与验证/审批证据链的关系（引用必须仍可解析到同一不可变原文）；
  4. 与既有"超限才保留"规则如何合并为**一条**规则，而不是两套并行逻辑。

### 方案 B：`read_file` 增加结构模式 + 调整页大小（代价最小）

- **结构模式**：返回文件内的类/函数名与行号，而非正文。先摸骨架、再定点读细节，
  可把"摸清 253 个文件"从 693 页正文降到数十次极小输出。
- **页大小**：当前 120 行偏小。提高到 300–400 行可使同等覆盖的调用次数降约 3 倍。
- **ADR 边界**：结构模式是**新的工具输出形态**，其确定性、有界性与证据语义需要明确；
  页大小属于有界证据参数，调整需说明对单条事件体积的影响。

### 方案 C：向主方暴露自身上下文水位

既有上下文压力计算在 `chat/context_pressure.py`（TUI 状态条已在显示）。
可将同一信号接入 Product 的 Step 视图，使主方**看得见**自己的水位。

- **风险（必须写进 ADR）**：这条做重了会从"让它知情"滑向"逼它派发"，
  而"模型是否自主选择协作"正是当前尚未回答的问题。任何强制派发的实现都会**永久毁掉该观测**。

---

## 5. 明确不建议的做法

- **只加预算上限**。三轮实测（约 754 万 token）已证明无效：瓶颈是上下文投影与单次输出边界，
  不是额度。
- **给 Product 接现有压缩以解决本问题**。粒度不匹配，见 3.3。
  （是否为了别的理由接上，是另一个独立问题。）
- **把"必须派发"写成流程硬约束**。见 4.C 风险。
- **继续在同一实验形态上拧参数重跑**。三轮各撞不同边界，已满足停止条件。

---

## 6. 待决问题

1. Product 路径不接压缩，是有意取舍还是遗漏？（无记录可查）
2. TUI 配置表单不暴露压缩开关，是否应补？（与方案 A 是两件事）
3. 方案 A 的可见性规则由谁拥有——请求构建器、压缩服务，还是一个新的投影规则？
4. **已查明（原列为未解释观察）**：第 3 轮 multi 臂以 `provider-timeout` 失败后未产生第二次尝试，并非重试被拒绝，而是**重试已发起、在预算准入处失败**。完整链条，每环均有源码与事件佐证：

   1. `product/runtime.py` 构造 `RuntimeConfig` 时不传 `token_budget`，因此没有 token 计数器（对比 chat 路径 `cli/main.py:617`）；
   2. `budgets/enforcement.py:781-783`：无计数器时，单次调用的预留额 = **账户全部剩余**；
   3. `llm/openai_compatible.py:78` 构造的超时 `ProviderFailure` **不带 usage**（对比同文件 72-76 行的 DNS 失败带 `Usage(0, 0, EXACT)`）；
   4. `budgets/enforcement.py:599-600`：失败且无 usage → 按**预留全额**计费，quality=UNKNOWN；
   5. `budgets/enforcement.py:719-720`：剩余为 0 → 重试的准入抛 `BudgetExhaustedError("max_tokens")`；
   6. `runtime/agent_loop.py:285`：该 `admit` 调用位于 Attempt 的 try 之外，异常直接逃出并使 Turn 失败，因此 ordinal 2 的 `model/attempt-start` 永不写入（该事件在第 307 行、`admit` 成功之后）。

   实测事件：`budget/usage-reserved tokens=4653578` → `budget/usage-settled tokens=4653578 quality=unknown` → `runtime/error BudgetExhaustedError`（step_id 与超时尝试相同）。

   **影响**：一次 Provider 超时会把整个剩余 token 预算计给一个零返回的调用，并使宿主自己声明的重试策略（TIMEOUT 可重试、最多 3 次）在数学上无法兑现。两条规则单独看都是安全方向（数不出就预留全部＝不超支；未知用量就全额计费＝不少计），**问题出在二者叠加**。

   **副作用**：本文第 2 节引用的 `known_subtotal_tokens` 不含这类 UNKNOWN 计费，因此**低估**了真实扣减。

   **候选修法**（需独立 ADR，动到预算账本计费语义）：(a) 为 Product 配置 token 计数器，使预留变为单次调用估算——最小改动且走既有受支持路径；(b) 无计数器时为预留额设单次上限；(c) 为未收到任何字节的超时附带零 usage——最不安全，120 秒超时很可能已真实消耗 token。

   本条与 3.2、3.3 构成同一模式：**能力已存在并接入 chat/episode 路径，Product 路径未接**。

---

## 7. 证据局限

- 三轮均为**每臂一个复本**，属观察而非统计量。
- 仅一个模型、一个端点（deepseek-v4-flash）。失败模式中至少两项
  （8192 输出上限、120 秒 HTTP 超时）与该端点配置强相关。
- 第 3 轮 single 臂与第 2 轮行为不一致（第 2 轮写出文档，第 3 轮未写），
  说明**同题同配置下行为本身不稳定**，单复本结论需谨慎。
- 2.3 的质量数据来自**一份**交付物，说明"可以做到"，不说明"稳定做到"。
- 本文未运行全量测试、L2–L4 或任何联网门禁；除上述真实轮次外未调用真实模型。
