# TraceHarness Py v0.8.0 发布候选验证记录

验证开始日期：2026-08-30

状态：**F5 实施中；真实体验后的 Product Chat、Plugin cleanup、TUI identity/layout/focus、Provider
multiline 与失败证据归属均已按根因修复。一次真实 TUI ProductTask 已从 Proposal 走到 Promotion；
Provider、TUI、失败证据与跨 owner 复审曾清零 P0/P1/P2。随后同一 TUI 又补齐 fresh 角色对话、fresh
完整身份和 Review evidence，可见性代码与测试发生了实质变化。因此此前
`2555 passed, 7 skipped`、退出码 0 只保留为本轮之前的历史证据；当前尚未运行新的最终全量，clean-input
资产、离线安装、真实 Provider 网格、tag 与发布也尚未完成。**

本记录描述从已发布 `v0.7.1` 到当前 `0.8.0` 候选的发布整合。F0–F4 的阶段事实、反例和停止点仍见
[`TRACEHARNESS_V0.8_STAGE_PLAN.md`](plan/TRACEHARNESS_V0.8_STAGE_PLAN.md)；本文只记录 F5 实际运行的
全局审查、最终全量、资产、离线安装和另行授权的真实 Provider 证据。未运行项必须继续写成未运行，
不能用 F4 或 v0.7.1 的历史数字替代。

## 1. 候选范围

`0.8.0` 候选整合以下已经实现的唯一产品主线：

- 两阶段 Model admission、Session dispatch permit 与 composed/dispatch 双请求证据；
- 唯一生产 SQLite EventStore，旧 JSONL 明确拒绝且零迁移/零 fallback；
- typed Provider failure 与同 Provider、同模型、同冻结请求的 bounded retry；
- Line/Textual 共用的 UI-neutral Chat Driver、唯一 ephemeral activity projection、Product control 与纯读
  durable observation；当前 Textual presentation 明确区分 transient operation、durable facts 与模型自述，
  只显示合法 typed-confirmation 闸门并展示事实/等待年龄；`Ctrl+T`/`Ctrl+P` 通过现有 observation/Session
  readers fresh 重建角色对话和完整身份，没有第二事实源；
- 既有人工 `START`、Approval、bare target Promotion 与唯一 `traceh eval` ProductTask Benchmark。

F5 没有增加 durable schema、Runtime、Workflow、ProductTask 状态、Event bus、Benchmark Runner、
Provider/model fallback、Skill、Memory、RAG、OS sandbox 或真实远端 Promotion。

## 2. 版本与独立插件元数据

唯一版本源 [`src/traceh/version.py`](../src/traceh/version.py) 已切到 `0.8.0`；`pyproject.toml` 仍只动态
读取该属性。具名版本合同同时钉住 Package、Distribution、核心插件身份、插件 API 与 CLI 派生关系。

两个独立示例插件在 v0.7.1 时明确排斥 0.8，因此随各自元数据升补丁版本，而不是放宽核心验证器：

| Distribution | 候选版本 | Distribution / Manifest 范围 |
|---|---:|---|
| `traceh-plugin-creator-skill-plugin` | `0.2.2` | `>=0.6,<0.9` |
| `traceh-python-quality-plugin` | `0.2.2` | `>=0.5,<0.9` |

Plugin Creator 面向新候选的作者模板使用 `traceharness-py>=0.8,<0.9`。这些是显式版本合同，不是生产
默认值或对 v0.9 的兼容承诺。

反向 PEP 440 探针证明旧 `>=0.6,<0.8` 与 `>=0.5,<0.8` 都确定性排斥 `0.8.0`，当前两个 Manifest
范围则都接受 `0.8.0`。第一次临时探针误从 `traceh.api.plugins` 导入不存在的异常名并以 `ImportError`
结束，没有被计入验证；改用仓库真实依赖的 `packaging.SpecifierSet` 后才得到上述可复核结果。

当前已运行的窄前置检查：

| 检查 | 结果 |
|---|---:|
| 核心版本与 Product 架构具名钉住 | 12 passed |
| Plugin Creator 自身 | 10 passed |
| Python Quality 自身 | 18 passed |

开发解释器原有 editable Distribution metadata 为 `0.7.1`；使用本地源码重新登记后，导入模块与
Distribution metadata 均为 `0.8.0`。该动作只用于当前开发解释器，不替代后续 clean-input Wheel 和
`--no-index` 安装证据。

## 3. F5 独立审查前门禁

以下门禁已经完成，均为离线确定性测试或真实本地 Git/SQLite/子进程路径：

