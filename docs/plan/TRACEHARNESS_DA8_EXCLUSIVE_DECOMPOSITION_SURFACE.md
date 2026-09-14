# DA-8：Adaptive 独占拆分决策界面

> 状态：实验结束，生产候选已撤回。本文保留本轮冻结目标、边界、停止条件与实际结论。

## 目标

DA-7 已证明：当拆分决策工具与普通读写工具同时可见时，真实模型会跳过检查点，或在可拆任务上直接选择 `local`。DA-8 只验证一个更窄的假设：在显式 `adaptive` Product 主 Agent 的第一个执行阶段，仅展示 `decide_task_decomposition`，接受一次结构化 `local`/`separable` 决策后再展示正常工具，能否让模型稳定完成执行形态选择。

`single`、普通 Chat、只读调查 Agent 保持原执行路径。DA-8 不新增 Workflow 节点，不把一条 Supervisor 消息拆成多个 Turn，也不创建 planner Agent。

## 架构归属

Runtime 增加一个通用、可选的 Step 阶段表面接缝。它只能：

1. 从当前 Step 的完整 Generation lease 派生模型可见的冻结 Composition；
2. 在原 Continuation 已完成 Budget 结算后，决定是否需要继续当前 Turn。

接缝不知道 Product、Adaptive 或分解语义。Product 层提供 DA-8 实现，并且每次从原 Session 事件重建当前 Turn 是否已有合法决策。决定仍由原 `tool/call`、Effect、`tool/result` 记录；没有新表、新 projector、可变消息事实源或第二调度器。

模型只能执行本次冻结 Composition 中公开的工具。ToolRuntime 在原工具准入前核对公开工具名；模型即使伪造一个注册但未公开的调用，也只会获得持久化拒绝结果，不会执行。

## 决策与执行

决策工具固定要求：`decision`、`main_goal`、`child_goal`、`child_deliverable`、`briefing`、`independent_readonly`。

- `local`：主目标非空；三个子工作字段必须为空串，`independent_readonly=false`。
- `separable`：主目标和三个子工作字段均非空，`independent_readonly=true`，主目标与子目标不能完全相同。

`separable` 复用原 `delegate_investigation` 实现，在同一个外部事务 Effect 内创建只读子 Agent 并发送工作消息。Workspace、Budget、Supervisor、Inbox、Delivery 和报告收回继续由原 owner 管理。`local` 接受后，当前 Turn 的普通读写工具恢复，但调查工具不再公开；`separable` 接受后，普通工具与原调查工具恢复。决策工具在本 Turn 不再公开。

漏答、非法参数或调用未公开工具时，最多给两次完全相同的修复提示；第三个决策 Step 仍没有合法决定则本 Turn 明确失败。原 Budget Continuation 先结算每个 Step；预算先耗尽时按原预算原因结束，DA-8 不增加额度。一次 Turn 至多接受一个决定，重复决定明确失败。

## 边界

- 默认模式不变；只有调用方已明确选择 `adaptive` 才启用。
- 不增加工具权限、Token、Step、子任务数、深度、并发或进程额度。
- 不改变 ProductTask、Workflow、Artifact、Verification、Approval、Promotion 或 Git 事实。
- 不根据主题、文件名、题目文字或测试夹具硬编码分类。
- 不自动批准、采用、提交、发布或切换默认模式。
- 不通过追加更多分解提示、改写真实题目或放松 verifier 追分。

## 验证与停止

先以确定性 Provider 覆盖：`local`、`separable`、决策阶段工具独占、未公开工具拒绝、漏答修复上限、非法/重复决定、Budget 先耗尽、子创建/发送失败和取消收敛。再运行 Runtime、ToolRuntime、Product Registry/Assembly、Adaptive、Evaluation 的定向及相邻回归，并执行固定断网 Docker verifier。

真实实验只跑三条冻结场景各一次：可拆双主题、紧耦合链、简单读取。使用既有 `qwen-plus` 直连配置、无 Provider 重试和每场景调用上限；保存请求、Composition、Session、Effect、Budget、Workspace、子报告和任务结果。不开基线、语义裁判、旧 72 题、全量 pytest、L2-L4 或 Wheel。

成功要求同时满足：三个任务均完成硬验证；可拆题选择 `separable`、创建并收回至少一个只读调查；另外两题选择 `local` 且不创建子 Agent；没有未公开工具执行、重复决定、预算或 Workspace 泄漏。若真实模型仍不能分类，或机制破坏现有 owner/收敛合同，保留证据、撤回生产候选并停止 DA-8。

## 实际结果与决定

候选先通过确定性合同与真实 Git/Docker Product 相邻主线。首轮 22 次真实调用中，三题均完成硬验证并作出预期决定；可拆题创建并运行助手，但主方没有 collect。首轮分析器还跨 Session 取错首个 Composition，保存摘要中的空工具列表不是主 Session 事实；只读复核确认三题首 Step 均只公开决定 Tool。

按原题修正分析器，并增加“初始子工作必须精确 collect 到非 pending 报告或显式 stop，原停止原因优先”的通用收口后，在全新目录重冻并复测。第二轮 32 次调用中，三题独占工具面均正确；可拆题误判 local 且任务失败，耦合题判 local 但任务失败，只有简单题完整通过。两轮共 54 次调用，无 Provider failure，六次 Budget 收敛且 Workspace live=0。

第二轮已经命中“真实模型仍不能分类”的停止条件。未再重跑、改题或放宽门槛；Step 接缝、决策 Tool、ToolRuntime 表面拦截、Product 接线及专用真实驱动已删除。原 adaptive 与默认 single 保持。完整解释见[记录 034](../deal/034-exclusive-adaptive-decomposition.md)，精简证据见[验证目录](../validation-data/dynamic-collaboration/exclusive-decomposition/README.md)。
