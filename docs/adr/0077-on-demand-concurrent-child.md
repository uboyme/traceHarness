# ADR-0077：按需并发的一主一子交接

状态：接受。日期：2026-09-13。扩展 ADR-0071 的必经分工与 ADR-0072 的可写交接，不改写历史决定。

## 原因

在此之前，`submit_collaboration_plan` 内部自带等待：宿主派发助手后在同一次工具调用里等报告（上限 300 秒），主方在这段时间不能做任何自己的工作。`multi` 因此只能串行，"一主一子并行"一直被列为后续范围。用户要求按需并行：主方保留实质工作时可以先派发、继续自己的工作，再显式收报告；已有串行用法和人工审批边界必须保留。

## 决定

计划输入新增可选字段 `handoff`，取值 `await_report`（默认，等于现有串行行为）或 `dispatch_and_continue`。缺省或 `await_report` 时，派发调用仍在内部等待精确报告并返回 `outcome=completed`；`dispatch_and_continue` 时，派发调用在 Supervisor 接受消息后立即返回 `outcome=dispatched` 与 accepted 的 child 身份，不等待、不捕获。未知取值在派发之前按原 schema/`CollaborationPlanInputInvalid` 拒绝，属于可纠正输入。

派发、等待、预算与取消仍归原 owner：`Supervisor.create/send` 派发，`Supervisor.wait_message/report` 提供等待与终局事实，`BudgetLedgerService`/`BudgetEnforcement` 管额度，`Supervisor.dispose`（经原 Product 任务 owner 树）管收敛。并发模式不新增调度器、等待队列、并行状态机或第二事实源；派发调用不再持有等待者，因此也不再由它负责停止助手。

主方的按需等待由既有收集工具承担：只读助手用原 `collect_investigation`（wait_seconds 0–30，pending 不是答案）；可写助手新增 `collect_child_patch`，同样是 0–30 秒有界等待，内部复用原 CaptureGate/PatchCaptureService 先捕获再交回真实 Artifact 身份，重复收集返回同一身份且不重复捕获，未结算消息返回 pending 而不是伪造完成。该工具只授予主方，宿主装配，Profile 不能自行声明。

Step 视图按原事件推导：接受的计划若声明并发，execute 与 review 视图额外暴露唯一一个收集工具（授予 patch_author 时优先 `collect_child_patch`），继续隐藏 delegate/followup/stop/预算决定和计划工具；串行运行的视图与之前完全一致。并发时视图附加说明：助手在并发运行、pending 不是答案、未收集或未完成的报告不能交付。

Continuation 增加交付前的硬门禁：声明并发的任务在本 Turn 结束前，必须存在一次成功收集且 `status`/`reason` 均为 `completed`、agent/message 与派发身份一致的报告，否则 `CollaborationChildIncomplete`。可写完成校验改为接受"等待式派发"或"显式收集"两种同一身份的交接证据，仍要求本 Turn 的 applied 整合回执；两处证据不一致或缺失时拒绝。

## 边界

仍是一层、一个助手、一次派发：并发不等于多助手、递归委派、动态 DAG 或额外预算。并发只改变谁在等待，不改变权限、修订版本、捕获、验证、人工审批和 Promotion 的任何边界；助手报告仍是带证据的主张。工程机制通过定向验证，但收益（时延或质量）未测量，也未用真实模型验收。