| 门禁 | 结果 |
|---|---:|
| compileall（core/tests/两个独立插件） | passed |
| F0 Admission/取消 + F1 SQLite/Feed/跨进程 | 106 passed, 2 skipped |
| F2 retry/Provider + F3 Driver/observation/Product | 103 passed |
| F4 Textual 8.2.8 headless/optional（replacement 前历史） | 14 passed |
| Session/Turn/Step/Attempt/Runtime/Product 相邻组 | 313 passed |
| Workflow/Budget/Workspace/Artifact/Promotion/Evaluation 相邻组 | 455 passed, 3 skipped |
| 全部 CLI 测试 + Evolution comparison | 538 passed, 1 skipped |
| core-only collect-only | 2496 |
| 隔离 `[tui]` collect-only | 2503 |
| 修改范围 Ruff | passed |
| `git diff --check` + 新验证记录 trailing whitespace | passed |
| 13 个改动 Markdown、415 个本地链接、围栏与正式↔通俗映射 | passed |
| 变化范围秘密/本机路径/Provider 示例与生产示例硬编码扫描 | passed |
| 四个受保护核心文件 | zero diff |
| `traceh` / `chat` / `eval` help 与 `0.8.0` 版本派生 | passed |

这些测试组有意按 owner 分层且彼此存在重叠，不能把 passed 数字相加冒充全仓总数。六个 skip 都是
既有 Windows 平台标记，来自目录/数据库链接权限与路径不能包含 NUL 的边界；没有新增 skip、失败或被
过滤的红灯。SQLite exact schema、未知 hot
journal、同流跨进程 CAS、异流 busy、backup/restore、legacy refusal、close/cancel，Evaluation attempt
与 Evolution comparison 的显式 Store ownership，以及所有 CLI production roots 均在上述组中覆盖。

完成这些门禁只允许进入一次全局独立 P0/P1 审查，不等价于最终发布通过；最终完整 pytest 按冻结计划
继续等待审查清零。

## 4. 全局审查、最终全量与发布资产

2026-08-30 的 F5 全局独立审查已结束：**P0=0、P1=0、P2=2**。两个 P2 都是早期项目上下文没有
随现行架构回填的叙述偏差，不影响运行时或打包兼容性：Windows CI 仍被写成已删除 JSONL
`msvcrt/fcntl` 锁分支的门禁；Plugin Creator Wheel 仍被写成携带 v0.6 作者合同。发布候选已先修正式
上下文，再同步通俗版：CI 现在准确描述 `.[dev,tui]` 下的 SQLite/Git 子进程、终端、路径长度和 Textual
平台覆盖，Creator 资源准确描述当前 v0.8 合同及 `traceharness-py>=0.8,<0.9` 新候选范围。

独立审查实际复核了版本/Discovery/Manifest/TUI optional `61 passed`、Product 版本钉住 `2 passed`、
Plugin Creator `10 passed`、Python Quality `18 passed`，并通过 PEP 440、diff 与工作区不变性检查。审查者
首次从插件目录误调用两个仓库根测试，因路径不存在而退出；随后从仓库根完整重跑，前一条无效命令
没有计入证据。审查没有运行最终全量、联网、Wheel、离线安装或真实 Provider。

全局审查清零 P0/P1 后，唯一最终全量已在安装 Textual 8.2.8 的开发解释器中运行，确保 optional TUI
Pilot 也进入同一门禁。命令使用全新仓库外短 basetemp，未筛选测试、未使用 `--lf`、未联网：

```powershell
C:\traceh-tui-dev\Scripts\python.exe -m pytest -o addopts='' -q --durations=30 --basetemp C:\thf5-final-v080
```

结果：**`2496 passed, 7 skipped in 2133.68s (0:35:33)`，退出码 0**；总收集口径为 `2503`。七个
skip 均为既有 Windows 平台边界，没有新增 skip 或过滤失败。最慢项为
`test_real_candidate_validation_runs_every_l2_gate` 的完整 L2 隔离验证，耗时 `1089.38s`；其后是 clean
plugin Wheel E2E setup `70.13s`，再后是 Product Benchmark E2E `39.21s`。中间的长静默由这些真实隔离
门禁解释，不是进程挂起。

### 4.1 发布前 TUI replacement 重新打开门禁

上述全量之后，真实用户体验确认旧 TUI 的固定按钮、弱 operation 反馈和不可见退出过程不满足本版本的
可用性目标。实现因此在同一个 `traceh chat --tui` 路径直接替换 presentation，没有保留 legacy TUI、
兼容开关、第二 Runtime、第二 Product control 或第二事实源。新版从 fresh Product observation 读取
Product/Workflow 的 projection-bound head 和其他相关流 latest event/time，以宿主 monotonic clock 显示
task-bound 事实年龄与等待时长；只渲染合法闸门并要求 typed
confirmation；START 在途而 durable Workflow 已 RUNNING 时仍可通过原 Product control 正常 Cancel；
initial observation error 独立可见并周期重试；分歧时 refresh 保持纯读；关闭过程逐 owner 可见。

