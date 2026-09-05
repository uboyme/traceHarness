# TraceHarness Py v0.8.0 发布候选验证记录

验证开始日期：2026-08-30

状态：**F5 实施中；真实体验后的 Product Chat、Plugin cleanup、TUI identity/layout/focus、Provider
multiline 与失败证据归属均已按根因修复。一次真实 TUI ProductTask 已从 Proposal 走到 Promotion；
Provider、TUI、失败证据与跨 owner 复审曾清零 P0/P1/P2。随后同一 TUI 又补齐 fresh 角色对话、fresh
完整身份和 Review evidence；M1 随后完成完整 Patch/任务对话出口，M2 又把 requester Product context
升级为同 Session 有界 format-7 任务目录、当前 focus 最小执行摘要和同 Session 按需证据 Tool。代码与
测试均发生了实质变化，因此此前 `2555 passed, 7 skipped` 只保留为历史证据。M2 当前候选已经通过
独立代码/文档复审、真实 L2 与新的唯一最终全量：`2665 collected / 2658 passed / 7 skipped`、退出码 0。
clean-input 资产、双形态离线安装、真实 Provider 网格、commit、push、tag 与发布仍未执行。**

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
- requester Product context 在每个 Turn 前 fresh replay 同一 EventStore，以一条 format-7 Session 收据原子
  携带当前 focus、同 Session 近期任务、准确总数/省略数，以及 system 当前事实 + user 历史参考；稳定
  checkpoint 的当前 focus 只增加六项最小执行摘要，详细事实由同 Session、精确 task id 的纯读
  `read_product_task_evidence` 按需重建；它不新增 Workspace Memory、RAG、缓存或控制权限；
- 既有人工 `START`、Approval、bare target Promotion 与唯一 `traceh eval` ProductTask Benchmark。

F5 没有增加 Runtime、Workflow、ProductTask 状态、Event bus、Benchmark Runner、Provider/model fallback、
Skill、Workspace Memory、RAG、OS sandbox 或真实远端 Promotion。M2 只把既有 Session perception receipt
破坏式升级到 format 7，并增加同一 EventStore 上的无状态 pure-read evidence join；它不是新的 Memory 域、
缓存或第二事实源。

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
批准/驳回为同一透明描边；facts 固定列宽；短对话底锚；模型正文以低饱和紫色斜体 `模型 ·` 显示。没有旧 TUI、
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
- 该检查点的默认 Review 区恢复 inspection evidence 的 changed paths（最多八项并标余数）、Verifier command
  id/status/exit/argv digest 和最多十二行 Patch preview；后续 N10–N11 已按 4.10 用完整 Artifact 摘要取代 preview。
- 该检查点状态栏只提供退出、`Ctrl+T` 和 `Ctrl+P`；后续 4.10 增加已实现的 `Ctrl+D`。仍不提供 `Ctrl+I` 写入型 reconcile 或
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
实际体验并据此发现上述 P2；在该历史检查点，小修尚待短复审，新的最终全量也尚未运行。后续 M2 的当前
复审与全量结果见 4.13；clean-input 打包和发布门禁仍未执行。

首批可读性改造只完成 R1、R3、R2，并在当时的截图确认前显式停止：`Ctrl+T` reader/screen 从 494 行降到 490 行；
主对话使用用户默认、宿主 teal、模型逐行缩进低饱和紫色斜体三种左边缘；Product pane 固定为任务头、最近 durable
事实、证据、临时操作/终态四组，组间恰好三条分隔线。该时点尚未实现后续改动页。

随后按第四轮工单只完成第一组 N1–N4，并再次停止。主聊天和 `Ctrl+T` 不再把长正文交给 Textual 猜测
换行，而是按 terminal cell 宽度扣除稳定前缀后预折行；跨 110 列断点时，主聊天只重排 RichLog 已有
可见行，任务页只从打开时 snapshot 重画。两条路径都没有新增缓存、Store reader 或事实源。模型与工具
左缘改为 `▏`。工具 seq 区间仅在单行可容纳时以 dim 右对齐，窄屏省略。shell 继续只显示 canonical
arguments 的 UTF-8 字节数；其他工具只复用 Timeline 安全 allowlist，`search_text` 显示必填 `query`，
敏感 query/path 与未知工具继续 fail closed。精确整数 exit 0 显示成功，非零以 warning 显示
`完成 · exit=N`。

第一次真实 Textual SVG 截图还发现 mount handler 运行时 RichLog 尚无有效布局宽度，Session/Workspace 会
按 1 列逐字预折行。根修先读取 durable surface，再用 `call_after_refresh` 在首个真实布局上一次性绘制系统
信息与已有 conversation；确定性 Pilot 等待首帧并断言稳定宿主前缀，截图重新生成后不再竖排。

用户确认 N1–N4 截图后，第二组只实现 N5–N6：模型自述正文统一为唯一浅色主题的低饱和紫色
`#7d6bab`，保留斜体、取消 dim 继承且不加粗；marker 继续 dim。任务对话的工具行拆成独立 Rich
segments，只有左侧 `▏` 使用既有强调蓝，工具名与安全参数保持默认色，seq dim、成功结果 dim、非零
exit warning 均不变。实现中发现 Rich `Text` 的 base style 会被后续 segment 继承，因此改为从空 `Text`
逐段 append；这是一条渲染根因修复，不改变任何字符串、cell 宽度、遮蔽、owner、闸门、snapshot 或
durable reader。当前前景语义为默认、dim、teal、purple、emphasis、warning 六种，danger 仍为第七个
预留槽；没有深色主题分支或第二种紫色。

N7–N9 随后只收尾右栏：`^p 查看完整身份` 只保留在 digest 行；completed 终态只保留精确
`已合入 · Promotion receipt 已记录`；生命周期轨在任一 ProductTask durable 终态只对该轨使用 dim，证据和
终态结论不随之变暗。证据首行从同一 EventStore fresh 重建 Agent Directory 与 Budget Ledger，按当前
ProductTask owner 的 durable 所有权子树只累计 `charged` Token、Step 与 wall milliseconds；其他任务的
账户不混入，缺失、未知或尚未 settlement 的维度显示 `—`，没有本地计时器估算。Budget 全局 Stream 只
触发 refresh，仍被排除在当前任务事实年龄之外。没有新增 durable 写入、控制面、快捷键、颜色或第二事实源。

当前验证结果：

