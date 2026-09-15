# ADR-0078：一次分工、多个直接助手与按需并发

状态：接受。日期：2026-09-13。扩展 [ADR-0071](0071-required-multi-allocation.md) 的必经分工、[ADR-0072](0072-owned-writable-collaboration.md) 的可写交接与 [ADR-0077](0077-on-demand-concurrent-child.md) 的按需并发；不改写这三份历史决定。

## 原因

ADR-0077 已允许主方派发一个助手后继续自己的工作，记录 071 实测成立。但分工计划只表达一个 `child`，可写路径还显式拒绝第二个助手，完成证据按"唯一 Artifact"校验，因此无法表达"一次分工、多个独立助手"。仅调大 `max_children` 不能得到这个能力。

## 决定

### 分工与身份

计划输入把单一 `child` 替换为非空 `children` 列表，不保留双写：每项含计划内唯一且非空的 `assignment_id`、明确 `role`（`investigator` 或 `patch_author`）、原有 goal/scope/exclusions/deliverable/briefing，可写项另含非空精确相对 `paths`。`main_work` 与 `handoff` 语义不变。N=1 的两种返回行为与今天一致，但旧的 `child` 字段不再接受。

`assignment_id` 只是计划内关联键，不授予任何权限。宿主从真实 owner、原调用身份（session/turn/step/tool_call）与 `assignment_id` 派生每个助手的 agent/session/message 与预留身份；后续所有操作绑定真实 `agent_id` + `message_id`，不接受列表下标、角色名或模型自报的 Artifact。

工作信封同步升级并明确拒绝旧结构：`writable-assignment` 从 format 1 升到 **format 2**，只读 `readonly-investigation` 从 format 2 升到 **format 3**，两者都新增 `assignment_id` 与 `role`，并由同一套严格字段/摘要规则校验。直接 `delegate_investigation`/`followup_investigation` 没有计划条目，宿主用该调用派生的键作为 assignment_id，followup 沿用同一助手已接受的键。Product/host config、Session/Context/SQLite、Promotion/VerificationPlan 协议不变。历史实验保留原库，必须用其冻结源码读取；记录 071 的冻结驱动（提交 `child` 字段）不再适用于新代码。

单一计划工具按授权角色装配：`CollaborationPlanTool` 持有一组 `AssignmentRole`（role → 原 Control、收集工具、说明），只读与可写不再各有一个计划工具类。未装配 patch_author 时 `role` 枚举只含 investigator，计划里出现 patch_author 按未授权角色在派发前拒绝。

### 数量授权与资源

助手数量授权沿用既有 owner：主方账户的 `coder.budget.max_children` 就是本批直接助手数量上限，`_ensure_capacity` 在预留时原子校验累计直接子数，token/步数/工具/时间按子逐份从主方额度中永久划出。计划校验先做可纠正的预检（N ≥ 1、不超过剩余额度），最终以原 CAS 准入为准；预检不替代准入。宿主默认配置的 `max_children` 改为 1，因此默认仍只授权一个助手，要多助手必须由宿主显式提高该值。`max_processes` 不是并发数。

整批先完成字段、角色授权、`assignment_id` 唯一性、数量与可写路径不重叠的校验，再进入派发；语法或未授权角色错误零派发。逐个创建/发送按顺序执行，但不等待前一个助手完成。第 k 个失败时收敛本批已启动的助手、释放未使用预留、保留已产生事实，再返回明确失败；不声称跨 owner 事务，不自动重派整批。成功后生命周期归原 Product owner 树。

### 等待、收集与完成门禁

`dispatch_and_continue` 在整批消息被接受后返回全部精确身份；`await_report` 先派发整批再等待全部，**共享一个截止时间**，不为每个助手重置总时限。主方可按精确 agent/message 收集任意一个已完成助手，不因另一个未完成而阻塞；混合角色时同时开放两类已授权收集工具。pending 只表示未结算，取消一次收集只取消该等待。

交付前门禁对集合检查：每个已接受 assignment 都必须有同身份、已结算且成功收集的 `completed` 报告；任一 pending、失败、遗漏或身份替换都不通过，一份报告不得抵充另一个助手。第一版所有助手都是必需交接，没有自动忽略失败或主方静默接管。

### 多份 Patch

每个可写助手独立捕获、独立完整读取、独立 `integrate_child_patch`；主工作区内的整合仍然串行，沿用原 Effect、前像核对与逐文件回执。计划阶段拒绝声明路径重叠；捕获与整合阶段按实际 `changed_paths` 再次拒绝重叠，不自动选后到者、不做文本 rebase、不自动合并。

完成证据从"唯一 Artifact"改为按 assignment/agent/message/artifact 绑定的一组交接与 applied 回执：同一助手重复收集去重，不同助手不得合并为一项；缺任一 applied 回执、交叉身份或伪造回执都不通过。无关路径上的其他助手改动不使已有回执失效，真正的前像漂移仍然拒绝。最终仍捕获主方汇总 Artifact，经原固定 Verifier、Review 与人工 Approval/Promotion。

## 边界

一层、一次分工、一批助手：不做递归团队、子方再派发、追加或替换助手、动态 DAG、广播、队列调度、自动扩容与自动冲突合并。并发只改变谁在等待，不改变权限、修订、捕获、验证与人工审批边界。数量上限是授权不是要求，收益未测量。