当前 replacement 原定向组为 **38 passed**，当时 core-only 与隔离 `[tui]` 分别收集 **2504** 与
**2520** 项；
包括一条真实 `ProductChatHost` + auto Router + fixed multi
topology + managed local Git + Verifier/Review 到 Approval barrier 的 Textual Pilot。该链抓住并根修了
durable task 已存在后旧 transient Proposal 仍压住 Approval 的缺陷；另一个确定性竞态证明 Product refresh
必须串行，较早的旧读不能覆盖较新的 projection。移除 operation 同步刷新、refresh lock 或 durable-task
优先规则时，相应反例均按根因失败，恢复后通过。退出卡死又定位为 UI 自己误占 Textual 内部 `_closing`
字段；现在使用独立 UI 字段，真实 active Provider + Ctrl+C 测试完成收敛并退出。

replacement 独立审查得到 **P0=0、P1=2、P2=2**。两项 P1 是 START caller 活跃时正常 Cancel 不可达，以及
initial observation 失败被伪装成无任务且不再恢复；两项 P2 是其他任务的全局流刷新当前任务事实年龄，
以及 details/窄屏展开状态不同步。四项均已在原 presentation/observation/Product control 主线修复。新增
测试覆盖真实 Product host RUNNING → typed Cancel → durable CANCELLED、初始读取失败后的可见错误与周期
恢复、后续成功读取清旧 observation error、task-bound age 和 details 双向同步。六项反向验证分别恢复旧
gate、空面板早退、无 discovery retry、无 task-bound 过滤、单向 details class 和旧 observation error；
每项都按根因失败，恢复后上述 `38 passed`。短独立复审随后确认这四项 `P0/P1/P2` 清零。

### 4.2 真实体验发现 Product Chat 越过 START 前权限边界

短复审之后的真实 TUI 流程出现 `failed / workspace-source-invalid`。durable 证据与真实 Git 状态证明：
用户自然地把 Product source 同时作为 Chat Workspace 后，请求者 Chat 在 confirmation Tool 返回后仍继承
普通 Coding Chat 的 `apply_patch`/`shell`，模型先修改了 source；真正 Product START 随后由原
`LocalGitWorkspaceProvider` source-clean gate 正确拒绝。该失败不是 TUI widget、Promotion 或 Git ref
问题，也不能通过清理 source 或放宽 Workspace owner 掩盖。

根修位于唯一 CLI Product Chat composition：关闭默认 Coding Tool 集，只装配 list/read/search 与
proposal/confirmation；`ProductChatSideEffectPolicy` 对所有声明为 Workspace 写入、进程、网络写或外部
事务的 core/plugin Tool 单调拒绝。普通 Chat 不带 Product 配置时仍保留五个默认 Tool；Product coder 在
START 后的 managed worktree 仍保留 Profile-owned 写权限。没有修改 AgentLoop、AgentRuntime、Supervisor、
PluginManager、Workspace gate、Product 状态或 TUI control path。

当前新增证据：

- 真实 Git/Line E2E 让 Chat Workspace 与 Product source 完全相同，并让确定性 Provider 在确认后强行
  请求 `apply_patch` 与 `shell`；两次只留下 `UnknownTool` result，source clean，Product 正常到 Approval；
- 已注册的通用 `WORKSPACE_WRITE` Tool 被同一 Policy 记录为 `ToolDenied`，没有 Effect；
- Textual 8.2.8 Pilot 复用同一真实 source，走 proposal/confirmation/typed START/auto→multi/Verifier/
  Review 并到达 Approval，source 仍 clean；
- 反向移除只读工具面和 Policy 后，公开路径真实改写 `tracked.txt`，Product 精确恢复
  `failed/workspace-source-invalid`；恢复保护后重新通过。

该阶段根修分层验证为 Product F3 E2E **23 passed**、CLI/Product 架构配置 **54 passed**、完整 Textual
Pilot **16 passed**、Product observation/CLI activity/timeline 相邻组 **256 passed**；这些组有重叠，
不能相加冒充总数。core-only 与隔离 `[tui]` collect-only 当时分别为 **2508** 与 **2524**。

### 4.3 多轮 START 体验定位 eager cleanup 锁死