- N7–N9 Product observation/presentation/TUI 直接组：**`55 passed`**；
- Product observation/presentation/inspection/F3 选择相邻组：**`66 passed`**；
- Product/TUI architecture 与 optional 边界：**`23 passed`**；
- 全仓 collect-only：**`2605`**；
- `python -m compileall -q src tests`、修改范围 Ruff、`git diff --check`：通过；
- 示例硬编码扫描在 N7–N9 修改的生产文件无命中，没有把库存、文件名、模型、任务 id 或本机路径写入默认；
- N1–N4 与 N5–N6 两次独立只读短复审：均为 **`P0=0 / P1=0 / P2=0`**；后者另行重跑直接三文件
  **`54 passed`**，确认 base style 继承根因关闭、六色前景语义成立；N7–N9 独立只读复审同样为
  **`P0=0 / P1=0 / P2=0`**，并另行运行 observation/presentation **`28 passed`** 与真实 Product
  host/Approval TUI 路径 **`2 passed`**。

本检查点未运行新的完整 pytest、联网、真实 Provider/API、Wheel、离线安装或发布门禁；当时 N10–N12 与 R4
没有提前实现。N7–N9 的真实 Textual 截图在该停止点生成并经用户确认，随后才进入 4.10。

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

### 4.9 requester Product status context 根修

2026-08-31 的后续真实体验出现一条上下文反例：Product/Workflow/Promotion 已 durable 完成，TUI 右栏
显示 completed，但下一轮 requester 模型仍声称任务未执行。第一版修复增加 status-only context；2026-09-01
的再次体验证明请求已经含 `status: completed`，模型仍把 requester 自己没有写 Tool 调用和 requester
Workspace 未变误当成 ProductTask 未执行。根因因此收敛为 canonical bridge 的关系/actor/status 语义不完整，
不是 TUI、SQLite、Provider、Compaction 或并发缺陷。

format-3 历史检查点保留同一条 host-owned `product/context-snapshot`，把 envelope schema 1 内的 message protocol
硬切到 format 3。ProductTask 仍是状态权威；Session 事件冻结 task/head/status，以及固定 same-requester-Session、
条件式 host-managed Agent/managed Workspace、逐 status meaning、明细省略范围、response rule 和非授权说明。
`started` 只证明 ProductTask 已接受 START，不把 Product 事实夸成 Workflow run-start 事实；`completed` 只证明
ProductTask durable terminal 且携带 Promotion reference，说明正常控制路径在 promotion 后记录该状态，同时
明确 bounded Product-only context 不独立重验/暴露 receipt、不得再次要求 START。失败/取消等状态不冒充成功。
它仍不含 Review/Promotion id、Patch、digest、revision、路径、Verifier 或失败正文；Product control plane 从不
读取它。请求仍只凭 Session `source_seq` 重建，没有 request-time cross-stream join。
旧 status-only format 1 与旧跨 owner 文案 format 2 都明确拒绝且零改写，无多版本 parser、迁移或 fallback。

后续真实体验证明 format-3 已经把最新 completed 正确交给模型，但模型又把一个 selected task 推成完整任务
inventory、把 requester Workspace 路径推成 Product 执行路径并复述 XML wrapper。当前 format 4 因此移除
provider-visible XML wrapper，新增 selected-not-inventory 与 Workspace/明细省略范围，并明确允许自然总结和
合理推断，只要求区分宿主事实、推断和未提供的具体信息。旧 format 1/2/3 全部明确拒绝；事实源、owner、
控制权限、selection 算法、Session replay 与 `2048` 字符上限均未改变。

本检查点实际运行：

- context protocol + Surface：**`30 passed`**；
- Product context + manual Compaction：**`15 passed`**；
- Product observation/config/CLI 相邻合并组（含上组）：**`121 passed, 1 skipped`**；
- 完整 `tests/test_product_f3_e2e.py`：**`24 passed`**；
- Product Benchmark 真实本地 Git 主线与模型敏感值反例：**`2 passed`**；
- Model admission + Recovery：**`26 passed`**；
- 当前解释器（未收集 optional Textual Pilot module）全仓 collect-only：**`2573`**；
- `python -m compileall -q src tests`、修改范围 Ruff、`git diff --check`：通过；
- 独立审查先发现 `format_version=True` 可利用 Python `True == 1` 穿过 exact parser 的 P2；修复为类型/值
  双重精确检查并补反例后，复审为 **`P0=0 / P1=0 / P2=0`**。

定向反例还覆盖 no-task、completed/failed、restart idempotency、新 task/head 覆盖、旧 head 晚 append、
非 canonical payload、JSON 类型敏感 commit reconciliation、Session CAS、普通 Store read failure、取消前
未写、append 已提交未返回，以及一次/二次 manual Compaction 后宿主状态证据仍在 Surface 且 request
snapshot 可重建。真实本地 Git next-request E2E 证明批准到 completed 后下一份 Provider request
正好包含一条安全 status context，并通过 `verify_request_snapshots()`。

本轮未运行完整 pytest、联网、真实 Provider/API、Wheel、离线安装、commit、tag、push 或 release；上节
历史全量仍不能认证当前工作树。

语义跟进检查先把新断言跑在旧 status-only 逻辑上，得到预期 **5 failed**：completed/failed/cancelled 都缺少
关系和执行主体，真实本地 Git 的下一份 `ModelRequest` 也缺少 completed 语义，format 1 仍会被接受。补齐
语义后的 format 2 随后被短复审发现 STARTED/COMPLETED 跨 owner 过度声明；收窄 canonical message 后，协议
复查又证明继续复用 format 2 会让旧合法 event 与新 context id 别名并破坏 replay，因此当前版本硬切为 format 3。

- 完整 Product model context + manual Compaction + 完整 Product F3 E2E：**`50 passed`**；
- Surface/Core invariants + Product architecture：**`33 passed`**；
- 全仓 collect-only：**`2589 tests collected`**；当前解释器仍未收集 optional `tests/test_tui.py`；
- `python -m compileall -q src tests`、修改范围 Ruff、anti-hardcoding scan、`git diff --check`：通过；
- scanner 在两个生产文件中没有发现 demo/example term；非 demo 的 256 字符 task identity 覆盖所有九种
  status 并保持 canonical message 不超过 2048 字符。

独立短复审随后发现两项不会改变权限或持久事实、但会夸大 Product-only 证据的 P2：STARTED 被写成 Workflow
已经 durable start，COMPLETED 被写成 bridge 已经解析 Promotion receipt；另有文档把“请求明确携带语义”写成
“模型必然遵从”。实现和文档已统一收窄到上面的证据边界，新增断言禁止这两种跨 owner 过度声明。修后结果
见本节最终门禁，不以真实 Provider 行为冒充确定性测试结果。

上面的 `30/15/121/24/...` 数字只证明 format-1 检查点，`50/33/2589` 只证明被替换的 format-2 检查点，
都不能冒充后续 format-3/4 候选认证。本轮仍未运行
完整 pytest、联网、真实 Provider/API、Wheel、离线安装、commit、tag、push 或 release。

