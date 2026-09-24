# ADR-0082：后台优化收窄为“检测 + 受限建议”，验证交还给人工触发的评测

状态：**已决定**。日期：2026-09-23。修订 [ADR-0068](0068-runtime-background-bounded-optimization.md) 的后台职责；
AO-0 的候选准入、AO-1 的原评估闭环、ADR-0067 的人工采用边界不变。

## 背景

AO-3 的后台宿主在一次“实验”里同时做分析、完整双臂评测与模型审阅。离线复现证明它在真实 TUI 主线上不能持续运行：

1. 每次聊天或 Product 操作经 `foreground(True)` 取消后台实验。轮内记录的观察要等 TUI 空闲 `_pulse`
   （约 1 秒）再 `kick()`；但一次评测要跑完整 trial，前台稍有操作就被取消，实际很难跑完。
   （本 ADR 起草时曾把“`foreground(False)` 不 `kick()`”误判为观察永不启动；TUI 的空闲 pulse 已负责恢复，
   反向验证证实去掉额外 `kick()` 后真实 TUI 路径仍会准入，故未增加该代码。）
2. `BackgroundExperiment` 捕获并吞掉 `CancelledError`，取消被当作普通结算读取。
3. `blocked` 只能由 `settled` 写入，而续批与新周期都拒绝 `blocked`，一次 Provider 抖动即永久停用该工作区的后台流。
4. 周期绑定整棵源码摘要，流 ID 只按工作区；采纳候选、升级或修改设置后 `open()` 永久
   `background-period-settings-mismatch`，恰好让“审阅 → 采纳”这条预期路径废掉闭环。
5. Product 路径只接受 multi/multi 与 `ALLOCATION_GUIDANCE`，默认 Single 任务不产生观察。
6. 自动观察只有“工具被拒/失败计数”，分析模型拿不到可区分的失败机制。

## 决定

### 1. 职责

后台宿主只做两件事：**从已结束的原证据中确定性检测失败机制**，以及**在同一机制跨多个来源重复出现时，
经原 AO 路径提出一份受白名单约束的文本建议**。后台不再运行评测或模型审阅。建议是否值得验证、在哪些题上验证，
由用户用公开的 `traceh eval --run-plan`（`text_candidate`，candidate 变体引用建议产出的 patch 文件）决定并触发。
模型建议不是采用、推广或安装授权。

### 2. 检测

`evolution/detection.py` 从单个已结束 Session 的原事件派生类型化检测，复用评测的
[`context_work`](../../src/traceh/evaluation/evaluators/context_diagnostics.py)，不写新事件、不建新库。
类别是封闭集合：`truncated-response`、`empty-response`、`rerun-after-fold`、`repeated-readback`、
`context-over-limit`、`tool-failed`、`tool-denied`。每条检测附原证据定位（`stream@seq`）与只含计数的说明；
不上传对话正文，不判断答案对错。

### 3. 来源

- TUI 聊天中已结束的 Turn（原有的显式反馈仍保留为 `user-feedback-unverified`）；
- Product 任务（single 与 multi 均可），沿用原 Product/Activity/Inbox 读取器与已结算条件；
- 评测运行目录：先经原 `load_run()` 核对整份证据，且冻结的 benchmark 摘要必须等于本周期设置绑定的题库，
  再按每个 trial 的 session 证据引用只读取原流。

### 4. 聚类触发

观察按原证据身份去重。只有同一检测类别在**至少两个不同来源**（聊天 Session、Product 任务或评测 trial）的
未消费观察中出现，才准入一次建议；准入只消费该类别的观察。用户显式反馈自成一类，同样需要两个来源。
单次出现保持待定，不触发模型调用。

### 5. 建议产出

原 `run_strategy_optimization()` 拆出 `propose_strategy()`：冻结请求、经原插件 Generation Lease 借用
`HostAnalysis`、严格解析、`admit_proposal()` 准入，写 `strategy.json`（`mode=proposal-only`）、`analysis/`、
`proposal.json`，合法候选另写 `candidate.json`（原 AO patch 字节，可被 run plan 的 candidate `source` 直接引用）。
`run_strategy_optimization()` 仍是显式 AO-2 实验入口，由 `propose_strategy()` 加原 AO-1 执行组成，不形成第二套提案逻辑。

### 6. 持久协议（后台流 schema 2）

`optimization-background:<workspace fingerprint>` 流事件升为 schema 2，schema 1 明确拒绝，需使用新的数据目录。
事件：`period-opened`、`enabled`、`observed`、`admitted`、`settled`、`review-dismissed`、`blocked-acknowledged`。

- 预留只剩建议的控制 token；周期不再有 trial 上限。
- 源码或设置变化后，`open()` 不再失败，而是把状态标为 `stale`，拒绝新的准入；用户显式批准新周期时以当前
  源码与设置写入新的 `period-opened`，旧周期的去重历史继续生效，额度不因重启或变更而重置。
- `blocked` 由用户的 `blocked-acknowledged` 解除（记录说明与原证据定位），已预留额度不退还。

### 7. 生命周期

前台期间不准入新建议，恢复准入仍由 TUI 空闲 pulse 调用同一 `kick()` 负责。已在运行的建议是一次有界分析
调用，在独立插件 Runtime 中执行、不共享工作区或 Session，前台操作**不取消**它：取消会让用量未知并阻塞宿主，
每次聊天都需人工核实。退出应用或暂停时仍取消，取消原样传播，结算记为 `cancelled-unsettled` 并阻塞，
等待原 Runtime 与存储收敛后返回。

## 后果

- 后台能在真实 TUI 使用中持续产生建议；评测费用只在用户选择验证时发生。
- 旧设置（format 1）与旧后台流（schema 1）被拒绝，不迁移、不自动删除。
- 固定题库成绩仍不能证明修复了某条未标注的真实反馈；建议的价值只能由用户选定的评测回答。

## 被否决的方案

- **保留后台评测、只修取消**：一次评测数分钟到数十分钟，前台优先原则下仍几乎无法完成，且后台自动花评测费用。
- **单次出现即触发建议**：单次 Provider 抖动或偶发拒绝会直接消耗分析调用，并把噪声写进建议。
- **保留 schema 1 兼容读取**：违反 pre-1.0 单一主线约定，也会让旧的永久阻塞状态被静默继承。