早期 clean demo 首先证明这不是普通的模型等待：Router Session 在约 `1171 ms` 内写出严格可解析结果、
successful Attempt/Turn 与 delivery terminal，Product 却停在 `task-opened`。后续 demo 又出现 coder 已写
failed `turn/end` 和 `agent/message-failed`，Workflow 仍没有 node-failed/run-finished，说明故障不只属于
Router responder。相同 Provider、配置和 Product mainline 经 Line adapter 能在角色失败后约两秒写出
Workflow/Product terminal，因此 Provider error 是触发，不是根因。

真实 Textual 复现时，App 内 asyncio watchdog 同样停止。一个独立 native thread 每十秒调用
`faulthandler.dump_traceback(all_threads=True)`，连续得到完全相同的主线程栈：
`AgentRuntime._shutdown` → `GenerationCompositionRuntime.dispose/_cleanup_generation` →
`CompositionGeneration._cleanup_owned_resources` →
`PluginActivationSet._dispose_for_generation/_dispose_body`，最终阻塞在
`plugins/manager.py` 的 `_claim_lock`。

旧 `PluginActivationSet` 持有非重入 `threading.Lock` 时调用 `asyncio.create_task(_dispose_body())`。Python
3.12 eager task factory 会在 `create_task()` 返回前同步进入协程；Textual 路径暴露了该语义。core/empty
ActivationSet 的 cleanup 在第一次 suspension 前再次申请同一把锁，因而同步冻结整个 event-loop thread。
Line adapter 通常使用 lazy task scheduling，解释了同一代码的界面差异。此前删除 Router responder-local
dispose 的修改属于症状修补，现已撤回；Supervisor/Product/Workflow cleanup owner 没有改变。

当前唯一修复在 Plugin ActivationSet：同步锁内只核对 owner 并冻结 `disposing`，锁外才创建 cleanup Task；
一个 async start lock 保证并发 caller 仍共享唯一 Task、相同终态/失败和重复取消收敛。确定性反例在真实
Runtime dispose 路径启用 `asyncio.eager_task_factory`，并把同线程锁重入变成立即 `RuntimeError`。旧实现
精确经 Composition Drain 失败；保护恢复后该反例转绿。插件 ActivationSet、Runtime dispose/cancellation/
Composition 相邻组、生产 SQLite auto ProductTask 和完整 Textual Pilot 均继续通过；没有增加 TUI 特例、
retry、fallback、第二事实源或第二生命周期。

实际 Textual 8.2.8 环境的完整 Pilot 首次整组运行还暴露三个测试事件循环交接缺口：点击/回车后立即读取
confirmation 或 `_operation_task`，会偶发读到上一帧；对应单项重跑通过。测试在交互边界显式等待一次
Textual `pilot.pause()` 后，目标 Cancel 单项连续 **10 passed**，完整 Pilot **16 passed**。这是测试同步
修复，没有在生产 TUI 增加任意 sleep。

本次 eager-cleanup 根修的聚焦门禁已经确认新增反例旧逻辑失败、正确逻辑通过：Plugin ActivationSet
**21 passed**，Runtime dispose/plugin cancellation/Composition **72 passed**，
生产 SQLite auto ProductTask **1 passed**，Textual Pilot **16 passed**，Product/Observation/Contract/
Architecture **118 passed**，presentation/observation/activity/timeline **256 passed**。更宽的插件生命周期组
**200 passed**；这些组有重叠，不相加冒充全量。core-only 与隔离 `[tui]` 当时分别收集 **2508** 与
**2524** 项；这是下一项身份交接修复之前的聚焦证据，没有运行最终完整全量或新的真实 Provider
acceptance。

### 4.4 终态任务到下一项 Proposal 的精确身份交接

继续真实体验时，用户在一个已 durable FAILED 的任务之后提交并确认了下一项 Proposal。公开界面的实际
结果是：左栏已经显示新 Proposal 与精确 START request，右栏却把新 requirement 和旧任务的
`workflow-node-failed` 混在一起，并隐藏 `START task`。用户输入与模型确认均已成功；错误来自 TUI 保留
旧 `_observation`，并用“是否存在任何 durable task”替代“durable/transient 是否为同一 exact task id”。

根修位于唯一 `TracehTuiApp` observation lifecycle：接收 Proposal 或 START request 时先执行同一 task
identity handoff；若当前 selection/observation 属于另一 task，则关闭并等待旧 observer 收敛，清除的只有
旧内存 projection、接收时间与 observation error，再选择新 pending task。SQLite 里的旧终态事实不删除、
不迁移；尚未 START 的新任务也不伪造 Product fact。没有修改 ProductTask control、Workflow、Store、
Provider、Supervisor 或 Promotion owner，也没有增加第二个 pane 状态机或 legacy TUI compatibility。