format-3 修后历史门禁：

- Product context + manual Compaction + 完整 Product F3 E2E：**`51 passed`**；
- Surface/Core invariants + Product architecture：**`33 passed`**；
- 全仓 collect-only：**`2590 tests collected`**；当前解释器仍未收集 optional `tests/test_tui.py`；
- `python -m compileall -q src tests`、修改范围 Ruff、anti-hardcoding scan、`git diff --check`：通过；
- 最大合法 identity 下，completed/started canonical message 分别为 `1752/1614` 字符，均小于 `2048`；
- 三处旧跨 owner 文案的反向验证为预期 **`3 failed`**；临时把协议版本退回 2 时，legacy format-2
  identity 隔离反例为预期 **`1 failed`**。恢复 Product-only 语义和 format 3 后上述门禁全绿。

这组数字在 format-4 表达边界修复后只保留为历史证据。

format-4 当前门禁：

- Product context + manual Compaction + 完整 Product F3 E2E 首次运行发现一处只匹配旧
  `status: completed` 文案的测试断言；同步为新自然语言消息后完整重跑：**`52 passed`**；
- Surface/Core invariants + Product architecture：**`33 passed`**；
- 全仓 collect-only：**`2591 tests collected`**；当前解释器仍未收集 optional `tests/test_tui.py`；
- `python -m compileall -q src tests`、修改范围 Ruff、anti-hardcoding scan 与 `git diff --check`：通过；
- 最大合法 identity 下，completed/started canonical message 分别为 `1886/1748` 字符，均小于 `2048`；
- 临时移除 selected-not-inventory 说明时九种状态合同得到预期 **`9 failed`**；临时把新文案错误保留为
  format 3 时，旧合法 format-3 event 与新消息得到相同 context id，identity 隔离反例得到预期
  **`1 failed`**。恢复完整语义和 format 4 后上述门禁全绿。

format-5 历史门禁（已被 format 6 替代）：

- 真实冻结请求审计确认 format-4 completed context 已进入请求，但位于旧 assistant “waiting START”之后；
  模型随后复述旧状态。durable Product head、bridge、request snapshot 与 Provider dispatch 均正确，根因是
  Surface 对当前宿主事实和历史模型自述没有结构化优先级；
- 修复前先加入冲突 unit 与真实 Product F3 首项反例，两项都按预期失败；修复后 canonical Product system
  message 成为 `ModelRequest.messages[0]`，其余 conversation 内容和内部顺序不变；
- OpenAI-compatible HTTP capture 证明 wire 依次保留既有 composition system、当前 Product system、旧
  assistant 与当前 user；adapter 没有合并、删除或改写消息；
- focused conflict/protocol/F3/Compaction/Provider 组：**`17 passed`**；Product context、两次 Compaction、
  完整 Product F3、OpenAI-compatible、Surface/Core invariant 与 Product architecture 六模块合并组：
  **`111 passed`**；
- format 5 参与 context identity，旧 format 1/2/3/4 全部 fail closed、零迁移、零 fallback；两次人工
  Compaction 后 canonical context 仍为 Surface 第一项，request snapshot 可精确重建；
- 最大 256 字符 task identity 与 SQLite 最大有符号 64-bit `source_seq` 下，completed canonical message 为
  **`2008/2048`** 字符；协议不会生成自己拒绝的合法边界消息；
- `python -m compileall -q src tests`、修改范围 Ruff、anti-hardcoding production scan 与
  `git diff --check`：通过；当前解释器全仓 collect-only 为 **`2620 tests`**；正式/通俗版编号章节均为
  `0..20`，本轮文档围栏闭合且相对链接存在。
- 三路独立只读短复审分别检查事实源/replay/Compaction、Provider-neutral request/wire 合同和
  legacy/长度/伪绿边界，结论一致为 **`P0=0 / P1=0 / P2=0`**；其中 Provider 复审独立复跑四条公开路径为
  **`4 passed`**。复审确认当前没有证据要求在 OpenAI-compatible adapter 合并两个 leading system message。

本轮仍未运行完整 pytest、联网、真实 Provider/API、Wheel、离线安装、commit、tag、push 或 release；也不把
确定性的请求内容断言冒充外部模型必然遵从。当前修复明确允许自然总结和合理推断，只要求区分宿主事实、
推断与未提供的细节；历史对话也完整保留。修复只能减少上下文冲突，不能保证外部模型百分百遵从。

### 4.10 N10–N11：完整 Patch 摘要与按需改动页

默认 Product observation 原先只暴露有界 Patch preview，不能用它计算完整 `+/-`，否则截断后的数字会看起来
真实却是错误证据。N10–N11 没有放宽这个 preview，也没有把完整 bytes 塞进长期 observation；实现复用既有
`PatchArtifactReader.load()`：从 Review 精确绑定 Artifact，fresh 重放 `artifacts:catalog`，再从内容寻址 CAS
读取原始 bytes 并校验 sha256/size。SQLite/EventStore 仍是身份与生命周期的唯一事实源，CAS 仍只保存既有
不可变 Patch 内容；没有新增缓存、durable 写入、状态机或第二 reader 主线。

统一 diff parser 从完整 bytes 生成只读 summary：总字节、文件数、逐文件新增/修改/删除/重命名、二进制标记
与可靠的 `+/-`。默认右栏只显示 summary，不再显示 diff 正文。畸形 Patch fail soft：manifest 文件身份仍可
显示，无法证明的状态/计数为 unknown/`?`；二进制不冒充文本行统计。parser 还保留 mode、无末尾换行与 Git
C-style quoted path 等 metadata，不能因为界面精简而丢证据。

`Ctrl+D` 每次打开都调用 `ProductObservationReader.load_patch()` fresh 重走 task/Review/Workflow/Artifact
校验链，展示完整 Patch：默认展开第一个文件，上下键选择、Enter 展开/折叠、Esc 返回；hunk header 转为
旧/新行号，新增/删除/上下文/metadata 使用既有七色以内的语义。页面没有 diff 行数上限；每个物理行先走
terminal-safe 转义，再按当前 Rich cell width 手工换行，续行保留八列前缀。`Ctrl+E` 写出的具名临时
`.patch` 与校验后的 CAS bytes 逐字节相同，导出文件不进入 SQLite、observation、模型上下文或状态机。
读取、解析或导出失败只显示稳定错误，不展示 raw exception。

当前定向门禁：

- `tests/test_tui.py` + `tests/test_tui_presentation.py`：**`50 passed`**；
- `tests/test_unified_diff.py`、Product observation、inspection leaf、TUI optional 与 Product architecture：
  **`56 passed`**；
