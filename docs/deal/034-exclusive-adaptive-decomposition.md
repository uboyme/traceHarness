# 034：DA-8 Adaptive 独占拆分界面实验

DA-8 测试了一个比 DA-7 更强、但仍受原架构约束的假设：显式 `adaptive` 主 Agent 的首个 Step 只公开 `decide_task_decomposition`，模型完成一次 `local`/`separable` 决定后，普通工具才出现。独占表面确实消除了“模型先调用读取工具、跳过决定”的失败，但两轮真实 `qwen-plus` 结果证明它仍不能稳定判断任务是否值得拆分。候选已按预先冻结的停止规则撤回。

## 候选如何保持原边界

- Runtime 候选只有一个通用可选 Step 表面接缝：从完整 Generation lease 派生本 Step 可见的冻结 Composition，再在原 Budget Continuation 之后决定是否继续。接缝本身不认识 Product 或拆分语义。
- Product 候选每一步从原 Session 事件重建当前 Turn 是否已有合法决定，不保存可变 `messages`、额外状态表或第二 projector。
- 决定仍是原 `tool/call → Effect → tool/result`。`separable` 在同一外部事务 Effect 内复用 `delegate_investigation`，因此子身份、Workspace、Budget、Supervisor、Inbox、Delivery 与报告仍由原 owner 管理。
- ToolRuntime 候选核对调用是否属于本 Step 冻结请求公开的 Tool。模型伪造已注册但未公开的工具调用时，只记录拒绝结果，不执行。
- 默认 `single`、普通 Chat、调查 Agent、ProductTask、Workflow、Artifact、Verification、Approval 与 Promotion 均未改变，也没有增加 Token、Step、子任务、深度或进程额度。

确定性 Provider 覆盖 local/separable、独占表面、未公开工具、非法或重复决定、两次漏答修复、预算停止、创建/发送失败与取消。显式 Docker Product 测试还走了两条真实主线：两个助手报告被收回后交付，以及未使用助手在产物捕获前被取消并等待整树收敛。这些测试证明候选可以接入现有 owner，不证明真实模型能稳定分类。

## 两轮真实结果

两轮均使用同三类冻结材料：可拆双主题、紧耦合状态链、简单读取；固定 `qwen-plus` 直连、一次 Provider Attempt、场景调用上限和断网 Docker Verifier。没有 baseline、语义裁判、旧 72 题或自动采用。

| 轮次 | 真实调用 | 任务硬通过 | 分类正确 | 完整行为通过 | 关键结果 |
|---|---:|---:|---:|---:|---|
| 首轮 | 22 | 3/3 | 3/3 | 2/3 | 可拆题创建并运行助手，但主方未 collect |
| 收口修复后 | 32 | 1/3 | 2/3 | 1/3 | 可拆题误判 local；耦合题分类对但任务失败 |
| 合计 | 54 | 4/6 | 5/6 | 3/6 | 六次 Budget 收敛，Workspace 泄漏 0，Provider failure 0 |

首轮保存的 `summary.json` 中三条 `first_step_tools=[]` 是分析器错误：它对按 `stream_id, seq` 排序的多 Session 事件直接取了第一个 Composition。只读重查原 SQLite 后，真正包含决定调用的主 Session 三次首步均为 `[decide_task_decomposition]`。保存摘要不改写；修正后的第二轮按主 Session 选择 Composition，三次均得到正确工具面。

首轮还暴露出真实闭环问题：创建助手不等于主方使用报告。候选据此增加了一条通用规则，要求初始助手在主方结束前按决定回执的精确 `agent_id + message_id` collect 到非 pending 报告，或者被显式 stop；Budget、最大步数、验证失败和重复拒绝的停止原因仍优先。这个修复通过确定性及真实 Docker 相邻测试，但第二轮在第一步就把可拆题选成 local，证明主要瓶颈仍是模型分类，不是子生命周期机制。

## 决定

合同要求三题均完成硬验证，可拆题选择 separable、创建并收回助手，另外两题选择 local 且不创建助手。第二轮未满足这些条件，也出现同一类可拆材料在两轮间 `separable → local` 的直接不稳定证据。继续重跑会变成挑选幸运样本，因此停止实验并删除候选 Step 接缝、决策 Tool、运行时接线、工具表面拦截和专用 live driver。

撤回后的生产仍是原自由 `adaptive` 工具面和默认 `single`。架构保护、Product Registry/Assembly、ToolRuntime、委派预算、调查工具，以及真实 Git/Docker Product 的收回与取消路径均重新通过。精简合同、预检和两轮原摘要见[验证目录](../validation-data/dynamic-collaboration/exclusive-decomposition/README.md)。

后续若继续，不应再叠一层判断提示。当前最值得单独冻结的假设是“先允许有界只读侦察，再进入独占 typed 决定，最后执行”，因为首 Step 在没有工作区证据时一次性关闭协作路径本身可能过早。另一个可分离实验是固定同一判断材料比较不同模型的重复稳定性。两者都不应复活本次已撤回代码，也不能新增第二事实源、Product 状态或默认预算。