新增 Textual Pilot 从 restored FAILED task 出发，通过正常 Chat resolution 先返回新 Proposal、再返回其
`ProductStartRequest`，验证旧 observer 已关闭、新面板不再含旧 failure、Proposal 阶段没有越权 START，
确认后只出现新 task 的 START。反向验证临时移除 Proposal 与 START 两处 handoff，测试稳定恢复“新标题 +
旧失败 + 无 START”；恢复保护后单项与完整 Pilot 转绿。该阶段 Textual Pilot 为 **17 passed**，
presentation/optional/observation/Pilot 聚焦组为 **39 passed**；再加 Product F3 E2E、Contract 与
Architecture 的六文件相邻回归为 **143 passed**。隔离 `[tui]` collect-only 为 **2525** 项；这些分组
彼此重叠，不包含也不冒充最终全量。

### 4.5 重复真实失败：Provider multiline Tool arguments 根因

2026-08-31 的全新真实 TUI 运行不再卡住，但 coder 稳定以 `workflow-node-failed` 结束。没有从界面文案
猜根因，而是从同一 SQLite fresh replay Product、Workflow、Directory、Session、Budget 与 Workspace：
Router 成功并严格解析，Workspace/Agent/Workflow 创建正常；coder 已成功执行 list/read，随后没有
ApplyPatch Effect、Review 或 Promotion。使用该 Session 的 exact frozen request 重新调用同一 Endpoint，
得到 HTTP 200 与 `apply_patch` Tool Call，但 `function.arguments` 的 multiline `old_text/new_text` 使用
Python 双三引号，标准 `json.loads()` 在位置 39 稳定失败。首个真实错误因此是
`provider-response-invalid`，后续 `agent/message-failed`、`workflow-agent-message-failed` 与
`workflow-node-failed` 都只是包装；Budget、Git、TUI 和 retry window 不是根因。

根修位于现有 [`OpenAICompatibleProvider`](../src/traceh/llm/openai_compatible.py) response boundary，并由
[`ADR-0038`](adr/0038-schema-gated-multiline-tool-arguments.md) 冻结：标准 JSON 永远先行；只有失败时，
tokenizer 才能识别顶层 object value 的双三引号 token，并要求 exact frozen Tool schema 把该字段声明为
`type=string`。替换后整体仍必须由标准 parser 解析为 object。未知 Tool/字段、非字符串/嵌套字段、单
三引号、表达式、注释、尾逗号、未闭合与其他邻近 malformed 输入继续 fail closed；没有
`eval`/`literal_eval`/JSON5、retry/fallback 或按 qwen、库存、文件名、Tool 名硬编码。

新增 Provider 证据包括真实 response 同形 multiline 正向例、12 个邻近拒绝反例（包括 strict/normalized
`NaN` 与正负 `Infinity`），以及公开
`AgentRuntime + local HTTP OpenAI-compatible + ApplyPatchTool` 路径。修复前正向例稳定得到 protocol
failure 且零 Tool/Effect；恢复保护后完成 Turn、落盘 Tool Call/Result 并修改临时文件。当前 Provider 文件
定向组为 **24 passed**，Provider + retry 相邻组为 **45 passed**。临时恢复 Python 默认 decoder 后，
`strict-json-nan` 反例稳定出现 `DID NOT RAISE`；恢复 strict decoder 后重新通过。

### 4.6 唯一浅色布局、叶子失败与真实完成路径

用户提供的外部 `TUI_LAYOUT_FIX_ORDER.md` 只作为本轮验收输入，没有进入仓库资产、链接或运行时默认。
实现按 D1–D7 原地替换同一个 Textual adapter：唯一 `textual-light`
主题；Product 摘要顶部起排、gate 底部；摘要不重复 gate 指令；typed confirmation 只替换 gate；START/
批准/驳回为同一透明描边；facts 固定列宽；短对话底锚；模型正文以暗色斜体 `模型 ·` 显示。没有旧 TUI、
兼容开关、第二 widget 状态机或 owner/control/observation 写入变化。