- 安装 Textual 8.2.8 的解释器全仓 collect-only：**`2634 tests`**；
- `python -m compileall -q src tests`、修改范围 Ruff、anti-hardcoding production scan 与
  `git diff --check`：通过；
- 独立只读复审在关闭 malformed observation、首帧 auto-scroll、重复快捷提示、metadata 保留、长行续行与
  文件标题横线二次折行缺口后，
  结论为 **`P0=0 / P1=0 / P2=0`**。

一次较宽的组合 Pilot 在两个不同运行中各观察到一项既有 Textual 调度时序断言（跨双栏重排、短日志首帧
bottom anchor）；前者随后五个独立进程全部通过，后者的根因是 `RichLog.lines` 已建立但下一帧尚未绘制，
测试在读取 `render_line()` 前增加一次 Textual idle-frame 同步，不改生产 auto-scroll。
重复真实 auto Product host Pilot 还定位到一条测试同步缺口：业务 confirmation Turn 已结束、按钮
`display=True` 时，下一帧布局可能尚未分配 region；旧测试先冻结 `(0,0)` 坐标再点击，并忽略
`pilot.click()` 的 `False` 返回值，随后才误报确认框未获焦点。修复只在该测试点击前等待 Textual idle frame
并断言点击成功；生产 Button/focus 路径零 diff。修后该公开路径连续十个独立进程及完整直接组均通过。
这里不把测试调度说成生产通过证据，也没有为同步增加产品状态。当前未运行完整 pytest、联网、真实
Provider/API、Wheel、离线安装或发布门禁；本批曾停在真实截图确认点，随后用户确认后才进入 4.11。

### 4.11 N12 + R4：任务对话完整出口与最终分区

N12 关闭的是三层同根截断：screen 不再取 `role.messages[:12]` 或显示遗漏数量，`RichLog` 删除固定
`max_lines`，`TaskConversationReader` 对 user/assistant message 调用同一 `safe_display_block()` 时显式关闭
字符、行数与单行宽度限制。安全函数的默认参数仍然有界；只有明确的“看全部”出口使用无界值，并继续把
ESC、CR、NUL、bidi 与 U+2028/U+2029 等不安全字符转成可见转义。Reader 仍只 fresh read observation 已
绑定的 Session/Effect streams，screen 只消费一次 snapshot，没有缓存、全 Store 扫描、第二 reader、写事实
或新状态机。

R4 把角色段头改成全宽横线内嵌角色与 turns/tools/tokens/age，当前段使用既有强调色、其他段 dim，不使用
反色背景。正常宽度保持一行；44 列放不下时，统计按 Rich cell width 手工折到后续行，完整字段仍保留且
`max_scroll_x=0`。展开内容缩进两格，需求正文缩进四格并预折行；模型统一为 `模型 ·`，连续多个空行压成
一个。默认 Session id 使用短把手，完整值仍来自既有 `Ctrl+P` 身份页。重复底部说明与固定分隔线删除，
标题右侧说明“打开时快照 · 不实时 tail”，44 列使用等义短句“快照 · 非实时”。

定向反例覆盖 20 次工具调用全部可见且没有“还有 N 条”、65 行末尾哨兵、超过 4000 字符的模型行、危险
Unicode/control 转义、窄屏完整统计无水平滚动、连续空行压缩、四格需求折行、短 Session handle，以及
2105 条消息保留首尾并可按 End 滚到底部。2105 条 Pilot 的 call 阶段约 **1.47 秒**，因此没有证据要求实现
工单只在真实卡顿时才允许的增量渲染；也没有退回任何截断。

当前最终定向门禁为：TUI/presentation/task-conversation **`65 passed`**，统一 diff、Product observation、
inspection leaf、TUI optional 与 Product architecture 相邻组 **`56 passed`**，全仓 collect-only
**`2638 tests`**；`python -m compileall -q src tests`、修改范围 Ruff 和 `git diff --check` 通过。production
anti-hardcoding scan 仅命中 `ChatDriver` 的导入和构造两处；它是 UI-neutral 的通用架构类型，经人工分类
不是任务、fixture、模型或本机路径硬编码。一次整组重跑曾在动态 START→Cancel 布局后丢弃
`Pilot.click()` 的未命中返回值，因而误等焦点；产品闸门与 focus 代码未改，测试改为先等待一帧并断言真实
鼠标命中。该用例随后连续 **`12/12`**，完整 65 项重跑通过。独立只读复审为
**`P0=0 / P1=0 / P2=0`**。

完整 pytest、联网、真实 Provider/API、Wheel、离线安装和发布门禁仍不属于本次 M1 提交。

### 4.12 M2 第一检查点（历史）：format-6 同 Session 有界 ProductTask 目录

真实体验证明 format-5 已能把当前 durable Product 状态放在旧聊天之前，但它故意只选择一项任务；同一
requester Session 连续完成多个 ProductTask 后，较早任务仍在 EventStore 中，却不再进入后续模型请求。
因此根因是渐进披露投影过窄，不是 ProductTask 状态丢失，也不是缺少第二套 Memory 数据库。

M2 在原 Product Chat → Session Surface bridge 内破坏式切到 format 6。每次 requester Turn 前，宿主 fresh
重放同一 EventStore 中全部 `product-task:*` heads 与当前 Session 的 origin/confirmation evidence：唯一 live
task 优先；否则以 confirmation `inbox/accepted.seq` 最大的 terminal task 为 focus；其余任务按该 seq 降序。
focus 加近期历史总计最多六项，另存精确 `total_tasks` 与 `omitted_tasks`。多个 live、重复排序、origin/确认
Session 混属、消息缺失或 Turn 错配都 fail closed，不按时间戳、task id 或 stream 枚举顺序猜测。

宿主只 append **一条** schema-1 `product/context-snapshot`，其 context identity 覆盖完整目录。该事件原子生成
两条有序模型消息：system-role 当前 focus 事实先于完整旧对话，user-role 历史参考紧随其后。每项 source
request excerpt 来自已经验证的 `inbox/accepted.content`，按 canonical JSON 字符表示限制在 320 字符并记录
截断；它以 JSON string 出现，且明确只是历史 requester 原文，不是 canonical requirement、当前指令或
START/批准/推广权限。provider-visible 消息不暴露 requirement digest、Patch/Review/Promotion identity、
路径、命令、Tool output 或 Verifier 明细。

