# ADR 0069：执行节点内的按需只读协作

日期：2026-09-11。状态：已接受设计，实施按 DA-0～DA-5；替代 Product 固定 multi/auto 组织方式的决定，不改写其历史记录。

## 决定

ProductTask 保留目标、批准、预算与交付语义，Workflow 保留执行、验证、人工审批尾部。主 Agent 可按任务临时派出只读调查；只有主方写入，子方读取固定 base revision，无孙 Agent、兄弟广播或隐式父历史。single/adaptive 共用原主线，最终删除固定多角色链和旧 Router。

原 Supervisor、Inbox/Delivery、Budget、Workspace、Session/Effect 与 Artifact 继续各自拥有事实。宿主委派工具组合既有公共操作，不创建协作数据库或新的 Runtime 循环。交接是原记录的派生读取；父方结束必须先使子树收敛再捕获产物。

原统一评估区分执行策略对照与同策略说明文本候选对照，核算整树成本，接 Product 自己的语义审阅规则。原 AO 仅获得额外批准的委派说明节点；采用仍由用户决定。

## 原因与代价

动态的是调查目标和组织时机，固定的是身份、权限、生命周期和证据合同。这样能让简单任务零委派，同时避免把聊天、任务状态和费用分散到第二套协作系统。

只读子任务不能审查主方未冻结的最新改动。主方保留额度需要原 Budget 的原子准入支持；交接与整树统计增加成本。收益需要真实 single/adaptive 对照，不能因“有多 Agent”而假定更好。

详细字段、实验控制与阶段门禁见 [DA-0 合同](../plan/TRACEHARNESS_DYNAMIC_COLLABORATION_DA0_CONTRACT.md) 和 [执行计划](../plan/TRACEHARNESS_DYNAMIC_COLLABORATION_EXECUTION_PLAN.md)。