18 条 Textual Pilot 与 10 条纯 presentation 合同最初为 **28 passed**。随后真实截图暴露 facts 内宽为 52，
而 18/22/12 数据列再加两个分隔符实际需要 58 的确定性算术错误；列宽改为 17/21/12（含两个单字符
分隔后正好 52），测试固定总宽。复审又证明 100–109 列双栏实际只给 facts 47–51 列，因此唯一 narrow
breakpoint 提高到 `<110`；99/100/109 明确单栏，110 才进入至少 52 列的双栏，避免年龄列再次折行。
确认态保留摘要、短聊天最后一条消息距输入框不超过两行，长日志仍自动滚动，110×34 与实际 PowerShell
截图均已查看。临时把 breakpoint 退回 100 后，109 列真实 Pilot 稳定失败；恢复 `<110` 后
产出可见性补齐前，TUI/presentation 为 **34 passed**，短复审确认当时范围 **P0/P1/P2=0**。

为了让失败不再只显示包装 code，`ProductInspectionEvidenceReader` 通过失败 node 重算 exact
Agent/Session/create-request/message identity，核对 Directory；`workflow-agent-identity-conflict` 不能
采纳碰巧占住可预测 id 的外来 Agent。只有 `workflow-agent-message-failed` 才会在同一 Store fresh replay，
经过 `CoreInvariantChecker` 后，只投影 `turn/start.message_id` 等于 deterministic Workflow message 的精确
Turn 中合法 `runtime/error` 的稳定 code/category/type。读取器按事件顺序维护实际 open Turn；Workflow
message/Turn 必须唯一，目标 Turn 只能有一条 runtime error、error 必须绑定当时实际 open Turn，并以
`reason=failed` 闭合。同一 Session 后续通过公开 `resume` 运行的无关 Turn、payload 冒充旧 Turn 或重复
message/Turn 都不能覆盖原失败。TUI 先显示叶子失败，再保留 Workflow wrapper；缺少可靠 leaf 时明确
`unavailable`，原始正文、header、message 与 traceback 不显示。14 条 leaf 反例覆盖 Provider/generic/
missing/invariant/malformed、真实 preset/request identity conflict、非 message failure、后续无关 Turn、
伪称旧 Turn、缺少或矛盾 terminal、重复 runtime error 与重复 message/Turn；leaf/presentation/observation/
architecture 聚焦组为 **49 passed**。反向移除 message/open-Turn/terminal/唯一性 binding 时对应新反例
稳定变红；移除 identity-conflict 隔离时外来 Agent 被错误采纳；恢复保护后对应测试全部通过。

最后从全新 `C:\traceh-tui-accept-1` 执行一次真实 TUI acceptance。CLI 使用实际 Product config、SQLite、
同一 Driver/Product host、真实 `openai-compatible/qwen-plus`、managed Git、Verifier/Review、typed gate；
进程代理变量显式置空。既有 loader 使用 `.env`，但本窗口没有打开、搜索、打印、复制或记录 Key。
用户路径按 Proposal → 自然确认 → START 点击与 typed `START` → auto resolved single → coder →
awaiting approval → Approve 点击与 typed `APPROVE` → Promotion 执行。

结果为 Product `completed`。Source 始终 clean 且保持原 commit；一次性 bare target 从原 base 前进到新的
integration commit，推广 checkout 的 `python -m unittest discover` 为 **4 tests / OK**。fresh public
reader 核对 4 个 Session stream 的核心不变量均为 0、open turn 为 0；3 个 Budget account 均 closed，
13 个 reservation 均 settled，3 个 Workspace 均 released。最终浅色 completed 截图还确认 facts 年龄列
不再折行。这是一条定向真实验收，不是完整 18-attempt Benchmark，不作质量统计结论。

这次 replacement 发生在旧全量之后，所以 `2496 passed, 7 skipped` **只保留为替换前历史证据**。产出
可见性补齐前，core-only 与隔离 `[tui]` collect-only 分别为 **2540** 与 **2562** 项，不是全量通过数。最终相邻 owner
回归（Provider/retry、失败证据、Product observation/F3/contract/architecture、Workflow execution/
architecture、TUI/presentation/optional）在最新树重跑为 **251 passed / exit 0**。首跑唯一失败来自 Pilot
在同一 tick 私调 observation refresh 后绕过用户点击同步；测试改为等待 Textual 重排后经 `pilot.click()`
走真实用户坐标，生产逻辑未为测试增加分支，随后 TUI **34 passed** 与相邻 **251 passed** 均转绿。
Provider/TUI/失败证据与跨 owner 最终复审当时已经清零；补齐前候选完整全量的实际过程与最终结果见 4.8。

### 4.7 产出可见性补齐

完成真实 ProductTask 后，默认 Chat Session 只能显示 requester 对话，Router、parent/reviewer/coder 各自
Session 的模型文字和工具活动不可见。修复没有增加第二 TUI、第二 EventStore reader、缓存或跨 Agent
时间线，而是在现有浅色 adapter 上增加两个全宽只读页面：