ProductTask streams 仍是生命周期权威，Session 事件仍是请求重建证据；Product control plane 从不反读该
快照。没有新增 Memory stream、缓存、LLM 摘要、FTS、embedding、RAG 或跨 Session/Workspace 检索。
Session CAS 冲突会重新 fresh 读全目录；取消和 may-have-committed append 继续先收敛再返回。Compaction 不
替换该收据，`request/snapshot.source_seq` 因而仍可只依赖 Session 精确重建当时请求。parser 只接受 format 6；
旧 format 1–5 零迁移、零 fallback，需使用新 data-dir。决定见
[`ADR-0040`](adr/0040-session-scoped-product-task-history-context.md)。

当前实际门禁：

- `tests/test_product_model_context.py` + `tests/test_compaction.py`：**`39 passed`**；覆盖多个 terminal、唯一
  live focus、跨 Session 排除、混属/缺失/错配 evidence、多个 live、重复 accepted seq、六项上限、准确
  omitted count、恶意摘录转义/截断、format 1–5/布尔版本拒绝、CAS、JSON 类型敏感对账及取消收敛；
- 再合并完整 Product F3 E2E、OpenAI-compatible Provider、Surface/Core invariants、Product architecture
  与 Product service 的七模块相邻组：**`166 passed`**；其中一条旧 format-5 单消息文案断言先真实失败，
  更新为 format-6 原子双消息合同后，单项与整组均重跑到绿色；
- 安装 Textual 8.2.8 的解释器全仓 collect-only：**`2647 tests collected`**；
- `python -m compileall -q src tests`、修改范围 Ruff、`git diff --check` 与 production anti-hardcoding scan：
  **通过**；正式/通俗上下文编号均为 `0..20`，本轮 Markdown 围栏闭合且变更文档的相对链接均存在；
- 临时把目录选择退回只保留 focus 时，多任务连续性反例按预期 **`1 failed`**；恢复最多六项选择后该反例
  重新通过，证明测试真实经过生产投影；
- 独立只读审查检查单一事实源、跨 Session evidence、排序/focus、CAS/取消、恶意历史文本、canonical
  protocol 与 Compaction/replay，结论为 **`P0=0 / P1=0 / P2=0`**。

完整 pytest、联网、真实 Provider/API、Wheel、离线安装、commit、tag、push 与 release 均未运行或执行，
不以确定性请求断言冒充外部模型必然记住或服从。

### 4.13 M2 完成检查点：format-7 最小摘要与按需证据

format 6 解决了“同一 requester Session 完成多个 ProductTask 后较早任务从请求中消失”，但真实体验继续
证明，仅给出 task/status 仍会让模型从 requester Chat 自己没有执行 Tool 的历史中错误推断“宿主管理的
Product 工作没有发生”。Product/Workflow/Promotion durable facts 没有丢；缺口仍在模型感知投影，而不是
控制状态或事实存储。

M2 最终在同一条 Product Chat → Session Surface bridge 内破坏式切到 format 7。它保留 format 6 的当前
focus、最多六项同 Session 任务、精确总数/省略数和原子 system + user 两消息；只为
`awaiting_approval`、`completed`、`rejected`、`cancelled`、`failed` 的当前 focus 增加六项宿主验证的最小
执行摘要：Workflow 状态、managed Tool-call 数、changed-path 数、verification 结论、Verifier 数量和
Promotion 是否记录。历史任务以及 `opened`、`routed`、`started`、`abandoned` 不带摘要，避免把大量执行
细节默认灌入每轮请求。

Product-configured requester Chat 同时增加一个 `PURE_READ` Tool：`read_product_task_evidence`。调用方必须
给出精确 task id；reader 每次从 durable Product origin/confirmation 与 requester Session 事实 fresh 证明
同 Session 关系，再连接 Workflow、Agent、Session、Artifact/CAS、Review、Approval、Promotion 与 Budget。
成功结果只给有界结构化事实，包括最多八个 changed paths、每角色最近八次 managed Tool 结果和最多八个
Verifier；完整 canonical JSON 上限 20,000 字符。不存在、跨 Session、损坏或不可读统一返回
`product-task-evidence-unavailable`，避免 task-id 探测。caller cancellation 原样传播，不伪装成 unavailable。
原始 Patch、Tool 参数/输出、模型 prose 和 Product workspace 路径始终不返回；该 Tool 也没有 START、审批、
Promotion、重试或写 workspace 的权限。

所有内容仍由同一个 durable EventStore 与既有 Artifact CAS 拥有。`ProductTaskMemoryReader` 与
`ProductTaskActivityReader` 都是无状态 fresh join，不写 Memory stream、缓存、索引、RAG 或第二状态机。
CLI 因 Tool registry 冻结与进程内 Feed 的装配顺序，分别建立两份不同 Python 实例的
`ProductReadModels` bundle：requester Tool 使用 Runtime 创建前的原 Store，host context/observation 使用
Runtime 创建后的 publishing Store wrapper。CLI 对两次构造传入同一原始 durable log、CAS、Profile、
VerificationPlan、Promotion target 与报告上界，因此当前生产入口按构造保证两者同源同配置；host 不跨
实例比较这两份 bundle，只校验传给自己的第二份 bundle 与 host 一致，并拒绝该 bundle 内部拼接的 mixed
reader chain。Agent activity 还必须属于当前 ProductTask owner subtree；不能从另一个任务借用同名
router/coder 事实。

format-7 memory join 先冻结 Product head，完成跨流读取后再次确认；head 在连接期间变化会让整次 context
sync 从 fresh reads 重启，不能把旧 status 与新 evidence 拼成一张快照。Review target、Promotion review/
target、Artifact CAS、固定 VerificationPlan 与 owner identity 任一不一致都 fail closed。parser 只接受
format 7；formats 1–6 明确拒绝、零迁移、零 fallback、零双 reader，测试与体验必须使用新的 data-dir。
决定见 [`ADR-0041`](adr/0041-session-scoped-product-task-evidence-memory.md)。

M2 最终确定性证据：

- Product memory/model-context/Compaction/observation/inspection/architecture/service/TUI task-conversation 九个
  直接与相邻模块整组：**`172 passed`**；共享 activity reader 另对 Runtime/Recovery 的七种 durable Tool
  结果状态逐项验证，未知值继续 fail closed，`pending` 只用于未配对的在途调用；
- 完整 `tests/test_product_f3_e2e.py`：**`26 passed`**，覆盖真实本地 Git、SQLite、managed worktree、
  Product Workflow/Approval/Promotion、下一请求 format-7 感知、真实 ToolRuntime evidence 调用和混合 bundle
  拒绝；最慢用例 `28.87s`；
- 临时移除 Product owner-subtree 的两处生产校验后，router/coder 两个公开投影反例按预期
  **`2 failed`**；恢复校验后同一组 **`2 passed`**，证明反例确实经过共享 activity reader；
