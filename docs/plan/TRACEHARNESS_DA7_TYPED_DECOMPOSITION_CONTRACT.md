# DA-7：Adaptive 结构化拆分决策合同

> 状态：**真实实验完成，生产候选拒绝并撤回。** 四轮 12 个场景共 68 次真实 `qwen-plus` 调用，没有一次 `separable` 选择或助手创建；12/12 Budget 收敛且 Workspace 无泄漏。本文保留实验前冻结的目标和停止规则，不代表当前生产具备必经 typed 拆分检查点。结果见[记录 033](../deal/033-typed-adaptive-decomposition.md)和[验证数据](../validation-data/dynamic-collaboration/typed-decomposition/README.md)。

## 目标

只在显式 `adaptive` Product 主 Agent 的第一步增加一个必经的结构化判断。模型必须通过 `decide_task_decomposition` 选择 `local` 或 `separable`；不能用普通回复、自由文本计划或先读文件代替。`single` 继续使用原 Prompt、工具和执行路径。

## 归属和事实

拆分决定属于一次主 Agent Turn 的执行证据，写入原 Session 的 `tool/call`、Effect 和 `tool/result`。它不进入 ProductTask 事件，不建立新的状态表或可变缓存。Reader 每次从原 Session 重建决定；Evaluation 只能派生观察，不能更改决定。

选择 `separable` 时，结构化调用必须同时给出主方目标、只读子目标、交付物和 briefing，并明确没有共享状态、顺序证据或未提交修改依赖。程序只能验证字段、界限、显式依赖与两份目标不相同，不能假装理解语义必然正确。验证通过后，由同一外部事务 Tool 调用已经存在的 `delegate_investigation` owner；Workspace、Budget、Supervisor、Inbox、Delivery 和子报告事实仍由原层负责。

## 收敛规则

决定完成前，Adaptive Policy 拒绝读取、搜索、写入、shell 和直接委派；漏答最多给两次相同的固定修复提示，第三次仍没有合法决定则本次 Agent Turn 失败。这个次数由程序常量限制，不由模型自行延长。`separable` 创建的准确 `agent_id/message_id` 必须在主方结束前通过 `collect_investigation` 收到 terminal 报告；pending 不是完成。失败或取消报告可以被收回并由主方如实处理，但不能消失。选择 `local` 后，调查工具继续被 Policy 拒绝；选择 `separable` 后，后续不同调查仍受原 Profile、Budget、深度和并发边界约束。

决定 Tool 至多成功一次；重复决定明确拒绝。决定、委派和收回都使用原 Session/Supervisor 持久身份，取消仍由 Workflow 结束前的原子树收敛屏障负责。决定不增加权限、预算或子任务深度，不自动 Approval、Promotion、提交、发布或切换默认模式。

## 验证

先以确定性 Provider 覆盖 local、separable、漏答修复、重复决定、错误依赖、错 owner/Session、收回 pending/terminal、失败和取消；再跑 Product/Registry/Assembly/Evaluation 相邻回归及真实 Git/固定断网 Docker。最后只执行三类冻结材料的一次真实 adaptive 小样：可拆双主题、紧耦合链、简单读取。固定 Provider/模型/无重试/调用上限，保存请求、Effect、Session、Budget、Workspace 和报告；不跑基线、语义裁判、旧 72 题、全量、L2–L4 或 Wheel。

真实小样要分别报告分类、是否创建助手、是否收回报告、任务结果与成本。若模型仍不能正确分类，保留失败证据并撤回生产接入；不得通过改题、硬编码主题或放松验证制造成功。

## 实验结论

确定性实现证明决定 schema、原 Session 证据、Policy 拒绝、现有只读调查复用和 terminal 收回可以保持既有 owner 边界。真实模型却不能可靠完成必经检查点：它会先调用同时可见的普通工具，或在可拆任务上选择 `local`。通用 schema/说明修正、Tool 排序和两次有界修复都没有产生 `separable`。

这触发了本文冻结的撤回条件。候选生产代码和测试已移除，默认 single 与原 adaptive 不变。后续若研究“普通工具可见前的独占决策界面”，必须另立合同验证生命周期、请求冻结和取消收敛，不能把本文的候选重新描述为当前能力。