- `Ctrl+T` 每次打开先由现有 `ProductObservationReader` fresh read exact task。Router 必须经 Agent
  Directory 证明 Agent/Session 绑定；固定 Workflow 角色继续使用 deterministic node identity，并核对
  Directory 的 Session/create request。每条 Session fresh read Session/Effect facts，通过
  `CoreInvariantChecker` 后，按 canonical `seq` 单遍组合 user/model 发言与 tool call/result；工具调用和结果合成
  一行并显示精确 `seq` 区间，超出页面上限时明确报告剩余工具调用和发言数量。页面按
  router/parent/reviewer/coder 分组，不扫描 Store、不缓存、不订阅、不 append，也不实时 tail。Usage
  unknown/missing/malformed 时显示 unavailable；shell 参数、tool result 正文、文件内容、stdout 与 raw
  payload 不显示。
- `Ctrl+P` 对 durable task 同样 fresh read observation，再显示 task、Chat/origin/confirmation/router/固定
  角色 Session、Workflow、source、Review、target、Patch、digest/receipt 等已经建立的完整身份。复制只取
  选定原值；剪贴板失败时才写独立临时文本文件并显示路径，该导出不进入 SQLite 或模型上下文。
- 默认 Review 区恢复现有 inspection evidence 的 changed paths（最多八项并标余数）、Verifier command
  id/status/exit/argv digest 和最多十二行 Patch preview；截断与 UTF-8 replacement 明示，不冒充完整 diff。
- 当前状态栏只提供真实存在的退出、`Ctrl+T` 和 `Ctrl+P`。本轮不提供 `Ctrl+I` 写入型 reconcile 或
  `Ctrl+R` raw events；分歧时提示使用既有 Line `/task inspect <task-id>`，普通 refresh 仍绝不写事实。
- 用户实际批准后又发现左栏仍停在此前 `awaiting_approval` 提示，而右栏已从 fresh facts 显示 completed。
  根因不是 Store 或 Promotion 不一致，而是 TUI `_execute_product()` 丢弃了既有 typed
  `ProductCommandResult.advance`。现在批准/驳回/取消/放弃成功并完成 fresh observation 后，左栏追加操作名
  与 durable Product status；该提示只存在于 widget，不 append Session/SQLite，也不进入模型上下文。

确定性测试源码覆盖 exact multi 四角色、Router Session/Directory 篡改拒绝、Session invariant 拒绝、
unknown Usage unavailable、shell 参数与 result 正文遮蔽、无全 Store 扫描、读操作不改变 heads，以及外部
append 后重新打开可见；Textual Pilot 覆盖宽/窄屏 screen、fresh identity/copy fallback，并用真实本地 Git
auto→multi Product host 到达 Approval 后在 `Ctrl+T` 显示 router/parent/reviewer/coder 四段。实际结果：

- 产出可见性补齐检查点的 `tests/test_tui.py`、presentation、conversation projection 与 optional 边界：
  **`54 passed`**；其后的首批可读性改造（R1/R3/R2）在同一 Textual 8.2.8 环境重新运行三个直接测试模块，
  **`47 passed`**；相邻 Product observation/inspection/F3/contract/architecture/CLI Chat owner 为
  **`161 passed`**；
- 加入 Product observation/inspection/F3、CLI Chat、contract/architecture：**`215 passed`**；
- 全仓 `[tui]` collect-only：**`2575`**；compileall、修改范围 Ruff、`git diff --check` 通过；
- 独立短复审：**`P0=0 / P1=0 / P2=0`**。复审解释器未安装 Textual，故其本地只运行纯投影
  `22 passed`；上面的 `25` 项 Textual Pilot 与 `215` 项相邻组由安装 Textual 8.2.8 的隔离环境实际运行；
- 反向验证临时移除 Router Agent→Session Directory 绑定后，合法但无关 Session 被接受，测试确定性报
  `DID NOT RAISE ProductStateError`；恢复保护后转绿。
- 新审批反馈反例真正点击批准、输入 `APPROVE` 并让右栏到达 completed；旧逻辑精确失败于左栏缺少完成
  提示，恢复保护后转绿。测试同时 fresh replay Chat Session，证明该 UI 提示没有成为新的 durable 消息。

该批改动发生在下节记录的 `2555 passed, 7 skipped` 之后；本记录不把旧数字冒充当前最终全量。用户已开始
实际体验并据此发现上述 P2；该小修尚待短复审，新的唯一最终全量仍未运行，也未进入 clean-input 打包或
发布门禁。