- 临时移除新补齐的四种 Runtime/Recovery 结果状态后，参数化公开读取反例精确得到 **`4 failed`**；恢复
  七状态白名单后，七状态与 owner-subtree 组合 **`9 passed`**，完整任务对话组 **`22 passed`**；
- `python -m compileall -q src tests` 与修改范围 Ruff 已在上述最后代码改动后通过。

第一次无筛选完整 pytest 使用当前用户工作树运行，得到
**`2 failed, 2655 passed, 7 skipped in 1938.85s (32:18)`**。它没有被当成完成证据，两项失败均定位到
当前生产/合同根因：

- real L2 从 Git HEAD 建立 clean core，而 core-only 环境会被 `traceh.tui.__init__` 间接 eager import 的 Rich
  破坏；当前包入口不再 import Rich/Textual，presentation 只在真正需要 cell/Text 的函数内局部 import
  Rich。确定性 subprocess 反例同时屏蔽 Rich/Textual 后仍可 import core TUI boundary 与 safe display；临时
  恢复 eager Rich import 时该反例按预期失败；
- Product host 合法使用 Promotion 域的 `freeze_verification_plan` 与 `verifier_definition_digest`，但精确
  Promotion architecture whitelist 仍停在旧合同。白名单只同步这两个既有纯函数，没有放宽其他 Promotion
  seam；Promotion/Product architecture 组恢复为绿色。

因为 real L2 固定 clone/checkout Git HEAD，而本轮没有 commit 授权，最终验收没有拿“旧 HEAD 内层回归 +
脏工作树外层测试”冒充一份证据。仓库外建立了临时候选提交：唯一父节点是用户仓库当前 HEAD
`2e65613ab5817453ae198367791d6fe1ad0ead6d`，28 个 tracked diff 与明确允许的 5 个新文件组成恰好 33
个候选路径；复制前后 SHA-256 和 Git blob 逐项相同，用户的 `.pytest-tmp-codex/`、学习笔记与
`docs/claude-recmd/` 均未进入，临时提交后快照干净。验证完成后再次核对用户 HEAD 与 33 个 blob，均未漂移。
该快照的独立真实 L2 得到 **`1 passed in 1487.10s (24:47)`**。

随后在同一候选提交、安装 Textual 8.2.8 的开发解释器、全新仓库外短 basetemp 上执行唯一最终全量；不带
筛选、不用 `--lf`，并保留内嵌 real L2：

```powershell
C:\traceh-tui-dev\Scripts\python.exe -m pytest -o addopts='' -q --durations=30 --basetemp C:\th-m2-final2-20260902-2050
```

结果为 **`2665 collected / 2658 passed / 7 skipped`**、退出码 0、耗时
**`3226.49s (53:46)`**；最慢项正是内嵌 real L2，`1583.65s`。七项 skip 仍是已有 Windows 平台边界。
当前 M2 直接/相邻九模块为 **`172 passed`**，完整 TUI/optional/presentation/task-conversation 为
**`82 passed`**，Product F3 E2E 为 **`26 passed`**，全仓 collect-only 为 **`2665 tests`**。最终独立代码
复审与文档校准后均为 **`P0=0 / P1=0 / P2=0`**；compileall、修改范围 Ruff、anti-hardcoding、
`git diff --check`、受保护核心零 diff、文档围栏/链接/两版同步门禁均通过。

全量结束且候选身份复核完成后，只同步了 README、CHANGELOG、本验证记录和正式/通俗两份上下文：写入
上述真实结果，并关闭 bundle 描述、旧 checkpoint 时态和通俗 Mermaid 的 docs-only P2；production code 与
tests 未再变化。随后重新运行文档围栏、相对链接、20.38/20.32 映射、Mermaid 当前语义、秘密/本机路径和
`git diff --check`，全部通过；没有用文档写入后的重复全量冒充另一轮候选验证。

联网真实 Provider/API、发布用 clean-input Wheel/sdist/source ZIP、双形态离线安装、commit、tag、push 与
release 不在本次 M2 完整验收授权内，继续保持 **NOT RUN**。临时 L2 自己按现有门禁构建/安装候选与核心
Wheel，不等同于执行这些尚未授权的发布资产门禁。

## 5. 真实 Provider 与秘密边界

完整 18-attempt 真实 Provider acceptance 需要用户另行授权；当前 **NOT RUN**。若获授权，只允许使用全新
SQLite data dir 和全新 eval output，一次完成，不补跑失败项、不 fallback、不覆盖历史报告，不打开、
打印、复制或记录 `.env` Key。历史 v0.7 网格不能冒充 v0.8 retry/SQLite/TUI 候选的发布证据。

当前已完成上节一次定向真实 TUI ProductTask acceptance：通过既有 loader 使用 `.env`，但没有打开、
搜索、打印、复制或记录 Key；进程代理显式置空；只使用本地 source 与一次性 bare target，没有真实远端
Git。它不替代本节仍为 NOT RUN 的完整 18-attempt 网格。用户未跟踪笔记、缓存与审查目录不进入候选提交
或 source ZIP。

## 6. M3：宿主自动上下文压缩与精确重放（另行授权的后续阶段）

本节只认证 M3 工作树，不覆盖或替代上面的 v0.8.0 候选证据。设计决定见
[ADR-0042](adr/0042-host-owned-automatic-surface-compaction.md)，当前事实见
[`project-context.md`](note/project-context.md) 的 12.2 与 20.39。

### 6.1 已执行并通过

```powershell
python -m compileall -q src tests
python -m pytest tests/test_compaction.py -q -o addopts='' -p no:cacheprovider
python -m pytest tests/test_surface_and_invariants.py tests/test_cli_timeline.py `
  tests/test_cli_read_only_commands.py tests/test_cli_env.py -q -o addopts='' -p no:cacheprovider
python -m pytest --collect-only -q -o addopts='' -p no:cacheprovider
python -m ruff check <修改范围文件>
git diff --check
```

- 定向组（`tests/` 中 `test_compaction.py`、`test_surface_and_invariants.py`、`test_cli_timeline.py`、`test_cli_read_only_commands.py`、`test_cli_env.py`、`test_product_architecture.py`、`test_cli_resume.py`、`test_cli_resume_safety.py` 八个文件）：**`470 passed, 1 skipped`**；唯一 skip 是
  `test_cli_resume_safety.py` 的 “a path cannot contain NUL”，属既有 Windows 平台边界。
  其中 `tests/test_compaction.py` 单独为 **`41 passed`**。覆盖未达阈值零新增事件、显式禁用、部分配置被拒、
  只压缩闭合且保留最近 Turn 的前缀、开放 Turn 不入 source、tool call/result 同进同出、format-7 Product
  context 永不入 source、多次压缩只剩一条摘要、摘要不越过当前用户消息、压缩前后请求逐字节重建、replay 不
  调用摘要器、摘要期间 head 变化与 CAS 冲突、摘要器失败/写入失败/取消收敛、摘要器无 Tool 与控制权限、
  敌意与中文摘要的限长与转义、Line/TUI 同源展示且不泄漏正文、手动压缩同协议与拒绝 Turn 内切口，以及
  四条不变量反例（非闭合 Turn 切口、遮蔽 Product 证据、拆开 tool 配对、旧 format 1）。
- 相邻 owner 回归组（Product model-context/memory/observation/F3 E2E、recovery、inspector、runtime
  e2e/factory/dispose、budget enforcement/supervision、TUI 四套、CLI chat/run-dispose/activity、cancellation、
  event feed、event store contract、model attempt admission/retry、plugin runtime、composition generations、
  agent supervisor 共二十六个文件）：**`526 passed, 1 skipped`**。唯一 skip 是 `test_tui.py` 的模块级
  `importorskip("textual")`：本机解释器没有安装可选 `tui` extra。这是环境边界，不能写成平台边界。
- `pytest --collect-only`：**M3 检查点当时**本机 **`2681 tests`**，装有 `tui` extra 的解释器
  **`2714 tests`**。两个数字都正确，差额完全来自上面那条模块级 `importorskip`；本文件引用收集数时一律
  注明解释器是否安装该 extra。**这两个数只认证 M3 检查点**：M4 之后又新增了 Context 透明度用例，当前工作树
  的收集数见 7.1。
- **修改范围 Ruff**（`git diff --name-only` 的 `.py` 文件加新增的 `session/surface_replacement.py`）只有
  **一条** `ASYNC240`，位于 `runtime/agent_runtime.py` 中本轮未触碰的既有代码，并在干净 `HEAD` 上
  `git stash` 后独立复现，属既有基线。按整包目录扫描另会带出 `runtime/continuation.py` 的 `E501` 与
  `session/projections.py` 的 `UP042`，但这两个文件本轮没有改动，不计入修改范围。
- `tests/test_product_architecture.py` 的受保护核心 pin 中，`runtime/agent_loop.py` 与
  `runtime/agent_runtime.py` 两条按该文件自身的约定在同一次改动中更新，并在其 docstring 中写明原因
  （Turn 前压缩线性化点、非致命失败处理、显式 CompactionPolicy/summarizer 装配与单一 owner 守卫）。

### 6.2 反向验证

依次临时破坏以下保护，确认对应测试按各自根因变红，然后逐一恢复并重新跑绿：

| 临时移除的保护 | 变红的测试 |
|---|---|
| 闭合 Turn/Step 边界（cut 取任意 seq） | 8 项，含只压闭合前缀、Product 证据、逻辑位置、请求重建、head 竞争、摘要器可见范围、UI 展示、手动拒绝 |
| Product context 排除（把该类型加入模型可见集合 + 去掉不变量检查） | 3 项，含 `surface-replacement-source-type` 反例 |
| request reconstruction 的历史 `source_seq` | 4 项，含压缩前后逐字节比较 |
| Session CAS（`expected_seq` 改为 `None`） | 1 项，摘要器只被调用 1 次而不是 2 次 |
| 逻辑位置排序（改回按 append seq） | 1 项，摘要跑到当前用户消息之后 |
| tool call/result 配对规则 | 1 项，`surface-replacement-tool-pairs` 反例 |
| 摘要器能力边界（`SummaryRequest` 增加 `tools` 字段） | 1 项 |
| 写入顺序改回逻辑顺序（P1-1 根因） | 2 项，手动与自动两条扩大 cut 的路径都报 `compaction-payload-invalid` |
| 不变量对派生事实的重算（P1-2 根因） | 2 项，含完整前缀反例与伪造 digest/字节数反例 |
| `committed` 三态折叠成一句话（P1-3 根因） | 3 项，含 `true`/`null` 与 `0`/`"false"`/缺失 |
| 恢复命令携带压缩策略（P2-1） | 1 项 |
| 手动切口必须精确命中闭合 Turn（P2-2） | 1 项 |

复审中另发现一处“摘要完成后再读一次 head 与选择时比较”的保护与 Store CAS 完全重复：移除它时没有任何测试
变红，也答不出“删掉后哪条公开合同会被破坏”。按最小保护原则删除，改由 `expected_seq` 单独承担该不变量，
随后按上表重新对 CAS 做反向验证。

### 6.3 独立复审 Finding 的处置

第一轮独立复审给出 P0=0、P1=3、P2=2，全部按各自 owner 根修并补了确定性反例：

| Finding | 根因与根修位置 | 反向验证 |
|---|---|---|
| P1-1 二次扩展压缩稳定失败 | 选择按逻辑顺序而协议要求 seq 升序；`surface_prefix()` 现在把“交给摘要器的逻辑顺序”与“写入及 digest 的升序”分开 | 2 项 |
| P1-2 format-2 精确绑定未被验证 | `CoreInvariantChecker` 现用同一个 `surface_prefix()` 重算完整前缀、digest 与两个字节数并逐项比对，新增 `surface-replacement-prefix` 与 `surface-replacement-derivation` | 2 项，另有一条“诚实派生零违规”的正向断言 |
| P1-3 unknown/committed 被说成“历史未改变” | `cli/timeline.py` 与 `tui/presentation.py` 按三态分别渲染，只有精确布尔 `False` 才是“未改变” | 3 项 |
| P2-1 恢复命令静默关闭压缩 | `cli/chat.py` 的 resume token 列表补齐四项非秘密整数 | 1 项 |
| P2-2 人工 cut 合同自相矛盾 | 统一为“必须精确命中闭合 Turn”，新增 `compaction-boundary-not-closed-turn`；代码、CLI、README、CHANGELOG、ADR 与测试同步 | 1 项 |

### 6.4 本轮 NOT RUN

完整 `python -m pytest -q`、Wheel/sdist/source ZIP、双形态离线安装、真实 Provider/API、L2–L4 递归门禁，
以及 commit、push、tag、release，均未在本阶段执行，也不由本节宣称。M3 引入的是破坏式 `surface/replace`
format 2 协议切换，旧 format-1 数据必须使用新的数据目录。

## 7. M4：上下文透明度与最终体验（另行授权的后续阶段）

本节只认证 M4 工作树，不覆盖第 6 节的 M3 证据，也不认证任何发布门禁。工程事实见
[`project-context.md`](note/project-context.md) 的 12.3 与 20.40。M4 是纯展示层改动，没有新增
durable 协议，因此不新增 ADR。

### 7.1 解释器边界

`tests/test_tui.py` 顶部是模块级 `pytest.importorskip("textual")`。本机默认解释器没有安装可选 `tui`
extra，因此 TUI 相关门禁一律在已安装 Textual 8.2.8 的 `C:\traceh-tui-dev` 解释器上运行，并在下面逐条标明。
两个解释器的全仓收集数因此不同，这是环境差异而不是缺陷：

- 默认解释器（无 `tui` extra）：`--collect-only` **`2719 tests`**；
- `C:\traceh-tui-dev`（Textual 8.2.8）：`--collect-only` **`2765 tests`**。

### 7.2 已执行并通过

```powershell
python -m compileall -q src tests
python -m pytest tests/test_compaction.py tests/test_surface_and_invariants.py `
  tests/test_product_model_context.py tests/test_model_attempt_admission.py `
  tests/test_model_retry.py tests/test_recovery.py -q -o addopts='' -p no:cacheprovider