首批可读性改造只完成 R1、R3、R2，并在截图确认前显式停止：`Ctrl+T` reader/screen 从 494 行降到 490 行；
主对话使用用户默认、宿主 teal、模型逐行缩进暗斜体三种左边缘；Product pane 固定为任务头、最近 durable
事实、证据、临时操作/终态四组，组间恰好三条分隔线。角色导航与补丁预览精简属于 R4/R5，尚未实现。

### 4.8 replacement 后、产出可见性补齐前的完整全量

第一次完整运行没有筛选、没有 `--lf`，使用安装 Textual 8.2.8 的开发解释器和全新仓库外短 basetemp：

```powershell
C:\traceh-tui-dev\Scripts\python.exe -m pytest -q --durations=30 -o addopts='' --basetemp C:\traceh-test\full-20260831-a
```

结果为 **`2553 passed, 7 skipped, 2 failed in 3106.78s (0:51:46)`**。两项失败分别证明：

- START 可能在测试读取 transient 文案前已经合法推进到 durable opened；这两个观测点都能证明同一次
  START 已进入宿主主线，后续断言仍要求真实 operation、Approval/Review 证据和 source clean；
- Button handler 内立即 `focus()` 会被 Textual 点击收尾的焦点处理覆盖，真实用户紧接着输入的 typed
  confirmation 可能被吞。

生产根修只在当前 TUI presentation owner 使用 Textual 8.2.8 公开
`call_after_refresh(field.focus)`；没有增加状态、控制面、权限或兼容路径。短复审随后复现一项测试稳定性
P2：Pilot 仍可能在 deferred focus 前直接给 `Input.value` 赋值并把 Enter 发给当前按钮。该 Finding 出现后，
一次尚未完成的全量在 691 passed 时主动中止，不计入完整门禁证据。所有 START/Cancel/Approve Pilot
随后统一等待并断言真实 `Input.has_focus` 后才模拟键入。

第二次完整运行使用另一全新目录，结果为
**`2554 passed, 7 skipped, 1 failed in 3076.66s (0:51:16)`**。唯一失败证明测试还遗留两处
`pilot.click()` 后、handler 消息尚未处理就立即读取 `_confirmation_action`；异常 teardown 后队列中的点击
才执行，因 Screen 已卸载而出现 `NoMatches`。这仍是同一个测试同步根因，不是新的 Product/TUI 生产故障。
断言移到统一焦点同步点后，原失败用例在 10 个独立 pytest 进程全部通过，完整 TUI/presentation 又连续
三轮 **`34 passed`**；独立短复审结论为 **P0=0、P1=0、P2=0**。

最终从第三个全新目录完整重跑：

```powershell
C:\traceh-tui-dev\Scripts\python.exe -m pytest -q --durations=30 -o addopts='' --basetemp C:\traceh-test\full-20260831-d
```

结果为 **`2555 passed, 7 skipped in 3078.80s (0:51:18)`，退出码 0**；总口径 `2562` 项。它证明的是
4.7 之前的候选，不能认证当前工作树。
最慢项仍是 `test_real_candidate_validation_runs_every_l2_gate` 的完整 L2 隔离验证，耗时 `1403.74s`，
不是挂起。七个 skip 仍是既有 Windows/optional 平台边界，没有新增 skip、过滤失败或用缓存结果冒充。

当前仍为 **NOT RUN / PENDING**：

- 从 clean committed input 构建 Wheel、sdist 与 Git-index source ZIP；
- archive 内容/秘密/版本审计；
- 无 `[tui]` 核心与带 `[tui]` 的两套全新离线安装。

候选资产完成后应为：

- `traceharness_py-0.8.0-py3-none-any.whl`；
- `traceharness_py-0.8.0.tar.gz`；
- `traceharness-py-v0.8.0-source.zip`。

提交、annotated tag、push 与 GitHub Release 也尚未执行。

## 5. 真实 Provider 与秘密边界

完整 18-attempt 真实 Provider acceptance 需要用户另行授权；当前 **NOT RUN**。若获授权，只允许使用全新
SQLite data dir 和全新 eval output，一次完成，不补跑失败项、不 fallback、不覆盖历史报告，不打开、
打印、复制或记录 `.env` Key。历史 v0.7 网格不能冒充 v0.8 retry/SQLite/TUI 候选的发布证据。

当前已完成上节一次定向真实 TUI ProductTask acceptance：通过既有 loader 使用 `.env`，但没有打开、
搜索、打印、复制或记录 Key；进程代理显式置空；只使用本地 source 与一次性 bare target，没有真实远端
Git。它不替代本节仍为 NOT RUN 的完整 18-attempt 网格。用户未跟踪笔记、缓存与审查目录不进入候选提交
或 source ZIP。