C:\traceh-tui-dev\Scripts\python.exe -m pytest tests/test_tui.py tests/test_tui_presentation.py `
  tests/test_tui_task_conversation.py tests/test_tui_optional.py `
  tests/test_tui_context_inspection.py -q -o addopts='' -p no:cacheprovider
python -m pytest --collect-only -q -o addopts='' -p no:cacheprovider
python -m ruff check <修改范围文件>
git diff --check
```

- **TUI 定向组 + M4 新增**（`test_tui.py`、`test_tui_presentation.py`、`test_tui_task_conversation.py`、
  `test_tui_optional.py`、`test_tui_context_inspection.py`，在 `C:\traceh-tui-dev` 上）：
  **`133 passed`**。其中新增的 `tests/test_tui_context_inspection.py` 为 **`38 passed`**，
  `tests/test_tui.py` 新增 13 项 Pilot 用例（含窄屏错误态与详情页折行）。
- **Context/M3 相邻回归**（`test_compaction.py`、`test_surface_and_invariants.py`、
  `test_product_model_context.py`、`test_model_attempt_admission.py`、`test_model_retry.py`、
  `test_recovery.py`，默认解释器）：**`144 passed`**。
- **修改范围 Ruff**（`git diff --name-only` 的 `.py` 文件加新增的
  `session/surface_replacement.py`、`tui/context_inspection.py`、`tests/test_tui_context_inspection.py`）：
  只有一条 `ASYNC240`，位于 `runtime/agent_runtime.py` 中本轮未触碰的既有代码，属既有基线。
- `compileall` 与 `git diff --check` 通过；文档相对链接、代码围栏与两版章节对应检查通过。

覆盖范围包括：空 Session、压缩关闭、压缩开启（精确策略与字节分母）、单次/多次/扩大 cut 的压缩、manual 与
automatic 区分、历史策略 digest 不冒充当前策略、压缩失败三态（含 `0`/`"false"`/缺失都算 unknown）、
Product context focus/shown/total/omitted、历史请求按自身 `source_seq` 选 Product context、最近合法
`request/snapshot` 的 composed/dispatch 字节与 fingerprint 与 output ceiling、后续压缩不改变旧请求展示、
Store 读失败/畸形 replacement/畸形 request snapshot 全部 fail closed、只读不写入（多次刷新后事件数不变）、
宽窄屏一行、Ctrl+X 从聊天输入获得焦点时可打开、Esc 返回后输入可用、Footer 只显示已实现按键、自动压缩后
无需重启即更新、`prepare_turn()` 写入后能更新、样式不继承、敌意摘要不破坏排版。

### 7.3 反向验证

依次临时破坏以下保护，确认对应测试按各自根因变红，然后逐一恢复并重新跑绿（恢复后 `114 passed`）：

| 临时移除的保护 | 变红的测试 |
|---|---|
| 历史请求的 Product context 边界改用当前 head | 3 项，含 `test_a_frozen_request_keeps_the_product_context_of_its_own_boundary` |
| 用 durable 压缩次数冒充当前可见摘要数 | 1 项 `test_durable_compaction_count_is_separate_from_visible_summaries` |
| 把 UTF-8 字节标成 token 与通用 context 百分比 | 6 项，含状态条文案合同与 Pilot 用例 |
| 详情页样式不再按 span 限定（标题 bold 继承） | 2 项，含纯投影与真实 RichLog 两层 |
| 最近冻结请求改用当前 Surface 重算 | 1 项 `test_the_frozen_request_is_read_not_recomputed` |

### 7.4 独立复审两项窄屏 P2 的处置

第一轮独立复审为 P0=0、P1=0、P2=2，两项都属展示层可读性，已按各自 owner 根修并补确定性反例：

| Finding | 根因与根修 | 反向验证 |
|---|---|---|
| P2-1 窄屏状态条仍被静默裁断 | 44 列终端下状态条实际只有 40 cells，错误态 51 cells、"压缩失败 + 任务计数" 46 cells 都会被 Textual 裁断。把 `narrow` 布尔换成显式可用 cell 数与逐个测量的候选阶梯；`app.py` 传入真实 `content_region.width`（首次布局前回退到屏宽减 padding），resize 后重新组合；错误态里稳定 code 最后才被牺牲 | 移除 width 拟合后 **14 项**变红（含 8 项纯投影参数化与窄屏 Pilot 错误态） |
| P2-2 详情页 `wrap=True` 未按视口折行 | `log.write(row, width=row.cell_len)` 钉死虚拟行宽，且 `RichLog` 默认 `min_width=78`；44 列下内容宽 36 却排到 60，产生 Footer 未提示的横向溢出。去掉显式 `width=` 并设 `min_width=1` | 恢复默认 `min_width` 后 **1 项**变红（`test_the_context_detail_page_wraps_instead_of_overflowing`） |

新增覆盖：状态条在 110/80/60/44/40/30/20/12 cells 下均 `cell_len <= width` 且严格一行；错误码在
110/60/44/40/34/30 下保持完整；宽度极端不足时显式加省略号；Pilot 在 110/72/44 列验证错误态完整可读，
并在 44 列验证详情页 `virtual_size.width <= content_region.width` 且每行不超过视口宽度。

### 7.5 本轮 NOT RUN

完整无筛选 `python -m pytest -q`、Wheel/sdist/source ZIP、双形态离线安装、真实 Provider/API、L2–L4 递归
门禁，以及 commit、push、tag、release，均未执行，也不由本节宣称。按阶段计划，M3+M4 的唯一一次最终全量
应在独立审查清零 P0/P1 之后运行；由于 `test_tui.py` 的模块级 `importorskip`，最终全量应在安装了 `tui`
extra 的解释器上执行，否则会静默跳过全部 TUI 覆盖。
